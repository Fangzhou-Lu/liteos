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
/* exfat_inode — inode lifecycle (alloc/free), Lookup VOP, Open/Close
 * Fop, Getattr/Seek. Mirrors Linux fs/exfat/{inode,namei}.c plus
 * LiteOS-specific exfat_inode_info heap lifecycle. */

#include <errno.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>

#include "exfat.h"
#include "disk.h"
#include "fs/mount.h"
#include "los_memory.h"
#include "los_mux.h"
#include "los_printf.h"
#include "securec.h"
#include "vnode.h"

#define LE16_TO_HOST(x) ((uint16_t)(x))
#define LE32_TO_HOST(x) ((uint32_t)(x))
#define LE64_TO_HOST(x) ((uint64_t)(x))
#define HOST_TO_LE16(x) ((uint16_t)(x))
#define EXFAT_STAT_BLOCK_SIZE 512u
#define EXFAT_OFF_MAX ((off_t)0x7FFFFFFFFFFFFFFFLL)

/* ----- merged from exfat_inode_alloc.c ----- */

extern UINT8 *m_aucSysMem0;

/* ---------------------------------------------------------------------------
 * exfat_inode_alloc
 *
 * zalloc(sizeof(exfat_inode_info)) → init LosMuxAttr (PRIO_INHERIT) → LOS_MuxInit.
 *
 * Failure stack (reverse-LIFO cleanup; Invariant exfat-inode-alloc-leak-free):
 *   - zalloc failure                    → -ENOMEM, no allocation made.
 *   - LOS_MuxAttrInit / SetProtocol     → -EIO, free zalloced ei.
 *   - LOS_MuxInit                       → -EIO, destroy attr, free ei.
 * On success the local LosMuxAttr is destroyed (ei->inode_lock has copied the
 * relevant fields) before returning.
 *
 * Invariant exfat-inode-alloc-prio-inherit: PRIO_INHERIT must be set via
 * LOS_MuxAttrSetProtocol — never via direct field assignment, never NULL attr.
 * --------------------------------------------------------------------------- */
