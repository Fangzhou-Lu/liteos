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

extern int exfat_truncate_extend(exfat_sb_info *sbi, exfat_inode_info *ei,
                                 uint64_t new_size);
extern int exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                                  uint32_t *next_clu);
extern INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);

#define TX_BLOCKSIZE       512u
#define TX_CLUSTER_SIZE    512u
#define TX_PART_ID         123
#define TX_NUM_CLUSTERS    8u
#define TX_MAP_CLU         2u
#define TX_MAP_SECTORS     1u
#define TX_FAT_OFFSET      1u
#define TX_FAT_LENGTH      1u
#define TX_CLU_OFFSET      4u
#define TX_NUM_FATS        1u
#define TX_NSECTORS        16u
#define TX_IMG_LEN         (TX_NSECTORS * TX_BLOCKSIZE)
#define TX_VOL_FLAGS_OFF   106u

static exfat_sb_info     *g_sbi;
static exfat_inode_info  *g_ei;
static uint8_t           *g_vol_amap;
static uint8_t           *g_boot_buf;
static uint8_t            g_image[TX_IMG_LEN];

static void fat_image_set(uint32_t clu, uint32_t next_clu)
{
    uint64_t off = (uint64_t)TX_FAT_OFFSET * TX_BLOCKSIZE + (uint64_t)clu * 4u;
    g_image[off + 0] = (uint8_t)(next_clu & 0xFFu);
    g_image[off + 1] = (uint8_t)((next_clu >> 8) & 0xFFu);
    g_image[off + 2] = (uint8_t)((next_clu >> 16) & 0xFFu);
    g_image[off + 3] = (uint8_t)((next_clu >> 24) & 0xFFu);
}

static int truncate_extend_setup(void **state)
{
    (void)state;
    memset(g_image, 0u, sizeof(g_image));
    for (uint32_t i = 0; i < TX_NUM_CLUSTERS; i++) {
        fat_image_set(i, EXFAT_EOF_CLUSTER);
    }
    g_image[TX_VOL_FLAGS_OFF + 0] = 0;
    g_image[TX_VOL_FLAGS_OFF + 1] = 0;
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    g_sbi = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_ei = (exfat_inode_info *)calloc(1u, sizeof(*g_ei));
    assert_non_null(g_ei);
    g_vol_amap = (uint8_t *)calloc(1u, TX_MAP_SECTORS * TX_BLOCKSIZE);
    assert_non_null(g_vol_amap);
    g_boot_buf = (uint8_t *)calloc(1u, TX_BLOCKSIZE);
    assert_non_null(g_boot_buf);

    g_sbi->blocksize          = TX_BLOCKSIZE;
    g_sbi->blocksize_bits     = 9u;
    g_sbi->cluster_size       = TX_CLUSTER_SIZE;
    g_sbi->cluster_size_bits  = 9u;
    g_sbi->sect_per_clus_bits = 0u;
    g_sbi->num_fats           = TX_NUM_FATS;
    g_sbi->num_clusters       = TX_NUM_CLUSTERS;
    g_sbi->fat_offset         = TX_FAT_OFFSET;
    g_sbi->fat_length         = TX_FAT_LENGTH;
    g_sbi->fat2_offset        = TX_FAT_OFFSET;
    g_sbi->clu_offset         = TX_CLU_OFFSET;
    g_sbi->map_clu            = TX_MAP_CLU;
    g_sbi->map_sectors        = TX_MAP_SECTORS;
    g_sbi->vol_amap           = g_vol_amap;
    g_sbi->boot_buf           = g_boot_buf;
    g_sbi->part_id            = TX_PART_ID;
    g_sbi->used_clusters      = 0u;
    g_sbi->clu_srch_ptr       = EXFAT_FIRST_CLUSTER;
    g_sbi->vol_flags          = 0u;
    g_sbi->vol_flags_persistent = 0u;
    g_sbi->s_maxbytes         = (uint64_t)(TX_NUM_CLUSTERS - EXFAT_RESERVED_CLUSTERS) *
                                (uint64_t)TX_CLUSTER_SIZE;

    g_ei->type          = TYPE_FILE;
    g_ei->flags         = ALLOC_FAT_CHAIN;
    g_ei->start_clu     = EXFAT_EOF_CLUSTER;
    g_ei->size          = 0u;
    g_ei->valid_size    = 0u;
    g_ei->i_size_ondisk = 0u;
    return 0;
}

