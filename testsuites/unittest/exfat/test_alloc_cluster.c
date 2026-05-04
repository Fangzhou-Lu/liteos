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

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <errno.h>
#include <cmocka.h>

#include "exfat.h"
#include "mock_disk.h"

extern int exfat_set_bitmap(exfat_sb_info *sbi, uint32_t clu);
extern int exfat_find_free_bitmap(const exfat_sb_info *sbi, uint32_t hint_clu, uint32_t *out_clu);
extern int exfat_alloc_cluster(exfat_sb_info *sbi, uint32_t num_alloc, exfat_chain *p_chain);
extern int exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu, uint32_t *next_clu);

#define AC_BLOCKSIZE       512u
#define AC_PART_ID         99
#define AC_NUM_CLUSTERS    16u
#define AC_MAP_CLU         2u
#define AC_MAP_SECTORS     1u
#define AC_FAT_OFFSET      1u
#define AC_FAT_LENGTH      1u
#define AC_CLU_OFFSET      4u
#define AC_NUM_FATS        1u
#define AC_NSECTORS        8u
#define AC_IMG_LEN         (AC_NSECTORS * AC_BLOCKSIZE)

static exfat_sb_info *g_sbi;
static uint8_t       *g_vol_amap;
static uint8_t        g_image[AC_IMG_LEN];

static int alloc_cluster_setup(void **state)
{
    (void)state;
    memset(g_image, 0u, sizeof(g_image));
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    g_sbi = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_vol_amap = (uint8_t *)calloc(1u, AC_MAP_SECTORS * AC_BLOCKSIZE);
    assert_non_null(g_vol_amap);

    g_sbi->blocksize          = AC_BLOCKSIZE;
    g_sbi->blocksize_bits     = 9u;
    g_sbi->sect_per_clus_bits = 0u;
    g_sbi->num_fats           = AC_NUM_FATS;
    g_sbi->num_clusters       = AC_NUM_CLUSTERS;
    g_sbi->fat_offset         = AC_FAT_OFFSET;
    g_sbi->fat_length         = AC_FAT_LENGTH;
    g_sbi->fat2_offset        = AC_FAT_OFFSET;
    g_sbi->clu_offset         = AC_CLU_OFFSET;
    g_sbi->map_clu            = AC_MAP_CLU;
    g_sbi->map_sectors        = AC_MAP_SECTORS;
    g_sbi->vol_amap           = g_vol_amap;
    g_sbi->part_id            = AC_PART_ID;
    g_sbi->used_clusters      = 0u;
    g_sbi->clu_srch_ptr       = EXFAT_FIRST_CLUSTER;
    return 0;
}

static int alloc_cluster_teardown(void **state)
{
    (void)state;
    free(g_sbi);
    g_sbi = NULL;
    free(g_vol_amap);
    g_vol_amap = NULL;
    mock_disk_unload();
    return 0;
}

static int bit_is_set(uint32_t clu)
{
    uint32_t ent = clu - EXFAT_RESERVED_CLUSTERS;
    return (g_vol_amap[ent / 8u] >> (ent & 7u)) & 1u;
}

static void test_set_bitmap_happy(void **state)
{
    (void)state;
    int rc = exfat_set_bitmap(g_sbi, 5u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)bit_is_set(5u), 1u);
    assert_int_equal((unsigned int)bit_is_set(4u), 0u);
    assert_int_equal((unsigned int)bit_is_set(6u), 0u);
    assert_int_equal((unsigned long)mock_disk_write_count(), 1ul);
}

static void test_set_bitmap_clu_below_first(void **state)
{
    (void)state;
    int rc = exfat_set_bitmap(g_sbi, EXFAT_FIRST_CLUSTER - 1u);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_set_bitmap_clu_at_or_above_num(void **state)
{
    (void)state;
    int rc = exfat_set_bitmap(g_sbi, AC_NUM_CLUSTERS);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_set_bitmap_write_fail_returns_eio(void **state)
{
    (void)state;
    mock_disk_set_write_fail_at(1u);
    int rc = exfat_set_bitmap(g_sbi, 7u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned int)bit_is_set(7u), 1u);
}

static void test_find_free_bitmap_hits_at_hint(void **state)
{
    (void)state;
    uint32_t out = 0;
    int rc = exfat_find_free_bitmap(g_sbi, 5u, &out);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)out, 5u);
}

static void test_find_free_bitmap_hits_after_some_allocations(void **state)
{
    (void)state;
    g_vol_amap[0] |= (uint8_t)((1u << 3) | (1u << 4) | (1u << 5));
    uint32_t out = 0;
    int rc = exfat_find_free_bitmap(g_sbi, 5u, &out);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)out, 8u);
}

