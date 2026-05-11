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
 * test_mount_ops_rest.c — host cmocka suite for VfsExfatStatfs / VfsExfatSync
 * (spec: spec/exfat/interface/exfat_mount_ops_rest.spec)
 *
 * Both callbacks are `static` in exfat_super.c; we invoke them via the
 * exposed g_exfatMountOps function-pointer table (the same dispatch path
 * the real VFS uses). No I/O, no locks: pure metadata reader + no-op.
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <errno.h>
#include <cmocka.h>
#include <sys/statfs.h>

#include "exfat.h"
#include "fs/mount.h"

extern struct MountOps g_exfatMountOps;

static exfat_sb_info *g_sbi;
static struct Mount  *g_mount;
static struct statfs *g_sbp;

static int mor_setup(void **state)
{
    (void)state;
    g_sbi = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_mount = (struct Mount *)calloc(1u, sizeof(*g_mount));
    assert_non_null(g_mount);
    g_sbp = (struct statfs *)calloc(1u, sizeof(*g_sbp));
    assert_non_null(g_sbp);

    g_sbi->cluster_size  = 4096u;
    g_sbi->num_clusters  = 1000u;
    g_sbi->used_clusters = 250u;
    g_mount->data = g_sbi;
    return 0;
}

static int mor_teardown(void **state)
{
    (void)state;
    free(g_sbi); g_sbi = NULL;
    free(g_mount); g_mount = NULL;
    free(g_sbp); g_sbp = NULL;
    return 0;
}

/* ---------- Case 1: Statfs success — full happy path ---------- */

static void test_statfs_happy(void **state)
{
    (void)state;
    int rc = g_exfatMountOps.Statfs(g_mount, g_sbp);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_sbp->f_type, (unsigned long)EXFAT_SUPER_MAGIC);
    assert_int_equal((unsigned long)g_sbp->f_bsize, 4096ul);
    assert_int_equal((unsigned long)g_sbp->f_blocks, 998ul);
    assert_int_equal((unsigned long)g_sbp->f_bfree, 748ul);
    assert_int_equal((unsigned long)g_sbp->f_bavail, 748ul);
    assert_int_equal((unsigned long)g_sbp->f_namelen, (unsigned long)EXFAT_MAX_NAME_LEN);
}

/* ---------- Case 1 boundary: num_clusters < 2 → f_blocks == 0 ---------- */

static void test_statfs_tiny_volume(void **state)
{
    (void)state;
    g_sbi->num_clusters  = 1u;
    g_sbi->used_clusters = 0u;
    int rc = g_exfatMountOps.Statfs(g_mount, g_sbp);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_sbp->f_blocks, 0ul);
    assert_int_equal((unsigned long)g_sbp->f_bfree, 0ul);
}

/* ---------- Case 2: validation failure on NULL ---------- */

static void test_statfs_null_mount(void **state)
{
    (void)state;
    int rc = g_exfatMountOps.Statfs(NULL, g_sbp);
    assert_int_equal(rc, -EINVAL);
}

static void test_statfs_null_data(void **state)
{
    (void)state;
    g_mount->data = NULL;
    int rc = g_exfatMountOps.Statfs(g_mount, g_sbp);
    assert_int_equal(rc, -EINVAL);
}

static void test_statfs_null_sbp(void **state)
{
    (void)state;
    int rc = g_exfatMountOps.Statfs(g_mount, NULL);
    assert_int_equal(rc, -EINVAL);
}

/* ---------- Invariant exfat-mount-ops-rest-untracked-used-as-zero-free ---------- */

static void test_statfs_invariant_untracked_reports_zero_free(void **state)
{
    (void)state;
    g_sbi->used_clusters = EXFAT_CLUSTERS_UNTRACKED;
    int rc = g_exfatMountOps.Statfs(g_mount, g_sbp);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_sbp->f_bfree, 0ul);
    assert_int_equal((unsigned long)g_sbp->f_bavail, 0ul);
    /* f_blocks is the geometric total, unaffected by untracked sentinel. */
    assert_int_equal((unsigned long)g_sbp->f_blocks, 998ul);
}

