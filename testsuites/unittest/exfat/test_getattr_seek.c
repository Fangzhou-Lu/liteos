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
 * test_getattr_seek — cmocka host-harness tests for VfsExfatGetattr / VfsExfatSeek.
 *
 * Production source: fs/exfat/exfat_attr.c
 * Dependencies: struct file, struct Vnode, struct Mount, exfat_inode_info, exfat_sb_info.
 * No VFS hash, no disk IO.
 *
 * Suite: test_getattr_seek_tests  (16 testpoints)
 *
 * Getattr (8):
 *   getattr_null_vnode           — NULL vp → -EINVAL
 *   getattr_null_mount           — vp->originMount NULL → -EINVAL
 *   getattr_null_sbi             — mnt->data NULL → -EINVAL
 *   getattr_null_vdata           — vp->data NULL → -EINVAL
 *   getattr_null_stat            — NULL st → -EINVAL
 *   getattr_st_cleared           — result struct is zeroed before fill
 *   getattr_size_from_ei         — st_size == ei->size
 *   getattr_blksize_cluster      — st_blksize == sbi->cluster_size
 *
 * Seek (8):
 *   seek_null_filep              — NULL filep → -EINVAL (cast to off_t)
 *   seek_set_negative            — SEEK_SET offset < 0 → -EINVAL
 *   seek_set_zero                — SEEK_SET 0 → 0
 *   seek_set_positive            — SEEK_SET 512 → 512, f_pos updated
 *   seek_cur_advance             — SEEK_CUR +100 from pos 512 → 612
 *   seek_cur_negative_result     — SEEK_CUR to negative → -EINVAL
 *   seek_end_at_size             — SEEK_END 0 → ei->size
 *   seek_rejects_non_file        — ei->type DIR → -EINVAL
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/stat.h>
#include <cmocka.h>

#include "../../../fs/exfat/include/exfat.h"

/* Production function declarations. */
int   VfsExfatGetattr(struct Vnode *vp, struct stat *st);
off_t VfsExfatSeek(struct file *filep, off_t offset, int whence);

/* ---- fixture helpers ---------------------------------------------------- */

static void make_sbi(exfat_sb_info *sbi, uint32_t cluster_size, INT32 part_id)
{
    memset(sbi, 0, sizeof(*sbi));
    sbi->cluster_size = cluster_size;
    sbi->part_id      = part_id;
}

static void make_ei(exfat_inode_info *ei, uint32_t type, uint64_t size)
{
    memset(ei, 0, sizeof(*ei));
    ei->type  = type;
    ei->size  = size;
    ei->i_pos = ((uint64_t)42u << 32) | 7u; /* arbitrary non-zero i_pos */
}

static void make_mount(struct Mount *mnt, void *data)
{
    memset(mnt, 0, sizeof(*mnt));
    mnt->data = data;
}

static void make_vnode_full(struct Vnode *vp, enum VnodeType vtype,
                            struct Mount *mnt, void *data, mode_t mode)
{
    memset(vp, 0, sizeof(*vp));
    vp->type        = vtype;
    vp->originMount = mnt;
    vp->data        = data;
    vp->mode        = mode;
}

static void make_filep(struct file *f, struct Vnode *vp, loff_t pos)
{
    memset(f, 0, sizeof(*f));
    f->f_vnode = vp;
    f->f_pos   = pos;
}

/* ---- Getattr testpoints ------------------------------------------------ */

static void getattr_null_vnode(void **state)
{
    (void)state;
    struct stat st;
    assert_int_equal(VfsExfatGetattr(NULL, &st), -EINVAL);
}

static void getattr_null_mount(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Vnode vp;
    struct stat st;

    make_ei(&ei, TYPE_FILE, 1024);
    memset(&vp, 0, sizeof(vp));
    vp.originMount = NULL;
    vp.data        = &ei;
    assert_int_equal(VfsExfatGetattr(&vp, &st), -EINVAL);
}

