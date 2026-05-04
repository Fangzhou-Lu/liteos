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
 * test_dentry_set_write — exfat_set_dentry / exfat_set_dentry_set
 * (Wave B Stage 4a).
 *
 * Strategy: build the standard 6 KiB synthetic image, write a brand-new
 * dentry into root cluster slot 2 (UNUSED terminator slot), then read it
 * back via exfat_get_dentry and compare byte-for-byte. Negative paths
 * use mock_disk_set_{read,write}_fail_at + parameter sweeps.
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

/* Forward decls for SUT. */
int exfat_set_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                     int entry_idx, const struct exfat_dentry *in);
int exfat_set_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
                         int start_entry, const struct exfat_dentry *set,
                         int num_entries);

/* Read counterpart for verification. */
int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                     int entry_idx, struct exfat_dentry *out,
                     uint64_t *out_sector);

/* ---- geometry helper (mirror of test_dentry_iter.c) --------------------- */

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

static void make_root_chain(exfat_chain *chain)
{
    chain->dir   = TIMG_ROOT_CLUSTER;
    chain->size  = 1;
    chain->flags = (uint8_t)ALLOC_NO_FAT_CHAIN;
}

/* ---- setup / teardown --------------------------------------------------- */

static int sw_setup(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();
    return 0;
}

static int sw_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

/* ---- helpers ------------------------------------------------------------ */

/* Build a synthetic 32-byte payload — type EXFAT_FILE primary, deterministic
 * fields. We only care about byte-exact write/read round-trip; semantic
 * meaning of fields is irrelevant at this layer. */
static void build_payload_file(struct exfat_dentry *out, uint8_t marker)
{
    memset(out, 0, sizeof(*out));
    out->type = (uint8_t)EXFAT_FILE;
    out->dentry.file.num_ext  = 2;
    out->dentry.file.checksum = (uint16_t)((uint16_t)marker << 8 | marker);
    /* Fill remaining bytes with marker so we can detect partial writes. */
    uint8_t *raw = (uint8_t *)out;
    for (int i = 4; i < (int)DENTRY_SIZE; i++) {
        raw[i] = (uint8_t)(marker + (uint8_t)i);
    }
}

/* Compare two raw 32-byte dentry buffers byte-for-byte. */
static int dentry_bytes_equal(const struct exfat_dentry *a,
                              const struct exfat_dentry *b)
{
    return memcmp(a, b, DENTRY_SIZE) == 0;
}

/* ============================================================
 * exfat_set_dentry — argument validation
 * ============================================================ */

static void test_set_null_sbi(void **state)
{
    (void)state;
    exfat_chain dir; make_root_chain(&dir);
    struct exfat_dentry d; build_payload_file(&d, 0x55);
    assert_int_equal(exfat_set_dentry(NULL, &dir, 0, &d), -EINVAL);
}

static void test_set_null_dir(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    struct exfat_dentry d; build_payload_file(&d, 0x55);
    assert_int_equal(exfat_set_dentry(&sbi, NULL, 0, &d), -EINVAL);
}

static void test_set_null_in(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    assert_int_equal(exfat_set_dentry(&sbi, &dir, 0, NULL), -EINVAL);
}

static void test_set_negative_entry_idx(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    struct exfat_dentry d; build_payload_file(&d, 0x55);
    assert_int_equal(exfat_set_dentry(&sbi, &dir, -1, &d), -EINVAL);
}

static void test_set_invalid_dir_flags(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    dir.flags = 0xFFu;  /* not ALLOC_FAT_CHAIN nor ALLOC_NO_FAT_CHAIN */
    struct exfat_dentry d; build_payload_file(&d, 0x55);
    assert_int_equal(exfat_set_dentry(&sbi, &dir, 0, &d), -EINVAL);
}

static void test_set_zero_cluster_size(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    sbi.cluster_size = 0;
    exfat_chain dir; make_root_chain(&dir);
    struct exfat_dentry d; build_payload_file(&d, 0x55);
    assert_int_equal(exfat_set_dentry(&sbi, &dir, 0, &d), -EINVAL);
}

