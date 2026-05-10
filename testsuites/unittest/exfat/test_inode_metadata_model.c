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
 * test_inode_metadata_model — Layer T coverage for the inode metadata model
 * helpers in fs/exfat/exfat_inode.c.
 *
 * Covers all 11 spec Cases plus timezone decode branches, civil-time sentinel
 * dates, version-singlebump behavior, checksum preservation, and defensive
 * nlink derivation.
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <cmocka.h>

#include "../../../fs/exfat/include/exfat.h"
#include "../../../fs/exfat/include/exfat_raw.h"
#include "securec.h"

#ifdef LOSCFG_FS_EXFAT

/* Public interface under test — production object is linked by Makefile. */
void exfat_encode_atime(const exfat_sb_info *sbi, uint64_t epoch_sec,
                        uint16_t *time_out, uint16_t *date_out,
                        uint8_t *tz_out);
void exfat_encode_mtime(const exfat_sb_info *sbi, uint64_t epoch_sec,
                        uint16_t *time_out, uint16_t *date_out,
                        uint8_t *cs_out, uint8_t *tz_out);
void exfat_encode_ctime(const exfat_sb_info *sbi, uint64_t epoch_sec,
                        uint16_t *time_out, uint16_t *date_out,
                        uint8_t *cs_out, uint8_t *tz_out);
uint64_t exfat_decode_entry_time(const exfat_sb_info *sbi,
                                 uint16_t time_le, uint16_t date_le,
                                 uint8_t cs, uint8_t tz);
uint64_t exfat_now_seconds(void);
uint64_t exfat_truncate_atime_seconds(uint64_t epoch_sec);
void exfat_inode_touch_atime(exfat_inode_info *ei);
void exfat_inode_touch_mtime(exfat_inode_info *ei);
void exfat_inode_touch_ctime(exfat_inode_info *ei);
void exfat_inode_touch_atime_mtime(exfat_inode_info *ei);
void exfat_inode_touch_mtime_ctime(exfat_inode_info *ei);
void exfat_inode_touch_now(exfat_inode_info *ei);
void exfat_inode_bump_version(exfat_inode_info *ei);
int exfat_inode_load_metadata(const exfat_sb_info *sbi, exfat_inode_info *ei,
                              const struct exfat_dentry *file_dentry);
int exfat_inode_store_metadata(const exfat_sb_info *sbi,
                               const exfat_inode_info *ei,
                               struct exfat_dentry *file_dentry);
uint32_t exfat_inode_get_nlink(const exfat_inode_info *ei);

struct epoch_case {
    uint64_t sec;
    const char *name;
};

static const struct epoch_case g_sentinel_dates[] = {
    { 315532800ULL,  "1980-01-01" },
    { 951827696ULL,  "2000-02-29" },
    { 4107542398ULL, "2100-02-28" },
    { 4354819199ULL, "2107-12-31" },
    { 645411724ULL,  "1990-06-15" },
    { 1267419968ULL, "2010-03-01" },
    { 1735686000ULL, "2024-12-31" },
    { 2540548800ULL, "2050-07-04" },
    { 3471292802ULL, "2080-01-01" },
    { 4338858630ULL, "2107-06-30" },
};

static void init_sbi(exfat_sb_info *sbi, int32_t time_offset)
{
    assert_int_equal(memset_s(sbi, sizeof(*sbi), 0, sizeof(*sbi)), EOK);
    sbi->options.time_offset = time_offset;
}

static void init_inode(exfat_inode_info *ei)
{
    assert_int_equal(memset_s(ei, sizeof(*ei), 0, sizeof(*ei)), EOK);
    ei->atime_sec = 11ULL;
    ei->mtime_sec = 22ULL;
    ei->ctime_sec = 33ULL;
    ei->version = 7u;
}

static void init_file_dentry(struct exfat_dentry *d)
{
    assert_int_equal(memset_s(d, sizeof(*d), 0, sizeof(*d)), EOK);
    d->type = EXFAT_FILE;
    d->dentry.file.num_ext = 2u;
    d->dentry.file.checksum = 0xBEEFu;
    d->dentry.file.attr = ATTR_ARCHIVE;
}

