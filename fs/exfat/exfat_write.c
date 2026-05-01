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
#include "fs/mount.h"
#include "disk.h"

extern UINT8 *m_aucSysMem0;

/* ---------------------------------------------------------------------------
 * ExfatWriteGetClusterAt — walk fat chain idx steps from start_clu.
 *
 * Same semantics as exfat_file.c::ExfatGetClusterAt (Invariant
 * exfat-write-helper-duplication-temp). Wave B Stage 2 (truncate) will
 * promote the duplicated logic to fs/exfat/util/exfat_fat_chain.c as
 * exfat_pos_to_cluster, then delete both static copies.
 *
 * Caller already holds ei->inode_lock; this helper takes no additional lock.
 * --------------------------------------------------------------------------- */
static int ExfatWriteGetClusterAt(const exfat_sb_info *sbi, uint32_t start_clu,
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
 * VfsExfatWrite — file_operations_vfs.write callback (Wave B Stage 1).
 *
 * In-place overwrite only; clamped to [filep->f_pos, ei->size). No allocation,
 * no extending, no dentry persistence (deferred to Wave B2 / B6).
 *
 * Cluster-level read-modify-write: read whole cluster, memcpy_s the user data
 * into the affected byte range, write whole cluster back. Sector-level RMW
 * is a future Wave B optimization.
 *
 * Linux-faithful lock model: holds ei->inode_lock for the entire body
 * (mirroring upper-layer vfs_write's exclusive inode->i_rwsem). Does NOT
 * take sbi->s_lock (Linux exfat_get_block(create=0) doesn't either) nor
 * bitmap_lock (no allocation in B1).
 *
 * Short-write semantics: if any byte already written when an IO error hits,
 * return the short count (mirror Linux generic_file_write_iter). Only
 * zero-byte path returns negative errno.
 * --------------------------------------------------------------------------- */
ssize_t VfsExfatWrite(struct file *filep, const char *buf, size_t len)
{
    struct Vnode *vp;
    struct Mount *mount;
    exfat_sb_info *sbi;
    exfat_inode_info *ei;
    uint8_t *cluster_buf = NULL;
    uint64_t cur_off;
    uint64_t to_write;
    size_t   written = 0;
    int      ret;
    int      err = 0;

    if (len == 0) {
        return 0;                                       /* exfat-write-zero-len-fast-path */
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

    if (ei->type != TYPE_FILE) {
        return -EISDIR;                                 /* exfat-write-isdir-rejected */
    }

    (VOID)LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER);

    /* Invariant exfat-write-no-extend: do not extend past ei->size in B1. */
    if ((uint64_t)filep->f_pos >= ei->size) {
        (VOID)LOS_MuxUnlock(&ei->inode_lock);
        return 0;
    }

    /* Invariant exfat-write-clamp-by-size. */
    to_write = ei->size - (uint64_t)filep->f_pos;
    if (to_write > (uint64_t)len) {
        to_write = (uint64_t)len;
    }

    cluster_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->cluster_size);
    if (cluster_buf == NULL) {
        PRINT_ERR("[%s] alloc cluster_buf failed (cluster_size=%u)\n",
                  __func__, sbi->cluster_size);
        err = -ENOMEM;
        goto UNLOCK;
    }

    cur_off = (uint64_t)filep->f_pos;
    while ((uint64_t)written < to_write) {
        uint32_t clu_idx     = (uint32_t)(cur_off >> sbi->cluster_size_bits);
        uint32_t byte_in_clu = (uint32_t)(cur_off & (sbi->cluster_size - 1u));
        uint32_t cur_clu;
        uint64_t sect;
        uint32_t nr_sect;
        size_t   chunk;
        size_t   remain;

        ret = ExfatWriteGetClusterAt(sbi, ei->start_clu, ei->flags, clu_idx, &cur_clu);
        if (ret != 0) {
            PRINT_ERR("[%s] cluster walk failed at idx=%u: %d\n",
                      __func__, clu_idx, ret);
            err = ret;
            goto IO_ERR;
        }

        sect    = exfat_clu_to_sector(sbi, cur_clu);
        nr_sect = sbi->cluster_size >> sbi->blocksize_bits;

        /* RMW step 1: read existing cluster. Invariant
         * exfat-write-rmw-read-failure-aborts. */
        ret = los_part_read(sbi->part_id, cluster_buf, sect, nr_sect, TRUE);
        if (ret != 0) {
            PRINT_ERR("[%s] los_part_read failed clu=%u sect=%llu: %d\n",
                      __func__, cur_clu, sect, ret);
            err = -EIO;
            goto IO_ERR;
        }

        remain = (size_t)(to_write - (uint64_t)written);
        chunk  = (size_t)(sbi->cluster_size - byte_in_clu);
        if (chunk > remain) {
            chunk = remain;
        }

        /* RMW step 2: patch bytes in cluster_buf. */
        ret = memcpy_s(cluster_buf + byte_in_clu,
                       sbi->cluster_size - byte_in_clu,
                       buf + written, chunk);
        if (ret != EOK) {
            PRINT_ERR("[%s] memcpy_s failed: %d\n", __func__, ret);
            err = -EIO;
            goto IO_ERR;
        }

        /* RMW step 3: write whole cluster back. */
        ret = los_part_write(sbi->part_id, cluster_buf, sect, nr_sect);
        if (ret != 0) {
            PRINT_ERR("[%s] los_part_write failed clu=%u sect=%llu: %d\n",
                      __func__, cur_clu, sect, ret);
            err = -EIO;
            goto IO_ERR;
        }

        written += chunk;
        cur_off += chunk;
    }

    (void)LOS_MemFree(m_aucSysMem0, cluster_buf);
    filep->f_pos += (loff_t)written;
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)written;

IO_ERR:
    (void)LOS_MemFree(m_aucSysMem0, cluster_buf);
    /* Invariant exfat-write-short-write-on-mid-failure. */
    if (written > 0) {
        filep->f_pos += (loff_t)written;
        (VOID)LOS_MuxUnlock(&ei->inode_lock);
        return (ssize_t)written;
    }
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)err;

UNLOCK:
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)err;
}

#endif /* LOSCFG_FS_EXFAT */
