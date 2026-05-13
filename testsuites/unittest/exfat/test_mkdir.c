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
 * test_mkdir — exfat_calc_num_entries / zeroed_cluster / alloc_new_dir /
 * init_dir_entry / init_ext_entry / add_entry / VfsExfatMkdir
 * (Wave B Stage 4e).
 *
 * Strategy: build the standard 6 KiB synthetic image (root cluster has
 * BITMAP at slot 0, UPCASE at slot 1, UNUSED terminator at slot 2 — slots
 * 2..15 free for the new dentry-set). For mkdir end-to-end tests we wire a
 * full sbi + ei + vnode harness (mirroring test_truncate_vop pattern) so
 * vol_flags bracket runs cleanly.
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sys/stat.h>
#include <cmocka.h>

#include "../../../fs/exfat/include/exfat.h"
#include "../../../fs/exfat/include/exfat_raw.h"
#include "exfat_image_builder.h"
#include "mock_disk.h"

/* ---- SUT forward decls -------------------------------------------------- */
int exfat_calc_num_entries(const struct exfat_uni_name *p_uniname);
int exfat_zeroed_cluster(exfat_sb_info *sbi, uint32_t clu);
int exfat_alloc_new_dir(exfat_sb_info *sbi, exfat_chain *clu_out);
int exfat_init_dir_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                         int entry, uint32_t type, uint32_t start_clu,
                         uint64_t size);
int exfat_init_ext_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                         int entry, int num_entries,
                         const struct exfat_uni_name *p_uniname);
int exfat_add_entry(exfat_sb_info *sbi, struct Vnode *parent_vp,
                    const char *name, uint32_t type,
                    struct exfat_dir_entry *info);
int VfsExfatMkdir(struct Vnode *parent_vp, const char *name,
                  mode_t mode, struct Vnode **vpp);
int VfsExfatCreate(struct Vnode *parent_vp, const char *name,
                   int mode, struct Vnode **vpp);

/* helper used by chksum verification. */
int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                     int entry_idx, struct exfat_dentry *out,
                     uint64_t *out_sector);

/* ============================================================
 * calc_num_entries — pure compute, no setup
 * ============================================================ */

static void test_calc_num_entries_len1(void **state)
{
    (void)state;
    struct exfat_uni_name u;
    memset(&u, 0, sizeof(u));
    u.name_len = 1u;
    assert_int_equal(exfat_calc_num_entries(&u), 3);
}

static void test_calc_num_entries_len15(void **state)
{
    (void)state;
    struct exfat_uni_name u;
    memset(&u, 0, sizeof(u));
    u.name_len = 15u;
    assert_int_equal(exfat_calc_num_entries(&u), 3);
}

static void test_calc_num_entries_len16(void **state)
{
    (void)state;
    struct exfat_uni_name u;
    memset(&u, 0, sizeof(u));
    u.name_len = 16u;
    assert_int_equal(exfat_calc_num_entries(&u), 4);
}

static void test_calc_num_entries_len255(void **state)
{
    (void)state;
    struct exfat_uni_name u;
    memset(&u, 0, sizeof(u));
    u.name_len = 255u;
    assert_int_equal(exfat_calc_num_entries(&u), 19);
}

static void test_calc_num_entries_einval(void **state)
{
    (void)state;
    assert_int_equal(exfat_calc_num_entries(NULL), -EINVAL);

    struct exfat_uni_name u;
    memset(&u, 0, sizeof(u));
    u.name_len = 0u;
    assert_int_equal(exfat_calc_num_entries(&u), -EINVAL);
}

/* ============================================================
 * zeroed_cluster harness — uses default test image
 * ============================================================ */

static void make_sbi_default(exfat_sb_info *sbi)
{
    memset(sbi, 0, sizeof(*sbi));
    sbi->sect_size_bits     = 9;
    sbi->sect_per_clus_bits = 0;
    sbi->blocksize          = 512;
    sbi->cluster_size       = 512;
    sbi->cluster_size_bits  = 9;
    sbi->dentries_per_clu   = 512u >> DENTRY_SIZE_BITS;
    sbi->num_clusters       = TIMG_NUM_CLUSTERS + EXFAT_RESERVED_CLUSTERS;
    sbi->fat_offset         = TIMG_FAT_OFFSET;
    sbi->fat_length         = 1;
    sbi->fat2_offset        = TIMG_FAT_OFFSET;
    sbi->clu_offset         = TIMG_CLU_OFFSET;
    sbi->root_dir           = TIMG_ROOT_CLUSTER;
    sbi->num_fats           = 1;
    sbi->part_id            = 0;
}

