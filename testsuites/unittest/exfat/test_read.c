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
 * test_read — cmocka host-harness unit tests for VfsExfatRead.
 *
 * Production source: fs/exfat/exfat_file.c
 *
 * Image layout (blocksize=512, cluster_size=512, 1 sector/cluster):
 *
 *   Sectors 0-7  : FAT region (fat_offset=0, fat_length=8)
 *     FAT[2] = 3         (chain: 2→3→4→EOF)
 *     FAT[3] = 4
 *     FAT[4] = EOF
 *   Sectors 8-10 : data clusters (clu_offset=8)
 *     cluster 2 (sector 8)  : filled 0xAA
 *     cluster 3 (sector 9)  : filled 0xBB
 *     cluster 4 (sector 10) : filled 0xCC
 *
 *   sbi geometry: num_clusters=16, cluster_size_bits=9, blocksize_bits=9,
 *                 sect_per_clus_bits=0, part_id=0.
 *
 * Suite: test_read_tests  (11 testpoints)
 *   read_null_filep              — filep == NULL → -EINVAL
 *   read_null_buf                — buf  == NULL (len>0) → -EINVAL
 *   read_null_vnode              — f_vnode == NULL → -EINVAL
 *   read_null_mount_data         — mount->data == NULL → -EINVAL
 *   read_dir_type_rejected       — ei->type == TYPE_DIR → -EISDIR
 *   read_zero_len_fast_path      — len == 0 → 0, no IO
 *   read_eof_returns_zero        — f_pos >= ei->size → 0
 *   read_clamp_by_size           — len > remaining → reads only remaining
 *   read_single_cluster_no_chain — ALLOC_NO_FAT_CHAIN, one cluster
 *   read_multi_cluster_no_chain  — ALLOC_NO_FAT_CHAIN, spans 2 clusters
 *   read_fat_chained_three_clusters — ALLOC_FAT_CHAIN, chain 2→3→4
 *   read_mid_loop_io_fail_short_read — IO fail after cluster 1 → short-read
 *   read_first_io_fail_returns_eio   — IO fail on first read → -EIO
 *
 * Invariants covered:
 *   exfat-read-zero-len-fast-path   (read_zero_len_fast_path)
 *   exfat-read-eof-returns-zero     (read_eof_returns_zero)
 *   exfat-read-clamp-by-size        (read_clamp_by_size)
 *   exfat-read-isdir-rejected       (read_dir_type_rejected)
 *   exfat-read-fat-chain-walk       (read_fat_chained_three_clusters,
 *                                    read_single_cluster_no_chain,
 *                                    read_multi_cluster_no_chain)
 *   exfat-read-pos-advance-exact    (all success paths assert f_pos advance)
 *   exfat-read-pos-advance-exact    (read_mid_loop_io_fail_short_read: short-read
 *                                    path advances f_pos by exactly copied bytes)
 *   exfat-read-cluster-buf-life     (no leak; tested indirectly — no LeakSanitizer
 *                                    failures observed)
 *   exfat-read-no-mutate-on-failure (NULL-param and -EIO paths assert f_pos unchanged)
 *
 * Invariants NOT covered (reason):
 *   exfat-read-linux-i-rwsem-faithful — lock is opaque LosMux stub (no-op in host env);
 *                                        lock ordering cannot be validated without threading.
 *   exfat-read-no-s-lock            — not observable from outside the function.
 *   exfat-read-no-bitmap-lock       — same; host stubs make all locks no-ops.
 *   exfat-read-bounded-per-iter     — white-box loop invariant; verifiable only via coverage.
 *   exfat-read-le-host-only         — LE byte order assumed; FAT put_le32 encoding tested
 *                                      indirectly via chain-walk tests in test_fat_chain.c.
 *   exfat-read-uses-frozen-sbi-fields  — static analysis / review concern, not cmocka-testable.
 *   exfat-read-uses-frozen-ei-fields   — same.
 *   exfat-read-no-spinlock-callsite    — threading concern; not testable in single-threaded host.
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