static void encode_file_times(const exfat_sb_info *sbi, struct exfat_dentry *d,
                              uint64_t atime, uint64_t mtime, uint64_t ctime)
{
    uint16_t access_time;
    uint16_t access_date;
    uint16_t modify_time;
    uint16_t modify_date;
    uint16_t create_time;
    uint16_t create_date;
    uint8_t access_tz;
    uint8_t modify_cs;
    uint8_t modify_tz;
    uint8_t create_cs;
    uint8_t create_tz;

    exfat_encode_atime(sbi, atime, &access_time, &access_date, &access_tz);
    exfat_encode_mtime(sbi, mtime, &modify_time, &modify_date, &modify_cs, &modify_tz);
    exfat_encode_ctime(sbi, ctime, &create_time, &create_date, &create_cs, &create_tz);

    d->dentry.file.access_time = access_time;
    d->dentry.file.access_date = access_date;
    d->dentry.file.access_tz = access_tz;
    d->dentry.file.modify_time = modify_time;
    d->dentry.file.modify_date = modify_date;
    d->dentry.file.modify_time_cs = modify_cs;
    d->dentry.file.modify_tz = modify_tz;
    d->dentry.file.create_time = create_time;
    d->dentry.file.create_date = create_date;
    d->dentry.file.create_time_cs = create_cs;
    d->dentry.file.create_tz = create_tz;
}

static void assert_between_u64(uint64_t low, uint64_t got, uint64_t high)
{
    assert_true(got >= low);
    assert_true(got <= high);
}

static uint64_t time_before(void)
{
    return (uint64_t)time(NULL);
}

static uint64_t time_after(void)
{
    return (uint64_t)time(NULL);
}

static void test_civil_time_sentinel_roundtrips(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    init_sbi(&sbi, 0);

    for (size_t i = 0; i < sizeof(g_sentinel_dates) / sizeof(g_sentinel_dates[0]); i++) {
        uint16_t t = 0;
        uint16_t d = 0;
        uint8_t cs = 0;
        uint8_t tz = 0;
        uint64_t s = g_sentinel_dates[i].sec;
        (void)g_sentinel_dates[i].name;

        exfat_encode_mtime(&sbi, s, &t, &d, &cs, &tz);
        assert_int_equal(tz, EXFAT_TZ_VALID);
        assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, tz), s);

        exfat_encode_ctime(&sbi, s, &t, &d, &cs, &tz);
        assert_int_equal(tz, EXFAT_TZ_VALID);
        assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, tz), s);
    }
}

static void test_atime_even_and_odd_roundtrip(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    init_sbi(&sbi, 0);
    const uint64_t even = 1735686000ULL;
    const uint64_t odd = even + 1ULL;
    uint16_t t = 0;
    uint16_t d = 0;
    uint8_t tz = 0;

    exfat_encode_atime(&sbi, even, &t, &d, &tz);
    assert_int_equal(tz, EXFAT_TZ_VALID);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, 0u, tz), even);

    exfat_encode_atime(&sbi, odd, &t, &d, &tz);
    assert_int_equal(tz, EXFAT_TZ_VALID);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, 0u, tz), odd - 1ULL);
}

static void test_mtime_ctime_even_and_odd_roundtrip(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    init_sbi(&sbi, 0);
    const uint64_t even = 2540548800ULL;
    const uint64_t odd = even + 1ULL;
    uint16_t t = 0;
    uint16_t d = 0;
    uint8_t cs = 0;
    uint8_t tz = 0;

    exfat_encode_mtime(&sbi, even, &t, &d, &cs, &tz);
    assert_int_equal(cs, 0);
    assert_int_equal(tz, EXFAT_TZ_VALID);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, tz), even);

    exfat_encode_mtime(&sbi, odd, &t, &d, &cs, &tz);
    assert_int_equal(cs, 100);
    assert_int_equal(tz, EXFAT_TZ_VALID);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, tz), odd);

    exfat_encode_ctime(&sbi, odd, &t, &d, &cs, &tz);
    assert_int_equal(cs, 100);
    assert_int_equal(tz, EXFAT_TZ_VALID);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, tz), odd);
}

static void test_out_of_range_clamp_and_tz_emission(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    init_sbi(&sbi, 0);
    uint16_t t = 0;
    uint16_t d = 0;
    uint8_t cs = 0;
    uint8_t tz = 0;

    exfat_encode_mtime(&sbi, 1ULL, &t, &d, &cs, &tz);
    assert_int_equal(tz, EXFAT_TZ_VALID);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, tz),
                     (uint64_t)EXFAT_MIN_TIMESTAMP_SECS);

    exfat_encode_atime(&sbi, 999999999999ULL, &t, &d, &tz);
    assert_int_equal(tz, EXFAT_TZ_VALID);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, 0u, tz),
                     exfat_truncate_atime_seconds((uint64_t)EXFAT_MAX_TIMESTAMP_SECS));
}

