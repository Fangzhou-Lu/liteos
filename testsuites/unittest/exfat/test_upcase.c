/*
 * test_upcase — exfat_create_upcase_table / exfat_free_upcase_table.
 * Exercises the strict no-fallback policy and chksum mismatch path.
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

int  exfat_create_upcase_table(exfat_sb_info *sbi);
void exfat_free_upcase_table(exfat_sb_info *sbi);

static void make_sbi_geometry(exfat_sb_info *sbi)
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

static int upcase_setup(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();
    return 0;
}

static int upcase_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

static void test_create_upcase_happy(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    int r = exfat_create_upcase_table(&sbi);
    assert_int_equal(r, 0);
    assert_non_null(sbi.vol_utbl);
    assert_int_equal(sbi.vol_utbl_clu, TIMG_UPCASE_CLUSTER);
    assert_int_equal(sbi.vol_utbl_size, TIMG_UPCASE_SIZE);
    /* identity table: vol_utbl[5] should be 5 */
    assert_int_equal(sbi.vol_utbl[5], 5);
    exfat_free_upcase_table(&sbi);
    assert_null(sbi.vol_utbl);
}

static void test_create_upcase_chksum_mismatch_einval(void **state)
{
    (void)state;
    mock_disk_unload();
    exfat_test_image img;
    exfat_test_image_build_corrupt_upcase_chksum(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));

    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    int r = exfat_create_upcase_table(&sbi);
    assert_int_equal(r, -EINVAL);
    /* Strict no-fallback: vol_utbl must remain NULL. */
    assert_null(sbi.vol_utbl);
}

static void test_create_upcase_io_failure(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    /* Read 1 = root cluster (find_root_dentry returns upcase dentry).
     * Read 2 = upcase cluster — fail this. */
    mock_disk_set_read_fail_at(2);
    int r = exfat_create_upcase_table(&sbi);
    assert_int_equal(r, -EIO);
    assert_null(sbi.vol_utbl);
}

static void test_create_upcase_double_create_einval(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    assert_int_equal(exfat_create_upcase_table(&sbi), 0);
    /* Calling twice without free → vol_utbl != NULL precondition fails. */
    int r = exfat_create_upcase_table(&sbi);
    assert_int_equal(r, -EINVAL);
    exfat_free_upcase_table(&sbi);
}

static void test_free_upcase_idempotent(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    exfat_free_upcase_table(&sbi);     /* unloaded → no-op */
    assert_int_equal(exfat_create_upcase_table(&sbi), 0);
    exfat_free_upcase_table(&sbi);
    assert_null(sbi.vol_utbl);
    exfat_free_upcase_table(&sbi);     /* repeat */
    assert_null(sbi.vol_utbl);
}

const struct CMUnitTest test_upcase_tests[] = {
    cmocka_unit_test_setup_teardown(test_create_upcase_happy,                upcase_setup, upcase_teardown),
    cmocka_unit_test_setup_teardown(test_create_upcase_chksum_mismatch_einval,upcase_setup, upcase_teardown),
    cmocka_unit_test_setup_teardown(test_create_upcase_io_failure,           upcase_setup, upcase_teardown),
    cmocka_unit_test_setup_teardown(test_create_upcase_double_create_einval, upcase_setup, upcase_teardown),
    cmocka_unit_test_setup_teardown(test_free_upcase_idempotent,             upcase_setup, upcase_teardown),
};

const size_t test_upcase_tests_count =
    sizeof(test_upcase_tests) / sizeof(test_upcase_tests[0]);
