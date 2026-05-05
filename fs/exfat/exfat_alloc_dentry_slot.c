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
#include "los_printf.h"

/* ---------------------------------------------------------------------------
 * exfat_alloc_dentry_slot
 *
 * Linear scan for `n_entries` contiguous "writable" slots in dir's chain.
 * "Writable" = either EXFAT_UNUSED (type 0x00, end-of-dir terminator) or
 * type with bit 7 cleared (deleted slot, e.g. 0x05 / 0x40 / 0x41).
 *
 * Hitting an EXFAT_UNUSED terminator extends the run all the way to the
 * end of the dir bound — Microsoft spec: every slot past terminator is also
 * unused. So once we hit a terminator we evaluate "remaining slots from
 * run_start" against n_entries and decide success or -ENOSPC right there
 * (no further IO).
 *
 * v1 does NOT grow the directory chain. -ENOSPC is final.
 * --------------------------------------------------------------------------- */
int exfat_alloc_dentry_slot(const exfat_sb_info *sbi, const exfat_chain *dir,
                            int n_entries, int *slot_idx_out)
{
    int max_dentries;
    int run_start;
    int i;
    struct exfat_dentry probe;
    int err;

    if (sbi == NULL || dir == NULL || slot_idx_out == NULL) {
        return -EINVAL;
    }
    if (n_entries < 1 || n_entries > (int)EXFAT_DENTRY_SET_MAX) {
        return -EINVAL;
    }
    if (dir->flags != (uint8_t)ALLOC_FAT_CHAIN &&
        dir->flags != (uint8_t)ALLOC_NO_FAT_CHAIN) {
        return -EINVAL;
    }
    if (dir->dir < EXFAT_FIRST_CLUSTER) {
        return -EINVAL;
    }
    if (sbi->dentries_per_clu == 0u || dir->size == 0u) {
        return -EINVAL;
    }

    max_dentries = (int)((uint64_t)dir->size * (uint64_t)sbi->dentries_per_clu);
    run_start = -1;

    for (i = 0; i < max_dentries; i++) {
        err = exfat_get_dentry(sbi, dir, i, &probe, NULL);
        if (err != 0) {
            return err;
        }

        /* Terminator: every slot from `i` onward is unused. */
        if (probe.type == EXFAT_UNUSED) {
            if (run_start < 0) {
                run_start = i;
            }
            if ((max_dentries - run_start) >= n_entries) {
                *slot_idx_out = run_start;
                return 0;
            }
            return -ENOSPC;
        }

        /* Deleted slot: bit 7 cleared but not the terminator. Reusable. */
        if ((probe.type & (uint8_t)0x80u) == 0u) {
            if (run_start < 0) {
                run_start = i;
            }
            if ((i - run_start + 1) >= n_entries) {
                *slot_idx_out = run_start;
                return 0;
            }
            continue;
        }

        /* In-use slot: reset run. */
        run_start = -1;
    }

    return -ENOSPC;
}

#endif /* LOSCFG_FS_EXFAT */

/* Assumptions:
 * - dir->size is in clusters; max_dentries = dir->size * dentries_per_clu.
 *   On overflow (huge dir), the multiply is uint64_t; the int cast is safe
 *   for any plausible exFAT directory (up to ~2M dentries).
 * - exfat_get_dentry's own chain-walk handles cluster boundaries; we only
 *   address linear entry indices here.
 * - The scan reads each dentry exactly once; no caching. Up to N IO.
 *   Performance optimization (sector-batch read) is deferred to v2.
 */