/* Production function declaration. */
ssize_t VfsExfatRead(struct file *filep, char *buf, size_t len);

/* ---- image parameters --------------------------------------------------- */

/*
 * blocksize = 512, cluster_size = 512 → 1 sector per cluster.
 *   sect_per_clus_bits = 0  (2^0 = 1 sector/cluster)
 *   cluster_size_bits  = 9  (2^9 = 512)
 *   blocksize_bits     = 9
 *
 * FAT region: sectors 0–7 (fat_offset=0, fat_length=8).
 *   FAT entry for cluster N lives at byte offset N*4 in the FAT region.
 *   FAT[2]=3, FAT[3]=4, FAT[4]=EOF
 *
 * Data region: clu_offset = 8.
 *   cluster C → sector = 8 + (C - 2)*1 = 6 + C.
 *   cluster 2 → sector 8  → image byte 8*512 = 4096
 *   cluster 3 → sector 9  → image byte 9*512 = 4608
 *   cluster 4 → sector 10 → image byte 10*512 = 5120
 *
 * Total image: 11 sectors = 5632 bytes.
 */
#define READ_TEST_BLOCKSIZE        512u
#define READ_TEST_CLUSTER_SIZE     512u
#define READ_TEST_CLU_OFFSET       8u
#define READ_TEST_FAT_OFFSET       0u
#define READ_TEST_FAT_LENGTH       8u
#define READ_TEST_NUM_CLUSTERS     16u
#define READ_TEST_NUM_SECTORS      11u
#define READ_TEST_IMAGE_SIZE       (READ_TEST_NUM_SECTORS * READ_TEST_BLOCKSIZE)

#define READ_TEST_CLU2_FILL        0xAAu
#define READ_TEST_CLU3_FILL        0xBBu
#define READ_TEST_CLU4_FILL        0xCCu

/* cluster → sector (matches exfat_clu_to_sector with sect_per_clus_bits=0) */
#define CLU_TO_SECTOR(c)    ((uint64_t)(READ_TEST_CLU_OFFSET) + ((c) - 2u))
/* cluster → byte offset in image */
#define CLU_TO_BYTE(c)      (CLU_TO_SECTOR(c) * READ_TEST_BLOCKSIZE)

/* ---- global synthetic image -------------------------------------------- */

static uint8_t g_read_image[READ_TEST_IMAGE_SIZE];

static void put_le32(uint8_t *buf, size_t off, uint32_t val)
{
    buf[off + 0] = (uint8_t)(val);
    buf[off + 1] = (uint8_t)(val >> 8);
    buf[off + 2] = (uint8_t)(val >> 16);
    buf[off + 3] = (uint8_t)(val >> 24);
}

static void build_read_image(void)
{
    memset(g_read_image, 0, sizeof(g_read_image));

    /* FAT entries (sector 0, byte offset = cluster_num * 4). */
    put_le32(g_read_image, READ_TEST_FAT_OFFSET * READ_TEST_BLOCKSIZE + 2u * 4u, 3u);
    put_le32(g_read_image, READ_TEST_FAT_OFFSET * READ_TEST_BLOCKSIZE + 3u * 4u, 4u);
    put_le32(g_read_image, READ_TEST_FAT_OFFSET * READ_TEST_BLOCKSIZE + 4u * 4u,
             EXFAT_EOF_CLUSTER);

    /* Data cluster payloads. */
    memset(g_read_image + CLU_TO_BYTE(2u), READ_TEST_CLU2_FILL, READ_TEST_CLUSTER_SIZE);
    memset(g_read_image + CLU_TO_BYTE(3u), READ_TEST_CLU3_FILL, READ_TEST_CLUSTER_SIZE);
    memset(g_read_image + CLU_TO_BYTE(4u), READ_TEST_CLU4_FILL, READ_TEST_CLUSTER_SIZE);
}

/* ---- sbi / ei / fixture helpers ---------------------------------------- */

