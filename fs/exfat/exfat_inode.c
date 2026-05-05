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
#include <sys/stat.h>
#include <sys/types.h>

#include "exfat.h"
#include "fs/mount.h"
#include "los_memory.h"
#include "los_mux.h"
#include "los_printf.h"
#include "securec.h"
#include "vnode.h"

#define LE16_TO_HOST(x) ((uint16_t)(x))
#define LE32_TO_HOST(x) ((uint32_t)(x))
#define LE64_TO_HOST(x) ((uint64_t)(x))
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
