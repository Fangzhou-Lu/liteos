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

/*
 * test_dentry_iter — exfat_get_dentry / exfat_get_dentry_set /
 * exfat_validate_dentry_set.
 *
 * Happy paths use the standard 6 KiB synthetic image (root cluster has
 * BITMAP at idx 0, UPCASE at idx 1, UNUSED terminator at idx 2).
 * Negative paths inject IO failures via mock_disk_set_read_fail_at() or
 * hand-mutate the image after exfat_test_image_build().
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <cmocka.h>

#include "../../../fs/exfat/include/exfat.h"
#include "../../../fs/exfat/include/exfat_raw.h"
#include "exfat_image_builder.h"
#include "mock_disk.h"

/* Forward declarations of the three functions under test. */
int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                     int entry_idx, struct exfat_dentry *out,
                     uint64_t *out_sector);
int exfat_get_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
                         int start_entry, struct exfat_dentry *set,
                         int max_entries, int *num_entries);
int exfat_validate_dentry_set(const struct exfat_dentry *set, int num_entries);

/* ---- geometry helper (matches exfat_image_builder layout) ---------------- */

static void make_sbi(exfat_sb_info *sbi)
{
    memset(sbi, 0, sizeof(*sbi));
    sbi->sect_size_bits     = 9;
    sbi->sect_per_clus_bits = 0;
    sbi->blocksize          = 512;
    sbi->cluster_size       = 512;
    sbi->cluster_size_bits  = 9;
    sbi->dentries_per_clu   = 512u >> DENTRY_SIZE_BITS;
    sbi->num_clusters       = TIMG_NUM_CLUSTERS + EXFAT_RESERVED_CLUSTERS;
    sbi->fat_offset         = TIMG_FAT_OFFSET;
    sbi->fat_length         = 1;
    sbi->clu_offset         = TIMG_CLU_OFFSET;
    sbi->root_dir           = TIMG_ROOT_CLUSTER;
    sbi->num_fats           = 1;
    sbi->part_id            = 0;
}

/* Root directory chain: contiguous (ALLOC_NO_FAT_CHAIN), starts at cluster 2. */
static void make_root_chain(exfat_chain *chain)
{
    chain->dir   = TIMG_ROOT_CLUSTER;
    chain->size  = 1;           /* 1 cluster */
    chain->flags = (uint8_t)ALLOC_NO_FAT_CHAIN;
}

/* ---- setup / teardown ---------------------------------------------------- */

static int iter_setup(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();
    return 0;
}

static int iter_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

/* ---- helpers for checksum computation (mirrors production ROR+add) ------- */

static uint16_t local_chksum16(const void *data, int len, uint16_t seed, int type)
{
    const uint8_t *c = (const uint8_t *)data;
    uint16_t csum = seed;
    for (int i = 0; i < len; i++) {
        /* CS_DIR_ENTRY (type==0): skip bytes 2 and 3 of the first entry
         * (the SetChecksum field itself). */
        if (type == CS_DIR_ENTRY && (i == 2 || i == 3)) {
            continue;
        }
        csum = (uint16_t)(((csum << 15) | (csum >> 1)) + (uint16_t)c[i]);
    }
    return csum;
}

static void put_u16_le(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
}

/* ============================================================
 * exfat_get_dentry tests
 * ============================================================ */

/* T01: Read dentry index 0 from root cluster → BITMAP entry. */
static void test_get_dentry_idx0_bitmap(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain   dir; make_root_chain(&dir);

    struct exfat_dentry out;
    memset(&out, 0xAA, sizeof(out));

    int r = exfat_get_dentry(&sbi, &dir, 0, &out, NULL);
    assert_int_equal(r, 0);
    assert_int_equal(out.type, EXFAT_BITMAP);
    /* bitmap.start_clu must equal TIMG_BITMAP_CLUSTER. */
    assert_int_equal(out.dentry.bitmap.start_clu, TIMG_BITMAP_CLUSTER);
}

/* T02: Read dentry index 1 from root cluster → UPCASE entry. */
static void test_get_dentry_idx1_upcase(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain   dir; make_root_chain(&dir);

    struct exfat_dentry out;
    uint64_t sector = 0xDEADBEEFDEADBEEFULL;

    int r = exfat_get_dentry(&sbi, &dir, 1, &out, &sector);
    assert_int_equal(r, 0);
    assert_int_equal(out.type, EXFAT_UPCASE);
    /* out_sector must be the root cluster sector (sector 9). */
    assert_int_equal(sector, (uint64_t)TIMG_CLU_OFFSET);
}

/* T03: NULL params → -EINVAL. */
static void test_get_dentry_null_params(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain   dir; make_root_chain(&dir);
    struct exfat_dentry out;

    assert_int_equal(exfat_get_dentry(NULL, &dir, 0, &out, NULL), -EINVAL);
    assert_int_equal(exfat_get_dentry(&sbi, NULL,  0, &out, NULL), -EINVAL);
    assert_int_equal(exfat_get_dentry(&sbi, &dir,  0, NULL, NULL), -EINVAL);
    assert_int_equal(exfat_get_dentry(&sbi, &dir, -1, &out, NULL), -EINVAL);
}

