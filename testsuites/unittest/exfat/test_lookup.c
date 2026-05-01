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
 * test_lookup — cmocka host-harness unit tests for VfsExfatLookup.
 *
 * Production source: fs/exfat/exfat_lookup.c
 *
 * Strategy:
 *   Use the standard 6 KiB exfat_test_image_build() image and mutate the
 *   root cluster (sector 9) to inject a {FILE, STREAM, NAME} three-entry
 *   dentry set for "HELLO.TXT" (file) and "SUBDIR" (directory) starting at
 *   dentry slot 2, replacing the EXFAT_UNUSED terminator that normally lives
 *   there.  Root cluster #2 is used as both image root AND as the parent-dir
 *   chain for all tests — valid because exFAT root is itself a directory.
 *
 *   The SetChecksum16 field in set[0].file.checksum is computed locally using
 *   the same CS_DIR_ENTRY algorithm (ROR-1 + add, skip bytes 2-3 of dentry 0).
 *   No dependency on the production exfat_calc_chksum16 symbol at compile-time;
 *   it is only called at test-setup time.
 *
 * Suite: test_lookup_tests  (12 testpoints)
 *   lookup_null_parent            — NULL parent     → -EINVAL
 *   lookup_null_name              — NULL name       → -EINVAL
 *   lookup_null_vpp               — NULL vpp        → -EINVAL
 *   lookup_parent_not_dir         — parent type REG → -EINVAL
 *   lookup_name_too_long          — >EXFAT_MAX_NAME_LEN*4 bytes → -ENAMETOOLONG
 *   lookup_no_match_enoent        — name "NOTEXIST" → -ENOENT
 *   lookup_happy_file             — "HELLO.TXT"     → 0, vp S_IFREG|0644
 *   lookup_happy_dir              — "SUBDIR"        → 0, vp S_IFDIR|0755
 *   lookup_case_insensitive       — "hello.TxT"     → 0 (same file, folded)
 *   lookup_malformed_utf8         — 0xFF start byte → -EINVAL
 *   lookup_io_fail_during_walk    — read error on 1st sector read → -EIO
 *   lookup_no_mutate_on_failure   — *vpp untouched on -ENOENT
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <cmocka.h>

#include "../../../fs/exfat/include/exfat.h"
#include "../../../fs/exfat/include/exfat_raw.h"
#include "exfat_image_builder.h"
#include "mock_disk.h"

/* Production symbol under test. */
int VfsExfatLookup(struct Vnode *parent, const char *name, int len,
                   struct Vnode **vpp);

/* ---- geometry constants (must match exfat_image_builder layout) ---------- */

/*
 * Standard image: 12 sectors × 512 B.
 *   sector 8  = FAT
 *   sector 9  = root cluster #2 (BITMAP @ slot 0, UPCASE @ slot 1)
 *   sector 10 = bitmap cluster #3
 *   sector 11 = upcase cluster #4
 *
 * We mutate sector 9 (root cluster) to add a {FILE,STREAM,NAME} triple from
 * dentry slot 2 onward, so the root directory now contains:
 *   slot 0: BITMAP
 *   slot 1: UPCASE
 *   slot 2: FILE  "HELLO.TXT"  (file, ATTR_ARCHIVE)
 *   slot 3: STREAM extension
 *   slot 4: NAME  "HELLO.TXT"
 *   slot 5: FILE  "SUBDIR"     (directory, ATTR_SUBDIR)
 *   slot 6: STREAM extension
 *   slot 7: NAME  "SUBDIR"
 *   slot 8: EXFAT_UNUSED  (terminator)
 *
 * Each cluster holds 512/32 = 16 dentry slots, so all 9 slots fit in one
 * cluster.  The upcase table (sector 11) covers only 256 entries (identity),
 * so ASCII name comparison works correctly.
 */

#define LTEST_ROOT_CLUSTER      TIMG_ROOT_CLUSTER    /* 2 */
#define LTEST_ROOT_SECTOR       TIMG_CLU_OFFSET      /* 9 */
#define LTEST_DENTRIES_PER_CLU  (TIMG_SECTOR_SIZE / DENTRY_SIZE)   /* 16 */

/* Fake start_clu for the file and dir entries (just need a valid-looking value;
 * lookup does not read the file/dir data). */
#define LTEST_FILE_START_CLU    5u
#define LTEST_DIR_START_CLU     6u
#define LTEST_FILE_SIZE         4096u
#define LTEST_DIR_SIZE          512u

