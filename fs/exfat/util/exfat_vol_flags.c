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
#include "los_printf.h"
#include "disk.h"

/* Identity host->LE on LiteOS-A ARM (LE host); kept as wrapper so a future BE
 * port can replace this single macro instead of every call site. Mirrors the
 * convention in fs/exfat/util/exfat_fat_chain.c (Invariant
 * exfat-vol-flags-le16-on-disk). */
#define HOST_TO_LE16(x)  ((uint16_t)(x))

#define EXFAT_MAIN_BOOT_SECTOR  0u
#define EXFAT_BOOT_WRITE_COUNT  1u

/* ---------------------------------------------------------------------------
 * exfat_set_vol_flags — internal helper, mirrors Linux fs/exfat/super.c
 * exfat_set_vol_flags. Caller MUST hold sbi->s_lock; this routine itself does
 * not acquire / release any LiteOS-A primitive (Invariant
 * exfat-vol-flags-no-lock-acquire) and does not allocate (Invariant
 * exfat-vol-flags-no-realloc).
 *
 * Returns 0 on success (including the no-op case where final_flags equals the
 * current sbi->vol_flags) or -EIO when the synchronous boot-sector write-back
 * fails. -EINVAL is reserved for the public-API guards above this helper.
 * --------------------------------------------------------------------------- */
static int exfat_set_vol_flags(exfat_sb_info *sbi, uint16_t new_flags)
{
    struct exfat_boot_sector *p_boot;
    uint16_t final_flags;
    int ret;

    final_flags = (uint16_t)(new_flags | sbi->vol_flags_persistent);
    if (sbi->vol_flags == final_flags) {
        return 0;
    }

    sbi->vol_flags = final_flags;
    p_boot = (struct exfat_boot_sector *)sbi->boot_buf;
    p_boot->vol_flags = HOST_TO_LE16(final_flags);

    ret = los_part_write(sbi->part_id, sbi->boot_buf,
                         (UINT64)EXFAT_MAIN_BOOT_SECTOR,
                         (UINT32)EXFAT_BOOT_WRITE_COUNT);
    if (ret != 0) {
        PRINT_ERR("[%s] part_write failed: %d\n", __func__, ret);
        return -EIO;
    }
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_set_volume_dirty — sets VOLUME_DIRTY in sbi->vol_flags and writes the
 * main boot sector back. Wave B write paths (create / unlink / rename / write
 * / truncate) call this BEFORE staging on-disk mutations so that a power loss
 * mid-write leaves a recoverable marker.
 *
 * Caller must hold sbi->s_lock (LosMux). Returns 0 on success, -EINVAL when
 * sbi state is incomplete (NULL sbi, NULL boot_buf, zero blocksize), or -EIO
 * if los_part_write fails after the in-memory bookkeeping has been updated.
 * --------------------------------------------------------------------------- */
int exfat_set_volume_dirty(exfat_sb_info *sbi)
{
    if (sbi == NULL || sbi->boot_buf == NULL || sbi->blocksize == 0u) {
        return -EINVAL;
    }
    return exfat_set_vol_flags(sbi, (uint16_t)(sbi->vol_flags | VOLUME_DIRTY));
}

/* ---------------------------------------------------------------------------
 * exfat_clear_volume_dirty — clears VOLUME_DIRTY in sbi->vol_flags. The
 * persistent-merge inside exfat_set_vol_flags ensures MEDIA_FAILURE (and any
 * latched VOLUME_DIRTY in vol_flags_persistent) survives this call (Invariant
 * exfat-vol-flags-persistent-merged).
 *
 * Caller must hold sbi->s_lock. Same return contract as
 * exfat_set_volume_dirty.
 * --------------------------------------------------------------------------- */
int exfat_clear_volume_dirty(exfat_sb_info *sbi)
{
    if (sbi == NULL || sbi->boot_buf == NULL || sbi->blocksize == 0u) {
        return -EINVAL;
    }
    return exfat_set_vol_flags(sbi, (uint16_t)(sbi->vol_flags & ~VOLUME_DIRTY));
}

#endif /* LOSCFG_FS_EXFAT */

/* Assumptions made:
 * - On-disk byte order is little-endian (matches Linux upstream exFAT and ARM
 *   little-endian config for LiteOS-A QEMU virt + Hi3516).
 * - struct exfat_boot_sector is the packed layout from fs/exfat/include/
 *   exfat_raw.h; vol_flags lives at byte offset 106 (LE16) and is naturally
 *   2-byte aligned, so the cast-and-store is unaligned-access safe on ARM.
 * - los_part_write is synchronous; success implies the boot sector is durable
 *   on the underlying virtio-blk / MMC device.
 */
