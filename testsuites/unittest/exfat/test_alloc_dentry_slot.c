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
 * test_alloc_dentry_slot — exfat_alloc_dentry_slot (Wave B Stage 4b).
 *
 * Default test image root cluster:
 *   slot 0  = BITMAP   (type 0x81, in-use)
 *   slot 1  = UPCASE   (type 0x82, in-use)
 *   slot 2  = UNUSED   (type 0x00, terminator → all 14 remaining slots free)
 *
 * → max_dentries = 1 cluster * (512/32) = 16 entries.
 *
 * Tests use exfat_set_dentry (Stage 4a, already validated) to seed
 * synthetic in-use / deleted layouts before exercising alloc_dentry_slot.
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

/* SUT */
int exfat_alloc_dentry_slot(const exfat_sb_info *sbi, const exfat_chain *dir,
                            int n_entries, int *slot_idx_out);
/* Stage 4a helper used to seed test layouts. */
int exfat_set_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                     int entry_idx, const struct exfat_dentry *in);

/* ---- harness ------------------------------------------------------------ */

static void make_sbi(exfat_sb_info *sbi)
{
    memset(sbi, 0, sizeof(*sbi));
    sbi->sect_size_bits     = 9;
    sbi->sect_per_clus_bits = 0;
    sbi->blocksize          = 512;
    sbi->cluster_size       = 512;
    sbi->cluster_size_bits  = 9;
    sbi->dentries_per_clu   = 512u >> DENTRY_SIZE_BITS;   /* 16 */
    sbi->num_clusters       = TIMG_NUM_CLUSTERS + EXFAT_RESERVED_CLUSTERS;
    sbi->fat_offset         = TIMG_FAT_OFFSET;
    sbi->fat_length         = 1;
    sbi->clu_offset         = TIMG_CLU_OFFSET;
    sbi->root_dir           = TIMG_ROOT_CLUSTER;
    sbi->num_fats           = 1;
    sbi->part_id            = 0;
}

static void make_root_chain(exfat_chain *chain)
{
    chain->dir   = TIMG_ROOT_CLUSTER;
    chain->size  = 1;                              /* 1 cluster → 16 dentries */
    chain->flags = (uint8_t)ALLOC_NO_FAT_CHAIN;
}

static int as_setup(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();
    return 0;
}

static int as_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

/* Helpers to seed slots. */
static void seed_in_use_at(int slot)
{
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    struct exfat_dentry d;
    memset(&d, 0, sizeof(d));
    d.type = (uint8_t)EXFAT_FILE;        /* 0x85 → bit7 set, in-use */
    assert_int_equal(exfat_set_dentry(&sbi, &dir, slot, &d), 0);
}

static void seed_deleted_at(int slot)
{
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    struct exfat_dentry d;
    memset(&d, 0, sizeof(d));
    d.type = (uint8_t)0x05;              /* deleted file primary (bit7 clear) */
    assert_int_equal(exfat_set_dentry(&sbi, &dir, slot, &d), 0);
}

/* ============================================================
 * Argument validation (8)
 * ============================================================ */

static void test_alloc_null_sbi(void **state)
{
    (void)state;
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(NULL, &dir, 1, &slot), -EINVAL);
    assert_int_equal(slot, -1);
}

static void test_alloc_null_dir(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, NULL, 1, &slot), -EINVAL);
}

static void test_alloc_null_out(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 1, NULL), -EINVAL);
}

static void test_alloc_n_zero(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 0, &slot), -EINVAL);
}

static void test_alloc_n_over_max(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir,
                                             (int)EXFAT_DENTRY_SET_MAX + 1,
                                             &slot),
                     -EINVAL);
}

static void test_alloc_bad_dir_flags(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    dir.flags = 0xFFu;
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 1, &slot), -EINVAL);
}

static void test_alloc_bad_dir_dir(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    dir.dir = 1;  /* < EXFAT_FIRST_CLUSTER */
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 1, &slot), -EINVAL);
}

static void test_alloc_zero_dir_size(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    dir.size = 0;
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 1, &slot), -EINVAL);
}

/* ============================================================
 * Default-image happy paths (4)
 * ============================================================ */

/* slot 2 is the UNUSED terminator → run extends 2..15 (14 slots). */
static void test_alloc_n1_returns_terminator(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 1, &slot), 0);
    assert_int_equal(slot, 2);
}

static void test_alloc_n3_returns_terminator(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 3, &slot), 0);
    assert_int_equal(slot, 2);
}

/* Boundary: 14 slots fit exactly (slots 2..15). */
static void test_alloc_n14_exact_fit(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 14, &slot), 0);
    assert_int_equal(slot, 2);
}

/* Boundary: 15 slots can't fit (only 14 free). */
static void test_alloc_n15_enospc(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 15, &slot), -ENOSPC);
    assert_int_equal(slot, -1);
}

/* ============================================================
 * Layout-sensitive tests using set_dentry seeding (5)
 * ============================================================ */

/* In-use file primary at slot 2 + terminator at slot 3 → run from 3..15 = 13. */
static void test_alloc_after_in_use_prefix(void **state)
{
    (void)state;
    seed_in_use_at(2);  /* slot 2 = EXFAT_FILE in-use */
    /* slot 3 stays UNUSED in the synthetic image (memcpy_s only copied 32B). */

    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 5, &slot), 0);
    assert_int_equal(slot, 3);
}

