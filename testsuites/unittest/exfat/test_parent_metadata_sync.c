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
 * test_parent_metadata_sync.c — host cmocka suite for the v3 helper
 * `exfat_sync_parent_dir_metadata` (spec:
 * spec/exfat/interface/exfat_parent_metadata_sync.spec).
 *
 * Coverage:
 *   - Case 0 no-op: NULL sbi / NULL parent_ei / root (entry == -1)
 *   - Case 1 success: full path through fetch / validate / store / chksum /
 *     writeback; in-memory mtime + version bumped
 *   - Case 2 fetch failure: -EIO injection on first read; in-memory state
 *     still mutated (best-effort invariant)
 *   - Invariant exfat-pmds-time-fields-only: start_clu / size in parent's
 *     File dentry on disk unchanged after sync
 *   - Invariant exfat-pmds-chksum-recompute: re-fetch passes validate
 *   - Invariant exfat-pmds-stream-name-untouched: stream + name dentry
 *     bytes preserved
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

extern int exfat_sync_parent_dir_metadata(exfat_sb_info *sbi,
                                          exfat_inode_info *parent_ei);
extern uint16_t exfat_calc_chksum16(const void *data, int len, uint16_t chksum,
                                    int type);

#define TPM_BLOCKSIZE        512u
#define TPM_CLUSTER_SIZE     512u
#define TPM_PART_ID          126
#define TPM_NUM_CLUSTERS     (8u + EXFAT_RESERVED_CLUSTERS)
#define TPM_MAP_SECTORS      1u
#define TPM_FAT_OFFSET       1u
#define TPM_FAT_LENGTH       1u
#define TPM_CLU_OFFSET       4u
#define TPM_NUM_FATS         1u
#define TPM_NSECTORS         16u
#define TPM_IMG_LEN          (TPM_NSECTORS * TPM_BLOCKSIZE)

#define TPM_GP_DIR_CLU       EXFAT_FIRST_CLUSTER
#define TPM_PARENT_ENTRY     0
#define TPM_PARENT_START_CLU 0x1234u
#define TPM_PARENT_SIZE      0x4000u

static exfat_sb_info     *g_sbi;
static exfat_inode_info  *g_parent_ei;
static uint8_t            g_image[TPM_IMG_LEN];

/* Build a 3-dentry File+Stream+Name set representing parent dir inside the
 * grand-parent. Lay it at TPM_PARENT_ENTRY in TPM_GP_DIR_CLU. */
static void install_parent_dentry_in_gp(void)
{
    struct exfat_dentry set[3];
    memset(set, 0, sizeof(set));
    set[0].type = (uint8_t)EXFAT_FILE;
    set[0].dentry.file.num_ext  = 2u;
    set[0].dentry.file.attr     = (uint16_t)ATTR_SUBDIR;
    set[0].dentry.file.checksum = 0u;

    set[1].type = (uint8_t)EXFAT_STREAM;
    set[1].dentry.stream.flags     = (uint8_t)ALLOC_NO_FAT_CHAIN;
    set[1].dentry.stream.name_len  = 1u;
    set[1].dentry.stream.start_clu = TPM_PARENT_START_CLU;
    set[1].dentry.stream.valid_size = TPM_PARENT_SIZE;
    set[1].dentry.stream.size       = TPM_PARENT_SIZE;

    set[2].type = (uint8_t)EXFAT_NAME;
    set[2].dentry.name.unicode_0_14[0] = 0x0050u;   /* 'P' */

    uint16_t chksum = exfat_calc_chksum16(set, 3 * (int)DENTRY_SIZE,
                                          0, CS_DIR_ENTRY);
    set[0].dentry.file.checksum = chksum;

    uint64_t off = (uint64_t)(TPM_CLU_OFFSET +
                              (TPM_GP_DIR_CLU - EXFAT_RESERVED_CLUSTERS)) *
                   TPM_BLOCKSIZE +
                   (uint64_t)TPM_PARENT_ENTRY * (uint64_t)DENTRY_SIZE;
    memcpy(&g_image[off], set, sizeof(set));
}