int exfat_inode_alloc(exfat_inode_info **out)
{
    exfat_inode_info *ei = NULL;
    LosMuxAttr        attr;
    UINT32            ret;

    if (out == NULL) {
        return -EINVAL;
    }

    ei = (exfat_inode_info *)zalloc(sizeof(exfat_inode_info));
    if (ei == NULL) {
        return -ENOMEM;
    }

    ret = LOS_MuxAttrInit(&attr);
    if (ret != LOS_OK) {
        PRINT_ERR("[%s] LOS_MuxAttrInit failed: 0x%x\n", __func__, ret);
        LOS_MemFree(m_aucSysMem0, ei);
        return -EIO;
    }

    ret = LOS_MuxAttrSetProtocol(&attr, LOS_MUX_PRIO_INHERIT);
    if (ret != LOS_OK) {
        PRINT_ERR("[%s] LOS_MuxAttrSetProtocol failed: 0x%x\n", __func__, ret);
        (void)LOS_MuxAttrDestroy(&attr);
        LOS_MemFree(m_aucSysMem0, ei);
        return -EIO;
    }

    ret = LOS_MuxInit(&ei->inode_lock, &attr);
    if (ret != LOS_OK) {
        PRINT_ERR("[%s] LOS_MuxInit failed: 0x%x\n", __func__, ret);
        (void)LOS_MuxAttrDestroy(&attr);
        LOS_MemFree(m_aucSysMem0, ei);
        return -EIO;
    }

    /* attr is a stack local; LOS_MuxInit copies the protocol bits it needs
     * into the LosMux body, so it's safe to destroy the attr now. */
    (void)LOS_MuxAttrDestroy(&attr);

    *out = ei;
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_inode_free
 *
 * NULL-safe destruct + free. Caller is responsible for ensuring no thread
 * currently holds ei->inode_lock (Invariant exfat-inode-free-idempotent-null-safe
 * does NOT cover double-free of the same non-NULL pointer; caller must NULL
 * out the pointer after calling).
 * --------------------------------------------------------------------------- */
void exfat_inode_free(exfat_inode_info *ei)
{
    if (ei == NULL) {
        return;
    }
    (void)LOS_MuxDestroy(&ei->inode_lock);
    LOS_MemFree(m_aucSysMem0, ei);
}

/* ---------------------------------------------------------------------------
 * exfat_inode_init_dir_chain
 *
 * Pure five-field assignment for a freshly-allocated exfat_inode_info that
 * represents a directory inode (per spec Post-Condition). Caller guarantees
 * start_clu >= EXFAT_FIRST_CLUSTER (Invariant exfat-inode-init-dir-chain-pure;
 * function does not validate).
 * --------------------------------------------------------------------------- */
void exfat_inode_init_dir_chain(exfat_inode_info *ei, uint32_t start_clu)
{
    ei->dir.dir   = start_clu;
    ei->dir.flags = (uint8_t)ALLOC_FAT_CHAIN;
    ei->dir.size  = 0;
    ei->type      = (uint32_t)TYPE_DIR;
    ei->start_clu = start_clu;
}

/* ----- merged from exfat_lookup.c ----- */

extern UINT8 *m_aucSysMem0;

/* Identity LE→host on LiteOS-A ARMv7-A LE host; kept as macro so future
 * BE port can change one place (Invariant exfat-lookup-le-host-only). */

/* ---------------------------------------------------------------------------
 * ExfatLookupExtractName — pull UTF-16 filename out of a validated dentry-set.
 *
 * Set layout (post-validate): set[0] = EXFAT_FILE primary, set[1] = EXFAT_STREAM
 * secondary, set[2..] = EXFAT_NAME secondaries (each carrying 15 UTF-16 units).
 * Total UTF-16 length is set[1].dentry.stream.name_len.
 *
 * Pure compute (no IO/lock/alloc); returns 0 + *uni_len, or -EINVAL on any
 * shape mismatch.
 * --------------------------------------------------------------------------- */
static int ExfatLookupExtractName(const struct exfat_dentry *set, int num_entries,
                                  uint16_t *uni, int uni_max, int *uni_len)
{
    int nm_len;
    int needed;
    int written = 0;
    int i;
    int j;
    int chunk;

    if (set == NULL || uni == NULL || uni_len == NULL || num_entries < 2) {
        return -EINVAL;
    }
    if (set[0].type != EXFAT_FILE || set[1].type != EXFAT_STREAM) {
        return -EINVAL;
    }

    nm_len = (int)set[1].dentry.stream.name_len;
    if (nm_len <= 0 || nm_len > uni_max) {
        return -EINVAL;
    }
    needed = (nm_len + EXFAT_FILE_NAME_LEN - 1) / EXFAT_FILE_NAME_LEN;
    if (num_entries < 2 + needed) {
        return -EINVAL;
    }

    for (i = 0; i < needed; i++) {
        const struct exfat_dentry *nd = &set[2 + i];

        if (nd->type != EXFAT_NAME) {
            return -EINVAL;
        }
        chunk = (i == needed - 1)
                    ? (nm_len - i * EXFAT_FILE_NAME_LEN)
                    : EXFAT_FILE_NAME_LEN;
        for (j = 0; j < chunk; j++) {
            uni[written++] = LE16_TO_HOST(nd->dentry.name.unicode_0_14[j]);
        }
    }

    *uni_len = nm_len;
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatLookup — vop->Lookup callback. Find a name in `parent` directory.
 *
 * Lock model: faithful to Linux fs/exfat/namei.c::exfat_lookup. Take
 * sbi->s_lock for the entire body (line 708 of upstream). No per-inode lock,
 * no bitmap_lock, no inode_hash_lock — Linux exfat namei code doesn't take
 * them either.
 *
 * On match: allocate exfat_inode_info + Vnode, populate per spec, hash insert.
 * On miss: -ENOENT. On IO error during walk: -EIO. On invalid args: -EINVAL.
 * On name-too-long: -ENAMETOOLONG. On OOM: -ENOMEM.
 *
 * goto-stack reverse-LIFO cleanup (Invariant exfat-lookup-leak-free); failure
 * leaves *vpp untouched (Invariant exfat-lookup-no-mutate-on-failure).
 * --------------------------------------------------------------------------- */
int VfsExfatLookup(struct Vnode *parent, const char *name, int len,
                   struct Vnode **vpp)
{
    exfat_sb_info       *sbi = NULL;
    exfat_inode_info    *parent_ei = NULL;
    exfat_inode_info    *ei = NULL;
    struct Vnode        *vp = NULL;
    uint16_t            *uni_target = NULL;
    uint16_t            *uni_found = NULL;
    struct exfat_dentry *set_buf = NULL;
    exfat_chain          dir;
    int                  uni_target_len = 0;
    int                  uni_found_len = 0;
    int                  max_dentries;
    int                  entry_idx;
    int                  matched_entry = -1;
    int                  matched_num = 0;
    int                  had_io_err = 0;
    int                  locked = 0;
    int                  ret = 0;
    int                  err;

    /* Parameter-level checks (s_lock not yet held). */
    if (parent == NULL || name == NULL || vpp == NULL) {
        return -EINVAL;
    }
    if (parent->type != VNODE_TYPE_DIR || parent->data == NULL ||
        parent->originMount == NULL || parent->originMount->data == NULL) {
        return -EINVAL;
    }
    if (len <= 0) {
        return -EINVAL;
    }
    if (len > EXFAT_MAX_NAME_LEN * 4) {
        return -ENAMETOOLONG;
    }

    sbi = (exfat_sb_info *)parent->originMount->data;
    parent_ei = (exfat_inode_info *)parent->data;
    if (sbi->cluster_size == 0u || sbi->dentries_per_clu == 0u) {
        return -EIO;
    }

    /* Allocate working buffers. Heap-side rather than stack to keep stack
     * usage bounded — total ~1.6 KiB across three allocations. */
    uni_target = (uint16_t *)LOS_MemAlloc(m_aucSysMem0,
        (uint32_t)(EXFAT_MAX_NAME_LEN * sizeof(uint16_t)));
    if (uni_target == NULL) {
        ret = ENOMEM;
        goto ERROR_EXIT;
    }
    uni_found = (uint16_t *)LOS_MemAlloc(m_aucSysMem0,
        (uint32_t)(EXFAT_MAX_NAME_LEN * sizeof(uint16_t)));
    if (uni_found == NULL) {
        ret = ENOMEM;
        goto ERROR_FREE_TARGET;
    }
    set_buf = (struct exfat_dentry *)LOS_MemAlloc(m_aucSysMem0,
        (uint32_t)(EXFAT_DENTRY_SET_MAX * sizeof(struct exfat_dentry)));
    if (set_buf == NULL) {
        ret = ENOMEM;
        goto ERROR_FREE_FOUND;
    }

    /* Acquire FS-global lock — Linux namei discipline. */
    if (LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER) != LOS_OK) {
        ret = EIO;
        goto ERROR_FREE_SET;
    }
    locked = 1;

    /* Decode the target name to UTF-16. exfat_utf8_to_uni returns negative
     * POSIX errno; -ENAMETOOLONG bubbles up directly, anything else → EINVAL. */
    err = exfat_utf8_to_uni(name, len, uni_target, EXFAT_MAX_NAME_LEN,
                            &uni_target_len);
    if (err == -ENAMETOOLONG) {
        ret = ENAMETOOLONG;
        goto ERROR_UNLOCK;
    }
    if (err != 0) {
        ret = EINVAL;
        goto ERROR_UNLOCK;
    }
    if (uni_target_len <= 0) {
        ret = EINVAL;
        goto ERROR_UNLOCK;
    }
    if (uni_target_len > EXFAT_MAX_NAME_LEN) {
        ret = ENAMETOOLONG;
        goto ERROR_UNLOCK;
    }

    /* Build the parent directory chain. */
    dir.dir = parent_ei->start_clu;
    dir.flags = parent_ei->flags;
    if (parent_ei->size > 0u) {
        dir.size = (uint32_t)((parent_ei->size + sbi->cluster_size - 1u) /
                              sbi->cluster_size);
    } else {
        /* Root with implicit size — let stop-on-unused terminate the walk. */
        dir.size = 0u;
    }

    /* Compute scan upper bound (Invariant exfat-lookup-bounded). */
    max_dentries = MAX_EXFAT_DENTRIES;
    if (dir.size != 0u) {
        uint64_t bound = (uint64_t)dir.size * (uint64_t)sbi->dentries_per_clu;

        if (bound < (uint64_t)MAX_EXFAT_DENTRIES) {
            max_dentries = (int)bound;
        }
    }

    /* Walk parent dentry stream. Two-step pattern (peek primary, then fetch
     * full set on EXFAT_FILE) keeps non-file primaries / unused / deleted
     * out of the dentry-set helper, matching its set-primary-required
     * contract. */
    for (entry_idx = 0; entry_idx < max_dentries;) {
        struct exfat_dentry primary;
        int num_entries = 0;

        err = exfat_get_dentry(sbi, &dir, entry_idx, &primary, NULL);
        if (err == -EIO) {
            had_io_err = 1;
            entry_idx++;
            continue;
        }
        if (err != 0) {
            /* -EINVAL / -ENOMEM out of the helper — treat as fatal. */
            ret = -err;
            goto ERROR_UNLOCK;
        }

        if (primary.type == EXFAT_UNUSED) {
            break; /* End of directory. */
        }
        if (primary.type != EXFAT_FILE) {
            entry_idx++;
            continue;
        }

        err = exfat_get_dentry_set(sbi, &dir, entry_idx, set_buf,
                                   EXFAT_DENTRY_SET_MAX, &num_entries);
        if (err == -EIO) {
            had_io_err = 1;
            entry_idx++;
            continue;
        }
        if (err != 0 || num_entries <= 0) {
            entry_idx++;
            continue;
        }

        if (exfat_validate_dentry_set(set_buf, num_entries) != 0) {
            had_io_err = 1;
            entry_idx += num_entries;
            continue;
        }

        if (ExfatLookupExtractName(set_buf, num_entries, uni_found,
                                   EXFAT_MAX_NAME_LEN, &uni_found_len) != 0) {
            entry_idx += num_entries;
            continue;
        }

        if (exfat_uniname_cmp(sbi, uni_target, uni_target_len,
                              uni_found, uni_found_len) == 0) {
            matched_entry = entry_idx;
            matched_num = num_entries;
            break; /* set_buf still holds the matched set. */
        }

        entry_idx += num_entries;
    }
    (void)matched_num;

    if (matched_entry < 0) {
        ret = had_io_err ? EIO : ENOENT;
        goto ERROR_UNLOCK;
    }

    /* Allocate inode and populate from matched set. */
    err = exfat_inode_alloc(&ei);
    if (err != 0) {
        ret = (err == -ENOMEM) ? ENOMEM : EIO;
        goto ERROR_UNLOCK;
    }

    {
        const struct exfat_dentry *fp = &set_buf[0];
        const struct exfat_dentry *sp = &set_buf[1];
        uint16_t attr = LE16_TO_HOST(fp->dentry.file.attr);
        uint8_t  flags = sp->dentry.stream.flags;
        uint32_t start_clu = LE32_TO_HOST(sp->dentry.stream.start_clu);
        uint64_t size = LE64_TO_HOST(sp->dentry.stream.size);
        uint64_t valid_size = LE64_TO_HOST(sp->dentry.stream.valid_size);

        ei->dir.dir = parent_ei->start_clu;
        ei->dir.size = dir.size;
        ei->dir.flags = parent_ei->flags;
        ei->entry = matched_entry;
        ei->type = (attr & ATTR_SUBDIR) ? TYPE_DIR : TYPE_FILE;
        ei->attr = attr;
        ei->start_clu = start_clu;
        ei->flags = flags;
        ei->size = size;
        ei->valid_size = valid_size;
        ei->i_size_ondisk = size;
        ei->i_pos = ((uint64_t)start_clu << 32) | (uint32_t)matched_entry;
        ei->num_subdirs = 0u;

        err = VnodeAlloc(&g_exfatVops, &vp);
        if (err != 0) {
            ret = ENOMEM;
            goto ERROR_FREE_INODE;
        }

        vp->type = (attr & ATTR_SUBDIR) ? VNODE_TYPE_DIR : VNODE_TYPE_REG;
        vp->vop = &g_exfatVops;
        vp->fop = &g_exfatFops;
        vp->data = ei;
        vp->parent = parent;
        vp->originMount = parent->originMount;
        vp->uid = sbi->options.fs_uid;
        vp->gid = sbi->options.fs_gid;
        /* Permission bits: dir → 0755, file → 0644, ANDed with options
         * dmask/fmask. Without this VFS open() rejects with EACCES.
         * exFAT on-disk has no per-file permission; fmask/dmask are the
         * mount-time policy (Wave A: zero default → 0755/0644 verbatim). */
        if (attr & ATTR_SUBDIR) {
            vp->mode = S_IFDIR | (mode_t)(0755 & ~sbi->options.fs_dmask);
        } else {
            vp->mode = S_IFREG | (mode_t)(0644 & ~sbi->options.fs_fmask);
        }

        err = VfsHashInsert(vp, (uint32_t)ei->i_pos);
        if (err != 0) {
            ret = ENOMEM;
            goto ERROR_FREE_VNODE;
        }
    }

    *vpp = vp;
    LOS_MuxUnlock(&sbi->s_lock);
    locked = 0;

    LOS_MemFree(m_aucSysMem0, set_buf);
    LOS_MemFree(m_aucSysMem0, uni_found);
    LOS_MemFree(m_aucSysMem0, uni_target);
    return 0;

ERROR_FREE_VNODE:
    /* Detach our ei before VnodeFree so VFS Reclaim hook (if invoked) does
     * not double-free (we still own ei and free it at ERROR_FREE_INODE). */
    if (vp != NULL) {
        vp->data = NULL;
        (void)VnodeFree(vp);
        vp = NULL;
    }
ERROR_FREE_INODE:
    if (ei != NULL) {
        exfat_inode_free(ei);
        ei = NULL;
    }
ERROR_UNLOCK:
    if (locked) {
        LOS_MuxUnlock(&sbi->s_lock);
        locked = 0;
    }
ERROR_FREE_SET:
    LOS_MemFree(m_aucSysMem0, set_buf);
ERROR_FREE_FOUND:
    LOS_MemFree(m_aucSysMem0, uni_found);
ERROR_FREE_TARGET:
    LOS_MemFree(m_aucSysMem0, uni_target);
ERROR_EXIT:
    return -ret;
}

/* ----- merged from exfat_open_close.c ----- */

/* ---------------------------------------------------------------------------
 * VfsExfatOpen — file_operations_vfs.open callback (Wave A stub).
 *
 * Mirrors Linux's default .open = generic_file_open (exfat doesn't override).
 * No allocation, no lock, no IO — just type-check the vnode and let read take
 * over. All per-file metadata is stored on the vnode (vp->data), so no
 * per-open state lives in filep->f_priv.
 *
 * Returns 0 / -EISDIR / -EINVAL. Never modifies sbi, ei, vp, or filep state.
 * --------------------------------------------------------------------------- */
int VfsExfatOpen(struct file *filep)
{
    struct Vnode *vp;
    exfat_inode_info *ei;

    if (filep == NULL) {
        return -EINVAL;
    }
    vp = filep->f_vnode;
    if (vp == NULL || vp->originMount == NULL || vp->data == NULL) {
        return -EINVAL;
    }

    ei = (exfat_inode_info *)vp->data;

    /* Invariant exfat-open-rejects-dir: directory should go through the
     * Opendir/Readdir bundle on g_exfatVops, not the file open path. */
    if (vp->type == VNODE_TYPE_DIR || ei->type == TYPE_DIR) {
        return -EISDIR;
    }
    if (vp->type != VNODE_TYPE_REG || ei->type != TYPE_FILE) {
        return -EINVAL;
    }

    /* Invariant exfat-open-pos-untouched / exfat-open-no-alloc:
     * sys_open guarantees f_pos == 0 and f_priv == NULL on entry; leave both. */
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatClose — file_operations_vfs.close callback (Wave A stub).
 *
 * Mirrors Linux exfat's choice to leave .release unimplemented (VFS skips the
 * FS-level callback). Wave A allocates no per-open state and the FS is
 * read-only, so there is nothing to release or flush. Always returns 0.
 *
 * Wave B (write path) will extend this to acquire sbi->s_lock and call the
 * future fsync/truncate-on-close logic; the spec invariants reserve room.
 * --------------------------------------------------------------------------- */
int VfsExfatClose(struct file *filep)
{
    (void)filep;
    return 0;
}

/* ----- merged from exfat_attr.c ----- */

/* POSIX st_blocks unit. */

/* off_t in LiteOS-A musl is signed 64-bit (int64_t-equivalent); SUSv4 lseek
 * returns EOVERFLOW when the resulting offset would exceed this. */

/* ---------------------------------------------------------------------------
 * VfsExfatGetattr — VnodeOps.Getattr callback.
 *
 * Linux-faithful: mirrors fs/exfat/file.c::exfat_getattr by filling stat from
 * in-memory inode info plus sbi geometry. Wave A leaves all timestamps zero
 * (ei has no timestamp fields yet); Wave B will parse dentry CrtTime/MtimeOff
 * and extend ei + this routine via spec evolve.
 *
 * Lock: takes ei->inode_lock briefly to snapshot ei->size; does NOT take
 * sbi->s_lock (Linux exfat_getattr also doesn't).
 * --------------------------------------------------------------------------- */
int VfsExfatGetattr(struct Vnode *vp, struct stat *st)
{
    exfat_sb_info *sbi;
    exfat_inode_info *ei;
    uint64_t snap_size;
    errno_t serr;

    if (vp == NULL || vp->originMount == NULL || vp->data == NULL || st == NULL) {
        return -EINVAL;
    }
    sbi = (exfat_sb_info *)vp->originMount->data;
    ei  = (exfat_inode_info *)vp->data;
    if (sbi == NULL) {
        return -EINVAL;
    }

    /* Invariant exfat-vfsops-getattr-uses-inode-lock. */
    (VOID)LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER);
    snap_size = ei->size;
    (VOID)LOS_MuxUnlock(&ei->inode_lock);

    /* Invariant exfat-vfsops-getattr-clears-st. */
    serr = memset_s(st, sizeof(*st), 0, sizeof(*st));
    if (serr != EOK) {
        return -EINVAL;
    }

    st->st_dev     = (dev_t)sbi->part_id;
    st->st_ino     = (ino_t)(ei->i_pos >> 32);
    st->st_mode    = vp->mode;
    st->st_nlink   = 1;                                /* exfat-vfsops-getattr-nlink-one */
    st->st_uid     = sbi->options.fs_uid;
    st->st_gid     = sbi->options.fs_gid;
    st->st_size    = (off_t)snap_size;
    st->st_blksize = (blksize_t)sbi->cluster_size;     /* exfat-vfsops-getattr-blksize-cluster */
    st->st_blocks  = (snap_size > 0) ?                 /* exfat-vfsops-getattr-blocks-512 */
        (blkcnt_t)((snap_size + EXFAT_STAT_BLOCK_SIZE - 1u) / EXFAT_STAT_BLOCK_SIZE) : 0;

    /* Invariant exfat-vfsops-getattr-no-timestamps: leave atime/mtime/ctime
     * zeroed by memset_s above. Wave B will populate after dentry timestamp
     * parsing lands in ei. */

    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatSeek — file_operations_vfs.seek callback.
 *
 * SEEK_SET / SEEK_CUR are pure arithmetic on filep->f_pos (per-fd, no lock).
 * SEEK_END snapshots ei->size under ei->inode_lock and computes new pos.
 *
 * POSIX semantics: allow seek past EOF (read at that pos returns 0 per
 * exfat-read-eof-returns-zero). Reject negative new pos with -EINVAL; reject
 * off_t overflow on SEEK_END with -EOVERFLOW (SUSv4 spec).
 * --------------------------------------------------------------------------- */
off_t VfsExfatSeek(struct file *filep, off_t offset, int whence)
{
    struct Vnode *vp;
    exfat_inode_info *ei;
    off_t new_pos;
    uint64_t cur_size;

    if (filep == NULL) {
        return (off_t)-EINVAL;
    }
    vp = filep->f_vnode;
    if (vp == NULL || vp->data == NULL) {
        return (off_t)-EINVAL;
    }
    ei = (exfat_inode_info *)vp->data;
    if (ei->type != TYPE_FILE) {
        return (off_t)-EINVAL;                         /* exfat-vfsops-seek-rejects-non-file */
    }

    switch (whence) {
        case SEEK_SET:
            if (offset < 0) {
                return (off_t)-EINVAL;
            }
            new_pos = offset;
            break;

        case SEEK_CUR:
            /* Detect signed overflow: filep->f_pos + offset. */
            if (offset > 0 && filep->f_pos > EXFAT_OFF_MAX - offset) {
                return (off_t)-EOVERFLOW;
            }
            new_pos = (off_t)filep->f_pos + offset;
            if (new_pos < 0) {
                return (off_t)-EINVAL;
            }
            break;

        case SEEK_END:
            (VOID)LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER);
            cur_size = ei->size;
            (VOID)LOS_MuxUnlock(&ei->inode_lock);
            /* Invariant exfat-vfsops-seek-overflow-detection. */
            if (offset > 0 && cur_size > (uint64_t)(EXFAT_OFF_MAX - offset)) {
                return (off_t)-EOVERFLOW;
            }
            new_pos = (off_t)cur_size + offset;
            if (new_pos < 0) {
                return (off_t)-EINVAL;
            }
            break;

        default:
            return (off_t)-EINVAL;
    }

    filep->f_pos = (loff_t)new_pos;
    return new_pos;
}

