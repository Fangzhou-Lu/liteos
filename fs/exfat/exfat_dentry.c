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
#include <string.h>
#include "securec.h"
#include "los_memory.h"
#include "los_printf.h"
#include "disk.h"

extern UINT8 *m_aucSysMem0;

/* Identity LE→host on LiteOS-A ARM (LE host); kept as wrappers for BE port. */
#define LE16_TO_HOST(x) ((uint16_t)(x))
#define LE32_TO_HOST(x) ((uint32_t)(x))
#define LE64_TO_HOST(x) ((uint64_t)(x))

/* ---------------------------------------------------------------------------
 * exfat_parse_boot_sector
 * --------------------------------------------------------------------------- */
int exfat_parse_boot_sector(exfat_sb_info *sbi,
                            const struct exfat_boot_sector *bs,
                            uint32_t logical_sector_size)
{
    static const uint8_t exfat_fs_name[BOOTSEC_FS_NAME_LEN] = {
        'E', 'X', 'F', 'A', 'T', ' ', ' ', ' '
    };
    uint32_t i;
    uint32_t sect_size;
    uint64_t fat_bytes;

    if (sbi == NULL || bs == NULL || logical_sector_size == 0) {
        return -EINVAL;
    }

    if (LE16_TO_HOST(bs->signature) != BOOT_SIGNATURE) {
        return -EINVAL;
    }
    if (memcmp(bs->fs_name, exfat_fs_name, BOOTSEC_FS_NAME_LEN) != 0) {
        return -EINVAL;
    }
    for (i = 0; i < BOOTSEC_OLDBPB_LEN; i++) {
        if (bs->must_be_zero[i] != 0) {
            return -EINVAL;
        }
    }
    if (bs->num_fats != 1 && bs->num_fats != 2) {
        return -EINVAL;
    }
    if (bs->sect_size_bits < EXFAT_MIN_SECT_SIZE_BITS ||
        bs->sect_size_bits > EXFAT_MAX_SECT_SIZE_BITS) {
        return -EINVAL;
    }
    if (bs->sect_per_clus_bits > (uint8_t)(25 - bs->sect_size_bits)) {
        return -EINVAL;
    }
    sect_size = 1u << bs->sect_size_bits;
    if (logical_sector_size != sect_size) {
        return -EINVAL;
    }

    /* —— Geometry —— all multi-byte fields go through LE*_TO_HOST per
     *    Invariant exfat-dentry-parse-byte-order. */
    sbi->sect_size_bits      = bs->sect_size_bits;
    sbi->sect_per_clus_bits  = bs->sect_per_clus_bits;
    sbi->num_fats            = bs->num_fats;
    sbi->partition_offset    = LE64_TO_HOST(bs->partition_offset);
    sbi->vol_length          = LE64_TO_HOST(bs->vol_length);
    sbi->fat_offset          = LE32_TO_HOST(bs->fat_offset);
    sbi->fat_length          = LE32_TO_HOST(bs->fat_length);
    sbi->fat2_offset         = (bs->num_fats == 2)
                               ? sbi->fat_offset + sbi->fat_length
                               : sbi->fat_offset;
    sbi->clu_offset          = LE32_TO_HOST(bs->clu_offset);
    sbi->num_clusters        = LE32_TO_HOST(bs->clu_count) + EXFAT_RESERVED_CLUSTERS;
    sbi->root_dir            = LE32_TO_HOST(bs->root_cluster);
    sbi->cluster_size_bits   = (uint32_t)bs->sect_size_bits + (uint32_t)bs->sect_per_clus_bits;
    sbi->cluster_size        = 1u << sbi->cluster_size_bits;
    sbi->blocksize           = sect_size;
    sbi->blocksize_bits      = (uint32_t)bs->sect_size_bits;
    sbi->dentries_per_clu    = sbi->cluster_size >> DENTRY_SIZE_BITS;
    sbi->vol_flags           = LE16_TO_HOST(bs->vol_flags);
    sbi->vol_flags_persistent = sbi->vol_flags & (uint16_t)(VOLUME_DIRTY | MEDIA_FAILURE);
    sbi->s_maxbytes          = (uint64_t)(sbi->num_clusters - EXFAT_RESERVED_CLUSTERS)
                               << sbi->cluster_size_bits;
    sbi->clu_srch_ptr        = EXFAT_FIRST_CLUSTER;
    sbi->used_clusters       = EXFAT_CLUSTERS_UNTRACKED;

    /* —— Cross-field consistency (mirrors Linux exfat_read_boot_sector tail) —— */
    fat_bytes = (uint64_t)sbi->fat_length << bs->sect_size_bits;
    if (fat_bytes < (uint64_t)sbi->num_clusters * 4u) {
        return -EINVAL;
    }
    if ((uint64_t)sbi->clu_offset <
        (uint64_t)sbi->fat_offset +
        (uint64_t)sbi->fat_length * (uint64_t)bs->num_fats) {
        return -EINVAL;
    }

    return 0;
}

