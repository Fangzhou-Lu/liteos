/*
 * Copyright (c) 2024-2026 Huawei Device Co., Ltd. All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without modification,
 * are permitted provided that the following conditions are met:
 *
 * 1. Redistributions of source code must retain the above copyright notice, this list of
 *    conditions and the following disclaimer.
 *
 * 2. Redistributions in binary form must reproduce the above copyright notice, this list
 *    of conditions and the following disclaimer in the documentation and/or other materials
 *    provided with the distribution.
 *
 * 3. Neither the name of the copyright holder nor the names of its contributors may be used
 *    to endorse or promote products derived from this software without specific prior written
 *    permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS
 * OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
 * COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
 * GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
 * AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
 * NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
 * OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "exfat.h"
#ifdef LOSCFG_FS_EXFAT

#include <errno.h>
#include <string.h>
#include <stdlib.h>
#include <sys/statfs.h>
#include "securec.h"
#include "los_memory.h"
#include "los_mux.h"
#include "los_printf.h"
#include "los_tables.h"
#include "fs/file.h"
#include "fs/fs.h"
#include "fs/mount.h"
#include "fs/dirent_fs.h"
#include "vnode.h"
#include "path_cache.h"
#include "disk.h"
#include "disk_pri.h"

extern UINT8 *m_aucSysMem0;

/* ---------------------------------------------------------------------------
 * Forward declarations of MountOps callbacks for the file-scope g_exfatMountOps
 * --------------------------------------------------------------------------- */
static int VfsExfatMount(struct Mount *mount, struct Vnode *blk, const void *data);
static int VfsExfatUnmount(struct Mount *mount, struct Vnode **blkdriver);
static int VfsExfatStatfs(struct Mount *mount, struct statfs *sbp);
static int VfsExfatSync(struct Mount *mount);

/* ---------------------------------------------------------------------------
 * VfsExfatReclaim — vop->Reclaim handler. Called by VFS framework's
 * VnodeFree() AFTER VnodePathCacheFree() walks the vnode's path_cache lists
 * and BEFORE the vnode struct is recycled. This is the proper hook for
 * releasing FS-private inode_info attached to vnode->data; doing it inside
 * VfsExfatUnmount() instead causes a use-after-free during VnodeFreeAll's
 * subsequent VnodePathCacheFree (data_abort far=0x4 — see fix history).
 * Mirrors fatfs's reclaim handling.
 * --------------------------------------------------------------------------- */
int VfsExfatReclaim(struct Vnode *vnode)
{
    exfat_inode_info *ei = NULL;
    if (vnode == NULL) {
        return 0;
    }
    ei = (exfat_inode_info *)vnode->data;
    if (ei != NULL) {
        (void)LOS_MuxDestroy(&ei->inode_lock);
        LOS_MemFree(m_aucSysMem0, ei);
        vnode->data = NULL;
    }
    return 0;
}

struct MountOps g_exfatMountOps = {
    .Mount   = VfsExfatMount,
    .Unmount = VfsExfatUnmount,
    .Statfs  = VfsExfatStatfs,
    .Sync    = VfsExfatSync,
};

/* zero registration: linker aggregates the entry into g_fsmap[]. */
FSMAP_ENTRY(exfat_fsmap, "exfat", g_exfatMountOps, FALSE, TRUE);

/* ---------------------------------------------------------------------------
 * Inline boot-region strict CRC32 verification (sectors 1..11 + checksum sector 12).
 * The spec's [RELY] only declares exfat_calc_chksum32 — the loop lives here.
 * --------------------------------------------------------------------------- */