static void test_decode_tz_valid_signed_offsets(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    init_sbi(&sbi, 0);
    const uint64_t s = 1735686000ULL;
    uint16_t t = 0;
    uint16_t d = 0;
    uint8_t cs = 0;
    uint8_t tz = 0;

    exfat_encode_mtime(&sbi, s, &t, &d, &cs, &tz);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, EXFAT_TZ_VALID), s);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, EXFAT_TZ_VALID | 4u),
                     s - 3600ULL);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, EXFAT_TZ_VALID | 0x7Cu),
                     s + 3600ULL);
}

static void test_decode_tz_clear_uses_mount_time_offset(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    const uint64_t s = 1735686000ULL;
    uint16_t t = 0;
    uint16_t d = 0;
    uint8_t cs = 0;
    uint8_t tz = 0;

    init_sbi(&sbi, 0);
    exfat_encode_mtime(&sbi, s, &t, &d, &cs, &tz);

    init_sbi(&sbi, 120);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, 0u), s - 7200ULL);

    init_sbi(&sbi, -60);
    assert_int_equal(exfat_decode_entry_time(&sbi, t, d, cs, 0u), s + 3600ULL);
}

static void test_touch_atime_updates_atime_ctime_only(void **state)
{
    (void)state;
    exfat_inode_info ei;
    init_inode(&ei);
    uint64_t old_mtime = ei.mtime_sec;
    uint32_t old_version = ei.version;
    uint64_t t0 = time_before();

    exfat_inode_touch_atime(&ei);
    uint64_t t1 = time_after();

    assert_int_equal(ei.atime_sec & 1ULL, 0);
    assert_between_u64(t0 & ~1ULL, ei.atime_sec, t1);
    assert_between_u64(t0, ei.ctime_sec, t1);
    assert_int_equal(ei.mtime_sec, old_mtime);
    assert_int_equal(ei.version, old_version + 1u);
}

static void test_touch_mtime_updates_mtime_ctime_only(void **state)
{
    (void)state;
    exfat_inode_info ei;
    init_inode(&ei);
    uint64_t old_atime = ei.atime_sec;
    uint32_t old_version = ei.version;
    uint64_t t0 = time_before();

    exfat_inode_touch_mtime(&ei);
    uint64_t t1 = time_after();

    assert_int_equal(ei.atime_sec, old_atime);
    assert_between_u64(t0, ei.mtime_sec, t1);
    assert_between_u64(t0, ei.ctime_sec, t1);
    assert_int_equal(ei.version, old_version + 1u);
}

static void test_touch_ctime_updates_ctime_only(void **state)
{
    (void)state;
    exfat_inode_info ei;
    init_inode(&ei);
    uint64_t old_atime = ei.atime_sec;
    uint64_t old_mtime = ei.mtime_sec;
    uint32_t old_version = ei.version;
    uint64_t t0 = time_before();

    exfat_inode_touch_ctime(&ei);
    uint64_t t1 = time_after();

    assert_int_equal(ei.atime_sec, old_atime);
    assert_int_equal(ei.mtime_sec, old_mtime);
    assert_between_u64(t0, ei.ctime_sec, t1);
    assert_int_equal(ei.version, old_version + 1u);
}

static void test_touch_atime_mtime_leaves_ctime_unchanged(void **state)
{
    (void)state;
    exfat_inode_info ei;
    init_inode(&ei);
    uint64_t old_ctime = ei.ctime_sec;
    uint32_t old_version = ei.version;
    uint64_t t0 = time_before();

    exfat_inode_touch_atime_mtime(&ei);
    uint64_t t1 = time_after();

    assert_int_equal(ei.atime_sec & 1ULL, 0);
    assert_between_u64(t0 & ~1ULL, ei.atime_sec, t1);
    assert_between_u64(t0, ei.mtime_sec, t1);
    assert_int_equal(ei.ctime_sec, old_ctime);
    assert_int_equal(ei.version, old_version + 1u);
}

static void test_touch_mtime_ctime_leaves_atime_unchanged(void **state)
{
    (void)state;
    exfat_inode_info ei;
    init_inode(&ei);
    uint64_t old_atime = ei.atime_sec;
    uint32_t old_version = ei.version;
    uint64_t t0 = time_before();

    exfat_inode_touch_mtime_ctime(&ei);
    uint64_t t1 = time_after();

    assert_int_equal(ei.atime_sec, old_atime);
    assert_between_u64(t0, ei.mtime_sec, t1);
    assert_between_u64(t0, ei.ctime_sec, t1);
    assert_int_equal(ei.version, old_version + 1u);
}

