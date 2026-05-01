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
 * test_readdir — cmocka host-harness unit tests for:
 *   VfsExfatOpendir / VfsExfatReaddir / VfsExfatClosedir / VfsExfatRewinddir
 *
 * Production source: fs/exfat/exfat_readdir.c
 *
 * Image strategy: Strategy A — inline hand-patched image.
 * The standard 6 KiB test image (root cluster = BITMAP + UPCASE + UNUSED) is
 * built via exfat_test_image_build(), then the root cluster is mutated in RAM
 * to insert FILE+STREAM+NAME dentry sets before the UNUSED terminator.  The
 * mutation does NOT touch the standard image struct — each test that needs file
 * entries calls rddir_image_build_with_files() which copies and patches in
 * RAM and calls mock_disk_load() on the result.  Tests that need an empty dir
 * load the vanilla image directly (BITMAP+UPCASE+UNUSED root means no EXFAT_FILE
 * primaries → readdir returns 0 immediately for the file-scanning loop).
 *
 * Testpoints (13):
 *   RD01  opendir on DIR vnode → 0, idir fields initialised
 *   RD02  opendir on FILE vnode (ei->type = TYPE_FILE, vp->type = VNODE_TYPE_REG) → -EINVAL
 *   RD03  opendir NULL vp → -EINVAL
 *   RD04  opendir NULL idir → -EINVAL
 *   RD05  closedir NULL vp → -EINVAL
 *   RD06  closedir NULL idir → -EINVAL
 *   RD07  rewinddir resets fd_int_offset and fd_position to 0
 *   RD08  rewinddir NULL vp → -EINVAL
 *   RD09  readdir NULL vp → -EINVAL
 *   RD10  readdir NULL idir → -EINVAL
 *   RD11  readdir empty dir (no EXFAT_FILE dentries) → 0 filled
 *   RD12  readdir with 1 ASCII file ("HELLO.TXT") → 1 filled, name correct, d_type=DT_REG
 *   RD13  readdir with emoji filename (U+1F600 surrogate pair) → 1 filled, UTF-8 4-byte correct
 *   RD14  rewinddir after readdir → fd_int_offset == 0, second readdir returns same count
 *   RD15  IO failure on primary dentry read → entry skipped; end-of-dir returns 0
 *         (injected via mock_disk_set_read_fail_at after the BITMAP read)
 */

/* Enable glibc extensions (DT_REG, DT_DIR enum values in <dirent.h>). */
#define _GNU_SOURCE

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <stdlib.h>
#include <dirent.h>
#include <cmocka.h>

#include "../../../fs/exfat/include/exfat.h"
#include "../../../fs/exfat/include/exfat_raw.h"
#include "fs/dirent_fs.h"
#include "exfat_image_builder.h"
#include "mock_disk.h"

/* Production function declarations (exfat_readdir.c not yet in PROD_SRCS;
 * declared here so the -c compile check resolves symbols against the TU). */
int VfsExfatOpendir(struct Vnode *vp, struct fs_dirent_s *idir);
int VfsExfatReaddir(struct Vnode *vp, struct fs_dirent_s *idir);
int VfsExfatClosedir(struct Vnode *vp, struct fs_dirent_s *idir);
int VfsExfatRewinddir(struct Vnode *vp, struct fs_dirent_s *idir);

/* ============================================================
 * Geometry helpers (match exfat_image_builder layout)
 * ============================================================ */

static void make_sbi_rddir(exfat_sb_info *sbi)
{
    memset(sbi, 0, sizeof(*sbi));
    sbi->sect_size_bits     = 9;
    sbi->sect_per_clus_bits = 0;
    sbi->blocksize          = 512;
    sbi->cluster_size       = 512;
    sbi->cluster_size_bits  = 9;
    sbi->dentries_per_clu   = 512u >> DENTRY_SIZE_BITS;   /* 16 */
    sbi->num_clusters       = TIMG_NUM_CLUSTERS + EXFAT_RESERVED_CLUSTERS;
    sbi->fat_offset         = TIMG_FAT_OFFSET;
    sbi->fat_length         = 1;
    sbi->clu_offset         = TIMG_CLU_OFFSET;
    sbi->root_dir           = TIMG_ROOT_CLUSTER;
    sbi->num_fats           = 1;
    sbi->part_id            = 0;
}