/* ---------------------------------------------------------------------------
 * Internal: read a single FAT entry (FAT[clu]) into *next_clu.
 *
 * Returns 0 on success, -EIO on los_part_read failure, -ENOMEM on alloc fail.
 * --------------------------------------------------------------------------- */
static int ReadFatEntry(const exfat_sb_info *sbi, uint32_t clu, uint32_t *next_clu)
{
    uint64_t fat_byte_off;
    uint64_t fat_sector;
    uint32_t in_sector_off;
    uint8_t *fat_buf;
    int err = 0;

    fat_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->blocksize);
    if (fat_buf == NULL) {
        return -ENOMEM;
    }

    fat_byte_off  = (uint64_t)clu * 4u;
    fat_sector    = (uint64_t)sbi->fat_offset + (fat_byte_off / sbi->blocksize);
    in_sector_off = (uint32_t)(fat_byte_off % sbi->blocksize);

    if (los_part_read(sbi->part_id, fat_buf, fat_sector, 1u, TRUE) < 0) {
        err = -EIO;
        goto out;
    }

    {
        uint32_t raw = 0;
        (void)memcpy_s(&raw, sizeof(raw), fat_buf + in_sector_off, sizeof(raw));
        *next_clu = LE32_TO_HOST(raw);
    }

out:
    LOS_MemFree(m_aucSysMem0, fat_buf);
    return err;
}

/* ---------------------------------------------------------------------------
 * exfat_find_root_dentry
 *
 * FAT chain walk with bounded iterations (per Invariant
 * exfat-dentry-fat-traversal-bounded). All allocations released in
 * reverse-LIFO on every return path (per *-buf-leak-free).
 * --------------------------------------------------------------------------- */
int exfat_find_root_dentry(const exfat_sb_info *sbi, uint8_t type,
                           struct exfat_dentry *out)
{
    uint8_t *clu_buf = NULL;
    uint32_t cur_clu;
    uint32_t iter = 0;
    uint32_t sect_per_clus;
    int err = 0;

    if (sbi == NULL || out == NULL) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u || sbi->blocksize == 0u ||
        sbi->root_dir < EXFAT_FIRST_CLUSTER) {
        return -EINVAL;
    }

    sect_per_clus = 1u << sbi->sect_per_clus_bits;
    cur_clu = sbi->root_dir;

    clu_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->cluster_size);
    if (clu_buf == NULL) {
        return -ENOMEM;
    }

    while (iter < sbi->num_clusters) {
        uint64_t data_sector;
        uint32_t i;
        uint32_t next_clu = 0;

        if (cur_clu == EXFAT_EOF_CLUSTER) {
            err = -ENOENT;
            goto out;
        }
        if (cur_clu == EXFAT_BAD_CLUSTER || cur_clu == EXFAT_FREE_CLUSTER ||
            cur_clu < EXFAT_FIRST_CLUSTER || cur_clu >= sbi->num_clusters) {
            err = -EIO;
            goto out;
        }

        data_sector = (uint64_t)sbi->clu_offset +
                      (uint64_t)(cur_clu - EXFAT_FIRST_CLUSTER) * (uint64_t)sect_per_clus;
        if (los_part_read(sbi->part_id, clu_buf, data_sector, sect_per_clus, TRUE) < 0) {
            err = -EIO;
            goto out;
        }

        for (i = 0; i < sbi->dentries_per_clu; i++) {
            const struct exfat_dentry *ep =
                (const struct exfat_dentry *)(clu_buf + ((uint32_t)i << DENTRY_SIZE_BITS));
            if (ep->type == EXFAT_UNUSED) {
                err = -ENOENT;
                goto out;
            }
            if (ep->type == type) {
                if (memcpy_s(out, sizeof(*out), ep, sizeof(*ep)) != EOK) {
                    err = -EINVAL;
                } else {
                    err = 0;
                }
                goto out;
            }
        }

        err = ReadFatEntry(sbi, cur_clu, &next_clu);
        if (err != 0) {
            goto out;
        }
        cur_clu = next_clu;
        iter++;
    }

    /* Exceeded num_clusters iterations — corrupt FAT chain (loop). */
    err = -EIO;

out:
    LOS_MemFree(m_aucSysMem0, clu_buf);
    return err;
}

#endif /* LOSCFG_FS_EXFAT */
