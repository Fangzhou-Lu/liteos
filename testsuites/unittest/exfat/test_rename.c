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
 * test_rename.c — host cmocka suite for VfsExfatRename
 * (spec: spec/exfat/inode/exfat_rename.spec)
 *
 * The harness shares the synthetic-image pattern of test_unlink.c and
 * test_rmdir.c: a 32-sector image with a parent directory cluster, an
 * optional second parent for cross-dir scenarios, and a target file slot
 * pre-populated by install_dentry_set_for_src(). Each test resets the
 * image via setup/teardown.
 *
 * Test coverage matrix:
 *   Case 6 validation:  NULL parent/src/dstName, cross-fs, deleted src,
 *                       negative entry, empty dstName, wrong parent type.
 *   Case 7 ENAMETOOLONG.
 *   Case 1 same-parent rename (no overwrite).
 *   Case 6/2 cross-parent move tested via cross_parent_move (we use a
 *           single sbi but two distinct parent inodes).
 *
 * Layer-B note: Case 3/4 (overwrite of existing dst) and Case 9 (non-empty
 * dir overwrite) require populating dst dentries in the harness and walking
 * the rename's resolve scan; this host suite covers the simpler "no dst"
 * paths plus validation refusals. Overwrite-with-cluster-free is exercised
 * by QEMU LTP smoke (Wave B).
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

extern int VfsExfatRename(struct Vnode *src, struct Vnode *dstParent,
                          const char *srcName, const char *dstName);
extern uint16_t exfat_calc_chksum16(const void *data, int len, uint16_t chksum,
                                    int type);

#define TRN_BLOCKSIZE        512u
#define TRN_CLUSTER_SIZE     512u
#define TRN_PART_ID          125
#define TRN_NUM_CLUSTERS     (16u + EXFAT_RESERVED_CLUSTERS)
#define TRN_MAP_CLU          2u
#define TRN_MAP_SECTORS      1u
#define TRN_FAT_OFFSET       1u
#define TRN_FAT_LENGTH       1u
#define TRN_CLU_OFFSET       4u
#define TRN_NUM_FATS         1u
#define TRN_NSECTORS         32u
#define TRN_IMG_LEN          (TRN_NSECTORS * TRN_BLOCKSIZE)

#define TRN_PARENT_DIR_CLU   EXFAT_FIRST_CLUSTER
#define TRN_SRC_ENTRY        0
#define TRN_DENTRIES_PER_CLU (TRN_CLUSTER_SIZE / DENTRY_SIZE)

static exfat_sb_info     *g_sbi;
static exfat_inode_info  *g_src_parent_ei;
static exfat_inode_info  *g_src_ei;
static struct Vnode      *g_src_parent_vp;
static struct Vnode      *g_src_vp;
static struct Vnode      *g_dst_parent_vp;
static struct Mount      *g_mount;
static uint8_t           *g_vol_amap;
static uint8_t           *g_boot_buf;
static uint8_t            g_image[TRN_IMG_LEN];

static void fat_image_set(uint32_t clu, uint32_t next_clu)
{
    uint64_t off = (uint64_t)TRN_FAT_OFFSET * TRN_BLOCKSIZE + (uint64_t)clu * 4u;
    g_image[off + 0] = (uint8_t)(next_clu & 0xFFu);
    g_image[off + 1] = (uint8_t)((next_clu >> 8) & 0xFFu);
    g_image[off + 2] = (uint8_t)((next_clu >> 16) & 0xFFu);
    g_image[off + 3] = (uint8_t)((next_clu >> 24) & 0xFFu);
}

/* Install a File+Stream+Name dentry-set for "old" leaf-name (utf-8 "o").
 * The uniname is single uint16 == 'o' (0x006F), length 1. SetChecksum is
 * computed via exfat_calc_chksum16 with CS_DIR_ENTRY. */
