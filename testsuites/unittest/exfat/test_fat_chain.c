/*
 * test_fat_chain — exfat_get_next_cluster + exfat_chain_walk.
 *
 * Builds a synthetic 4-sector FAT image directly (no exfat_image_builder
 * dependency) and exercises the fat-chain helpers via mock_disk.
 *
 * FAT layout (fat_offset=0, blocksize=512 → 128 entries per sector):
 *   FAT[2]  = 3                           happy chain (2→3→4→EOF)
 *   FAT[3]  = 4
 *   FAT[4]  = EOF
 *   FAT[5]  = 5                           1-cycle (self-loop)
 *   FAT[6]  = 7                           2-cycle (6→7→6)
 *   FAT[7]  = 6
 *   FAT[8]  = 0xFFFFFFF8 (reserved>BAD)   reserved-remap → EOF
 *   FAT[9]  = EXFAT_BAD_CLUSTER           BAD → -EIO
 *   FAT[10] = EXFAT_FREE_CLUSTER          FREE → -EIO
 *   FAT[11] = 100                         OOR (≥ num_clusters=16) → -EIO
 *   FAT[12] = EOF
 *
 * num_clusters = 16 → valid range [2, 16); cycle bound = 16 iterations.
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
#include "mock_disk.h"

int  exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                            uint32_t *next_clu);
int  exfat_chain_walk(const exfat_sb_info *sbi, uint32_t start_clu,
                      exfat_chain_visitor_t visitor, void *ctx);
int  exfat_ent_set(const exfat_sb_info *sbi, uint32_t loc, uint32_t value);

#define FAT_TEST_NUM_CLUSTERS  16u
#define FAT_TEST_SECTORS       4
#define FAT_TEST_BUF_SIZE      (FAT_TEST_SECTORS * 512)

static uint8_t g_fat_image[FAT_TEST_BUF_SIZE];

static void put_le32(uint8_t *buf, size_t off, uint32_t val)
{
    buf[off + 0] = (uint8_t)(val);
    buf[off + 1] = (uint8_t)(val >> 8);
    buf[off + 2] = (uint8_t)(val >> 16);
    buf[off + 3] = (uint8_t)(val >> 24);
}

static void build_fat_image(void)
{
    memset(g_fat_image, 0, sizeof(g_fat_image));
    put_le32(g_fat_image,  2 * 4u, 3u);
    put_le32(g_fat_image,  3 * 4u, 4u);
    put_le32(g_fat_image,  4 * 4u, EXFAT_EOF_CLUSTER);
    put_le32(g_fat_image,  5 * 4u, 5u);
    put_le32(g_fat_image,  6 * 4u, 7u);
    put_le32(g_fat_image,  7 * 4u, 6u);
    put_le32(g_fat_image,  8 * 4u, 0xFFFFFFF8u);
    put_le32(g_fat_image,  9 * 4u, EXFAT_BAD_CLUSTER);
    put_le32(g_fat_image, 10 * 4u, EXFAT_FREE_CLUSTER);
    put_le32(g_fat_image, 11 * 4u, 100u);
    put_le32(g_fat_image, 12 * 4u, EXFAT_EOF_CLUSTER);
}

static void make_fat_sbi(exfat_sb_info *sbi)
{
    memset(sbi, 0, sizeof(*sbi));
    sbi->blocksize    = 512u;
    sbi->fat_offset   = 0u;
    sbi->fat_length   = FAT_TEST_SECTORS;
    sbi->num_clusters = FAT_TEST_NUM_CLUSTERS;
    sbi->part_id      = 0;
}

static int fat_setup(void **state)
{
    (void)state;
    build_fat_image();
    mock_disk_load(g_fat_image, sizeof(g_fat_image));
    mock_disk_reset_counters();
    return 0;
}

static int fat_teardown(void **state)
{
    (void)state;
    mock_disk_unload();
    return 0;
}

/* ============================================================================
 * exfat_get_next_cluster
 * ========================================================================== */

