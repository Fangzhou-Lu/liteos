/*
 * test_nls_utf16 — exfat_uni_to_utf8 / exfat_utf8_to_uni / exfat_uniname_cmp.
 *
 * Pure compute (no mock_disk). For uniname_cmp, build a 65536-entry upcase
 * table on the heap that is identity except 'a'..'z' → 'A'..'Z' (ASCII fold)
 * — sufficient to exercise the case-insensitive path. Surrogate halves bypass
 * the table per Invariant exfat-nls-utf16-surrogate-pair-bit-exact.
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <cmocka.h>

#include "../../../fs/exfat/include/exfat.h"
#include "../../../fs/exfat/include/exfat_raw.h"

int exfat_uni_to_utf8(const uint16_t *uni, int uni_len,
                      char *out, int out_max);
int exfat_utf8_to_uni(const char *utf8, int utf8_len,
                      uint16_t *uni, int uni_max, int *uni_len_out);
int exfat_uniname_cmp(const exfat_sb_info *sbi,
                      const uint16_t *a, int a_len,
                      const uint16_t *b, int b_len);

/* ============================================================================
 * exfat_uni_to_utf8
 * ========================================================================== */

static void test_uni2utf8_ascii(void **state)
{
    (void)state;
    uint16_t uni[] = { 'h', 'i', '!' };
    char out[8] = { 0 };
    int n = exfat_uni_to_utf8(uni, 3, out, sizeof(out));
    assert_int_equal(n, 3);
    assert_memory_equal(out, "hi!", 3);
}

static void test_uni2utf8_two_byte(void **state)
{
    (void)state;
    /* U+00A9 © → 0xC2 0xA9 */
    uint16_t uni[] = { 0x00A9 };
    char out[8] = { 0 };
    int n = exfat_uni_to_utf8(uni, 1, out, sizeof(out));
    assert_int_equal(n, 2);
    assert_int_equal((uint8_t)out[0], 0xC2);
    assert_int_equal((uint8_t)out[1], 0xA9);
}

static void test_uni2utf8_three_byte(void **state)
{
    (void)state;
    /* U+4E2D 中 → 0xE4 0xB8 0xAD */
    uint16_t uni[] = { 0x4E2D };
    char out[8] = { 0 };
    int n = exfat_uni_to_utf8(uni, 1, out, sizeof(out));
    assert_int_equal(n, 3);
    assert_int_equal((uint8_t)out[0], 0xE4);
    assert_int_equal((uint8_t)out[1], 0xB8);
    assert_int_equal((uint8_t)out[2], 0xAD);
}

static void test_uni2utf8_surrogate_pair_supplementary(void **state)
{
    (void)state;
    /* U+1F600 😀 → high D83D + low DE00 → UTF-8 0xF0 0x9F 0x98 0x80 */
    uint16_t uni[] = { 0xD83D, 0xDE00 };
    char out[8] = { 0 };
    int n = exfat_uni_to_utf8(uni, 2, out, sizeof(out));
    assert_int_equal(n, 4);
    assert_int_equal((uint8_t)out[0], 0xF0);
    assert_int_equal((uint8_t)out[1], 0x9F);
    assert_int_equal((uint8_t)out[2], 0x98);
    assert_int_equal((uint8_t)out[3], 0x80);
}

static void test_uni2utf8_nul_passthrough(void **state)
{
    (void)state;
    /* NUL emitted as single 0x00 byte (Invariant exfat-nls-utf16-no-modified-utf8). */
    uint16_t uni[] = { 'a', 0x0000, 'b' };
    char out[8] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };
    int n = exfat_uni_to_utf8(uni, 3, out, sizeof(out));
    assert_int_equal(n, 3);
    assert_int_equal((uint8_t)out[0], 'a');
    assert_int_equal((uint8_t)out[1], 0x00);
    assert_int_equal((uint8_t)out[2], 'b');
}

static void test_uni2utf8_lone_high_surrogate(void **state)
{
    (void)state;
    uint16_t uni[] = { 0xD83D };
    char out[8] = { 0 };
    assert_int_equal(exfat_uni_to_utf8(uni, 1, out, sizeof(out)), -EINVAL);
}

static void test_uni2utf8_lone_low_surrogate(void **state)
{
    (void)state;
    uint16_t uni[] = { 0xDE00 };
    char out[8] = { 0 };
    assert_int_equal(exfat_uni_to_utf8(uni, 1, out, sizeof(out)), -EINVAL);
}