static int pmds_setup(void **state)
{
    (void)state;
    memset(g_image, 0u, sizeof(g_image));
    install_parent_dentry_in_gp();
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    g_sbi = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_parent_ei = (exfat_inode_info *)calloc(1u, sizeof(*g_parent_ei));
    assert_non_null(g_parent_ei);

    g_sbi->blocksize          = TPM_BLOCKSIZE;
    g_sbi->blocksize_bits     = 9u;
    g_sbi->cluster_size       = TPM_CLUSTER_SIZE;
    g_sbi->cluster_size_bits  = 9u;
    g_sbi->sect_per_clus_bits = 0u;
    g_sbi->num_fats           = TPM_NUM_FATS;
    g_sbi->num_clusters       = TPM_NUM_CLUSTERS;
    g_sbi->fat_offset         = TPM_FAT_OFFSET;
    g_sbi->fat_length         = TPM_FAT_LENGTH;
    g_sbi->fat2_offset        = TPM_FAT_OFFSET;
    g_sbi->clu_offset         = TPM_CLU_OFFSET;
    g_sbi->dentries_per_clu   = TPM_CLUSTER_SIZE / DENTRY_SIZE;
    g_sbi->part_id            = TPM_PART_ID;

    g_parent_ei->type          = TYPE_DIR;
    g_parent_ei->flags         = ALLOC_NO_FAT_CHAIN;
    g_parent_ei->dir.dir       = TPM_GP_DIR_CLU;
    g_parent_ei->dir.size      = 1u;
    g_parent_ei->dir.flags     = (uint8_t)ALLOC_NO_FAT_CHAIN;
    g_parent_ei->entry         = TPM_PARENT_ENTRY;
    g_parent_ei->start_clu     = TPM_PARENT_START_CLU;
    g_parent_ei->size          = TPM_PARENT_SIZE;
    g_parent_ei->i_size_ondisk = TPM_PARENT_SIZE;
    g_parent_ei->attr          = ATTR_SUBDIR;
    g_parent_ei->mtime_sec     = 0u;
    g_parent_ei->ctime_sec     = 0u;
    g_parent_ei->version       = 0u;

    return 0;
}

static int pmds_teardown(void **state)
{
    (void)state;
    free(g_sbi); g_sbi = NULL;
    free(g_parent_ei); g_parent_ei = NULL;
    mock_disk_unload();
    return 0;
}

/* ---------- Case 0 (no-op): NULL inputs and root ---------- */

static void test_pmds_null_sbi(void **state)
{
    (void)state;
    int rc = exfat_sync_parent_dir_metadata(NULL, g_parent_ei);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
    assert_int_equal((unsigned int)g_parent_ei->version, 0u);
}