static void test_touch_now_updates_all_three_once(void **state)
{
    (void)state;
    exfat_inode_info ei;
    init_inode(&ei);
    uint32_t old_version = ei.version;
    uint64_t t0 = time_before();

    exfat_inode_touch_now(&ei);
    uint64_t t1 = time_after();

    assert_int_equal(ei.atime_sec & 1ULL, 0);
    assert_between_u64(t0 & ~1ULL, ei.atime_sec, t1);
    assert_between_u64(t0, ei.mtime_sec, t1);
    assert_between_u64(t0, ei.ctime_sec, t1);
    assert_int_equal(ei.version, old_version + 1u);
}

static void test_bump_version_changes_no_timestamps(void **state)
{
    (void)state;
    exfat_inode_info ei;
    init_inode(&ei);
    uint64_t old_atime = ei.atime_sec;
    uint64_t old_mtime = ei.mtime_sec;
    uint64_t old_ctime = ei.ctime_sec;
    uint32_t old_version = ei.version;

    exfat_inode_bump_version(&ei);

    assert_int_equal(ei.atime_sec, old_atime);
    assert_int_equal(ei.mtime_sec, old_mtime);
    assert_int_equal(ei.ctime_sec, old_ctime);
    assert_int_equal(ei.version, old_version + 1u);
}

static void test_version_singlebump_wraparound(void **state)
{
    (void)state;
    exfat_inode_info ei;
    init_inode(&ei);
    ei.version = UINT32_MAX;

    exfat_inode_touch_ctime(&ei);
    assert_int_equal(ei.version, 0u);

    exfat_inode_bump_version(&ei);
    assert_int_equal(ei.version, 1u);
}

static void test_load_metadata_decodes_fields_without_version_change(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    exfat_inode_info ei;
    struct exfat_dentry d;
    init_sbi(&sbi, 0);
    init_inode(&ei);
    init_file_dentry(&d);
    const uint32_t old_version = ei.version;
    const uint64_t atime = 1267419968ULL;
    const uint64_t mtime = 1267419969ULL;
    const uint64_t ctime = 1735686001ULL;

    encode_file_times(&sbi, &d, atime, mtime, ctime);
    assert_int_equal(exfat_inode_load_metadata(&sbi, &ei, &d), 0);

    assert_int_equal(ei.atime_sec, exfat_truncate_atime_seconds(atime));
    assert_int_equal(ei.mtime_sec, mtime);
    assert_int_equal(ei.ctime_sec, ctime);
    assert_int_equal(ei.version, old_version);
}

static void test_store_metadata_writes_fields_and_preserves_checksum(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    exfat_inode_info ei;
    struct exfat_dentry d;
    init_sbi(&sbi, 0);
    init_inode(&ei);
    init_file_dentry(&d);
    ei.atime_sec = 645411725ULL;
    ei.mtime_sec = 951827697ULL;
    ei.ctime_sec = 4338858631ULL;

    assert_int_equal(exfat_inode_store_metadata(&sbi, &ei, &d), 0);

    assert_int_equal(d.dentry.file.checksum, 0xBEEFu);
    assert_int_equal(d.dentry.file.access_tz, EXFAT_TZ_VALID);
    assert_int_equal(d.dentry.file.modify_tz, EXFAT_TZ_VALID);
    assert_int_equal(d.dentry.file.create_tz, EXFAT_TZ_VALID);
    assert_int_equal(d.dentry.file.modify_time_cs, 100);
    assert_int_equal(d.dentry.file.create_time_cs, 100);
    assert_int_equal(exfat_decode_entry_time(&sbi, d.dentry.file.access_time,
        d.dentry.file.access_date, 0u, d.dentry.file.access_tz),
        exfat_truncate_atime_seconds(ei.atime_sec));
    assert_int_equal(exfat_decode_entry_time(&sbi, d.dentry.file.modify_time,
        d.dentry.file.modify_date, d.dentry.file.modify_time_cs,
        d.dentry.file.modify_tz), ei.mtime_sec);
    assert_int_equal(exfat_decode_entry_time(&sbi, d.dentry.file.create_time,
        d.dentry.file.create_date, d.dentry.file.create_time_cs,
        d.dentry.file.create_tz), ei.ctime_sec);
}