/* Build a DIR vnode + ei pointing at the root cluster. */
static void make_dir_vnode(struct Vnode *vp, exfat_inode_info *ei,
                           struct Mount *mnt, exfat_sb_info *sbi)
{
    memset(ei,  0, sizeof(*ei));
    ei->type       = TYPE_DIR;
    ei->start_clu  = TIMG_ROOT_CLUSTER;
    ei->flags      = (uint8_t)ALLOC_NO_FAT_CHAIN;
    ei->size       = 512;   /* 1 cluster */

    memset(mnt, 0, sizeof(*mnt));
    mnt->data = sbi;

    memset(vp,  0, sizeof(*vp));
    vp->type        = VNODE_TYPE_DIR;
    vp->data        = ei;
    vp->originMount = mnt;
}

/* Build an idir ready for up to HOST_STUB_MAX_DIRENT_NUM results. */
static void make_idir(struct fs_dirent_s *idir, int read_cnt)
{
    memset(idir, 0, sizeof(*idir));
    idir->read_cnt = read_cnt;
}

/* ============================================================
 * Dentry-set helpers for inline image mutation (Strategy A)
 * ============================================================ */

static void put_u16_le_rd(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
}

/* Compute SetChecksum (CS_DIR_ENTRY): ROR1+add over all bytes, skip bytes
 * 2-3 of the first entry (the checksum field itself). */
static uint16_t compute_set_chksum(const uint8_t *set_bytes, int total_entries)
{
    uint16_t csum = 0;
    int total_bytes = total_entries * DENTRY_SIZE;
    for (int i = 0; i < total_bytes; i++) {
        if (i == 2 || i == 3) {
            continue;   /* skip SetChecksum field */
        }
        csum = (uint16_t)(((csum << 15) | (csum >> 1)) + (uint16_t)set_bytes[i]);
    }
    return csum;
}

/*
 * Write a FILE + STREAM + NAME×1 dentry set for an ASCII name (≤15 chars)
 * into `dst` (must have space for 3 × DENTRY_SIZE bytes).
 * attr: ATTR_ARCHIVE for regular file, ATTR_ARCHIVE|ATTR_SUBDIR for dir.
 */
static void write_file_dentry_set(uint8_t *dst, const char *name_ascii,
                                   uint16_t attr)
{
    int name_len = (int)strlen(name_ascii);
    uint8_t set[3 * DENTRY_SIZE];

    memset(set, 0, sizeof(set));

    /* [0] Primary FILE dentry */
    set[0] = (uint8_t)EXFAT_FILE;
    set[1] = 2u;                        /* num_ext = 2 */
    /* checksum at [2..3] — filled after */
    put_u16_le_rd(set + 4, attr);

    /* [1] STREAM extension */
    set[DENTRY_SIZE + 0] = (uint8_t)EXFAT_STREAM;
    set[DENTRY_SIZE + 1] = (uint8_t)ALLOC_NO_FAT_CHAIN;   /* flags */
    set[DENTRY_SIZE + 2] = 0u;                             /* reserved1 */
    set[DENTRY_SIZE + 3] = (uint8_t)name_len;             /* name_len */
    /* valid_size / size = 0 (empty file, OK for readdir) */

    /* [2] NAME dentry */
    set[2 * DENTRY_SIZE + 0] = (uint8_t)EXFAT_NAME;
    set[2 * DENTRY_SIZE + 1] = 0u;     /* flags */
    for (int i = 0; i < name_len; i++) {
        /* LE UTF-16: ASCII chars are direct */
        put_u16_le_rd(set + 2 * DENTRY_SIZE + 2 + i * 2,
                      (uint16_t)(uint8_t)name_ascii[i]);
    }

    /* Compute and write SetChecksum. */
    uint16_t csum = compute_set_chksum(set, 3);
    put_u16_le_rd(set + 2, csum);

    memcpy(dst, set, sizeof(set));
}