static void test_uni2utf8_high_not_followed_by_low(void **state)
{
    (void)state;
    /* high surrogate followed by ASCII (not low) → -EINVAL */
    uint16_t uni[] = { 0xD83D, 'A' };
    char out[8] = { 0 };
    assert_int_equal(exfat_uni_to_utf8(uni, 2, out, sizeof(out)), -EINVAL);
}

static void test_uni2utf8_buffer_overflow(void **state)
{
    (void)state;
    /* '中' is 3 UTF-8 bytes; out_max=2 → -ENAMETOOLONG */
    uint16_t uni[] = { 0x4E2D };
    char out[2] = { 0 };
    assert_int_equal(exfat_uni_to_utf8(uni, 1, out, sizeof(out)), -ENAMETOOLONG);
}

static void test_uni2utf8_null_out(void **state)
{
    (void)state;
    uint16_t uni[] = { 'a' };
    assert_int_equal(exfat_uni_to_utf8(uni, 1, NULL, 8), -EINVAL);
}

static void test_uni2utf8_negative_uni_len(void **state)
{
    (void)state;
    char out[8] = { 0 };
    assert_int_equal(exfat_uni_to_utf8(NULL, -1, out, sizeof(out)), -EINVAL);
}

static void test_uni2utf8_uni_len_too_large(void **state)
{
    (void)state;
    char out[8] = { 0 };
    assert_int_equal(exfat_uni_to_utf8(NULL, EXFAT_MAX_NAME_LEN + 1,
                                       out, sizeof(out)), -EINVAL);
}

static void test_uni2utf8_null_uni_with_zero_len(void **state)
{
    (void)state;
    /* uni=NULL OK if uni_len==0; out still requires size ≥ 1. */
    char out[1] = { 'X' };
    int n = exfat_uni_to_utf8(NULL, 0, out, sizeof(out));
    assert_int_equal(n, 0);
}

/* ============================================================================
 * exfat_utf8_to_uni
 * ========================================================================== */

static void test_utf82uni_ascii(void **state)
{
    (void)state;
    uint16_t uni[8] = { 0 };
    int u_len = -1;
    int r = exfat_utf8_to_uni("hi!", 3, uni, 8, &u_len);
    assert_int_equal(r, 0);
    assert_int_equal(u_len, 3);
    assert_int_equal(uni[0], 'h');
    assert_int_equal(uni[1], 'i');
    assert_int_equal(uni[2], '!');
}

static void test_utf82uni_two_byte(void **state)
{
    (void)state;
    /* © U+00A9: 0xC2 0xA9 */
    char in[] = { (char)0xC2, (char)0xA9 };
    uint16_t uni[8] = { 0 };
    int u_len = -1;
    int r = exfat_utf8_to_uni(in, 2, uni, 8, &u_len);
    assert_int_equal(r, 0);
    assert_int_equal(u_len, 1);
    assert_int_equal(uni[0], 0x00A9);
}

static void test_utf82uni_three_byte(void **state)
{
    (void)state;
    /* 中 U+4E2D: 0xE4 0xB8 0xAD */
    char in[] = { (char)0xE4, (char)0xB8, (char)0xAD };
    uint16_t uni[8] = { 0 };
    int u_len = -1;
    int r = exfat_utf8_to_uni(in, 3, uni, 8, &u_len);
    assert_int_equal(r, 0);
    assert_int_equal(u_len, 1);
    assert_int_equal(uni[0], 0x4E2D);
}

static void test_utf82uni_four_byte_surrogate_pair(void **state)
{
    (void)state;
    /* 😀 U+1F600: 0xF0 0x9F 0x98 0x80 → high D83D + low DE00 */
    char in[] = { (char)0xF0, (char)0x9F, (char)0x98, (char)0x80 };
    uint16_t uni[8] = { 0 };
    int u_len = -1;
    int r = exfat_utf8_to_uni(in, 4, uni, 8, &u_len);
    assert_int_equal(r, 0);
    assert_int_equal(u_len, 2);
    assert_int_equal(uni[0], 0xD83D);
    assert_int_equal(uni[1], 0xDE00);
}

static void test_utf82uni_overlong_two_byte(void **state)
{
    (void)state;
    /* 0xC0 0x80 = overlong NUL → reject (RFC 3629). */
    char in[] = { (char)0xC0, (char)0x80 };
    uint16_t uni[8] = { 0 };
    int u_len;
    assert_int_equal(exfat_utf8_to_uni(in, 2, uni, 8, &u_len), -EINVAL);
}

