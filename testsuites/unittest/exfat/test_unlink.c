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
 * test_unlink.c — host cmocka suite for VfsExfatUnlink
 * (spec: spec/exfat/inode/exfat_unlink.spec)
 *
 * TODO (callee handles, surfaced for reviewer):
 *   - Makefile::PROD_SRCS already has $(EXFAT)/exfat_inode.c (shared with mkdir).
 *   - main.c: add extern test_unlink_tests[] / test_unlink_tests_count and a
 *     run_suite("unlink", ...) call.
 *
 * Layer-B note: VfsHashRemove / Reclaim hook + VFS path_cache eviction are
 * exercised by QEMU LTP smoke (Wave B), not by this host suite.
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

extern int VfsExfatUnlink(struct Vnode *parent_vp, struct Vnode *target_vp,
                          const char *fileName);
extern uint16_t exfat_calc_chksum16(const void *data, int len, uint16_t chksum,
                                    int type);

#define TU_BLOCKSIZE       512u
#define TU_CLUSTER_SIZE    512u
#define TU_PART_ID         123
#define TU_NUM_CLUSTERS    (8u + EXFAT_RESERVED_CLUSTERS)
#define TU_MAP_CLU         2u
#define TU_MAP_SECTORS     1u
#define TU_FAT_OFFSET      1u
#define TU_FAT_LENGTH      1u
#define TU_CLU_OFFSET      4u
#define TU_NUM_FATS        1u
#define TU_NSECTORS        16u
#define TU_IMG_LEN         (TU_NSECTORS * TU_BLOCKSIZE)
#define TU_VOL_FLAGS_OFF   106u

#define TU_PARENT_DIR_CLU  EXFAT_FIRST_CLUSTER
#define TU_TARGET_DATA_CLU (EXFAT_FIRST_CLUSTER + 1u)
#define TU_TARGET_ENTRY    0
#define TU_DENTRIES_PER_CLU     (TU_CLUSTER_SIZE / DENTRY_SIZE)

static exfat_sb_info     *g_sbi;
static exfat_inode_info  *g_target_ei;
static struct Vnode      *g_parent_vp;
static struct Vnode      *g_target_vp;
static struct Mount      *g_mount;
static uint8_t           *g_vol_amap;
static uint8_t           *g_boot_buf;
static uint8_t            g_image[TU_IMG_LEN];

static void fat_image_set(uint32_t clu, uint32_t next_clu)
{
    uint64_t off = (uint64_t)TU_FAT_OFFSET * TU_BLOCKSIZE + (uint64_t)clu * 4u;
    g_image[off + 0] = (uint8_t)(next_clu & 0xFFu);
    g_image[off + 1] = (uint8_t)((next_clu >> 8) & 0xFFu);
    g_image[off + 2] = (uint8_t)((next_clu >> 16) & 0xFFu);
    g_image[off + 3] = (uint8_t)((next_clu >> 24) & 0xFFu);
}

/* Build a 3-entry File+Stream+Name set with a real CS_DIR_ENTRY checksum
 * (exfat_validate_dentry_set computes via exfat_calc_chksum16 with type
 * CS_DIR_ENTRY which skips bytes 2-3 of set[0] — the SetChecksum field).
 * Then lay it into the parent directory's first cluster at TU_TARGET_ENTRY. */
static void install_dentry_set_for_target(void)
{
    struct exfat_dentry set[3];
    memset(set, 0, sizeof(set));
    set[0].type = (uint8_t)EXFAT_FILE;
    set[0].dentry.file.num_ext  = 2u;
    set[0].dentry.file.checksum = 0u;   /* placeholder — overwritten below */
    set[1].type = (uint8_t)EXFAT_STREAM;
    set[2].type = (uint8_t)EXFAT_NAME;

    uint16_t chksum = exfat_calc_chksum16(set, 3 * (int)DENTRY_SIZE,
                                          0, CS_DIR_ENTRY);
    set[0].dentry.file.checksum = chksum;

    uint64_t parent_data_off =
        (uint64_t)(TU_CLU_OFFSET +
                   (TU_PARENT_DIR_CLU - EXFAT_RESERVED_CLUSTERS)) * TU_BLOCKSIZE;
    uint64_t entry_off = parent_data_off +
                         (uint64_t)TU_TARGET_ENTRY * (uint64_t)DENTRY_SIZE;
    memcpy(&g_image[entry_off], set, sizeof(set));
}