/*
 * Write a FILE + STREAM + NAME×1 dentry set where the filename is a single
 * surrogate pair (U+1F600 = 0xD83D 0xDE00 in UTF-16).  name_len in the stream
 * entry = 2 (two UTF-16 code units).
 */
static void write_emoji_dentry_set(uint8_t *dst)
{
    uint8_t set[3 * DENTRY_SIZE];

    memset(set, 0, sizeof(set));

    /* [0] FILE primary */
    set[0] = (uint8_t)EXFAT_FILE;
    set[1] = 2u;
    put_u16_le_rd(set + 4, (uint16_t)ATTR_ARCHIVE);

    /* [1] STREAM */
    set[DENTRY_SIZE + 0] = (uint8_t)EXFAT_STREAM;
    set[DENTRY_SIZE + 1] = (uint8_t)ALLOC_NO_FAT_CHAIN;
    set[DENTRY_SIZE + 3] = 2u;   /* name_len = 2 UTF-16 code units */

    /* [2] NAME: U+1F600 as surrogate pair 0xD83D 0xDE00 */
    set[2 * DENTRY_SIZE + 0] = (uint8_t)EXFAT_NAME;
    put_u16_le_rd(set + 2 * DENTRY_SIZE + 2, 0xD83Du);   /* high surrogate */
    put_u16_le_rd(set + 2 * DENTRY_SIZE + 4, 0xDE00u);   /* low surrogate  */

    uint16_t csum = compute_set_chksum(set, 3);
    put_u16_le_rd(set + 2, csum);

    memcpy(dst, set, sizeof(set));
}

/*
 * Build a test image whose root cluster starts with `num_sets` file dentry
 * sets then an UNUSED terminator.  Caller fills each set via the callbacks
 * already written into `root_base`.  Here we support one or two entries.
 *
 * Layout in root cluster (sector TIMG_CLU_OFFSET):
 *   offset 0                   : set 0 (3 × 32 = 96 bytes)
 *   offset 96   (if num_sets≥2): set 1 (96 bytes)
 *   offset 192+                : UNUSED (0x00)
 */
typedef struct {
    uint8_t bytes[TIMG_TOTAL_BYTES];
} rddir_image;

static void rddir_image_build_one_ascii(rddir_image *img, const char *name,
                                        uint16_t attr)
{
    exfat_test_image base;
    exfat_test_image_build(&base);
    memcpy(img->bytes, base.bytes, TIMG_TOTAL_BYTES);

    uint8_t *root = img->bytes + TIMG_CLU_OFFSET * TIMG_SECTOR_SIZE;
    /* Clear the first 4 dentry slots (128 bytes) so previous BITMAP/UPCASE
     * content doesn't confuse the scanner.  The root cluster only has
     * BITMAP(0) + UPCASE(1) + UNUSED(2) in the standard image — we want our
     * FILE set at index 0 for the readdir test.  We leave index 3+ as zero
     * (UNUSED) which terminates the scan naturally. */
    memset(root, 0, 4 * DENTRY_SIZE);
    write_file_dentry_set(root, name, attr);
    /* index 3 = 0x00 = EXFAT_UNUSED → terminates scan */
}

static void rddir_image_build_two(rddir_image *img,
                                   const char *name0, uint16_t attr0,
                                   const char *name1, uint16_t attr1)
{
    exfat_test_image base;
    exfat_test_image_build(&base);
    memcpy(img->bytes, base.bytes, TIMG_TOTAL_BYTES);

    uint8_t *root = img->bytes + TIMG_CLU_OFFSET * TIMG_SECTOR_SIZE;
    memset(root, 0, 7 * DENTRY_SIZE);
    write_file_dentry_set(root + 0 * DENTRY_SIZE * 3, name0, attr0);
    write_file_dentry_set(root + 1 * DENTRY_SIZE * 3, name1, attr1);
    /* index 6 = 0x00 = EXFAT_UNUSED */
}

static void rddir_image_build_emoji(rddir_image *img)
{
    exfat_test_image base;
    exfat_test_image_build(&base);
    memcpy(img->bytes, base.bytes, TIMG_TOTAL_BYTES);

    uint8_t *root = img->bytes + TIMG_CLU_OFFSET * TIMG_SECTOR_SIZE;
    memset(root, 0, 4 * DENTRY_SIZE);
    write_emoji_dentry_set(root);
}

