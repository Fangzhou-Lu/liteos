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

extern int exfat_clear_bitmap(exfat_sb_info *sbi, uint32_t clu);
extern int exfat_free_cluster(exfat_sb_info *sbi, const exfat_chain *p_chain);
extern INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);

#define FC_BLOCKSIZE       512u
#define FC_PART_ID         88
#define FC_NUM_CLUSTERS    16u
#define FC_MAP_CLU         2u
#define FC_MAP_SECTORS     1u
#define FC_FAT_OFFSET      1u
#define FC_FAT_LENGTH      1u
#define FC_CLU_OFFSET      4u
#define FC_NUM_FATS        1u
#define FC_NSECTORS        8u
#define FC_IMG_LEN         (FC_NSECTORS * FC_BLOCKSIZE)

static exfat_sb_info *g_sbi;
static uint8_t       *g_vol_amap;
static uint8_t        g_image[FC_IMG_LEN];

static void fat_image_set(uint32_t clu, uint32_t next_clu)
{
    uint64_t off = (uint64_t)FC_FAT_OFFSET * FC_BLOCKSIZE + (uint64_t)clu * 4u;
    g_image[off + 0] = (uint8_t)(next_clu & 0xFFu);
    g_image[off + 1] = (uint8_t)((next_clu >> 8) & 0xFFu);
    g_image[off + 2] = (uint8_t)((next_clu >> 16) & 0xFFu);
    g_image[off + 3] = (uint8_t)((next_clu >> 24) & 0xFFu);
}

static int free_cluster_setup(void **state)
{
    (void)state;
    memset(g_image, 0u, sizeof(g_image));
    for (uint32_t i = 0; i < FC_NUM_CLUSTERS; i++) {
        fat_image_set(i, EXFAT_EOF_CLUSTER);
    }
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    g_sbi = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_vol_amap = (uint8_t *)calloc(1u, FC_MAP_SECTORS * FC_BLOCKSIZE);
    assert_non_null(g_vol_amap);
    memset(g_vol_amap, 0xFFu, FC_MAP_SECTORS * FC_BLOCKSIZE);

    g_sbi->blocksize          = FC_BLOCKSIZE;
    g_sbi->blocksize_bits     = 9u;
    g_sbi->sect_per_clus_bits = 0u;
    g_sbi->num_fats           = FC_NUM_FATS;
    g_sbi->num_clusters       = FC_NUM_CLUSTERS;
    g_sbi->fat_offset         = FC_FAT_OFFSET;
    g_sbi->fat_length         = FC_FAT_LENGTH;
    g_sbi->fat2_offset        = FC_FAT_OFFSET;
    g_sbi->clu_offset         = FC_CLU_OFFSET;
    g_sbi->map_clu            = FC_MAP_CLU;
    g_sbi->map_sectors        = FC_MAP_SECTORS;
    g_sbi->vol_amap           = g_vol_amap;
    g_sbi->part_id            = FC_PART_ID;
    g_sbi->used_clusters      = FC_NUM_CLUSTERS - EXFAT_RESERVED_CLUSTERS;
    return 0;
}

static int free_cluster_teardown(void **state)
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

static void test_clear_bitmap_happy(void **state)
{
    (void)state;
    int rc = exfat_clear_bitmap(g_sbi, 5u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)bit_is_set(5u), 0u);
    assert_int_equal((unsigned int)bit_is_set(4u), 1u);
    assert_int_equal((unsigned int)bit_is_set(6u), 1u);
    assert_int_equal((unsigned long)mock_disk_write_count(), 1ul);
}

static void test_clear_bitmap_clu_below_first(void **state)
{
    (void)state;
    int rc = exfat_clear_bitmap(g_sbi, EXFAT_FIRST_CLUSTER - 1u);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_clear_bitmap_clu_at_or_above_num(void **state)
{
    (void)state;
    int rc = exfat_clear_bitmap(g_sbi, FC_NUM_CLUSTERS);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_clear_bitmap_write_fail_returns_eio(void **state)
{
    (void)state;
    mock_disk_set_write_fail_at(1u);
    int rc = exfat_clear_bitmap(g_sbi, 7u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned int)bit_is_set(7u), 0u);
}

static void test_free_cluster_dir_eof_noop(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = EXFAT_EOF_CLUSTER, .size = 5u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
    assert_int_equal((unsigned int)g_sbi->used_clusters,
                     (unsigned int)(FC_NUM_CLUSTERS - EXFAT_RESERVED_CLUSTERS));
}

static void test_free_cluster_dir_free_noop(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = EXFAT_FREE_CLUSTER, .size = 5u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_free_cluster_dir_below_first_noop(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = 1u, .size = 5u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_free_cluster_zero_size_noop(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = 5u, .size = 0u, .flags = ALLOC_NO_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_free_cluster_dir_out_of_range_eio(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = FC_NUM_CLUSTERS + 5u, .size = 1u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
    assert_int_equal((unsigned int)g_sbi->used_clusters,
                     (unsigned int)(FC_NUM_CLUSTERS - EXFAT_RESERVED_CLUSTERS));
}

static void test_free_cluster_no_fat_chain_happy(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = 5u, .size = 3u, .flags = ALLOC_NO_FAT_CHAIN };
    uint32_t used_before = g_sbi->used_clusters;

    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)bit_is_set(5u), 0u);
    assert_int_equal((unsigned int)bit_is_set(6u), 0u);
    assert_int_equal((unsigned int)bit_is_set(7u), 0u);
    assert_int_equal((unsigned int)bit_is_set(4u), 1u);
    assert_int_equal((unsigned int)bit_is_set(8u), 1u);
    assert_int_equal((unsigned int)g_sbi->used_clusters, used_before - 3u);
    assert_int_equal((unsigned long)mock_disk_write_count(), 3ul);
    assert_int_equal((unsigned long)mock_disk_read_count(), 0ul);
}

static void test_free_cluster_fat_chain_happy(void **state)
{
    (void)state;
    fat_image_set(5u, 6u);
    fat_image_set(6u, 7u);
    fat_image_set(7u, EXFAT_EOF_CLUSTER);
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();
    uint32_t used_before = g_sbi->used_clusters;

    exfat_chain ch = { .dir = 5u, .size = 3u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)bit_is_set(5u), 0u);
    assert_int_equal((unsigned int)bit_is_set(6u), 0u);
    assert_int_equal((unsigned int)bit_is_set(7u), 0u);
    assert_int_equal((unsigned int)g_sbi->used_clusters, used_before - 3u);
    assert_int_equal((unsigned long)mock_disk_write_count(), 3ul);
}