static int truncate_extend_teardown(void **state)
{
    (void)state;
    free(g_sbi);
    g_sbi = NULL;
    free(g_ei);
    g_ei = NULL;
    free(g_vol_amap);
    g_vol_amap = NULL;
    free(g_boot_buf);
    g_boot_buf = NULL;
    mock_disk_unload();
    return 0;
}

static void preload_existing_chain(uint32_t start, uint32_t n, uint64_t logical_size)
{
    for (uint32_t i = 0; i < n; i++) {
        uint32_t cur  = start + i;
        uint32_t next = (i + 1u < n) ? (start + i + 1u) : EXFAT_EOF_CLUSTER;
        fat_image_set(cur, next);
        uint32_t ent = cur - EXFAT_RESERVED_CLUSTERS;
        g_vol_amap[ent / 8u] |= (uint8_t)(1u << (ent & 7u));
    }
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();
    g_ei->start_clu     = start;
    g_ei->size          = logical_size;
    g_ei->valid_size    = logical_size;
    g_ei->i_size_ondisk = (uint64_t)n * (uint64_t)TX_CLUSTER_SIZE;
    g_sbi->used_clusters = n;
}

static void test_truncate_extend_null_sbi(void **state)
{
    (void)state;
    int rc = exfat_truncate_extend(NULL, g_ei, 1024u);
    assert_int_equal(rc, -EINVAL);
}

static void test_truncate_extend_null_ei(void **state)
{
    (void)state;
    int rc = exfat_truncate_extend(g_sbi, NULL, 1024u);
    assert_int_equal(rc, -EINVAL);
}

static void test_truncate_extend_new_size_not_greater(void **state)
{
    (void)state;
    g_ei->size = 1024u;
    int rc = exfat_truncate_extend(g_sbi, g_ei, 1024u);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_truncate_extend_no_fat_chain(void **state)
{
    (void)state;
    g_ei->flags = ALLOC_NO_FAT_CHAIN;
    int rc = exfat_truncate_extend(g_sbi, g_ei, 1024u);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_truncate_extend_too_big(void **state)
{
    (void)state;
    int rc = exfat_truncate_extend(g_sbi, g_ei, g_sbi->s_maxbytes + 1u);
    assert_int_equal(rc, -EFBIG);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_truncate_extend_within_existing_alloc(void **state)
{
    (void)state;
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 1u, 200u);
    uint32_t old_start = g_ei->start_clu;
    uint64_t old_ondisk = g_ei->i_size_ondisk;
    uint64_t old_valid  = g_ei->valid_size;

    int rc = exfat_truncate_extend(g_sbi, g_ei, 400u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 400ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)old_ondisk);
    assert_int_equal((unsigned long)g_ei->valid_size, (unsigned long)old_valid);
    assert_int_equal((unsigned int)g_ei->start_clu, (unsigned int)old_start);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_truncate_extend_empty_file_alloc(void **state)
{
    (void)state;
    int rc = exfat_truncate_extend(g_sbi, g_ei, TX_CLUSTER_SIZE + 100u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, (unsigned long)(TX_CLUSTER_SIZE + 100u));
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)(2u * TX_CLUSTER_SIZE));
    assert_int_equal((unsigned long)g_ei->valid_size, 0ul);
    assert_int_not_equal((unsigned int)g_ei->start_clu, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 2u);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
    uint32_t next = 0;
    int wrc = exfat_get_next_cluster(g_sbi, g_ei->start_clu, &next);
    assert_int_equal(wrc, 0);
    assert_int_not_equal((unsigned int)next, EXFAT_EOF_CLUSTER);
    wrc = exfat_get_next_cluster(g_sbi, next, &next);
    assert_int_equal(wrc, 0);
    assert_int_equal((unsigned int)next, EXFAT_EOF_CLUSTER);
}

