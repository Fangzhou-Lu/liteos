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

extern int exfat_truncate_shrink(exfat_sb_info *sbi, exfat_inode_info *ei,
                                 uint64_t new_size);
extern int exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                                  uint32_t *next_clu);

#define TS_BLOCKSIZE       512u
#define TS_CLUSTER_SIZE    512u
#define TS_PART_ID         123
#define TS_NUM_CLUSTERS    8u
#define TS_MAP_CLU         2u
#define TS_MAP_SECTORS     1u
#define TS_FAT_OFFSET      1u
#define TS_FAT_LENGTH      1u
#define TS_CLU_OFFSET      4u
#define TS_NUM_FATS        1u
#define TS_NSECTORS        16u
#define TS_IMG_LEN         (TS_NSECTORS * TS_BLOCKSIZE)
#define TS_VOL_FLAGS_OFF   106u

static exfat_sb_info     *g_sbi;
static exfat_inode_info  *g_ei;
static uint8_t           *g_vol_amap;
static uint8_t           *g_boot_buf;
static uint8_t            g_image[TS_IMG_LEN];

static void fat_image_set(uint32_t clu, uint32_t next_clu)
{
    uint64_t off = (uint64_t)TS_FAT_OFFSET * TS_BLOCKSIZE + (uint64_t)clu * 4u;
    g_image[off + 0] = (uint8_t)(next_clu & 0xFFu);
    g_image[off + 1] = (uint8_t)((next_clu >> 8) & 0xFFu);
    g_image[off + 2] = (uint8_t)((next_clu >> 16) & 0xFFu);
    g_image[off + 3] = (uint8_t)((next_clu >> 24) & 0xFFu);
}

static int truncate_shrink_setup(void **state)
{
    (void)state;
    memset(g_image, 0u, sizeof(g_image));
    for (uint32_t i = 0; i < TS_NUM_CLUSTERS; i++) {
        fat_image_set(i, EXFAT_EOF_CLUSTER);
    }
    g_image[TS_VOL_FLAGS_OFF + 0] = 0;
    g_image[TS_VOL_FLAGS_OFF + 1] = 0;
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    g_sbi = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_ei = (exfat_inode_info *)calloc(1u, sizeof(*g_ei));
    assert_non_null(g_ei);
    g_vol_amap = (uint8_t *)calloc(1u, TS_MAP_SECTORS * TS_BLOCKSIZE);
    assert_non_null(g_vol_amap);
    g_boot_buf = (uint8_t *)calloc(1u, TS_BLOCKSIZE);
    assert_non_null(g_boot_buf);

    g_sbi->blocksize          = TS_BLOCKSIZE;
    g_sbi->blocksize_bits     = 9u;
    g_sbi->cluster_size       = TS_CLUSTER_SIZE;
    g_sbi->cluster_size_bits  = 9u;
    g_sbi->sect_per_clus_bits = 0u;
    g_sbi->num_fats           = TS_NUM_FATS;
    g_sbi->num_clusters       = TS_NUM_CLUSTERS;
    g_sbi->fat_offset         = TS_FAT_OFFSET;
    g_sbi->fat_length         = TS_FAT_LENGTH;
    g_sbi->fat2_offset        = TS_FAT_OFFSET;
    g_sbi->clu_offset         = TS_CLU_OFFSET;
    g_sbi->map_clu            = TS_MAP_CLU;
    g_sbi->map_sectors        = TS_MAP_SECTORS;
    g_sbi->vol_amap           = g_vol_amap;
    g_sbi->boot_buf           = g_boot_buf;
    g_sbi->part_id            = TS_PART_ID;
    g_sbi->used_clusters      = 0u;
    g_sbi->clu_srch_ptr       = EXFAT_FIRST_CLUSTER;
    g_sbi->vol_flags          = 0u;
    g_sbi->vol_flags_persistent = 0u;
    g_sbi->s_maxbytes         = (uint64_t)(TS_NUM_CLUSTERS - EXFAT_RESERVED_CLUSTERS) *
                                (uint64_t)TS_CLUSTER_SIZE;

    g_ei->type          = TYPE_FILE;
    g_ei->flags         = ALLOC_FAT_CHAIN;
    g_ei->start_clu     = EXFAT_EOF_CLUSTER;
    g_ei->size          = 0u;
    g_ei->valid_size    = 0u;
    g_ei->i_size_ondisk = 0u;
    return 0;
}