/* ============================================================
 * Setup / teardown
 * ============================================================ */

static int rddir_setup_empty(void **state)
{
    (void)state;
    /* Standard image: root cluster has BITMAP+UPCASE+UNUSED — no EXFAT_FILE. */
    exfat_test_image img;
    exfat_test_image_build(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();
    return 0;
}

static int rddir_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

/* ============================================================
 * RD01 — opendir on DIR vnode → 0, idir fields initialised
 * ============================================================ */
static void test_opendir_dir_ok(void **state)
{
    (void)state;
    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);

    struct fs_dirent_s idir;
    make_idir(&idir, 4);
    /* Pre-poison the cursor fields so we know Opendir resets them. */
    idir.fd_int_offset = 99;
    idir.fd_position   = 7;

    int r = VfsExfatOpendir(&vp, &idir);
    assert_int_equal(r, 0);
    assert_int_equal((int)idir.fd_int_offset, 0);
    assert_int_equal((int)idir.fd_position,   0);
    assert_null(idir.u.fs_dir);
}

/* ============================================================
 * RD02 — opendir on REG vnode (not DIR) → -EINVAL
 * ============================================================ */
static void test_opendir_not_dir(void **state)
{
    (void)state;
    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);

    /* Override: pretend this vnode is a regular file. */
    vp.type  = VNODE_TYPE_REG;
    ei.type  = TYPE_FILE;

    struct fs_dirent_s idir;
    make_idir(&idir, 4);

    int r = VfsExfatOpendir(&vp, &idir);
    assert_int_equal(r, -EINVAL);
}

/* ============================================================
 * RD03 — opendir NULL vp → -EINVAL
 * ============================================================ */
static void test_opendir_null_vp(void **state)
{
    (void)state;
    struct fs_dirent_s idir;
    make_idir(&idir, 4);
    assert_int_equal(VfsExfatOpendir(NULL, &idir), -EINVAL);
}

/* ============================================================
 * RD04 — opendir NULL idir → -EINVAL
 * ============================================================ */
static void test_opendir_null_idir(void **state)
{
    (void)state;
    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);
    assert_int_equal(VfsExfatOpendir(&vp, NULL), -EINVAL);
}

/* ============================================================
 * RD05 — closedir NULL vp → -EINVAL
 * ============================================================ */
static void test_closedir_null_vp(void **state)
{
    (void)state;
    struct fs_dirent_s idir;
    make_idir(&idir, 4);
    assert_int_equal(VfsExfatClosedir(NULL, &idir), -EINVAL);
}

/* ============================================================
 * RD06 — closedir NULL idir → -EINVAL
 * ============================================================ */
static void test_closedir_null_idir(void **state)
{
    (void)state;
    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);
    assert_int_equal(VfsExfatClosedir(&vp, NULL), -EINVAL);
}

/* ============================================================
 * RD07 — rewinddir resets fd_int_offset and fd_position to 0
 * ============================================================ */
static void test_rewinddir_resets(void **state)
{
    (void)state;
    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);

    struct fs_dirent_s idir;
    make_idir(&idir, 4);
    idir.fd_int_offset = 42;
    idir.fd_position   = 13;

    int r = VfsExfatRewinddir(&vp, &idir);
    assert_int_equal(r, 0);
    assert_int_equal((int)idir.fd_int_offset, 0);
    assert_int_equal((int)idir.fd_position,   0);
}

/* ============================================================
 * RD08 — rewinddir NULL vp → -EINVAL
 * ============================================================ */
static void test_rewinddir_null_vp(void **state)
{
    (void)state;
    struct fs_dirent_s idir;
    make_idir(&idir, 4);
    idir.fd_int_offset = 5;
    assert_int_equal(VfsExfatRewinddir(NULL, &idir), -EINVAL);
}

/* ============================================================
 * RD09 — readdir NULL vp → -EINVAL
 * ============================================================ */