static void test_get_next_happy(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    uint32_t next = 0;
    assert_int_equal(exfat_get_next_cluster(&sbi, 2u, &next), 0);
    assert_int_equal(next, 3u);
    assert_int_equal(exfat_get_next_cluster(&sbi, 3u, &next), 0);
    assert_int_equal(next, 4u);
    assert_int_equal(exfat_get_next_cluster(&sbi, 4u, &next), 0);
    assert_int_equal(next, EXFAT_EOF_CLUSTER);
}

static void test_get_next_null_sbi(void **state)
{
    (void)state;
    uint32_t next;
    assert_int_equal(exfat_get_next_cluster(NULL, 2u, &next), -EINVAL);
}

static void test_get_next_null_out(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    assert_int_equal(exfat_get_next_cluster(&sbi, 2u, NULL), -EINVAL);
}

static void test_get_next_below_first_cluster(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    uint32_t next;
    assert_int_equal(exfat_get_next_cluster(&sbi, 0u, &next), -EIO);
    assert_int_equal(exfat_get_next_cluster(&sbi, 1u, &next), -EIO);
}

static void test_get_next_at_or_above_num_clusters(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    uint32_t next;
    assert_int_equal(exfat_get_next_cluster(&sbi, FAT_TEST_NUM_CLUSTERS, &next), -EIO);
    assert_int_equal(exfat_get_next_cluster(&sbi, FAT_TEST_NUM_CLUSTERS + 1u, &next), -EIO);
}

static void test_get_next_zero_blocksize(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.blocksize = 0u;
    uint32_t next;
    assert_int_equal(exfat_get_next_cluster(&sbi, 2u, &next), -EIO);
}

static void test_get_next_reserved_remap_to_eof(void **state)
{
    (void)state;
    /* FAT[8] = 0xFFFFFFF8 (raw > BAD=0xFFFFFFF7) → mapped to EOF. */
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    uint32_t next = 0;
    assert_int_equal(exfat_get_next_cluster(&sbi, 8u, &next), 0);
    assert_int_equal(next, EXFAT_EOF_CLUSTER);
}

static void test_get_next_bad_cluster(void **state)
{
    (void)state;
    /* FAT[9] = BAD; the local raw>BAD remap maps strictly greater values to
     * EOF, so raw==BAD survives validation only to be rejected explicitly. */
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    uint32_t next;
    assert_int_equal(exfat_get_next_cluster(&sbi, 9u, &next), -EIO);
}

static void test_get_next_free_cluster(void **state)
{
    (void)state;
    /* FAT[10] = FREE → mapped path treats FREE as terminator-not-allowed. */
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    uint32_t next;
    assert_int_equal(exfat_get_next_cluster(&sbi, 10u, &next), -EIO);
}

static void test_get_next_out_of_range(void **state)
{
    (void)state;
    /* FAT[11] = 100 (≥ num_clusters=16) but not EOF/BAD → -EIO. */
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    uint32_t next;
    assert_int_equal(exfat_get_next_cluster(&sbi, 11u, &next), -EIO);
}

static void test_get_next_io_failure(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    mock_disk_set_read_fail_at(1);
    uint32_t next;
    assert_int_equal(exfat_get_next_cluster(&sbi, 2u, &next), -EIO);
}

/* ============================================================================
 * exfat_chain_walk
 * ========================================================================== */

#define MAX_VISITED 32u
typedef struct {
    uint32_t visited[MAX_VISITED];
    uint32_t count;
    int      stop_at;     /* if visiting clu == stop_at, return 1 (success-stop) */
    int      err_at;      /* if visiting clu == err_at, return -EPERM */
} visit_ctx;

static int collector_visitor(uint32_t clu, void *ctx)
{
    visit_ctx *vc = (visit_ctx *)ctx;
    if (vc->count < MAX_VISITED) {
        vc->visited[vc->count++] = clu;
    }
    if (vc->stop_at != 0 && (uint32_t)vc->stop_at == clu) {
        return 1;
    }
    if (vc->err_at != 0 && (uint32_t)vc->err_at == clu) {
        return -EPERM;
    }
    return 0;
}