static void test_free_cluster_fat_chain_mid_write_fail(void **state)
{
    (void)state;
    fat_image_set(5u, 6u);
    fat_image_set(6u, 7u);
    fat_image_set(7u, EXFAT_EOF_CLUSTER);
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();
    mock_disk_set_write_fail_at(2u);

    exfat_chain ch = { .dir = 5u, .size = 3u, .flags = ALLOC_FAT_CHAIN };
    uint32_t used_before = g_sbi->used_clusters;
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned int)bit_is_set(5u), 0u);
    /* clear_bitmap(6) cleared the bit in memory before its write failed
     * (Invariant exfat-clear-bitmap-bit-then-disk). */
    assert_int_equal((unsigned int)bit_is_set(6u), 0u);
    assert_int_equal((unsigned int)bit_is_set(7u), 1u);
    /* Only clu=5 incremented `freed` (clear_bitmap(6) returned before
     * freed++); used_clusters drops by 1 not 2. */
    assert_int_equal((unsigned int)g_sbi->used_clusters, used_before - 1u);
}

static void test_invariant_bitmap_lock_released_after_eio(void **state)
{
    (void)state;
    exfat_chain bad = { .dir = FC_NUM_CLUSTERS + 1u, .size = 1u, .flags = ALLOC_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &bad);
    assert_int_equal(rc, -EIO);
    rc = exfat_free_cluster(g_sbi, &bad);
    assert_int_equal(rc, -EIO);
}

static void test_invariant_no_fat_write_only_bitmap(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = 5u, .size = 2u, .flags = ALLOC_NO_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 2ul);
    assert_int_equal((unsigned long)mock_disk_read_count(), 0ul);
}

static void test_invariant_no_realloc(void **state)
{
    (void)state;
    uint8_t *amap_before = g_sbi->vol_amap;
    exfat_chain ch = { .dir = 5u, .size = 1u, .flags = ALLOC_NO_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, 0);
    assert_ptr_equal(g_sbi->vol_amap, amap_before);
}

static void test_invariant_used_clusters_saturate(void **state)
{
    (void)state;
    g_sbi->used_clusters = 1u;
    exfat_chain ch = { .dir = 5u, .size = 2u, .flags = ALLOC_NO_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 0u);
}

static void test_invariant_bit_then_disk_ordering(void **state)
{
    (void)state;
    g_vol_amap[0] &= (uint8_t)~(1u << 3u);
    mock_disk_set_write_fail_at(1u);
    int rc = exfat_clear_bitmap(g_sbi, 5u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned int)bit_is_set(5u), 0u);
}

static void test_invariant_single_sector_per_clear(void **state)
{
    (void)state;
    exfat_chain ch = { .dir = 5u, .size = 3u, .flags = ALLOC_NO_FAT_CHAIN };
    int rc = exfat_free_cluster(g_sbi, &ch);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 3ul);
}

static void test_invariant_no_discard(void **state)
{
    (void)state;
    int rc = exfat_clear_bitmap(g_sbi, 5u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 1ul);
}

const struct CMUnitTest test_free_cluster_tests[] = {
    cmocka_unit_test_setup_teardown(test_clear_bitmap_happy,                       free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_clear_bitmap_clu_below_first,             free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_clear_bitmap_clu_at_or_above_num,         free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_clear_bitmap_write_fail_returns_eio,      free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_free_cluster_dir_eof_noop,                free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_free_cluster_dir_free_noop,               free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_free_cluster_dir_below_first_noop,        free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_free_cluster_zero_size_noop,              free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_free_cluster_dir_out_of_range_eio,        free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_free_cluster_no_fat_chain_happy,          free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_free_cluster_fat_chain_happy,             free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_free_cluster_fat_chain_mid_write_fail,    free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_bitmap_lock_released_after_eio, free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_no_fat_write_only_bitmap,       free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_no_realloc,                     free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_used_clusters_saturate,         free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_bit_then_disk_ordering,         free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_single_sector_per_clear,        free_cluster_setup, free_cluster_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_no_discard,                     free_cluster_setup, free_cluster_teardown),
};

const size_t test_free_cluster_tests_count =
    sizeof(test_free_cluster_tests) / sizeof(test_free_cluster_tests[0]);
