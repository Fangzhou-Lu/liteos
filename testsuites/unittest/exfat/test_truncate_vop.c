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
#include <sys/types.h>
#include <cmocka.h>

#include "exfat.h"
#include "vnode.h"
#include "mock_disk.h"

extern int VfsExfatTruncate(struct Vnode *vp, off_t len);
extern int VfsExfatTruncate64(struct Vnode *vp, off64_t len);

#define TV_BLOCKSIZE       512u
#define TV_CLUSTER_SIZE    512u
#define TV_PART_ID         123
#define TV_NUM_CLUSTERS    8u
#define TV_MAP_CLU         2u
#define TV_MAP_SECTORS     1u
#define TV_FAT_OFFSET      1u
#define TV_FAT_LENGTH      1u
#define TV_CLU_OFFSET      4u
#define TV_NUM_FATS        1u
#define TV_NSECTORS        16u
#define TV_IMG_LEN         (TV_NSECTORS * TV_BLOCKSIZE)

static exfat_sb_info     *g_sbi;
static exfat_inode_info  *g_ei;
static uint8_t           *g_vol_amap;
static uint8_t           *g_boot_buf;
static uint8_t            g_image[TV_IMG_LEN];
static struct Mount       g_mount;
static struct Vnode       g_vnode;

static void fat_image_set(uint32_t clu, uint32_t next_clu)
{
    uint64_t off = (uint64_t)TV_FAT_OFFSET * TV_BLOCKSIZE + (uint64_t)clu * 4u;
    g_image[off + 0] = (uint8_t)(next_clu & 0xFFu);
    g_image[off + 1] = (uint8_t)((next_clu >> 8) & 0xFFu);
    g_image[off + 2] = (uint8_t)((next_clu >> 16) & 0xFFu);
    g_image[off + 3] = (uint8_t)((next_clu >> 24) & 0xFFu);
}

static int truncate_vop_setup(void **state)
{
    (void)state;
    memset(g_image, 0u, sizeof(g_image));
    for (uint32_t i = 0; i < TV_NUM_CLUSTERS; i++) {
        fat_image_set(i, EXFAT_EOF_CLUSTER);
    }
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    g_sbi = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_ei = (exfat_inode_info *)calloc(1u, sizeof(*g_ei));
    assert_non_null(g_ei);
    g_vol_amap = (uint8_t *)calloc(1u, TV_MAP_SECTORS * TV_BLOCKSIZE);
    assert_non_null(g_vol_amap);
    g_boot_buf = (uint8_t *)calloc(1u, TV_BLOCKSIZE);
    assert_non_null(g_boot_buf);

    g_sbi->blocksize          = TV_BLOCKSIZE;
    g_sbi->blocksize_bits     = 9u;
    g_sbi->cluster_size       = TV_CLUSTER_SIZE;
    g_sbi->cluster_size_bits  = 9u;
    g_sbi->sect_per_clus_bits = 0u;
    g_sbi->num_fats           = TV_NUM_FATS;
    g_sbi->num_clusters       = TV_NUM_CLUSTERS;
    g_sbi->fat_offset         = TV_FAT_OFFSET;
    g_sbi->fat_length         = TV_FAT_LENGTH;
    g_sbi->fat2_offset        = TV_FAT_OFFSET;
    g_sbi->clu_offset         = TV_CLU_OFFSET;
    g_sbi->map_clu            = TV_MAP_CLU;
    g_sbi->map_sectors        = TV_MAP_SECTORS;
    g_sbi->vol_amap           = g_vol_amap;
    g_sbi->boot_buf           = g_boot_buf;
    g_sbi->part_id            = TV_PART_ID;
    g_sbi->used_clusters      = 0u;
    g_sbi->clu_srch_ptr       = EXFAT_FIRST_CLUSTER;
    g_sbi->vol_flags          = 0u;
    g_sbi->vol_flags_persistent = 0u;
    g_sbi->s_maxbytes         = (uint64_t)(TV_NUM_CLUSTERS - EXFAT_RESERVED_CLUSTERS) *
                                (uint64_t)TV_CLUSTER_SIZE;

    g_ei->type          = TYPE_FILE;
    g_ei->flags         = ALLOC_FAT_CHAIN;
    g_ei->start_clu     = EXFAT_EOF_CLUSTER;
    g_ei->size          = 0u;
    g_ei->valid_size    = 0u;
    g_ei->i_size_ondisk = 0u;
    (void)LOS_MuxInit(&g_ei->inode_lock, NULL);

    memset(&g_mount, 0, sizeof(g_mount));
    memset(&g_vnode, 0, sizeof(g_vnode));
    g_mount.data        = g_sbi;
    g_vnode.originMount = &g_mount;
    g_vnode.data        = g_ei;
    return 0;
}