/* T04: entry_idx that maps beyond num_clusters (OOR cluster) → -EIO.
 * With cluster_size=512 and root starting at cluster 2, entry index 16
 * requires cluster 2+16=18, which >= num_clusters (16). */
static void test_get_dentry_oor_idx_eio(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain   dir; make_root_chain(&dir);
    struct exfat_dentry out;

    /* Each cluster holds dentries_per_clu = 16 dentries.
     * entry_idx = 16 maps to clu_offset = 1 → cur_clu = 2+1 = 3,
     * which IS within range. Use a larger index: 16*14 = 224 → clu_offset=14,
     * cur_clu = 2+14 = 16 >= num_clusters(16). */
    int r = exfat_get_dentry(&sbi, &dir, 224, &out, NULL);
    assert_int_equal(r, -EIO);
}

/* T05: IO failure injected on first read → -EIO. */
static void test_get_dentry_io_fail(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain   dir; make_root_chain(&dir);
    struct exfat_dentry out;

    mock_disk_set_read_fail_at(1);
    int r = exfat_get_dentry(&sbi, &dir, 0, &out, NULL);
    assert_int_equal(r, -EIO);
}

/* ============================================================
 * exfat_get_dentry_set tests
 * ============================================================ */

/* T06: NULL params → -EINVAL. */
static void test_get_dentry_set_null_params(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain   dir; make_root_chain(&dir);
    struct exfat_dentry set[3];
    int num = 0;

    assert_int_equal(
        exfat_get_dentry_set(NULL, &dir, 0, set, 3, &num), -EINVAL);
    assert_int_equal(
        exfat_get_dentry_set(&sbi, NULL,  0, set, 3, &num), -EINVAL);
    assert_int_equal(
        exfat_get_dentry_set(&sbi, &dir,  0, NULL, 3, &num), -EINVAL);
    assert_int_equal(
        exfat_get_dentry_set(&sbi, &dir,  0, set, 3, NULL), -EINVAL);
}

/* T07: max_entries out of range (0 or >EXFAT_DENTRY_SET_MAX) → -EINVAL. */
static void test_get_dentry_set_bad_max_entries(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain   dir; make_root_chain(&dir);
    struct exfat_dentry set[3];
    int num = 0;

    assert_int_equal(
        exfat_get_dentry_set(&sbi, &dir, 0, set, 0, &num), -EINVAL);
    assert_int_equal(
        exfat_get_dentry_set(&sbi, &dir, 0, set, EXFAT_DENTRY_SET_MAX + 1, &num),
        -EINVAL);
    assert_int_equal(
        exfat_get_dentry_set(&sbi, &dir, -1, set, 3, &num), -EINVAL);
}

/* T08: Primary dentry is not EXFAT_FILE (BITMAP at idx 0) → -EIO. */
static void test_get_dentry_set_non_file_primary_eio(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain   dir; make_root_chain(&dir);
    struct exfat_dentry set[3];
    int num = 0;

    /* Root idx 0 is BITMAP (0x81), not EXFAT_FILE (0x85). */
    int r = exfat_get_dentry_set(&sbi, &dir, 0, set, 3, &num);
    assert_int_equal(r, -EIO);
}

/* T09: max_entries too small for the set declared by num_ext.
 * Patch the image: set root dentry 0 to FILE with num_ext=2,
 * then call with max_entries=2 (total needed = 3). */
static void test_get_dentry_set_buf_too_small_eio(void **state)
{
    (void)state;
    /* Build fresh image, mutate, reload. */
    mock_disk_unload();
    exfat_test_image img;
    exfat_test_image_build(&img);

    /* Root cluster starts at sector TIMG_CLU_OFFSET = 9. */
    uint8_t *root = img.bytes + TIMG_CLU_OFFSET * TIMG_SECTOR_SIZE;
    /* Overwrite dentry 0: type=EXFAT_FILE, num_ext=2. */
    memset(root, 0, DENTRY_SIZE);
    root[0] = (uint8_t)EXFAT_FILE;
    root[1] = 2u;   /* num_ext = 2 → total = 3 entries needed */

    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();

    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain   dir; make_root_chain(&dir);
    struct exfat_dentry set[3];
    int num = 0;

    /* max_entries=2 < total=3 → -EIO. */
    int r = exfat_get_dentry_set(&sbi, &dir, 0, set, 2, &num);
    assert_int_equal(r, -EIO);
}

/* T10: IO failure during get_dentry_set → propagated as -EIO. */
static void test_get_dentry_set_io_fail(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain   dir; make_root_chain(&dir);
    struct exfat_dentry set[3];
    int num = 0;

    mock_disk_set_read_fail_at(1);
    int r = exfat_get_dentry_set(&sbi, &dir, 0, set, 3, &num);
    assert_int_equal(r, -EIO);
}