static int ExfatVerifyBootRegion(exfat_sb_info *sbi)
{
    uint8_t *buf = NULL;
    uint32_t chksum = 0;
    int ret = 0;
    int sn;

    buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->blocksize);
    if (buf == NULL) {
        return ENOMEM;
    }

    /* 11 boot sub-region sectors (0..10) — sector 0 chksum type CS_BOOT_SECTOR,
     * 1..10 chksum type CS_DEFAULT. Sector 0 is already in sbi->boot_buf, but
     * we re-read so the loop is uniform. */
    for (sn = 0; sn < 11; sn++) {
        if (los_part_read(sbi->part_id, buf, (UINT64)sn, 1, TRUE) < 0) {
            ret = EIO;
            goto out;
        }
        chksum = exfat_calc_chksum32(buf, sbi->blocksize, chksum,
                                     sn ? CS_DEFAULT : CS_BOOT_SECTOR);
    }

    /* checksum sector (sector 11): every 4-byte LE32 must equal accumulated chksum */
    if (los_part_read(sbi->part_id, buf, 11ULL, 1, TRUE) < 0) {
        ret = EIO;
        goto out;
    }
    for (uint32_t i = 0; i < sbi->blocksize; i += sizeof(uint32_t)) {
        uint32_t expect = 0;
        (void)memcpy_s(&expect, sizeof(expect), &buf[i], sizeof(expect));
        if (expect != chksum) {
            PRINT_ERR("exfat: boot checksum mismatch (calc=0x%08x stored=0x%08x off=%u)\n",
                      chksum, expect, i);
            ret = EINVAL;
            goto out;
        }
    }

out:
    LOS_MemFree(m_aucSysMem0, buf);
    return ret;
}

/* ---------------------------------------------------------------------------
 * VfsExfatMount — see spec/exfat/interface/exfat_mount.spec.
 *
 * Goto-stack labels follow spec Invariant `exfat-mount-rollback-lifo`:
 *   error labels are reverse-LIFO of the resource acquisition order; internal
 *   variable `ret` carries positive errno; final `return -ret`.
 * --------------------------------------------------------------------------- */
