/*
 * test_dentry — exfat_parse_boot_sector + exfat_find_root_dentry.
 *
 * parse_boot_sector tests use hand-crafted 512-byte buffers (no IO).
 * find_root_dentry tests use the synthetic image via mock_part_read.
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

int exfat_parse_boot_sector(exfat_sb_info *sbi,
                            const struct exfat_boot_sector *bs,
                            uint32_t logical_sector_size);
int exfat_find_root_dentry(const exfat_sb_info *sbi, uint8_t type,
                           struct exfat_dentry *out);

static void make_sbi_geometry(exfat_sb_info *sbi)
{
    /* Geometry that matches exfat_image_builder. parse_boot_sector hasn't
     * been run yet, so we fill manually for find_root_dentry tests. */
    memset(sbi, 0, sizeof(*sbi));
    sbi->sect_size_bits     = 9;
    sbi->sect_per_clus_bits = 0;
    sbi->blocksize          = 512;
    sbi->cluster_size       = 512;
    sbi->cluster_size_bits  = 9;
    sbi->dentries_per_clu   = 512u >> DENTRY_SIZE_BITS;
    sbi->num_clusters       = TIMG_NUM_CLUSTERS;
    sbi->fat_offset         = TIMG_FAT_OFFSET;
    sbi->fat_length         = 1;
    sbi->clu_offset         = TIMG_CLU_OFFSET;
    sbi->root_dir           = TIMG_ROOT_CLUSTER;
    sbi->num_fats           = 1;
    sbi->part_id            = 0;
}

/* ---- parse_boot_sector ----------------------------------------------- */

static void test_parse_bs_happy(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build(&img);

    const struct exfat_boot_sector *bs =
        (const struct exfat_boot_sector *)img.bytes;

    exfat_sb_info sbi;
    memset(&sbi, 0, sizeof(sbi));
    int r = exfat_parse_boot_sector(&sbi, bs, 512);
    assert_int_equal(r, 0);
    assert_int_equal(sbi.sect_size_bits, 9);
    assert_int_equal(sbi.sect_per_clus_bits, 0);
    assert_int_equal(sbi.num_fats, 1);
    assert_int_equal(sbi.fat_offset, TIMG_FAT_OFFSET);
    assert_int_equal(sbi.fat_length, 1);
    assert_int_equal(sbi.clu_offset, TIMG_CLU_OFFSET);
    assert_int_equal(sbi.num_clusters, TIMG_NUM_CLUSTERS + EXFAT_RESERVED_CLUSTERS);
    assert_int_equal(sbi.root_dir, TIMG_ROOT_CLUSTER);
    assert_int_equal(sbi.cluster_size, 512);
    assert_int_equal(sbi.blocksize, 512);
    /* used_clusters must be the untracked sentinel — Step 12 fills it later. */
    assert_int_equal(sbi.used_clusters, EXFAT_CLUSTERS_UNTRACKED);
}

static void test_parse_bs_bad_signature_einval(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build_bad_signature(&img);
    exfat_sb_info sbi; memset(&sbi, 0, sizeof(sbi));
    int r = exfat_parse_boot_sector(
        &sbi, (const struct exfat_boot_sector *)img.bytes, 512);
    assert_int_equal(r, -EINVAL);
}

static void test_parse_bs_bad_fs_name_einval(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build_bad_fs_name(&img);
    exfat_sb_info sbi; memset(&sbi, 0, sizeof(sbi));
    int r = exfat_parse_boot_sector(
        &sbi, (const struct exfat_boot_sector *)img.bytes, 512);
    assert_int_equal(r, -EINVAL);
}

static void test_parse_bs_logical_sector_mismatch_einval(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build(&img);
    exfat_sb_info sbi; memset(&sbi, 0, sizeof(sbi));
    /* sect_size_bits is 9 → expected 512; we lie and pass 1024. */
    int r = exfat_parse_boot_sector(
        &sbi, (const struct exfat_boot_sector *)img.bytes, 1024);
    assert_int_equal(r, -EINVAL);
}

static void test_parse_bs_null_args(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    struct exfat_boot_sector bs;
    assert_int_equal(exfat_parse_boot_sector(NULL, &bs, 512), -EINVAL);
    assert_int_equal(exfat_parse_boot_sector(&sbi, NULL, 512), -EINVAL);
    assert_int_equal(exfat_parse_boot_sector(&sbi, &bs, 0),    -EINVAL);
}

/* ---- find_root_dentry (uses mock_disk) ------------------------------- */

static int dentry_setup(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();
    return 0;
}

static int dentry_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

static void test_find_bitmap_dentry(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    struct exfat_dentry out;
    int r = exfat_find_root_dentry(&sbi, EXFAT_BITMAP, &out);
    assert_int_equal(r, 0);
    assert_int_equal(out.type, EXFAT_BITMAP);
    /* Field offsets: bitmap.start_clu at +20, .size at +24 */
    uint8_t *p = (uint8_t *)&out;
    uint32_t start_clu = (uint32_t)p[20] | ((uint32_t)p[21] << 8) |
                        ((uint32_t)p[22] << 16) | ((uint32_t)p[23] << 24);
    assert_int_equal(start_clu, TIMG_BITMAP_CLUSTER);
}

static void test_find_upcase_dentry(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    struct exfat_dentry out;
    int r = exfat_find_root_dentry(&sbi, EXFAT_UPCASE, &out);
    assert_int_equal(r, 0);
    assert_int_equal(out.type, EXFAT_UPCASE);
}

static void test_find_missing_dentry_enoent(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    struct exfat_dentry out;
    /* No FILE dentry exists in our root → terminator EXFAT_UNUSED encountered. */
    int r = exfat_find_root_dentry(&sbi, EXFAT_FILE, &out);
    assert_int_equal(r, -ENOENT);
}

static void test_find_dentry_io_error(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    /* Fail the very first read (root cluster). */
    mock_disk_set_read_fail_at(1);
    struct exfat_dentry out;
    int r = exfat_find_root_dentry(&sbi, EXFAT_BITMAP, &out);
    assert_int_equal(r, -EIO);
}

static void test_find_dentry_null_args(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    struct exfat_dentry out;
    assert_int_equal(exfat_find_root_dentry(NULL, EXFAT_BITMAP, &out), -EINVAL);
    assert_int_equal(exfat_find_root_dentry(&sbi, EXFAT_BITMAP, NULL), -EINVAL);
}

const struct CMUnitTest test_dentry_tests[] = {
    cmocka_unit_test(test_parse_bs_happy),
    cmocka_unit_test(test_parse_bs_bad_signature_einval),
    cmocka_unit_test(test_parse_bs_bad_fs_name_einval),
    cmocka_unit_test(test_parse_bs_logical_sector_mismatch_einval),
    cmocka_unit_test(test_parse_bs_null_args),
    cmocka_unit_test_setup_teardown(test_find_bitmap_dentry, dentry_setup, dentry_teardown),
    cmocka_unit_test_setup_teardown(test_find_upcase_dentry, dentry_setup, dentry_teardown),
    cmocka_unit_test_setup_teardown(test_find_missing_dentry_enoent, dentry_setup, dentry_teardown),
    cmocka_unit_test_setup_teardown(test_find_dentry_io_error, dentry_setup, dentry_teardown),
    cmocka_unit_test_setup_teardown(test_find_dentry_null_args, dentry_setup, dentry_teardown),
};

const size_t test_dentry_tests_count =
    sizeof(test_dentry_tests) / sizeof(test_dentry_tests[0]);
