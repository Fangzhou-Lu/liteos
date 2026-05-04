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

#define EXFAT_MAX_CHAIN_LEN 0x10000000u

static inline uint64_t bytes_to_cluster_count(uint64_t bytes, uint32_t cluster_size)
{
    if (cluster_size == 0u) {
        return 0;
    }
    return (bytes + cluster_size - 1u) / cluster_size;
}

int exfat_truncate_extend(exfat_sb_info *sbi, exfat_inode_info *ei,
                          uint64_t new_size)
{
    uint64_t num_new_clu_64;
    uint64_t num_phys_clu_64;
    uint32_t num_new_clu;
    uint32_t num_phys_clu;
    uint32_t num_to_alloc;
    uint32_t last_existing_clu;
    int      ret;
    int      vd_ret;

    if (sbi == NULL || ei == NULL) {
        return -EINVAL;
    }
    if (new_size <= ei->size) {
        return -EINVAL;
    }
    if (ei->flags != ALLOC_FAT_CHAIN) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u) {
        return -EINVAL;
    }
    if (new_size > sbi->s_maxbytes) {
        return -EFBIG;
    }

    num_new_clu_64  = bytes_to_cluster_count(new_size, sbi->cluster_size);
    num_phys_clu_64 = bytes_to_cluster_count(ei->i_size_ondisk, sbi->cluster_size);
    if (num_new_clu_64 > 0xFFFFFFFFull) {
        return -EFBIG;
    }
    num_new_clu  = (uint32_t)num_new_clu_64;
    num_phys_clu = (uint32_t)num_phys_clu_64;

    ret = exfat_set_volume_dirty(sbi);
    if (ret != 0) {
        PRINT_ERR("[%s] set_volume_dirty failed: %d\n", __func__, ret);
        return ret;
    }

    if (num_new_clu <= num_phys_clu) {
        ei->size = new_size;
        goto clear_dirty_out;
    }

    num_to_alloc = num_new_clu - num_phys_clu;

    last_existing_clu = EXFAT_EOF_CLUSTER;
    if (num_phys_clu > 0u && ei->start_clu != EXFAT_EOF_CLUSTER &&
        ei->start_clu != EXFAT_FREE_CLUSTER) {
        uint32_t cur   = ei->start_clu;
        uint32_t guard = 0u;
        uint32_t next  = 0u;
        while (guard + 1u < num_phys_clu && guard < EXFAT_MAX_CHAIN_LEN) {
            ret = exfat_get_next_cluster(sbi, cur, &next);
            if (ret != 0) {
                PRINT_ERR("[%s] walk to tail failed at clu=%u: %d\n",
                          __func__, cur, ret);
                goto clear_dirty_eio;
            }
            if (next == EXFAT_EOF_CLUSTER) {
                break;
            }
            cur = next;
            guard++;
        }
        last_existing_clu = cur;
    }

    {
        exfat_chain new_chain;
        new_chain.dir   = EXFAT_EOF_CLUSTER;
        new_chain.size  = 0u;
        new_chain.flags = ALLOC_FAT_CHAIN;

        ret = exfat_alloc_cluster(sbi, num_to_alloc, &new_chain);
        if (ret != 0) {
            vd_ret = exfat_clear_volume_dirty(sbi);
            if (vd_ret != 0) {
                PRINT_ERR("[%s] clear_volume_dirty after alloc fail: %d\n",
                          __func__, vd_ret);
            }
            return ret;
        }

        if (last_existing_clu == EXFAT_EOF_CLUSTER) {
            ei->start_clu = new_chain.dir;
        } else {
            ret = exfat_ent_set(sbi, last_existing_clu, new_chain.dir);
            if (ret != 0) {
                PRINT_ERR("[%s] link old tail %u -> new head %u failed: %d\n",
                          __func__, last_existing_clu, new_chain.dir, ret);
                (void)exfat_free_cluster(sbi, &new_chain);
                vd_ret = exfat_clear_volume_dirty(sbi);
                if (vd_ret != 0) {
                    PRINT_ERR("[%s] clear_volume_dirty after link fail: %d\n",
                              __func__, vd_ret);
                }
                return -EIO;
            }
        }

        ei->size          = new_size;
        ei->i_size_ondisk = (uint64_t)num_new_clu * (uint64_t)sbi->cluster_size;
    }

clear_dirty_out:
    vd_ret = exfat_clear_volume_dirty(sbi);
    if (vd_ret != 0) {
        PRINT_ERR("[%s] clear_volume_dirty (success path): %d\n",
                  __func__, vd_ret);
    }
    return 0;

clear_dirty_eio:
    vd_ret = exfat_clear_volume_dirty(sbi);
    if (vd_ret != 0) {
        PRINT_ERR("[%s] clear_volume_dirty (eio path): %d\n",
                  __func__, vd_ret);
    }
    return -EIO;
}

#endif /* LOSCFG_FS_EXFAT */
