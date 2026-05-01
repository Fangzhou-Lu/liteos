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
#include <dirent.h>
#include "securec.h"
#include "los_memory.h"
#include "los_mux.h"
#include "los_printf.h"
#include "vnode.h"
#include "mount.h"
#include "fs/dirent_fs.h"

extern UINT8 *m_aucSysMem0;

/* Identity LE→host on LiteOS-A ARMv7-A LE host (Invariant
 * exfat-readdir-le-host-only). */
#define LE16_TO_HOST(x) ((uint16_t)(x))

/* ---------------------------------------------------------------------------
 * ExfatReaddirExtractName8 — pull UTF-16 filename from a validated dentry-set
 * and convert to UTF-8 in-place into the caller-supplied buffer.
 *
 * Returns 0 on success with NUL-terminated UTF-8 name in `out`. Returns
 * -EINVAL if set shape is malformed or UTF-8 output would overflow `out_max`.
 * Pure compute (no IO/lock/alloc).
 * --------------------------------------------------------------------------- */
static int ExfatReaddirExtractName8(const struct exfat_dentry *set, int num_entries,
                                    uint16_t *uni, int uni_max,
                                    char *out, int out_max)
{
    int nm_len;
    int needed;
    int written = 0;
    int u8_len;
    int i;
    int j;
    int chunk;

    if (set == NULL || uni == NULL || out == NULL || num_entries < 2 ||
        uni_max <= 0 || out_max <= 1) {
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

    /* exfat_uni_to_utf8 returns bytes written excluding NUL on success, or
     * -ENAMETOOLONG / negative on failure. Reserve 1 byte for trailing NUL. */
    u8_len = exfat_uni_to_utf8(uni, nm_len, out, out_max - 1);
    if (u8_len < 0) {
        return -EINVAL;
    }
    if (u8_len >= out_max) {
        return -EINVAL;
    }
    out[u8_len] = '\0';
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatOpendir — vop->Opendir callback. Initializes per-DIR cursor.
 * No exfat lock acquired (idir is per-fd private; no shared state touched).
 * --------------------------------------------------------------------------- */
int VfsExfatOpendir(struct Vnode *vp, struct fs_dirent_s *idir)
{
    if (vp == NULL || idir == NULL) {
        return -EINVAL;
    }
    if (vp->type != VNODE_TYPE_DIR) {
        return -EINVAL;
    }

    idir->fd_int_offset = 0;
    idir->fd_position = 0;
    idir->u.fs_dir = NULL;
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatReaddir — vop->Readdir callback. Linux-faithful s_lock model
 * (fs/exfat/dir.c::exfat_iterate at lines 229/285).
 *
 * Continues from idir->fd_int_offset (entry_idx), filling at most
 * idir->read_cnt entries into idir->fd_dir[]. Returns number filled
 * (>= 0; 0 = end-of-dir / read_cnt==0). Negative = hard error.
 *
 * Two-step scan (peek primary, then full set fetch on EXFAT_FILE) mirrors
 * lookup. Single-entry IO errors skip-and-advance; only zero-fill+at-least-
 * one-IO-err returns -EIO.
 * --------------------------------------------------------------------------- */
int VfsExfatReaddir(struct Vnode *vp, struct fs_dirent_s *idir)
{
    exfat_sb_info       *sbi = NULL;
    exfat_inode_info    *ei = NULL;
    uint16_t            *uni_buf = NULL;
    struct exfat_dentry *set_buf = NULL;
    exfat_chain          dir;
    int                  max_dentries;
    int                  entry_idx;
    int                  filled = 0;
    int                  had_io_err = 0;
    int                  locked = 0;
    int                  ret = 0;
    int                  err;

    /* Parameter-level checks (no s_lock). */
    if (vp == NULL || idir == NULL) {
        return -EINVAL;
    }
    if (vp->type != VNODE_TYPE_DIR || vp->data == NULL ||
        vp->originMount == NULL || vp->originMount->data == NULL) {
        return -EINVAL;
    }
    if (idir->read_cnt <= 0) {
        return 0; /* No request → no work, no lock. */
    }

    sbi = (exfat_sb_info *)vp->originMount->data;
    ei = (exfat_inode_info *)vp->data;
    if (sbi->cluster_size == 0u || sbi->dentries_per_clu == 0u) {
        return -EIO;
    }

    /* Allocate working buffers. uni_buf for UTF-16 staging, set_buf for one
     * dentry-set. Total ~1.1 KiB. */
    uni_buf = (uint16_t *)LOS_MemAlloc(m_aucSysMem0,
        (uint32_t)(EXFAT_MAX_NAME_LEN * sizeof(uint16_t)));
    if (uni_buf == NULL) {
        ret = ENOMEM;
        goto ERROR_EXIT;
    }
    set_buf = (struct exfat_dentry *)LOS_MemAlloc(m_aucSysMem0,
        (uint32_t)(EXFAT_DENTRY_SET_MAX * sizeof(struct exfat_dentry)));
    if (set_buf == NULL) {
        ret = ENOMEM;
        goto ERROR_FREE_UNI;
    }

    /* Acquire FS-global lock — Linux exfat_iterate discipline. */
    if (LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER) != LOS_OK) {
        ret = EIO;
        goto ERROR_FREE_SET;
    }
    locked = 1;

    /* Build parent chain. */
    dir.dir = ei->start_clu;
    dir.flags = ei->flags;
    if (ei->size > 0u) {
        dir.size = (uint32_t)((ei->size + sbi->cluster_size - 1u) /
                              sbi->cluster_size);
    } else {
        dir.size = 0u; /* root with implicit size — stop-on-unused gates us. */
    }

    /* Compute scan upper bound (Invariant exfat-readdir-bounded). */
    max_dentries = MAX_EXFAT_DENTRIES;
    if (dir.size != 0u) {
        uint64_t bound = (uint64_t)dir.size * (uint64_t)sbi->dentries_per_clu;

        if (bound < (uint64_t)MAX_EXFAT_DENTRIES) {
            max_dentries = (int)bound;
        }
    }

    /* Continue from saved cursor. */
    entry_idx = (int)idir->fd_int_offset;
    if (entry_idx < 0) {
        entry_idx = 0;
    }

    while (entry_idx < max_dentries && filled < idir->read_cnt) {
        struct exfat_dentry primary;
        struct dirent *dirp;
        int num_entries = 0;
        int dst_max;

        err = exfat_get_dentry(sbi, &dir, entry_idx, &primary, NULL);
        if (err == -EIO) {
            had_io_err = 1;
            entry_idx++;
            continue;
        }
        if (err != 0) {
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

        /* Decode name into the destination dirent slot directly. */
        dirp = &idir->fd_dir[filled];
        dst_max = (int)sizeof(dirp->d_name);
        if (ExfatReaddirExtractName8(set_buf, num_entries, uni_buf,
                                     EXFAT_MAX_NAME_LEN,
                                     dirp->d_name, dst_max) != 0) {
            /* Name decode failure (invalid UTF-16 or > d_name capacity) —
             * skip this entry per Invariant exfat-readdir-utf8-only. */
            entry_idx += num_entries;
            continue;
        }

        {
            uint16_t attr = LE16_TO_HOST(set_buf[0].dentry.file.attr);

            dirp->d_type   = (attr & ATTR_SUBDIR) ? DT_DIR : DT_REG;
            dirp->d_reclen = (uint16_t)sizeof(struct dirent);
            idir->fd_position++;
            dirp->d_off = idir->fd_position;
        }

        entry_idx += num_entries;
        filled++;
    }

    /* Persist cursor for next call. */
    idir->fd_int_offset = entry_idx;

    /* Decide return code. */
    if (filled > 0) {
        ret = 0;
        LOS_MuxUnlock(&sbi->s_lock);
        locked = 0;
        LOS_MemFree(m_aucSysMem0, set_buf);
        LOS_MemFree(m_aucSysMem0, uni_buf);
        return filled;
    }
    if (had_io_err) {
        ret = EIO;
        goto ERROR_UNLOCK;
    }
    /* End-of-directory: filled == 0, no IO error. */
    LOS_MuxUnlock(&sbi->s_lock);
    locked = 0;
    LOS_MemFree(m_aucSysMem0, set_buf);
    LOS_MemFree(m_aucSysMem0, uni_buf);
    return 0;

ERROR_UNLOCK:
    if (locked) {
        LOS_MuxUnlock(&sbi->s_lock);
        locked = 0;
    }
ERROR_FREE_SET:
    LOS_MemFree(m_aucSysMem0, set_buf);
ERROR_FREE_UNI:
    LOS_MemFree(m_aucSysMem0, uni_buf);
ERROR_EXIT:
    return -ret;
}

/* ---------------------------------------------------------------------------
 * VfsExfatClosedir — vop->Closedir callback. No-op (Opendir didn't allocate).
 * --------------------------------------------------------------------------- */
int VfsExfatClosedir(struct Vnode *vp, struct fs_dirent_s *idir)
{
    if (vp == NULL || idir == NULL) {
        return -EINVAL;
    }
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatRewinddir — vop->Rewinddir callback. Reset cursor to 0.
 * --------------------------------------------------------------------------- */
int VfsExfatRewinddir(struct Vnode *vp, struct fs_dirent_s *idir)
{
    if (vp == NULL || idir == NULL) {
        return -EINVAL;
    }
    idir->fd_int_offset = 0;
    idir->fd_position = 0;
    return 0;
}

#endif /* LOSCFG_FS_EXFAT */

/* Assumptions made:
 * - struct dirent::d_name capacity comes from libc / VFS dirent header
 *   (typically 256 bytes incl. NUL). UTF-8 names exceeding that are skipped
 *   per Invariant exfat-readdir-utf8-only.
 * - "." / ".." are NOT synthesized here; VFS upper layer or libc handles
 *   them (consistent with fatfs_readdir behavior).
 * - idir->u.fs_dir stays NULL throughout this stage; future hint-cache
 *   stage can populate it without breaking the contract.
 * - When filled > 0 + had_io_err, we return the partial count (success-
 *   ish) rather than -EIO. Caller's next Readdir call will encounter the
 *   bad entries again from where we left off — at which point zero-fill
 *   condition triggers -EIO. This mirrors lookup's per-call IO tolerance.
 */