/* ============================================================
 * exfat_validate_dentry_set tests
 * ============================================================ */

/* T11: NULL set → -EINVAL. */
static void test_validate_set_null(void **state)
{
    (void)state;
    assert_int_equal(exfat_validate_dentry_set(NULL, 1), -EINVAL);
}

/* T12: num_entries out of range → -EINVAL. */
static void test_validate_set_bad_count(void **state)
{
    (void)state;
    struct exfat_dentry set[1];
    memset(set, 0, sizeof(set));
    set[0].type = (uint8_t)EXFAT_FILE;

    assert_int_equal(exfat_validate_dentry_set(set, 0),  -EINVAL);
    assert_int_equal(exfat_validate_dentry_set(set, EXFAT_DENTRY_SET_MAX + 1), -EINVAL);
}

/* T13: set[0].type != EXFAT_FILE → -EINVAL. */
static void test_validate_set_wrong_primary_type(void **state)
{
    (void)state;
    struct exfat_dentry set[1];
    memset(set, 0, sizeof(set));
    set[0].type = (uint8_t)EXFAT_BITMAP;   /* wrong type */

    assert_int_equal(exfat_validate_dentry_set(set, 1), -EINVAL);
}

/* T14: Correct checksum → 0.
 * Build a minimal 1-entry FILE dentry set and compute the expected
 * chksum16 using the same ROR+add algorithm (CS_DIR_ENTRY skips bytes 2-3). */
static void test_validate_set_correct_chksum(void **state)
{
    (void)state;
    struct exfat_dentry set[1];
    memset(set, 0, sizeof(set));
    set[0].type              = (uint8_t)EXFAT_FILE;
    set[0].dentry.file.num_ext    = 0;
    set[0].dentry.file.checksum   = 0;  /* placeholder; will be replaced */
    set[0].dentry.file.attr       = ATTR_ARCHIVE;

    /* Compute checksum over the raw bytes (32 bytes), skipping bytes 2-3. */
    uint16_t chksum = local_chksum16(&set[0], (int)DENTRY_SIZE, 0, CS_DIR_ENTRY);

    /* Write computed checksum into the set (little-endian). */
    put_u16_le((uint8_t *)&set[0] + offsetof(struct exfat_dentry, dentry.file.checksum),
               chksum);

    int r = exfat_validate_dentry_set(set, 1);
    assert_int_equal(r, 0);
}

/* T15: Corrupted checksum → -EIO. */
static void test_validate_set_bad_chksum(void **state)
{
    (void)state;
    struct exfat_dentry set[1];
    memset(set, 0, sizeof(set));
    set[0].type                 = (uint8_t)EXFAT_FILE;
    set[0].dentry.file.num_ext  = 0;
    /* Deliberately wrong checksum. */
    set[0].dentry.file.checksum = 0xDEAD;

    int r = exfat_validate_dentry_set(set, 1);
    assert_int_equal(r, -EIO);
}

/* ============================================================
 * Suite table
 * ============================================================ */

const struct CMUnitTest test_dentry_iter_tests[] = {
    /* exfat_get_dentry — 5 testpoints */
    cmocka_unit_test_setup_teardown(test_get_dentry_idx0_bitmap,    iter_setup, iter_teardown),
    cmocka_unit_test_setup_teardown(test_get_dentry_idx1_upcase,    iter_setup, iter_teardown),
    cmocka_unit_test_setup_teardown(test_get_dentry_null_params,    iter_setup, iter_teardown),
    cmocka_unit_test_setup_teardown(test_get_dentry_oor_idx_eio,    iter_setup, iter_teardown),
    cmocka_unit_test_setup_teardown(test_get_dentry_io_fail,        iter_setup, iter_teardown),
    /* exfat_get_dentry_set — 5 testpoints */
    cmocka_unit_test_setup_teardown(test_get_dentry_set_null_params,         iter_setup, iter_teardown),
    cmocka_unit_test_setup_teardown(test_get_dentry_set_bad_max_entries,     iter_setup, iter_teardown),
    cmocka_unit_test_setup_teardown(test_get_dentry_set_non_file_primary_eio,iter_setup, iter_teardown),
    cmocka_unit_test_setup_teardown(test_get_dentry_set_buf_too_small_eio,   iter_setup, iter_teardown),
    cmocka_unit_test_setup_teardown(test_get_dentry_set_io_fail,             iter_setup, iter_teardown),
    /* exfat_validate_dentry_set — 5 testpoints */
    cmocka_unit_test(test_validate_set_null),
    cmocka_unit_test(test_validate_set_bad_count),
    cmocka_unit_test(test_validate_set_wrong_primary_type),
    cmocka_unit_test(test_validate_set_correct_chksum),
    cmocka_unit_test(test_validate_set_bad_chksum),
};

const size_t test_dentry_iter_tests_count =
    sizeof(test_dentry_iter_tests) / sizeof(test_dentry_iter_tests[0]);
