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
#include <cmocka.h>

#include "exfat.h"
#include "mock_disk.h"

extern int exfat_set_volume_dirty(exfat_sb_info *sbi);
extern int exfat_clear_volume_dirty(exfat_sb_info *sbi);
extern INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);

#define VF_BLOCKSIZE       512u
#define VF_BOOT_LEN        VF_BLOCKSIZE
#define VF_PART_ID         77
#define VF_VOL_FLAGS_OFF   106u
#define VF_NSECTORS        2u
#define VF_IMG_LEN         (VF_NSECTORS * VF_BLOCKSIZE)

static exfat_sb_info *g_sbi;
static uint8_t       *g_boot_buf;
static uint8_t        g_image[VF_IMG_LEN];

static void vf_image_init(uint16_t initial_vol_flags)
{
    memset(g_image, 0u, sizeof(g_image));
    g_image[510] = 0x55u;
    g_image[511] = 0xAAu;
    g_image[VF_VOL_FLAGS_OFF + 0u] = (uint8_t)(initial_vol_flags & 0xFFu);
    g_image[VF_VOL_FLAGS_OFF + 1u] = (uint8_t)((initial_vol_flags >> 8) & 0xFFu);
}

static int vol_flags_setup(void **state)
{
    (void)state;
    vf_image_init(0u);
    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    g_sbi = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_boot_buf = (uint8_t *)calloc(1u, VF_BOOT_LEN);
    assert_non_null(g_boot_buf);
    g_boot_buf[VF_VOL_FLAGS_OFF + 0u] = 0u;
    g_boot_buf[VF_VOL_FLAGS_OFF + 1u] = 0u;
    /* mirror image signature so test_invariant_boot_buf_patched_before_write can
     * verify post-write that bytes outside [VF_VOL_FLAGS_OFF..+1] survive
     * (boot_buf IS what gets written, not the original image). */
    g_boot_buf[510]                   = 0x55u;
    g_boot_buf[511]                   = 0xAAu;

    g_sbi->boot_buf             = g_boot_buf;
    g_sbi->blocksize            = VF_BLOCKSIZE;
    g_sbi->part_id              = VF_PART_ID;
    g_sbi->vol_flags            = 0u;
    g_sbi->vol_flags_persistent = 0u;
    return 0;
}

static int vol_flags_teardown(void **state)
{
    (void)state;
    free(g_sbi);
    g_sbi = NULL;
    free(g_boot_buf);
    g_boot_buf = NULL;
    mock_disk_unload();
    return 0;
}

static uint8_t *read_boot_sector_from_mock(void)
{
    uint8_t *buf = (uint8_t *)calloc(1u, VF_BLOCKSIZE);
    assert_non_null(buf);
    INT32 rc = los_part_read((INT32)VF_PART_ID, buf, 0u, 1u, 1);
    assert_int_equal(rc, 0);
    return buf;
}

static void test_set_dirty_transition_writes_boot_sector(void **state)
{
    (void)state;
    int rc = exfat_set_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->vol_flags, (unsigned int)VOLUME_DIRTY);
    assert_int_equal((unsigned int)g_boot_buf[VF_VOL_FLAGS_OFF + 0u],
                     (unsigned int)(VOLUME_DIRTY & 0xFFu));
    assert_int_equal((unsigned int)g_boot_buf[VF_VOL_FLAGS_OFF + 1u],
                     (unsigned int)((VOLUME_DIRTY >> 8) & 0xFFu));
    assert_int_equal((unsigned long)mock_disk_write_count(), 1ul);
}

static void test_clear_dirty_transition_writes_boot_sector(void **state)
{
    (void)state;
    g_sbi->vol_flags                  = VOLUME_DIRTY;
    g_boot_buf[VF_VOL_FLAGS_OFF + 0u] = (uint8_t)(VOLUME_DIRTY & 0xFFu);
    g_boot_buf[VF_VOL_FLAGS_OFF + 1u] = (uint8_t)((VOLUME_DIRTY >> 8) & 0xFFu);
    mock_disk_reset_counters();

    int rc = exfat_clear_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
    assert_int_equal((unsigned int)g_boot_buf[VF_VOL_FLAGS_OFF + 0u], 0u);
    assert_int_equal((unsigned int)g_boot_buf[VF_VOL_FLAGS_OFF + 1u], 0u);
    assert_int_equal((unsigned long)mock_disk_write_count(), 1ul);
}

