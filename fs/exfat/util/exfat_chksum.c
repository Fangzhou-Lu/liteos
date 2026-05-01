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

#include <stdint.h>

/*
 * exFAT-specific checksum (Microsoft exFAT specification §3.1.1.4 / §6.3.3 / §7.2.5).
 * Fixed algorithm: 1-bit right rotate + 8-bit byte add. NOT an IEEE 802.3 CRC32 —
 * do NOT replace with LOS_Crc32 (different polynomial, incompatible result).
 */

uint32_t exfat_calc_chksum32(const void *data, uint32_t len,
                             uint32_t chksum, int type)
{
    const uint8_t *c = (const uint8_t *)data;
    uint32_t i;

    for (i = 0; i < len; i++) {
        if (type == CS_BOOT_SECTOR && (i == 106u || i == 107u || i == 112u)) {
            continue;
        }
        chksum = ((chksum << 31) | (chksum >> 1)) + (uint32_t)c[i];
    }
    return chksum;
}

uint16_t exfat_calc_chksum16(const void *data, int len,
                             uint16_t chksum, int type)
{
    const uint8_t *c = (const uint8_t *)data;
    int i;

    for (i = 0; i < len; i++) {
        if (type == CS_DIR_ENTRY && (i == 2 || i == 3)) {
            continue;
        }
        chksum = (uint16_t)(((chksum << 15) | (chksum >> 1)) + (uint16_t)c[i]);
    }
    return chksum;
}

#endif /* LOSCFG_FS_EXFAT */