static int unlink_setup(void **state)
{
    (void)state;
    memset(g_image, 0u, sizeof(g_image));
    for (uint32_t i = 0; i < TU_NUM_CLUSTERS; i++) {
        fat_image_set(i, EXFAT_EOF_CLUSTER);
    }
    g_image[TU_VOL_FLAGS_OFF + 0] = 0;
    g_image[TU_VOL_FLAGS_OFF + 1] = 0;
    install_dentry_set_for_target();
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    g_sbi = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_target_ei = (exfat_inode_info *)calloc(1u, sizeof(*g_target_ei));
    assert_non_null(g_target_ei);
    g_parent_vp = (struct Vnode *)calloc(1u, sizeof(*g_parent_vp));
    assert_non_null(g_parent_vp);
    g_target_vp = (struct Vnode *)calloc(1u, sizeof(*g_target_vp));
    assert_non_null(g_target_vp);
    g_mount = (struct Mount *)calloc(1u, sizeof(*g_mount));
    assert_non_null(g_mount);
    g_vol_amap = (uint8_t *)calloc(1u, TU_MAP_SECTORS * TU_BLOCKSIZE);
    assert_non_null(g_vol_amap);
    g_boot_buf = (uint8_t *)calloc(1u, TU_BLOCKSIZE);
    assert_non_null(g_boot_buf);

    g_sbi->blocksize          = TU_BLOCKSIZE;
    g_sbi->blocksize_bits     = 9u;
    g_sbi->cluster_size       = TU_CLUSTER_SIZE;
    g_sbi->cluster_size_bits  = 9u;
    g_sbi->sect_per_clus_bits = 0u;
    g_sbi->num_fats           = TU_NUM_FATS;
    g_sbi->num_clusters       = TU_NUM_CLUSTERS;
    g_sbi->fat_offset         = TU_FAT_OFFSET;
    g_sbi->fat_length         = TU_FAT_LENGTH;
    g_sbi->fat2_offset        = TU_FAT_OFFSET;
    g_sbi->clu_offset         = TU_CLU_OFFSET;
    g_sbi->map_clu            = TU_MAP_CLU;
    g_sbi->map_sectors        = TU_MAP_SECTORS;
    g_sbi->dentries_per_clu   = TU_DENTRIES_PER_CLU;
    g_sbi->vol_amap           = g_vol_amap;
    g_sbi->boot_buf           = g_boot_buf;
    g_sbi->part_id            = TU_PART_ID;
    g_sbi->used_clusters      = 0u;
    g_sbi->clu_srch_ptr       = EXFAT_FIRST_CLUSTER;
    g_sbi->vol_flags          = 0u;
    g_sbi->vol_flags_persistent = 0u;

    g_mount->data = g_sbi;
    g_parent_vp->originMount = g_mount;
    g_parent_vp->data = NULL;

    g_target_ei->type          = TYPE_FILE;
    g_target_ei->flags         = ALLOC_FAT_CHAIN;
    g_target_ei->dir.dir       = TU_PARENT_DIR_CLU;
    g_target_ei->dir.size      = 1u;
    g_target_ei->dir.flags     = (uint8_t)ALLOC_NO_FAT_CHAIN;
    g_target_ei->entry         = TU_TARGET_ENTRY;
    g_target_ei->start_clu     = EXFAT_EOF_CLUSTER;
    g_target_ei->size          = 0u;
    g_target_ei->valid_size    = 0u;
    g_target_ei->i_size_ondisk = 0u;
    g_target_vp->data = g_target_ei;

    return 0;
}