static void test_walk_happy_three_clusters(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    visit_ctx vc; memset(&vc, 0, sizeof(vc));
    int r = exfat_chain_walk(&sbi, 2u, collector_visitor, &vc);
    assert_int_equal(r, 0);
    assert_int_equal(vc.count, 3u);
    assert_int_equal(vc.visited[0], 2u);
    assert_int_equal(vc.visited[1], 3u);
    assert_int_equal(vc.visited[2], 4u);
}

static void test_walk_empty_chain_eof_start(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    visit_ctx vc; memset(&vc, 0, sizeof(vc));
    int r = exfat_chain_walk(&sbi, EXFAT_EOF_CLUSTER, collector_visitor, &vc);
    assert_int_equal(r, 0);
    assert_int_equal(vc.count, 0u);
}

static void test_walk_null_sbi(void **state)
{
    (void)state;
    visit_ctx vc; memset(&vc, 0, sizeof(vc));
    assert_int_equal(exfat_chain_walk(NULL, 2u, collector_visitor, &vc), -EINVAL);
}

static void test_walk_null_visitor(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    assert_int_equal(exfat_chain_walk(&sbi, 2u, NULL, NULL), -EINVAL);
}

static void test_walk_visitor_early_stop(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    visit_ctx vc; memset(&vc, 0, sizeof(vc));
    vc.stop_at = 3;
    int r = exfat_chain_walk(&sbi, 2u, collector_visitor, &vc);
    assert_int_equal(r, 0);
    assert_int_equal(vc.count, 2u);
    assert_int_equal(vc.visited[0], 2u);
    assert_int_equal(vc.visited[1], 3u);
}

static void test_walk_visitor_propagates_error(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    visit_ctx vc; memset(&vc, 0, sizeof(vc));
    vc.err_at = 3;
    int r = exfat_chain_walk(&sbi, 2u, collector_visitor, &vc);
    assert_int_equal(r, -EPERM);
    /* Visited 2 then 3, then propagated. */
    assert_int_equal(vc.count, 2u);
}

static void test_walk_self_loop_bound_exhausted(void **state)
{
    (void)state;
    /* FAT[5] = 5 → self-loop. Walk visits 5 num_clusters times then bails -EIO. */
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    visit_ctx vc; memset(&vc, 0, sizeof(vc));
    int r = exfat_chain_walk(&sbi, 5u, collector_visitor, &vc);
    assert_int_equal(r, -EIO);
    assert_int_equal(vc.count, FAT_TEST_NUM_CLUSTERS);
}

static void test_walk_two_cycle_bound_exhausted(void **state)
{
    (void)state;
    /* FAT[6]=7, FAT[7]=6 → 2-cycle. Walk bails after num_clusters iterations. */
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    visit_ctx vc; memset(&vc, 0, sizeof(vc));
    int r = exfat_chain_walk(&sbi, 6u, collector_visitor, &vc);
    assert_int_equal(r, -EIO);
    assert_int_equal(vc.count, FAT_TEST_NUM_CLUSTERS);
}

static void test_walk_propagates_get_next_error(void **state)
{
    (void)state;
    /* Start at 9 (FAT[9]=BAD). Visitor runs once on 9, then get_next_cluster
     * returns -EIO and walk propagates it (Invariant exfat-fat-chain-bounded
     * + reject-bad). */
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    visit_ctx vc; memset(&vc, 0, sizeof(vc));
    int r = exfat_chain_walk(&sbi, 9u, collector_visitor, &vc);
    assert_int_equal(r, -EIO);
    assert_int_equal(vc.count, 1u);
    assert_int_equal(vc.visited[0], 9u);
}

/* ============================================================================
 * exfat_ent_set
 * ========================================================================== */

/* Helper: read FAT[loc] back from mock_disk's RAM snapshot via los_part_read.
 * mock_disk_load copies the source buffer into a private snapshot, so direct
 * reads from g_fat_image would miss any writes made by exfat_ent_set. The
 * verification path goes through the mock the same way the production code
 * does. NOTE: this issues an extra los_part_read which bumps mock_disk_read_count;
 * call AFTER any read/write count assertions, never before.
 */
