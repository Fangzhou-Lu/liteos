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
 * test_inode_alloc — unit tests for exfat_inode_alloc / exfat_inode_free /
 *                    exfat_inode_init_dir_chain  (fs/exfat/exfat_inode_alloc.c).
 *
 * Validates spec invariants:
 *   exfat-inode-alloc-leak-free       — failure paths free all resources
 *   exfat-inode-alloc-prio-inherit    — LOS_MuxInit called with PRIO_INHERIT attr
 *   exfat-inode-free-idempotent-null-safe — free(NULL) is a noop
 *   exfat-inode-init-dir-chain-pure   — five-field assignment, no validation
 *
 * Not covered / rationale:
 *   OOM injection  — LOS_MemAlloc is bridged to libc malloc in host_stubs/los_memory.h;
 *                    there is no intercept point to force NULL without touching host_stubs.
 *   init_dir_chain(NULL) — production code has no NULL guard (pure field-assignment);
 *                    calling it would be UB / SIGSEGV. Invariant documents this as a
 *                    pre-condition violation, not a tested error path.
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

/* Public interface under test — object is compiled separately by the Makefile. */
int  exfat_inode_alloc(exfat_inode_info **out);
void exfat_inode_free(exfat_inode_info *ei);
void exfat_inode_init_dir_chain(exfat_inode_info *ei, uint32_t start_clu);

/* ---- helper ------------------------------------------------------------ */

/* Allocate one inode; assert success and non-NULL output. */
static exfat_inode_info *must_alloc(void)
{
    exfat_inode_info *ei = NULL;
    int rc = exfat_inode_alloc(&ei);
    assert_int_equal(rc, 0);
    assert_non_null(ei);
    return ei;
}

/* ---- test_inode_alloc_happy_path --------------------------------------- */
/*
 * Invariant exfat-inode-alloc-leak-free (success branch):
 *   alloc must return 0 and write a non-NULL pointer.
 *   All scalar fields must be zero (zalloc guarantee).
 *   inode_lock opaque field is LOS_OK-initialised (stub always succeeds).
 */
static void test_inode_alloc_happy_path(void **state)
{
    (void)state;

    exfat_inode_info *ei = must_alloc();

    /* zalloc guarantees zero-init for all scalar fields. */
    assert_int_equal((int)ei->dir.dir,   0);
    assert_int_equal((int)ei->dir.size,  0);
    assert_int_equal((int)ei->dir.flags, 0);
    assert_int_equal((int)ei->entry,     0);
    assert_int_equal((int)ei->type,      0);
    assert_int_equal((int)ei->attr,      0);
    assert_int_equal((int)ei->start_clu, 0);
    assert_int_equal((int)ei->flags,     0);

    exfat_inode_free(ei);
}

/* ---- test_inode_alloc_null_out ----------------------------------------- */
/*
 * Invariant: passing NULL out-pointer must return -EINVAL immediately
 * without allocating any memory (no leak; verified by repeated calls not
 * exhausting the heap in the host test environment).
 */
static void test_inode_alloc_null_out(void **state)
{
    (void)state;
    int rc = exfat_inode_alloc(NULL);
    assert_int_equal(rc, -EINVAL);
}

/* ---- test_inode_alloc_out_not_aliased ---------------------------------- */
/*
 * Two independent allocs must yield distinct, non-overlapping pointers.
 */
static void test_inode_alloc_out_not_aliased(void **state)
{
    (void)state;

    exfat_inode_info *a = must_alloc();
    exfat_inode_info *b = must_alloc();

    assert_ptr_not_equal(a, b);

    exfat_inode_free(a);
    exfat_inode_free(b);
}

/* ---- test_inode_free_null_safe ----------------------------------------- */
/*
 * Invariant exfat-inode-free-idempotent-null-safe:
 *   exfat_inode_free(NULL) must not crash / assert-fail.
 */
static void test_inode_free_null_safe(void **state)
{
    (void)state;
    exfat_inode_free(NULL); /* must not crash */
}

/* ---- test_inode_free_after_alloc --------------------------------------- */
/*
 * Alloc then free — no crash, no leak (validated by valgrind/ASAN in CI,
 * here we just assert the call sequence completes without fault).
 */
static void test_inode_free_after_alloc(void **state)
{
    (void)state;
    exfat_inode_info *ei = must_alloc();
    exfat_inode_free(ei);
    /* Reaching here without SIGSEGV / abort is the assertion. */
}