static void test_readdir_null_vp(void **state)
{
    (void)state;
    struct fs_dirent_s idir;
    make_idir(&idir, 4);
    assert_int_equal(VfsExfatReaddir(NULL, &idir), -EINVAL);
}

/* ============================================================
 * RD10 — readdir NULL idir → -EINVAL
 * ============================================================ */
static void test_readdir_null_idir(void **state)
{
    (void)state;
    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);
    assert_int_equal(VfsExfatReaddir(&vp, NULL), -EINVAL);
}

/* ============================================================
 * RD11 — readdir on empty dir (standard image: BITMAP/UPCASE at idx 0/1,
 *         UNUSED at idx 2) → scanner skips non-FILE and hits UNUSED → 0
 * ============================================================ */
static void test_readdir_empty_dir(void **state)
{
    (void)state;
    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);

    struct fs_dirent_s idir;
    make_idir(&idir, 4);

    int r = VfsExfatReaddir(&vp, &idir);
    /* BITMAP (0x81) and UPCASE (0x82) are not EXFAT_FILE (0x85) → skipped.
     * UNUSED (0x00) → break.  filled == 0, had_io_err == 0 → return 0. */
    assert_int_equal(r, 0);
}

/* ============================================================
 * RD12 — readdir with 1 ASCII file → name correct, d_type=DT_REG
 * ============================================================ */
static void test_readdir_one_ascii_file(void **state)
{
    (void)state;
    rddir_image img;
    rddir_image_build_one_ascii(&img, "HELLO.TXT", (uint16_t)ATTR_ARCHIVE);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();

    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);
    /* Root cluster ei->size = 512, start_clu = TIMG_ROOT_CLUSTER = 2 */

    struct fs_dirent_s idir;
    make_idir(&idir, 4);

    int r = VfsExfatReaddir(&vp, &idir);
    assert_int_equal(r, 1);
    assert_string_equal(idir.fd_dir[0].d_name, "HELLO.TXT");
    assert_int_equal((int)idir.fd_dir[0].d_type, DT_REG);
    /* fd_position incremented once. */
    assert_int_equal((int)idir.fd_position, 1);
    /* Cursor advanced past the 3-dentry set. */
    assert_int_equal((int)idir.fd_int_offset, 3);

    mock_disk_unload();
}

/* ============================================================
 * RD13 — readdir with emoji filename (U+1F600 surrogate pair)
 *         → 1 filled, d_name contains 4-byte UTF-8 sequence F0 9F 98 80
 * ============================================================ */
static void test_readdir_emoji_filename(void **state)
{
    (void)state;
    rddir_image img;
    rddir_image_build_emoji(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();

    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);

    struct fs_dirent_s idir;
    make_idir(&idir, 4);

    int r = VfsExfatReaddir(&vp, &idir);
    assert_int_equal(r, 1);
    /* U+1F600 in UTF-8: F0 9F 98 80 */
    assert_int_equal((unsigned char)idir.fd_dir[0].d_name[0], 0xF0u);
    assert_int_equal((unsigned char)idir.fd_dir[0].d_name[1], 0x9Fu);
    assert_int_equal((unsigned char)idir.fd_dir[0].d_name[2], 0x98u);
    assert_int_equal((unsigned char)idir.fd_dir[0].d_name[3], 0x80u);
    assert_int_equal((unsigned char)idir.fd_dir[0].d_name[4], 0x00u);
    assert_int_equal((int)idir.fd_dir[0].d_type, DT_REG);

    mock_disk_unload();
}

/* ============================================================
 * RD14 — rewinddir after readdir restores cursor; second readdir
 *         returns the same count (idempotent scan from start)
 * ============================================================ */