static void getattr_null_sbi(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct stat st;

    make_ei(&ei, TYPE_FILE, 1024);
    make_mount(&mnt, NULL); /* sbi == NULL */
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);
    assert_int_equal(VfsExfatGetattr(&vp, &st), -EINVAL);
}

static void getattr_null_vdata(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    struct Mount mnt;
    struct Vnode vp;
    struct stat st;

    make_sbi(&sbi, 4096, 0);
    make_mount(&mnt, &sbi);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, NULL /* no data */, 0644);
    assert_int_equal(VfsExfatGetattr(&vp, &st), -EINVAL);
}

static void getattr_null_stat(void **state)
{
    (void)state;
    exfat_inode_info ei;
    exfat_sb_info sbi;
    struct Mount mnt;
    struct Vnode vp;

    make_ei(&ei, TYPE_FILE, 1024);
    make_sbi(&sbi, 4096, 0);
    make_mount(&mnt, &sbi);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);
    assert_int_equal(VfsExfatGetattr(&vp, NULL), -EINVAL);
}

static void getattr_st_cleared(void **state)
{
    (void)state;
    exfat_inode_info ei;
    exfat_sb_info sbi;
    struct Mount mnt;
    struct Vnode vp;
    struct stat st;

    /* Pre-poison the stat buffer. */
    memset(&st, 0xFF, sizeof(st));

    make_ei(&ei, TYPE_FILE, 0);
    make_sbi(&sbi, 512, 0);
    make_mount(&mnt, &sbi);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);

    assert_int_equal(VfsExfatGetattr(&vp, &st), 0);
    /* Invariant exfat-vfsops-getattr-no-timestamps: timestamps zeroed. */
    assert_int_equal((int)st.st_atime, 0);
    assert_int_equal((int)st.st_mtime, 0);
    assert_int_equal((int)st.st_ctime, 0);
}

static void getattr_size_from_ei(void **state)
{
    (void)state;
    exfat_inode_info ei;
    exfat_sb_info sbi;
    struct Mount mnt;
    struct Vnode vp;
    struct stat st;

    make_ei(&ei, TYPE_FILE, 65536);
    make_sbi(&sbi, 4096, 3);
    sbi.options.fs_uid = 1000;
    sbi.options.fs_gid = 1000;
    make_mount(&mnt, &sbi);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);

    assert_int_equal(VfsExfatGetattr(&vp, &st), 0);
    /* Invariant exfat-vfsops-getattr-size: st_size == ei->size. */
    assert_int_equal((int)st.st_size, 65536);
    /* Invariant exfat-vfsops-getattr-nlink-one. */
    assert_int_equal((int)st.st_nlink, 1);
    assert_int_equal((int)st.st_uid, 1000);
    assert_int_equal((int)st.st_gid, 1000);
}

static void getattr_blksize_cluster(void **state)
{
    (void)state;
    exfat_inode_info ei;
    exfat_sb_info sbi;
    struct Mount mnt;
    struct Vnode vp;
    struct stat st;

    make_ei(&ei, TYPE_FILE, 8192);
    make_sbi(&sbi, 32768, 0); /* 32 KiB cluster */
    make_mount(&mnt, &sbi);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);

    assert_int_equal(VfsExfatGetattr(&vp, &st), 0);
    /* Invariant exfat-vfsops-getattr-blksize-cluster. */
    assert_int_equal((int)st.st_blksize, 32768);
    /* Invariant exfat-vfsops-getattr-blocks-512: ceil(8192/512)=16. */
    assert_int_equal((int)st.st_blocks, 16);
}

/* ---- Seek testpoints --------------------------------------------------- */

static void seek_null_filep(void **state)
{
    (void)state;
    off_t r = VfsExfatSeek(NULL, 0, SEEK_SET);
    assert_int_equal((int)r, -EINVAL);
}