static void test_set_dir_below_first_cluster(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    dir.dir = 1;  /* < EXFAT_FIRST_CLUSTER */
    struct exfat_dentry d; build_payload_file(&d, 0x55);
    assert_int_equal(exfat_set_dentry(&sbi, &dir, 0, &d), -EINVAL);
}

/* ============================================================
 * exfat_set_dentry — happy path
 * ============================================================ */

/* Write a payload to slot 2 (the UNUSED terminator), read it back, expect
 * byte-for-byte equality. Slot 0 (BITMAP) and 1 (UPCASE) untouched. */
static void test_set_round_trip_no_fat_chain(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    struct exfat_dentry payload; build_payload_file(&payload, 0xA5);

    assert_int_equal(exfat_set_dentry(&sbi, &dir, 2, &payload), 0);

    struct exfat_dentry readback;
    memset(&readback, 0, sizeof(readback));
    assert_int_equal(exfat_get_dentry(&sbi, &dir, 2, &readback, NULL), 0);
    assert_true(dentry_bytes_equal(&payload, &readback));
}

/* Slot 0 and slot 1 (BITMAP / UPCASE) must remain unchanged after the
 * Stage-4a write touches only slot 2. */
static void test_set_does_not_clobber_neighbours(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    /* Snapshot slot 0 and 1 before the write. */
    struct exfat_dentry slot0_before, slot1_before;
    assert_int_equal(exfat_get_dentry(&sbi, &dir, 0, &slot0_before, NULL), 0);
    assert_int_equal(exfat_get_dentry(&sbi, &dir, 1, &slot1_before, NULL), 0);

    struct exfat_dentry payload; build_payload_file(&payload, 0x42);
    assert_int_equal(exfat_set_dentry(&sbi, &dir, 2, &payload), 0);

    struct exfat_dentry slot0_after, slot1_after;
    assert_int_equal(exfat_get_dentry(&sbi, &dir, 0, &slot0_after, NULL), 0);
    assert_int_equal(exfat_get_dentry(&sbi, &dir, 1, &slot1_after, NULL), 0);

    assert_true(dentry_bytes_equal(&slot0_before, &slot0_after));
    assert_true(dentry_bytes_equal(&slot1_before, &slot1_after));
}

/* The implementation must NOT silently rewrite the SetChecksum field of
 * the primary — caller is the chksum authority. We pre-compute a deliberate
 * non-canonical checksum and verify exfat_set_dentry preserves it byte-exact
 * (Invariant exfat-set-dentry-no-chksum-touch). */
static void test_set_preserves_caller_chksum(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    struct exfat_dentry payload;
    memset(&payload, 0, sizeof(payload));
    payload.type = (uint8_t)EXFAT_FILE;
    payload.dentry.file.num_ext  = 0;
    payload.dentry.file.checksum = 0xBEEF;
    /* Fill bytes 4..31 with a known pattern. */
    uint8_t *raw = (uint8_t *)&payload;
    for (int i = 4; i < (int)DENTRY_SIZE; i++) {
        raw[i] = (uint8_t)i;
    }

    assert_int_equal(exfat_set_dentry(&sbi, &dir, 2, &payload), 0);

    struct exfat_dentry readback;
    memset(&readback, 0xFF, sizeof(readback));
    assert_int_equal(exfat_get_dentry(&sbi, &dir, 2, &readback, NULL), 0);

    assert_int_equal(readback.dentry.file.checksum, 0xBEEFu);
    assert_true(dentry_bytes_equal(&payload, &readback));
}

/* ============================================================
 * exfat_set_dentry — failure paths
 * ============================================================ */

