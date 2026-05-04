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
 * test_write — cmocka host-harness unit tests for VfsExfatWrite.
 *
 * Production source: fs/exfat/exfat_write.c
 *
 * Image layout (blocksize=512, cluster_size=512, 1 sector/cluster):
 *
 *   Sectors 0-7  : FAT region (fat_offset=0, fat_length=8)
 *     FAT[2] = 3         (chain: 2→3→4→EOF)
 *     FAT[3] = 4
 *     FAT[4] = EOF
 *   Sectors 8-10 : data clusters (clu_offset=8)
 *     cluster 2 (sector 8)  : pre-filled 0xAA
 *     cluster 3 (sector 9)  : pre-filled 0xBB
 *     cluster 4 (sector 10) : pre-filled 0xCC
 *
 *   sbi geometry: num_clusters=16, cluster_size_bits=9, blocksize_bits=9,
 *                 sect_per_clus_bits=0, part_id=0.
 *
 * Suite: test_write_tests
 *
 * Cases (from spec [SPECIFICATION]):
 *   Case 1 ZeroLen          → write_zero_len_fast_path
 *   Case 2 非常规 vnode      → write_dir_type_rejected
 *   Case 3 BeyondEOF        → write_beyond_eof_returns_zero
 *   Case 4 Success          → write_single_cluster_no_chain,
 *                             write_multi_cluster_no_chain,
 *                             write_fat_chained_three_clusters,
 *                             write_clamp_by_size,
 *                             write_partial_offset_in_cluster
 *   Case 5 IO 失败 + N>0    → write_mid_loop_io_fail_short_write
 *   Case 6 IO 失败 + N==0   → write_first_io_fail_returns_eio,
 *                             write_first_part_write_fail_returns_eio
 *   Case 7 分配失败          → LAYER_B (host LOS_MemAlloc stub maps to malloc;
 *                             cmocka cannot reliably simulate ENOMEM without
 *                             extra alloc-fail hook plumbing).
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
#include "../../../fs/exfat/include/exfat_raw.h"
#include "mock_disk.h"

/* Production function declarations. */
ssize_t VfsExfatWrite(struct file *filep, const char *buf, size_t len);
ssize_t VfsExfatRead(struct file *filep, char *buf, size_t len);

/* ---- image parameters (mirror test_read.c geometry) ------------------- */

#define WRITE_TEST_BLOCKSIZE        512u
#define WRITE_TEST_CLUSTER_SIZE     512u
#define WRITE_TEST_CLU_OFFSET       8u
#define WRITE_TEST_FAT_OFFSET       0u
#define WRITE_TEST_FAT_LENGTH       8u
#define WRITE_TEST_NUM_CLUSTERS     16u
#define WRITE_TEST_NUM_SECTORS      11u
#define WRITE_TEST_IMAGE_SIZE       (WRITE_TEST_NUM_SECTORS * WRITE_TEST_BLOCKSIZE)

#define WRITE_TEST_CLU2_FILL        0xAAu
#define WRITE_TEST_CLU3_FILL        0xBBu
#define WRITE_TEST_CLU4_FILL        0xCCu

#define CLU_TO_SECTOR(c)    ((uint64_t)(WRITE_TEST_CLU_OFFSET) + ((c) - 2u))
#define CLU_TO_BYTE(c)      (CLU_TO_SECTOR(c) * WRITE_TEST_BLOCKSIZE)

/* ---- global synthetic image -------------------------------------------- */

static uint8_t g_write_image[WRITE_TEST_IMAGE_SIZE];

static void put_le32(uint8_t *buf, size_t off, uint32_t val)
{
    buf[off + 0] = (uint8_t)(val);
    buf[off + 1] = (uint8_t)(val >> 8);
    buf[off + 2] = (uint8_t)(val >> 16);
    buf[off + 3] = (uint8_t)(val >> 24);
}

static void build_write_image(void)
{
    memset(g_write_image, 0, sizeof(g_write_image));

    /* FAT chain 2→3→4→EOF (sector 0). */
    put_le32(g_write_image, WRITE_TEST_FAT_OFFSET * WRITE_TEST_BLOCKSIZE + 2u * 4u, 3u);
    put_le32(g_write_image, WRITE_TEST_FAT_OFFSET * WRITE_TEST_BLOCKSIZE + 3u * 4u, 4u);
    put_le32(g_write_image, WRITE_TEST_FAT_OFFSET * WRITE_TEST_BLOCKSIZE + 4u * 4u,
             EXFAT_EOF_CLUSTER);

    /* Data clusters with distinct fill patterns. */
    memset(g_write_image + CLU_TO_BYTE(2u), WRITE_TEST_CLU2_FILL, WRITE_TEST_CLUSTER_SIZE);
    memset(g_write_image + CLU_TO_BYTE(3u), WRITE_TEST_CLU3_FILL, WRITE_TEST_CLUSTER_SIZE);
    memset(g_write_image + CLU_TO_BYTE(4u), WRITE_TEST_CLU4_FILL, WRITE_TEST_CLUSTER_SIZE);
}