static void test_utf82uni_overlong_three_byte(void **state)
{
    (void)state;
    /* 0xE0 0x80 0x80 = overlong NUL via 3-byte form → reject. */
    char in[] = { (char)0xE0, (char)0x80, (char)0x80 };
    uint16_t uni[8] = { 0 };
    int u_len;
    assert_int_equal(exfat_utf8_to_uni(in, 3, uni, 8, &u_len), -EINVAL);
}

static void test_utf82uni_above_unicode_max(void **state)
{
    (void)state;
    /* 0xF4 0x90 0x80 0x80 = U+110000, just above U+10FFFF → reject. */
    char in[] = { (char)0xF4, (char)0x90, (char)0x80, (char)0x80 };
    uint16_t uni[8] = { 0 };
    int u_len;
    assert_int_equal(exfat_utf8_to_uni(in, 4, uni, 8, &u_len), -EINVAL);
}

static void test_utf82uni_raw_surrogate_codepoint(void **state)
{
    (void)state;
    /* 0xED 0xA0 0x80 = U+D800 (high surrogate as raw codepoint) → reject. */
    char in[] = { (char)0xED, (char)0xA0, (char)0x80 };
    uint16_t uni[8] = { 0 };
    int u_len;
    assert_int_equal(exfat_utf8_to_uni(in, 3, uni, 8, &u_len), -EINVAL);
}

static void test_utf82uni_truncated(void **state)
{
    (void)state;
    /* 0xE4 0xB8 — middle byte missing of 3-byte form → reject. */
    char in[] = { (char)0xE4, (char)0xB8 };
    uint16_t uni[8] = { 0 };
    int u_len;
    assert_int_equal(exfat_utf8_to_uni(in, 2, uni, 8, &u_len), -EINVAL);
}

static void test_utf82uni_malformed_continuation(void **state)
{
    (void)state;
    /* 0xC2 0x40 — continuation byte missing 0b10xxxxxx prefix → reject. */
    char in[] = { (char)0xC2, (char)0x40 };
    uint16_t uni[8] = { 0 };
    int u_len;
    assert_int_equal(exfat_utf8_to_uni(in, 2, uni, 8, &u_len), -EINVAL);
}

static void test_utf82uni_illegal_start_byte(void **state)
{
    (void)state;
    /* 0xFF is not a valid UTF-8 start byte. */
    char in[] = { (char)0xFF };
    uint16_t uni[8] = { 0 };
    int u_len;
    assert_int_equal(exfat_utf8_to_uni(in, 1, uni, 8, &u_len), -EINVAL);
}

static void test_utf82uni_uni_buffer_overflow(void **state)
{
    (void)state;
    /* Two ASCII chars but uni_max=1 → ENAMETOOLONG on second. */
    uint16_t uni[1] = { 0 };
    int u_len;
    assert_int_equal(exfat_utf8_to_uni("ab", 2, uni, 1, &u_len), -ENAMETOOLONG);
}

static void test_utf82uni_supplementary_buffer_overflow(void **state)
{
    (void)state;
    /* 4-byte UTF-8 emits 2 uint16; uni_max=1 → ENAMETOOLONG. */
    char in[] = { (char)0xF0, (char)0x9F, (char)0x98, (char)0x80 };
    uint16_t uni[1] = { 0 };
    int u_len;
    assert_int_equal(exfat_utf8_to_uni(in, 4, uni, 1, &u_len), -ENAMETOOLONG);
}

static void test_utf82uni_null_uni(void **state)
{
    (void)state;
    int u_len;
    assert_int_equal(exfat_utf8_to_uni("a", 1, NULL, 8, &u_len), -EINVAL);
}

static void test_utf82uni_null_uni_len_out(void **state)
{
    (void)state;
    uint16_t uni[8] = { 0 };
    assert_int_equal(exfat_utf8_to_uni("a", 1, uni, 8, NULL), -EINVAL);
}

static void test_utf82uni_negative_utf8_len(void **state)
{
    (void)state;
    uint16_t uni[8] = { 0 };
    int u_len;
    assert_int_equal(exfat_utf8_to_uni("a", -1, uni, 8, &u_len), -EINVAL);
}