extern INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector,
                           UINT32 count, BOOL useRead);
static uint32_t read_fat_entry_via_mock(INT32 part_id,
                                        uint32_t fat_sector_offset,
                                        uint32_t loc, uint32_t blocksize)
{
    uint8_t buf[1024]; /* covers blocksize up to 1024; tests use 512 */
    uint64_t byte_off = (uint64_t)loc * 4u;
    uint64_t sector   = (uint64_t)fat_sector_offset + (byte_off / blocksize);
    uint32_t in_off   = (uint32_t)(byte_off % blocksize);
    uint32_t v = 0;
    (void)los_part_read(part_id, buf, sector, 1u, 1);
    v |= (uint32_t)buf[in_off + 0];
    v |= ((uint32_t)buf[in_off + 1]) << 8;
    v |= ((uint32_t)buf[in_off + 2]) << 16;
    v |= ((uint32_t)buf[in_off + 3]) << 24;
    return v;
}

static void test_ent_set_null_sbi(void **state)
{
    (void)state;
    assert_int_equal(exfat_ent_set(NULL, 2u, 3u), -EINVAL);
}

static void test_ent_set_loc_below_first(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats = 1u;
    assert_int_equal(exfat_ent_set(&sbi, 0u, EXFAT_EOF_CLUSTER), -EINVAL);
    assert_int_equal(exfat_ent_set(&sbi, 1u, EXFAT_EOF_CLUSTER), -EINVAL);
    /* No IO issued: bounds checked before alloc. */
    assert_int_equal((int)mock_disk_write_count(), 0);
}

static void test_ent_set_loc_at_or_above_num_clusters(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats = 1u;
    assert_int_equal(exfat_ent_set(&sbi, FAT_TEST_NUM_CLUSTERS, EXFAT_EOF_CLUSTER), -EINVAL);
    assert_int_equal(exfat_ent_set(&sbi, FAT_TEST_NUM_CLUSTERS + 1u, EXFAT_EOF_CLUSTER), -EINVAL);
    assert_int_equal((int)mock_disk_write_count(), 0);
}

static void test_ent_set_value_bad_cluster_rejected(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats = 1u;
    /* Invariant exfat-ent-set-rejects-bad-cluster. */
    assert_int_equal(exfat_ent_set(&sbi, 2u, EXFAT_BAD_CLUSTER), -EINVAL);
    assert_int_equal((int)mock_disk_write_count(), 0);
}

static void test_ent_set_value_out_of_range_rejected(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats = 1u;
    /* value == 1: below FIRST and not EOF/FREE → -EINVAL. */
    assert_int_equal(exfat_ent_set(&sbi, 2u, 1u), -EINVAL);
    /* value == num_clusters: at upper bound (exclusive) and not sentinel → -EINVAL. */
    assert_int_equal(exfat_ent_set(&sbi, 2u, FAT_TEST_NUM_CLUSTERS), -EINVAL);
    /* value == 100: clearly OOR. */
    assert_int_equal(exfat_ent_set(&sbi, 2u, 100u), -EINVAL);
    assert_int_equal((int)mock_disk_write_count(), 0);
}

static void test_ent_set_value_eof_succeeds(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats = 1u;
    /* Pre: FAT[2] currently == 3 from the test image. */
    assert_int_equal(exfat_ent_set(&sbi, 2u, EXFAT_EOF_CLUSTER), 0);
    /* Single read + single write (num_fats=1, no mirror). Asserted BEFORE
     * the verification read to avoid the verifier polluting the count. */
    assert_int_equal((int)mock_disk_read_count(), 1);
    assert_int_equal((int)mock_disk_write_count(), 1);
    /* Post: mock_disk RAM snapshot shows EOF at FAT[2]. */
    assert_int_equal(read_fat_entry_via_mock(0, 0u, 2u, 512u),
                     EXFAT_EOF_CLUSTER);
}