static void test_truncate_extend_link_to_existing_tail(void **state)
{
    (void)state;
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);

    int rc = exfat_truncate_extend(g_sbi, g_ei, 1500u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 1500ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)(3u * TX_CLUSTER_SIZE));
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_FIRST_CLUSTER);
    uint32_t cur = g_ei->start_clu;
    uint32_t next;
    for (uint32_t i = 0; i < 2u; i++) {
        int wrc = exfat_get_next_cluster(g_sbi, cur, &next);
        assert_int_equal(wrc, 0);
        assert_int_not_equal((unsigned int)next, EXFAT_EOF_CLUSTER);
        cur = next;
    }
    int wrc = exfat_get_next_cluster(g_sbi, cur, &next);
    assert_int_equal(wrc, 0);
    assert_int_equal((unsigned int)next, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 3u);
}

static void test_truncate_extend_enospc(void **state)
{
    (void)state;
    g_sbi->used_clusters = TX_NUM_CLUSTERS - EXFAT_RESERVED_CLUSTERS;
    int rc = exfat_truncate_extend(g_sbi, g_ei, 100u);
    assert_int_equal(rc, -ENOSPC);
    assert_int_equal((unsigned long)g_ei->size, 0ul);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_truncate_extend_vol_flags_set_eio(void **state)
{
    (void)state;
    mock_disk_set_write_fail_at(1u);
    int rc = exfat_truncate_extend(g_sbi, g_ei, 100u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned long)g_ei->size, 0ul);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_EOF_CLUSTER);
}

static void test_truncate_extend_alloc_eio(void **state)
{
    (void)state;
    /* Sequence (empty file, alloc 1 cluster):
     *   write 1: vol_flags set     (sector 0)
     *   write 2: bitmap set        (sector 4)
     *   write 3: ent_set new=EOF   (sector 1)
     *   write 4: vol_flags clear   (sector 0)
     * Inject failure on write 3 — alloc_cluster's ent_set fails internally
     * and does inline rollback. */
    mock_disk_set_write_fail_at(3u);
    int rc = exfat_truncate_extend(g_sbi, g_ei, 100u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned long)g_ei->size, 0ul);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 0u);
}

static void test_truncate_extend_link_eio(void **state)
{
    (void)state;
    /* Existing chain 2 (1 cluster, size 256). Extend to 1.5 clusters:
     *   write 1: vol_flags set
     *   (walk skipped — only 1 phys cluster)
     *   write 2: alloc set_bitmap(c=3)
     *   write 3: alloc ent_set(3, EOF) FAT
     *   write 4: link ent_set(2, 3) FAT  ← fail here
     */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 1u, 256u);
    mock_disk_set_write_fail_at(4u);
    int rc = exfat_truncate_extend(g_sbi, g_ei, TX_CLUSTER_SIZE + 100u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned long)g_ei->size, 256ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)TX_CLUSTER_SIZE);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_FIRST_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_truncate_extend_walk_eio(void **state)
{
    (void)state;
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 3u, 1500u);
    mock_disk_set_read_fail_at(1u);

    int rc = exfat_truncate_extend(g_sbi, g_ei, 2000u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned long)g_ei->size, 1500ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)(3u * TX_CLUSTER_SIZE));
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_invariant_monotone_size_on_failure(void **state)
{
    (void)state;
    g_ei->size = 500u;
    g_ei->flags = ALLOC_NO_FAT_CHAIN;
    int rc = exfat_truncate_extend(g_sbi, g_ei, 1000u);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)g_ei->size, 500ul);
}