static int img_setup(void **state)
{
    (void)state;
    exfat_test_image img;
    exfat_test_image_build(&img);
    mock_disk_load(img.bytes, sizeof(img.bytes));
    mock_disk_reset_counters();
    return 0;
}

static int img_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

static void test_zeroed_cluster_success(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    make_sbi_default(&sbi);

    /* Cluster 5 is in the data region (clu_offset=9, blocksize=512;
     * sect = 9 + (5-2) = 12 — but image is only 12 sectors). Actually
     * clusters 2..13 reachable; pick cluster 5 → sector 12 — bounds-fail.
     * Use cluster 2 (root) instead — sector 9. */
    int rc = exfat_zeroed_cluster(&sbi, 2u);
    assert_int_equal(rc, 0);
    /* 1 sector per cluster → exactly 1 write call. */
    assert_int_equal((int)mock_disk_write_count(), 1);
}

static void test_zeroed_cluster_einval_null_sbi(void **state)
{
    (void)state;
    assert_int_equal(exfat_zeroed_cluster(NULL, 2u), -EINVAL);
}

static void test_zeroed_cluster_einval_bad_clu(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    make_sbi_default(&sbi);
    /* clu < EXFAT_FIRST_CLUSTER */
    assert_int_equal(exfat_zeroed_cluster(&sbi, 1u), -EINVAL);
    /* clu >= num_clusters */
    assert_int_equal(exfat_zeroed_cluster(&sbi, sbi.num_clusters + 1u), -EINVAL);
}

static void test_zeroed_cluster_eio(void **state)
{
    (void)state;
    exfat_sb_info sbi;
    make_sbi_default(&sbi);
    mock_disk_set_write_fail_at(1);   /* fail very first write */
    int rc = exfat_zeroed_cluster(&sbi, 2u);
    assert_int_equal(rc, -EIO);
}

/* ============================================================
 * alloc_new_dir / mkdir end-to-end harness — full sbi+ei+vnode
 * Synthetic 16-sector image: boot(1) + FAT(1) + bitmap_cluster(1) +
 * data(13). Cluster_size=512, blocksize=512, sect_per_clus_bits=0.
 *
 * Layout (sectors):
 *   0       boot   (unused — vol_flags is read direct from boot_buf, separate)
 *   1       FAT
 *   2       bitmap (vol_amap mirrored here)
 *   3..15   data clusters (cluster #2 = parent dir at sector 3)
 *
 * Cluster numbering: data region starts at clu_offset=3, so cluster #2
 * lives at sector 3, cluster #3 at sector 4, ..., cluster #14 at sector 15.
 * ============================================================ */

#define MK_BLOCKSIZE       512u
#define MK_CLUSTER_SIZE    512u
#define MK_PART_ID         42
#define MK_NUM_CLUSTERS    14u            /* clusters 2..15 reachable -> +reserved 2 -> 14 */
#define MK_MAP_CLU         2u             /* bitmap lives in cluster #2 sectors */
#define MK_MAP_SECTORS     1u
#define MK_FAT_OFFSET      1u
#define MK_FAT_LENGTH      1u
#define MK_CLU_OFFSET      3u             /* data starts at sector 3 → cluster #2 */
#define MK_NUM_FATS        1u
#define MK_NSECTORS        16u
#define MK_IMG_LEN         (MK_NSECTORS * MK_BLOCKSIZE)
#define MK_PARENT_CLU      2u             /* cluster #2 is parent dir */

static exfat_sb_info     *g_sbi;
static exfat_inode_info  *g_pei;
static uint8_t           *g_vol_amap;
static uint8_t           *g_boot_buf;
static uint8_t            g_image[MK_IMG_LEN];
static struct Mount       g_mount;
static struct Vnode       g_pvnode;

static void mk_fat_set(uint32_t clu, uint32_t next_clu)
{
    uint64_t off = (uint64_t)MK_FAT_OFFSET * MK_BLOCKSIZE + (uint64_t)clu * 4u;
    g_image[off + 0] = (uint8_t)(next_clu & 0xFFu);
    g_image[off + 1] = (uint8_t)((next_clu >> 8) & 0xFFu);
    g_image[off + 2] = (uint8_t)((next_clu >> 16) & 0xFFu);
    g_image[off + 3] = (uint8_t)((next_clu >> 24) & 0xFFu);
}

