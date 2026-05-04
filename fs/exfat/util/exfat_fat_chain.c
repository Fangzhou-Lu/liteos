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

/* Identity LE→host on LiteOS-A ARM (LE host); kept as wrapper so a future
 * BE port can replace this single macro instead of every call site.
 * Mirrors fs/exfat/exfat_dentry.c's local definition (Invariant
 * exfat-fat-chain-le-host-only). */
#define LE32_TO_HOST(x) ((uint32_t)(x))
#define HOST_TO_LE32(x) ((uint32_t)(x))

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

#endif /* LOSCFG_FS_EXFAT */

/* Assumptions made:
 * - On-disk byte order: little-endian (matches Linux upstream and ARMv7-A
 *   default LiteOS-A config). LE32_TO_HOST is identity here; defined locally
 *   like in exfat_dentry.c.
 * - FAT entries are always 4 bytes, naturally aligned within a 512-byte (or
 *   larger power-of-two) sector — entries never cross sector boundaries.
 *   memcpy_s used instead of direct cast to defend against any future packed
 *   accessor or unaligned-buffer scenario (cheap; one 4-byte copy).
 * - exfat_dentry.c's static ReadFatEntry helper is left in place; not
 *   refactored to call exfat_get_next_cluster in this stage to preserve the
 *   approved dentry stage's symbol surface and invariants. A follow-up
 *   refactor (separate stage, would require dentry stage re-approval) can
 *   collapse the duplication.
 */