static void test_find_free_bitmap_wraps_around(void **state)
{
    (void)state;
    g_vol_amap[1] |= (uint8_t)((1u << 4) | (1u << 5));
    uint32_t out = 0;
    int rc = exfat_find_free_bitmap(g_sbi, 14u, &out);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)out, 2u);
}

static void test_find_free_bitmap_full(void **state)
{
    (void)state;
    memset(g_vol_amap, 0xFFu, AC_MAP_SECTORS * AC_BLOCKSIZE);
    uint32_t out = 0;
    int rc = exfat_find_free_bitmap(g_sbi, EXFAT_FIRST_CLUSTER, &out);
    assert_int_equal(rc, -ENOSPC);
    assert_int_equal((unsigned int)out, EXFAT_EOF_CLUSTER);
}

static void test_alloc_cluster_bad_flags_einval(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_NO_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 1u, &ch);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_alloc_cluster_zero_num_einval(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 0u, &ch);
    assert_int_equal(rc, -EINVAL);
}

static void test_alloc_cluster_null_chain_einval(void **state)
{
    (void)state;
    int rc = exfat_alloc_cluster(g_sbi, 1u, NULL);
    assert_int_equal(rc, -EINVAL);
}

static void test_alloc_cluster_capacity_enospc(void **state)
{
    (void)state;
    g_sbi->used_clusters = 10u;
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 5u, &ch);
    assert_int_equal(rc, -ENOSPC);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_alloc_cluster_single_happy(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 1u, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)ch.dir, EXFAT_FIRST_CLUSTER);
    assert_int_equal((unsigned int)ch.size, 1u);
    assert_int_equal((unsigned int)bit_is_set(EXFAT_FIRST_CLUSTER), 1u);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 1u);
    assert_int_equal((unsigned int)g_sbi->clu_srch_ptr, EXFAT_FIRST_CLUSTER);
    uint32_t next = 0;
    int wrc = exfat_get_next_cluster(g_sbi, ch.dir, &next);
    assert_int_equal(wrc, 0);
    assert_int_equal((unsigned int)next, EXFAT_EOF_CLUSTER);
}

static void test_alloc_cluster_multi_happy(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 3u, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)ch.dir, EXFAT_FIRST_CLUSTER);
    assert_int_equal((unsigned int)ch.size, 3u);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 3u);
    assert_int_equal((unsigned int)bit_is_set(2u), 1u);
    assert_int_equal((unsigned int)bit_is_set(3u), 1u);
    assert_int_equal((unsigned int)bit_is_set(4u), 1u);
    uint32_t next = 0;
    assert_int_equal(exfat_get_next_cluster(g_sbi, 2u, &next), 0);
    assert_int_equal((unsigned int)next, 3u);
    assert_int_equal(exfat_get_next_cluster(g_sbi, 3u, &next), 0);
    assert_int_equal((unsigned int)next, 4u);
    assert_int_equal(exfat_get_next_cluster(g_sbi, 4u, &next), 0);
    assert_int_equal((unsigned int)next, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->clu_srch_ptr, 4u);
}

static void test_alloc_cluster_mid_set_bitmap_fail(void **state)
{
    (void)state;
    mock_disk_set_write_fail_at(3u);
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_FAT_CHAIN };
    uint32_t used_before = g_sbi->used_clusters;
    int rc = exfat_alloc_cluster(g_sbi, 2u, &ch);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned int)ch.dir, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)ch.size, 0u);
    assert_int_equal((unsigned int)g_sbi->used_clusters, used_before);
    assert_int_equal((unsigned int)g_sbi->clu_srch_ptr, EXFAT_FIRST_CLUSTER);
}

static void test_alloc_cluster_mid_enospc_rollback(void **state)
{
    (void)state;
    g_vol_amap[0] = (uint8_t)0xFEu;
    g_vol_amap[1] = (uint8_t)0xFFu;
    g_sbi->used_clusters = 12u;

    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 2u, &ch);
    assert_int_equal(rc, -ENOSPC);
    assert_int_equal((unsigned int)ch.dir, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)ch.size, 0u);
    assert_int_equal((unsigned int)bit_is_set(2u), 0u);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 12u);
}

static void test_invariant_bitmap_lock_released_after_error(void **state)
{
    (void)state;
    exfat_chain bad = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_NO_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 1u, &bad);
    assert_int_equal(rc, -EINVAL);
    bad.flags = ALLOC_FAT_CHAIN;
    rc = exfat_alloc_cluster(g_sbi, 1u, &bad);
    assert_int_equal(rc, 0);
}

