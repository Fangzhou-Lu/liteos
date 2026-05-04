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

static int walk_to_new_tail(const exfat_sb_info *sbi, uint32_t start_clu,
                            uint32_t hops, uint32_t *new_tail_out)
{
    uint32_t cur   = start_clu;
    uint32_t next  = 0u;
    uint32_t guard = 0u;
    int      ret;

    while (guard < hops) {
        if (guard >= EXFAT_MAX_CHAIN_LEN) {
            return -EIO;
        }
        ret = exfat_get_next_cluster(sbi, cur, &next);
        if (ret != 0) {
            return ret;
        }
        if (next == EXFAT_EOF_CLUSTER || next == EXFAT_FREE_CLUSTER) {
            return -EIO;
        }
        cur = next;
        guard++;
    }
    *new_tail_out = cur;
    return 0;
}

int exfat_truncate_shrink(exfat_sb_info *sbi, exfat_inode_info *ei,
                          uint64_t new_size)
{
    uint64_t num_new_clu_64;
    uint64_t num_phys_clu_64;
    uint32_t num_new_clu;
    uint32_t num_phys_clu;
    int      ret;
    int      vd_ret;

    if (sbi == NULL || ei == NULL) {
        return -EINVAL;
    }
    if (new_size >= ei->size) {
        return -EINVAL;
    }
    if (ei->flags != ALLOC_FAT_CHAIN) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u) {
        return -EINVAL;
    }

    num_new_clu_64  = bytes_to_cluster_count(new_size, sbi->cluster_size);
    num_phys_clu_64 = bytes_to_cluster_count(ei->i_size_ondisk, sbi->cluster_size);
    num_new_clu  = (uint32_t)num_new_clu_64;
    num_phys_clu = (uint32_t)num_phys_clu_64;

    ret = exfat_set_volume_dirty(sbi);
    if (ret != 0) {
        PRINT_ERR("[%s] set_volume_dirty failed: %d\n", __func__, ret);
        return ret;
    }

    if (num_new_clu >= num_phys_clu) {
        ei->size = new_size;
        if (ei->valid_size > new_size) {
            ei->valid_size = new_size;
        }
        goto clear_dirty_out;
    }

    if (num_new_clu == 0u) {
        exfat_chain whole_chain;
        whole_chain.dir   = ei->start_clu;
        whole_chain.size  = num_phys_clu;
        whole_chain.flags = ALLOC_FAT_CHAIN;

        ei->size          = 0u;
        ei->i_size_ondisk = 0u;
        ei->valid_size    = 0u;
        ei->start_clu     = EXFAT_EOF_CLUSTER;

        ret = exfat_free_cluster(sbi, &whole_chain);
        if (ret != 0) {
            PRINT_ERR("[%s] free entire chain (start=%u, n=%u) failed: %d\n",
                      __func__, whole_chain.dir, num_phys_clu, ret);
            vd_ret = exfat_clear_volume_dirty(sbi);
            if (vd_ret != 0) {
                PRINT_ERR("[%s] clear_volume_dirty after free fail: %d\n",
                          __func__, vd_ret);
            }
            return -EIO;
        }
        goto clear_dirty_out;
    }

    {
        uint32_t new_tail_clu      = EXFAT_EOF_CLUSTER;
        uint32_t first_discard_clu = EXFAT_EOF_CLUSTER;

        ret = walk_to_new_tail(sbi, ei->start_clu, num_new_clu - 1u, &new_tail_clu);
        if (ret != 0) {
            PRINT_ERR("[%s] walk to new tail failed: %d\n", __func__, ret);
            goto clear_dirty_eio;
        }

        ret = exfat_get_next_cluster(sbi, new_tail_clu, &first_discard_clu);
        if (ret != 0) {
            PRINT_ERR("[%s] get first_discard_clu failed at %u: %d\n",
                      __func__, new_tail_clu, ret);
            goto clear_dirty_eio;
        }
        if (first_discard_clu == EXFAT_EOF_CLUSTER ||
            first_discard_clu == EXFAT_FREE_CLUSTER) {
            PRINT_ERR("[%s] no discard tail at %u (next=%u)\n",
                      __func__, new_tail_clu, first_discard_clu);
            goto clear_dirty_eio;
        }

        ret = exfat_ent_set(sbi, new_tail_clu, EXFAT_EOF_CLUSTER);
        if (ret != 0) {
            PRINT_ERR("[%s] ent_set(EOF) at %u failed: %d\n",
                      __func__, new_tail_clu, ret);
            goto clear_dirty_eio;
        }

        ei->size          = new_size;
        ei->i_size_ondisk = (uint64_t)num_new_clu * (uint64_t)sbi->cluster_size;
        if (ei->valid_size > new_size) {
            ei->valid_size = new_size;
        }

        {
            exfat_chain discard_chain;
            discard_chain.dir   = first_discard_clu;
            discard_chain.size  = num_phys_clu - num_new_clu;
            discard_chain.flags = ALLOC_FAT_CHAIN;
            ret = exfat_free_cluster(sbi, &discard_chain);
            if (ret != 0) {
                PRINT_ERR("[%s] free discard chain (start=%u, n=%u) failed: %d\n",
                          __func__, first_discard_clu,
                          num_phys_clu - num_new_clu, ret);
                vd_ret = exfat_clear_volume_dirty(sbi);
                if (vd_ret != 0) {
                    PRINT_ERR("[%s] clear_volume_dirty after free fail: %d\n",
                              __func__, vd_ret);
                }
                return -EIO;
            }
        }
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