static int unlink_teardown(void **state)
{
    (void)state;
    free(g_sbi); g_sbi = NULL;
    free(g_target_ei); g_target_ei = NULL;
    free(g_parent_vp); g_parent_vp = NULL;
    free(g_target_vp); g_target_vp = NULL;
    free(g_mount); g_mount = NULL;
    free(g_vol_amap); g_vol_amap = NULL;
    free(g_boot_buf); g_boot_buf = NULL;
    mock_disk_unload();
    return 0;
}

/* Mark target as a non-empty file: 1 cluster at TU_TARGET_DATA_CLU. */
static void preload_target_with_one_cluster(void)
{
    uint32_t cur = TU_TARGET_DATA_CLU;
    fat_image_set(cur, EXFAT_EOF_CLUSTER);
    uint32_t ent = cur - EXFAT_RESERVED_CLUSTERS;
    g_vol_amap[ent / 8u] |= (uint8_t)(1u << (ent & 7u));
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();
    g_target_ei->start_clu     = cur;
    g_target_ei->size          = 100u;
    g_target_ei->valid_size    = 100u;
    g_target_ei->i_size_ondisk = (uint64_t)TU_CLUSTER_SIZE;
    g_sbi->used_clusters       = 1u;
}

/* ---------- Case 2: validation failures (-EINVAL / -ENOENT) ---------- */

static void test_unlink_null_parent(void **state)
{
    (void)state;
    int rc = VfsExfatUnlink(NULL, g_target_vp, "x");
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_unlink_null_target(void **state)
{
    (void)state;
    int rc = VfsExfatUnlink(g_parent_vp, NULL, "x");
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_unlink_null_filename(void **state)
{
    (void)state;
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, NULL);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_unlink_null_mount_data(void **state)
{
    (void)state;
    g_mount->data = NULL;
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, -EINVAL);
}

static void test_unlink_null_target_data(void **state)
{
    (void)state;
    g_target_vp->data = NULL;
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, -EINVAL);
}

static void test_unlink_directory_rejected(void **state)
{
    (void)state;
    g_target_ei->type = TYPE_DIR;
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_unlink_already_tombstoned(void **state)
{
    (void)state;
    g_target_ei->dir.dir = DIR_DELETED;
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, -ENOENT);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_unlink_negative_entry(void **state)
{
    (void)state;
    g_target_ei->entry = -1;
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, -ENOENT);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

/* ---------- Case 3: Phase 1 fetch / validate failure (-EIO) ---------- */

static void test_unlink_dentry_fetch_io_error(void **state)
{
    (void)state;
    /* First read inside Phase 1 is the dentry-set fetch. */
    mock_disk_set_read_fail_at(1);
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, -EIO);
    /* Case 3 must NOT apply the in-memory tombstone. */
    assert_int_not_equal((unsigned int)g_target_ei->dir.dir, DIR_DELETED);
}

/* ---------- Case 1b: empty file fast path (success without Phase 2) ---------- */

static void test_unlink_empty_file_skips_phase2(void **state)
{
    (void)state;
    /* Default setup: start_clu = EXFAT_EOF_CLUSTER, vol_amap all zero. */
    uint32_t writes_before = (uint32_t)mock_disk_write_count();
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, 0);
    /* In-memory tombstone applied. */
    assert_int_equal((unsigned int)g_target_ei->dir.dir, DIR_DELETED);
    /* Phase 1 wrote dentry-set back; Phase 2 must NOT have touched the bitmap.
     * Bitmap byte 0 still all zeros. */
    assert_int_equal((unsigned int)g_vol_amap[0], 0u);
    /* used_clusters unchanged (stayed 0). */
    assert_int_equal((unsigned int)g_sbi->used_clusters, 0u);
    /* At least one disk write happened (the tombstone write-back). */
    assert_true((uint32_t)mock_disk_write_count() > writes_before);
}

/* ---------- Case 1: success — non-empty file, Phase 2 frees clusters ---------- */

