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
/* exfat_cluster — cluster management: FAT chain (fatent) + bitmap allocation
 * (balloc) + alloc/free cluster. Mirrors Linux fs/exfat/{fatent,balloc}.c. */

#include <errno.h>
#include <stddef.h>
#include <stdint.h>

#include "exfat.h"
#include "disk.h"
#include "los_memory.h"
#include "los_mux.h"
#include "los_printf.h"
#include "securec.h"

#define LE32_TO_HOST(x) ((uint32_t)(x))
#define HOST_TO_LE32(x) ((uint32_t)(x))
#define LE64_TO_HOST(x) ((uint64_t)(x))
#define EXFAT_MAX_CHAIN_LEN 0x10000000u

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

/* ----- merged from exfat_fat_chain.c ----- */

extern UINT8 *m_aucSysMem0;

/* Identity LE→host on LiteOS-A ARM (LE host); kept as wrapper so a future
 * BE port can replace this single macro instead of every call site.
 * Mirrors fs/exfat/exfat_dentry.c's local definition (Invariant
 * exfat-fat-chain-le-host-only). */

/* ---------------------------------------------------------------------------
 * exfat_get_next_cluster
 *
 * Read FAT[cur_clu] into *next_clu. Promotes the dentry stage's private
 * ReadFatEntry helper to a public API; sequence mirrors Linux __exfat_ent_get
 * + exfat_ent_get validation:
 *   1. Range-check cur_clu against [EXFAT_FIRST_CLUSTER, num_clusters).
 *   2. Read one FAT sector via los_part_read.
 *   3. Extract LE32 entry, remap raw > EXFAT_BAD_CLUSTER to EXFAT_EOF_CLUSTER
 *      (Linux __exfat_ent_get policy — Invariant exfat-fat-chain-reserved-remap).
 *   4. Reject FREE / BAD / out-of-range non-EOF results.
 *
 * fat_buf released on every return path (Invariant exfat-fat-chain-leak-free).
 * Read-only — no los_part_write call (Invariant exfat-fat-chain-readonly).
 * --------------------------------------------------------------------------- */
int exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                           uint32_t *next_clu)
{
    uint8_t *fat_buf = NULL;
    uint64_t fat_byte_off;
    uint64_t fat_sector;
    uint32_t in_sector_off;
    uint32_t raw = 0;
    uint32_t mapped;
    int err = 0;

    if (sbi == NULL || next_clu == NULL) {
        return -EINVAL;
    }
    if (cur_clu < EXFAT_FIRST_CLUSTER || cur_clu >= sbi->num_clusters) {
        return -EIO;
    }
    if (sbi->blocksize == 0u) {
        return -EIO;
    }

    fat_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->blocksize);
    if (fat_buf == NULL) {
        return -ENOMEM;
    }

    fat_byte_off  = (uint64_t)cur_clu * 4u;
    fat_sector    = (uint64_t)sbi->fat_offset + (fat_byte_off / sbi->blocksize);
    in_sector_off = (uint32_t)(fat_byte_off % sbi->blocksize);

    if (los_part_read(sbi->part_id, fat_buf, fat_sector, 1u, TRUE) < 0) {
        err = -EIO;
        goto out;
    }

    if (memcpy_s(&raw, sizeof(raw), fat_buf + in_sector_off,
                 sizeof(raw)) != EOK) {
        err = -EIO;
        goto out;
    }
    raw = LE32_TO_HOST(raw);

    /* Linux __exfat_ent_get: remap reserved (raw > BAD) to EOF prior to
     * validation. Sequence is fixed by exfat-fat-chain-reserved-remap. */
    if (raw > EXFAT_BAD_CLUSTER) {
        mapped = EXFAT_EOF_CLUSTER;
    } else {
        mapped = raw;
    }

    /* Validate mapped value: reject FREE / BAD / out-of-range non-EOF. */
    if (mapped == EXFAT_FREE_CLUSTER) {
        err = -EIO;
        goto out;
    }
    if (mapped == EXFAT_BAD_CLUSTER) {
        err = -EIO;
        goto out;
    }
    if (mapped != EXFAT_EOF_CLUSTER &&
        (mapped < EXFAT_FIRST_CLUSTER || mapped >= sbi->num_clusters)) {
        err = -EIO;
        goto out;
    }

    *next_clu = mapped;
    err = 0;

out:
    LOS_MemFree(m_aucSysMem0, fat_buf);
    return err;
}

/* ---------------------------------------------------------------------------
 * exfat_chain_walk
 *
 * Bounded FAT chain traversal. Visits each cluster of the chain starting at
 * start_clu, invoking visitor(clu, ctx) per visit. Visitor return values:
 *   0   → continue to next cluster
 *   1   → stop walk (success)
 *   <0  → stop walk, propagate as exfat_chain_walk's return value
 *
 * Bound: at most sbi->num_clusters iterations. Reaching the bound without
 * encountering EOF returns -EIO (Invariant exfat-fat-chain-bounded — defends
 * against corrupt FAT cycles).
 *
 * No locks acquired (Invariant exfat-fat-chain-no-locks). Calling context
 * must not hold any LiteOS spinlock (Invariant exfat-fat-chain-no-spinlock-
 * callsite) because exfat_get_next_cluster performs LOS_MemAlloc + IO.
 * --------------------------------------------------------------------------- */