static int mk_setup(void **state)
{
    (void)state;
    memset(g_image, 0u, sizeof(g_image));
    /* FAT[0],[1] — sentinels; rest stays FREE (0). */
    mk_fat_set(0, 0xFFFFFFF8u);
    mk_fat_set(1, 0xFFFFFFFFu);
    /* parent dir cluster #2 — single cluster, terminator at slot 0
     * (whole cluster zero already). FAT[2] = EOF to mark end-of-chain. */
    mk_fat_set(MK_PARENT_CLU, EXFAT_EOF_CLUSTER);

    mock_disk_load(g_image, sizeof(g_image));
    mock_disk_reset_counters();

    g_sbi      = (exfat_sb_info *)calloc(1u, sizeof(*g_sbi));
    assert_non_null(g_sbi);
    g_pei      = (exfat_inode_info *)calloc(1u, sizeof(*g_pei));
    assert_non_null(g_pei);
    g_vol_amap = (uint8_t *)calloc(1u, MK_MAP_SECTORS * MK_BLOCKSIZE);
    assert_non_null(g_vol_amap);
    g_boot_buf = (uint8_t *)calloc(1u, MK_BLOCKSIZE);
    assert_non_null(g_boot_buf);

    g_sbi->blocksize          = MK_BLOCKSIZE;
    g_sbi->blocksize_bits     = 9u;
    g_sbi->cluster_size       = MK_CLUSTER_SIZE;
    g_sbi->cluster_size_bits  = 9u;
    g_sbi->sect_per_clus_bits = 0u;
    g_sbi->num_fats           = MK_NUM_FATS;
    g_sbi->num_clusters       = MK_NUM_CLUSTERS + EXFAT_RESERVED_CLUSTERS;
    g_sbi->fat_offset         = MK_FAT_OFFSET;
    g_sbi->fat_length         = MK_FAT_LENGTH;
    g_sbi->fat2_offset        = MK_FAT_OFFSET;
    g_sbi->clu_offset         = MK_CLU_OFFSET;
    g_sbi->dentries_per_clu   = MK_CLUSTER_SIZE >> DENTRY_SIZE_BITS;
    g_sbi->map_clu            = MK_MAP_CLU;
    g_sbi->map_sectors        = MK_MAP_SECTORS;
    g_sbi->vol_amap           = g_vol_amap;
    g_sbi->boot_buf           = g_boot_buf;
    g_sbi->part_id            = MK_PART_ID;
    g_sbi->used_clusters      = 1u;        /* parent dir cluster reserved */
    g_sbi->clu_srch_ptr       = EXFAT_FIRST_CLUSTER;
    g_sbi->vol_flags          = 0u;
    g_sbi->vol_flags_persistent = 0u;
    /* parent_dir cluster bit set in bitmap (data idx 0). */
    g_vol_amap[0] = 0x01u;
    g_sbi->options.fs_uid     = 0u;
    g_sbi->options.fs_gid     = 0u;
    g_sbi->options.fs_dmask   = 0u;

    g_pei->type       = TYPE_DIR;
    g_pei->flags      = (uint8_t)ALLOC_NO_FAT_CHAIN;
    g_pei->start_clu  = MK_PARENT_CLU;
    g_pei->size       = MK_CLUSTER_SIZE;
    g_pei->valid_size = MK_CLUSTER_SIZE;
    g_pei->i_size_ondisk = MK_CLUSTER_SIZE;
    g_pei->dir.dir    = MK_PARENT_CLU;
    g_pei->dir.size   = 1u;
    g_pei->dir.flags  = (uint8_t)ALLOC_NO_FAT_CHAIN;
    g_pei->num_subdirs = 0u;

    memset(&g_mount, 0, sizeof(g_mount));
    memset(&g_pvnode, 0, sizeof(g_pvnode));
    g_mount.data         = g_sbi;
    g_pvnode.type        = VNODE_TYPE_DIR;
    g_pvnode.originMount = &g_mount;
    g_pvnode.data        = g_pei;
    return 0;
}

static int mk_teardown(void **state)
{
    (void)state;
    free(g_sbi); g_sbi = NULL;
    free(g_pei); g_pei = NULL;
    free(g_vol_amap); g_vol_amap = NULL;
    free(g_boot_buf); g_boot_buf = NULL;
    mock_disk_unload();
    return 0;
}

/* ---- alloc_new_dir tests ----------------------------------------------- */

