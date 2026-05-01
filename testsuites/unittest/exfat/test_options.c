/*
 * test_options — exhaustive cases for fs/exfat/util/exfat_options.c.
 * Validates spec Cases 1..5 + 5 invariants.
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <cmocka.h>

#include "../../../fs/exfat/include/exfat.h"   /* exfat_mount_options + EXFAT_ERRORS_* */

int exfat_parse_options(const char *data, exfat_mount_options *opts);

static void zero_opts(exfat_mount_options *o) { memset(o, 0, sizeof(*o)); }

static void test_null_data_returns_zero(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options(NULL, &o), 0);
}

static void test_empty_data_returns_zero(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("", &o), 0);
}

static void test_uid_gid(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("uid=1000,gid=2000", &o), 0);
    assert_int_equal(o.fs_uid, 1000u);
    assert_int_equal(o.fs_gid, 2000u);
}

static void test_uid_overflow_eranges(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("uid=99999999999", &o), -ERANGE);
}

static void test_uid_non_digit_einval(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("uid=abc", &o), -EINVAL);
}

static void test_umask_sets_both_masks(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("umask=022", &o), 0);
    assert_int_equal(o.fs_fmask, 022);
    assert_int_equal(o.fs_dmask, 022);
}

static void test_umask_overflow_eranges(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("umask=07777", &o), -ERANGE);
}

static void test_fmask_dmask_separately(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    o.fs_fmask = 0; o.fs_dmask = 0;
    assert_int_equal(exfat_parse_options("fmask=0700,dmask=0750", &o), 0);
    assert_int_equal(o.fs_fmask, 0700);
    assert_int_equal(o.fs_dmask, 0750);
}

static void test_iocharset_utf8(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("iocharset=utf8", &o), 0);
    assert_non_null(o.iocharset);
    assert_int_equal(o.utf8, 1);
}

static void test_iocharset_non_utf8_einval(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("iocharset=gbk", &o), -EINVAL);
}

static void test_errors_enum(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("errors=continue", &o), 0);
    assert_int_equal(o.errors, EXFAT_ERRORS_CONT);
    assert_int_equal(exfat_parse_options("errors=panic", &o), 0);
    assert_int_equal(o.errors, EXFAT_ERRORS_PANIC);
    assert_int_equal(exfat_parse_options("errors=remount-ro", &o), 0);
    assert_int_equal(o.errors, EXFAT_ERRORS_RO);
}

static void test_errors_unknown_einval(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("errors=blowup", &o), -EINVAL);
}

static void test_discard_switch(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("discard", &o), 0);
    assert_int_equal(o.discard, 1);
}

static void test_discard_with_value_einval(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("discard=1", &o), -EINVAL);
}

static void test_time_offset_range(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("time_offset=120", &o), 0);
    assert_int_equal(o.time_offset, 120);

    assert_int_equal(exfat_parse_options("time_offset=-1440", &o), 0);
    assert_int_equal(o.time_offset, -1440);

    assert_int_equal(exfat_parse_options("time_offset=1441", &o), -ERANGE);
    assert_int_equal(exfat_parse_options("time_offset=-1441", &o), -ERANGE);
}

static void test_unknown_key_einval(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    assert_int_equal(exfat_parse_options("foo=bar", &o), -EINVAL);
    /* Linux's deprecated keys MUST also be rejected per Invariant strict-key-rejection */
    assert_int_equal(exfat_parse_options("debug", &o), -EINVAL);
    assert_int_equal(exfat_parse_options("namecase=0", &o), -EINVAL);
    assert_int_equal(exfat_parse_options("codepage=437", &o), -EINVAL);
}

static void test_combined_options(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    o.fs_fmask = 0022; o.fs_dmask = 0022;   /* defaults */
    int r = exfat_parse_options(
        "uid=500,gid=500,iocharset=utf8,errors=continue,fmask=0077,"
        "dmask=0077,discard,time_offset=60,allow_utime=0022", &o);
    assert_int_equal(r, 0);
    assert_int_equal(o.fs_uid, 500u);
    assert_int_equal(o.fs_gid, 500u);
    assert_int_equal(o.utf8, 1);
    assert_int_equal(o.errors, EXFAT_ERRORS_CONT);
    assert_int_equal(o.fs_fmask, 0077);
    assert_int_equal(o.fs_dmask, 0077);
    assert_int_equal(o.discard, 1);
    assert_int_equal(o.time_offset, 60);
    assert_int_equal(o.allow_utime, 0022);
}

static void test_defaults_preserved_for_absent_keys(void **state)
{
    (void)state;
    exfat_mount_options o; zero_opts(&o);
    o.fs_uid = 4242u; o.fs_gid = 8484u; o.fs_fmask = 0700; o.fs_dmask = 0700;
    /* Only set uid; gid/fmask/dmask must keep caller's defaults. */
    assert_int_equal(exfat_parse_options("uid=1", &o), 0);
    assert_int_equal(o.fs_uid, 1u);
    assert_int_equal(o.fs_gid, 8484u);
    assert_int_equal(o.fs_fmask, 0700);
    assert_int_equal(o.fs_dmask, 0700);
}

const struct CMUnitTest test_options_tests[] = {
    cmocka_unit_test(test_null_data_returns_zero),
    cmocka_unit_test(test_empty_data_returns_zero),
    cmocka_unit_test(test_uid_gid),
    cmocka_unit_test(test_uid_overflow_eranges),
    cmocka_unit_test(test_uid_non_digit_einval),
    cmocka_unit_test(test_umask_sets_both_masks),
    cmocka_unit_test(test_umask_overflow_eranges),
    cmocka_unit_test(test_fmask_dmask_separately),
    cmocka_unit_test(test_iocharset_utf8),
    cmocka_unit_test(test_iocharset_non_utf8_einval),
    cmocka_unit_test(test_errors_enum),
    cmocka_unit_test(test_errors_unknown_einval),
    cmocka_unit_test(test_discard_switch),
    cmocka_unit_test(test_discard_with_value_einval),
    cmocka_unit_test(test_time_offset_range),
    cmocka_unit_test(test_unknown_key_einval),
    cmocka_unit_test(test_combined_options),
    cmocka_unit_test(test_defaults_preserved_for_absent_keys),
};

const size_t test_options_tests_count =
    sizeof(test_options_tests) / sizeof(test_options_tests[0]);