static int VfsExfatMount(struct Mount *mount, struct Vnode *blk, const void *data)
{
    int ret = 0;
    struct drv_data *drv = NULL;
    struct block_operations *bops = NULL;
    los_part *part = NULL;
    exfat_sb_info *sbi = NULL;
    uint8_t *new_boot = NULL;
    struct Vnode *vp = NULL;
    exfat_inode_info *inode_info = NULL;

    /* progress flags drive the goto-stack */
    int opened = 0, part_name_set = 0;
    int s_lock_init = 0, bitmap_lock_init = 0, inode_hash_lock_init = 0;
    int boot_buf_alloc = 0, upcase_loaded = 0, bitmap_loaded = 0;
    int inode_lock_init = 0, vnode_alloc = 0;

    if (mount == NULL || blk == NULL || mount->vnodeBeCovered == NULL) {
        return -EINVAL;
    }

    /* Step 1: open the block device */
    drv = (struct drv_data *)blk->data;
    if (drv == NULL || drv->ops == NULL) {
        ret = ENODEV;
        goto err_open;
    }
    bops = (struct block_operations *)drv->ops;
    if (bops->open != NULL) {
        ret = bops->open(blk);
        if (ret != 0) {
            ret = (ret > 0) ? ret : -ret;   /* canonicalise to positive errno */
            goto err_open;
        }
    }
    opened = 1;

    /* Step 2: bind partition; refuse if already claimed */
    part = los_part_find(blk);
    if (part == NULL) {
        ret = ENODEV;
        goto err_part;
    }
    if (part->part_name != NULL) {
        ret = EBUSY;
        goto err_part;
    }
    if (SetDiskPartName(part, "exfat") != ENOERR) {
        ret = EBUSY;
        goto err_part;
    }
    part_name_set = 1;

    /* Step 3: allocate sbi (zeroed) and stash part_id immediately */
    sbi = (exfat_sb_info *)zalloc(sizeof(*sbi));
    if (sbi == NULL) {
        ret = ENOMEM;
        goto err_sbi;
    }
    sbi->part_id = part->part_id;

    /* Step 4: parse mount options (defaults inherit from cover vnode) */
    sbi->options.fs_uid    = mount->vnodeBeCovered->uid;
    sbi->options.fs_gid    = mount->vnodeBeCovered->gid;
    sbi->options.fs_fmask  = 0022;
    sbi->options.fs_dmask  = 0022;
    sbi->options.utf8      = 1;
    sbi->options.errors    = EXFAT_ERRORS_RO;
    if (exfat_parse_options((const char *)data, &sbi->options) != 0) {
        ret = EINVAL;
        goto err_opts;
    }

    /* Step 5: init the three sb-level locks (BEFORE any IO so that failure
     *         destroys can run unconditionally per Invariant *-rollback-lifo).
     *         init order matches spec Refine Prompt point 1. */
    if (LOS_MuxInit(&sbi->s_lock, NULL) != LOS_OK) {
        ret = ENOMEM;
        goto err_s_lock;
    }
    s_lock_init = 1;
    if (LOS_MuxInit(&sbi->bitmap_lock, NULL) != LOS_OK) {
        ret = ENOMEM;
        goto err_bm_lock;
    }
    bitmap_lock_init = 1;
    if (LOS_MuxInit(&sbi->inode_hash_lock, NULL) != LOS_OK) {
        ret = ENOMEM;
        goto err_ih_lock;
    }
    inode_hash_lock_init = 1;

    /* Step 6: read sector 0 with default 512-byte blocksize */
    sbi->blocksize = EXFAT_DEFAULT_BLOCKSIZE;
    sbi->boot_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, EXFAT_DEFAULT_BLOCKSIZE);
    if (sbi->boot_buf == NULL) {
        ret = ENOMEM;
        goto err_boot_alloc;
    }
    boot_buf_alloc = 1;
    if (los_part_read(sbi->part_id, sbi->boot_buf, 0ULL, 1, TRUE) < 0) {
        ret = EIO;
        goto err_boot_read;
    }

    /* Step 7: parse and validate boot sector — fills sbi geometry. */
    {
        struct exfat_boot_sector *p_boot = (struct exfat_boot_sector *)sbi->boot_buf;
        uint32_t logical_sect = 1u << p_boot->sect_size_bits;
        ret = exfat_parse_boot_sector(sbi, p_boot, logical_sect);
        if (ret != 0) {
            goto err_boot_read;
        }
    }

    /* Step 8: if logical sector > default blocksize, re-allocate boot_buf and re-read
     *         (Linux's exfat_calibrate_blocksize equivalent). */
    if (sbi->blocksize > EXFAT_DEFAULT_BLOCKSIZE) {
        new_boot = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->blocksize);
        if (new_boot == NULL) {
            ret = ENOMEM;
            goto err_boot_read;
        }
        if (los_part_read(sbi->part_id, new_boot, 0ULL, 1, TRUE) < 0) {
            LOS_MemFree(m_aucSysMem0, new_boot);
            ret = EIO;
            goto err_boot_read;
        }
        LOS_MemFree(m_aucSysMem0, sbi->boot_buf);
        sbi->boot_buf = new_boot;
        new_boot = NULL;
    }

    /* Step 9: STRICT boot region CRC32 verification (per ask-first answer #2). */
    ret = ExfatVerifyBootRegion(sbi);
    if (ret != 0) {
        goto err_boot_read;
    }

    /* Step 10: load upcase table from disk (per ask-first answer #3). */
    ret = exfat_create_upcase_table(sbi);
    if (ret != 0) {
        goto err_boot_read;
    }
    upcase_loaded = 1;

    /* Step 11: load allocation bitmap from disk. */
    ret = exfat_load_bitmap(sbi);
    if (ret != 0) {
        goto err_upcase;
    }
    bitmap_loaded = 1;

    /* Step 12: count used clusters (mem-only after bitmap loaded). */
    ret = exfat_count_used_clusters(sbi, &sbi->used_clusters);
    if (ret != 0) {
        goto err_bitmap;
    }

    /* Step 13: allocate root inode_info (zeroed) and init its lock. */
    inode_info = (exfat_inode_info *)zalloc(sizeof(*inode_info));
    if (inode_info == NULL) {
        ret = ENOMEM;
        goto err_bitmap;
    }
    if (LOS_MuxInit(&inode_info->inode_lock, NULL) != LOS_OK) {
        ret = ENOMEM;
        goto err_iinfo_alloc;
    }
    inode_lock_init = 1;
    inode_info->dir.dir   = sbi->root_dir;
    inode_info->dir.size  = 0;
    inode_info->dir.flags = ALLOC_FAT_CHAIN;
    inode_info->entry     = -1;
    inode_info->type      = TYPE_DIR;
    inode_info->start_clu = sbi->root_dir;
    inode_info->flags     = ALLOC_FAT_CHAIN;
    inode_info->i_pos     = ((uint64_t)sbi->root_dir << 32) | 0xFFFFFFFFu;

    /* Step 14: allocate root vnode and populate fields BEFORE making it visible. */
    ret = VnodeAlloc(&g_exfatVops, &vp);
    if (ret != 0 || vp == NULL) {
        ret = ENOMEM;
        goto err_inode_lock;
    }
    vnode_alloc = 1;
    vp->fop          = &g_exfatFops;
    vp->data         = inode_info;
    vp->originMount  = mount;
    vp->parent       = mount->vnodeBeCovered;
    vp->type         = VNODE_TYPE_DIR;
    vp->uid          = mount->vnodeBeCovered->uid;
    vp->gid          = mount->vnodeBeCovered->gid;
    vp->mode         = mount->vnodeBeCovered->mode;

    /* Step 15: VfsHashInsert — vnode becomes externally visible NOW.
     *          per Invariant exfat-mount-vnode-visible-after-init: every lock
     *          and every inode_info field is already initialized at this point. */
    if (VfsHashInsert(vp, sbi->root_dir) != LOS_OK) {
        ret = EBUSY;
        goto err_vnode;
    }

    /* Step 16: lenient warnings (Linux-equivalent) — never converted to error. */
    if (sbi->vol_flags & VOLUME_DIRTY) {
        PRINT_WARN("exfat: volume was not properly unmounted; please run fsck\n");
    }
    if (sbi->vol_flags & MEDIA_FAILURE) {
        PRINT_WARN("exfat: medium has reported failures; data may be lost\n");
    }

    /* Final two writes — only after every partial state is consistent. */
    mount->data         = sbi;
    mount->vnodeCovered = vp;
    return 0;

    /* -- failure rollback: reverse-LIFO per spec invariant -- */
