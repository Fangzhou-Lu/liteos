/*
 * test_balloc — exfat_load_bitmap / exfat_free_bitmap / exfat_count_used_clusters.
 * All three exercised against the synthesized image via mock_disk.
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

int  exfat_load_bitmap(exfat_sb_info *sbi);
void exfat_free_bitmap(exfat_sb_info *sbi);
int  exfat_count_used_clusters(const exfat_sb_info *sbi, uint32_t *out);

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

static int balloc_setup(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();
    return 0;
}

static int balloc_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

static void test_load_bitmap_happy(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    int r = exfat_load_bitmap(&sbi);
    assert_int_equal(r, 0);
    assert_non_null(sbi.vol_amap);
    assert_int_equal(sbi.map_clu, TIMG_BITMAP_CLUSTER);
    assert_true(sbi.map_sectors >= 1);
    /* Bytes 0 of vol_amap must reflect the bitmap we baked: 0x07 (clusters 2,3,4 used). */
    assert_int_equal(sbi.vol_amap[0], 0x07);
    exfat_free_bitmap(&sbi);
}

static void test_load_bitmap_no_dentry_enoent(void **state)
{
    (void)state;
    /* Replace the live snapshot with a no-bitmap variant. */
    mock_disk_unload();
    exfat_test_image img;
    exfat_test_image_build_no_bitmap_dentry(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));

    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    int r = exfat_load_bitmap(&sbi);
    assert_int_equal(r, -ENOENT);
    assert_null(sbi.vol_amap);
    assert_int_equal(sbi.map_sectors, 0);
}

static void test_load_bitmap_too_small_eio(void **state)
{
    (void)state;
    mock_disk_unload();
    exfat_test_image img;
    exfat_test_image_build_bitmap_too_small(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));

    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    int r = exfat_load_bitmap(&sbi);
    assert_int_equal(r, -EIO);
    assert_null(sbi.vol_amap);
}

static void test_load_bitmap_io_failure(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    /* The find_root_dentry walk reads root cluster + maybe FAT;
     * load_bitmap then reads bitmap cluster. Inject failure on a
     * later read (bitmap cluster). 3 reads typical: root, fat (none for first cluster),
     * bitmap. Inject at #2 to hit the bitmap-cluster read. */
    mock_disk_set_read_fail_at(2);
    int r = exfat_load_bitmap(&sbi);
    assert_int_equal(r, -EIO);
    assert_null(sbi.vol_amap);
}

static void test_load_bitmap_double_load_einval(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    assert_int_equal(exfat_load_bitmap(&sbi), 0);
    /* second call: precondition vol_amap == NULL fails */
    int r = exfat_load_bitmap(&sbi);
    assert_int_equal(r, -EINVAL);
    exfat_free_bitmap(&sbi);
}

static void test_count_used_clusters_happy(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    assert_int_equal(exfat_load_bitmap(&sbi), 0);

    uint32_t used = 0xDEADBEEFu;
    int r = exfat_count_used_clusters(&sbi, &used);
    assert_int_equal(r, 0);
    /* baked bitmap byte 0 = 0x07 → 3 set bits inside the data-cluster window */
    assert_int_equal(used, 3);
    exfat_free_bitmap(&sbi);
}

static void test_count_used_clusters_einval_when_unloaded(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    uint32_t used;
    /* Without load_bitmap, vol_amap is NULL → -EINVAL */
    assert_int_equal(exfat_count_used_clusters(&sbi, &used), -EINVAL);
}

static void test_free_bitmap_idempotent(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi_geometry(&sbi);
    exfat_free_bitmap(&sbi);   /* on un-loaded sbi: no-op, no crash */
    assert_int_equal(exfat_load_bitmap(&sbi), 0);
    exfat_free_bitmap(&sbi);
    assert_null(sbi.vol_amap);
    assert_int_equal(sbi.map_sectors, 0);
    /* repeat */
    exfat_free_bitmap(&sbi);
    assert_null(sbi.vol_amap);
}

const struct CMUnitTest test_balloc_tests[] = {
    cmocka_unit_test_setup_teardown(test_load_bitmap_happy,             balloc_setup, balloc_teardown),
    cmocka_unit_test_setup_teardown(test_load_bitmap_no_dentry_enoent,  balloc_setup, balloc_teardown),
    cmocka_unit_test_setup_teardown(test_load_bitmap_too_small_eio,     balloc_setup, balloc_teardown),
    cmocka_unit_test_setup_teardown(test_load_bitmap_io_failure,        balloc_setup, balloc_teardown),
    cmocka_unit_test_setup_teardown(test_load_bitmap_double_load_einval,balloc_setup, balloc_teardown),
    cmocka_unit_test_setup_teardown(test_count_used_clusters_happy,     balloc_setup, balloc_teardown),
    cmocka_unit_test_setup_teardown(test_count_used_clusters_einval_when_unloaded, balloc_setup, balloc_teardown),
    cmocka_unit_test_setup_teardown(test_free_bitmap_idempotent,        balloc_setup, balloc_teardown),
};

const size_t test_balloc_tests_count =
    sizeof(test_balloc_tests) / sizeof(test_balloc_tests[0]);