static void install_dentry_set_for_src(int is_dir)
{
    struct exfat_dentry set[3];
    memset(set, 0, sizeof(set));
    set[0].type = (uint8_t)EXFAT_FILE;
    set[0].dentry.file.num_ext = 2u;
    set[0].dentry.file.attr    = is_dir ? (uint16_t)ATTR_SUBDIR
                                        : (uint16_t)ATTR_ARCHIVE;
    set[0].dentry.file.checksum = 0u;

    set[1].type = (uint8_t)EXFAT_STREAM;
    set[1].dentry.stream.flags     = (uint8_t)ALLOC_NO_FAT_CHAIN;
    set[1].dentry.stream.name_len  = 1u;
    {
        uint8_t name_buf[2] = {0x6F, 0x00};
        set[1].dentry.stream.name_hash =
            exfat_calc_chksum16(name_buf, 2, 0u, CS_DEFAULT);
    }
    set[1].dentry.stream.start_clu  = EXFAT_EOF_CLUSTER;
    set[1].dentry.stream.valid_size = 0u;
    set[1].dentry.stream.size       = 0u;

    set[2].type = (uint8_t)EXFAT_NAME;
    set[2].dentry.name.unicode_0_14[0] = 0x006Fu;   /* 'o' (LE host) */

    uint16_t chksum = exfat_calc_chksum16(set, 3 * (int)DENTRY_SIZE,
                                          0, CS_DIR_ENTRY);
    set[0].dentry.file.checksum = chksum;

    uint64_t parent_data_off =
        (uint64_t)(TRN_CLU_OFFSET +
                   (TRN_PARENT_DIR_CLU - EXFAT_RESERVED_CLUSTERS)) *
        TRN_BLOCKSIZE;
    uint64_t entry_off = parent_data_off +
                         (uint64_t)TRN_SRC_ENTRY * (uint64_t)DENTRY_SIZE;
    memcpy(&g_image[entry_off], set, sizeof(set));
}

static int rename_setup(void **state)
{
    (void)state;
    memset(g_image, 0u, sizeof(g_image));
    for (uint32_t i = 0; i < TRN_NUM_CLUSTERS; i++) {
        fat_image_set(i, EXFAT_EOF_CLUSTER);
    }
    install_dentry_set_for_src(0);
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    g_sbi = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_src_parent_ei = (exfat_inode_info *)calloc(1u, sizeof(*g_src_parent_ei));
    assert_non_null(g_src_parent_ei);
    g_src_ei = (exfat_inode_info *)calloc(1u, sizeof(*g_src_ei));
    assert_non_null(g_src_ei);
    g_src_parent_vp = (struct Vnode *)calloc(1u, sizeof(*g_src_parent_vp));
    assert_non_null(g_src_parent_vp);
    g_src_vp = (struct Vnode *)calloc(1u, sizeof(*g_src_vp));
    assert_non_null(g_src_vp);
    g_dst_parent_vp = (struct Vnode *)calloc(1u, sizeof(*g_dst_parent_vp));
    assert_non_null(g_dst_parent_vp);
    g_mount = (struct Mount *)calloc(1u, sizeof(*g_mount));
    assert_non_null(g_mount);
    g_vol_amap = (uint8_t *)calloc(1u, TRN_MAP_SECTORS * TRN_BLOCKSIZE);
    assert_non_null(g_vol_amap);
    g_boot_buf = (uint8_t *)calloc(1u, TRN_BLOCKSIZE);
    assert_non_null(g_boot_buf);

    g_sbi->blocksize          = TRN_BLOCKSIZE;
    g_sbi->blocksize_bits     = 9u;
    g_sbi->cluster_size       = TRN_CLUSTER_SIZE;
    g_sbi->cluster_size_bits  = 9u;
    g_sbi->sect_per_clus_bits = 0u;
    g_sbi->num_fats           = TRN_NUM_FATS;
    g_sbi->num_clusters       = TRN_NUM_CLUSTERS;
    g_sbi->fat_offset         = TRN_FAT_OFFSET;
    g_sbi->fat_length         = TRN_FAT_LENGTH;
    g_sbi->fat2_offset        = TRN_FAT_OFFSET;
    g_sbi->clu_offset         = TRN_CLU_OFFSET;
    g_sbi->map_clu            = TRN_MAP_CLU;
    g_sbi->map_sectors        = TRN_MAP_SECTORS;
    g_sbi->dentries_per_clu   = TRN_DENTRIES_PER_CLU;
    g_sbi->vol_amap           = g_vol_amap;
    g_sbi->boot_buf           = g_boot_buf;
    g_sbi->part_id            = TRN_PART_ID;
    g_sbi->used_clusters      = 0u;
    g_sbi->clu_srch_ptr       = EXFAT_FIRST_CLUSTER;
    g_sbi->vol_flags          = 0u;
    g_sbi->vol_flags_persistent = 0u;

    g_mount->data = g_sbi;
    g_src_parent_vp->originMount = g_mount;
    g_src_parent_vp->data = g_src_parent_ei;
    g_dst_parent_vp->originMount = g_mount;
    g_dst_parent_vp->data = g_src_parent_ei;   /* default: same parent */

    g_src_parent_ei->type        = TYPE_DIR;
    g_src_parent_ei->flags       = ALLOC_NO_FAT_CHAIN;
    g_src_parent_ei->start_clu   = TRN_PARENT_DIR_CLU;
    g_src_parent_ei->size        = (uint64_t)TRN_CLUSTER_SIZE;
    g_src_parent_ei->i_size_ondisk = (uint64_t)TRN_CLUSTER_SIZE;
    g_src_parent_ei->num_subdirs = 0u;
    g_src_parent_ei->dir.dir     = EXFAT_FIRST_CLUSTER;
    g_src_parent_ei->entry       = -1;

    g_src_ei->type          = TYPE_FILE;
    g_src_ei->flags         = ALLOC_NO_FAT_CHAIN;
    g_src_ei->dir.dir       = TRN_PARENT_DIR_CLU;
    g_src_ei->dir.size      = 1u;
    g_src_ei->dir.flags     = (uint8_t)ALLOC_NO_FAT_CHAIN;
    g_src_ei->entry         = TRN_SRC_ENTRY;
    g_src_ei->start_clu     = EXFAT_EOF_CLUSTER;
    g_src_ei->attr          = ATTR_ARCHIVE;
    g_src_ei->size          = 0u;
    g_src_ei->valid_size    = 0u;
    g_src_ei->i_size_ondisk = 0u;
    g_src_vp->data          = g_src_ei;
    g_src_vp->originMount   = g_mount;
    g_src_vp->parent        = g_src_parent_vp;

    return 0;
}