/* ---- local chksum16 (CS_DIR_ENTRY) --------------------------------------- */

/*
 * Same algorithm as production exfat_calc_chksum16 with type=CS_DIR_ENTRY:
 * 1-bit right-rotate + byte add; skip bytes 2-3 of the FIRST dentry only.
 * Covers all `num_entries` dentry structs (each DENTRY_SIZE bytes).
 */
static uint16_t local_set_chksum16(const uint8_t *set_bytes, int num_entries)
{
    uint16_t csum = 0;
    int total = num_entries * DENTRY_SIZE;

    for (int i = 0; i < total; i++) {
        /* CS_DIR_ENTRY: skip bytes 2 and 3 of the first 32-byte entry. */
        if (i == 2 || i == 3) {
            continue;
        }
        csum = (uint16_t)(((csum << 15) | (csum >> 1)) + (uint16_t)set_bytes[i]);
    }
    return csum;
}

/* ---- low-level LE helpers ------------------------------------------------ */

static void put16le(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8u) & 0xFFu);
}

static void put32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8u)  & 0xFFu);
    p[2] = (uint8_t)((v >> 16u) & 0xFFu);
    p[3] = (uint8_t)((v >> 24u) & 0xFFu);
}

static void put64le(uint8_t *p, uint64_t v)
{
    for (int i = 0; i < 8; i++) {
        p[i] = (uint8_t)((v >> (8u * (unsigned)i)) & 0xFFu);
    }
}

/* ---- dentry-set builder helpers ------------------------------------------ */

/*
 * write_dentry_set — write a {FILE, STREAM, NAME} triple into `slot_base`
 * within the root sector buffer `root`.
 *
 * `name_u16[]` is the UTF-16LE filename, `name_len` its character count
 * (≤ EXFAT_FILE_NAME_LEN = 15 for a single NAME dentry).
 * `attr` is the file-attribute word (ATTR_ARCHIVE or ATTR_SUBDIR).
 * `start_clu` is the first data cluster.
 * `size` is the data-stream size in bytes.
 *
 * Returns the next free dentry slot index.
 */
static int write_dentry_set(uint8_t *root, int slot_base,
                            const uint16_t *name_u16, int name_len,
                            uint16_t attr, uint32_t start_clu, uint64_t size)
{
    /* Each dentry is 32 bytes; address within root sector. */
    uint8_t *e0 = root + (size_t)slot_base       * DENTRY_SIZE;  /* FILE    */
    uint8_t *e1 = root + (size_t)(slot_base + 1) * DENTRY_SIZE;  /* STREAM  */
    uint8_t *e2 = root + (size_t)(slot_base + 2) * DENTRY_SIZE;  /* NAME    */

    /* --- FILE primary (0x85) --- */
    memset(e0, 0, DENTRY_SIZE);
    e0[0] = (uint8_t)EXFAT_FILE;
    e0[1] = 2u;                             /* num_ext = 2 (STREAM + NAME) */
    /* e0[2..3] = checksum — filled below */
    put16le(e0 + 4, attr);

    /* --- STREAM secondary (0xC0) --- */
    memset(e1, 0, DENTRY_SIZE);
    e1[0] = (uint8_t)EXFAT_STREAM;
    e1[1] = (uint8_t)ALLOC_NO_FAT_CHAIN;   /* flags: no-FAT-chain (contiguous) */
    /* e1[2] = reserved1: zero */
    e1[3] = (uint8_t)name_len;             /* name_len at stream struct offset 2 → dentry byte 3 */
    /* e1[4..5] = name_hash — zero: validate_dentry_set only checks file.checksum */
    put64le(e1 + 8,  size);                 /* valid_size at offset 8 */
    /* reserved3 at offset 16..19: zero */
    put32le(e1 + 20, start_clu);            /* start_clu */
    put64le(e1 + 24, size);                 /* size */

    /* --- NAME secondary (0xC1) --- */
    memset(e2, 0, DENTRY_SIZE);
    e2[0] = (uint8_t)EXFAT_NAME;
    e2[1] = 0u;                             /* flags */
    for (int i = 0; i < name_len && i < EXFAT_FILE_NAME_LEN; i++) {
        put16le(e2 + 2 + (size_t)i * 2u, name_u16[i]);
    }

    /* Compute and backfill SetChecksum (3 entries × 32 bytes). */
    uint16_t chksum = local_set_chksum16(e0, 3);
    put16le(e0 + 2, chksum);

    return slot_base + 3;
}

/* ---- fixture state ------------------------------------------------------- */