/* ---- sbi / ei / fixture helpers ---------------------------------------- */

static void make_sbi(exfat_sb_info *sbi)
{
    memset(sbi, 0, sizeof(*sbi));
    sbi->blocksize          = WRITE_TEST_BLOCKSIZE;
    sbi->blocksize_bits     = 9u;
    sbi->cluster_size       = WRITE_TEST_CLUSTER_SIZE;
    sbi->cluster_size_bits  = 9u;
    sbi->sect_per_clus_bits = 0u;
    sbi->clu_offset         = WRITE_TEST_CLU_OFFSET;
    sbi->fat_offset         = WRITE_TEST_FAT_OFFSET;
    sbi->fat_length         = WRITE_TEST_FAT_LENGTH;
    sbi->num_clusters       = WRITE_TEST_NUM_CLUSTERS;
    sbi->part_id            = 0;
}

static void make_ei(exfat_inode_info *ei, uint32_t start_clu,
                    uint8_t flags, uint64_t size)
{
    memset(ei, 0, sizeof(*ei));
    ei->type      = TYPE_FILE;
    ei->start_clu = start_clu;
    ei->flags     = flags;
    ei->size      = size;
}

static void make_mount(struct Mount *mnt, void *data)
{
    memset(mnt, 0, sizeof(*mnt));
    mnt->data = data;
}

static void make_vnode(struct Vnode *vp, struct Mount *mnt, void *data)
{
    memset(vp, 0, sizeof(*vp));
    vp->type        = VNODE_TYPE_REG;
    vp->originMount = mnt;
    vp->data        = data;
}

static void make_filep(struct file *f, struct Vnode *vp, loff_t pos)
{
    memset(f, 0, sizeof(*f));
    f->f_vnode = vp;
    f->f_pos   = pos;
}

/* ---- setup / teardown -------------------------------------------------- */

static int write_setup(void **state)
{
    (void)state;
    build_write_image();
    mock_disk_load(g_write_image, sizeof(g_write_image));
    mock_disk_reset_counters();
    return 0;
}

static int write_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

/* ---- testpoints --------------------------------------------------------- */

/* Case 1 / Invariant exfat-write-zero-len-fast-path: len==0 → 0, no IO. */
static void write_zero_len_fast_path(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[16] = {0};

    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatWrite(&f, buf, 0u);
    assert_int_equal((int)ret, 0);
    assert_int_equal((int)f.f_pos, 0);
    assert_int_equal((int)mock_disk_read_count(), 0);
    assert_int_equal((int)mock_disk_write_count(), 0);
}

/* NULL filep → -EINVAL. */
static void write_null_filep(void **state)
{
    (void)state;
    char buf[16] = {0};
    ssize_t ret = VfsExfatWrite(NULL, buf, 16u);
    assert_int_equal((int)ret, -EINVAL);
}

/* NULL buf with len>0 → -EINVAL; f_pos untouched. */
static void write_null_buf(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;

    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatWrite(&f, NULL, 16u);
    assert_int_equal((int)ret, -EINVAL);
    assert_int_equal((int)f.f_pos, 0);
}

/* NULL f_vnode → -EINVAL. */
static void write_null_vnode(void **state)
{
    (void)state;
    struct file f;
    char buf[16] = {0};
    memset(&f, 0, sizeof(f));
    f.f_vnode = NULL;
    ssize_t ret = VfsExfatWrite(&f, buf, 16u);
    assert_int_equal((int)ret, -EINVAL);
    assert_int_equal((int)f.f_pos, 0);
}

/* mount->data == NULL → -EINVAL (sbi == NULL guard). */
static void write_null_mount_data(void **state)
{
    (void)state;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[16] = {0};

    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, NULL); /* data == NULL */
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatWrite(&f, buf, 16u);
    assert_int_equal((int)ret, -EINVAL);
    assert_int_equal((int)f.f_pos, 0);
}