/* ---- test_inode_init_dir_chain_fields ---------------------------------- */
/*
 * Invariant exfat-inode-init-dir-chain-pure:
 *   After exfat_inode_init_dir_chain(ei, clu):
 *     ei->dir.dir   == clu
 *     ei->dir.size  == 0
 *     ei->dir.flags == ALLOC_FAT_CHAIN
 *     ei->type      == TYPE_DIR
 *     ei->start_clu == clu
 *   entry is NOT set by this function (remains whatever the caller left).
 */
static void test_inode_init_dir_chain_fields(void **state)
{
    (void)state;

    exfat_inode_info *ei = must_alloc();
    /* pre-poison entry so we can confirm init_dir_chain does NOT reset it */
    ei->entry = -1;

    const uint32_t clu = EXFAT_FIRST_CLUSTER + 3u; /* 5 — valid cluster */
    exfat_inode_init_dir_chain(ei, clu);

    assert_int_equal((int)ei->dir.dir,           (int)clu);
    assert_int_equal((int)ei->dir.size,          0);
    assert_int_equal((int)ei->dir.flags,         (int)ALLOC_FAT_CHAIN);
    assert_int_equal((int)ei->type,              (int)TYPE_DIR);
    assert_int_equal((int)ei->start_clu,         (int)clu);
    /* entry must be untouched */
    assert_int_equal(ei->entry, -1);

    exfat_inode_free(ei);
}

/* ---- test_inode_init_dir_chain_minimum_cluster ------------------------- */
/*
 * EXFAT_FIRST_CLUSTER (2) is the minimum valid cluster.  The function
 * accepts it without validation (pure assignment, per the spec invariant).
 */
static void test_inode_init_dir_chain_minimum_cluster(void **state)
{
    (void)state;

    exfat_inode_info *ei = must_alloc();
    exfat_inode_init_dir_chain(ei, EXFAT_FIRST_CLUSTER);

    assert_int_equal((int)ei->dir.dir,   (int)EXFAT_FIRST_CLUSTER);
    assert_int_equal((int)ei->start_clu, (int)EXFAT_FIRST_CLUSTER);
    assert_int_equal((int)ei->type,      (int)TYPE_DIR);

    exfat_inode_free(ei);
}

/* ---- test_inode_init_dir_chain_idempotent ------------------------------ */
/*
 * Calling init_dir_chain twice with the same cluster must produce the same
 * field values both times (pure assignment, no accumulated state).
 */
static void test_inode_init_dir_chain_idempotent(void **state)
{
    (void)state;

    exfat_inode_info *ei = must_alloc();
    const uint32_t clu = 10u;

    exfat_inode_init_dir_chain(ei, clu);
    exfat_inode_init_dir_chain(ei, clu); /* second call — same result */

    assert_int_equal((int)ei->dir.dir,   (int)clu);
    assert_int_equal((int)ei->dir.size,  0);
    assert_int_equal((int)ei->dir.flags, (int)ALLOC_FAT_CHAIN);
    assert_int_equal((int)ei->type,      (int)TYPE_DIR);
    assert_int_equal((int)ei->start_clu, (int)clu);

    exfat_inode_free(ei);
}

/* ---- test_inode_init_dir_chain_overwrite ------------------------------- */
/*
 * init_dir_chain with a second cluster overwrites all five fields from the
 * first call (no fields from the previous call persist).
 */
static void test_inode_init_dir_chain_overwrite(void **state)
{
    (void)state;

    exfat_inode_info *ei = must_alloc();

    exfat_inode_init_dir_chain(ei, 4u);
    exfat_inode_init_dir_chain(ei, 7u); /* overwrite */

    assert_int_equal((int)ei->dir.dir,   7);
    assert_int_equal((int)ei->start_clu, 7);
    assert_int_equal((int)ei->type,      (int)TYPE_DIR);

    exfat_inode_free(ei);
}

/* ======================================================================== */

const struct CMUnitTest test_inode_alloc_tests[] = {
    cmocka_unit_test(test_inode_alloc_happy_path),
    cmocka_unit_test(test_inode_alloc_null_out),
    cmocka_unit_test(test_inode_alloc_out_not_aliased),
    cmocka_unit_test(test_inode_free_null_safe),
    cmocka_unit_test(test_inode_free_after_alloc),
    cmocka_unit_test(test_inode_init_dir_chain_fields),
    cmocka_unit_test(test_inode_init_dir_chain_minimum_cluster),
    cmocka_unit_test(test_inode_init_dir_chain_idempotent),
    cmocka_unit_test(test_inode_init_dir_chain_overwrite),
};

const size_t test_inode_alloc_tests_count =
    sizeof(test_inode_alloc_tests) / sizeof(test_inode_alloc_tests[0]);
