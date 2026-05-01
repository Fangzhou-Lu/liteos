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
#include "los_memory.h"
#include "los_printf.h"
#include "disk.h"

extern UINT8 *m_aucSysMem0;

#define LE32_TO_HOST(x) ((uint32_t)(x))
#define LE64_TO_HOST(x) ((uint64_t)(x))

/* Byte popcount lookup table — 8-bit input, returns count of set bits. */
static const uint8_t g_byte_popcount[256] = {
    0,1,1,2,1,2,2,3, 1,2,2,3,2,3,3,4,
    1,2,2,3,2,3,3,4, 2,3,3,4,3,4,4,5,
    1,2,2,3,2,3,3,4, 2,3,3,4,3,4,4,5,
    2,3,3,4,3,4,4,5, 3,4,4,5,4,5,5,6,
    1,2,2,3,2,3,3,4, 2,3,3,4,3,4,4,5,
    2,3,3,4,3,4,4,5, 3,4,4,5,4,5,5,6,
    2,3,3,4,3,4,4,5, 3,4,4,5,4,5,5,6,
    3,4,4,5,4,5,5,6, 4,5,5,6,5,6,6,7,
    1,2,2,3,2,3,3,4, 2,3,3,4,3,4,4,5,
    2,3,3,4,3,4,4,5, 3,4,4,5,4,5,5,6,
    2,3,3,4,3,4,4,5, 3,4,4,5,4,5,5,6,
    3,4,4,5,4,5,5,6, 4,5,5,6,5,6,6,7,
    2,3,3,4,3,4,4,5, 3,4,4,5,4,5,5,6,
    3,4,4,5,4,5,5,6, 4,5,5,6,5,6,6,7,
    3,4,4,5,4,5,5,6, 4,5,5,6,5,6,6,7,
    4,5,5,6,5,6,6,7, 5,6,6,7,6,7,7,8,
};

/* ---------------------------------------------------------------------------
 * exfat_load_bitmap
 * --------------------------------------------------------------------------- */
int exfat_load_bitmap(exfat_sb_info *sbi)
{
    struct exfat_dentry dentry;
    uint32_t map_start_clu;
    uint64_t map_size;
    uint64_t need_map_size;
    uint64_t data_cluster_count;
    uint32_t map_sectors;
    uint64_t buf_bytes;
    uint64_t data_sector;
    uint32_t sect_per_clus;
    int err;

    if (sbi == NULL || sbi->vol_amap != NULL) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u || sbi->blocksize == 0u ||
        sbi->num_clusters < EXFAT_RESERVED_CLUSTERS) {
        return -EINVAL;
    }

    err = exfat_find_root_dentry(sbi, EXFAT_BITMAP, &dentry);
    if (err != 0) {
        return err;
    }

    map_start_clu = LE32_TO_HOST(dentry.dentry.bitmap.start_clu);
    map_size      = LE64_TO_HOST(dentry.dentry.bitmap.size);

    if (map_start_clu < EXFAT_FIRST_CLUSTER || map_start_clu >= sbi->num_clusters) {
        return -EIO;
    }

    data_cluster_count = (uint64_t)EXFAT_DATA_CLUSTER_COUNT(sbi);
    if (data_cluster_count == 0u) {
        return -EIO;
    }
    need_map_size = (data_cluster_count - 1u) / 8u + 1u;

    if (need_map_size > map_size) {
        PRINT_ERR("exfat: bitmap too small (need=%llu, on-disk=%llu)\n",
                  need_map_size, map_size);
        return -EIO;
    }
    if (need_map_size < map_size) {
        PRINT_WARN("exfat: bitmap padding (need=%llu, on-disk=%llu)\n",
                   need_map_size, map_size);
    }

    map_sectors = (uint32_t)((need_map_size + sbi->blocksize - 1u) / sbi->blocksize);
    if (map_sectors == 0u) {
        return -EIO;
    }
    buf_bytes = (uint64_t)map_sectors * (uint64_t)sbi->blocksize;

    sbi->vol_amap = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, (UINT32)buf_bytes);
    if (sbi->vol_amap == NULL) {
        return -ENOMEM;
    }

    sect_per_clus = 1u << sbi->sect_per_clus_bits;
    data_sector = (uint64_t)sbi->clu_offset +
                  (uint64_t)(map_start_clu - EXFAT_FIRST_CLUSTER) * (uint64_t)sect_per_clus;

    if (los_part_read(sbi->part_id, sbi->vol_amap, data_sector, map_sectors, TRUE) < 0) {
        LOS_MemFree(m_aucSysMem0, sbi->vol_amap);
        sbi->vol_amap = NULL;
        return -EIO;
    }

    sbi->map_clu     = map_start_clu;
    sbi->map_sectors = map_sectors;
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_free_bitmap — idempotent.
 * --------------------------------------------------------------------------- */
void exfat_free_bitmap(exfat_sb_info *sbi)
{
    if (sbi == NULL) {
        return;
    }
    if (sbi->vol_amap != NULL) {
        LOS_MemFree(m_aucSysMem0, sbi->vol_amap);
        sbi->vol_amap = NULL;
    }
    sbi->map_sectors = 0u;
}

/* ---------------------------------------------------------------------------
 * exfat_count_used_clusters — byte-walk popcount with tail mask.
 * --------------------------------------------------------------------------- */
int exfat_count_used_clusters(const exfat_sb_info *sbi, uint32_t *out)
{
    uint32_t data_clusters;
    uint32_t full_bytes;
    uint32_t tail_bits;
    uint32_t count = 0;
    uint32_t i;

    if (sbi == NULL || out == NULL || sbi->vol_amap == NULL) {
        return -EINVAL;
    }

    data_clusters = (sbi->num_clusters > EXFAT_RESERVED_CLUSTERS)
                    ? (sbi->num_clusters - EXFAT_RESERVED_CLUSTERS)
                    : 0u;
    full_bytes = data_clusters / 8u;
    tail_bits  = data_clusters % 8u;

    for (i = 0; i < full_bytes; i++) {
        count += (uint32_t)g_byte_popcount[sbi->vol_amap[i]];
    }
    if (tail_bits != 0u) {
        uint8_t tail_mask = (uint8_t)((1u << tail_bits) - 1u);
        count += (uint32_t)g_byte_popcount[sbi->vol_amap[full_bytes] & tail_mask];
    }

    *out = count;
    return 0;
}

#endif /* LOSCFG_FS_EXFAT */
