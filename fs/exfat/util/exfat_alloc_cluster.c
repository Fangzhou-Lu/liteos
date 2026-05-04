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

int exfat_set_bitmap(exfat_sb_info *sbi, uint32_t clu)
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

    sbi->vol_amap[byte_in_amap] |= (uint8_t)(1u << bit_in_byte);

    ret = los_part_write(sbi->part_id, sector_buf, target_sector, 1u);
    if (ret != 0) {
        PRINT_ERR("[%s] part_write failed clu=%u sect=%llu: %d\n",
                  __func__, clu, (unsigned long long)target_sector, ret);
        return -EIO;
    }
    return 0;
}

int exfat_find_free_bitmap(const exfat_sb_info *sbi, uint32_t hint_clu, uint32_t *out_clu)
{
    uint32_t start;
    uint32_t scanned;
    uint32_t total;

    if (out_clu == NULL) {
        return -EINVAL;
    }

    total = sbi->num_clusters - EXFAT_RESERVED_CLUSTERS;
    if (total == 0u) {
        *out_clu = EXFAT_EOF_CLUSTER;
        return -ENOSPC;
    }

    if (hint_clu < EXFAT_FIRST_CLUSTER || hint_clu >= sbi->num_clusters) {
        start = EXFAT_FIRST_CLUSTER;
    } else {
        start = hint_clu;
    }

    for (scanned = 0u; scanned < total && scanned < EXFAT_MAX_CHAIN_LEN; scanned++) {
        uint32_t clu = start + scanned;
        if (clu >= sbi->num_clusters) {
            clu = EXFAT_FIRST_CLUSTER + (clu - sbi->num_clusters);
        }
        uint32_t ent_idx     = clu - EXFAT_RESERVED_CLUSTERS;
        uint32_t byte_in_amap = ent_idx / 8u;
        uint32_t bit_in_byte  = ent_idx & 7u;
        uint8_t  byte         = sbi->vol_amap[byte_in_amap];
        if ((byte & (uint8_t)(1u << bit_in_byte)) == 0u) {
            *out_clu = clu;
            return 0;
        }
    }

    *out_clu = EXFAT_EOF_CLUSTER;
    return -ENOSPC;
}