/* Case 2 / Invariant exfat-write-isdir-rejected: ei->type=TYPE_DIR → -EISDIR. */
static void write_dir_type_rejected(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[16];

    memset(buf, 0x55, sizeof(buf));
    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    ei.type = TYPE_DIR;
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatWrite(&f, buf, 16u);
    assert_int_equal((int)ret, -EISDIR);
    assert_int_equal((int)f.f_pos, 0);
    assert_int_equal((int)mock_disk_read_count(), 0);
    assert_int_equal((int)mock_disk_write_count(), 0);
}

/* Case 3 / Invariant exfat-write-no-extend: f_pos>=ei->size → 0, no IO. */
static void write_beyond_eof_returns_zero(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[16];

    memset(buf, 0x77, sizeof(buf));
    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 256u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 256); /* exactly EOF */

    ssize_t ret = VfsExfatWrite(&f, buf, 16u);
    assert_int_equal((int)ret, 0);
    assert_int_equal((int)f.f_pos, 256);

    /* Beyond EOF too. */
    f.f_pos = 1024;
    ret = VfsExfatWrite(&f, buf, 16u);
    assert_int_equal((int)ret, 0);
    assert_int_equal((int)f.f_pos, 1024);

    /* No part_write either path. */
    assert_int_equal((int)mock_disk_write_count(), 0);
}

/* Case 4 happy: ALLOC_NO_FAT_CHAIN, single-cluster overwrite (512 bytes). */
static void write_single_cluster_no_chain(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[512];

    memset(buf, 0x11, sizeof(buf));
    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatWrite(&f, buf, 512u);
    assert_int_equal((int)ret, 512);
    assert_int_equal((int)f.f_pos, 512);
    /* RMW issued exactly one read + one write. */
    assert_int_equal((int)mock_disk_read_count(), 1);
    assert_int_equal((int)mock_disk_write_count(), 1);
}

/* Case 4 happy: ALLOC_NO_FAT_CHAIN, write spans clusters 2→3 (1024 bytes). */
static void write_multi_cluster_no_chain(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[1024];

    memset(buf, 0x22, sizeof(buf));
    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 1024u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatWrite(&f, buf, 1024u);
    assert_int_equal((int)ret, 1024);
    assert_int_equal((int)f.f_pos, 1024);
    assert_int_equal((int)mock_disk_read_count(), 2);
    assert_int_equal((int)mock_disk_write_count(), 2);
}

/* Case 4 happy: ALLOC_FAT_CHAIN, three clusters 2→3→4 via FAT chain. */
static void write_fat_chained_three_clusters(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[1536];

    memset(buf, 0x33, sizeof(buf));
    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_FAT_CHAIN, 1536u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatWrite(&f, buf, 1536u);
    assert_int_equal((int)ret, 1536);
    assert_int_equal((int)f.f_pos, 1536);
    /* Three RMW write rounds (FAT walks issue extra reads but no writes). */
    assert_true(mock_disk_write_count() == 3);
}

/* Case 4 / Invariant exfat-write-clamp-by-size: len>remaining → short write. */
static void write_clamp_by_size(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[512];

    memset(buf, 0x44, sizeof(buf));
    make_sbi(&sbi);
    /* File is 100 bytes; f_pos=60 → 40 bytes remain. */
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 100u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 60);

    /* Request 200, must clamp to 40. */
    ssize_t ret = VfsExfatWrite(&f, buf, 200u);
    assert_int_equal((int)ret, 40);
    assert_int_equal((int)f.f_pos, 100);
    /* Single-cluster RMW. */
    assert_int_equal((int)mock_disk_read_count(), 1);
    assert_int_equal((int)mock_disk_write_count(), 1);
}

/*
 * Case 4 + Invariant exfat-write-rmw-cluster-granularity:
 * partial-cluster patch must NOT corrupt bytes outside the patch range.
 */
static void write_partial_offset_in_cluster(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f_w, f_r;
    char             wbuf[100];
    char             rbuf[512];

    memset(wbuf, 0x55, sizeof(wbuf));
    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f_w, &vp, 200);

    ssize_t wr = VfsExfatWrite(&f_w, wbuf, 100u);
    assert_int_equal((int)wr, 100);
    assert_int_equal((int)f_w.f_pos, 300);

    /* Read full cluster back via VfsExfatRead — same image, same vnode. */
    make_filep(&f_r, &vp, 0);
    memset(rbuf, 0, sizeof(rbuf));
    ssize_t rd = VfsExfatRead(&f_r, rbuf, 512u);
    assert_int_equal((int)rd, 512);

    /* Bytes outside [200, 300) remain 0xAA. */
    for (int i = 0; i < 200; i++) {
        assert_int_equal((uint8_t)rbuf[i], WRITE_TEST_CLU2_FILL);
    }
    for (int i = 300; i < 512; i++) {
        assert_int_equal((uint8_t)rbuf[i], WRITE_TEST_CLU2_FILL);
    }
    /* Patched range is 0x55. */
    for (int i = 200; i < 300; i++) {
        assert_int_equal((uint8_t)rbuf[i], 0x55);
    }
}

