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
#include <stddef.h>
#include <stdint.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <limits.h>
#include "securec.h"
#include "los_mux.h"
#include "vnode.h"
#include "fs/mount.h"

/* POSIX st_blocks unit. */
#define EXFAT_STAT_BLOCK_SIZE 512u

/* off_t in LiteOS-A musl is signed 64-bit (int64_t-equivalent); SUSv4 lseek
 * returns EOVERFLOW when the resulting offset would exceed this. */
#define EXFAT_OFF_MAX ((off_t)0x7FFFFFFFFFFFFFFFLL)

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

#endif /* LOSCFG_FS_EXFAT */