static void test_ent_set_value_free_succeeds(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats = 1u;
    assert_int_equal(exfat_ent_set(&sbi, 4u, EXFAT_FREE_CLUSTER), 0);
    assert_int_equal(read_fat_entry_via_mock(0, 0u, 4u, 512u),
                     EXFAT_FREE_CLUSTER);
}

static void test_ent_set_value_legal_cluster_succeeds(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats = 1u;
    /* Set FAT[2] = 5 (a legal cluster index in [2, 16)). */
    assert_int_equal(exfat_ent_set(&sbi, 2u, 5u), 0);
    assert_int_equal(read_fat_entry_via_mock(0, 0u, 2u, 512u), 5u);
    /* Subsequent get_next observes the new value. */
    uint32_t next = 0;
    assert_int_equal(exfat_get_next_cluster(&sbi, 2u, &next), 0);
    assert_int_equal(next, 5u);
}

/*
 * Mirror semantics: num_fats == 2. The RAW fat_buf returned by step 5
 * (memcpy_s patched LE32) is reused byte-for-byte in step 7's FAT2 write.
 * Verified by reading both FAT1 and FAT2 sectors from the in-RAM image and
 * asserting they hold the same bytes at the entry offset.
 */
static void test_ent_set_mirror_byte_exact_num_fats_2(void **state)
{
    (void)state;
    /* Build an 8-sector image: FAT1 at sectors 0-3, FAT2 at sectors 4-7.
     * Pre-fill both with the same FAT data so we can assert equality after. */
    static uint8_t mirror_image[8 * 512];
    memset(mirror_image, 0, sizeof(mirror_image));
    /* FAT1 base entries. */
    put_le32(mirror_image,  2 * 4u, 3u);
    put_le32(mirror_image,  3 * 4u, 4u);
    put_le32(mirror_image,  4 * 4u, EXFAT_EOF_CLUSTER);
    /* FAT2 base entries (mirror) at sector 4. */
    put_le32(mirror_image, 4 * 512u + 2 * 4u, 3u);
    put_le32(mirror_image, 4 * 512u + 3 * 4u, 4u);
    put_le32(mirror_image, 4 * 512u + 4 * 4u, EXFAT_EOF_CLUSTER);

    mock_disk_unload();
    mock_disk_load(mirror_image, sizeof(mirror_image));
    mock_disk_reset_counters();

    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats     = 2u;
    sbi.fat2_offset  = 4u;  /* 4 sectors into the image. */

    assert_int_equal(exfat_ent_set(&sbi, 2u, EXFAT_EOF_CLUSTER), 0);
    /* Read 1 + Write 2 (FAT1 + FAT2) — assert BEFORE verification reads to
     * avoid those polluting the count. */
    assert_int_equal((int)mock_disk_read_count(), 1);
    assert_int_equal((int)mock_disk_write_count(), 2);
    /* Both FAT1 and FAT2 must show the new EOF. */
    assert_int_equal(read_fat_entry_via_mock(0, 0u, 2u, 512u),
                     EXFAT_EOF_CLUSTER);
    assert_int_equal(read_fat_entry_via_mock(0, 4u, 2u, 512u),
                     EXFAT_EOF_CLUSTER);
}

static void test_ent_set_zero_blocksize(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats  = 1u;
    sbi.blocksize = 0u;
    assert_int_equal(exfat_ent_set(&sbi, 2u, EXFAT_EOF_CLUSTER), -EIO);
}

static void test_ent_set_read_fail_returns_eio(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats = 1u;
    /* First los_part_read fails → -EIO; no part_write issued. */
    mock_disk_set_read_fail_at(1u);
    assert_int_equal(exfat_ent_set(&sbi, 2u, EXFAT_EOF_CLUSTER), -EIO);
    assert_int_equal((int)mock_disk_write_count(), 0);
    /* Disable read-fail injection before the verification read; otherwise
     * the verifier itself would fail. FAT must still hold the original 3. */
    mock_disk_set_read_fail_at(0u);
    assert_int_equal(read_fat_entry_via_mock(0, 0u, 2u, 512u), 3u);
}