typedef struct {
    exfat_test_image img;
    exfat_sb_info    sbi;
    exfat_inode_info parent_ei;
    struct Mount     mnt;
    struct Vnode     parent_vp;
    /* upcase table: full 65536-entry identity-fold (ASCII a-z → A-Z) */
    uint16_t        *upcase;
} lookup_ctx;

static int lookup_setup(void **state)
{
    lookup_ctx *c = (lookup_ctx *)calloc(1, sizeof(*c));
    if (c == NULL) { return -1; }

    /* Build the base image. */
    exfat_test_image_build(&c->img);

    /* Inject file "HELLO.TXT" at dentry slots 2-4 in root cluster. */
    uint8_t *root = c->img.bytes + LTEST_ROOT_SECTOR * TIMG_SECTOR_SIZE;

    static const uint16_t hello_u16[] = {
        'H','E','L','L','O','.','T','X','T'
    };
    int next_slot = write_dentry_set(root, 2,
                                     hello_u16, 9,
                                     ATTR_ARCHIVE, LTEST_FILE_START_CLU,
                                     LTEST_FILE_SIZE);

    /* Inject directory "SUBDIR" at dentry slots 5-7. */
    static const uint16_t subdir_u16[] = { 'S','U','B','D','I','R' };
    next_slot = write_dentry_set(root, next_slot,
                                 subdir_u16, 6,
                                 ATTR_SUBDIR, LTEST_DIR_START_CLU,
                                 LTEST_DIR_SIZE);

    /* Slot after the two sets: write EXFAT_UNUSED terminator. */
    memset(root + (size_t)next_slot * DENTRY_SIZE, 0, DENTRY_SIZE);

    /* Load image into mock_disk. */
    mock_disk_load(c->img.bytes, sizeof(c->img.bytes));
    mock_disk_reset_counters();

    /* Build upcase table: full 65536 identity, ASCII fold. */
    c->upcase = (uint16_t *)calloc(65536, sizeof(uint16_t));
    if (c->upcase == NULL) { free(c); return -1; }
    for (int i = 0; i < 65536; i++) { c->upcase[i] = (uint16_t)i; }
    for (int i = 'a'; i <= 'z'; i++) { c->upcase[i] = (uint16_t)(i - 'a' + 'A'); }

    /* Populate sbi matching image geometry. */
    memset(&c->sbi, 0, sizeof(c->sbi));
    c->sbi.sect_size_bits     = 9;
    c->sbi.sect_per_clus_bits = 0;
    c->sbi.blocksize          = 512;
    c->sbi.cluster_size       = 512;
    c->sbi.cluster_size_bits  = 9;
    c->sbi.dentries_per_clu   = LTEST_DENTRIES_PER_CLU;
    c->sbi.num_clusters       = TIMG_NUM_CLUSTERS + EXFAT_RESERVED_CLUSTERS;
    c->sbi.fat_offset         = TIMG_FAT_OFFSET;
    c->sbi.fat_length         = 1;
    c->sbi.clu_offset         = TIMG_CLU_OFFSET;
    c->sbi.root_dir           = LTEST_ROOT_CLUSTER;
    c->sbi.num_fats           = 1;
    c->sbi.part_id            = 0;
    c->sbi.vol_utbl           = c->upcase;
    /* fmask=0 → file mode = 0644; dmask=0 → dir mode = 0755 */
    c->sbi.options.fs_fmask   = 0;
    c->sbi.options.fs_dmask   = 0;
    c->sbi.options.fs_uid     = 0;
    c->sbi.options.fs_gid     = 0;

    /* Mount object. */
    memset(&c->mnt, 0, sizeof(c->mnt));
    c->mnt.data = &c->sbi;

    /* Parent inode_info: root directory cluster. */
    memset(&c->parent_ei, 0, sizeof(c->parent_ei));
    c->parent_ei.start_clu  = LTEST_ROOT_CLUSTER;
    c->parent_ei.flags      = (uint8_t)ALLOC_NO_FAT_CHAIN;
    c->parent_ei.size       = TIMG_SECTOR_SIZE;   /* 1 cluster = 512 bytes */
    c->parent_ei.type       = TYPE_DIR;

    /* Parent vnode: directory. */
    memset(&c->parent_vp, 0, sizeof(c->parent_vp));
    c->parent_vp.type        = VNODE_TYPE_DIR;
    c->parent_vp.data        = &c->parent_ei;
    c->parent_vp.originMount = &c->mnt;

    *state = c;
    return 0;
}

