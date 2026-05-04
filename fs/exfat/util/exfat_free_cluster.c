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
#include "los_mux.h"
#include "los_printf.h"
#include "disk.h"

#define EXFAT_MAX_CHAIN_LEN 0x10000000u

int exfat_clear_bitmap(exfat_sb_info *sbi, uint32_t clu)
{
    uint32_t ent_idx;
    uint32_t byte_in_amap;
    uint32_t bit_in_byte;
    uint32_t sector_in_amap;
    uint64_t target_sector;
    uint8_t *sector_buf;
    int ret;

    if (clu < EXFAT_FIRST_CLUSTER || clu >= sbi->num_clusters) {
        return -EINVAL;
    }

    ent_idx        = clu - EXFAT_RESERVED_CLUSTERS;
    byte_in_amap   = ent_idx / 8u;
    bit_in_byte    = ent_idx & 7u;
    sector_in_amap = byte_in_amap / sbi->blocksize;
    target_sector  = exfat_clu_to_sector(sbi, sbi->map_clu) +
                     (uint64_t)sector_in_amap;
    sector_buf     = sbi->vol_amap + (uint64_t)sector_in_amap * sbi->blocksize;

    sbi->vol_amap[byte_in_amap] &= (uint8_t)~(1u << bit_in_byte);

    ret = los_part_write(sbi->part_id, sector_buf, target_sector, 1u);
    if (ret != 0) {
        PRINT_ERR("[%s] part_write failed clu=%u sect=%llu: %d\n",
                  __func__, clu, (unsigned long long)target_sector, ret);
        return -EIO;
    }
    return 0;
}

static inline void sub_used_clusters(exfat_sb_info *sbi, uint32_t freed)
{
    if (sbi->used_clusters == EXFAT_CLUSTERS_UNTRACKED) {
        return;
    }
    if (sbi->used_clusters >= freed) {
        sbi->used_clusters -= freed;
    } else {
        sbi->used_clusters = 0u;
    }
}

int exfat_free_cluster(exfat_sb_info *sbi, const exfat_chain *p_chain)
{
    int      ret      = 0;
    uint32_t freed    = 0u;
    uint32_t clu;
    uint32_t next_clu;
    uint32_t loop_guard;

    LOS_MuxLock(&sbi->bitmap_lock, LOS_WAIT_FOREVER);

    if (p_chain->dir == EXFAT_FREE_CLUSTER ||
        p_chain->dir == EXFAT_EOF_CLUSTER ||
        p_chain->dir < EXFAT_FIRST_CLUSTER ||
        p_chain->size == 0u) {
        ret = 0;
        goto unlock_out;
    }

    if (p_chain->dir >= sbi->num_clusters) {
        PRINT_ERR("[%s] invalid start cluster %u (num=%u)\n",
                  __func__, p_chain->dir, sbi->num_clusters);
        ret = -EIO;
        goto unlock_out;
    }

    if (p_chain->flags == ALLOC_NO_FAT_CHAIN) {
        clu = p_chain->dir;
        while (freed < p_chain->size) {
            ret = exfat_clear_bitmap(sbi, clu);
            if (ret != 0) {
                goto unlock_out;
            }
            clu++;
            freed++;
        }
        goto unlock_out;
    }

    /* ALLOC_FAT_CHAIN: walk via exfat_get_next_cluster, bounded loop. */
    clu        = p_chain->dir;
    loop_guard = 0u;
    while (clu != EXFAT_EOF_CLUSTER && loop_guard < EXFAT_MAX_CHAIN_LEN) {
        ret = exfat_clear_bitmap(sbi, clu);
        if (ret != 0) {
            goto unlock_out;
        }
        freed++;

        ret = exfat_get_next_cluster(sbi, clu, &next_clu);
        if (ret != 0) {
            goto unlock_out;
        }
        clu = next_clu;
        loop_guard++;
    }

    if (loop_guard >= EXFAT_MAX_CHAIN_LEN && clu != EXFAT_EOF_CLUSTER) {
        PRINT_ERR("[%s] FAT chain bound exhausted (start=%u freed=%u)\n",
                  __func__, p_chain->dir, freed);
        ret = -EIO;
    }

unlock_out:
    sub_used_clusters(sbi, freed);
    LOS_MuxUnlock(&sbi->bitmap_lock);
    return ret;
}

#endif /* LOSCFG_FS_EXFAT */