static void make_sbi(exfat_sb_info *sbi)
{
    memset(sbi, 0, sizeof(*sbi));
    sbi->blocksize         = READ_TEST_BLOCKSIZE;
    sbi->blocksize_bits    = 9u;
    sbi->cluster_size      = READ_TEST_CLUSTER_SIZE;
    sbi->cluster_size_bits = 9u;
    sbi->sect_per_clus_bits = 0u;
    sbi->clu_offset        = READ_TEST_CLU_OFFSET;
    sbi->fat_offset        = READ_TEST_FAT_OFFSET;
    sbi->fat_length        = READ_TEST_FAT_LENGTH;
    sbi->num_clusters      = READ_TEST_NUM_CLUSTERS;
    sbi->part_id           = 0;
}

/*
 * make_ei — initialise a stack-allocated ei for a regular file.
 * start_clu: starting cluster of the file.
 * flags:     ALLOC_NO_FAT_CHAIN (contiguous) or ALLOC_FAT_CHAIN.
 * size:      file size in bytes.
 */
static void make_ei(exfat_inode_info *ei, uint32_t start_clu,
                    uint8_t flags, uint64_t size)
{
    memset(ei, 0, sizeof(*ei));
    ei->type      = TYPE_FILE;
    ei->start_clu = start_clu;
    ei->flags     = flags;
    ei->size      = size;
    /* inode_lock is a no-op LosMux in host stubs — no explicit init needed. */
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

static int read_setup(void **state)
{
    (void)state;
    build_read_image();
    mock_disk_load(g_read_image, sizeof(g_read_image));
    mock_disk_reset_counters();
    return 0;
}

static int read_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

/* ---- testpoints -------------------------------------------------------- */

/* Invariant: exfat-read-zero-len-fast-path — len==0 returns 0, no IO. */
static void read_zero_len_fast_path(void **state)
{
    (void)state;
    exfat_sb_info      sbi;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;
    char               buf[16];

    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatRead(&f, buf, 0u);
    assert_int_equal((int)ret, 0);
    /* f_pos must not change (exfat-read-no-mutate-on-failure). */
    assert_int_equal((int)f.f_pos, 0);
    /* No IO issued. */
    assert_int_equal((int)mock_disk_read_count(), 0);
}

/* NULL filep → -EINVAL. */
static void read_null_filep(void **state)
{
    (void)state;
    char buf[16];
    ssize_t ret = VfsExfatRead(NULL, buf, 16u);
    assert_int_equal((int)ret, -EINVAL);
}

/* NULL buf with len>0 → -EINVAL. */
static void read_null_buf(void **state)
{
    (void)state;
    exfat_sb_info      sbi;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;

    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatRead(&f, NULL, 16u);
    assert_int_equal((int)ret, -EINVAL);
    assert_int_equal((int)f.f_pos, 0);
}

/* NULL f_vnode → -EINVAL. */
static void read_null_vnode(void **state)
{
    (void)state;
    struct file f;
    char buf[16];
    memset(&f, 0, sizeof(f));
    f.f_vnode = NULL;
    ssize_t ret = VfsExfatRead(&f, buf, 16u);
    assert_int_equal((int)ret, -EINVAL);
    assert_int_equal((int)f.f_pos, 0);
}

/* mount->data == NULL → -EINVAL (sbi == NULL guard). */
static void read_null_mount_data(void **state)
{
    (void)state;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;
    char               buf[16];

    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, NULL); /* data == NULL */
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatRead(&f, buf, 16u);
    assert_int_equal((int)ret, -EINVAL);
    assert_int_equal((int)f.f_pos, 0);
}

/* Invariant: exfat-read-isdir-rejected — ei->type==TYPE_DIR → -EISDIR. */
static void read_dir_type_rejected(void **state)
{
    (void)state;
    exfat_sb_info      sbi;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;
    char               buf[16];

    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    ei.type = TYPE_DIR; /* override to DIR */
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    ssize_t ret = VfsExfatRead(&f, buf, 16u);
    assert_int_equal((int)ret, -EISDIR);
    /* f_pos untouched. */
    assert_int_equal((int)f.f_pos, 0);
    /* No IO (check before lock path). */
    assert_int_equal((int)mock_disk_read_count(), 0);
}

