/*
 * main — cmocka driver. Runs all 5 suites in deterministic order; group
 * setup is mock_disk_unload (defensive — every test owns its load/unload).
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <cmocka.h>

#include "mock_disk.h"

extern const struct CMUnitTest test_chksum_tests[];   extern const size_t test_chksum_tests_count;
extern const struct CMUnitTest test_options_tests[];  extern const size_t test_options_tests_count;
extern const struct CMUnitTest test_dentry_tests[];   extern const size_t test_dentry_tests_count;
extern const struct CMUnitTest test_balloc_tests[];   extern const size_t test_balloc_tests_count;
extern const struct CMUnitTest test_upcase_tests[];   extern const size_t test_upcase_tests_count;

static int run_suite(const char *name,
                     const struct CMUnitTest *tests, size_t n)
{
    fprintf(stderr, "\n=== suite: %s (%zu tests) ===\n", name, n);
    int rc = _cmocka_run_group_tests(name, tests, n, NULL, NULL);
    if (rc != 0) {
        fprintf(stderr, "*** suite %s FAILED rc=%d\n", name, rc);
    }
    return rc;
}

int main(void)
{
    int total = 0;
    total += run_suite("chksum",  test_chksum_tests,  test_chksum_tests_count);
    total += run_suite("options", test_options_tests, test_options_tests_count);
    total += run_suite("dentry",  test_dentry_tests,  test_dentry_tests_count);
    total += run_suite("balloc",  test_balloc_tests,  test_balloc_tests_count);
    total += run_suite("upcase",  test_upcase_tests,  test_upcase_tests_count);

    fprintf(stderr, "\n=== exfat TOTAL FAILURES: %d ===\n", total);
    return total == 0 ? 0 : 1;
}