static void test_alloc_new_dir_success(void **state)
{
    (void)state;
    exfat_chain clu;
    int rc = exfat_alloc_new_dir(g_sbi, &clu);
    assert_int_equal(rc, 0);
    /* First free cluster after reserved+used=2,3 → cluster #3. */
    assert_int_equal((unsigned int)clu.dir, 3u);
    assert_int_equal((unsigned int)clu.flags, ALLOC_NO_FAT_CHAIN);
    assert_int_equal((unsigned int)g_sbi->used_clusters, 2u);
}

static void test_alloc_new_dir_enospc(void **state)
{
    (void)state;
    /* Mark all data clusters as used → ENOSPC. */
    g_sbi->used_clusters = MK_NUM_CLUSTERS;
    exfat_chain clu;
    int rc = exfat_alloc_new_dir(g_sbi, &clu);
    assert_int_equal(rc, -ENOSPC);
    assert_int_equal((unsigned int)clu.dir, EXFAT_EOF_CLUSTER);
}

static void test_alloc_new_dir_eio_rolls_back(void **state)
{
    (void)state;
    /* alloc_cluster issues writes for: bitmap (1) + ent_set FAT1 (1) =
     * 2 writes. zeroed_cluster issues 1 write. Fail the 3rd write to
     * trigger rollback. */
    mock_disk_set_write_fail_at(3u);
    exfat_chain clu;
    int rc = exfat_alloc_new_dir(g_sbi, &clu);
    assert_int_equal(rc, -EIO);
    assert_int_equal((unsigned int)clu.dir, EXFAT_EOF_CLUSTER);
    /* Used count rolled back. */
    assert_int_equal((unsigned int)g_sbi->used_clusters, 1u);
}

/* ---- init_dir_entry tests --------------------------------------------- */

static void test_init_dir_entry_dir_success(void **state)
{
    (void)state;
    exfat_chain p_dir;
    p_dir.dir = MK_PARENT_CLU; p_dir.size = 1; p_dir.flags = ALLOC_NO_FAT_CHAIN;

    int rc = exfat_init_dir_entry(g_sbi, &p_dir, 0, TYPE_DIR, 5u,
                                  (uint64_t)MK_CLUSTER_SIZE);
    assert_int_equal(rc, 0);

    struct exfat_dentry fep;
    rc = exfat_get_dentry(g_sbi, &p_dir, 0, &fep, NULL);
    assert_int_equal(rc, 0);
    assert_int_equal((int)fep.type, EXFAT_FILE);
    assert_int_equal((unsigned int)fep.dentry.file.attr, ATTR_SUBDIR);

    struct exfat_dentry sep;
    rc = exfat_get_dentry(g_sbi, &p_dir, 1, &sep, NULL);
    assert_int_equal(rc, 0);
    assert_int_equal((int)sep.type, EXFAT_STREAM);
    assert_int_equal((unsigned int)sep.dentry.stream.flags, ALLOC_NO_FAT_CHAIN);
    assert_int_equal((unsigned int)sep.dentry.stream.start_clu, 5u);
    assert_int_equal((unsigned long long)sep.dentry.stream.size, MK_CLUSTER_SIZE);
}

static void test_init_dir_entry_file_success(void **state)
{
    (void)state;
    exfat_chain p_dir;
    p_dir.dir = MK_PARENT_CLU; p_dir.size = 1; p_dir.flags = ALLOC_NO_FAT_CHAIN;

    int rc = exfat_init_dir_entry(g_sbi, &p_dir, 0, TYPE_FILE,
                                  EXFAT_EOF_CLUSTER, 0u);
    assert_int_equal(rc, 0);

    struct exfat_dentry sep;
    rc = exfat_get_dentry(g_sbi, &p_dir, 1, &sep, NULL);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)sep.dentry.stream.flags, ALLOC_FAT_CHAIN);
    assert_int_equal((unsigned int)sep.dentry.stream.start_clu, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned long long)sep.dentry.stream.size, 0ull);
}

static void test_init_dir_entry_einval(void **state)
{
    (void)state;
    exfat_chain p_dir;
    p_dir.dir = MK_PARENT_CLU; p_dir.size = 1; p_dir.flags = ALLOC_NO_FAT_CHAIN;

    /* bad type */
    assert_int_equal(exfat_init_dir_entry(g_sbi, &p_dir, 0, 0xBADu, 5u, 512u),
                     -EINVAL);
    /* NULL p_dir */
    assert_int_equal(exfat_init_dir_entry(g_sbi, NULL, 0, TYPE_DIR, 5u, 512u),
                     -EINVAL);
    /* entry < 0 */
    assert_int_equal(exfat_init_dir_entry(g_sbi, &p_dir, -1, TYPE_DIR, 5u, 512u),
                     -EINVAL);
}