err_vnode:
    if (vnode_alloc) {
        VnodeFree(vp);
    }
err_inode_lock:
    if (inode_lock_init) {
        (void)LOS_MuxDestroy(&inode_info->inode_lock);
    }
err_iinfo_alloc:
    if (inode_info != NULL) {
        LOS_MemFree(m_aucSysMem0, inode_info);
    }
err_bitmap:
    if (bitmap_loaded) {
        exfat_free_bitmap(sbi);
    }
err_upcase:
    if (upcase_loaded) {
        exfat_free_upcase_table(sbi);
    }
err_boot_read:
    if (boot_buf_alloc) {
        LOS_MemFree(m_aucSysMem0, sbi->boot_buf);
    }
err_boot_alloc:
    if (inode_hash_lock_init) {
        (void)LOS_MuxDestroy(&sbi->inode_hash_lock);
    }
err_ih_lock:
    if (bitmap_lock_init) {
        (void)LOS_MuxDestroy(&sbi->bitmap_lock);
    }
err_bm_lock:
    if (s_lock_init) {
        (void)LOS_MuxDestroy(&sbi->s_lock);
    }
err_s_lock:
err_opts:
    if (sbi != NULL) {
        LOS_MemFree(m_aucSysMem0, sbi);
    }
err_sbi:
    /* Mirror fs/fat/os_adapt/fatfs.c:1226 — there is no public ClearDiskPartName;
     * the part_name buffer was strdup'd by SetDiskPartName, so free + NULL by hand. */
    if (part_name_set && part != NULL && part->part_name != NULL) {
        free(part->part_name);
        part->part_name = NULL;
    }
err_part:
    if (opened && bops != NULL && bops->close != NULL) {
        (void)bops->close(blk);
    }
err_open:
    return -(int)ret;
}

/* ---------------------------------------------------------------------------
 * VfsExfatUnmount — natural inverse of mount; mirrors fs/fat/os_adapt/fatfs.c
 * ::fatfs_umount. The previous v1 implementation had a use-after-free:
 * `*blkdriver = root->parent` after `VnodeFree(root)` — root was already on
 * the freelist when its `parent` field was dereferenced. Fix:
 *   - resolve block device vnode via `get_part(sbi->part_id)->dev` BEFORE any
 *     frees (parallels fatfs_umount lines 1246-1255);
 *   - free part_name on the way out so remount succeeds;
 *   - call bops->close on the block device with the SAVED *blkdriver value;
 *   - tear down inode + sbi resources;
 *   - finally write `*blkdriver = device` and clear mount fields.
 * --------------------------------------------------------------------------- */