static int truncate_shrink_teardown(void **state)
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

/* Build a real FAT chain of n clusters starting at `start`, mark the
 * matching bitmap bits, and update inode metadata to a logical_size that
 * implies that many physical clusters. */
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
    g_ei->i_size_ondisk = (uint64_t)n * (uint64_t)TS_CLUSTER_SIZE;
    g_sbi->used_clusters = n;
}

/* ---------- input validation ---------- */

static void test_truncate_shrink_null_sbi(void **state)
{
    (void)state;
    g_ei->size = 1024u;
    int rc = exfat_truncate_shrink(NULL, g_ei, 512u);
    assert_int_equal(rc, -EINVAL);
}

static void test_truncate_shrink_null_ei(void **state)
{
    (void)state;
    int rc = exfat_truncate_shrink(g_sbi, NULL, 512u);
    assert_int_equal(rc, -EINVAL);
}

static void test_truncate_shrink_new_size_equal(void **state)
{
    (void)state;
    g_ei->size = 1024u;
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 1024u);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_truncate_shrink_new_size_greater(void **state)
{
    (void)state;
    g_ei->size = 1024u;
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 2048u);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_truncate_shrink_no_fat_chain(void **state)
{
    (void)state;
    g_ei->size = 2048u;
    g_ei->flags = ALLOC_NO_FAT_CHAIN;
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 1024u);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

/* ---------- success paths ---------- */

static void test_truncate_shrink_within_last_cluster(void **state)
{
    (void)state;
    /* 1 cluster file (512B physical, 400B logical). Shrink to 200B —
     * still inside the same cluster, no chain mutation. */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 1u, 400u);
    uint64_t old_ondisk = g_ei->i_size_ondisk;

    int rc = exfat_truncate_shrink(g_sbi, g_ei, 200u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 200ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)old_ondisk);
    assert_int_equal((unsigned long)g_ei->valid_size, 200ul);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_FIRST_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 1u);
}

static void test_truncate_shrink_cross_cluster_boundary(void **state)
{
    (void)state;
    /* 3 cluster file (1500B logical). Shrink to 600B → 2 clusters. */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 3u, 1500u);

    int rc = exfat_truncate_shrink(g_sbi, g_ei, 600u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 600ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)(2u * TS_CLUSTER_SIZE));
    assert_int_equal((unsigned long)g_ei->valid_size, 600ul);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_FIRST_CLUSTER);

    /* New chain length = 2; start → +1 → EOF. */
    uint32_t cur = g_ei->start_clu;
    uint32_t next = 0u;
    int wrc = exfat_get_next_cluster(g_sbi, cur, &next);
    assert_int_equal(wrc, 0);
    assert_int_not_equal((unsigned int)next, EXFAT_EOF_CLUSTER);
    cur = next;
    wrc = exfat_get_next_cluster(g_sbi, cur, &next);
    assert_int_equal(wrc, 0);
    assert_int_equal((unsigned int)next, EXFAT_EOF_CLUSTER);

    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 2u);
}

static void test_truncate_shrink_to_zero(void **state)
{
    (void)state;
    /* 3 cluster file, shrink to 0 — entire chain freed. */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 3u, 1500u);

    int rc = exfat_truncate_shrink(g_sbi, g_ei, 0u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 0ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, 0ul);
    assert_int_equal((unsigned long)g_ei->valid_size, 0ul);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 0u);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_truncate_shrink_keep_one_drop_one(void **state)
{
    (void)state;
    /* 2 cluster file, shrink to fit in 1 cluster. */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);

    int rc = exfat_truncate_shrink(g_sbi, g_ei, 256u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 256ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)TS_CLUSTER_SIZE);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_FIRST_CLUSTER);

    uint32_t next = 0u;
    int wrc = exfat_get_next_cluster(g_sbi, g_ei->start_clu, &next);
    assert_int_equal(wrc, 0);
    assert_int_equal((unsigned int)next, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 1u);
}

/* ---------- IO failure paths ---------- */