/* ---- init_ext_entry tests --------------------------------------------- */

static void prep_uniname_short(struct exfat_uni_name *u)
{
    /* "AB" — 2 UTF-16 code units → num_entries = 3. */
    memset(u, 0, sizeof(*u));
    u->name[0] = (uint16_t)'A';
    u->name[1] = (uint16_t)'B';
    u->name_len = 2u;
    u->name_hash = exfat_calc_chksum16(u->name, 2 * (int)sizeof(uint16_t),
                                       0, CS_DEFAULT);
}

static void test_init_ext_entry_short_success(void **state)
{
    (void)state;
    exfat_chain p_dir;
    p_dir.dir = MK_PARENT_CLU; p_dir.size = 1; p_dir.flags = ALLOC_NO_FAT_CHAIN;

    /* Seed file + stream first via init_dir_entry. */
    int rc = exfat_init_dir_entry(g_sbi, &p_dir, 0, TYPE_DIR, 5u, MK_CLUSTER_SIZE);
    assert_int_equal(rc, 0);

    struct exfat_uni_name u;
    prep_uniname_short(&u);
    rc = exfat_init_ext_entry(g_sbi, &p_dir, 0, 3, &u);
    assert_int_equal(rc, 0);

    /* Verify file dentry num_ext = 2 and chksum non-zero. */
    struct exfat_dentry fep;
    rc = exfat_get_dentry(g_sbi, &p_dir, 0, &fep, NULL);
    assert_int_equal(rc, 0);
    assert_int_equal((int)fep.dentry.file.num_ext, 2);
    assert_int_not_equal((int)fep.dentry.file.checksum, 0);

    /* Verify stream dentry name_len + name_hash. */
    struct exfat_dentry sep;
    rc = exfat_get_dentry(g_sbi, &p_dir, 1, &sep, NULL);
    assert_int_equal(rc, 0);
    assert_int_equal((int)sep.dentry.stream.name_len, 2);
    assert_int_equal((unsigned int)sep.dentry.stream.name_hash, u.name_hash);

    /* Verify name dentry slot 2 contains 'A','B'. */
    struct exfat_dentry nep;
    rc = exfat_get_dentry(g_sbi, &p_dir, 2, &nep, NULL);
    assert_int_equal(rc, 0);
    assert_int_equal((int)nep.type, EXFAT_NAME);
    assert_int_equal((int)nep.dentry.name.unicode_0_14[0], 'A');
    assert_int_equal((int)nep.dentry.name.unicode_0_14[1], 'B');
    assert_int_equal((int)nep.dentry.name.unicode_0_14[2], 0);
}

static void test_init_ext_entry_chksum_spans_all(void **state)
{
    (void)state;
    exfat_chain p_dir;
    p_dir.dir = MK_PARENT_CLU; p_dir.size = 1; p_dir.flags = ALLOC_NO_FAT_CHAIN;

    int rc = exfat_init_dir_entry(g_sbi, &p_dir, 0, TYPE_DIR, 5u, MK_CLUSTER_SIZE);
    assert_int_equal(rc, 0);

    struct exfat_uni_name u;
    prep_uniname_short(&u);
    rc = exfat_init_ext_entry(g_sbi, &p_dir, 0, 3, &u);
    assert_int_equal(rc, 0);

    /* Recompute chksum locally and compare. */
    struct exfat_dentry probe;
    uint16_t chksum;
    rc = exfat_get_dentry(g_sbi, &p_dir, 0, &probe, NULL);
    assert_int_equal(rc, 0);
    chksum = exfat_calc_chksum16(&probe, DENTRY_SIZE, 0, CS_DIR_ENTRY);
    rc = exfat_get_dentry(g_sbi, &p_dir, 1, &probe, NULL);
    assert_int_equal(rc, 0);
    chksum = exfat_calc_chksum16(&probe, DENTRY_SIZE, chksum, CS_DEFAULT);
    rc = exfat_get_dentry(g_sbi, &p_dir, 2, &probe, NULL);
    assert_int_equal(rc, 0);
    chksum = exfat_calc_chksum16(&probe, DENTRY_SIZE, chksum, CS_DEFAULT);

    /* fep.checksum is now the verified expected value. */
    struct exfat_dentry fep;
    rc = exfat_get_dentry(g_sbi, &p_dir, 0, &fep, NULL);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)fep.dentry.file.checksum, chksum);
}