/* ----- merged from exfat_mkdir (Stage 4e) ----- */

/* ---------------------------------------------------------------------------
 * exfat_set_entry_time_now — fill file dentry's create/modify/access time
 * fields from current wall-clock time.
 *
 * Linux (fs/exfat/misc.c::exfat_set_entry_time) packs (year-1980 << 9) |
 * (month << 5) | mday into a date u16 and (hour << 11) | (min << 5) |
 * (sec >> 1) into a time u16; tz uses EXFAT_TZ_VALID with the offset zero
 * (== "local time = UTC"). v1 keeps the same encoding; tz/cs sub-fields
 * left at 0 since LiteOS-A `time_offset` mount option is not yet wired.
 *
 * Invariant exfat-init-dir-entry-time-set-on-success: timestamp != 0 must
 * be observable on the success path.
 * --------------------------------------------------------------------------- */
static inline void exfat_set_entry_time_now(struct exfat_dentry *fep)
{
    time_t    now;
    struct tm tmv;
    uint16_t  date;
    uint16_t  tval;

    now = time(NULL);
    if (now <= 0) {
        /* Time source unavailable — encode 1980-01-01 00:00:00 so the field
         * stays non-zero per spec invariant exfat-init-dir-entry-time-set-on-success. */
        date = (uint16_t)((1u << 5) | 1u);
        tval = 0u;
    } else {
        if (gmtime_r(&now, &tmv) == NULL) {
            date = (uint16_t)((1u << 5) | 1u);
            tval = 0u;
        } else {
            int year_off = tmv.tm_year - 80; /* tm_year=year-1900; we need year-1980 */
            if (year_off < 0) {
                year_off = 0;
            }
            if (year_off > 127) {
                year_off = 127; /* exFAT spec caps at 2107. */
            }
            date = (uint16_t)((((uint16_t)year_off) << 9) |
                              ((((uint16_t)tmv.tm_mon) + 1u) << 5) |
                              (uint16_t)tmv.tm_mday);
            tval = (uint16_t)((((uint16_t)tmv.tm_hour) << 11) |
                              (((uint16_t)tmv.tm_min) << 5) |
                              ((uint16_t)tmv.tm_sec >> 1));
        }
    }

    fep->dentry.file.create_time = tval;
    fep->dentry.file.create_date = date;
    fep->dentry.file.modify_time = tval;
    fep->dentry.file.modify_date = date;
    fep->dentry.file.access_time = tval;
    fep->dentry.file.access_date = date;
    fep->dentry.file.create_time_cs = 0u;
    fep->dentry.file.modify_time_cs = 0u;
    fep->dentry.file.create_tz = 0u;
    fep->dentry.file.modify_tz = 0u;
    fep->dentry.file.access_tz = 0u;
}

/* ---------------------------------------------------------------------------
 * exfat_calc_num_entries
 *
 * Pure compute. Linux dir.c::exfat_calc_num_entries formula: 1 file + 1 stream
 * + ceil(name_len / EXFAT_FILE_NAME_LEN) name dentries =
 * `((name_len - 1) / 15) + 3`. Range guarantees [3, 19].
 * --------------------------------------------------------------------------- */
int exfat_calc_num_entries(const struct exfat_uni_name *p_uniname)
{
    int len;

    if (p_uniname == NULL) {
        return -EINVAL;
    }
    len = (int)p_uniname->name_len;
    if (len <= 0 || len > EXFAT_MAX_NAME_LEN) {
        return -EINVAL;
    }
    return ((len - 1) / EXFAT_FILE_NAME_LEN) + 3;
}

/* ---------------------------------------------------------------------------
 * exfat_zeroed_cluster
 *
 * Per-sector loop over the cluster: heap-alloc one blocksize zero buffer,
 * los_part_write it nr_sect times.
 *
 * Invariant exfat-zeroed-cluster-blocksize-iteration: per-sector loop is
 * mandated; integrated multi-sector write is forbidden.
 * --------------------------------------------------------------------------- */
int exfat_zeroed_cluster(exfat_sb_info *sbi, uint32_t clu)
{
    uint8_t *zbuf;
    uint64_t first_sect;
    uint32_t nr_sect;
    uint32_t i;
    errno_t  serr;
    int      ret;

    if (sbi == NULL || sbi->blocksize == 0u) {
        return -EINVAL;
    }
    if (clu < EXFAT_FIRST_CLUSTER || clu >= sbi->num_clusters) {
        return -EINVAL;
    }
    if (sbi->sect_per_clus_bits > 25u) {
        /* exFAT spec EXFAT_MAX_SECT_PER_CLUS_BITS bound. */
        return -EINVAL;
    }

    zbuf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->blocksize);
    if (zbuf == NULL) {
        return -ENOMEM;
    }
    serr = memset_s(zbuf, sbi->blocksize, 0, sbi->blocksize);
    if (serr != EOK) {
        LOS_MemFree(m_aucSysMem0, zbuf);
        return -EIO;
    }

    first_sect = exfat_clu_to_sector(sbi, clu);
    nr_sect    = 1u << sbi->sect_per_clus_bits;

    for (i = 0; i < nr_sect; i++) {
        ret = los_part_write(sbi->part_id, zbuf, first_sect + (uint64_t)i, 1u);
        if (ret < 0) {
            PRINT_ERR("[%s] zero sector %llu failed: %d\n",
                      __func__, (unsigned long long)(first_sect + i), ret);
            LOS_MemFree(m_aucSysMem0, zbuf);
            return -EIO;
        }
    }

    LOS_MemFree(m_aucSysMem0, zbuf);
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_alloc_new_dir
 *
 * Allocates 1 cluster (via exfat_alloc_cluster which requires
 * ALLOC_FAT_CHAIN), zeros the cluster, then promotes the chain flag to
 * ALLOC_NO_FAT_CHAIN (single-cluster chain — no FAT walk needed).
 *
 * Invariant exfat-alloc-new-dir-rollback-on-zero-fail: zeroed_cluster
 * failure rolls back via clear_bitmap + ent_set(FREE) inline (do NOT call
 * exfat_free_cluster — would self-deadlock on bitmap_lock; honors
 * Invariant exfat-add-entry-no-self-cluster-double-free).
 * --------------------------------------------------------------------------- */