static void test_invariant_no_fat_chain_rejected(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_NO_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 1u, &ch);
    assert_int_equal(rc, -EINVAL);
}

static void test_invariant_no_realloc(void **state)
{
    (void)state;
    uint8_t *amap_before = g_sbi->vol_amap;
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 2u, &ch);
    assert_int_equal(rc, 0);
    assert_ptr_equal(g_sbi->vol_amap, amap_before);
}

static void test_invariant_fat_eof_on_tail(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 4u, &ch);
    assert_int_equal(rc, 0);
    uint32_t cur = ch.dir;
    uint32_t next;
    for (uint32_t i = 0; i < 3u; i++) {
        assert_int_equal(exfat_get_next_cluster(g_sbi, cur, &next), 0);
        cur = next;
    }
    assert_int_equal(exfat_get_next_cluster(g_sbi, cur, &next), 0);
    assert_int_equal((unsigned int)next, EXFAT_EOF_CLUSTER);
}

static void test_invariant_find_free_no_io(void **state)
{
    (void)state;
    mock_disk_reset_counters();
    uint32_t out = 0;
    (void)exfat_find_free_bitmap(g_sbi, 5u, &out);
    assert_int_equal((unsigned long)mock_disk_read_count(),  0ul);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_invariant_set_bit_then_disk(void **state)
{
    (void)state;
    mock_disk_set_write_fail_at(1u);
    int rc = exfat_set_bitmap(g_sbi, 9u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned int)bit_is_set(9u), 1u);
}

static void test_invariant_set_single_sector(void **state)
{
    (void)state;
    int rc = exfat_set_bitmap(g_sbi, 5u);  assert_int_equal(rc, 0);
    rc     = exfat_set_bitmap(g_sbi, 9u);  assert_int_equal(rc, 0);
    rc     = exfat_set_bitmap(g_sbi, 13u); assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 3ul);
}

static void test_invariant_srch_ptr_monotone(void **state)
{
    (void)state;
    g_sbi->clu_srch_ptr = EXFAT_FIRST_CLUSTER;
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 3u, &ch);
    assert_int_equal(rc, 0);
    assert_int_not_equal((unsigned int)g_sbi->clu_srch_ptr, EXFAT_FIRST_CLUSTER);

    uint32_t srch_before = g_sbi->clu_srch_ptr;
    exfat_chain bad = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_NO_FAT_CHAIN };
    rc = exfat_alloc_cluster(g_sbi, 1u, &bad);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned int)g_sbi->clu_srch_ptr, srch_before);
}

static void test_invariant_rollback_clears_bitmap(void **state)
{
    (void)state;
    mock_disk_set_write_fail_at(3u);
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 0u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_alloc_cluster(g_sbi, 2u, &ch);
    assert_int_equal(rc, -EIO);
    for (uint32_t c = EXFAT_FIRST_CLUSTER; c < AC_NUM_CLUSTERS; c++) {
        assert_int_equal((unsigned int)bit_is_set(c), 0u);
    }
}

const struct CMUnitTest test_alloc_cluster_tests[] = {
    cmocka_unit_test_setup_teardown(test_set_bitmap_happy,                       alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_set_bitmap_clu_below_first,             alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_set_bitmap_clu_at_or_above_num,         alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_set_bitmap_write_fail_returns_eio,      alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_find_free_bitmap_hits_at_hint,          alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_find_free_bitmap_hits_after_some_allocations, alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_find_free_bitmap_wraps_around,          alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_find_free_bitmap_full,                  alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_cluster_bad_flags_einval,         alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_cluster_zero_num_einval,          alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_cluster_null_chain_einval,        alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_cluster_capacity_enospc,          alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_cluster_single_happy,             alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_cluster_multi_happy,              alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_cluster_mid_set_bitmap_fail,      alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_cluster_mid_enospc_rollback,      alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_bitmap_lock_released_after_error, alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_no_fat_chain_rejected,        alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_no_realloc,                   alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_fat_eof_on_tail,              alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_find_free_no_io,              alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_set_bit_then_disk,            alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_set_single_sector,            alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_srch_ptr_monotone,            alloc_cluster_setup, alloc_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_rollback_clears_bitmap,       alloc_cluster_setup, alloc_cluster_teardown),
};

const size_t test_alloc_cluster_tests_count =
    sizeof(test_alloc_cluster_tests) / sizeof(test_alloc_cluster_tests[0]);
