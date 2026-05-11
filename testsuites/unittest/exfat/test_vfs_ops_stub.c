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
 * test_vfs_ops_stub.c — host cmocka suite for g_exfatVops / g_exfatFops
 * tables (spec: spec/exfat/interface/exfat_vfs_ops_stub.spec).
 *
 * The original spec invariant `exfat-vfs-stub-null-trap` ("all fields NULL
 * so VFS returns -ENOSYS") was the v1 baseline. Subsequent Wave A/B stages
 * (lookup, read, write, mkdir, unlink, rmdir, rename, ...) refined the
 * tables by filling individual slots — that's the explicit refinement path
 * called out in invariant `exfat-vfs-stub-symbol-stable` ("修改字段初值,
 * 但绝不重命名符号"). The current state therefore preserves the two
 * still-applicable invariants:
 *
 *   exfat-vfs-stub-symbol-stable    — same names, file scope, single-instance
 *   exfat-vfs-stub-no-runtime-init  — static .data init, no init() function
 *
 * `exfat-vfs-stub-null-trap` evolved into "any field still NULL traps to
 * -ENOSYS"; we assert the currently-wired slots are non-NULL and call out
 * the slots that are still legitimately NULL (Setattr / Chattr / Link / ...)
 * as v1 limitations.
 *
 * `exfat-vfs-stub-loscfg-gated` is a build-time invariant (the cmocka
 * harness sets -DLOSCFG_FS_EXFAT explicitly; we assert the macro is defined
 * via static_assert as a compile-time check).
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdlib.h>
#include <assert.h>
#include <cmocka.h>

#include "exfat.h"

extern struct VnodeOps             g_exfatVops;
extern struct file_operations_vfs  g_exfatFops;

/* Compile-time assertion that the LOSCFG gate is in effect for this TU.
 * Matches invariant exfat-vfs-stub-loscfg-gated. */
#ifndef LOSCFG_FS_EXFAT
#error "LOSCFG_FS_EXFAT must be set when compiling exfat host tests"
#endif

/* ---------- Invariant exfat-vfs-stub-symbol-stable ---------- */

static void test_vops_symbol_addressable(void **state)
{
    (void)state;
    assert_non_null((const void *)&g_exfatVops);
}

static void test_fops_symbol_addressable(void **state)
{
    (void)state;
    assert_non_null((const void *)&g_exfatFops);
}

/* ---------- Invariant exfat-vfs-stub-no-runtime-init ----------
 * The tables live in .data with static initialisers, so values are
 * already present without any init() call. We verify a wired slot is
 * non-NULL without first invoking any FS code. */
static void test_vops_static_init_no_call(void **state)
{
    (void)state;
    assert_non_null((const void *)g_exfatVops.Lookup);
    assert_non_null((const void *)g_exfatVops.Reclaim);
}

static void test_fops_static_init_no_call(void **state)
{
    (void)state;
    assert_non_null((const void *)g_exfatFops.read);
    assert_non_null((const void *)g_exfatFops.open);
}

/* ---------- Refined invariant exfat-vfs-stub-null-trap ----------
 * Wired slots (post-Wave-B) match mount.spec exports + the VFS surface
 * stages closed in this branch. Asserting them non-NULL ensures no future
 * stage accidentally NULL-out a wired field (which would silently turn
 * lookup into -ENOSYS on a mounted volume). */
static void test_vops_wired_slots_non_null(void **state)
{
    (void)state;
    assert_non_null((const void *)g_exfatVops.Lookup);
    assert_non_null((const void *)g_exfatVops.Reclaim);
    assert_non_null((const void *)g_exfatVops.Create);
    assert_non_null((const void *)g_exfatVops.Opendir);
    assert_non_null((const void *)g_exfatVops.Readdir);
    assert_non_null((const void *)g_exfatVops.Closedir);
    assert_non_null((const void *)g_exfatVops.Rewinddir);
    assert_non_null((const void *)g_exfatVops.Getattr);
    assert_non_null((const void *)g_exfatVops.Mkdir);
    assert_non_null((const void *)g_exfatVops.Unlink);
    assert_non_null((const void *)g_exfatVops.Rmdir);
    assert_non_null((const void *)g_exfatVops.Rename);
    assert_non_null((const void *)g_exfatVops.Truncate);
    assert_non_null((const void *)g_exfatVops.Truncate64);
}

static void test_fops_wired_slots_non_null(void **state)
{
    (void)state;
    assert_non_null((const void *)g_exfatFops.open);
    assert_non_null((const void *)g_exfatFops.close);
    assert_non_null((const void *)g_exfatFops.read);
    assert_non_null((const void *)g_exfatFops.write);
    assert_non_null((const void *)g_exfatFops.seek);
}

/* Slots still NULL in v1 — Setattr / Chattr / Link / Symlink / Readlink /
 * ReadPage / WritePage / Fscheck. Leaving them NULL is intentional; VFS
 * returns -ENOSYS to syscalls that hit them. Asserting this is the literal
 * realisation of the original -null-trap- invariant for the un-refined
 * slots. */
static void test_vops_unwired_slots_still_null(void **state)
{
    (void)state;
    assert_null((const void *)g_exfatVops.Setattr);
    assert_null((const void *)g_exfatVops.Chattr);
    assert_null((const void *)g_exfatVops.Link);
    assert_null((const void *)g_exfatVops.Symlink);
    assert_null((const void *)g_exfatVops.Readlink);
    assert_null((const void *)g_exfatVops.ReadPage);
    assert_null((const void *)g_exfatVops.WritePage);
    assert_null((const void *)g_exfatVops.Fscheck);
}

/* Wired-slot identity match against the canonical implementations.
 * If a future stage accidentally swapped a slot to a different function,
 * this test detects it. */
extern int VfsExfatLookup(struct Vnode *, const char *, int, struct Vnode **);
extern int VfsExfatReclaim(struct Vnode *);
extern int VfsExfatCreate(struct Vnode *, const char *, int, struct Vnode **);
extern int VfsExfatMkdir(struct Vnode *, const char *, mode_t, struct Vnode **);
extern int VfsExfatUnlink(struct Vnode *, struct Vnode *, const char *);
extern int VfsExfatRmdir(struct Vnode *, struct Vnode *, const char *);
extern int VfsExfatRename(struct Vnode *, struct Vnode *, const char *, const char *);

static void test_vops_slot_identity_match(void **state)
{
    (void)state;
    assert_ptr_equal((const void *)g_exfatVops.Lookup,  (const void *)VfsExfatLookup);
    assert_ptr_equal((const void *)g_exfatVops.Reclaim, (const void *)VfsExfatReclaim);
    assert_ptr_equal((const void *)g_exfatVops.Create,  (const void *)VfsExfatCreate);
    assert_ptr_equal((const void *)g_exfatVops.Mkdir,   (const void *)VfsExfatMkdir);
    assert_ptr_equal((const void *)g_exfatVops.Unlink,  (const void *)VfsExfatUnlink);
    assert_ptr_equal((const void *)g_exfatVops.Rmdir,   (const void *)VfsExfatRmdir);
    assert_ptr_equal((const void *)g_exfatVops.Rename,  (const void *)VfsExfatRename);
}

const struct CMUnitTest test_vfs_ops_stub_tests[] = {
    cmocka_unit_test(test_vops_symbol_addressable),
    cmocka_unit_test(test_fops_symbol_addressable),
    cmocka_unit_test(test_vops_static_init_no_call),
    cmocka_unit_test(test_fops_static_init_no_call),
    cmocka_unit_test(test_vops_wired_slots_non_null),
    cmocka_unit_test(test_fops_wired_slots_non_null),
    cmocka_unit_test(test_vops_unwired_slots_still_null),
    cmocka_unit_test(test_vops_slot_identity_match),
};
const size_t test_vfs_ops_stub_tests_count =
    sizeof(test_vfs_ops_stub_tests) / sizeof(test_vfs_ops_stub_tests[0]);