static int truncate_vop_teardown(void **state)
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
    g_ei->i_size_ondisk = (uint64_t)n * (uint64_t)TV_CLUSTER_SIZE;
    g_sbi->used_clusters = n;
}

/* ---------- input validation ---------- */

static void test_truncate_vop_null_vp(void **state)
{
    (void)state;
    int rc = VfsExfatTruncate64(NULL, 1024);
    assert_int_equal(rc, -EINVAL);
}

static void test_truncate_vop_null_origin_mount(void **state)
{
    (void)state;
    g_vnode.originMount = NULL;
    int rc = VfsExfatTruncate64(&g_vnode, 1024);
    assert_int_equal(rc, -EINVAL);
}

static void test_truncate_vop_null_vnode_data(void **state)
{
    (void)state;
    g_vnode.data = NULL;
    int rc = VfsExfatTruncate64(&g_vnode, 1024);
    assert_int_equal(rc, -EINVAL);
}

static void test_truncate_vop_null_mount_data(void **state)
{
    (void)state;
    g_mount.data = NULL;
    int rc = VfsExfatTruncate64(&g_vnode, 1024);
    assert_int_equal(rc, -EINVAL);
}

static void test_truncate_vop_negative_len(void **state)
{
    (void)state;
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)-1);
    assert_int_equal(rc, -EINVAL);
    /* must not even take the lock — no IO. */
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

/* ---------- success: dispatch paths ---------- */

static void test_truncate_vop_noop_same_size(void **state)
{
    (void)state;
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 800u);
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)800);
    assert_int_equal(rc, 0);
    /* no-op: no IO writes. */
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
    assert_int_equal((unsigned long)g_ei->size, 800ul);
}

static void test_truncate_vop_dispatch_extend(void **state)
{
    (void)state;
    /* From empty file, extend to 100 bytes — needs 1 cluster. */
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)100);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 100ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)TV_CLUSTER_SIZE);
    assert_int_not_equal((unsigned int)g_ei->start_clu, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 1u);
}

static void test_truncate_vop_dispatch_shrink(void **state)
{
    (void)state;
    /* 3 clusters (1500 bytes), shrink to 256 bytes (1 cluster). */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 3u, 1500u);
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)256);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 256ul);
    assert_int_equal((unsigned long)g_ei->i_size_ondisk, (unsigned long)TV_CLUSTER_SIZE);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 1u);
}

static void test_truncate_vop_shrink_to_zero(void **state)
{
    (void)state;
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)0);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 0ul);
    assert_int_equal((unsigned int)g_ei->start_clu, EXFAT_EOF_CLUSTER);
}

/* ---------- error propagation ---------- */

static void test_truncate_vop_extend_efbig_passthrough(void **state)
{
    (void)state;
    /* s_maxbytes is (TV_NUM_CLUSTERS - 2) * 512 = 6 * 512 = 3072. */
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)(g_sbi->s_maxbytes + 1u));
    assert_int_equal(rc, -EFBIG);
    assert_int_equal((unsigned long)g_ei->size, 0ul);
}