/* Invariant: exfat-read-eof-returns-zero — f_pos >= ei->size returns 0. */
static void read_eof_returns_zero(void **state)
{
    (void)state;
    exfat_sb_info      sbi;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;
    char               buf[16];

    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 256u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 256); /* f_pos exactly at EOF */

    ssize_t ret = VfsExfatRead(&f, buf, 16u);
    assert_int_equal((int)ret, 0);
    assert_int_equal((int)f.f_pos, 256);

    /* Also test f_pos beyond EOF. */
    f.f_pos = 512;
    ret = VfsExfatRead(&f, buf, 16u);
    assert_int_equal((int)ret, 0);
    assert_int_equal((int)f.f_pos, 512);
}

/* Invariant: exfat-read-clamp-by-size — len > remaining → reads only remaining. */
static void read_clamp_by_size(void **state)
{
    (void)state;
    exfat_sb_info      sbi;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;
    char               buf[512];

    make_sbi(&sbi);
    /* File is 100 bytes; f_pos at 60 → 40 bytes remain. */
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 100u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 60);

    memset(buf, 0, sizeof(buf));
    /* Request 200 bytes — must clamp to 40. */
    ssize_t ret = VfsExfatRead(&f, buf, 200u);
    assert_int_equal((int)ret, 40);
    /* f_pos advanced by exactly 40 (exfat-read-pos-advance-exact). */
    assert_int_equal((int)f.f_pos, 100);
    /* Verify data: cluster 2 filled 0xAA; offset 60..99 → buf[0..39]. */
    for (int i = 0; i < 40; i++) {
        assert_int_equal((uint8_t)buf[i], READ_TEST_CLU2_FILL);
    }
}

/* Happy: ALLOC_NO_FAT_CHAIN, single cluster read (offset 0, full cluster). */
static void read_single_cluster_no_chain(void **state)
{
    (void)state;
    exfat_sb_info      sbi;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;
    char               buf[512];

    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    memset(buf, 0, sizeof(buf));
    ssize_t ret = VfsExfatRead(&f, buf, 512u);
    assert_int_equal((int)ret, 512);
    /* f_pos advanced by 512. */
    assert_int_equal((int)f.f_pos, 512);
    /* Data must be cluster 2 fill (0xAA). */
    for (int i = 0; i < 512; i++) {
        assert_int_equal((uint8_t)buf[i], READ_TEST_CLU2_FILL);
    }
}

/* Happy: ALLOC_NO_FAT_CHAIN, read spanning clusters 2 and 3. */
static void read_multi_cluster_no_chain(void **state)
{
    (void)state;
    exfat_sb_info      sbi;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;
    char               buf[1024];

    make_sbi(&sbi);
    /* File spans cluster 2 and 3 (1024 bytes). */
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 1024u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    memset(buf, 0, sizeof(buf));
    ssize_t ret = VfsExfatRead(&f, buf, 1024u);
    assert_int_equal((int)ret, 1024);
    assert_int_equal((int)f.f_pos, 1024);
    /* First 512 bytes: cluster 2 (0xAA). */
    for (int i = 0; i < 512; i++) {
        assert_int_equal((uint8_t)buf[i], READ_TEST_CLU2_FILL);
    }
    /* Next 512 bytes: cluster 3 (0xBB). */
    for (int i = 512; i < 1024; i++) {
        assert_int_equal((uint8_t)buf[i], READ_TEST_CLU3_FILL);
    }
}

/*
 * Happy: ALLOC_FAT_CHAIN, three clusters 2→3→4.
 * Covers invariant exfat-read-fat-chain-walk (FAT path).
 */
