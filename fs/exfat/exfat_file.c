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
#include <sys/types.h>
#include "securec.h"
#include "los_memory.h"
#include "los_mux.h"
#include "los_printf.h"
#include "vnode.h"
#include "mount.h"
#include "disk.h"

extern UINT8 *m_aucSysMem0;

/* ---------------------------------------------------------------------------
 * ExfatGetClusterAt — walk fat chain idx steps from start_clu.
 *
 * Caller already holds ei->inode_lock (Invariant exfat-read-fat-chain-walk).
 * No additional lock taken by this helper.
 *
 *   ALLOC_NO_FAT_CHAIN  → contiguous run, *out = start_clu + idx (linear).
 *   ALLOC_FAT_CHAIN     → step idx times via exfat_get_next_cluster.
 *
 * Returns 0 / -EINVAL on chain breakage / -EIO from get_next_cluster.
 * Mid-walk EOF / FREE / out-of-range cluster numbers map to -EINVAL because
 * the spec forbids idx falling outside the chain (caller has clamped to_read
 * by ei->size, so the offset must lie inside the allocated chain).
 * --------------------------------------------------------------------------- */
static int ExfatGetClusterAt(const exfat_sb_info *sbi, uint32_t start_clu,
                             uint8_t flags, uint32_t idx, uint32_t *out_clu)
{
    uint32_t cur = start_clu;
    uint32_t next;
    uint32_t i;
    int ret;

    if (start_clu < EXFAT_FIRST_CLUSTER ||
        start_clu >= sbi->num_clusters + EXFAT_FIRST_CLUSTER) {
        return -EINVAL;
    }

    if (flags == ALLOC_NO_FAT_CHAIN) {
        cur = start_clu + idx;
        if (cur < EXFAT_FIRST_CLUSTER ||
            cur >= sbi->num_clusters + EXFAT_FIRST_CLUSTER) {
            return -EINVAL;
        }
        *out_clu = cur;
        return 0;
    }

    /* ALLOC_FAT_CHAIN — step idx times. */
    for (i = 0; i < idx; i++) {
        ret = exfat_get_next_cluster(sbi, cur, &next);
        if (ret != 0) {
            return ret;
        }
        if (next == EXFAT_EOF_CLUSTER || next == EXFAT_FREE_CLUSTER ||
            next < EXFAT_FIRST_CLUSTER ||
            next >= sbi->num_clusters + EXFAT_FIRST_CLUSTER) {
            return -EINVAL;
        }
        cur = next;
    }

    *out_clu = cur;
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatRead — file_operations_vfs.read callback.
 *
 * Linux-faithful lock model (Invariant exfat-read-linux-i-rwsem-faithful):
 * holds ei->inode_lock for the entire body, single-point release. Mirrors
 * Linux upper-layer VFS auto-acquired inode->i_rwsem (shared) during vfs_read.
 * Does NOT take sbi->s_lock (Linux exfat_get_block on read path doesn't).
 * Does NOT take sbi->bitmap_lock (no allocation).
 *
 * Short-read semantics (Invariant exfat-read-pos-advance-exact):
 *   On IO failure mid-loop, if any bytes already copied → return copied (>0)
 *   and advance f_pos accordingly; otherwise return negative errno and leave
 *   f_pos untouched.
 * --------------------------------------------------------------------------- */
ssize_t VfsExfatRead(struct file *filep, char *buf, size_t len)
{
    struct Vnode *vp;
    struct Mount *mount;
    exfat_sb_info *sbi;
    exfat_inode_info *ei;
    uint8_t *cluster_buf = NULL;
    uint64_t cur_off;
    uint64_t to_read;
    size_t   copied = 0;
    int      ret;
    int      err = 0;

    if (len == 0) {
        return 0;
    }
    if (filep == NULL || buf == NULL) {
        return -EINVAL;
    }

    vp = filep->f_vnode;
    if (vp == NULL || vp->originMount == NULL || vp->data == NULL) {
        return -EINVAL;
    }
    mount = vp->originMount;
    sbi = (exfat_sb_info *)mount->data;
    ei  = (exfat_inode_info *)vp->data;
    if (sbi == NULL) {
        return -EINVAL;
    }

    /* Invariant exfat-read-isdir-rejected. */
    if (ei->type != TYPE_FILE) {
        return -EISDIR;
    }

    (VOID)LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER);

    /* Invariant exfat-read-eof-returns-zero. */
    if ((uint64_t)filep->f_pos >= ei->size) {
        (VOID)LOS_MuxUnlock(&ei->inode_lock);
        return 0;
    }

    /* Invariant exfat-read-clamp-by-size. */
    to_read = ei->size - (uint64_t)filep->f_pos;
    if (to_read > (uint64_t)len) {
        to_read = (uint64_t)len;
    }

    cluster_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->cluster_size);
    if (cluster_buf == NULL) {
        PRINT_ERR("[%s] alloc cluster_buf failed (cluster_size=%u)\n",
                  __func__, sbi->cluster_size);
        err = -ENOMEM;
        goto UNLOCK;
    }

    cur_off = (uint64_t)filep->f_pos;
    while ((uint64_t)copied < to_read) {
        uint32_t clu_idx     = (uint32_t)(cur_off >> sbi->cluster_size_bits);
        uint32_t byte_in_clu = (uint32_t)(cur_off & (sbi->cluster_size - 1u));
        uint32_t cur_clu;
        uint64_t sect;
        uint32_t nr_sect;
        size_t   chunk;
        size_t   remain;

        ret = ExfatGetClusterAt(sbi, ei->start_clu, ei->flags, clu_idx, &cur_clu);
        if (ret != 0) {
            PRINT_ERR("[%s] cluster walk failed at idx=%u: %d\n",
                      __func__, clu_idx, ret);
            err = ret;
            goto IO_ERR;
        }

        sect    = exfat_clu_to_sector(sbi, cur_clu);
        nr_sect = sbi->cluster_size >> sbi->blocksize_bits;

        ret = los_part_read(sbi->part_id, cluster_buf, sect, nr_sect, TRUE);
        if (ret != 0) {
            PRINT_ERR("[%s] los_part_read failed clu=%u sect=%llu: %d\n",
                      __func__, cur_clu, sect, ret);
            err = -EIO;
            goto IO_ERR;
        }

        remain = (size_t)(to_read - (uint64_t)copied);
        chunk  = (size_t)(sbi->cluster_size - byte_in_clu);
        if (chunk > remain) {
            chunk = remain;
        }

        ret = memcpy_s(buf + copied, remain,
                       cluster_buf + byte_in_clu, chunk);
        if (ret != EOK) {
            PRINT_ERR("[%s] memcpy_s failed: %d\n", __func__, ret);
            err = -EIO;
            goto IO_ERR;
        }

        copied  += chunk;
        cur_off += chunk;
    }

    (void)LOS_MemFree(m_aucSysMem0, cluster_buf);
    filep->f_pos += (loff_t)copied;
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)copied;

IO_ERR:
    (void)LOS_MemFree(m_aucSysMem0, cluster_buf);
    /* Short-read semantics: report bytes already copied, ignore err. */
    if (copied > 0) {
        filep->f_pos += (loff_t)copied;
        (VOID)LOS_MuxUnlock(&ei->inode_lock);
        return (ssize_t)copied;
    }
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)err;

UNLOCK:
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)err;
}

#endif /* LOSCFG_FS_EXFAT */