static void test_truncate_shrink_vol_flags_set_eio(void **state)
{
    (void)state;
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);
    mock_disk_set_write_fail_at(1u);
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 256u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned long)g_ei->size, 1000ul);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_FIRST_CLUSTER);
}

static void test_truncate_shrink_walk_eio(void **state)
{
    (void)state;
    /* Need at least 1 walk hop. 3 clusters → shrink to 600 (num_new=2,
     * walk_hops=1). Inject read-fail at 1 — first FAT read fails. */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 3u, 1500u);
    mock_disk_set_read_fail_at(1u);
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 600u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned long)g_ei->size, 1500ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)(3u * TS_CLUSTER_SIZE));
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_FIRST_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 3u);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_truncate_shrink_ent_set_eio(void **state)
{
    (void)state;
    /* 2 clusters → 1 cluster. Sequence:
     *   write 1: vol_flags set (sector 0)
     *   (walk_hops=0, no IO)
     *   (get_next(start, &first_discard) read, no write)
     *   write 2: ent_set(start, EOF) FAT (sector 1)  ← inject fail
     */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);
    mock_disk_set_write_fail_at(2u);
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 256u);
    assert_int_equal(rc, -EIO);
    /* ei must not be mutated. */
    assert_int_equal((unsigned long)g_ei->size, 1000ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)(2u * TS_CLUSTER_SIZE));
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_FIRST_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 2u);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_truncate_shrink_free_to_zero_eio(void **state)
{
    (void)state;
    /* Shrink-to-zero of 3 clusters. Writes:
     *   1: vol_flags set
     *   2: bitmap clear clu=2
     *   3: bitmap clear clu=3
     *   4: bitmap clear clu=4
     *   5: vol_flags clear
     * Inject fail at 3 — second bitmap clear fails mid-loop. ei has been
     * pre-mutated to "empty" before free starts; -EIO returned (leak case 8). */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 3u, 1500u);
    mock_disk_set_write_fail_at(3u);
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 0u);
    assert_int_equal(rc, -EIO);
    /* ei is committed to "empty" state per spec Case 8. */
    assert_int_equal((unsigned long)g_ei->size, 0ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, 0ul);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_truncate_shrink_free_discard_eio(void **state)
{
    (void)state;
    /* 3 clusters → 1 cluster (drop 2). Writes:
     *   1: vol_flags set
     *   2: ent_set(start, EOF) FAT
     *   3: bitmap clear clu=3 (first discard)
     *   4: bitmap clear clu=4 (second discard)  ← inject fail
     *   5: vol_flags clear
     * Per spec Case 9: ei.size/i_size/valid all already updated; -EIO returned. */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 3u, 1500u);
    mock_disk_set_write_fail_at(4u);
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 256u);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned long)g_ei->size, 256ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)TS_CLUSTER_SIZE);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_FIRST_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

/* ---------- invariants ---------- */

static void test_invariant_monotone_size_on_failure(void **state)
{
    (void)state;
    g_ei->size = 500u;
    g_ei->flags = ALLOC_NO_FAT_CHAIN;
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 100u);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)g_ei->size, 500ul);
}

static void test_invariant_valid_size_clamped_when_larger(void **state)
{
    (void)state;
    /* valid_size > new_size pre — must be clamped to new_size post. */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);
    g_ei->valid_size = 900u;
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 256u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->valid_size, 256ul);
    assert_int_equal((unsigned long)g_ei->size, 256ul);
}

static void test_invariant_valid_size_preserved_when_smaller(void **state)
{
    (void)state;
    /* valid_size <= new_size pre — must be unchanged post. */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);
    g_ei->valid_size = 100u;
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 400u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->valid_size, 100ul);
    assert_int_equal((unsigned long)g_ei->size, 400ul);
}

static void test_invariant_i_size_ondisk_aligned(void **state)
{
    (void)state;
    /* 3 clusters → shrink to 700 (num_new=2). i_size_ondisk must be
     * 1024 (multiple of cluster_size, >= new_size). */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 3u, 1500u);
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 700u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, 1024ul);
    assert_int_equal(0, (int)(g_ei->i_size_ondisk % TS_CLUSTER_SIZE));
}

