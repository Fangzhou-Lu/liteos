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
#include "securec.h"
#include "los_memory.h"
#include "los_mux.h"
#include "los_printf.h"
#include "vnode.h"
#include "fs/mount.h"

extern UINT8 *m_aucSysMem0;

/* Identity LE→host on LiteOS-A ARMv7-A LE host; kept as macro so future
 * BE port can change one place (Invariant exfat-lookup-le-host-only). */
#define LE16_TO_HOST(x) ((uint16_t)(x))
#define LE32_TO_HOST(x) ((uint32_t)(x))
#define LE64_TO_HOST(x) ((uint64_t)(x))

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

#endif /* LOSCFG_FS_EXFAT */

/* Assumptions made:
 * - On-disk byte order: little-endian (matches Linux upstream and ARMv7-A
 *   LiteOS-A LE host). LE16/32/64_TO_HOST macros are identity here; defined
 *   locally to mirror exfat_dentry.c / exfat_fat_chain.c.
 * - Packed dentry fields are read by indexing into already-buffered
 *   struct exfat_dentry copies returned by exfat_get_dentry / exfat_get_
 *   dentry_set; alignment is therefore controlled by the caller's buffer
 *   (heap-aligned via LOS_MemAlloc), not by direct on-disk pointer access.
 * - VFS layer does not auto-take parent inode_lock around .Lookup; this
 *   matches Linux exfat code which only takes sbi->s_lock. v1 single-thread
 *   read path makes this safe; Wave A has no concurrent mutator of parent_ei.
 * - VfsHashInsert failure leaks the freshly-allocated Vnode by design — VFS
 *   eventually reaps it via the alloc/free list (consistent with fatfs_lookup
 *   behavior). v1 doesn't try to handle this rare OOM-like failure better.
 */