static void test_set_dirty_already_dirty_noop(void **state)
{
    (void)state;
    g_sbi->vol_flags = VOLUME_DIRTY;
    mock_disk_reset_counters();

    int rc = exfat_set_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->vol_flags, (unsigned int)VOLUME_DIRTY);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_clear_dirty_already_clean_noop(void **state)
{
    (void)state;
    mock_disk_reset_counters();

    int rc = exfat_clear_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_set_dirty_null_sbi_einval(void **state)
{
    (void)state;
    int rc = exfat_set_volume_dirty(NULL);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_clear_dirty_null_sbi_einval(void **state)
{
    (void)state;
    int rc = exfat_clear_volume_dirty(NULL);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_set_dirty_null_boot_buf_einval(void **state)
{
    (void)state;
    g_sbi->boot_buf = NULL;
    int rc = exfat_set_volume_dirty(g_sbi);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_set_dirty_zero_blocksize_einval(void **state)
{
    (void)state;
    g_sbi->blocksize = 0u;
    int rc = exfat_set_volume_dirty(g_sbi);
    assert_int_equal(rc, -EINVAL);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
    assert_int_equal((unsigned long)mock_disk_write_count(), 0ul);
}

static void test_set_dirty_part_write_fail_returns_eio(void **state)
{
    (void)state;
    mock_disk_set_write_fail_at(1u);
    int rc = exfat_set_volume_dirty(g_sbi);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned int)g_sbi->vol_flags, (unsigned int)VOLUME_DIRTY);
    assert_int_equal((unsigned int)g_boot_buf[VF_VOL_FLAGS_OFF + 0u],
                     (unsigned int)(VOLUME_DIRTY & 0xFFu));
}

static void test_clear_dirty_part_write_fail_returns_eio(void **state)
{
    (void)state;
    g_sbi->vol_flags                  = VOLUME_DIRTY;
    g_boot_buf[VF_VOL_FLAGS_OFF + 0u] = (uint8_t)(VOLUME_DIRTY & 0xFFu);
    g_boot_buf[VF_VOL_FLAGS_OFF + 1u] = (uint8_t)((VOLUME_DIRTY >> 8) & 0xFFu);
    mock_disk_reset_counters();
    mock_disk_set_write_fail_at(1u);

    int rc = exfat_clear_volume_dirty(g_sbi);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0u);
    assert_int_equal((unsigned int)g_boot_buf[VF_VOL_FLAGS_OFF + 0u], 0u);
}

static void test_invariant_no_realloc(void **state)
{
    (void)state;
    uint8_t *boot_before = g_sbi->boot_buf;
    int rc = exfat_set_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);
    assert_ptr_equal(g_sbi->boot_buf, boot_before);
}

static void test_invariant_persistent_merged(void **state)
{
    (void)state;
    g_sbi->vol_flags                  = (uint16_t)(VOLUME_DIRTY | MEDIA_FAILURE);
    g_sbi->vol_flags_persistent       = MEDIA_FAILURE;
    g_boot_buf[VF_VOL_FLAGS_OFF + 0u] = (uint8_t)(g_sbi->vol_flags & 0xFFu);
    g_boot_buf[VF_VOL_FLAGS_OFF + 1u] = (uint8_t)((g_sbi->vol_flags >> 8) & 0xFFu);

    int rc = exfat_clear_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->vol_flags, (unsigned int)MEDIA_FAILURE);
    assert_int_equal((unsigned int)(g_boot_buf[VF_VOL_FLAGS_OFF + 0u] |
                                    ((unsigned int)g_boot_buf[VF_VOL_FLAGS_OFF + 1u] << 8)),
                     (unsigned int)MEDIA_FAILURE);
}

static void test_invariant_le16_on_disk(void **state)
{
    (void)state;
    g_sbi->vol_flags_persistent = 0x4321u;
    int rc = exfat_set_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)g_sbi->vol_flags, 0x4323u);
    assert_int_equal((unsigned int)g_boot_buf[VF_VOL_FLAGS_OFF + 0u], 0x23u);
    assert_int_equal((unsigned int)g_boot_buf[VF_VOL_FLAGS_OFF + 1u], 0x43u);
}

static void test_invariant_no_lock_acquire(void **state)
{
    (void)state;
    int rc = exfat_set_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);
}

static void test_invariant_single_sector_write(void **state)
{
    (void)state;
    int rc = exfat_set_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 1ul);
    assert_int_equal((unsigned long)mock_disk_read_count(), 0ul);
}

static void test_invariant_eager_write_clear_path(void **state)
{
    (void)state;
    g_sbi->vol_flags                  = VOLUME_DIRTY;
    g_boot_buf[VF_VOL_FLAGS_OFF + 0u] = (uint8_t)(VOLUME_DIRTY & 0xFFu);
    g_boot_buf[VF_VOL_FLAGS_OFF + 1u] = (uint8_t)((VOLUME_DIRTY >> 8) & 0xFFu);
    mock_disk_reset_counters();

    int rc = exfat_clear_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned long)mock_disk_write_count(), 1ul);
}

static void test_invariant_boot_buf_patched_before_write(void **state)
{
    (void)state;
    int rc = exfat_set_volume_dirty(g_sbi);
    assert_int_equal(rc, 0);

    uint8_t *snap = read_boot_sector_from_mock();
    assert_int_equal((unsigned int)snap[VF_VOL_FLAGS_OFF + 0u],
                     (unsigned int)(VOLUME_DIRTY & 0xFFu));
    assert_int_equal((unsigned int)snap[VF_VOL_FLAGS_OFF + 1u],
                     (unsigned int)((VOLUME_DIRTY >> 8) & 0xFFu));
    assert_int_equal((unsigned int)snap[510], 0x55u);
    assert_int_equal((unsigned int)snap[511], 0xAAu);
    free(snap);
}

const struct CMUnitTest test_vol_flags_tests[] = {
    cmocka_unit_test_setup_teardown(test_set_dirty_transition_writes_boot_sector,   vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_clear_dirty_transition_writes_boot_sector, vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_set_dirty_already_dirty_noop,              vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_clear_dirty_already_clean_noop,            vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_set_dirty_null_sbi_einval,                 vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_clear_dirty_null_sbi_einval,               vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_set_dirty_null_boot_buf_einval,            vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_set_dirty_zero_blocksize_einval,           vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_set_dirty_part_write_fail_returns_eio,     vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_clear_dirty_part_write_fail_returns_eio,   vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_no_realloc,                      vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_persistent_merged,               vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_le16_on_disk,                    vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_no_lock_acquire,                 vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_single_sector_write,             vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_eager_write_clear_path,          vol_flags_setup, vol_flags_teardown),
    cmocka_unit_test_setup_teardown(test_invariant_boot_buf_patched_before_write,   vol_flags_setup, vol_flags_teardown),
};

const size_t test_vol_flags_tests_count =
    sizeof(test_vol_flags_tests) / sizeof(test_vol_flags_tests[0]);