static int rename_teardown(void **state)
{
    (void)state;
    free(g_sbi); g_sbi = NULL;
    free(g_src_parent_ei); g_src_parent_ei = NULL;
    free(g_src_ei); g_src_ei = NULL;
    free(g_src_parent_vp); g_src_parent_vp = NULL;
    free(g_src_vp); g_src_vp = NULL;
    free(g_dst_parent_vp); g_dst_parent_vp = NULL;
    free(g_mount); g_mount = NULL;
    free(g_vol_amap); g_vol_amap = NULL;
    free(g_boot_buf); g_boot_buf = NULL;
    mock_disk_unload();
    return 0;
}

/* ---------- Case 6: validation failures (-EINVAL) ---------- */

static void test_rename_null_src(void **state)
{
    (void)state;
    int rc = VfsExfatRename(NULL, g_dst_parent_vp, "o", "n");
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_rename_null_dst_parent(void **state)
{
    (void)state;
    int rc = VfsExfatRename(g_src_vp, NULL, "o", "n");
    assert_int_equal(rc, -EINVAL);
}

static void test_rename_null_srcname(void **state)
{
    (void)state;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, NULL, "n");
    assert_int_equal(rc, -EINVAL);
}

static void test_rename_null_dstname(void **state)
{
    (void)state;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", NULL);
    assert_int_equal(rc, -EINVAL);
}

static void test_rename_empty_dstname(void **state)
{
    (void)state;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", "");
    assert_int_equal(rc, -EINVAL);
}

static void test_rename_null_mount_data(void **state)
{
    (void)state;
    g_mount->data = NULL;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", "n");
    assert_int_equal(rc, -EINVAL);
}

static void test_rename_null_src_data(void **state)
{
    (void)state;
    g_src_vp->data = NULL;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", "n");
    assert_int_equal(rc, -EINVAL);
}

static void test_rename_null_src_parent(void **state)
{
    (void)state;
    g_src_vp->parent = NULL;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", "n");
    assert_int_equal(rc, -EINVAL);
}

static void test_rename_deleted_src(void **state)
{
    (void)state;
    g_src_ei->dir.dir = DIR_DELETED;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", "n");
    assert_int_equal(rc, -EINVAL);
}

static void test_rename_negative_entry(void **state)
{
    (void)state;
    g_src_ei->entry = -1;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", "n");
    assert_int_equal(rc, -EINVAL);
}

static void test_rename_src_parent_not_dir(void **state)
{
    (void)state;
    g_src_parent_ei->type = TYPE_FILE;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", "n");
    assert_int_equal(rc, -EINVAL);
}

/* Invariant exfat-rename-cross-fs-refused: different sbi in dstParent. */
static void test_rename_cross_fs_refused(void **state)
{
    (void)state;
    /* Build a foreign mount with a different sbi pointer. */
    struct Mount *foreign_mount = (struct Mount *)calloc(1, sizeof(*foreign_mount));
    exfat_sb_info *foreign_sbi = (exfat_sb_info *)calloc(1, sizeof(*foreign_sbi));
    foreign_mount->data = foreign_sbi;
    g_dst_parent_vp->originMount = foreign_mount;

    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", "n");
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);

    free(foreign_mount);
    free(foreign_sbi);
}