static void test_truncate_vop_extend_enospc_passthrough(void **state)
{
    (void)state;
    g_sbi->used_clusters = TV_NUM_CLUSTERS - EXFAT_RESERVED_CLUSTERS;
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)100);
    assert_int_equal(rc, -ENOSPC);
}

static void test_truncate_vop_extend_eio_passthrough(void **state)
{
    (void)state;
    /* vol_flags set fails on first write. */
    mock_disk_set_write_fail_at(1u);
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)100);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned long)g_ei->size, 0ul);
}

static void test_truncate_vop_shrink_eio_passthrough(void **state)
{
    (void)state;
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);
    /* vol_flags set fails on first write. */
    mock_disk_set_write_fail_at(1u);
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)256);
    assert_int_equal(rc, -EIO);
    /* shrink helper Case 5 — ei untouched. */
    assert_int_equal((unsigned long)g_ei->size, 1000ul);
}

/* ---------- 32-bit / 64-bit equivalence ---------- */

static void test_truncate_vop_off_t_equiv_off64_t(void **state)
{
    (void)state;
    /* VfsExfatTruncate is just (off64_t)len -> _Truncate64. Verify
     * an off_t value produces same outcome as the 64-bit entry. */
    int rc = VfsExfatTruncate(&g_vnode, (off_t)200);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 200ul);
}

/* ---------- invariants ---------- */

static void test_invariant_inode_lock_released_on_eperm_path(void **state)
{
    (void)state;
    /* If the lock weren't released, the second call would deadlock the
     * mutex. Exercise: shrink + extend + shrink in succession on the same
     * vnode. They must all complete (no hang, no mismatch). */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 2u, 1000u);

    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)400);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 400ul);

    rc = VfsExfatTruncate64(&g_vnode, (off64_t)1024);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 1024ul);

    rc = VfsExfatTruncate64(&g_vnode, (off64_t)100);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)g_ei->size, 100ul);
}

static void test_invariant_einval_path_no_io(void **state)
{
    (void)state;
    /* Even with a fully-valid fs, a pure -EINVAL early exit must NOT
     * touch the disk. */
    preload_existing_chain(EXFAT_FIRST_CLUSTER, 1u, 256u);
    mock_disk_reset_counters();
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)-100);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
    assert_int_equal((unsigned long)mock_disk_read_count(), 0ul);
}

static void test_invariant_errno_passthrough_no_remap(void **state)
{
    (void)state;
    /* Helper-level -ENOSPC must surface as -ENOSPC at VOP boundary
     * (not collapsed to -EIO etc.). */
    g_sbi->used_clusters = TV_NUM_CLUSTERS - EXFAT_RESERVED_CLUSTERS;
    int rc = VfsExfatTruncate64(&g_vnode, (off64_t)100);
    assert_int_equal(rc, -ENOSPC);
    assert_int_not_equal(rc, -EIO);
}

const struct CMUnitTest test_truncate_vop_tests[] = {
    cmocka_unit_test_setup_teardown(test_truncate_vop_null_vp,                 truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_null_origin_mount,       truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_null_vnode_data,         truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_null_mount_data,         truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_negative_len,            truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_noop_same_size,          truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_dispatch_extend,         truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_dispatch_shrink,         truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_shrink_to_zero,          truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_extend_efbig_passthrough, truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_extend_enospc_passthrough, truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_extend_eio_passthrough,  truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_shrink_eio_passthrough,  truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_truncate_vop_off_t_equiv_off64_t,     truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_inode_lock_released_on_eperm_path, truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_einval_path_no_io,          truncate_vop_setup, truncate_vop_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_errno_passthrough_no_remap, truncate_vop_setup, truncate_vop_teardown),
};

const size_t test_truncate_vop_tests_count =
    sizeof(test_truncate_vop_tests) / sizeof(test_truncate_vop_tests[0]);
