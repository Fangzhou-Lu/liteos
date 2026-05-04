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

extern const struct CMUnitTest test_chksum_tests[];        extern const size_t test_chksum_tests_count;
extern const struct CMUnitTest test_options_tests[];       extern const size_t test_options_tests_count;
extern const struct CMUnitTest test_dentry_tests[];        extern const size_t test_dentry_tests_count;
extern const struct CMUnitTest test_balloc_tests[];        extern const size_t test_balloc_tests_count;
extern const struct CMUnitTest test_upcase_tests[];        extern const size_t test_upcase_tests_count;
extern const struct CMUnitTest test_fat_chain_tests[];     extern const size_t test_fat_chain_tests_count;
extern const struct CMUnitTest test_nls_utf16_tests[];     extern const size_t test_nls_utf16_tests_count;
extern const struct CMUnitTest test_dentry_iter_tests[];   extern const size_t test_dentry_iter_tests_count;
extern const struct CMUnitTest test_inode_alloc_tests[];   extern const size_t test_inode_alloc_tests_count;
extern const struct CMUnitTest test_open_close_tests[];    extern const size_t test_open_close_tests_count;
extern const struct CMUnitTest test_getattr_seek_tests[];  extern const size_t test_getattr_seek_tests_count;
extern const struct CMUnitTest test_read_tests[];          extern const size_t test_read_tests_count;
extern const struct CMUnitTest test_readdir_tests[];       extern const size_t test_readdir_tests_count;
extern const struct CMUnitTest test_lookup_tests[];        extern const size_t test_lookup_tests_count;

extern const struct CMUnitTest test_write_tests[];        extern const size_t test_write_tests_count;
extern const struct CMUnitTest test_vol_flags_tests[];        extern const size_t test_vol_flags_tests_count;
extern const struct CMUnitTest test_free_cluster_tests[];        extern const size_t test_free_cluster_tests_count;
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
    total += run_suite("chksum",       test_chksum_tests,       test_chksum_tests_count);
    total += run_suite("options",      test_options_tests,      test_options_tests_count);
    total += run_suite("dentry",       test_dentry_tests,       test_dentry_tests_count);
    total += run_suite("balloc",       test_balloc_tests,       test_balloc_tests_count);
    total += run_suite("upcase",       test_upcase_tests,       test_upcase_tests_count);
    total += run_suite("fat_chain",    test_fat_chain_tests,    test_fat_chain_tests_count);
    total += run_suite("nls_utf16",    test_nls_utf16_tests,    test_nls_utf16_tests_count);
    total += run_suite("dentry_iter",  test_dentry_iter_tests,  test_dentry_iter_tests_count);
    total += run_suite("inode_alloc",  test_inode_alloc_tests,  test_inode_alloc_tests_count);
    total += run_suite("open_close",   test_open_close_tests,   test_open_close_tests_count);
    total += run_suite("getattr_seek", test_getattr_seek_tests, test_getattr_seek_tests_count);
    total += run_suite("read",         test_read_tests,         test_read_tests_count);
    total += run_suite("readdir",      test_readdir_tests,      test_readdir_tests_count);
    total += run_suite("lookup",       test_lookup_tests,       test_lookup_tests_count);

    total += run_suite("write", test_write_tests, test_write_tests_count);
    total += run_suite("vol_flags", test_vol_flags_tests, test_vol_flags_tests_count);
    total += run_suite("free_cluster", test_free_cluster_tests, test_free_cluster_tests_count);
    fprintf(stderr, "\n=== exfat TOTAL FAILURES: %d ===\n", total);
    return total == 0 ? 0 : 1;
}