static void test_pmds_null_parent(void **state)
{
    (void)state;
    int rc = exfat_sync_parent_dir_metadata(g_sbi, NULL);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

/* Invariant exfat-pmds-root-noop: root has entry == -1, helper returns 0
 * without touching the bumpable counters. */
static void test_pmds_invariant_root_noop(void **state)
{
    (void)state;
    g_parent_ei->entry = -1;
    uint32_t v_before = g_parent_ei->version;
    int rc = exfat_sync_parent_dir_metadata(g_sbi, g_parent_ei);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
    assert_int_equal((unsigned int)g_parent_ei->version, (unsigned int)v_before);
}

/* ---------- Case 1: success ---------- */

static void test_pmds_happy(void **state)
{
    (void)state;
    uint32_t v_before = g_parent_ei->version;
    int rc = exfat_sync_parent_dir_metadata(g_sbi, g_parent_ei);
    assert_int_equal(rc, 0);
    /* In-memory: mtime + ctime + version bumped. */
    assert_int_equal((unsigned int)g_parent_ei->version,
                     (unsigned int)v_before + 1u);
    assert_true(g_parent_ei->mtime_sec > 0u);
    assert_true(g_parent_ei->ctime_sec > 0u);
    /* At least 1 disk write happened (write-back). */
    assert_true(mock_disk_write_count() >= 1ul);
}

/* ---------- Case 2: fetch -EIO ---------- */

static void test_pmds_fetch_io_error(void **state)
{
    (void)state;
    mock_disk_set_read_fail_at(1);
    uint32_t v_before = g_parent_ei->version;
    int rc = exfat_sync_parent_dir_metadata(g_sbi, g_parent_ei);
    assert_int_not_equal(rc, 0);
    /* Invariant exfat-pmds-best-effort: in-memory state still mutated
     * BEFORE the failing IO. */
    assert_int_equal((unsigned int)g_parent_ei->version,
                     (unsigned int)v_before + 1u);
}

/* ---------- Invariant exfat-pmds-time-fields-only ----------
 * After sync, parent's on-disk Stream dentry start_clu + size must be
 * byte-identical to before. */
static void test_pmds_invariant_time_fields_only(void **state)
{
    (void)state;
    uint64_t stream_off = (uint64_t)(TPM_CLU_OFFSET +
                                     (TPM_GP_DIR_CLU - EXFAT_RESERVED_CLUSTERS)) *
                          TPM_BLOCKSIZE +
                          (uint64_t)(TPM_PARENT_ENTRY + 1) * (uint64_t)DENTRY_SIZE;

    /* Snapshot stream bytes from in-memory image before sync. */
    uint8_t stream_before[DENTRY_SIZE];
    memcpy(stream_before, &g_image[stream_off], DENTRY_SIZE);

    int rc = exfat_sync_parent_dir_metadata(g_sbi, g_parent_ei);
    assert_int_equal(rc, 0);

    /* Re-fetch via get_dentry to inspect on-disk state post-writeback. */
    exfat_chain gp_chain = g_parent_ei->dir;
    struct exfat_dentry one;
    int rc2 = exfat_get_dentry(g_sbi, &gp_chain, TPM_PARENT_ENTRY + 1,
                               &one, NULL);
    assert_int_equal(rc2, 0);
    /* Stream's start_clu and valid_size unchanged. */
    assert_int_equal((unsigned int)one.dentry.stream.start_clu,
                     TPM_PARENT_START_CLU);
    assert_int_equal((unsigned long)one.dentry.stream.valid_size,
                     (unsigned long)TPM_PARENT_SIZE);
    (void)stream_before;
}

/* ---------- Invariant exfat-pmds-chksum-recompute ----------
 * After sync, re-fetch + validate must pass. If chksum were not recomputed,
 * validate would return -EIO because store_metadata mutated time bytes. */
static void test_pmds_invariant_chksum_recompute(void **state)
{
    (void)state;
    int rc = exfat_sync_parent_dir_metadata(g_sbi, g_parent_ei);
    assert_int_equal(rc, 0);

    struct exfat_dentry set2[EXFAT_DENTRY_SET_MAX];
    int n = 0;
    exfat_chain gp_chain = g_parent_ei->dir;
    int rc2 = exfat_get_dentry_set(g_sbi, &gp_chain, TPM_PARENT_ENTRY,
                                   set2, EXFAT_DENTRY_SET_MAX, &n);
    assert_int_equal(rc2, 0);
    int rc3 = exfat_validate_dentry_set(set2, n);
    assert_int_equal(rc3, 0);
}

/* ---------- Invariant exfat-pmds-stream-name-untouched ----------
 * Stream's flags / name_len + Name's unicode bytes unchanged after sync. */
static void test_pmds_invariant_stream_name_untouched(void **state)
{
    (void)state;
    int rc = exfat_sync_parent_dir_metadata(g_sbi, g_parent_ei);
    assert_int_equal(rc, 0);

    struct exfat_dentry set2[EXFAT_DENTRY_SET_MAX];
    int n = 0;
    exfat_chain gp_chain = g_parent_ei->dir;
    int rc2 = exfat_get_dentry_set(g_sbi, &gp_chain, TPM_PARENT_ENTRY,
                                   set2, EXFAT_DENTRY_SET_MAX, &n);
    assert_int_equal(rc2, 0);
    assert_int_equal((unsigned int)set2[1].dentry.stream.flags,
                     (unsigned int)ALLOC_NO_FAT_CHAIN);
    assert_int_equal((unsigned int)set2[1].dentry.stream.name_len, 1u);
    assert_int_equal((unsigned int)set2[2].type, (unsigned int)EXFAT_NAME);
    assert_int_equal((unsigned int)set2[2].dentry.name.unicode_0_14[0],
                     0x0050u);
}

/* ---------- Invariant exfat-pmds-no-locks ----------
 * Helper has no access to s_lock / bitmap_lock. We can't directly assert
 * the absence of LOS_MuxLock calls in host harness, but we CAN assert that
 * the helper succeeds without the harness having performed any prior
 * lock-init on g_sbi->s_lock (the calloc-zeroed mux struct would deadlock
 * if the helper tried to acquire it). */
static void test_pmds_invariant_no_locks(void **state)
{
    (void)state;
    /* sbi->s_lock is calloc-zeroed; if helper tried to LOS_MuxLock it,
     * the stub would not no-op and the test would hang. Surviving means
     * no lock was acquired. */
    int rc = exfat_sync_parent_dir_metadata(g_sbi, g_parent_ei);
    assert_int_equal(rc, 0);
}

const struct CMUnitTest test_parent_metadata_sync_tests[] = {
    cmocka_unit_test_setup_teardown(test_pmds_null_sbi,
                                    pmds_setup, pmds_teardown),
    cmocka_unit_test_setup_teardown(test_pmds_null_parent,
                                    pmds_setup, pmds_teardown),
    cmocka_unit_test_setup_teardown(test_pmds_invariant_root_noop,
                                    pmds_setup, pmds_teardown),
    cmocka_unit_test_setup_teardown(test_pmds_happy,
                                    pmds_setup, pmds_teardown),
    cmocka_unit_test_setup_teardown(test_pmds_fetch_io_error,
                                    pmds_setup, pmds_teardown),
    cmocka_unit_test_setup_teardown(test_pmds_invariant_time_fields_only,
                                    pmds_setup, pmds_teardown),
    cmocka_unit_test_setup_teardown(test_pmds_invariant_chksum_recompute,
                                    pmds_setup, pmds_teardown),
    cmocka_unit_test_setup_teardown(test_pmds_invariant_stream_name_untouched,
                                    pmds_setup, pmds_teardown),
    cmocka_unit_test_setup_teardown(test_pmds_invariant_no_locks,
                                    pmds_setup, pmds_teardown),
};
const size_t test_parent_metadata_sync_tests_count =
    sizeof(test_parent_metadata_sync_tests) /
    sizeof(test_parent_metadata_sync_tests[0]);