static void test_invariant_valid_size_preserved(void **state)
{
    (void)state;
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 1u, 256u);
    g_ei->valid_size = 200u;
    int rc = exfat_truncate_extend(g_sbi, g_ei, 400u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->valid_size, 200ul);
    assert_int_equal((unsigned long)g_ei->size, 400ul);
}

static void test_invariant_i_size_ondisk_aligned(void **state)
{
    (void)state;
    int rc = exfat_truncate_extend(g_sbi, g_ei, 700u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, 1024ul);
    assert_int_equal(0, (int)(g_ei->i_size_ondisk % TX_CLUSTER_SIZE));
}

static void test_invariant_fat_chain_only(void **state)
{
    (void)state;
    g_ei->flags = ALLOC_NO_FAT_CHAIN;
    int rc = exfat_truncate_extend(g_sbi, g_ei, 1024u);
    assert_int_equal(rc, -EINVAL);
}

static void test_invariant_vol_flags_bracketed_success(void **state)
{
    (void)state;
    int rc = exfat_truncate_extend(g_sbi, g_ei, 100u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_invariant_vol_flags_bracketed_enospc(void **state)
{
    (void)state;
    g_sbi->used_clusters = TX_NUM_CLUSTERS - EXFAT_RESERVED_CLUSTERS;
    int rc = exfat_truncate_extend(g_sbi, g_ei, 100u);
    assert_int_equal(rc, -ENOSPC);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_invariant_no_physical_zerofill(void **state)
{
    (void)state;
    /* Stamp data-cluster 3's sector with a sentinel byte; if the helper
     * zero-filled, the byte would be wiped. */
    uint64_t data_sector_off = (uint64_t)(TX_CLU_OFFSET + 1) * TX_BLOCKSIZE;
    g_image[data_sector_off + 0] = 0xAAu;
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    int rc = exfat_truncate_extend(g_sbi, g_ei, 2u * TX_CLUSTER_SIZE);
    assert_int_equal(rc, 0);
    uint8_t buf[TX_BLOCKSIZE];
    INT32 prc = los_part_read((INT32)TX_PART_ID, buf,
                              (UINT64)(TX_CLU_OFFSET + 1u), 1u, 1);
    assert_int_equal(prc, 0);
    assert_int_equal((unsigned int)buf[0], 0xAAu);
}

static void test_invariant_rollback_on_link_failure(void **state)
{
    (void)state;
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 1u, 256u);
    uint32_t used_before = g_sbi->used_clusters;
    mock_disk_set_write_fail_at(4u);
    int rc = exfat_truncate_extend(g_sbi, g_ei, TX_CLUSTER_SIZE + 100u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned int)g_sbi->used_clusters, used_before);
}

const struct CMUnitTest test_truncate_extend_tests[] = {
    cmocka_unit_test_setup_teardown(test_truncate_extend_null_sbi,             truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_null_ei,              truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_new_size_not_greater, truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_no_fat_chain,         truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_too_big,              truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_within_existing_alloc, truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_empty_file_alloc,     truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_link_to_existing_tail, truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_enospc,               truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_vol_flags_set_eio,    truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_alloc_eio,            truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_link_eio,             truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_extend_walk_eio,             truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_monotone_size_on_failure,   truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_valid_size_preserved,       truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_i_size_ondisk_aligned,      truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_fat_chain_only,             truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_vol_flags_bracketed_success, truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_vol_flags_bracketed_enospc, truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_no_physical_zerofill,       truncate_extend_setup, truncate_extend_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_rollback_on_link_failure,   truncate_extend_setup, truncate_extend_teardown),
};

const size_t test_truncate_extend_tests_count =
    sizeof(test_truncate_extend_tests) / sizeof(test_truncate_extend_tests[0]);