static void test_unlink_nonempty_file_releases_chain(void **state)
{
    (void)state;
    preload_target_with_one_cluster();
    uint32_t ent = TU_TARGET_DATA_CLU - EXFAT_RESERVED_CLUSTERS;
    /* Pre-Cond: bitmap bit set. */
    assert_true((g_vol_amap[ent / 8u] & (1u << (ent & 7u))) != 0u);

    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, 0);
    /* In-memory tombstone applied. */
    assert_int_equal((unsigned int)g_target_ei->dir.dir, DIR_DELETED);
    /* Phase 2: bitmap bit cleared. */
    assert_int_equal((unsigned int)(g_vol_amap[ent / 8u] & (1u << (ent & 7u))),
                     0u);
}

/* ---------- Invariant tests ---------- */

/* Invariant exfat-unlink-tombstone-prevents-reuse: a second unlink on the
 * same target after first success returns -ENOENT (tombstone observable). */
static void test_unlink_invariant_tombstone_prevents_reuse(void **state)
{
    (void)state;
    preload_target_with_one_cluster();
    int rc1 = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc1, 0);
    int rc2 = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc2, -ENOENT);
}

/* Invariant exfat-unlink-no-vnode-free: the function must not free the
 * Vnode struct or its `data` pointer. After a successful unlink, target_vp
 * and target_vp->data remain valid and reachable. */
static void test_unlink_invariant_no_vnode_free(void **state)
{
    (void)state;
    preload_target_with_one_cluster();
    void *vp_before   = (void *)g_target_vp;
    void *data_before = g_target_vp->data;
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, 0);
    assert_ptr_equal((void *)g_target_vp, vp_before);
    assert_ptr_equal(g_target_vp->data, data_before);
}

/* Invariant exfat-unlink-phase2-skipped-when-empty: with start_clu =
 * EXFAT_EOF_CLUSTER, NO bitmap mutation occurs. Stronger than Case 1b
 * happy-path: also ensures used_clusters arithmetic was not applied. */
static void test_unlink_invariant_phase2_skipped_when_empty(void **state)
{
    (void)state;
    g_sbi->used_clusters = 5u;   /* sentinel */
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 5u);
    for (uint32_t i = 0; i < TU_MAP_SECTORS * TU_BLOCKSIZE; i++) {
        assert_int_equal((unsigned int)g_vol_amap[i], 0u);
    }
}

/* Invariant exfat-unlink-phase2-size-nonzero: with start_clu != EOF and a
 * one-cluster file, Phase 2 derives chain.size >= 1 and free_cluster
 * actually clears the bitmap bit. If chain.size were 0, free_cluster
 * Case 4 would no-op and the bit would still be set after return — this
 * test asserts the bit DID flip. */
static void test_unlink_invariant_phase2_size_nonzero(void **state)
{
    (void)state;
    preload_target_with_one_cluster();
    uint32_t ent = TU_TARGET_DATA_CLU - EXFAT_RESERVED_CLUSTERS;
    int rc = VfsExfatUnlink(g_parent_vp, g_target_vp, "x");
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)(g_vol_amap[ent / 8u] & (1u << (ent & 7u))),
                     0u);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 0u);
}

const struct CMUnitTest test_unlink_tests[] = {
    cmocka_unit_test_setup_teardown(test_unlink_null_parent,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_null_target,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_null_filename,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_null_mount_data,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_null_target_data,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_directory_rejected,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_already_tombstoned,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_negative_entry,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_dentry_fetch_io_error,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_empty_file_skips_phase2,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_nonempty_file_releases_chain,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_invariant_tombstone_prevents_reuse,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_invariant_no_vnode_free,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_invariant_phase2_skipped_when_empty,
                                    unlink_setup, unlink_teardown),
    cmocka_unit_test_setup_teardown(test_unlink_invariant_phase2_size_nonzero,
                                    unlink_setup, unlink_teardown),
};
const size_t test_unlink_tests_count =
    sizeof(test_unlink_tests) / sizeof(test_unlink_tests[0]);