/* Out-of-range entry_idx in NO_FAT_CHAIN mode must -EIO before any IO. */
static void test_set_no_fat_chain_oor_entry(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    /* dir.size = 1 cluster, so any entry past dentries_per_clu is OOR. */
    exfat_chain dir; make_root_chain(&dir);
    /* Force dir.dir + clu_offset to land at exactly num_clusters → OOR. */
    dir.dir = (uint32_t)(sbi.num_clusters - 1u);
    /* clu_offset = (entry_idx*32)/512 = 1 when entry_idx = 16 */
    int entry_idx = (int)(sbi.cluster_size / DENTRY_SIZE);  /* 16 */
    struct exfat_dentry d; build_payload_file(&d, 0x33);
    assert_int_equal(exfat_set_dentry(&sbi, &dir, entry_idx, &d), -EIO);
}

/* Read failure during the read-modify-write cycle. */
static void test_set_read_fail_eio(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    mock_disk_set_read_fail_at(1);  /* fail the very first read */
    struct exfat_dentry d; build_payload_file(&d, 0x77);
    assert_int_equal(exfat_set_dentry(&sbi, &dir, 0, &d), -EIO);
    /* Crucially: write must not have been triggered after read failure. */
    assert_int_equal((int)mock_disk_write_count(), 0);
}

/* Write failure path — read succeeds, write fails. */
static void test_set_write_fail_eio(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    mock_disk_set_write_fail_at(1);
    struct exfat_dentry d; build_payload_file(&d, 0x88);
    assert_int_equal(exfat_set_dentry(&sbi, &dir, 0, &d), -EIO);
}

/* ============================================================
 * exfat_set_dentry_set — argument validation
 * ============================================================ */

static void test_set_set_null_sbi(void **state)
{
    (void)state;
    exfat_chain dir; make_root_chain(&dir);
    struct exfat_dentry s[1]; build_payload_file(&s[0], 0x11);
    assert_int_equal(exfat_set_dentry_set(NULL, &dir, 0, s, 1), -EINVAL);
}

static void test_set_set_null_set(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    assert_int_equal(exfat_set_dentry_set(&sbi, &dir, 0, NULL, 1), -EINVAL);
}

static void test_set_set_zero_count(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    struct exfat_dentry s[1]; build_payload_file(&s[0], 0x11);
    assert_int_equal(exfat_set_dentry_set(&sbi, &dir, 0, s, 0), -EINVAL);
}

static void test_set_set_count_over_max(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);
    struct exfat_dentry s[1]; build_payload_file(&s[0], 0x11);
    assert_int_equal(exfat_set_dentry_set(&sbi, &dir, 0,
                                          s, (int)EXFAT_DENTRY_SET_MAX + 1),
                     -EINVAL);
}

/* ============================================================
 * exfat_set_dentry_set — happy + partial-failure paths
 * ============================================================ */

/* Write a 3-dentry set (file/stream/name) starting at slot 2; readback each
 * via exfat_get_dentry. Round-trip equality on all three. */
static void test_set_set_round_trip_3(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    struct exfat_dentry s[3];
    build_payload_file(&s[0], 0xA0);
    build_payload_file(&s[1], 0xB0);
    build_payload_file(&s[2], 0xC0);

    assert_int_equal(exfat_set_dentry_set(&sbi, &dir, 2, s, 3), 0);

    for (int i = 0; i < 3; i++) {
        struct exfat_dentry rb;
        memset(&rb, 0, sizeof(rb));
        assert_int_equal(exfat_get_dentry(&sbi, &dir, 2 + i, &rb, NULL), 0);
        assert_true(dentry_bytes_equal(&s[i], &rb));
    }
}

/* Partial failure: write_fail_at(2) → first set_dentry succeeds, second's
 * write fails. Must return -EIO; first dentry already on disk (no rollback,
 * per Invariant exfat-set-dentry-set-no-rollback). */
static void test_set_set_partial_failure_no_rollback(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    /* The 2nd write call (i.e., 2nd set_dentry's write step) fails. */
    mock_disk_set_write_fail_at(2);

    struct exfat_dentry s[3];
    build_payload_file(&s[0], 0xA0);
    build_payload_file(&s[1], 0xB0);
    build_payload_file(&s[2], 0xC0);

    int rc = exfat_set_dentry_set(&sbi, &dir, 2, s, 3);
    assert_int_equal(rc, -EIO);

    /* Slot 2 (first set_dentry) should be on disk. */
    struct exfat_dentry rb0;
    assert_int_equal(exfat_get_dentry(&sbi, &dir, 2, &rb0, NULL), 0);
    assert_true(dentry_bytes_equal(&s[0], &rb0));
    /* Slot 3 (second set_dentry) is undefined — could be original UNUSED
     * data — but importantly NOT s[1]. We don't assert specific content. */
}