static int lookup_teardown(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    mock_disk_unload();
    free(c->upcase);
    free(c);
    return 0;
}

/* ---- testpoints ---------------------------------------------------------- */

/* T01: NULL parent → -EINVAL (Invariant exfat-lookup-null-args). */
static void lookup_null_parent(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    struct Vnode *vpp = (struct Vnode *)0xDEADu;
    (void)c;
    assert_int_equal(VfsExfatLookup(NULL, "HELLO.TXT", 9, &vpp), -EINVAL);
}

/* T02: NULL name → -EINVAL. */
static void lookup_null_name(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    struct Vnode *vpp = (struct Vnode *)0xDEADu;
    assert_int_equal(VfsExfatLookup(&c->parent_vp, NULL, 0, &vpp), -EINVAL);
}

/* T03: NULL vpp → -EINVAL. */
static void lookup_null_vpp(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    assert_int_equal(VfsExfatLookup(&c->parent_vp, "HELLO.TXT", 9, NULL), -EINVAL);
}

/* T04: parent vnode type != DIR → -EINVAL
 * (Invariant exfat-lookup-parent-must-be-dir). */
static void lookup_parent_not_dir(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    struct Vnode *vpp = NULL;

    /* Clone parent, override type to REG. */
    struct Vnode bad_parent = c->parent_vp;
    bad_parent.type = VNODE_TYPE_REG;

    assert_int_equal(VfsExfatLookup(&bad_parent, "HELLO.TXT", 9, &vpp), -EINVAL);
    assert_null(vpp);
}

/* T05: name length > EXFAT_MAX_NAME_LEN * 4 bytes → -ENAMETOOLONG
 * (Invariant exfat-lookup-bounded). */
static void lookup_name_too_long(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    struct Vnode *vpp = NULL;

    /* EXFAT_MAX_NAME_LEN = 255 chars, max UTF-8 = 4 bytes/char → 1020.
     * len > 1020 triggers the pre-lock check. */
    char big_name[1025];
    memset(big_name, 'A', sizeof(big_name) - 1);
    big_name[1024] = '\0';

    assert_int_equal(
        VfsExfatLookup(&c->parent_vp, big_name, 1025, &vpp),
        -ENAMETOOLONG);
    assert_null(vpp);
}

/* T06: name not present in parent directory → -ENOENT
 * (Invariant exfat-lookup-no-match-enoent). */
static void lookup_no_match_enoent(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    struct Vnode *vpp = NULL;

    int r = VfsExfatLookup(&c->parent_vp, "NOTEXIST", 8, &vpp);
    assert_int_equal(r, -ENOENT);
    /* Invariant exfat-lookup-no-mutate-on-failure: *vpp untouched (still NULL). */
    assert_null(vpp);
}

/* T07: happy-path file match "HELLO.TXT"
 * Invariants covered:
 *   exfat-lookup-match-file-mode: vp->mode == S_IFREG | (0644 & ~fmask)
 *   exfat-lookup-vnode-populated: vp non-NULL, vp->type == VNODE_TYPE_REG
 *   exfat-lookup-inode-populated: ei->type == TYPE_FILE, ei->start_clu correct */
static void lookup_happy_file(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    struct Vnode *vpp = NULL;

    int r = VfsExfatLookup(&c->parent_vp, "HELLO.TXT", 9, &vpp);
    assert_int_equal(r, 0);
    assert_non_null(vpp);
    assert_int_equal(vpp->type, VNODE_TYPE_REG);
    assert_true(S_ISREG(vpp->mode));
    /* fmask=0 → permission bits must be 0644 */
    assert_int_equal((int)(vpp->mode & 0777u), 0644);

    /* inode_info sanity */
    exfat_inode_info *ei = (exfat_inode_info *)vpp->data;
    assert_non_null(ei);
    assert_int_equal(ei->type, TYPE_FILE);
    assert_int_equal(ei->start_clu, LTEST_FILE_START_CLU);

    /* VnodeFree to avoid leak (no VFS global list in stub). */
    free(vpp->data);
    free(vpp);
}

/* T08: happy-path directory match "SUBDIR"
 * Invariants covered:
 *   exfat-lookup-match-dir-mode: vp->mode == S_IFDIR | (0755 & ~dmask) */