static uint32_t alloc_inline_rollback(exfat_sb_info *sbi, uint32_t start_clu,
                                      uint32_t orphan_clu)
{
    uint32_t freed = 0u;
    uint32_t guard = 0u;
    uint32_t clu   = start_clu;
    uint32_t next_clu;

    while (clu != EXFAT_EOF_CLUSTER && clu != EXFAT_FREE_CLUSTER &&
           clu >= EXFAT_FIRST_CLUSTER && clu < sbi->num_clusters &&
           guard < EXFAT_MAX_CHAIN_LEN) {
        (void)exfat_clear_bitmap(sbi, clu);
        freed++;
        if (exfat_get_next_cluster(sbi, clu, &next_clu) != 0) {
            break;
        }
        clu = next_clu;
        guard++;
    }

    if (orphan_clu != EXFAT_EOF_CLUSTER && orphan_clu != EXFAT_FREE_CLUSTER &&
        orphan_clu >= EXFAT_FIRST_CLUSTER && orphan_clu < sbi->num_clusters) {
        (void)exfat_clear_bitmap(sbi, orphan_clu);
        freed++;
    }

    return freed;
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

int exfat_alloc_cluster(exfat_sb_info *sbi, uint32_t num_alloc, exfat_chain *p_chain)
{
    int      ret;
    uint32_t hint_clu;
    uint32_t orig_dir;
    uint32_t orig_size;
    uint32_t orig_srch_ptr;
    uint32_t allocated = 0u;
    uint32_t first_clu = EXFAT_EOF_CLUSTER;
    uint32_t last_clu  = EXFAT_EOF_CLUSTER;
    uint32_t total_data;

    if (p_chain == NULL || num_alloc == 0u) {
        return -EINVAL;
    }
    if (p_chain->flags != ALLOC_FAT_CHAIN) {
        return -EINVAL;
    }

    total_data = sbi->num_clusters - EXFAT_RESERVED_CLUSTERS;

    if (sbi->used_clusters != EXFAT_CLUSTERS_UNTRACKED &&
        num_alloc > total_data - sbi->used_clusters) {
        return -ENOSPC;
    }

    LOS_MuxLock(&sbi->bitmap_lock, LOS_WAIT_FOREVER);

    orig_dir      = p_chain->dir;
    orig_size     = p_chain->size;
    orig_srch_ptr = sbi->clu_srch_ptr;

    if (p_chain->dir != EXFAT_EOF_CLUSTER && p_chain->dir != EXFAT_FREE_CLUSTER &&
        p_chain->dir >= EXFAT_FIRST_CLUSTER && p_chain->dir < sbi->num_clusters) {
        hint_clu = p_chain->dir;
    } else if (sbi->clu_srch_ptr >= EXFAT_FIRST_CLUSTER &&
               sbi->clu_srch_ptr < sbi->num_clusters) {
        hint_clu = sbi->clu_srch_ptr;
    } else {
        hint_clu = EXFAT_FIRST_CLUSTER;
    }

    while (allocated < num_alloc) {
        uint32_t new_clu;

        ret = exfat_find_free_bitmap(sbi, hint_clu, &new_clu);
        if (ret != 0) {
            goto rollback;
        }

        ret = exfat_set_bitmap(sbi, new_clu);
        if (ret != 0) {
            (void)alloc_inline_rollback(sbi, first_clu, new_clu);
            sub_used_clusters(sbi, allocated);
            p_chain->dir  = orig_dir;
            p_chain->size = orig_size;
            sbi->clu_srch_ptr = orig_srch_ptr;
            LOS_MuxUnlock(&sbi->bitmap_lock);
            return ret;
        }

        ret = exfat_ent_set(sbi, new_clu, EXFAT_EOF_CLUSTER);
        if (ret != 0) {
            (void)alloc_inline_rollback(sbi, first_clu, new_clu);
            sub_used_clusters(sbi, allocated);
            p_chain->dir  = orig_dir;
            p_chain->size = orig_size;
            sbi->clu_srch_ptr = orig_srch_ptr;
            LOS_MuxUnlock(&sbi->bitmap_lock);
            return ret;
        }

        if (first_clu == EXFAT_EOF_CLUSTER) {
            first_clu = new_clu;
        } else {
            ret = exfat_ent_set(sbi, last_clu, new_clu);
            if (ret != 0) {
                (void)alloc_inline_rollback(sbi, first_clu, new_clu);
                sub_used_clusters(sbi, allocated);
                p_chain->dir  = orig_dir;
                p_chain->size = orig_size;
                sbi->clu_srch_ptr = orig_srch_ptr;
                LOS_MuxUnlock(&sbi->bitmap_lock);
                return ret;
            }
        }

        last_clu = new_clu;
        allocated++;
        if (sbi->used_clusters != EXFAT_CLUSTERS_UNTRACKED) {
            sbi->used_clusters++;
        }

        hint_clu = new_clu + 1u;
        if (hint_clu >= sbi->num_clusters) {
            hint_clu = EXFAT_FIRST_CLUSTER;
        }
    }

    if (orig_dir == EXFAT_EOF_CLUSTER || orig_dir == EXFAT_FREE_CLUSTER ||
        orig_dir < EXFAT_FIRST_CLUSTER || orig_dir >= sbi->num_clusters) {
        p_chain->dir = first_clu;
    }
    p_chain->size = orig_size + allocated;
    sbi->clu_srch_ptr = last_clu;

    LOS_MuxUnlock(&sbi->bitmap_lock);
    return 0;

rollback:
    (void)alloc_inline_rollback(sbi, first_clu, EXFAT_EOF_CLUSTER);
    sub_used_clusters(sbi, allocated);
    p_chain->dir  = orig_dir;
    p_chain->size = orig_size;
    sbi->clu_srch_ptr = orig_srch_ptr;
    LOS_MuxUnlock(&sbi->bitmap_lock);
    return -ENOSPC;
}

#endif /* LOSCFG_FS_EXFAT */