static void test_invariant_fat_chain_only(void **state)
{
    (void)state;
    g_ei->size = 1024u;
    g_ei->flags = ALLOC_NO_FAT_CHAIN;
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 256u);
    assert_int_equal(rc, -EINVAL);
}

static void test_invariant_vol_flags_bracketed_success(void **state)
{
    (void)state;
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 256u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
}

static void test_invariant_start_clu_on_empty(void **state)
{
    (void)state;
    /* Shrink-to-zero must reset start_clu = EOF (else "use-after-free" view). */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 0u);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_EOF_CLUSTER);
}

static void test_invariant_fat_then_bitmap_order(void **state)
{
    (void)state;
    /* If ent_set runs BEFORE the discard chain is freed, then a write-fail
     * on the FIRST bitmap clear (write 3) leaves: ent_set committed
     * (FAT chain truncated at new tail) but no bitmap bits cleared yet —
     * used_clusters reflects 0 frees. Spec Case 9. */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 3u, 1500u);
    uint32_t used_before = g_sbi->used_clusters;
    mock_disk_set_write_fail_at(3u);
    int rc = exfat_truncate_shrink(g_sbi, g_ei, 256u);
    assert_int_equal(rc, -EIO);
    /* used_clusters did NOT increment (only decrements possible).
     * No bitmap clears succeeded → used_clusters unchanged from 3. */
    assert_int_equal((unsigned int)g_sbi->used_clusters, used_before);
    /* But size has been committed (Case 9 semantics). */
    assert_int_equal((unsigned long)g_ei->size, 256ul);
}

static void test_invariant_chain_shorter_than_claimed(void **state)
{
    (void)state;
    /* Build a 1-cluster real chain (FAT[2] = EOF) but claim num_phys=4
     * via i_size_ondisk. Shrink to 600 bytes wants num_new=2 → walk_hops=1.
     * walk_to_new_tail steps once: get_next(2)=EOF → premature EOF mid-walk
     * is rejected with -EIO ("chain shorter than num_phys claims"). */
    fat_image_set(EXFAT_FIRST_CLUSTER, EXFAT_EOF_CLUSTER);
    uint32_t ent = EXFAT_FIRST_CLUSTER - EXFAT_RESERVED_CLUSTERS;
    g_vol_amap[ent / 8u] |= (uint8_t)(1u << (ent & 7u));
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();
    g_ei->start_clu      = EXFAT_FIRST_CLUSTER;
    g_ei->size           = 2000u;
    g_ei->valid_size     = 2000u;
    g_ei->i_size_ondisk  = 4u * TS_CLUSTER_SIZE;
    g_sbi->used_clusters = 1u;

    int rc = exfat_truncate_shrink(g_sbi, g_ei, 600u);
    assert_int_equal(rc, -EIO);
    /* ei must not be mutated. */
    assert_int_equal((unsigned long)g_ei->size, 2000ul);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_FIRST_CLUSTER);
}

const struct CMUnitTest test_truncate_shrink_tests[] = {
    cmocka_unit_test_setup_teardown(test_truncate_shrink_null_sbi,              truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_null_ei,               truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_new_size_equal,        truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_new_size_greater,      truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_no_fat_chain,          truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_within_last_cluster,   truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_cross_cluster_boundary, truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_to_zero,               truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_keep_one_drop_one,     truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_vol_flags_set_eio,     truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_walk_eio,              truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_ent_set_eio,           truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_free_to_zero_eio,      truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_shrink_free_discard_eio,      truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_monotone_size_on_failure,    truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_valid_size_clamped_when_larger,  truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_valid_size_preserved_when_smaller, truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_i_size_ondisk_aligned,       truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_fat_chain_only,              truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_vol_flags_bracketed_success, truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_start_clu_on_empty,          truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_fat_then_bitmap_order,       truncate_shrink_setup, truncate_shrink_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_chain_shorter_than_claimed, truncate_shrink_setup, truncate_shrink_teardown),
};

const size_t test_truncate_shrink_tests_count =
    sizeof(test_truncate_shrink_tests) / sizeof(test_truncate_shrink_tests[0]);