static void test_utf82uni_empty_input(void **state)
{
    (void)state;
    uint16_t uni[8] = { 0xFFFF };
    int u_len = -1;
    int r = exfat_utf8_to_uni(NULL, 0, uni, 8, &u_len);
    assert_int_equal(r, 0);
    assert_int_equal(u_len, 0);
}

/* ============================================================================
 * exfat_uniname_cmp (case-insensitive via upcase table + prefix rule)
 * ========================================================================== */

typedef struct {
    exfat_sb_info sbi;
    uint16_t     *upcase;     /* heap-allocated 65536 entries */
} cmp_ctx;

static int cmp_setup(void **state)
{
    cmp_ctx *c = (cmp_ctx *)calloc(1, sizeof(*c));
    c->upcase = (uint16_t *)calloc(65536, sizeof(uint16_t));
    /* Identity except 'a'..'z' → 'A'..'Z'. */
    for (int i = 0; i < 65536; i++) {
        c->upcase[i] = (uint16_t)i;
    }
    for (int i = 'a'; i <= 'z'; i++) {
        c->upcase[i] = (uint16_t)(i - 'a' + 'A');
    }
    c->sbi.vol_utbl = c->upcase;
    *state = c;
    return 0;
}

static int cmp_teardown(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    free(c->upcase);
    free(c);
    return 0;
}

static void test_cmp_equal_same_case(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    uint16_t a[] = { 'a', 'b', 'c' };
    uint16_t b[] = { 'a', 'b', 'c' };
    assert_int_equal(exfat_uniname_cmp(&c->sbi, a, 3, b, 3), 0);
}

static void test_cmp_equal_different_case(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    uint16_t a[] = { 'a', 'B', 'c' };
    uint16_t b[] = { 'A', 'b', 'C' };
    assert_int_equal(exfat_uniname_cmp(&c->sbi, a, 3, b, 3), 0);
}

static void test_cmp_a_less_than_b(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    uint16_t a[] = { 'a', 'a' };
    uint16_t b[] = { 'a', 'b' };
    assert_int_equal(exfat_uniname_cmp(&c->sbi, a, 2, b, 2), -1);
}

static void test_cmp_a_greater_than_b(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    uint16_t a[] = { 'b' };
    uint16_t b[] = { 'A' };
    /* upcase('b') = 'B' = 0x42, upcase('A') = 'A' = 0x41 → a > b */
    assert_int_equal(exfat_uniname_cmp(&c->sbi, a, 1, b, 1), 1);
}

static void test_cmp_prefix_shorter_is_less(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    uint16_t a[] = { 'f', 'o', 'o' };
    uint16_t b[] = { 'f', 'o', 'o', 'd' };
    assert_int_equal(exfat_uniname_cmp(&c->sbi, a, 3, b, 4), -1);
}

static void test_cmp_prefix_longer_is_greater(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    uint16_t a[] = { 'f', 'o', 'o', 'd' };
    uint16_t b[] = { 'f', 'o', 'o' };
    assert_int_equal(exfat_uniname_cmp(&c->sbi, a, 4, b, 3), 1);
}

static void test_cmp_empty_vs_nonempty(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    uint16_t b[] = { 'x' };
    assert_int_equal(exfat_uniname_cmp(&c->sbi, NULL, 0, b, 1), -1);
}

static void test_cmp_both_empty(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    assert_int_equal(exfat_uniname_cmp(&c->sbi, NULL, 0, NULL, 0), 0);
}

static void test_cmp_surrogate_halves_bypass_upcase(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    /* High surrogate values 0xD83D vs 0xD83E — must compare as raw u16, not
     * via upcase table (Invariant exfat-nls-utf16-surrogate-pair-bit-exact).
     * Our identity upcase happens to preserve this; the assertion mainly
     * documents the contract for future upcase tables that *do* fold here. */
    uint16_t a[] = { 0xD83D };
    uint16_t b[] = { 0xD83E };
    assert_int_equal(exfat_uniname_cmp(&c->sbi, a, 1, b, 1), -1);
}

static void test_cmp_null_sbi(void **state)
{
    (void)state;
    uint16_t a[] = { 'a' };
    uint16_t b[] = { 'a' };
    assert_int_equal(exfat_uniname_cmp(NULL, a, 1, b, 1), -EINVAL);
}

static void test_cmp_null_upcase_table(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    memset(&sbi, 0, sizeof(sbi));
    sbi.vol_utbl = NULL;
    uint16_t a[] = { 'a' };
    uint16_t b[] = { 'a' };
    assert_int_equal(exfat_uniname_cmp(&sbi, a, 1, b, 1), -EINVAL);
}