int exfat_chain_walk(const exfat_sb_info *sbi, uint32_t start_clu,
                     exfat_chain_visitor_t visitor, void *ctx)
{
    uint32_t cur_clu;
    uint32_t iter = 0;

    if (sbi == NULL || visitor == NULL) {
        return -EINVAL;
    }
    if (start_clu == EXFAT_EOF_CLUSTER) {
        return 0;
    }

    cur_clu = start_clu;

    while (iter < sbi->num_clusters) {
        uint32_t next_clu = 0;
        int v;
        int err;

        v = visitor(cur_clu, ctx);
        if (v == 1) {
            return 0;
        }
        if (v < 0) {
            return v;
        }
        /* v == 0: continue to next cluster. */

        err = exfat_get_next_cluster(sbi, cur_clu, &next_clu);
        if (err != 0) {
            return err;
        }
        if (next_clu == EXFAT_EOF_CLUSTER) {
            return 0;
        }

        cur_clu = next_clu;
        iter++;
    }

    /* Bound exceeded — corrupt FAT chain (loop). */
    return -EIO;
}

/* ---------------------------------------------------------------------------
 * exfat_ent_set
 *
 * Write FAT[loc] = value. Mirrors to FAT2 when sbi->num_fats == 2.
 * Sequence (Invariant exfat-ent-set-mirror-byte-exact + bounded-per-call):
 *   1. Range-check loc against [EXFAT_FIRST_CLUSTER, num_clusters).
 *   2. Validate value: must be EOF, FREE, or a legal cluster index.
 *      EXFAT_BAD_CLUSTER explicitly rejected (Invariant
 *      exfat-ent-set-rejects-bad-cluster).
 *   3. Compute fat_byte_off = loc * 4; fat_sector = fat_offset + off/blocksize;
 *      in_sector_off = off % blocksize.
 *   4. Read FAT1 sector via los_part_read.
 *   5. Patch the 4-byte LE32 entry in fat_buf via memcpy_s.
 *   6. Write FAT1 sector via los_part_write.
 *   7. If num_fats == 2, write the SAME fat_buf to fat2_offset + delta
 *      (Invariant exfat-ent-set-mirror-byte-exact: byte-exact reuse).
 *   8. Free fat_buf.
 *
 * Caller-side locking (Invariant exfat-ent-set-no-locks): function takes
 * no lock. Truncate-shrink / fsync paths hold ei->inode_lock; alloc_cluster
 * holds sbi->bitmap_lock. See spec Refine Prompt for the lock matrix.
 * --------------------------------------------------------------------------- */
int exfat_ent_set(const exfat_sb_info *sbi, uint32_t loc, uint32_t value)
{
    uint8_t *fat_buf = NULL;
    uint64_t fat_byte_off;
    uint64_t fat_sector;
    uint32_t in_sector_off;
    uint32_t le_value;
    int err = 0;

    if (sbi == NULL) {
        return -EINVAL;
    }
    if (sbi->blocksize == 0u) {
        return -EIO;
    }
    /* Invariant exfat-ent-set-validates-value-range. */
    if (loc < EXFAT_FIRST_CLUSTER || loc >= sbi->num_clusters) {
        return -EINVAL;
    }
    /* Invariant exfat-ent-set-rejects-bad-cluster. */
    if (value == EXFAT_BAD_CLUSTER) {
        return -EINVAL;
    }
    if (value != EXFAT_EOF_CLUSTER && value != EXFAT_FREE_CLUSTER) {
        if (value < EXFAT_FIRST_CLUSTER || value >= sbi->num_clusters) {
            return -EINVAL;
        }
    }

    fat_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->blocksize);
    if (fat_buf == NULL) {
        return -ENOMEM;
    }

    fat_byte_off  = (uint64_t)loc * 4u;
    fat_sector    = (uint64_t)sbi->fat_offset + (fat_byte_off / sbi->blocksize);
    in_sector_off = (uint32_t)(fat_byte_off % sbi->blocksize);

    /* RMW step 1: read FAT1 sector. */
    if (los_part_read(sbi->part_id, fat_buf, fat_sector, 1u, TRUE) < 0) {
        err = -EIO;
        goto out;
    }

    /* RMW step 2: patch entry as LE32 in-place. memcpy_s avoids unaligned
     * write on hosts where in_sector_off may not be 4-aligned (FAT entries
     * always are, but defensive). */
    le_value = HOST_TO_LE32(value);
    if (memcpy_s(fat_buf + in_sector_off, sizeof(uint32_t),
                 &le_value, sizeof(uint32_t)) != EOK) {
        err = -EIO;
        goto out;
    }

    /* RMW step 3: write FAT1 sector back. */
    if (los_part_write(sbi->part_id, fat_buf, fat_sector, 1u) < 0) {
        err = -EIO;
        goto out;
    }

    /* Mirror to FAT2 when configured. Invariant
     * exfat-ent-set-mirror-byte-exact: reuse the SAME fat_buf — never
     * re-encode the value or read FAT2 separately. */
    if (sbi->num_fats == 2u) {
        uint64_t fat2_sector = (uint64_t)sbi->fat2_offset +
                               (fat_byte_off / sbi->blocksize);
        if (los_part_write(sbi->part_id, fat_buf, fat2_sector, 1u) < 0) {
            /* Partial-commit window: FAT1 is new, FAT2 is old. Caller must
             * coordinate via vol_flags VOLUME_DIRTY (Wave B6 fsync).
             * See spec Case 7. */
            err = -EIO;
            goto out;
        }
    }

    err = 0;

out:
    /* Invariant exfat-ent-set-buf-leak-free. */
    LOS_MemFree(m_aucSysMem0, fat_buf);
    return err;
}

/* ----- merged from exfat_balloc.c ----- */

extern UINT8 *m_aucSysMem0;

/* Byte popcount lookup table — 8-bit input, returns count of set bits. */

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

/* ----- merged from exfat_free_cluster.c ----- */

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

/* ----- merged from exfat_alloc_cluster.c ----- */

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

/* sub_used_clusters: defined earlier (in the free_cluster section above). */

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