static void test_init_ext_entry_einval(void **state)
{
    (void)state;
    exfat_chain p_dir;
    p_dir.dir = MK_PARENT_CLU; p_dir.size = 1; p_dir.flags = ALLOC_NO_FAT_CHAIN;
    struct exfat_uni_name u;
    prep_uniname_short(&u);

    assert_int_equal(exfat_init_ext_entry(NULL, &p_dir, 0, 3, &u), -EINVAL);
    assert_int_equal(exfat_init_ext_entry(g_sbi, NULL, 0, 3, &u), -EINVAL);
    assert_int_equal(exfat_init_ext_entry(g_sbi, &p_dir, 0, 3, NULL), -EINVAL);
    assert_int_equal(exfat_init_ext_entry(g_sbi, &p_dir, 0, 2, &u), -EINVAL);
    assert_int_equal(exfat_init_ext_entry(g_sbi, &p_dir, 0, 20, &u), -EINVAL);
}

/* ---- add_entry tests --------------------------------------------------- */

static void test_add_entry_dir_success(void **state)
{
    (void)state;
    struct exfat_dir_entry info;
    memset(&info, 0, sizeof(info));
    int rc = exfat_add_entry(g_sbi, &g_pvnode, "newdir", TYPE_DIR, &info);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)info.type, TYPE_DIR);
    assert_int_equal((unsigned int)info.attr, ATTR_SUBDIR);
    assert_int_equal((unsigned int)info.flags, ALLOC_NO_FAT_CHAIN);
    assert_int_equal((unsigned long long)info.size, MK_CLUSTER_SIZE);
    assert_int_equal((unsigned int)info.num_subdirs, EXFAT_MIN_SUBDIR);
    /* dentry slot 0 (terminator) should have been reused. */
    assert_int_equal((int)info.entry, 0);
    assert_int_not_equal((unsigned int)info.start_clu, EXFAT_EOF_CLUSTER);
}

static void test_add_entry_file_success(void **state)
{
    (void)state;
    /* Stage 4d: TYPE_FILE branch now produces an empty regular file with
     * NO data cluster allocated. start_clu == EXFAT_EOF_CLUSTER, size 0,
     * attr ATTR_ARCHIVE, num_subdirs 0. */
    struct exfat_dir_entry info;
    memset(&info, 0, sizeof(info));
    int rc = exfat_add_entry(g_sbi, &g_pvnode, "newfile", TYPE_FILE, &info);
    assert_int_equal(rc, 0);
    assert_int_equal((unsigned int)info.start_clu, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)info.attr, ATTR_ARCHIVE);
    assert_int_equal((unsigned int)info.size, 0u);
    assert_int_equal((unsigned int)info.num_subdirs, 0u);
    assert_int_equal((unsigned int)info.type, TYPE_FILE);
    assert_int_equal((unsigned int)info.flags, ALLOC_FAT_CHAIN);
}

static void test_add_entry_enospc_no_dir_cluster(void **state)
{
    (void)state;
    /* Saturate cluster space: only the parent cluster left. add_entry
     * succeeds at slot reservation but alloc_new_dir fails with ENOSPC. */
    g_sbi->used_clusters = MK_NUM_CLUSTERS;
    struct exfat_dir_entry info;
    memset(&info, 0, sizeof(info));
    int rc = exfat_add_entry(g_sbi, &g_pvnode, "x", TYPE_DIR, &info);
    assert_int_equal(rc, -ENOSPC);
}

/* ---- VfsExfatMkdir tests ----------------------------------------------- */

static void test_mkdir_success(void **state)
{
    (void)state;
    struct Vnode *new_vp = NULL;
    int rc = VfsExfatMkdir(&g_pvnode, "subdir", 0755, &new_vp);
    assert_int_equal(rc, 0);
    assert_non_null(new_vp);
    assert_int_equal((int)new_vp->type, VNODE_TYPE_DIR);
    assert_non_null(new_vp->data);
    /* parent_ei->num_subdirs bumped. */
    assert_int_equal((unsigned int)g_pei->num_subdirs, 1u);

    /* Cleanup vnode + ei (host stub VnodeAlloc → calloc; exfat_inode_free
     * destroys mux + frees ei). */
    exfat_inode_info *ei = (exfat_inode_info *)new_vp->data;
    new_vp->data = NULL;
    free(new_vp);
    exfat_inode_free(ei);
}