/*
 * Case 5 / Invariant exfat-write-short-write-on-mid-failure:
 * IO fail on second cluster's part_read after first cluster fully written →
 * return short count (512), f_pos advanced exactly 512.
 */
static void write_mid_loop_io_fail_short_write(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[1024];

    memset(buf, 0x66, sizeof(buf));
    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 1024u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    mock_disk_set_read_fail_at(2u);
    ssize_t ret = VfsExfatWrite(&f, buf, 1024u);
    assert_int_equal((int)ret, 512);
    assert_int_equal((int)f.f_pos, 512);
    /* First cluster's RMW write completed. */
    assert_int_equal((int)mock_disk_write_count(), 1);
}

/*
 * Case 6 / Invariant exfat-write-no-mutate-on-failure +
 * Invariant exfat-write-rmw-read-failure-aborts:
 * First part_read fails, no bytes copied → -EIO; f_pos untouched;
 * NO part_write issued (read failure must NOT trigger the write step).
 */
static void write_first_io_fail_returns_eio(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[512];

    memset(buf, 0x77, sizeof(buf));
    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    mock_disk_set_read_fail_at(1u);
    ssize_t ret = VfsExfatWrite(&f, buf, 512u);
    assert_int_equal((int)ret, -EIO);
    assert_int_equal((int)f.f_pos, 0);
    /* exfat-write-rmw-read-failure-aborts: NO part_write issued. */
    assert_int_equal((int)mock_disk_write_count(), 0);
}

/*
 * Case 6 alt: first part_write fails (read OK) and no bytes already copied →
 * -EIO; f_pos untouched.
 */
static void write_first_part_write_fail_returns_eio(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[512];

    memset(buf, 0x88, sizeof(buf));
    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    mock_disk_set_write_fail_at(1u);
    ssize_t ret = VfsExfatWrite(&f, buf, 512u);
    assert_int_equal((int)ret, -EIO);
    assert_int_equal((int)f.f_pos, 0);
    /* RMW step 1 (read) succeeded; step 3 (write) failed once. */
    assert_int_equal((int)mock_disk_read_count(), 1);
    assert_int_equal((int)mock_disk_write_count(), 1);
}

/*
 * Invariant exfat-write-no-mutate-ei-on-success:
 * On a successful write, ei->size / start_clu / flags are unchanged.
 */
static void write_no_mutate_ei_on_success(void **state)
{
    (void)state;
    exfat_sb_info    sbi;
    exfat_inode_info ei;
    struct Mount     mnt;
    struct Vnode     vp;
    struct file      f;
    char             buf[512];

    memset(buf, 0x99, sizeof(buf));
    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    /* Snapshot before. */
    uint64_t size_before     = ei.size;
    uint32_t start_clu_before = ei.start_clu;
    uint8_t  flags_before    = ei.flags;
    uint32_t type_before     = ei.type;

    ssize_t ret = VfsExfatWrite(&f, buf, 512u);
    assert_int_equal((int)ret, 512);

    /* All ei metadata fields untouched. */
    assert_true(ei.size == size_before);
    assert_true(ei.start_clu == start_clu_before);
    assert_true(ei.flags == flags_before);
    assert_true(ei.type == type_before);
    assert_true(ei.valid_size == 0u);  /* Wave A: never set */
}

/* ---- suite registration ------------------------------------------------ */

const struct CMUnitTest test_write_tests[] = {
    cmocka_unit_test_setup_teardown(write_zero_len_fast_path,            write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_null_filep,                    write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_null_buf,                      write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_null_vnode,                    write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_null_mount_data,               write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_dir_type_rejected,             write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_beyond_eof_returns_zero,       write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_single_cluster_no_chain,       write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_multi_cluster_no_chain,        write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_fat_chained_three_clusters,    write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_clamp_by_size,                 write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_partial_offset_in_cluster,     write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_mid_loop_io_fail_short_write,  write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_first_io_fail_returns_eio,     write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_first_part_write_fail_returns_eio, write_setup, write_teardown),
    cmocka_unit_test_setup_teardown(write_no_mutate_ei_on_success,       write_setup, write_teardown),
};

const size_t test_write_tests_count =
    sizeof(test_write_tests) / sizeof(test_write_tests[0]);