static void test_ent_set_write_fail_returns_eio(void **state)
{
    (void)state;
    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats = 1u;
    /* First los_part_write fails (FAT1) → -EIO. */
    mock_disk_set_write_fail_at(1u);
    assert_int_equal(exfat_ent_set(&sbi, 2u, EXFAT_EOF_CLUSTER), -EIO);
    /* Read OK, write attempted once and failed. */
    assert_int_equal((int)mock_disk_read_count(), 1);
    assert_int_equal((int)mock_disk_write_count(), 1);
}

/*
 * Partial commit on FAT2 mirror failure (Case 7 in spec):
 * FAT1 write succeeds, FAT2 write fails → -EIO; FAT1 has new value, FAT2
 * has old value. Caller responsibility (VOLUME_DIRTY) — function exits as
 * specified.
 */
static void test_ent_set_mirror_write_fail_partial_commit(void **state)
{
    (void)state;
    static uint8_t mirror_image[8 * 512];
    memset(mirror_image, 0, sizeof(mirror_image));
    put_le32(mirror_image,         2 * 4u, 3u);
    put_le32(mirror_image, 4 * 512u + 2 * 4u, 3u);

    mock_disk_unload();
    mock_disk_load(mirror_image, sizeof(mirror_image));
    mock_disk_reset_counters();

    exfat_sb_info sbi; make_fat_sbi(&sbi);
    sbi.num_fats    = 2u;
    sbi.fat2_offset = 4u;

    /* 2nd write fails (FAT2), 1st (FAT1) succeeded. */
    mock_disk_set_write_fail_at(2u);
    assert_int_equal(exfat_ent_set(&sbi, 2u, EXFAT_EOF_CLUSTER), -EIO);
    /* FAT1 already updated. */
    assert_int_equal(read_fat_entry_via_mock(0, 0u, 2u, 512u),
                     EXFAT_EOF_CLUSTER);
    /* FAT2 still old. */
    assert_int_equal(read_fat_entry_via_mock(0, 4u, 2u, 512u), 3u);
    assert_int_equal((int)mock_disk_write_count(), 2);
}

const struct CMUnitTest test_fat_chain_tests[] = {
    cmocka_unit_test_setup_teardown(test_get_next_happy,                    fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_get_next_null_sbi,                 fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_get_next_null_out,                 fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_get_next_below_first_cluster,      fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_get_next_at_or_above_num_clusters, fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_get_next_zero_blocksize,           fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_get_next_reserved_remap_to_eof,    fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_get_next_bad_cluster,              fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_get_next_free_cluster,             fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_get_next_out_of_range,             fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_get_next_io_failure,               fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_walk_happy_three_clusters,         fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_walk_empty_chain_eof_start,        fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_walk_null_sbi,                     fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_walk_null_visitor,                 fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_walk_visitor_early_stop,           fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_walk_visitor_propagates_error,     fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_walk_self_loop_bound_exhausted,    fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_walk_two_cycle_bound_exhausted,    fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_walk_propagates_get_next_error,    fat_setup, fat_teardown),
    /* exfat_ent_set (Wave B Stage 2a). */
    cmocka_unit_test_setup_teardown(test_ent_set_null_sbi,                       fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_loc_below_first,                fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_loc_at_or_above_num_clusters,   fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_value_bad_cluster_rejected,     fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_value_out_of_range_rejected,    fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_value_eof_succeeds,             fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_value_free_succeeds,            fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_value_legal_cluster_succeeds,   fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_mirror_byte_exact_num_fats_2,   fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_zero_blocksize,                 fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_read_fail_returns_eio,          fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_write_fail_returns_eio,         fat_setup, fat_teardown),
    cmocka_unit_test_setup_teardown(test_ent_set_mirror_write_fail_partial_commit, fat_setup, fat_teardown),
};

const size_t test_fat_chain_tests_count =
    sizeof(test_fat_chain_tests) / sizeof(test_fat_chain_tests[0]);
