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
 * test_open_close — cmocka host-harness unit tests for VfsExfatOpen / VfsExfatClose.
 *
 * Production source: fs/exfat/exfat_open_close.c
 * Dependencies satisfied: struct file / struct Vnode / struct Mount / exfat_inode_info
 *   — all available via host stubs + exfat.h (no VFS hash, no disk IO).
 *
 * Suite: test_open_close_tests  (10 testpoints)
 *   open_null_filep             — NULL filep → -EINVAL
 *   open_null_vnode             — filep with NULL f_vnode → -EINVAL
 *   open_null_mount             — vnode with NULL originMount → -EINVAL
 *   open_null_data              — vnode with NULL data → -EINVAL
 *   open_dir_by_ei_type         — ei->type == TYPE_DIR → -EISDIR
 *   open_dir_by_vnode_type      — vp->type == VNODE_TYPE_DIR → -EISDIR
 *   open_bad_vnode_type         — ei FILE but vp type UNKNOWN → -EINVAL
 *   open_bad_ei_type            — vp REG but ei type unknown → -EINVAL
 *   open_regular_file_ok        — valid REG vnode + FILE ei → 0
 *   close_always_zero           — VfsExfatClose(any) → 0
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <stdlib.h>
#include <cmocka.h>

#include "../../../fs/exfat/include/exfat.h"

/* Production function declarations. */
int VfsExfatOpen(struct file *filep);
int VfsExfatClose(struct file *filep);

/* ---- fixture helpers ---------------------------------------------------- */

static void make_ei(exfat_inode_info *ei, uint32_t type)
{
    memset(ei, 0, sizeof(*ei));
    ei->type = type;
}

static void make_vnode(struct Vnode *vp, enum VnodeType vtype,
                       struct Mount *mnt, void *data)
{
    memset(vp, 0, sizeof(*vp));
    vp->type        = vtype;
    vp->originMount = mnt;
    vp->data        = data;
}

static void make_mount(struct Mount *mnt, void *data)
{
    memset(mnt, 0, sizeof(*mnt));
    mnt->data = data;
}

static void make_filep(struct file *f, struct Vnode *vp)
{
    memset(f, 0, sizeof(*f));
    f->f_vnode = vp;
}

/* ---- testpoints -------------------------------------------------------- */

static void open_null_filep(void **state)
{
    (void)state;
    assert_int_equal(VfsExfatOpen(NULL), -EINVAL);
}

static void open_null_vnode(void **state)
{
    (void)state;
    struct file f;
    memset(&f, 0, sizeof(f));
    f.f_vnode = NULL;
    assert_int_equal(VfsExfatOpen(&f), -EINVAL);
}

static void open_null_mount(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_FILE);
    make_vnode(&vp, VNODE_TYPE_REG, NULL /* no mount */, &ei);
    make_filep(&f, &vp);
    assert_int_equal(VfsExfatOpen(&f), -EINVAL);
}

static void open_null_data(void **state)
{
    (void)state;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_mount(&mnt, (void *)1); /* mnt->data non-NULL */
    make_vnode(&vp, VNODE_TYPE_REG, &mnt, NULL /* no data */);
    make_filep(&f, &vp);
    assert_int_equal(VfsExfatOpen(&f), -EINVAL);
}

static void open_dir_by_ei_type(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_DIR);               /* ei says DIR */
    make_mount(&mnt, (void *)1);
    make_vnode(&vp, VNODE_TYPE_REG, &mnt, &ei); /* vnode says REG — ei wins */
    make_filep(&f, &vp);
    assert_int_equal(VfsExfatOpen(&f), -EISDIR);
}

static void open_dir_by_vnode_type(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_FILE);              /* ei says FILE */
    make_mount(&mnt, (void *)1);
    make_vnode(&vp, VNODE_TYPE_DIR, &mnt, &ei); /* vnode says DIR — checked first */
    make_filep(&f, &vp);
    assert_int_equal(VfsExfatOpen(&f), -EISDIR);
}

static void open_bad_vnode_type(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_FILE);
    make_mount(&mnt, (void *)1);
    make_vnode(&vp, VNODE_TYPE_UNKNOWN, &mnt, &ei); /* not REG, not DIR */
    make_filep(&f, &vp);
    assert_int_equal(VfsExfatOpen(&f), -EINVAL);
}

static void open_bad_ei_type(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_BITMAP); /* neither FILE nor DIR */
    make_mount(&mnt, (void *)1);
    make_vnode(&vp, VNODE_TYPE_REG, &mnt, &ei);
    make_filep(&f, &vp);
    assert_int_equal(VfsExfatOpen(&f), -EINVAL);
}

static void open_regular_file_ok(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_FILE);
    make_mount(&mnt, (void *)1);
    make_vnode(&vp, VNODE_TYPE_REG, &mnt, &ei);
    make_filep(&f, &vp);
    /* Invariant exfat-open-pos-untouched: f_pos must remain 0 after open. */
    assert_int_equal(VfsExfatOpen(&f), 0);
    assert_int_equal((int)f.f_pos, 0);
    assert_null(f.f_priv);
}

static void close_always_zero(void **state)
{
    (void)state;
    /* VfsExfatClose ignores filep entirely — even NULL. */
    assert_int_equal(VfsExfatClose(NULL), 0);

    struct file f;
    memset(&f, 0, sizeof(f));
    assert_int_equal(VfsExfatClose(&f), 0);
}

/* ---- suite registration ------------------------------------------------ */

const struct CMUnitTest test_open_close_tests[] = {
    cmocka_unit_test(open_null_filep),
    cmocka_unit_test(open_null_vnode),
    cmocka_unit_test(open_null_mount),
    cmocka_unit_test(open_null_data),
    cmocka_unit_test(open_dir_by_ei_type),
    cmocka_unit_test(open_dir_by_vnode_type),
    cmocka_unit_test(open_bad_vnode_type),
    cmocka_unit_test(open_bad_ei_type),
    cmocka_unit_test(open_regular_file_ok),
    cmocka_unit_test(close_always_zero),
};

const size_t test_open_close_tests_count =
    sizeof(test_open_close_tests) / sizeof(test_open_close_tests[0]);
