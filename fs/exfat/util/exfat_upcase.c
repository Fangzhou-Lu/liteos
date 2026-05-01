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

#define EXFAT_UPCASE_MAX_BYTES   (65536u * 2u)   /* full BMP, 128 KiB */

int exfat_create_upcase_table(exfat_sb_info *sbi)
{
    struct exfat_dentry dentry;
    uint32_t tbl_clu;
    uint64_t tbl_size;
    uint32_t expected_chksum;
    uint32_t actual_chksum;
    uint32_t num_sectors;
    uint64_t buf_bytes;
    uint64_t data_sector;
    uint32_t sect_per_clus;
    uint8_t *buf = NULL;
    int err;

    if (sbi == NULL || sbi->vol_utbl != NULL) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u || sbi->blocksize == 0u ||
        sbi->num_clusters < EXFAT_RESERVED_CLUSTERS) {
        return -EINVAL;
    }

    err = exfat_find_root_dentry(sbi, EXFAT_UPCASE, &dentry);
    if (err != 0) {
        return err;
    }

    tbl_clu  = LE32_TO_HOST(dentry.dentry.upcase.start_clu);
    tbl_size = LE64_TO_HOST(dentry.dentry.upcase.size);
    expected_chksum = LE32_TO_HOST(dentry.dentry.upcase.checksum);

    if (tbl_clu < EXFAT_FIRST_CLUSTER || tbl_clu >= sbi->num_clusters) {
        return -EINVAL;
    }
    if (tbl_size == 0u || (tbl_size & 1u) != 0u || tbl_size > EXFAT_UPCASE_MAX_BYTES) {
        return -EINVAL;
    }

    num_sectors = (uint32_t)((tbl_size + sbi->blocksize - 1u) / sbi->blocksize);
    if (num_sectors == 0u) {
        return -EINVAL;
    }
    buf_bytes = (uint64_t)num_sectors * (uint64_t)sbi->blocksize;

    buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, (UINT32)buf_bytes);
    if (buf == NULL) {
        return -ENOMEM;
    }

    sect_per_clus = 1u << sbi->sect_per_clus_bits;
    data_sector = (uint64_t)sbi->clu_offset +
                  (uint64_t)(tbl_clu - EXFAT_FIRST_CLUSTER) * (uint64_t)sect_per_clus;

    if (los_part_read(sbi->part_id, buf, data_sector, num_sectors, TRUE) < 0) {
        err = -EIO;
        goto err_free;
    }

    actual_chksum = exfat_calc_chksum32(buf, (uint32_t)tbl_size, 0u, CS_DEFAULT);
    if (actual_chksum != expected_chksum) {
        PRINT_ERR("exfat: upcase checksum mismatch (calc=0x%08x stored=0x%08x)\n",
                  actual_chksum, expected_chksum);
        err = -EINVAL;
        goto err_free;
    }

    /* On LE host (LiteOS-A ARM), the byte buffer reinterprets cleanly as
     * uint16_t[] with no per-entry swap (Invariant exfat-upcase-byte-order). */
    sbi->vol_utbl       = (uint16_t *)buf;
    sbi->vol_utbl_clu   = tbl_clu;
    sbi->vol_utbl_size  = tbl_size;
    return 0;

err_free:
    LOS_MemFree(m_aucSysMem0, buf);
    return err;
}

void exfat_free_upcase_table(exfat_sb_info *sbi)
{
    if (sbi == NULL) {
        return;
    }
    if (sbi->vol_utbl != NULL) {
        LOS_MemFree(m_aucSysMem0, sbi->vol_utbl);
        sbi->vol_utbl = NULL;
    }
    sbi->vol_utbl_size = 0u;
}

#endif /* LOSCFG_FS_EXFAT */