/* Deleted slot at 2 followed by terminator at 3:
 * run includes slot 2 (deleted, reusable) then extends from terminator → 14. */
static void test_alloc_reuses_deleted_slot(void **state)
{
    (void)state;
    seed_deleted_at(2);  /* slot 2 = type 0x05 (deleted) */

    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 1, &slot), 0);
    assert_int_equal(slot, 2);
}

/* In-use slot breaks an accumulating run.
 *
 * Layout: slot 2 = deleted, slot 3 = in-use, slot 4 = terminator.
 * Asking for n=2 must skip slots 2-3 (run_len=1 reset by in-use at 3) and
 * return 4 (terminator → 12 free slots from there). */
static void test_alloc_in_use_resets_run(void **state)
{
    (void)state;
    seed_deleted_at(2);
    seed_in_use_at(3);
    /* slot 4 still UNUSED from base image. */

    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 2, &slot), 0);
    assert_int_equal(slot, 4);
}

/* n=1 should still find slot 2 even when in-use slot at 3 — single deleted
 * slot at 2 satisfies n=1 immediately. */
static void test_alloc_n1_returns_first_deleted(void **state)
{
    (void)state;
    seed_deleted_at(2);
    seed_in_use_at(3);

    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 1, &slot), 0);
    assert_int_equal(slot, 2);
}

/* Pack the entire root cluster with in-use file primaries → -ENOSPC. */
static void test_alloc_full_dir_enospc(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    /* Fill slots 2..15 with in-use primaries (slots 0,1 already in-use). */
    for (int i = 2; i < 16; i++) {
        struct exfat_dentry d;
        memset(&d, 0, sizeof(d));
        d.type = (uint8_t)EXFAT_FILE;
        assert_int_equal(exfat_set_dentry(&sbi, &dir, i, &d), 0);
    }
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 1, &slot), -ENOSPC);
    assert_int_equal(slot, -1);
}

/* ============================================================
 * Failure passthrough (1)
 * ============================================================ */

static void test_alloc_get_dentry_io_fail(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    mock_disk_set_read_fail_at(1);  /* fail very first read */
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 1, &slot), -EIO);
    assert_int_equal(slot, -1);
}

/* ============================================================
 * Invariants (3)
 * ============================================================ */

/* Read-only: alloc never writes to disk. */
static void test_invariant_no_writes(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    mock_disk_reset_counters();
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 3, &slot), 0);
    assert_int_equal((int)mock_disk_write_count(), 0);
}

/* No-mutate-on-failure: -EINVAL must NOT trigger any IO and must NOT
 * write *slot_idx_out. */
static void test_invariant_einval_no_io(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    mock_disk_reset_counters();
    int slot = 0xCAFE;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 0, &slot), -EINVAL);
    assert_int_equal((int)mock_disk_read_count(), 0);
    assert_int_equal((int)mock_disk_write_count(), 0);
    assert_int_equal(slot, 0xCAFE);  /* untouched */
}

/* Bounded-scan: terminator at slot 2 means at most 3 reads before answer
 * (slots 0, 1 in-use, slot 2 terminator). Tracks Invariant
 * exfat-alloc-slot-bounded + terminator-extends-run early-exit. */
static void test_invariant_terminator_short_circuit(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    mock_disk_reset_counters();
    int slot = -1;
    assert_int_equal(exfat_alloc_dentry_slot(&sbi, &dir, 1, &slot), 0);
    /* Slots 0 (BITMAP), 1 (UPCASE), 2 (UNUSED → terminator). 3 reads, then
     * the terminator path returns immediately without scanning slots 3-15. */
    assert_int_equal((int)mock_disk_read_count(), 3);
}

/* ============================================================
 * Suite table
 * ============================================================ */

const struct CMUnitTest test_alloc_dentry_slot_tests[] = {
    /* arg validation (8) */
    cmocka_unit_test_setup_teardown(test_alloc_null_sbi,                   as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_null_dir,                   as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_null_out,                   as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_n_zero,                     as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_n_over_max,                 as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_bad_dir_flags,              as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_bad_dir_dir,                as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_zero_dir_size,              as_setup, as_teardown),
    /* default-image happy paths (4) */
    cmocka_unit_test_setup_teardown(test_alloc_n1_returns_terminator,      as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_n3_returns_terminator,      as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_n14_exact_fit,              as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_n15_enospc,                 as_setup, as_teardown),
    /* layout-sensitive (5) */
    cmocka_unit_test_setup_teardown(test_alloc_after_in_use_prefix,        as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_reuses_deleted_slot,        as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_in_use_resets_run,          as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_n1_returns_first_deleted,   as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_full_dir_enospc,            as_setup, as_teardown),
    /* failure passthrough (1) */
    cmocka_unit_test_setup_teardown(test_alloc_get_dentry_io_fail,         as_setup, as_teardown),
    /* invariants (3) */
    cmocka_unit_test_setup_teardown(test_invariant_no_writes,              as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_einval_no_io,           as_setup, as_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_terminator_short_circuit, as_setup, as_teardown),
};

const size_t test_alloc_dentry_slot_tests_count =
    sizeof(test_alloc_dentry_slot_tests) / sizeof(test_alloc_dentry_slot_tests[0]);
