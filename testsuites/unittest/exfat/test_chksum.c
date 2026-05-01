/*
 * test_chksum — pure-function tests for fs/exfat/util/exfat_chksum.c.
 * Validates spec invariants:
 *   exfat-chksum-purity, exfat-chksum-byte-order-independence,
 *   exfat-chksum-not-iee-crc, exfat-chksum-skip-positions-fixed.
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#include <cmocka.h>

uint32_t exfat_calc_chksum32(const void *data, uint32_t len, uint32_t chksum, int type);
uint16_t exfat_calc_chksum16(const void *data, int len, uint16_t chksum, int type);

#define CS_BOOT_SECTOR  1
#define CS_DEFAULT      2
#define CS_DIR_ENTRY    0

/* Reference implementation — independently re-derived from the Microsoft spec
 * to catch a regression that mirrors the production code's bug. */
static uint32_t ref32(const uint8_t *p, uint32_t len, uint32_t cs, int type)
{
    for (uint32_t i = 0; i < len; i++) {
        if (type == CS_BOOT_SECTOR && (i == 106 || i == 107 || i == 112)) continue;
        cs = ((cs << 31) | (cs >> 1)) + p[i];
    }
    return cs;
}

static uint16_t ref16(const uint8_t *p, int len, uint16_t cs, int type)
{
    for (int i = 0; i < len; i++) {
        if (type == CS_DIR_ENTRY && (i == 2 || i == 3)) continue;
        cs = (uint16_t)(((cs << 15) | (cs >> 1)) + p[i]);
    }
    return cs;
}

static void test_chksum32_zero_len(void **state)
{
    (void)state;
    uint8_t buf[4] = {1,2,3,4};
    assert_int_equal(exfat_calc_chksum32(buf, 0, 0xCAFEu, CS_DEFAULT), 0xCAFEu);
    assert_int_equal(exfat_calc_chksum32(buf, 0, 0u,      CS_BOOT_SECTOR), 0u);
}

static void test_chksum32_default_no_skip(void **state)
{
    (void)state;
    uint8_t buf[16];
    for (int i = 0; i < 16; i++) buf[i] = (uint8_t)(i * 7 + 1);
    assert_int_equal(
        exfat_calc_chksum32(buf, 16, 0, CS_DEFAULT),
        ref32(buf, 16, 0, CS_DEFAULT));
}

static void test_chksum32_boot_sector_skips_106_107_112(void **state)
{
    (void)state;
    /* Build a 200-byte buffer where bytes 106/107/112 differ between two
     * inputs; CS_BOOT_SECTOR must give same chksum (those bytes skipped). */
    uint8_t a[200], b[200];
    for (int i = 0; i < 200; i++) { a[i] = (uint8_t)i; b[i] = (uint8_t)i; }
    b[106] = 0xFF; b[107] = 0xAA; b[112] = 0x55;

    uint32_t ca = exfat_calc_chksum32(a, 200, 0, CS_BOOT_SECTOR);
    uint32_t cb = exfat_calc_chksum32(b, 200, 0, CS_BOOT_SECTOR);
    assert_int_equal(ca, cb);

    /* And under CS_DEFAULT they MUST differ — invariant proves skipping was real. */
    uint32_t da = exfat_calc_chksum32(a, 200, 0, CS_DEFAULT);
    uint32_t db = exfat_calc_chksum32(b, 200, 0, CS_DEFAULT);
    assert_int_not_equal(da, db);
}

static void test_chksum32_running_sum_matches_concat(void **state)
{
    (void)state;
    uint8_t buf[64];
    for (int i = 0; i < 64; i++) buf[i] = (uint8_t)(i ^ 0x5A);
    uint32_t one_shot = exfat_calc_chksum32(buf, 64, 0, CS_DEFAULT);
    uint32_t a = exfat_calc_chksum32(buf, 32, 0, CS_DEFAULT);
    uint32_t b = exfat_calc_chksum32(buf + 32, 32, a, CS_DEFAULT);
    assert_int_equal(one_shot, b);
}

static void test_chksum16_zero_len(void **state)
{
    (void)state;
    uint8_t buf[4] = {1,2,3,4};
    assert_int_equal(exfat_calc_chksum16(buf, 0, 0xBEEFu, CS_DEFAULT), 0xBEEFu);
}

static void test_chksum16_dir_entry_skips_2_3(void **state)
{
    (void)state;
    uint8_t a[64], b[64];
    for (int i = 0; i < 64; i++) { a[i] = (uint8_t)(i + 1); b[i] = (uint8_t)(i + 1); }
    b[2] = 0xFF; b[3] = 0xAA;

    assert_int_equal(
        exfat_calc_chksum16(a, 64, 0, CS_DIR_ENTRY),
        exfat_calc_chksum16(b, 64, 0, CS_DIR_ENTRY));
    assert_int_not_equal(
        exfat_calc_chksum16(a, 64, 0, CS_DEFAULT),
        exfat_calc_chksum16(b, 64, 0, CS_DEFAULT));
}

static void test_chksum_purity(void **state)
{
    (void)state;
    /* Determinism — same input twice gives same output. */
    uint8_t buf[100];
    for (int i = 0; i < 100; i++) buf[i] = (uint8_t)(i * 13 + 2);
    uint32_t r1 = exfat_calc_chksum32(buf, 100, 0xDEADu, CS_DEFAULT);
    uint32_t r2 = exfat_calc_chksum32(buf, 100, 0xDEADu, CS_DEFAULT);
    assert_int_equal(r1, r2);
}

static void test_chksum_matches_reference_on_random_blob(void **state)
{
    (void)state;
    /* Deterministic LCG — no rand() to keep test reproducible. */
    uint32_t seed = 0xC0FFEE01u;
    uint8_t buf[512];
    for (int i = 0; i < 512; i++) {
        seed = seed * 1664525u + 1013904223u;
        buf[i] = (uint8_t)(seed >> 24);
    }
    assert_int_equal(
        exfat_calc_chksum32(buf, 512, 0, CS_DEFAULT),
        ref32(buf, 512, 0, CS_DEFAULT));
    assert_int_equal(
        exfat_calc_chksum32(buf, 512, 0, CS_BOOT_SECTOR),
        ref32(buf, 512, 0, CS_BOOT_SECTOR));
    assert_int_equal(
        exfat_calc_chksum16(buf, 512, 0, CS_DIR_ENTRY),
        ref16(buf, 512, 0, CS_DIR_ENTRY));
}

const struct CMUnitTest test_chksum_tests[] = {
    cmocka_unit_test(test_chksum32_zero_len),
    cmocka_unit_test(test_chksum32_default_no_skip),
    cmocka_unit_test(test_chksum32_boot_sector_skips_106_107_112),
    cmocka_unit_test(test_chksum32_running_sum_matches_concat),
    cmocka_unit_test(test_chksum16_zero_len),
    cmocka_unit_test(test_chksum16_dir_entry_skips_2_3),
    cmocka_unit_test(test_chksum_purity),
    cmocka_unit_test(test_chksum_matches_reference_on_random_blob),
};

const size_t test_chksum_tests_count =
    sizeof(test_chksum_tests) / sizeof(test_chksum_tests[0]);