static void test_mkdir_einval(void **state)
{
    (void)state;
    struct Vnode *new_vp = NULL;
    /* NULL parent. */
    assert_int_equal(VfsExfatMkdir(NULL, "x", 0755, &new_vp), -EINVAL);
    /* NULL name. */
    assert_int_equal(VfsExfatMkdir(&g_pvnode, NULL, 0755, &new_vp), -EINVAL);
    /* NULL vpp. */
    assert_int_equal(VfsExfatMkdir(&g_pvnode, "x", 0755, NULL), -EINVAL);
    /* Empty name. */
    assert_int_equal(VfsExfatMkdir(&g_pvnode, "", 0755, &new_vp), -EINVAL);
}

static void test_mkdir_enospc_passthrough(void **state)
{
    (void)state;
    /* Saturate to force alloc_new_dir → ENOSPC. */
    g_sbi->used_clusters = MK_NUM_CLUSTERS;
    struct Vnode *new_vp = NULL;
    int rc = VfsExfatMkdir(&g_pvnode, "y", 0755, &new_vp);
    assert_int_equal(rc, -ENOSPC);
    assert_null(new_vp);
}

/* ---- VfsExfatCreate tests (Stage 4d) ----------------------------------- */

static void test_create_success(void **state)
{
    (void)state;
    struct Vnode *new_vp = NULL;
    int rc = VfsExfatCreate(&g_pvnode, "newfile.txt", 0644, &new_vp);
    assert_int_equal(rc, 0);
    assert_non_null(new_vp);
    assert_int_equal((int)new_vp->type, VNODE_TYPE_REG);
    assert_non_null(new_vp->data);

    /* exfat-create-no-cluster-on-empty: empty file has no cluster. */
    exfat_inode_info *ei = (exfat_inode_info *)new_vp->data;
    assert_int_equal((unsigned int)ei->type, TYPE_FILE);
    assert_int_equal((unsigned int)ei->attr, ATTR_ARCHIVE);
    assert_int_equal((unsigned int)ei->start_clu, EXFAT_EOF_CLUSTER);
    assert_int_equal((unsigned int)ei->size, 0u);
    assert_int_equal((unsigned int)ei->valid_size, 0u);
    assert_int_equal((unsigned int)ei->num_subdirs, 0u);

    /* exfat-create-no-parent-subdir-bump: parent unchanged. */
    assert_int_equal((unsigned int)g_pei->num_subdirs, 0u);

    /* Cleanup. */
    new_vp->data = NULL;
    free(new_vp);
    exfat_inode_free(ei);
}

static void test_create_einval(void **state)
{
    (void)state;
    struct Vnode *new_vp = NULL;
    assert_int_equal(VfsExfatCreate(NULL, "x", 0644, &new_vp), -EINVAL);
    assert_int_equal(VfsExfatCreate(&g_pvnode, NULL, 0644, &new_vp), -EINVAL);
    assert_int_equal(VfsExfatCreate(&g_pvnode, "x", 0644, NULL), -EINVAL);
    assert_int_equal(VfsExfatCreate(&g_pvnode, "", 0644, &new_vp), -EINVAL);
}

static void test_create_attr_archive_on_disk(void **state)
{
    (void)state;
    struct Vnode *new_vp = NULL;
    int rc = VfsExfatCreate(&g_pvnode, "f", 0644, &new_vp);
    assert_int_equal(rc, 0);
    assert_non_null(new_vp);

    exfat_inode_info *ei = (exfat_inode_info *)new_vp->data;
    /* Read back the on-disk file dentry: it lives at ei->entry in parent. */
    exfat_chain p_dir;
    p_dir.dir   = MK_PARENT_CLU;
    p_dir.flags = (uint8_t)ALLOC_NO_FAT_CHAIN;
    p_dir.size  = 1u;

    struct exfat_dentry de;
    int gret = exfat_get_dentry(g_sbi, &p_dir, ei->entry, &de, NULL);
    assert_int_equal(gret, 0);
    assert_int_equal((unsigned int)de.type, EXFAT_FILE);
    assert_true((de.dentry.file.attr & ATTR_ARCHIVE) != 0u);
    assert_true((de.dentry.file.attr & ATTR_SUBDIR) == 0u);

    new_vp->data = NULL;
    free(new_vp);
    exfat_inode_free(ei);
}