int exfat_alloc_new_dir(exfat_sb_info *sbi, exfat_chain *clu_out)
{
    int ret;
    uint32_t allocated_clu;

    if (sbi == NULL || clu_out == NULL) {
        return -EINVAL;
    }

    /* Initial chain shape: alloc path requires ALLOC_FAT_CHAIN. */
    clu_out->dir   = EXFAT_EOF_CLUSTER;
    clu_out->size  = 0u;
    clu_out->flags = (uint8_t)ALLOC_FAT_CHAIN;

    ret = exfat_alloc_cluster(sbi, 1u, clu_out);
    if (ret != 0) {
        clu_out->dir   = EXFAT_EOF_CLUSTER;
        clu_out->size  = 0u;
        clu_out->flags = (uint8_t)ALLOC_NO_FAT_CHAIN;
        return ret;
    }

    allocated_clu = clu_out->dir;

    ret = exfat_zeroed_cluster(sbi, allocated_clu);
    if (ret != 0) {
        /* Rollback: release the cluster manually (Invariant
         * exfat-add-entry-no-self-cluster-double-free — clear_bitmap +
         * ent_set inline; do not re-enter exfat_free_cluster which would
         * grab bitmap_lock again indirectly). */
        (void)exfat_clear_bitmap(sbi, allocated_clu);
        (void)exfat_ent_set(sbi, allocated_clu, EXFAT_FREE_CLUSTER);
        if (sbi->used_clusters != EXFAT_CLUSTERS_UNTRACKED &&
            sbi->used_clusters > 0u) {
            sbi->used_clusters--;
        }
        clu_out->dir   = EXFAT_EOF_CLUSTER;
        clu_out->size  = 0u;
        clu_out->flags = (uint8_t)ALLOC_NO_FAT_CHAIN;
        return ret;
    }

    /* Promote single-cluster chain to NO_FAT_CHAIN — no FAT walk required
     * for a 1-cluster directory. */
    clu_out->flags = (uint8_t)ALLOC_NO_FAT_CHAIN;
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_init_dir_entry
 *
 * Write [entry, entry+1] = file dentry (0x85) + stream dentry (0xC0) using
 * the dentry-set-write helper from Stage 4a. num_ext, checksum, name_len,
 * name_hash placeholders are 0 — populated by exfat_init_ext_entry.
 *
 * Invariant exfat-init-dir-entry-stream-flags-by-type: stream.flags is
 * ALLOC_NO_FAT_CHAIN for TYPE_DIR, ALLOC_FAT_CHAIN for TYPE_FILE.
 * --------------------------------------------------------------------------- */
int exfat_init_dir_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                         int entry, uint32_t type, uint32_t start_clu,
                         uint64_t size)
{
    struct exfat_dentry fep;
    struct exfat_dentry sep;
    errno_t serr;
    int ret;

    if (sbi == NULL || p_dir == NULL || entry < 0) {
        return -EINVAL;
    }
    if (type != TYPE_DIR && type != TYPE_FILE) {
        return -EINVAL;
    }

    /* file dentry (primary). */
    serr = memset_s(&fep, sizeof(fep), 0, sizeof(fep));
    if (serr != EOK) {
        return -EIO;
    }
    fep.type = (uint8_t)EXFAT_FILE;
    fep.dentry.file.attr = (type == TYPE_DIR) ?
                           (uint16_t)ATTR_SUBDIR : (uint16_t)ATTR_ARCHIVE;
    fep.dentry.file.num_ext = 0u;       /* overwritten by init_ext_entry */
    fep.dentry.file.checksum = 0u;      /* overwritten by init_ext_entry */
    exfat_set_entry_time_now(&fep);

    ret = exfat_set_dentry(sbi, p_dir, entry, &fep);
    if (ret != 0) {
        return ret;
    }

    /* stream dentry (first secondary). */
    serr = memset_s(&sep, sizeof(sep), 0, sizeof(sep));
    if (serr != EOK) {
        return -EIO;
    }
    sep.type = (uint8_t)EXFAT_STREAM;
    sep.dentry.stream.flags = (type == TYPE_FILE) ?
                              (uint8_t)ALLOC_FAT_CHAIN :
                              (uint8_t)ALLOC_NO_FAT_CHAIN;
    sep.dentry.stream.name_len = 0u;    /* overwritten by init_ext_entry */
    sep.dentry.stream.name_hash = 0u;   /* overwritten by init_ext_entry */
    sep.dentry.stream.valid_size = size;
    sep.dentry.stream.size = size;
    sep.dentry.stream.start_clu = start_clu;

    ret = exfat_set_dentry(sbi, p_dir, entry + 1, &sep);
    if (ret != 0) {
        return ret;     /* Q3: no rollback of file dentry */
    }
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_init_ext_entry
 *
 * Patch num_ext into file dentry, name_len/hash into stream dentry, build
 * num_entries-2 EXFAT_NAME (0xC1) dentries, then read all num_entries
 * dentries back, compute the SetChecksum, and patch it into the file dentry.
 *
 * Invariant exfat-init-ext-entry-chksum-spans-all-N: chksum seed comes from
 * exfat_calc_chksum16(file, 32, 0, CS_DIR_ENTRY); subsequent dentries use
 * CS_DEFAULT continuing the seed; missing any dentry breaks lookup.
 * --------------------------------------------------------------------------- */
int exfat_init_ext_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                         int entry, int num_entries,
                         const struct exfat_uni_name *p_uniname)
{
    struct exfat_dentry fep;
    struct exfat_dentry sep;
    struct exfat_dentry nep;
    struct exfat_dentry probe;
    uint16_t chksum;
    errno_t  serr;
    int i;
    int k;
    int chunk_off;
    int ret;

    if (sbi == NULL || p_dir == NULL || p_uniname == NULL) {
        return -EINVAL;
    }
    if (num_entries < 3 || num_entries > (int)EXFAT_DENTRY_SET_MAX) {
        return -EINVAL;
    }
    if (entry < 0) {
        return -EINVAL;
    }
    /* name_len is uint8_t — upper bound is implicit (255 == EXFAT_MAX_NAME_LEN). */
    if (p_uniname->name_len == 0u) {
        return -EINVAL;
    }

    /* Step 1: re-fetch file dentry, patch num_ext, write back. */
    ret = exfat_get_dentry(sbi, p_dir, entry, &fep, NULL);
    if (ret != 0) {
        return ret;
    }
    fep.dentry.file.num_ext = (uint8_t)(num_entries - 1);
    ret = exfat_set_dentry(sbi, p_dir, entry, &fep);
    if (ret != 0) {
        return ret;
    }

    /* Step 2: re-fetch stream dentry, patch name_len + name_hash, write back. */
    ret = exfat_get_dentry(sbi, p_dir, entry + 1, &sep, NULL);
    if (ret != 0) {
        return ret;
    }
    sep.dentry.stream.name_len = p_uniname->name_len;
    sep.dentry.stream.name_hash = p_uniname->name_hash;
    ret = exfat_set_dentry(sbi, p_dir, entry + 1, &sep);
    if (ret != 0) {
        return ret;
    }

    /* Step 3: build EXFAT_NAME dentries — 15 UTF-16 units per slot. */
    for (i = 2; i < num_entries; i++) {
        serr = memset_s(&nep, sizeof(nep), 0, sizeof(nep));
        if (serr != EOK) {
            return -EIO;
        }
        nep.type = (uint8_t)EXFAT_NAME;
        nep.dentry.name.flags = 0u;

        chunk_off = (i - 2) * EXFAT_FILE_NAME_LEN;
        for (k = 0; k < EXFAT_FILE_NAME_LEN; k++) {
            int src = chunk_off + k;
            if (src < (int)p_uniname->name_len) {
                nep.dentry.name.unicode_0_14[k] =
                    p_uniname->name[src];
            } else {
                nep.dentry.name.unicode_0_14[k] = 0u;
            }
        }

        ret = exfat_set_dentry(sbi, p_dir, entry + i, &nep);
        if (ret != 0) {
            return ret;     /* Q3: no rollback */
        }
    }

    /* Step 4: re-read all num_entries dentries to compute chksum16
     * (Invariant exfat-init-ext-entry-chksum-spans-all-N). */
    ret = exfat_get_dentry(sbi, p_dir, entry, &probe, NULL);
    if (ret != 0) {
        return ret;
    }
    chksum = exfat_calc_chksum16(&probe, DENTRY_SIZE, 0, CS_DIR_ENTRY);

    for (i = 1; i < num_entries; i++) {
        ret = exfat_get_dentry(sbi, p_dir, entry + i, &probe, NULL);
        if (ret != 0) {
            return ret;
        }
        chksum = exfat_calc_chksum16(&probe, DENTRY_SIZE, chksum, CS_DEFAULT);
    }

    /* Step 5: re-fetch file dentry (whose num_ext was already patched in
     * step 1), set checksum, write back. */
    ret = exfat_get_dentry(sbi, p_dir, entry, &fep, NULL);
    if (ret != 0) {
        return ret;
    }
    fep.dentry.file.checksum = chksum;
    ret = exfat_set_dentry(sbi, p_dir, entry, &fep);
    if (ret != 0) {
        return ret;
    }
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_add_entry — composer.
 *
 * Linux fs/exfat/namei.c::exfat_add_entry analogue. Both TYPE_DIR (mkdir)
 * and TYPE_FILE (create) call this composer; the only difference is whether
 * a fresh data cluster is allocated (TYPE_DIR) or the file is left empty
 * with start_clu = EXFAT_EOF_CLUSTER (TYPE_FILE).
 *
 * Invariant exfat-add-entry-uniname-hash-cs-default: name_hash uses
 * CS_DEFAULT chksum16 over the UTF-16 byte stream (no upcase normalization).
 * --------------------------------------------------------------------------- */
int exfat_add_entry(exfat_sb_info *sbi, struct Vnode *parent_vp,
                    const char *name, uint32_t type,
                    struct exfat_dir_entry *info)
{
    exfat_inode_info     *parent_ei;
    exfat_chain           p_dir;
    struct exfat_uni_name uniname;
    int  num_entries;
    int  dentry_idx = -1;
    int  uni_len = 0;
    uint32_t start_clu = EXFAT_EOF_CLUSTER;
    uint64_t clu_size = 0u;
    exfat_chain new_clu;
    errno_t serr;
    int ret;

    if (sbi == NULL || parent_vp == NULL || parent_vp->data == NULL ||
        name == NULL || info == NULL) {
        return -EINVAL;
    }
    if (type != TYPE_DIR && type != TYPE_FILE) {
        return -EINVAL;
    }
    if (name[0] == '\0') {
        return -EINVAL;
    }

    parent_ei = (exfat_inode_info *)parent_vp->data;

    /* Step 1: decode UTF-8 → UTF-16. exfat_utf8_to_uni accepts -1 length
     * as "use strlen". Pass strlen explicitly via probe walk. */
    serr = memset_s(&uniname, sizeof(uniname), 0, sizeof(uniname));
    if (serr != EOK) {
        return -EIO;
    }
    {
        size_t name_bytes = 0;
        const char *p = name;
        while (*p != '\0') {
            p++;
            name_bytes++;
            if (name_bytes > (size_t)EXFAT_MAX_NAME_LEN * 4u) {
                return -ENAMETOOLONG;
            }
        }
        ret = exfat_utf8_to_uni(name, (int)name_bytes, uniname.name,
                                EXFAT_MAX_NAME_LEN, &uni_len);
        if (ret != 0) {
            return ret;
        }
    }
    if (uni_len <= 0 || uni_len > EXFAT_MAX_NAME_LEN) {
        return -EINVAL;
    }
    uniname.name_len  = (uint8_t)uni_len;
    uniname.name_hash = exfat_calc_chksum16(uniname.name,
                                            uni_len * (int)sizeof(uint16_t),
                                            0, CS_DEFAULT);

    /* Step 2: count dentry-set entries needed. */
    num_entries = exfat_calc_num_entries(&uniname);
    if (num_entries < 0) {
        return num_entries;
    }

    /* Step 3: build parent directory chain copy from parent_ei. */
    p_dir.dir   = parent_ei->start_clu;
    p_dir.flags = parent_ei->flags;
    if (sbi->cluster_size == 0u) {
        return -EIO;
    }
    if (parent_ei->size > 0u) {
        p_dir.size = (uint32_t)((parent_ei->size + sbi->cluster_size - 1u) /
                                sbi->cluster_size);
    } else {
        /* Root directory with implicit size: assume single cluster — the
         * default Wave A cmocka image uses single-cluster root. Production
         * mount path populates parent_ei->size at lookup time. */
        p_dir.size = 1u;
    }

    /* Step 4: reserve dentry slot (must precede alloc_new_dir per Linux order). */
    ret = exfat_alloc_dentry_slot(sbi, &p_dir, num_entries, &dentry_idx);
    if (ret != 0) {
        return ret;
    }

    /* Step 5: TYPE_DIR — allocate + zero a new directory cluster.
     *         TYPE_FILE — empty file: skip alloc, leave start_clu at EOF
     *         (Linux fs/exfat/namei.c::exfat_add_entry mirrors this). */
    if (type == TYPE_DIR) {
        serr = memset_s(&new_clu, sizeof(new_clu), 0, sizeof(new_clu));
        if (serr != EOK) {
            return -EIO;
        }
        ret = exfat_alloc_new_dir(sbi, &new_clu);
        if (ret != 0) {
            /* Q3: do not release dentry_idx slot — free slot is harmless. */
            return ret;
        }
        start_clu = new_clu.dir;
        clu_size  = (uint64_t)sbi->cluster_size;
    }
    /* TYPE_FILE: start_clu remains EXFAT_EOF_CLUSTER, clu_size remains 0. */

    /* Step 6: write the file + stream dentry pair. */
    ret = exfat_init_dir_entry(sbi, &p_dir, dentry_idx, type,
                               start_clu, clu_size);
    if (ret != 0) {
        /* Q3: leave allocated cluster + slot intact; vol_dirty bracket marks. */
        return ret;
    }

    /* Step 7: build name dentries + chksum. */
    ret = exfat_init_ext_entry(sbi, &p_dir, dentry_idx, num_entries, &uniname);
    if (ret != 0) {
        return ret;
    }

    /* Step 8: hand info back to caller (VfsExfatMkdir / VfsExfatCreate).
     * TYPE_DIR uses the freshly allocated cluster as start_clu and reports
     * EXFAT_MIN_SUBDIR; TYPE_FILE marks an empty regular file with
     * ATTR_ARCHIVE / EXFAT_EOF_CLUSTER / size 0 / num_subdirs 0. */
    info->dir         = p_dir;
    info->entry       = dentry_idx;
    info->type        = type;
    info->flags       = (uint8_t)ALLOC_NO_FAT_CHAIN;
    if (type == TYPE_DIR) {
        info->attr        = (uint16_t)ATTR_SUBDIR;
        info->start_clu   = start_clu;
        info->size        = clu_size;
        info->num_subdirs = EXFAT_MIN_SUBDIR;
    } else {
        info->attr        = (uint16_t)ATTR_ARCHIVE;
        info->start_clu   = EXFAT_EOF_CLUSTER;
        info->size        = 0u;
        info->num_subdirs = 0u;
    }
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatMkdir — VnodeOps.Mkdir callback. (v0.4 promote 2026-05-07)
 *
 * Two-phase locking per spec Refine Prompt:
 *   Phase 1 (disk mutation):  s_lock held; vol_dirty bracket; add_entry.
 *   Phase 2 (vnode creation): lock-free; heap + VFS helpers only.
 *
 * Invariants enforced:
 *   exfat-mkdir-s-lock-bracketed         (lock spans Phase 1 only)
 *   exfat-mkdir-vol-dirty-bracketed      (set/clear envelopes add_entry)
 *   exfat-mkdir-vfs-hash-insert-after-data-set
 *   exfat-mkdir-subdir-count             (parent num_subdirs++)
 *   exfat-mkdir-no-parent-dentry-write   (in-memory only)
 *   exfat-mkdir-leak-free-success        (success path zero leak)
 *
 * Error policy:
 *   Case 1 (success):              returns 0; *vpp set.
 *   Case 2 (add_entry failure):    s_lock released; *vpp untouched; returns errno.
 *   Case 3 (vnode setup failure):  s_lock already released; orphan dentry+cluster
 *                                  on disk (v1 no-rollback policy); returns -ENOMEM.
 * --------------------------------------------------------------------------- */
int VfsExfatMkdir(struct Vnode *parent_vp, const char *name,
                  mode_t mode, struct Vnode **vpp)
{
    exfat_sb_info        *sbi       = NULL;
    exfat_inode_info     *parent_ei = NULL;
    exfat_inode_info     *new_ei    = NULL;
    struct Vnode         *new_vp    = NULL;
    struct exfat_dir_entry info;
    errno_t serr;
    int     sd_ret;
    int     cd_ret;
    int     err = 0;

    (void)mode;   /* exFAT carries no per-file permission bits. */

    if (parent_vp == NULL || name == NULL || vpp == NULL) {
        return -EINVAL;
    }
    if (parent_vp->originMount == NULL ||
        parent_vp->originMount->data == NULL ||
        parent_vp->data == NULL) {
        return -EINVAL;
    }
    if (name[0] == '\0') {
        return -EINVAL;
    }

    sbi       = (exfat_sb_info *)parent_vp->originMount->data;
    parent_ei = (exfat_inode_info *)parent_vp->data;

    serr = memset_s(&info, sizeof(info), 0, sizeof(info));
    if (serr != EOK) {
        return -EIO;
    }

    /* ---- Phase 1: disk mutation (s_lock held) ----------------------------- */
    (void)LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER);

    sd_ret = exfat_set_volume_dirty(sbi);
    if (sd_ret != 0) {
        PRINT_ERR("[%s] set_volume_dirty: %d\n", __func__, sd_ret);
    }

    err = exfat_add_entry(sbi, parent_vp, name, TYPE_DIR, &info);

    cd_ret = exfat_clear_volume_dirty(sbi);
    if (cd_ret != 0) {
        PRINT_ERR("[%s] clear_volume_dirty: %d\n", __func__, cd_ret);
    }

    (void)LOS_MuxUnlock(&sbi->s_lock);

    if (err != 0) {
        return err;   /* Case 2 */
    }

    /* ---- Phase 2: vnode creation (lock-free) ------------------------------ */
    err = exfat_inode_alloc(&new_ei);
    if (err != 0) {
        PRINT_ERR("[%s] inode_alloc failed (%d) — orphan on disk\n",
                  __func__, err);
        return -ENOMEM;   /* Case 3 */
    }

    new_ei->dir           = info.dir;
    new_ei->entry         = info.entry;
    new_ei->type          = info.type;
    new_ei->attr          = info.attr;
    new_ei->start_clu     = info.start_clu;
    new_ei->flags         = info.flags;
    new_ei->size          = info.size;
    new_ei->valid_size    = info.size;
    new_ei->i_size_ondisk = info.size;
    new_ei->num_subdirs   = info.num_subdirs;
    new_ei->i_pos         = ((uint64_t)info.start_clu << 32) |
                            (uint32_t)info.entry;

    /* init_dir_chain rewires dir/start_clu/type/flags from the new cluster;
     * call after the manual copy so its values win (matches lookup pattern),
     * then restore fields it does not cover. */
    exfat_inode_init_dir_chain(new_ei, info.start_clu);
    new_ei->size          = info.size;
    new_ei->valid_size    = info.size;
    new_ei->i_size_ondisk = info.size;
    new_ei->attr          = info.attr;
    new_ei->num_subdirs   = info.num_subdirs;

    err = VnodeAlloc(&g_exfatVops, &new_vp);
    if (err != 0) {
        exfat_inode_free(new_ei);
        PRINT_ERR("[%s] VnodeAlloc failed — orphan on disk\n", __func__);
        return -ENOMEM;   /* Case 3 */
    }

    new_vp->type        = VNODE_TYPE_DIR;
    new_vp->vop         = &g_exfatVops;
    new_vp->fop         = &g_exfatFops;
    new_vp->data        = new_ei;
    new_vp->parent      = parent_vp;
    new_vp->originMount = parent_vp->originMount;
    new_vp->uid         = sbi->options.fs_uid;
    new_vp->gid         = sbi->options.fs_gid;
    new_vp->mode        = S_IFDIR | (mode_t)(0755u & ~sbi->options.fs_dmask);

    err = VfsHashInsert(new_vp, (uint32_t)info.start_clu);
    if (err != 0) {
        new_vp->data = NULL;
        (void)VnodeFree(new_vp);
        exfat_inode_free(new_ei);
        PRINT_ERR("[%s] VfsHashInsert failed — orphan on disk\n", __func__);
        return -ENOMEM;   /* Case 3 */
    }

    parent_ei->num_subdirs++;   /* in-memory only; on-disk sync deferred */

    *vpp = new_vp;
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatCreate — VnodeOps.Create callback. (Wave B Stage 4d, 2026-05-07)
 *
 * Creates an empty regular file under parent_vp. Mirrors VfsExfatMkdir's
 * Phase 1 / Phase 2 lock split, but:
 *   - exfat_add_entry receives TYPE_FILE; no data cluster is allocated.
 *     The on-disk dentry has start_clu = EXFAT_EOF_CLUSTER, size = 0.
 *   - parent num_subdirs is NOT incremented (files do not count).
 *   - vnode->type = VNODE_TYPE_REG; mode uses fs_fmask (file mask).
 *   - VfsHashInsert keys on (uint32_t)info.entry — the dentry index in the
 *     parent — to match the lookup-side convention. start_clu is identical
 *     (EXFAT_EOF_CLUSTER) for every empty file and would otherwise collide.
 *
 * Invariants enforced:
 *   exfat-create-no-cluster-on-empty       (start_clu == EXFAT_EOF_CLUSTER)
 *   exfat-create-attr-archive              (ATTR_ARCHIVE set, ATTR_SUBDIR clear)
 *   exfat-create-vol-dirty-bracketed       (set/clear envelope add_entry)
 *   exfat-create-no-parent-subdir-bump     (parent num_subdirs untouched)
 *   exfat-create-vfs-hash-insert-after-data-set
 *   exfat-create-s-lock-bracketed          (lock spans Phase 1 only)
 *   exfat-create-leak-free-success         (success path zero leak)
 *
 * Error policy: same Case 1/2/3/4 shape as VfsExfatMkdir.
 * --------------------------------------------------------------------------- */
int VfsExfatCreate(struct Vnode *parent_vp, const char *name,
                   int mode, struct Vnode **vpp)
{
    exfat_sb_info        *sbi    = NULL;
    exfat_inode_info     *new_ei = NULL;
    struct Vnode         *new_vp = NULL;
    struct exfat_dir_entry info;
    errno_t serr;
    int     sd_ret;
    int     cd_ret;
    int     err = 0;

    (void)mode;   /* exFAT carries no per-file permission bits on disk. */

    if (parent_vp == NULL || name == NULL || vpp == NULL) {
        return -EINVAL;
    }
    if (parent_vp->originMount == NULL ||
        parent_vp->originMount->data == NULL ||
        parent_vp->data == NULL) {
        return -EINVAL;
    }
    if (name[0] == '\0') {
        return -EINVAL;
    }

    sbi = (exfat_sb_info *)parent_vp->originMount->data;

    serr = memset_s(&info, sizeof(info), 0, sizeof(info));
    if (serr != EOK) {
        return -EIO;
    }

    /* ---- Phase 1: disk mutation (s_lock held) ----------------------------- */
    (void)LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER);

    sd_ret = exfat_set_volume_dirty(sbi);
    if (sd_ret != 0) {
        PRINT_ERR("[%s] set_volume_dirty: %d\n", __func__, sd_ret);
    }

    err = exfat_add_entry(sbi, parent_vp, name, TYPE_FILE, &info);

    cd_ret = exfat_clear_volume_dirty(sbi);
    if (cd_ret != 0) {
        PRINT_ERR("[%s] clear_volume_dirty: %d\n", __func__, cd_ret);
    }

    (void)LOS_MuxUnlock(&sbi->s_lock);

    if (err != 0) {
        return err;   /* Case 2 */
    }

    /* ---- Phase 2: vnode creation (lock-free) ------------------------------ */
    err = exfat_inode_alloc(&new_ei);
    if (err != 0) {
        PRINT_ERR("[%s] inode_alloc failed (%d) — orphan dentry on disk\n",
                  __func__, err);
        return -ENOMEM;   /* Case 3 */
    }

    /* Empty file: no init_dir_chain (file is not a directory chain). Copy
     * fields straight from info. */
    new_ei->dir           = info.dir;
    new_ei->entry         = info.entry;
    new_ei->type          = info.type;          /* TYPE_FILE */
    new_ei->attr          = info.attr;          /* ATTR_ARCHIVE */
    new_ei->start_clu     = info.start_clu;     /* EXFAT_EOF_CLUSTER */
    new_ei->flags         = info.flags;
    new_ei->size          = info.size;          /* 0 */
    new_ei->valid_size    = 0u;
    new_ei->i_size_ondisk = 0u;
    new_ei->num_subdirs   = info.num_subdirs;   /* 0 */
    new_ei->i_pos         = ((uint64_t)info.start_clu << 32) |
                            (uint32_t)info.entry;

    err = VnodeAlloc(&g_exfatVops, &new_vp);
    if (err != 0) {
        exfat_inode_free(new_ei);
        PRINT_ERR("[%s] VnodeAlloc failed — orphan dentry on disk\n", __func__);
        return -ENOMEM;   /* Case 3 */
    }

    new_vp->type        = VNODE_TYPE_REG;
    new_vp->vop         = &g_exfatVops;
    new_vp->fop         = &g_exfatFops;
    new_vp->data        = new_ei;
    new_vp->parent      = parent_vp;
    new_vp->originMount = parent_vp->originMount;
    new_vp->uid         = sbi->options.fs_uid;
    new_vp->gid         = sbi->options.fs_gid;
    new_vp->mode        = S_IFREG | (mode_t)(0644u & ~sbi->options.fs_fmask);

    /* Hash key: dentry index in parent. start_clu would collide across all
     * empty files (all EXFAT_EOF_CLUSTER); entry is unique per parent and
     * matches lookup's `(uint32_t)ei->i_pos` convention. */
    err = VfsHashInsert(new_vp, (uint32_t)info.entry);
    if (err != 0) {
        new_vp->data = NULL;
        (void)VnodeFree(new_vp);
        exfat_inode_free(new_ei);
        PRINT_ERR("[%s] VfsHashInsert failed — orphan dentry on disk\n", __func__);
        return -ENOMEM;   /* Case 4 */
    }

    /* Files do NOT count toward parent num_subdirs (Linux fs/exfat/namei.c
     * exfat_create matches: no inc_subdirs path). */

    *vpp = new_vp;
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatUnlink — VnodeOps.Unlink callback (spec/exfat/inode/exfat_unlink.spec).
 *
 * Two-phase locking per spec Refine Prompt:
 *   Phase 1 (under sbi->s_lock): vol_dirty bracket + dentry-set type-bit
 *           top-bit clear + write-back + in-memory tombstone.
 *   Phase 2 (lock-free w.r.t. s_lock): exfat_free_cluster releases the
 *           cluster chain; bitmap_lock taken transitively per cluster.
 *
 * Invariants enforced:
 *   exfat-unlink-dentry-type-top-bit-cleared  (Phase 1 step 5)
 *   exfat-unlink-tombstone-prevents-reuse     (target_ei->dir.dir = DIR_DELETED)
 *   exfat-unlink-vol-dirty-bracketed          (set / clear envelope add_entry-style)
 *   exfat-unlink-free-after-tombstone         (Phase 2 strictly after Phase 1)
 *   exfat-unlink-no-vnode-free                (target_vp lifetime owned by VFS)
 *   exfat-unlink-not-for-directories          (Pre-Cond rejects TYPE_DIR)
 *
 * Error policy:
 *   Case 1 (success):                returns 0; on-disk + bitmap + tombstone done.
 *   Case 2 (validation):             returns -EINVAL / -ENOENT; nothing touched.
 *   Case 3 (fetch / validate):       returns -EIO; vol_dirty bracket closed.
 *   Case 4 (write-back):             returns -EIO; bracket closed; partial on-disk.
 *   Case 5 (cluster-free fail):      returns errno; tombstone already persisted.
 * --------------------------------------------------------------------------- */
int VfsExfatUnlink(struct Vnode *parent_vp, struct Vnode *target_vp,
                   const char *fileName)
{
    exfat_sb_info       *sbi       = NULL;
    exfat_inode_info    *target_ei = NULL;
    struct exfat_dentry  set[EXFAT_DENTRY_SET_MAX];
    int                  num_entries = 0;
    int                  i;
    int                  step3 = 0;
    int                  step6 = 0;
    int                  err = 0;
    exfat_chain          chain;

    /* ---- Phase 0: argument validation (lock-free) ------------------------ */
    if (parent_vp == NULL || target_vp == NULL || fileName == NULL) {
        return -EINVAL;
    }
    if (parent_vp->originMount == NULL ||
        parent_vp->originMount->data == NULL ||
        target_vp->data == NULL) {
        return -EINVAL;
    }

    sbi       = (exfat_sb_info *)parent_vp->originMount->data;
    target_ei = (exfat_inode_info *)target_vp->data;

    if (target_ei->type != TYPE_FILE) {
        return -EINVAL;   /* Case 2: rmdir is separate */
    }
    if (target_ei->dir.dir == DIR_DELETED || target_ei->entry < 0) {
        return -ENOENT;   /* Case 2: already tombstoned */
    }

    /* ---- Phase 1: dentry-set mutation (s_lock held) ---------------------- */
    (void)LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER);

    (void)exfat_set_volume_dirty(sbi);

    step3 = exfat_get_dentry_set(sbi, &target_ei->dir, target_ei->entry,
                                 set, EXFAT_DENTRY_SET_MAX, &num_entries);
    if (step3 != 0) {
        err = -EIO;   /* Case 3 */
        goto phase1_unlock;
    }

    if (exfat_validate_dentry_set(set, num_entries) != 0) {
        err = -EIO;   /* Case 3 */
        goto phase1_unlock;
    }

    /* Step 5: clear the top bit of every dentry's type byte. exFAT readers
     * treat such dentries as deleted. */
    for (i = 0; i < num_entries; i++) {
        set[i].type &= 0x7Fu;
    }

    step6 = exfat_set_dentry_set(sbi, &target_ei->dir, target_ei->entry,
                                 set, num_entries);
    if (step6 != 0) {
        err = -EIO;   /* Case 4 — partial write may have hit disk */
        goto phase1_unlock;
    }

    /* Step 7: in-memory tombstone (only after on-disk write-back succeeded). */
    target_ei->dir.dir = DIR_DELETED;

phase1_unlock:
    /* Step 8: ALWAYS clear the volume-dirty bracket, even on failure. */
    (void)exfat_clear_volume_dirty(sbi);
    (void)LOS_MuxUnlock(&sbi->s_lock);

    if (err != 0) {
        return err;   /* Case 3 / Case 4 */
    }

    /* ---- Phase 2: cluster release (lock-free w.r.t. s_lock) --------------
     *
     * Per spec/exfat/inode/exfat_unlink.spec System Algorithm Phase 2:
     *   Step 1: empty-file fast path — if start_clu == EXFAT_EOF_CLUSTER the
     *           file never had data clusters allocated (lazy-alloc never
     *           triggered, e.g. created and immediately unlinked). No
     *           bitmap mutation; return 0 (Case 1b).
     *   Step 2: derive num_phys_clu = ceil(i_size_ondisk / cluster_size),
     *           clamp >= 1u. This dodges exfat_free_cluster.spec Case 4's
     *           size==0 short-circuit (line 106) which would otherwise make
     *           Phase 2 a silent no-op and leak the chain in the bitmap.
     *           See invariant exfat-unlink-phase2-size-nonzero.
     *   Step 3-4: build chain and call exfat_free_cluster.
     */
    if (target_ei->start_clu == EXFAT_EOF_CLUSTER) {
        return 0;   /* Case 1b: empty file, nothing to free */
    }

    {
        uint32_t cluster_size = sbi->cluster_size;
        uint64_t bytes        = target_ei->i_size_ondisk;
        uint32_t num_phys_clu;

        if (cluster_size == 0u) {
            /* Defensive: cluster_size == 0 means sbi corruption. Refuse to
             * call free_cluster which would also misbehave. */
            PRINT_ERR("[%s] sbi->cluster_size == 0 (sbi corruption)\n", __func__);
            return -EIO;   /* Case 5 */
        }

        num_phys_clu = (uint32_t)((bytes + (uint64_t)cluster_size - 1u) /
                                  (uint64_t)cluster_size);
        if (num_phys_clu == 0u) {
            num_phys_clu = 1u;   /* clamp per spec — anomaly defense */
        }

        chain.dir   = target_ei->start_clu;
        chain.size  = num_phys_clu;
        chain.flags = target_ei->flags;
    }

    err = exfat_free_cluster(sbi, &chain);
    if (err != 0) {
        PRINT_ERR("[%s] free_cluster failed (%d) — bitmap may carry orphans\n",
                  __func__, err);
        return err;   /* Case 5: tombstone already persisted */
    }

    return 0;
}

/* Spec stage: inode_metadata_model — see
 * spec/exfat/interface/exfat_inode_metadata_model.spec. All exports below are
 * pure / lock-free / IO-free; callers hold ei->inode_lock for touch_xxx and
 * bump_version. */

#define EXFAT_TOUCH_ATIME 0x1u
#define EXFAT_TOUCH_MTIME 0x2u
#define EXFAT_TOUCH_CTIME 0x4u

#define EXFAT_SECS_PER_DAY  86400ULL

/* civil_from_days / days_from_civil — Howard Hinnant public-domain algorithm.
 * Required because LiteOS-A has no portable timegm/mktime64 (invariant
 * exfat-meta-civil-time-epoch-self-contained). */
static void exfat_civil_from_seconds(uint64_t epoch_sec, int *year_out,
                                     unsigned *mon_out, unsigned *mday_out,
                                     unsigned *hour_out, unsigned *min_out,
                                     unsigned *sec_out)
{
    uint64_t days = epoch_sec / EXFAT_SECS_PER_DAY;
    uint64_t secs_of_day = epoch_sec - days * EXFAT_SECS_PER_DAY;

    int64_t z = (int64_t)days + 719468LL;
    int64_t era = (z >= 0 ? z : z - 146096LL) / 146097LL;
    uint64_t doe = (uint64_t)(z - era * 146097LL);
    uint64_t yoe = (doe - doe / 1460ULL + doe / 36524ULL - doe / 146096ULL) / 365ULL;
    int64_t y = (int64_t)yoe + era * 400LL;
    uint64_t doy = doe - (365ULL * yoe + yoe / 4ULL - yoe / 100ULL);
    uint64_t mp = (5ULL * doy + 2ULL) / 153ULL;
    unsigned d = (unsigned)(doy - (153ULL * mp + 2ULL) / 5ULL + 1ULL);
    unsigned m = (unsigned)(mp < 10ULL ? mp + 3ULL : mp - 9ULL);
    if (m <= 2u) {
        y += 1;
    }

    *year_out = (int)y;
    *mon_out  = m;
    *mday_out = d;
    *hour_out = (unsigned)(secs_of_day / 3600ULL);
    *min_out  = (unsigned)((secs_of_day / 60ULL) % 60ULL);
    *sec_out  = (unsigned)(secs_of_day % 60ULL);
}

static uint64_t exfat_seconds_from_civil(int year, unsigned mon, unsigned mday,
                                         unsigned hour, unsigned min, unsigned sec)
{
    int64_t y = year - (mon <= 2u ? 1 : 0);
    int64_t era = (y >= 0 ? y : y - 399LL) / 400LL;
    uint64_t yoe = (uint64_t)(y - era * 400LL);
    uint64_t m_adj = (mon > 2u) ? (mon - 3u) : (mon + 9u);
    uint64_t doy = (153ULL * m_adj + 2ULL) / 5ULL + (uint64_t)mday - 1ULL;
    uint64_t doe = yoe * 365ULL + yoe / 4ULL - yoe / 100ULL + doy;
    int64_t days = era * 146097LL + (int64_t)doe - 719468LL;
    if (days < 0) {
        days = 0;
    }
    return (uint64_t)days * EXFAT_SECS_PER_DAY +
           (uint64_t)hour * 3600ULL +
           (uint64_t)min  * 60ULL +
           (uint64_t)sec;
}

static uint64_t exfat_clamp_secs(uint64_t s)
{
    if (s < (uint64_t)EXFAT_MIN_TIMESTAMP_SECS) {
        return (uint64_t)EXFAT_MIN_TIMESTAMP_SECS;
    }
    if (s > (uint64_t)EXFAT_MAX_TIMESTAMP_SECS) {
        return (uint64_t)EXFAT_MAX_TIMESTAMP_SECS;
    }
    return s;
}

static void exfat_pack_date_time(uint64_t s, uint16_t *date_out, uint16_t *time_out)
{
    int year;
    unsigned mon, mday, hour, min, sec;
    exfat_civil_from_seconds(s, &year, &mon, &mday, &hour, &min, &sec);
    int year_off = year - 1980;
    if (year_off < 0) {
        year_off = 0;
    } else if (year_off > 127) {
        year_off = 127;
    }
    *date_out = (uint16_t)((((uint16_t)year_off) << 9) |
                           (((uint16_t)mon) << 5) |
                           (uint16_t)mday);
    *time_out = (uint16_t)((((uint16_t)hour) << 11) |
                           (((uint16_t)min) << 5) |
                           ((uint16_t)(sec >> 1u)));
}

uint64_t exfat_truncate_atime_seconds(uint64_t epoch_sec)
{
    return epoch_sec & ~((uint64_t)1u);
}

uint64_t exfat_now_seconds(void)
{
    time_t now = time(NULL);
    if (now <= 0) {
        return (uint64_t)EXFAT_MIN_TIMESTAMP_SECS;
    }
    return exfat_clamp_secs((uint64_t)now);
}

void exfat_encode_atime(const exfat_sb_info *sbi, uint64_t epoch_sec,
                        uint16_t *time_out, uint16_t *date_out,
                        uint8_t *tz_out)
{
    (void)sbi;
    uint64_t s = exfat_truncate_atime_seconds(exfat_clamp_secs(epoch_sec));
    exfat_pack_date_time(s, date_out, time_out);
    *tz_out = EXFAT_TZ_VALID;
}

void exfat_encode_mtime(const exfat_sb_info *sbi, uint64_t epoch_sec,
                        uint16_t *time_out, uint16_t *date_out,
                        uint8_t *cs_out, uint8_t *tz_out)
{
    (void)sbi;
    uint64_t s = exfat_clamp_secs(epoch_sec);
    exfat_pack_date_time(s, date_out, time_out);
    *cs_out = (uint8_t)(((s & 1ULL) != 0ULL) ? 100u : 0u);
    *tz_out = EXFAT_TZ_VALID;
}

void exfat_encode_ctime(const exfat_sb_info *sbi, uint64_t epoch_sec,
                        uint16_t *time_out, uint16_t *date_out,
                        uint8_t *cs_out, uint8_t *tz_out)
{
    (void)sbi;
    uint64_t s = exfat_clamp_secs(epoch_sec);
    exfat_pack_date_time(s, date_out, time_out);
    *cs_out = (uint8_t)(((s & 1ULL) != 0ULL) ? 100u : 0u);
    *tz_out = EXFAT_TZ_VALID;
}

uint64_t exfat_decode_entry_time(const exfat_sb_info *sbi,
                                 uint16_t time_le, uint16_t date_le,
                                 uint8_t cs, uint8_t tz)
{
    uint16_t date = LE16_TO_HOST(date_le);
    uint16_t time = LE16_TO_HOST(time_le);
    unsigned year = (unsigned)((date >> 9) & 0x7Fu) + 1980u;
    unsigned mon  = (unsigned)((date >> 5) & 0x0Fu);
    unsigned mday = (unsigned)(date & 0x1Fu);
    unsigned hour = (unsigned)((time >> 11) & 0x1Fu);
    unsigned min  = (unsigned)((time >> 5) & 0x3Fu);
    unsigned sec  = (unsigned)((time & 0x1Fu) << 1);

    static const unsigned days_in_month[] = {
        0u, 31u, 28u, 31u, 30u, 31u, 30u, 31u, 31u, 30u, 31u, 30u, 31u
    };
    if (mon < 1u || mon > 12u || mday < 1u) {
        return (uint64_t)EXFAT_MIN_TIMESTAMP_SECS;
    }
    unsigned mday_max = days_in_month[mon];
    if (mon == 2u) {
        unsigned leap = ((year % 4u == 0u && year % 100u != 0u) || year % 400u == 0u) ? 1u : 0u;
        if (leap != 0u) {
            mday_max = 29u;
        }
    }
    if (mday > mday_max) {
        return (uint64_t)EXFAT_MIN_TIMESTAMP_SECS;
    }
    if (hour > 23u || min > 59u || sec > 59u) {
        return (uint64_t)EXFAT_MIN_TIMESTAMP_SECS;
    }

    int64_t s = (int64_t)exfat_seconds_from_civil((int)year, mon, mday, hour, min, sec);

    if (cs > 0u && cs < 200u) {
        s += (int64_t)(cs / 100u);
    }

    if ((tz & EXFAT_TZ_VALID) != 0u) {
        int8_t off7 = (int8_t)(tz & 0x7Fu);
        if (off7 >= 0x40) {
            off7 = (int8_t)(off7 - 0x80);
        }
        s -= (int64_t)off7 * (15 * 60);
    } else if (sbi != NULL) {
        s -= (int64_t)sbi->options.time_offset * 60;
    }

    if (s < EXFAT_MIN_TIMESTAMP_SECS) {
        return (uint64_t)EXFAT_MIN_TIMESTAMP_SECS;
    }
    if (s > EXFAT_MAX_TIMESTAMP_SECS) {
        return (uint64_t)EXFAT_MAX_TIMESTAMP_SECS;
    }
    return (uint64_t)s;
}

static void exfat_inode_touch_mask(exfat_inode_info *ei, unsigned mask)
{
    uint64_t s = exfat_now_seconds();
    if ((mask & EXFAT_TOUCH_ATIME) != 0u) {
        ei->atime_sec = exfat_truncate_atime_seconds(s);
    }
    if ((mask & EXFAT_TOUCH_MTIME) != 0u) {
        ei->mtime_sec = s;
    }
    if ((mask & EXFAT_TOUCH_CTIME) != 0u) {
        ei->ctime_sec = s;
    }
    ei->version += 1u;
}

void exfat_inode_touch_atime(exfat_inode_info *ei)
{
    exfat_inode_touch_mask(ei, EXFAT_TOUCH_ATIME | EXFAT_TOUCH_CTIME);
}

void exfat_inode_touch_mtime(exfat_inode_info *ei)
{
    exfat_inode_touch_mask(ei, EXFAT_TOUCH_MTIME | EXFAT_TOUCH_CTIME);
}

void exfat_inode_touch_ctime(exfat_inode_info *ei)
{
    exfat_inode_touch_mask(ei, EXFAT_TOUCH_CTIME);
}

void exfat_inode_touch_atime_mtime(exfat_inode_info *ei)
{
    exfat_inode_touch_mask(ei, EXFAT_TOUCH_ATIME | EXFAT_TOUCH_MTIME);
}

void exfat_inode_touch_mtime_ctime(exfat_inode_info *ei)
{
    exfat_inode_touch_mask(ei, EXFAT_TOUCH_MTIME | EXFAT_TOUCH_CTIME);
}

void exfat_inode_touch_now(exfat_inode_info *ei)
{
    exfat_inode_touch_mask(ei, EXFAT_TOUCH_ATIME | EXFAT_TOUCH_MTIME | EXFAT_TOUCH_CTIME);
}

void exfat_inode_bump_version(exfat_inode_info *ei)
{
    ei->version += 1u;
}

int exfat_inode_load_metadata(const exfat_sb_info *sbi,
                              exfat_inode_info *ei,
                              const struct exfat_dentry *file_dentry)
{
    ei->atime_sec = exfat_decode_entry_time(sbi,
        LE16_TO_HOST(file_dentry->dentry.file.access_time),
        LE16_TO_HOST(file_dentry->dentry.file.access_date),
        0u,
        file_dentry->dentry.file.access_tz);
    ei->mtime_sec = exfat_decode_entry_time(sbi,
        LE16_TO_HOST(file_dentry->dentry.file.modify_time),
        LE16_TO_HOST(file_dentry->dentry.file.modify_date),
        file_dentry->dentry.file.modify_time_cs,
        file_dentry->dentry.file.modify_tz);
    ei->ctime_sec = exfat_decode_entry_time(sbi,
        LE16_TO_HOST(file_dentry->dentry.file.create_time),
        LE16_TO_HOST(file_dentry->dentry.file.create_date),
        file_dentry->dentry.file.create_time_cs,
        file_dentry->dentry.file.create_tz);
    return 0;
}

int exfat_inode_store_metadata(const exfat_sb_info *sbi,
                               const exfat_inode_info *ei,
                               struct exfat_dentry *file_dentry)
{
    uint16_t a_time, a_date, m_time, m_date, c_time, c_date;
    uint8_t  a_tz, m_tz, m_cs, c_tz, c_cs;

    exfat_encode_atime(sbi, ei->atime_sec, &a_time, &a_date, &a_tz);
    exfat_encode_mtime(sbi, ei->mtime_sec, &m_time, &m_date, &m_cs, &m_tz);
    exfat_encode_ctime(sbi, ei->ctime_sec, &c_time, &c_date, &c_cs, &c_tz);

    file_dentry->dentry.file.access_time    = HOST_TO_LE16(a_time);
    file_dentry->dentry.file.access_date    = HOST_TO_LE16(a_date);
    file_dentry->dentry.file.access_tz      = a_tz;
    file_dentry->dentry.file.modify_time    = HOST_TO_LE16(m_time);
    file_dentry->dentry.file.modify_date    = HOST_TO_LE16(m_date);
    file_dentry->dentry.file.modify_time_cs = m_cs;
    file_dentry->dentry.file.modify_tz      = m_tz;
    file_dentry->dentry.file.create_time    = HOST_TO_LE16(c_time);
    file_dentry->dentry.file.create_date    = HOST_TO_LE16(c_date);
    file_dentry->dentry.file.create_time_cs = c_cs;
    file_dentry->dentry.file.create_tz      = c_tz;
    return 0;
}

uint32_t exfat_inode_get_nlink(const exfat_inode_info *ei)
{
    if (ei->type == TYPE_DIR) {
        return (ei->num_subdirs > 2u) ? ei->num_subdirs : 2u;
    }
    return 1u;
}

/* ---------------------------------------------------------------------------
 * VfsExfatRmdir — VnodeOps.Rmdir callback. (Wave B Stage 4f, 2026-05-11)
 *
 * Directory analogue of VfsExfatUnlink with one extra precondition: the
 * target directory must be empty (no in-use primary FILE/DIR dentry inside
 * its data cluster chain).
 *
 * Phase 1 (s_lock held):
 *   - vol_dirty bracket open
 *   - emptiness walk over target dir's cluster chain (refuse on 0x85 primary)
 *   - fetch + validate + tombstone (top-bit clear) target's own dentry-set
 *   - write-back tombstone
 *   - in-memory: target_ei->dir.dir = DIR_DELETED; parent_ei->num_subdirs--
 *   - vol_dirty bracket close
 * Phase 2 (lock-free):
 *   - release target's data cluster chain via exfat_free_cluster
 *
 * Invariants enforced (8 IDs; see spec/exfat/inode/exfat_rmdir.spec):
 *   exfat-rmdir-must-be-empty
 *   exfat-rmdir-dentry-type-top-bit-cleared
 *   exfat-rmdir-parent-subdir-decrement
 *   exfat-rmdir-cluster-released-eagerly
 *   exfat-rmdir-phase2-size-nonzero
 *   exfat-rmdir-phase2-skipped-on-eof-start
 *   exfat-rmdir-no-vnode-free
 *   exfat-rmdir-s-lock-bracketed
 * --------------------------------------------------------------------------- */
int VfsExfatRmdir(struct Vnode *parent_vp, struct Vnode *target_vp,
                  const char *dirName)
{
    exfat_sb_info       *sbi       = NULL;
    exfat_inode_info    *parent_ei = NULL;
    exfat_inode_info    *target_ei = NULL;
    struct exfat_dentry  one;
    struct exfat_dentry  set[EXFAT_DENTRY_SET_MAX];
    exfat_chain          scan_dir;
    exfat_chain          chain;
    uint32_t             dentries_per_clu;
    int                  num_entries = 0;
    int                  i;
    int                  step;
    int                  err = 0;

    /* ---- Phase 0: argument validation (lock-free) ------------------------- */
    if (parent_vp == NULL || target_vp == NULL || dirName == NULL) {
        return -EINVAL;
    }
    if (parent_vp->originMount == NULL ||
        parent_vp->originMount->data == NULL ||
        parent_vp->data == NULL ||
        target_vp->data == NULL) {
        return -EINVAL;
    }

    sbi       = (exfat_sb_info *)parent_vp->originMount->data;
    parent_ei = (exfat_inode_info *)parent_vp->data;
    target_ei = (exfat_inode_info *)target_vp->data;

    if (parent_ei->type != TYPE_DIR || target_ei->type != TYPE_DIR) {
        return -EINVAL;   /* Case 2: wrong type — files use Unlink */
    }
    if (target_ei->dir.dir == DIR_DELETED || target_ei->entry < 0) {
        return -ENOENT;   /* Case 2: already tombstoned */
    }

    if (sbi->cluster_size == 0u || sbi->dentries_per_clu == 0u) {
        PRINT_ERR("[%s] sbi corruption (cluster_size or dentries_per_clu == 0)\n",
                  __func__);
        return -EIO;
    }
    dentries_per_clu  = sbi->dentries_per_clu;

    /* ---- Phase 1: emptiness scan + dentry-set tombstone (s_lock held) ----- */
    (void)LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER);
    (void)exfat_set_volume_dirty(sbi);

    /* Emptiness scan defensively guards a corrupt-empty directory: if start_clu
     * is EOF (no data cluster ever allocated), there are no dentries inside,
     * so the directory is trivially empty — skip the walk. Phase 2 will then
     * also skip via the same start_clu check. */
    if (target_ei->start_clu != EXFAT_EOF_CLUSTER) {
        scan_dir.dir   = target_ei->start_clu;
        scan_dir.size  = (uint32_t)((target_ei->i_size_ondisk +
                                     (uint64_t)sbi->cluster_size - 1u) /
                                    (uint64_t)sbi->cluster_size);
        if (scan_dir.size == 0u) {
            scan_dir.size = 1u;   /* defensive: at least one allocated cluster */
        }
        scan_dir.flags = target_ei->flags;

        /* Linear dentry index walk. exfat_get_dentry already handles
         * intra-chain cluster crossings; we bound the loop by
         * scan_dir.size * dentries_per_clu so a corrupt FAT chain can't
         * spin forever. */
        {
            uint64_t max_entries_u64 =
                (uint64_t)scan_dir.size * (uint64_t)dentries_per_clu;
            int max_entries =
                (max_entries_u64 > (uint64_t)INT32_MAX)
                    ? INT32_MAX
                    : (int)max_entries_u64;
            int entry_idx;

            for (entry_idx = 0; entry_idx < max_entries; entry_idx++) {
                step = exfat_get_dentry(sbi, &scan_dir, entry_idx, &one, NULL);
                if (step != 0) {
                    err = -EIO;   /* Case 4 */
                    goto phase1_unlock;
                }

                /* exFAT terminator: bit pattern 0x00 means "all subsequent
                 * dentries are free" — directory is empty for our purposes. */
                if (one.type == EXFAT_UNUSED) {
                    break;
                }

                /* In-use FILE/DIR primary (0x85) — directory NOT empty. */
                if (one.type == EXFAT_FILE) {
                    err = -ENOTEMPTY;   /* Case 3 */
                    goto phase1_unlock;
                }

                /* Top bit clear → deleted entry; skip it.
                 * Other in-use bytes (stream 0xC0 / name 0xC1 secondaries
                 * inside an active set) are allowed — only the primary
                 * 0x85 is the gatekeeper, matching Linux
                 * exfat_check_dir_empty. */
            }
        }
    }

    /* Emptiness check passed. Fetch the target's own dentry-set. */
    step = exfat_get_dentry_set(sbi, &target_ei->dir, target_ei->entry,
                                set, EXFAT_DENTRY_SET_MAX, &num_entries);
    if (step != 0) {
        err = -EIO;   /* Case 5 */
        goto phase1_unlock;
    }
    if (exfat_validate_dentry_set(set, num_entries) != 0) {
        err = -EIO;   /* Case 5 */
        goto phase1_unlock;
    }

    /* Clear top bit of every dentry's type byte. */
    for (i = 0; i < num_entries; i++) {
        set[i].type &= 0x7Fu;
    }

    step = exfat_set_dentry_set(sbi, &target_ei->dir, target_ei->entry,
                                set, num_entries);
    if (step != 0) {
        err = -EIO;   /* Case 5 — partial write may have hit disk */
        goto phase1_unlock;
    }

    /* In-memory tombstone + parent decrement (only on success path). */
    target_ei->dir.dir = DIR_DELETED;
    if (parent_ei->num_subdirs > 0u) {
        parent_ei->num_subdirs--;   /* in-memory only; on-disk sync deferred */
    }

phase1_unlock:
    (void)exfat_clear_volume_dirty(sbi);
    (void)LOS_MuxUnlock(&sbi->s_lock);

    if (err != 0) {
        return err;   /* Case 3 / Case 4 / Case 5 */
    }

    /* ---- Phase 2: cluster release (lock-free w.r.t. s_lock) --------------- */
    if (target_ei->start_clu == EXFAT_EOF_CLUSTER) {
        return 0;   /* Case 1b: corrupt-empty fast path */
    }

    {
        uint32_t cluster_size = sbi->cluster_size;
        uint64_t bytes        = target_ei->i_size_ondisk;
        uint32_t num_phys_clu;

        if (cluster_size == 0u) {
            PRINT_ERR("[%s] sbi->cluster_size == 0 (sbi corruption)\n", __func__);
            return -EIO;
        }

        num_phys_clu = (uint32_t)((bytes + (uint64_t)cluster_size - 1u) /
                                  (uint64_t)cluster_size);
        if (num_phys_clu == 0u) {
            num_phys_clu = 1u;   /* clamp per exfat-rmdir-phase2-size-nonzero */
        }

        chain.dir   = target_ei->start_clu;
        chain.size  = num_phys_clu;
        chain.flags = target_ei->flags;
    }

    err = exfat_free_cluster(sbi, &chain);
    if (err != 0) {
        PRINT_ERR("[%s] free_cluster failed (%d) — bitmap may carry orphans\n",
                  __func__, err);
        return err;   /* Case 6: tombstone already persisted */
    }

    (void)dirName;   /* v1: name accepted but only used for diagnostics */
    return 0;
}
