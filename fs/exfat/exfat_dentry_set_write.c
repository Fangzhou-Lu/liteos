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
#include <stdint.h>
#include <stddef.h>
#include "securec.h"
#include "los_memory.h"
#include "los_printf.h"
#include "disk.h"

extern UINT8 *m_aucSysMem0;

/* ---------------------------------------------------------------------------
 * exfat_set_dentry
 *
 * Write one 32B dentry into the directory chain at linear index entry_idx.
 * Strictly mirrors exfat_get_dentry's chain-walk + sector-locator path; the
 * payload step replaces "memcpy from buf to caller's out" with
 * "memcpy from caller's in into buf, then los_part_write the sector back".
 *
 * Invariants honoured: no locks (callee), leak-free buf, cluster-boundary
 * correctness (recomputed per-call), no chksum touch, no vol_flags touch.
 * --------------------------------------------------------------------------- */
int exfat_set_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                     int entry_idx, const struct exfat_dentry *in)
{
    uint8_t *buf = NULL;
    uint64_t byte_off;
    uint64_t sector_lba;
    uint32_t clu_offset;
    uint32_t byte_in_clu;
    uint32_t sector_in_clu;
    uint32_t byte_in_sector;
    uint32_t cur_clu;
    uint32_t i;
    int err = 0;

    if (sbi == NULL || dir == NULL || in == NULL || entry_idx < 0) {
        return -EINVAL;
    }
    if (dir->flags != (uint8_t)ALLOC_FAT_CHAIN &&
        dir->flags != (uint8_t)ALLOC_NO_FAT_CHAIN) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u || sbi->blocksize == 0u ||
        dir->dir < EXFAT_FIRST_CLUSTER) {
        return -EINVAL;
    }

    byte_off       = (uint64_t)(uint32_t)entry_idx * (uint64_t)DENTRY_SIZE;
    clu_offset     = (uint32_t)(byte_off / sbi->cluster_size);
    byte_in_clu    = (uint32_t)(byte_off % sbi->cluster_size);
    sector_in_clu  = byte_in_clu / sbi->blocksize;
    byte_in_sector = byte_in_clu % sbi->blocksize;

    /* Walk to the target cluster — same logic as exfat_get_dentry. */
    cur_clu = dir->dir;
    if (dir->flags == (uint8_t)ALLOC_NO_FAT_CHAIN) {
        cur_clu += clu_offset;
        if (cur_clu < EXFAT_FIRST_CLUSTER || cur_clu >= sbi->num_clusters) {
            return -EIO;
        }
    } else {
        for (i = 0; i < clu_offset; i++) {
            uint32_t next_clu = 0;
            err = exfat_get_next_cluster(sbi, cur_clu, &next_clu);
            if (err != 0) {
                return err;
            }
            if (next_clu == EXFAT_EOF_CLUSTER) {
                return -EIO;
            }
            cur_clu = next_clu;
        }
        err = 0;
    }

    sector_lba = exfat_clu_to_sector(sbi, cur_clu) + (uint64_t)sector_in_clu;

    buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->blocksize);
    if (buf == NULL) {
        return -ENOMEM;
    }

    if (los_part_read(sbi->part_id, buf, sector_lba, 1u, TRUE) < 0) {
        err = -EIO;
        goto out;
    }

    if (memcpy_s(buf + byte_in_sector,
                 (size_t)sbi->blocksize - (size_t)byte_in_sector,
                 in, sizeof(*in)) != EOK) {
        err = -EIO;
        goto out;
    }

    if (los_part_write(sbi->part_id, buf, sector_lba, 1u) < 0) {
        err = -EIO;
        goto out;
    }

out:
    LOS_MemFree(m_aucSysMem0, buf);
    return err;
}

/* ---------------------------------------------------------------------------
 * exfat_set_dentry_set
 *
 * Loop over num_entries calling exfat_set_dentry. Per Invariant
 * exfat-set-dentry-set-no-rollback, partial commits are NOT undone — caller
 * must mark vol_flags VOLUME_DIRTY in the failure path.
 * --------------------------------------------------------------------------- */
int exfat_set_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
                         int start_entry, const struct exfat_dentry *set,
                         int num_entries)
{
    int i;
    int err;

    if (sbi == NULL || dir == NULL || set == NULL) {
        return -EINVAL;
    }
    if (start_entry < 0 || num_entries < 1 ||
        num_entries > (int)EXFAT_DENTRY_SET_MAX) {
        return -EINVAL;
    }

    for (i = 0; i < num_entries; i++) {
        err = exfat_set_dentry(sbi, dir, start_entry + i, &set[i]);
        if (err != 0) {
            return err;
        }
    }
    return 0;
}

#endif /* LOSCFG_FS_EXFAT */

/* Assumptions:
 * - los_part_write returns negative on failure; the returned -EIO does not
 *   distinguish "write didn't happen" from "write happened partially". Caller
 *   responsible for VOLUME_DIRTY on -EIO.
 * - memcpy_s bounds: dest size = blocksize - byte_in_sector. Since dentry
 *   alignment is 32B and sector is >= 512B, byte_in_sector + 32 <= blocksize
 *   always holds; the guard is defence-in-depth.
 * - dir/sbi/in are not retained across the call — no captured pointer state.
 */