/* ---------- Invariant exfat-mount-ops-rest-zeroed-statfs ---------- */

static void test_statfs_invariant_zeroed_statfs(void **state)
{
    (void)state;
    memset(g_sbp, 0xAA, sizeof(*g_sbp));
    int rc = g_exfatMountOps.Statfs(g_mount, g_sbp);
    assert_int_equal(rc, 0);
    /* Fields not populated by exFAT (f_files / f_ffree / f_frsize / f_flags)
     * must be zero after the memset_s in VfsExfatStatfs. */
    assert_int_equal((unsigned long)g_sbp->f_files, 0ul);
    assert_int_equal((unsigned long)g_sbp->f_ffree, 0ul);
    assert_int_equal((long)g_sbp->f_frsize, 0l);
    assert_int_equal((long)g_sbp->f_flags, 0l);
}

/* ---------- Invariant exfat-mount-ops-rest-readonly-sbi ---------- */

static void test_statfs_invariant_readonly_sbi(void **state)
{
    (void)state;
    uint32_t before_clu  = g_sbi->cluster_size;
    uint32_t before_used = g_sbi->used_clusters;
    uint32_t before_num  = g_sbi->num_clusters;
    int rc = g_exfatMountOps.Statfs(g_mount, g_sbp);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->cluster_size, (unsigned int)before_clu);
    assert_int_equal((unsigned int)g_sbi->used_clusters, (unsigned int)before_used);
    assert_int_equal((unsigned int)g_sbi->num_clusters, (unsigned int)before_num);
}

/* ---------- Case 3: Sync no-op (any input) ---------- */

static void test_sync_returns_zero(void **state)
{
    (void)state;
    int rc1 = g_exfatMountOps.Sync(g_mount);
    int rc2 = g_exfatMountOps.Sync(NULL);
    assert_int_equal(rc1, 0);
    assert_int_equal(rc2, 0);
}

/* ---------- Invariant exfat-mount-ops-rest-sync-noop-stable ---------- */

static void test_sync_invariant_no_side_effect(void **state)
{
    (void)state;
    uint32_t before_used = g_sbi->used_clusters;
    int rc = g_exfatMountOps.Sync(g_mount);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->used_clusters, (unsigned int)before_used);
}

const struct CMUnitTest test_mount_ops_rest_tests[] = {
    cmocka_unit_test_setup_teardown(test_statfs_happy,
                                    mor_setup, mor_teardown),
    cmocka_unit_test_setup_teardown(test_statfs_tiny_volume,
                                    mor_setup, mor_teardown),
    cmocka_unit_test_setup_teardown(test_statfs_null_mount,
                                    mor_setup, mor_teardown),
    cmocka_unit_test_setup_teardown(test_statfs_null_data,
                                    mor_setup, mor_teardown),
    cmocka_unit_test_setup_teardown(test_statfs_null_sbp,
                                    mor_setup, mor_teardown),
    cmocka_unit_test_setup_teardown(test_statfs_invariant_untracked_reports_zero_free,
                                    mor_setup, mor_teardown),
    cmocka_unit_test_setup_teardown(test_statfs_invariant_zeroed_statfs,
                                    mor_setup, mor_teardown),
    cmocka_unit_test_setup_teardown(test_statfs_invariant_readonly_sbi,
                                    mor_setup, mor_teardown),
    cmocka_unit_test_setup_teardown(test_sync_returns_zero,
                                    mor_setup, mor_teardown),
    cmocka_unit_test_setup_teardown(test_sync_invariant_no_side_effect,
                                    mor_setup, mor_teardown),
};
const size_t test_mount_ops_rest_tests_count =
    sizeof(test_mount_ops_rest_tests) / sizeof(test_mount_ops_rest_tests[0]);