/* ============================================================
 * Invariants
 * ============================================================ */

/* IO bound: each successful set_dentry triggers exactly one read and one
 * write (Invariant exfat-set-dentry-bounded-per-call). */
static void test_invariant_bounded_io(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    mock_disk_reset_counters();
    struct exfat_dentry d; build_payload_file(&d, 0x99);
    assert_int_equal(exfat_set_dentry(&sbi, &dir, 0, &d), 0);

    assert_int_equal((int)mock_disk_read_count(), 1);
    assert_int_equal((int)mock_disk_write_count(), 1);
}

/* IO bound for set: N successful set_dentry → N reads + N writes. */
static void test_invariant_bounded_io_set(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    mock_disk_reset_counters();
    struct exfat_dentry s[3];
    build_payload_file(&s[0], 0xAA);
    build_payload_file(&s[1], 0xBB);
    build_payload_file(&s[2], 0xCC);
    assert_int_equal(exfat_set_dentry_set(&sbi, &dir, 2, s, 3), 0);

    assert_int_equal((int)mock_disk_read_count(), 3);
    assert_int_equal((int)mock_disk_write_count(), 3);
}

/* No-mutate-on-arg-failure: -EINVAL must NOT trigger any IO. */
static void test_invariant_einval_no_io(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_sbi(&sbi);
    exfat_chain dir; make_root_chain(&dir);

    mock_disk_reset_counters();
    /* Trigger -EINVAL via NULL `in`. */
    assert_int_equal(exfat_set_dentry(&sbi, &dir, 0, NULL), -EINVAL);
    assert_int_equal((int)mock_disk_read_count(), 0);
    assert_int_equal((int)mock_disk_write_count(), 0);
}

/* ============================================================
 * Suite table
 * ============================================================ */

const struct CMUnitTest test_dentry_set_write_tests[] = {
    /* exfat_set_dentry — arg validation (7) */
    cmocka_unit_test_setup_teardown(test_set_null_sbi,                  sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_null_dir,                  sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_null_in,                   sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_negative_entry_idx,        sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_invalid_dir_flags,         sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_zero_cluster_size,         sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_dir_below_first_cluster,   sw_setup, sw_teardown),
    /* exfat_set_dentry — happy path (3) */
    cmocka_unit_test_setup_teardown(test_set_round_trip_no_fat_chain,   sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_does_not_clobber_neighbours, sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_preserves_caller_chksum,   sw_setup, sw_teardown),
    /* exfat_set_dentry — failure paths (3) */
    cmocka_unit_test_setup_teardown(test_set_no_fat_chain_oor_entry,    sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_read_fail_eio,             sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_write_fail_eio,            sw_setup, sw_teardown),
    /* exfat_set_dentry_set — arg validation (4) */
    cmocka_unit_test_setup_teardown(test_set_set_null_sbi,              sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_set_null_set,              sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_set_zero_count,            sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_set_count_over_max,        sw_setup, sw_teardown),
    /* exfat_set_dentry_set — happy + partial failure (2) */
    cmocka_unit_test_setup_teardown(test_set_set_round_trip_3,          sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_set_set_partial_failure_no_rollback, sw_setup, sw_teardown),
    /* invariants (3) */
    cmocka_unit_test_setup_teardown(test_invariant_bounded_io,          sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_bounded_io_set,      sw_setup, sw_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_einval_no_io,        sw_setup, sw_teardown),
};

const size_t test_dentry_set_write_tests_count =
    sizeof(test_dentry_set_write_tests) / sizeof(test_dentry_set_write_tests[0]);