static void test_rewinddir_after_readdir(void **state)
{
    (void)state;
    rddir_image img;
    rddir_image_build_one_ascii(&img, "FOO.TXT", (uint16_t)ATTR_ARCHIVE);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();

    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);

    struct fs_dirent_s idir;
    make_idir(&idir, 4);

    /* First pass. */
    int r1 = VfsExfatReaddir(&vp, &idir);
    assert_int_equal(r1, 1);
    int offset_after = (int)idir.fd_int_offset;
    assert_true(offset_after > 0);

    /* Rewind. */
    int rw = VfsExfatRewinddir(&vp, &idir);
    assert_int_equal(rw, 0);
    assert_int_equal((int)idir.fd_int_offset, 0);
    assert_int_equal((int)idir.fd_position,   0);

    /* Second pass from offset 0 should again return 1 entry. */
    idir.read_cnt = 4;
    int r2 = VfsExfatReaddir(&vp, &idir);
    assert_int_equal(r2, 1);
    assert_string_equal(idir.fd_dir[0].d_name, "FOO.TXT");

    mock_disk_unload();
}

/* ============================================================
 * RD15 — IO failure during primary dentry read → entry skipped;
 *         readdir returns 0 (empty result, no hard error for the
 *         case where only IO errors were seen and filled==0 → -EIO)
 *
 * The read sequence for this image is:
 *   read 1: dentry idx 0 of root cluster (our FILE dentry)
 * We inject a failure on read 1.  The code path in Readdir:
 *   err == -EIO → had_io_err=1, entry_idx++, continue.
 * After the loop: filled==0, had_io_err==1 → ret=EIO → return -EIO.
 * ============================================================ */
static void test_readdir_io_fail_returns_eio(void **state)
{
    (void)state;
    rddir_image img;
    rddir_image_build_one_ascii(&img, "ERR.TXT", (uint16_t)ATTR_ARCHIVE);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();

    exfat_sb_info sbi;    make_sbi_rddir(&sbi);
    exfat_inode_info ei;
    struct Mount mnt;
    struct Vnode vp;
    make_dir_vnode(&vp, &ei, &mnt, &sbi);

    struct fs_dirent_s idir;
    make_idir(&idir, 4);

    /* Fail the 1st disk read (the primary dentry fetch at entry_idx 0). */
    mock_disk_set_read_fail_at(1);

    int r = VfsExfatReaddir(&vp, &idir);
    /* filled==0, had_io_err==1 → return -EIO. */
    assert_int_equal(r, -EIO);

    mock_disk_unload();
}

/* ============================================================
 * Suite table
 * ============================================================ */

const struct CMUnitTest test_readdir_tests[] = {
    /* opendir — 4 testpoints */
    cmocka_unit_test_setup_teardown(test_opendir_dir_ok,        rddir_setup_empty, rddir_teardown),
    cmocka_unit_test_setup_teardown(test_opendir_not_dir,       rddir_setup_empty, rddir_teardown),
    cmocka_unit_test_setup_teardown(test_opendir_null_vp,       rddir_setup_empty, rddir_teardown),
    cmocka_unit_test_setup_teardown(test_opendir_null_idir,     rddir_setup_empty, rddir_teardown),
    /* closedir — 2 testpoints */
    cmocka_unit_test_setup_teardown(test_closedir_null_vp,      rddir_setup_empty, rddir_teardown),
    cmocka_unit_test_setup_teardown(test_closedir_null_idir,    rddir_setup_empty, rddir_teardown),
    /* rewinddir — 2 testpoints */
    cmocka_unit_test_setup_teardown(test_rewinddir_resets,      rddir_setup_empty, rddir_teardown),
    cmocka_unit_test_setup_teardown(test_rewinddir_null_vp,     rddir_setup_empty, rddir_teardown),
    /* readdir NULL guards — 2 testpoints */
    cmocka_unit_test_setup_teardown(test_readdir_null_vp,       rddir_setup_empty, rddir_teardown),
    cmocka_unit_test_setup_teardown(test_readdir_null_idir,     rddir_setup_empty, rddir_teardown),
    /* readdir functional — 5 testpoints (each loads its own image) */
    cmocka_unit_test_setup_teardown(test_readdir_empty_dir,     rddir_setup_empty, rddir_teardown),
    cmocka_unit_test(test_readdir_one_ascii_file),
    cmocka_unit_test(test_readdir_emoji_filename),
    cmocka_unit_test(test_rewinddir_after_readdir),
    cmocka_unit_test(test_readdir_io_fail_returns_eio),
};

const size_t test_readdir_tests_count =
    sizeof(test_readdir_tests) / sizeof(test_readdir_tests[0]);