static void seek_set_negative(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_FILE, 1024);
    make_mount(&mnt, (void *)1);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);
    make_filep(&f, &vp, 0);

    assert_int_equal((int)VfsExfatSeek(&f, -1, SEEK_SET), -EINVAL);
}

static void seek_set_zero(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_FILE, 1024);
    make_mount(&mnt, (void *)1);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);
    make_filep(&f, &vp, 100);

    off_t r = VfsExfatSeek(&f, 0, SEEK_SET);
    assert_int_equal((int)r, 0);
    assert_int_equal((int)f.f_pos, 0);
}

static void seek_set_positive(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_FILE, 4096);
    make_mount(&mnt, (void *)1);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);
    make_filep(&f, &vp, 0);

    off_t r = VfsExfatSeek(&f, 512, SEEK_SET);
    assert_int_equal((int)r, 512);
    assert_int_equal((int)f.f_pos, 512);
}

static void seek_cur_advance(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_FILE, 4096);
    make_mount(&mnt, (void *)1);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);
    make_filep(&f, &vp, 512); /* start at 512 */

    off_t r = VfsExfatSeek(&f, 100, SEEK_CUR);
    assert_int_equal((int)r, 612);
    assert_int_equal((int)f.f_pos, 612);
}

static void seek_cur_negative_result(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_FILE, 4096);
    make_mount(&mnt, (void *)1);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);
    make_filep(&f, &vp, 10); /* pos = 10 */

    /* SEEK_CUR -100 → new_pos = -90 → -EINVAL. */
    assert_int_equal((int)VfsExfatSeek(&f, -100, SEEK_CUR), -EINVAL);
    /* f_pos must be untouched on failure. */
    assert_int_equal((int)f.f_pos, 10);
}

static void seek_end_at_size(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    make_ei(&ei, TYPE_FILE, 8192);
    make_mount(&mnt, (void *)1);
    make_vnode_full(&vp, VNODE_TYPE_REG, &mnt, &ei, 0644);
    make_filep(&f, &vp, 0);

    /* SEEK_END 0 → position == file size. */
    off_t r = VfsExfatSeek(&f, 0, SEEK_END);
    assert_int_equal((int)r, 8192);
    assert_int_equal((int)f.f_pos, 8192);
}

static void seek_rejects_non_file(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    struct file f;

    /* ei->type == TYPE_DIR → callback must reject immediately. */
    make_ei(&ei, TYPE_DIR, 0);
    make_mount(&mnt, (void *)1);
    make_vnode_full(&vp, VNODE_TYPE_DIR, &mnt, &ei, 0755);
    make_filep(&f, &vp, 0);

    assert_int_equal((int)VfsExfatSeek(&f, 0, SEEK_SET), -EINVAL);
}

/* ---- suite registration ------------------------------------------------ */

const struct CMUnitTest test_getattr_seek_tests[] = {
    /* Getattr */
    cmocka_unit_test(getattr_null_vnode),
    cmocka_unit_test(getattr_null_mount),
    cmocka_unit_test(getattr_null_sbi),
    cmocka_unit_test(getattr_null_vdata),
    cmocka_unit_test(getattr_null_stat),
    cmocka_unit_test(getattr_st_cleared),
    cmocka_unit_test(getattr_size_from_ei),
    cmocka_unit_test(getattr_blksize_cluster),
    /* Seek */
    cmocka_unit_test(seek_null_filep),
    cmocka_unit_test(seek_set_negative),
    cmocka_unit_test(seek_set_zero),
    cmocka_unit_test(seek_set_positive),
    cmocka_unit_test(seek_cur_advance),
    cmocka_unit_test(seek_cur_negative_result),
    cmocka_unit_test(seek_end_at_size),
    cmocka_unit_test(seek_rejects_non_file),
};

const size_t test_getattr_seek_tests_count =
    sizeof(test_getattr_seek_tests) / sizeof(test_getattr_seek_tests[0]);