static void test_load_store_roundtrip_preserves_dentry_bytes(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    exfat_inode_info ei;
    struct exfat_dentry in;
    struct exfat_dentry out;
    init_sbi(&sbi, 0);
    init_inode(&ei);
    init_file_dentry(&in);
    encode_file_times(&sbi, &in, 3471292802ULL, 4107542399ULL, 4338858631ULL);
    out = in;

    assert_int_equal(exfat_inode_load_metadata(&sbi, &ei, &in), 0);
    assert_int_equal(exfat_inode_store_metadata(&sbi, &ei, &out), 0);

    assert_memory_equal(&out, &in, sizeof(in));
}

static void test_get_nlink_file_and_unknown_return_one(void **state)
{
    (void)state;
    exfat_inode_info ei;
    init_inode(&ei);

    ei.type = TYPE_FILE;
    ei.num_subdirs = 99u;
    assert_int_equal(exfat_inode_get_nlink(&ei), 1u);

    ei.type = TYPE_UNUSED;
    ei.num_subdirs = 99u;
    assert_int_equal(exfat_inode_get_nlink(&ei), 1u);
}

static void test_get_nlink_dir_clamps_and_uses_num_subdirs(void **state)
{
    (void)state;
    exfat_inode_info ei;
    init_inode(&ei);
    ei.type = TYPE_DIR;

    ei.num_subdirs = 2u;
    assert_int_equal(exfat_inode_get_nlink(&ei), 2u);
    ei.num_subdirs = 5u;
    assert_int_equal(exfat_inode_get_nlink(&ei), 5u);
    ei.num_subdirs = 0u;
    assert_int_equal(exfat_inode_get_nlink(&ei), 2u);
}

static void test_truncate_atime_seconds_rounds_down(void **state)
{
    (void)state;

    assert_int_equal(exfat_truncate_atime_seconds(100ULL), 100ULL);
    assert_int_equal(exfat_truncate_atime_seconds(101ULL), 100ULL);
    assert_int_equal(exfat_truncate_atime_seconds((uint64_t)EXFAT_MIN_TIMESTAMP_SECS + 1ULL),
                     (uint64_t)EXFAT_MIN_TIMESTAMP_SECS);
}

static void test_now_seconds_is_in_exfat_range(void **state)
{
    (void)state;
    uint64_t now = exfat_now_seconds();

    assert_true(now >= (uint64_t)EXFAT_MIN_TIMESTAMP_SECS);
    assert_true(now <= (uint64_t)EXFAT_MAX_TIMESTAMP_SECS);
}

/* ======================================================================== */

const struct CMUnitTest test_inode_metadata_model_tests[] = {
    cmocka_unit_test(test_civil_time_sentinel_roundtrips),
    cmocka_unit_test(test_atime_even_and_odd_roundtrip),
    cmocka_unit_test(test_mtime_ctime_even_and_odd_roundtrip),
    cmocka_unit_test(test_out_of_range_clamp_and_tz_emission),
    cmocka_unit_test(test_decode_tz_valid_signed_offsets),
    cmocka_unit_test(test_decode_tz_clear_uses_mount_time_offset),
    cmocka_unit_test(test_touch_atime_updates_atime_ctime_only),
    cmocka_unit_test(test_touch_mtime_updates_mtime_ctime_only),
    cmocka_unit_test(test_touch_ctime_updates_ctime_only),
    cmocka_unit_test(test_touch_atime_mtime_leaves_ctime_unchanged),
    cmocka_unit_test(test_touch_mtime_ctime_leaves_atime_unchanged),
    cmocka_unit_test(test_touch_now_updates_all_three_once),
    cmocka_unit_test(test_bump_version_changes_no_timestamps),
    cmocka_unit_test(test_version_singlebump_wraparound),
    cmocka_unit_test(test_load_metadata_decodes_fields_without_version_change),
    cmocka_unit_test(test_store_metadata_writes_fields_and_preserves_checksum),
    cmocka_unit_test(test_load_store_roundtrip_preserves_dentry_bytes),
    cmocka_unit_test(test_get_nlink_file_and_unknown_return_one),
    cmocka_unit_test(test_get_nlink_dir_clamps_and_uses_num_subdirs),
    cmocka_unit_test(test_truncate_atime_seconds_rounds_down),
    cmocka_unit_test(test_now_seconds_is_in_exfat_range),
};

const size_t test_inode_metadata_model_tests_count =
    sizeof(test_inode_metadata_model_tests) / sizeof(test_inode_metadata_model_tests[0]);

#endif /* LOSCFG_FS_EXFAT */