/* ---------- Case 1: success — same-parent rename ---------- */

static void test_rename_same_parent_happy(void **state)
{
    (void)state;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", "n");
    assert_int_equal(rc, 0);
    /* src->data entry now points at a different slot (new_slot allocated
     * by exfat_alloc_dentry_slot). The old slot 0 was tombstoned. */
    assert_int_not_equal(g_src_ei->entry, TRN_SRC_ENTRY);
    /* src not deleted. */
    assert_int_not_equal((unsigned int)g_src_ei->dir.dir, DIR_DELETED);
    /* ATTR_ARCHIVE re-set (invariant exfat-rename-archive-bit-set-on-file). */
    assert_true((g_src_ei->attr & ATTR_ARCHIVE) != 0u);
}

/* Invariant exfat-rename-archive-bit-set-on-file: even if src had archive
 * cleared, rename re-sets it. */
static void test_rename_invariant_archive_set(void **state)
{
    (void)state;
    g_src_ei->attr = 0u;
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", "n");
    assert_int_equal(rc, 0);
    assert_true((g_src_ei->attr & ATTR_ARCHIVE) != 0u);
}

/* Invariant exfat-rename-subdir-accounting-cross-dir on simple cross-dir
 * move of a FILE: neither parent's num_subdirs is touched. */
static void test_rename_cross_dir_file_no_subdir_change(void **state)
{
    (void)state;
    exfat_inode_info *other_parent_ei =
        (exfat_inode_info *)calloc(1, sizeof(*other_parent_ei));
    other_parent_ei->type        = TYPE_DIR;
    other_parent_ei->flags       = ALLOC_NO_FAT_CHAIN;
    other_parent_ei->start_clu   = EXFAT_FIRST_CLUSTER + 1u;
    other_parent_ei->size        = (uint64_t)TRN_CLUSTER_SIZE;
    other_parent_ei->i_size_ondisk = (uint64_t)TRN_CLUSTER_SIZE;
    other_parent_ei->num_subdirs = 0u;
    other_parent_ei->dir.dir     = EXFAT_FIRST_CLUSTER + 1u;
    other_parent_ei->entry       = -1;

    struct Vnode *other_parent_vp = (struct Vnode *)calloc(1, sizeof(*other_parent_vp));
    other_parent_vp->originMount = g_mount;
    other_parent_vp->data        = other_parent_ei;

    g_src_parent_ei->num_subdirs = 5u;   /* sentinel */
    uint32_t before_src_subdirs = g_src_parent_ei->num_subdirs;
    uint32_t before_dst_subdirs = other_parent_ei->num_subdirs;

    int rc = VfsExfatRename(g_src_vp, other_parent_vp, "o", "n");
    /* File cross-dir move: neither num_subdirs changes. */
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_src_parent_ei->num_subdirs,
                     (unsigned int)before_src_subdirs);
    assert_int_equal((unsigned int)other_parent_ei->num_subdirs,
                     (unsigned int)before_dst_subdirs);

    free(other_parent_vp);
    free(other_parent_ei);
}

/* Long dstName → ENAMETOOLONG (Case 7). UTF-8 input > 255 UTF-16 units. */
static void test_rename_name_too_long(void **state)
{
    (void)state;
    char long_name[EXFAT_MAX_NAME_LEN + 10];
    memset(long_name, 'a', sizeof(long_name) - 1);
    long_name[sizeof(long_name) - 1] = '\0';
    int rc = VfsExfatRename(g_src_vp, g_dst_parent_vp, "o", long_name);
    assert_int_equal(rc, -ENAMETOOLONG);
}

const struct CMUnitTest test_rename_tests[] = {
    cmocka_unit_test_setup_teardown(test_rename_null_src,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_null_dst_parent,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_null_srcname,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_null_dstname,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_empty_dstname,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_null_mount_data,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_null_src_data,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_null_src_parent,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_deleted_src,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_negative_entry,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_src_parent_not_dir,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_cross_fs_refused,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_same_parent_happy,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_invariant_archive_set,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_cross_dir_file_no_subdir_change,
                                    rename_setup, rename_teardown),
    cmocka_unit_test_setup_teardown(test_rename_name_too_long,
                                    rename_setup, rename_teardown),
};
const size_t test_rename_tests_count =
    sizeof(test_rename_tests) / sizeof(test_rename_tests[0]);