static void lookup_happy_dir(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    struct Vnode *vpp = NULL;

    int r = VfsExfatLookup(&c->parent_vp, "SUBDIR", 6, &vpp);
    assert_int_equal(r, 0);
    assert_non_null(vpp);
    assert_int_equal(vpp->type, VNODE_TYPE_DIR);
    assert_true(S_ISDIR(vpp->mode));
    /* dmask=0 → permission bits must be 0755 */
    assert_int_equal((int)(vpp->mode & 0777u), 0755);

    exfat_inode_info *ei = (exfat_inode_info *)vpp->data;
    assert_non_null(ei);
    assert_int_equal(ei->type, TYPE_DIR);
    assert_int_equal(ei->start_clu, LTEST_DIR_START_CLU);

    free(vpp->data);
    free(vpp);
}

/* T09: case-insensitive match — input "hello.TxT" matches on-disk "HELLO.TXT"
 * (Invariant exfat-lookup-case-insensitive: fold via sbi->vol_utbl).
 * The upcase fixture maps 'a'-'z' → 'A'-'Z' (ASCII fold only), which is
 * sufficient for the ASCII filename "HELLO.TXT". */
static void lookup_case_insensitive(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    struct Vnode *vpp = NULL;

    int r = VfsExfatLookup(&c->parent_vp, "hello.TxT", 9, &vpp);
    assert_int_equal(r, 0);
    assert_non_null(vpp);
    assert_int_equal(vpp->type, VNODE_TYPE_REG);

    free(vpp->data);
    free(vpp);
}

/* T10: malformed UTF-8 input — 0xFF start byte → exfat_utf8_to_uni → -EINVAL
 * (Invariant exfat-lookup-utf8-decode-gate). */
static void lookup_malformed_utf8(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    struct Vnode *vpp = NULL;
    /* 0xFF is not a valid UTF-8 start byte. */
    const char bad[] = { (char)0xFF, '\0' };

    int r = VfsExfatLookup(&c->parent_vp, bad, 1, &vpp);
    assert_int_equal(r, -EINVAL);
    assert_null(vpp);
}

/* T11: IO failure during dentry walk → -EIO
 * (Invariant exfat-lookup-io-propagation).
 * Read sequence for a 9-slot root dir (sector 9, all dentries co-located):
 *   Read 1: slot 0 (BITMAP)  — skipped (non-FILE)
 *   Read 2: slot 1 (UPCASE)  — skipped (non-FILE)
 *   Read 3: slot 2 (FILE primary peek for "HELLO.TXT") → fail here
 *     → had_io_err=1, entry_idx++, continues
 *   Reads 4-6: slots 3-4 (STREAM/NAME) — skipped (non-FILE primary)
 *   Reads 7-10: slot 5 peek + get_dentry_set(SUBDIR) — read "SUBDIR", no match
 *   Result: had_io_err=1, no match → -EIO.
 * Inject at read #3 to hit the "HELLO.TXT" FILE primary peek. */
static void lookup_io_fail_during_walk(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    struct Vnode *vpp = NULL;

    mock_disk_set_read_fail_at(3);
    int r = VfsExfatLookup(&c->parent_vp, "HELLO.TXT", 9, &vpp);
    assert_int_equal(r, -EIO);
    assert_null(vpp);
}

/* T12: *vpp is untouched (sentinel preserved) when lookup returns an error
 * (Invariant exfat-lookup-no-mutate-on-failure). */
static void lookup_no_mutate_on_failure(void **state)
{
    lookup_ctx *c = (lookup_ctx *)*state;
    /* Use a recognisable sentinel that is != NULL and != a real vnode. */
    struct Vnode *sentinel = (struct Vnode *)0xCAFEBABEul;
    struct Vnode *vpp = sentinel;

    int r = VfsExfatLookup(&c->parent_vp, "NOTEXIST", 8, &vpp);
    assert_int_equal(r, -ENOENT);
    /* *vpp must remain the sentinel — not cleared, not overwritten. */
    assert_ptr_equal(vpp, sentinel);
}

/* ---- suite registration -------------------------------------------------- */

const struct CMUnitTest test_lookup_tests[] = {
    cmocka_unit_test_setup_teardown(lookup_null_parent,          lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_null_name,            lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_null_vpp,             lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_parent_not_dir,       lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_name_too_long,        lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_no_match_enoent,      lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_happy_file,           lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_happy_dir,            lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_case_insensitive,     lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_malformed_utf8,       lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_io_fail_during_walk,  lookup_setup, lookup_teardown),
    cmocka_unit_test_setup_teardown(lookup_no_mutate_on_failure, lookup_setup, lookup_teardown),
};

const size_t test_lookup_tests_count =
    sizeof(test_lookup_tests) / sizeof(test_lookup_tests[0]);