static int VfsExfatUnmount(struct Mount *mount, struct Vnode **blkdriver)
{
    exfat_sb_info *sbi = NULL;
    struct Vnode *root = NULL;
    struct Vnode *device = NULL;
    los_part *part = NULL;

    if (mount == NULL || mount->data == NULL) {
        return -EINVAL;
    }

    sbi  = (exfat_sb_info *)mount->data;
    root = mount->vnodeCovered;

    /* --- Step 1: capture block device vnode BEFORE any frees. --- */
    part = get_part(sbi->part_id);
    if (part != NULL) {
        device = part->dev;
    }

    /* --- Step 2: close the block device via the caller-supplied blkdriver
     *             handle (matches fatfs's use of *blkdriver). NULL-safe. --- */
    if (blkdriver != NULL && *blkdriver != NULL) {
        struct drv_data *dd = (struct drv_data *)((*blkdriver)->data);
        if (dd != NULL && dd->ops != NULL) {
            const struct block_operations *bops = (const struct block_operations *)dd->ops;
            if (bops->close != NULL) {
                (void)bops->close(*blkdriver);
            }
        }
    }

    /* --- Step 3: leave the root Vnode and its inode_info entirely to the
     *             VFS layer. Mirrors fatfs_umount which neither frees the
     *             root vnode nor its dfp (inode_info equivalent) inside the
     *             FS Unmount callback. VFS reaps everything via VnodeFreeAll
     *             once we return.
     *
     *             Past attempts that DID free here:
     *             1) calling VnodeFree(root) directly — caused double-free
     *                (VFS also calls VnodeFree from VnodeFreeAll).
     *             2) freeing inode_info + LOS_MuxDestroy and setting
     *                root->data = NULL — also crashes during VnodeFreeAll's
     *                VnodePathCacheFree(root) (data_abort far=0x00000004).
     *
     *             Trade-off: the inode_info pointed to by root->data leaks
     *             on each umount in v1.2 (minor; one heap object per cycle).
     *             v1.3 will plug this by populating g_exfatVops.Reclaim,
     *             which is the proper hook VFS uses for FS-private cleanup. */
    (void)root;

    /* --- Step 4: release sbi resources in reverse-mount order. --- */
    exfat_free_bitmap(sbi);
    exfat_free_upcase_table(sbi);
    if (sbi->boot_buf != NULL) {
        LOS_MemFree(m_aucSysMem0, sbi->boot_buf);
        sbi->boot_buf = NULL;
    }
    (void)LOS_MuxDestroy(&sbi->inode_hash_lock);
    (void)LOS_MuxDestroy(&sbi->bitmap_lock);
    (void)LOS_MuxDestroy(&sbi->s_lock);

    /* --- Step 5: free part_name so the partition can be remounted. --- */
    if (part != NULL && part->part_name != NULL) {
        free(part->part_name);
        part->part_name = NULL;
    }

    /* --- Step 6: free sbi and clear mount fields. --- */
    LOS_MemFree(m_aucSysMem0, sbi);
    mount->data         = NULL;
    mount->vnodeCovered = NULL;

    /* --- Step 7: return the block device vnode to the VFS layer. --- */
    if (blkdriver != NULL) {
        *blkdriver = device;
    }
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatStatfs — derived from sbi geometry; cheap.
 * --------------------------------------------------------------------------- */
static int VfsExfatStatfs(struct Mount *mount, struct statfs *sbp)
{
    exfat_sb_info *sbi = NULL;

    if (mount == NULL || mount->data == NULL || sbp == NULL) {
        return -EINVAL;
    }
    sbi = (exfat_sb_info *)mount->data;

    (void)memset_s(sbp, sizeof(*sbp), 0, sizeof(*sbp));
    sbp->f_type    = EXFAT_SUPER_MAGIC;
    sbp->f_bsize   = sbi->cluster_size;
    sbp->f_blocks  = (sbi->num_clusters >= 2u) ? (sbi->num_clusters - 2u) : 0u;
    sbp->f_bfree   = (sbp->f_blocks > sbi->used_clusters)
                        ? (sbp->f_blocks - sbi->used_clusters) : 0u;
    sbp->f_bavail  = sbp->f_bfree;
    sbp->f_namelen = EXFAT_MAX_NAME_LEN;
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatSync — v1 mount path is read-only; nothing to flush. Write paths
 * landing in later stages will replace this with a real implementation.
 * --------------------------------------------------------------------------- */
static int VfsExfatSync(struct Mount *mount)
{
    (void)mount;
    return 0;
}

#endif /* LOSCFG_FS_EXFAT */