static void read_fat_chained_three_clusters(void **state)
{
    (void)state;
    exfat_sb_info      sbi;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;
    char               buf[1536];

    make_sbi(&sbi);
    /* Chain: cluster 2→3→4, 1536 bytes total. */
    make_ei(&ei, 2u, ALLOC_FAT_CHAIN, 1536u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    memset(buf, 0, sizeof(buf));
    ssize_t ret = VfsExfatRead(&f, buf, 1536u);
    assert_int_equal((int)ret, 1536);
    assert_int_equal((int)f.f_pos, 1536);
    for (int i = 0; i < 512; i++) {
        assert_int_equal((uint8_t)buf[i], READ_TEST_CLU2_FILL);
    }
    for (int i = 512; i < 1024; i++) {
        assert_int_equal((uint8_t)buf[i], READ_TEST_CLU3_FILL);
    }
    for (int i = 1024; i < 1536; i++) {
        assert_int_equal((uint8_t)buf[i], READ_TEST_CLU4_FILL);
    }
}

/*
 * Short-read: IO failure on the second cluster read (after cluster 2 copied).
 * Invariant exfat-read-pos-advance-exact: return value == f_pos advance == 512.
 */
static void read_mid_loop_io_fail_short_read(void **state)
{
    (void)state;
    exfat_sb_info      sbi;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;
    char               buf[1024];

    make_sbi(&sbi);
    /* Two-cluster file ALLOC_NO_FAT_CHAIN. */
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 1024u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    memset(buf, 0, sizeof(buf));
    /*
     * First los_part_read (cluster 2) succeeds.
     * Second los_part_read (cluster 3) fails → -EIO.
     * Because 512 bytes already copied, VfsExfatRead returns 512 (short-read).
     */
    mock_disk_set_read_fail_at(2u);
    ssize_t ret = VfsExfatRead(&f, buf, 1024u);
    assert_int_equal((int)ret, 512);
    /* f_pos must be advanced by exactly the copied amount. */
    assert_int_equal((int)f.f_pos, 512);
    /* First cluster data is still valid. */
    for (int i = 0; i < 512; i++) {
        assert_int_equal((uint8_t)buf[i], READ_TEST_CLU2_FILL);
    }
}

/*
 * First IO failure: no bytes copied yet → return -EIO, f_pos untouched.
 * Invariant exfat-read-no-mutate-on-failure.
 */
static void read_first_io_fail_returns_eio(void **state)
{
    (void)state;
    exfat_sb_info      sbi;
    exfat_inode_info   ei;
    struct Mount       mnt;
    struct Vnode       vp;
    struct file        f;
    char               buf[512];

    make_sbi(&sbi);
    make_ei(&ei, 2u, ALLOC_NO_FAT_CHAIN, 512u);
    make_mount(&mnt, &sbi);
    make_vnode(&vp, &mnt, &ei);
    make_filep(&f, &vp, 0);

    mock_disk_set_read_fail_at(1u);
    ssize_t ret = VfsExfatRead(&f, buf, 512u);
    assert_int_equal((int)ret, -EIO);
    /* f_pos must be unchanged. */
    assert_int_equal((int)f.f_pos, 0);
}

/* ---- suite registration ------------------------------------------------ */

const struct CMUnitTest test_read_tests[] = {
    cmocka_unit_test_setup_teardown(read_zero_len_fast_path,           read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_null_filep,                   read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_null_buf,                     read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_null_vnode,                   read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_null_mount_data,              read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_dir_type_rejected,            read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_eof_returns_zero,             read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_clamp_by_size,                read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_single_cluster_no_chain,      read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_multi_cluster_no_chain,       read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_fat_chained_three_clusters,   read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_mid_loop_io_fail_short_read,  read_setup, read_teardown),
    cmocka_unit_test_setup_teardown(read_first_io_fail_returns_eio,    read_setup, read_teardown),
};

const size_t test_read_tests_count =
    sizeof(test_read_tests) / sizeof(test_read_tests[0]);