static void test_cmp_negative_lengths(void **state)
{
    cmp_ctx *c = (cmp_ctx *)*state;
    uint16_t a[] = { 'a' };
    uint16_t b[] = { 'a' };
    assert_int_equal(exfat_uniname_cmp(&c->sbi, a, -1, b, 1), -EINVAL);
    assert_int_equal(exfat_uniname_cmp(&c->sbi, a, 1, b, -1), -EINVAL);
}

const struct CMUnitTest test_nls_utf16_tests[] = {
    /* uni_to_utf8 */
    cmocka_unit_test(test_uni2utf8_ascii),
    cmocka_unit_test(test_uni2utf8_two_byte),
    cmocka_unit_test(test_uni2utf8_three_byte),
    cmocka_unit_test(test_uni2utf8_surrogate_pair_supplementary),
    cmocka_unit_test(test_uni2utf8_nul_passthrough),
    cmocka_unit_test(test_uni2utf8_lone_high_surrogate),
    cmocka_unit_test(test_uni2utf8_lone_low_surrogate),
    cmocka_unit_test(test_uni2utf8_high_not_followed_by_low),
    cmocka_unit_test(test_uni2utf8_buffer_overflow),
    cmocka_unit_test(test_uni2utf8_null_out),
    cmocka_unit_test(test_uni2utf8_negative_uni_len),
    cmocka_unit_test(test_uni2utf8_uni_len_too_large),
    cmocka_unit_test(test_uni2utf8_null_uni_with_zero_len),
    /* utf8_to_uni */
    cmocka_unit_test(test_utf82uni_ascii),
    cmocka_unit_test(test_utf82uni_two_byte),
    cmocka_unit_test(test_utf82uni_three_byte),
    cmocka_unit_test(test_utf82uni_four_byte_surrogate_pair),
    cmocka_unit_test(test_utf82uni_overlong_two_byte),
    cmocka_unit_test(test_utf82uni_overlong_three_byte),
    cmocka_unit_test(test_utf82uni_above_unicode_max),
    cmocka_unit_test(test_utf82uni_raw_surrogate_codepoint),
    cmocka_unit_test(test_utf82uni_truncated),
    cmocka_unit_test(test_utf82uni_malformed_continuation),
    cmocka_unit_test(test_utf82uni_illegal_start_byte),
    cmocka_unit_test(test_utf82uni_uni_buffer_overflow),
    cmocka_unit_test(test_utf82uni_supplementary_buffer_overflow),
    cmocka_unit_test(test_utf82uni_null_uni),
    cmocka_unit_test(test_utf82uni_null_uni_len_out),
    cmocka_unit_test(test_utf82uni_negative_utf8_len),
    cmocka_unit_test(test_utf82uni_empty_input),
    /* uniname_cmp */
    cmocka_unit_test_setup_teardown(test_cmp_equal_same_case,         cmp_setup, cmp_teardown),
    cmocka_unit_test_setup_teardown(test_cmp_equal_different_case,    cmp_setup, cmp_teardown),
    cmocka_unit_test_setup_teardown(test_cmp_a_less_than_b,           cmp_setup, cmp_teardown),
    cmocka_unit_test_setup_teardown(test_cmp_a_greater_than_b,        cmp_setup, cmp_teardown),
    cmocka_unit_test_setup_teardown(test_cmp_prefix_shorter_is_less,  cmp_setup, cmp_teardown),
    cmocka_unit_test_setup_teardown(test_cmp_prefix_longer_is_greater,cmp_setup, cmp_teardown),
    cmocka_unit_test_setup_teardown(test_cmp_empty_vs_nonempty,       cmp_setup, cmp_teardown),
    cmocka_unit_test_setup_teardown(test_cmp_both_empty,              cmp_setup, cmp_teardown),
    cmocka_unit_test_setup_teardown(test_cmp_surrogate_halves_bypass_upcase, cmp_setup, cmp_teardown),
    cmocka_unit_test(test_cmp_null_sbi),
    cmocka_unit_test(test_cmp_null_upcase_table),
    cmocka_unit_test_setup_teardown(test_cmp_negative_lengths,        cmp_setup, cmp_teardown),
};

const size_t test_nls_utf16_tests_count =
    sizeof(test_nls_utf16_tests) / sizeof(test_nls_utf16_tests[0]);
