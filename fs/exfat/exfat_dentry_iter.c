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

/* Identity LE→host wrapper; mirrors exfat_dentry.c / exfat_fat_chain.c
 * (Invariant exfat-dentry-iter-le-host-only). */
#define LE16_TO_HOST(x) ((uint16_t)(x))

/* ---------------------------------------------------------------------------
 * exfat_get_dentry
 *
 * Locate the cluster + sector holding the dentry at linear index `entry_idx`,
 * read that sector, and copy the 32B dentry into *out. ALLOC_NO_FAT_CHAIN
 * directories are addressed by simple addition (contiguous); ALLOC_FAT_CHAIN
 * directories require walking the FAT chain via exfat_get_next_cluster.
 *
 * Bounded: chain walk stops on EOF (-EIO if entry_idx not yet reached).
 * Leak-free: single LOS_MemAlloc + matching LOS_MemFree at out: label.
 * --------------------------------------------------------------------------- */
int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                     int entry_idx, struct exfat_dentry *out,
                     uint64_t *out_sector)
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

    if (sbi == NULL || dir == NULL || out == NULL || entry_idx < 0) {
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

    /* Skip clu_offset clusters along dir's chain. */
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

    if (memcpy_s(out, sizeof(*out),
                 buf + byte_in_sector, sizeof(*out)) != EOK) {
        err = -EIO;
        goto out;
    }

    if (out_sector != NULL) {
        *out_sector = sector_lba;
    }

out:
    LOS_MemFree(m_aucSysMem0, buf);
    return err;
}

/* ---------------------------------------------------------------------------
 * exfat_get_dentry_set
 *
 * Pull a contiguous file dentry-set from the directory chain. Each dentry is
 * fetched by an independent exfat_get_dentry call so cluster-boundary cases
 * are handled transparently (Invariant exfat-dentry-iter-set-cluster-boundary
 * — alternative big-read implementations are explicitly forbidden by spec).
 *
 * On any failure, *num_entries is left undefined and the caller must treat
 * `set` as garbage (Invariant exfat-dentry-iter-set-not-mutated-on-failure).
 * --------------------------------------------------------------------------- */
int exfat_get_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
                         int start_entry, struct exfat_dentry *set,
                         int max_entries, int *num_entries)
{
    int total;
    int i;
    int err;

    if (sbi == NULL || dir == NULL || set == NULL || num_entries == NULL) {
        return -EINVAL;
    }
    if (start_entry < 0 || max_entries < 1 ||
        max_entries > (int)EXFAT_DENTRY_SET_MAX) {
        return -EINVAL;
    }

    /* Step 1: read the primary at start_entry. */
    err = exfat_get_dentry(sbi, dir, start_entry, &set[0], NULL);
    if (err != 0) {
        return err;
    }

    /* Step 2: validate it's an in-use file primary. */
    if (set[0].type != (uint8_t)EXFAT_FILE) {
        return -EIO;
    }

    /* Step 3: derive total count from primary.num_ext. */
    total = 1 + (int)set[0].dentry.file.num_ext;
    if (total > (int)EXFAT_DENTRY_SET_MAX) {
        return -EIO;   /* primary declares more secondaries than spec allows */
    }
    if (total > max_entries) {
        return -EIO;   /* caller buffer too small */
    }

    /* Step 4: read each secondary; validate bit7-set "in-use" + known type. */
    for (i = 1; i < total; i++) {
        uint8_t t;
        err = exfat_get_dentry(sbi, dir, start_entry + i, &set[i], NULL);
        if (err != 0) {
            return err;
        }
        t = set[i].type;
        /* in-use marker = bit7 set; v1 only accepts known secondary types. */
        if ((t & (uint8_t)0x80u) == 0u) {
            return -EIO;   /* not in-use */
        }
        if (t != (uint8_t)EXFAT_STREAM && t != (uint8_t)EXFAT_NAME) {
            return -EIO;   /* unknown / vendor secondary — v1 rejects */
        }
    }

    *num_entries = total;
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_validate_dentry_set
 *
 * Pure compute. exfat_calc_chksum16 with CS_DIR_ENTRY skips bytes 2-3 of
 * set[0] (the SetChecksum field itself); the result is compared against
 * set[0].dentry.file.checksum after LE16 decode.
 * --------------------------------------------------------------------------- */
int exfat_validate_dentry_set(const struct exfat_dentry *set, int num_entries)
{
    uint16_t computed;
    uint16_t expected;

    if (set == NULL) {
        return -EINVAL;
    }
    if (num_entries < 1 || num_entries > (int)EXFAT_DENTRY_SET_MAX) {
        return -EINVAL;
    }
    if (set[0].type != (uint8_t)EXFAT_FILE) {
        return -EINVAL;
    }

    computed = exfat_calc_chksum16(set,
                                    num_entries * (int)DENTRY_SIZE,
                                    0,
                                    CS_DIR_ENTRY);
    expected = LE16_TO_HOST(set[0].dentry.file.checksum);

    if (computed != expected) {
        return -EIO;
    }
    return 0;
}

#endif /* LOSCFG_FS_EXFAT */

/* Assumptions made:
 * - Packed-struct field accesses (set[0].dentry.file.num_ext / .checksum)
 *   are emitted as byte-by-byte loads by the LiteOS-A clang ARM toolchain
 *   on naturally-aligned set buffers. Caller-supplied set[] should be at
 *   least 32-bit aligned (typical stack-allocated array of struct exfat_dentry
 *   is 1-byte aligned but ARM clang issues unaligned-safe accessors for
 *   packed unions).
 * - Secondary-type validation is strict v1: only EXFAT_STREAM (0xC0) and
 *   EXFAT_NAME (0xC1) are accepted. Vendor secondaries (0xE0/0xE1) and
 *   any reserved in-use type are rejected with -EIO. This matches the
 *   spec invariant exfat-dentry-iter-set-primary-required v1 strict policy.
 * - On-disk byte order: little-endian (matches Linux upstream).
 * - dentry-set buffer (struct exfat_dentry [N]) lifetime is the caller's;
 *   this module never retains a pointer to it across calls.
 */