static void test_create_used_clusters_unchanged(void **state)
{
    (void)state;
    /* exfat-create-no-cluster-on-empty: bitmap usage MUST NOT grow. */
    uint32_t before = g_sbi->used_clusters;
    struct Vnode *new_vp = NULL;
    int rc = VfsExfatCreate(&g_pvnode, "g", 0644, &new_vp);
    assert_int_equal(rc, 0);
    assert_int_equal(g_sbi->used_clusters, before);

    exfat_inode_info *ei = (exfat_inode_info *)new_vp->data;
    new_vp->data = NULL;
    free(new_vp);
    exfat_inode_free(ei);
}

static void test_create_then_mkdir_subdir_count(void **state)
{
    (void)state;
    /* Sequencing test: create() does not bump num_subdirs but a subsequent
     * mkdir() does. Catches accidental shared-state bugs in add_entry. */
    struct Vnode *file_vp = NULL;
    int rc1 = VfsExfatCreate(&g_pvnode, "a.txt", 0644, &file_vp);
    assert_int_equal(rc1, 0);
    assert_int_equal((unsigned int)g_pei->num_subdirs, 0u);

    struct Vnode *dir_vp = NULL;
    int rc2 = VfsExfatMkdir(&g_pvnode, "d", 0755, &dir_vp);
    assert_int_equal(rc2, 0);
    assert_int_equal((unsigned int)g_pei->num_subdirs, 1u);

    exfat_inode_info *fei = (exfat_inode_info *)file_vp->data;
    exfat_inode_info *dei = (exfat_inode_info *)dir_vp->data;
    file_vp->data = NULL;
    dir_vp->data  = NULL;
    free(file_vp);
    free(dir_vp);
    exfat_inode_free(fei);
    exfat_inode_free(dei);
}

/* ============================================================
 * Suite table
 * ============================================================ */

const struct CMUnitTest test_mkdir_tests[] = {
    /* calc_num_entries (5) */
    cmocka_unit_test(test_calc_num_entries_len1),
    cmocka_unit_test(test_calc_num_entries_len15),
    cmocka_unit_test(test_calc_num_entries_len16),
    cmocka_unit_test(test_calc_num_entries_len255),
    cmocka_unit_test(test_calc_num_entries_einval),
    /* zeroed_cluster (4) */
    cmocka_unit_test_setup_teardown(test_zeroed_cluster_success,         img_setup, img_teardown),
    cmocka_unit_test_setup_teardown(test_zeroed_cluster_einval_null_sbi, img_setup, img_teardown),
    cmocka_unit_test_setup_teardown(test_zeroed_cluster_einval_bad_clu,  img_setup, img_teardown),
    cmocka_unit_test_setup_teardown(test_zeroed_cluster_eio,             img_setup, img_teardown),
    /* alloc_new_dir (3) */
    cmocka_unit_test_setup_teardown(test_alloc_new_dir_success,          mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_new_dir_enospc,           mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_alloc_new_dir_eio_rolls_back,   mk_setup,  mk_teardown),
    /* init_dir_entry (3) */
    cmocka_unit_test_setup_teardown(test_init_dir_entry_dir_success,     mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_init_dir_entry_file_success,    mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_init_dir_entry_einval,          mk_setup,  mk_teardown),
    /* init_ext_entry (3) */
    cmocka_unit_test_setup_teardown(test_init_ext_entry_short_success,   mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_init_ext_entry_chksum_spans_all,mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_init_ext_entry_einval,          mk_setup,  mk_teardown),
    /* add_entry (3) */
    cmocka_unit_test_setup_teardown(test_add_entry_dir_success,          mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_add_entry_file_success,         mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_add_entry_enospc_no_dir_cluster,mk_setup,  mk_teardown),
    /* VfsExfatMkdir (3) */
    cmocka_unit_test_setup_teardown(test_mkdir_success,                  mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_mkdir_einval,                   mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_mkdir_enospc_passthrough,       mk_setup,  mk_teardown),
    /* VfsExfatCreate — Stage 4d (5) */
    cmocka_unit_test_setup_teardown(test_create_success,                 mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_create_einval,                  mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_create_attr_archive_on_disk,    mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_create_used_clusters_unchanged, mk_setup,  mk_teardown),
    cmocka_unit_test_setup_teardown(test_create_then_mkdir_subdir_count, mk_setup,  mk_teardown),
};

const size_t test_mkdir_tests_count =
    sizeof(test_mkdir_tests) / sizeof(test_mkdir_tests[0]);
