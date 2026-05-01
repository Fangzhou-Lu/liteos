/*
 * exfat_image_builder.c — see header. Hand-rolls a 6 KiB exFAT image and
 * its negative variants.
 *
 * The chksum32 used for upcase validation matches production
 * exfat_calc_chksum32 (1-bit ROR + byte add, CS_DEFAULT).
 */

#include "exfat_image_builder.h"
#include <string.h>

static void put_u16_le(uint8_t *p, uint16_t v) { p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF; }
static void put_u32_le(uint8_t *p, uint32_t v) {
    p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF;
    p[2] = (v >> 16) & 0xFF; p[3] = (v >> 24) & 0xFF;
}
static void put_u64_le(uint8_t *p, uint64_t v) {
    for (int i = 0; i < 8; i++) p[i] = (v >> (8 * i)) & 0xFF;
}

/* The very same algorithm as fs/exfat/util/exfat_chksum.c::exfat_calc_chksum32
 * (CS_DEFAULT, no skip). Self-contained so the builder doesn't depend on the
 * production object linking order. */
static uint32_t local_chksum32(const void *data, uint32_t len, uint32_t chksum)
{
    const uint8_t *c = (const uint8_t *)data;
    for (uint32_t i = 0; i < len; i++) {
        chksum = ((chksum << 31) | (chksum >> 1)) + (uint32_t)c[i];
    }
    return chksum;
}

static void fill_boot_sector(uint8_t *bs)
{
    memset(bs, 0, TIMG_SECTOR_SIZE);
    /* offset 3..10: fs_name = "EXFAT   " */
    memcpy(bs + 3, "EXFAT   ", 8);
    /* offset 11..63: must_be_zero (already zero) */
    /* offset 64: partition_offset (0) */
    /* offset 72: vol_length = TIMG_NUM_SECTORS */
    put_u64_le(bs + 72, (uint64_t)TIMG_NUM_SECTORS);
    /* offset 80: fat_offset */
    put_u32_le(bs + 80, TIMG_FAT_OFFSET);
    /* offset 84: fat_length (1 sector) */
    put_u32_le(bs + 84, 1u);
    /* offset 88: clu_offset */
    put_u32_le(bs + 88, TIMG_CLU_OFFSET);
    /* offset 92: clu_count */
    put_u32_le(bs + 92, TIMG_NUM_CLUSTERS);
    /* offset 96: root_cluster */
    put_u32_le(bs + 96, TIMG_ROOT_CLUSTER);
    /* offset 100: vol_serial */
    put_u32_le(bs + 100, 0xCAFEBABEu);
    /* offset 104: fs_revision = 1.0 */
    bs[104] = 0; bs[105] = 1;
    /* offset 106: vol_flags = 0 (clean volume) */
    put_u16_le(bs + 106, 0);
    /* offset 108: sect_size_bits = 9 (512 B) */
    bs[108] = 9;
    /* offset 109: sect_per_clus_bits = 0 (1 sector/cluster) */
    bs[109] = 0;
    /* offset 110: num_fats = 1 */
    bs[110] = 1;
    /* offset 111: drv_sel */
    bs[111] = 0x80;
    /* offset 112: percent_in_use */
    bs[112] = 0;
    /* offset 510: signature = 0xAA55 */
    put_u16_le(bs + 510, 0xAA55u);
}

static void fill_fat(uint8_t *fat)
{
    memset(fat, 0, TIMG_SECTOR_SIZE);
    /* FAT[0] = 0xFFFFFFF8 (media descriptor) */
    put_u32_le(fat + 0, 0xFFFFFFF8u);
    /* FAT[1] = 0xFFFFFFFF */
    put_u32_le(fat + 4, 0xFFFFFFFFu);
    /* FAT[2..4] = EOF (root, bitmap, upcase are each 1-cluster chains) */
    put_u32_le(fat + 8,  0xFFFFFFFFu);
    put_u32_le(fat + 12, 0xFFFFFFFFu);
    put_u32_le(fat + 16, 0xFFFFFFFFu);
    /* FAT[5..13] = FREE (0). */
}

static void fill_root_dentries(uint8_t *root, uint32_t upcase_chksum)
{
    memset(root, 0, TIMG_SECTOR_SIZE);
    /* Dentry 0: BITMAP (type 0x81)
     *   0:    type = 0x81
     *   1:    bitmap.flags = 0
     *   2..19 reserved
     *   20:   start_clu = TIMG_BITMAP_CLUSTER (LE32)
     *   24:   size = TIMG_BITMAP_SIZE (LE64)
     */
    root[0]  = 0x81;
    root[1]  = 0;
    put_u32_le(root + 20, TIMG_BITMAP_CLUSTER);
    put_u64_le(root + 24, TIMG_BITMAP_SIZE);

    /* Dentry 1 (offset 32): UPCASE (type 0x82)
     *   0:    type = 0x82
     *   1..3  reserved
     *   4:    checksum = upcase_chksum (LE32)
     *   8..19 reserved
     *   20:   start_clu = TIMG_UPCASE_CLUSTER
     *   24:   size = TIMG_UPCASE_SIZE
     */
    uint8_t *e = root + 32;
    e[0] = 0x82;
    put_u32_le(e + 4, upcase_chksum);
    put_u32_le(e + 20, TIMG_UPCASE_CLUSTER);
    put_u64_le(e + 24, TIMG_UPCASE_SIZE);

    /* Dentry 2 (offset 64): UNUSED terminator. Leaving 0 is sufficient
     * (type byte == EXFAT_UNUSED == 0x00 → terminator). */
}

static void fill_bitmap_cluster(uint8_t *p)
{
    memset(p, 0, TIMG_SECTOR_SIZE);
    /* clusters 2,3,4 used (indices 0,1,2 in data-cluster numbering). */
    p[0] = 0x07;
}

/* Build a 256-byte identity-ish upcase table (each uint16_t = its index). */
static void fill_upcase_cluster(uint8_t *p)
{
    memset(p, 0, TIMG_SECTOR_SIZE);
    for (uint32_t i = 0; i < TIMG_UPCASE_SIZE / 2; i++) {
        put_u16_le(p + 2 * i, (uint16_t)i);
    }
}

void exfat_test_image_build(exfat_test_image *out)
{
    memset(out, 0, sizeof(*out));

    /* Build clusters first so we can compute upcase chksum32. */
    uint8_t bitmap_cluster[TIMG_SECTOR_SIZE];
    uint8_t upcase_cluster[TIMG_SECTOR_SIZE];
    fill_bitmap_cluster(bitmap_cluster);
    fill_upcase_cluster(upcase_cluster);

    uint32_t chksum = local_chksum32(upcase_cluster, TIMG_UPCASE_SIZE, 0);
    out->expected_upcase_chksum32 = chksum;

    /* Lay sectors into the image. */
    fill_boot_sector(out->bytes + 0 * TIMG_SECTOR_SIZE);
    /* sectors 1..7 stay zero */
    fill_fat(out->bytes + TIMG_FAT_OFFSET * TIMG_SECTOR_SIZE);
    fill_root_dentries(out->bytes + TIMG_CLU_OFFSET * TIMG_SECTOR_SIZE, chksum);
    memcpy(out->bytes + (TIMG_CLU_OFFSET + 1) * TIMG_SECTOR_SIZE,
           bitmap_cluster, TIMG_SECTOR_SIZE);
    memcpy(out->bytes + (TIMG_CLU_OFFSET + 2) * TIMG_SECTOR_SIZE,
           upcase_cluster, TIMG_SECTOR_SIZE);
}

/* ------- negative variants: rebuild then mutate one field ----------------- */

void exfat_test_image_build_bad_signature(exfat_test_image *out)
{
    exfat_test_image_build(out);
    put_u16_le(out->bytes + 510, 0x1234);
}

void exfat_test_image_build_bad_fs_name(exfat_test_image *out)
{
    exfat_test_image_build(out);
    out->bytes[3] = 'F';
    out->bytes[4] = 'A';
    out->bytes[5] = 'T';
}

void exfat_test_image_build_no_bitmap_dentry(exfat_test_image *out)
{
    exfat_test_image_build(out);
    /* Replace BITMAP dentry's type byte with UPCASE so the search returns
     * the upcase entry first AND no bitmap is found. */
    uint8_t *root = out->bytes + TIMG_CLU_OFFSET * TIMG_SECTOR_SIZE;
    root[0] = 0x83;   /* VOLUME — neither BITMAP nor terminator */
}

void exfat_test_image_build_corrupt_upcase_chksum(exfat_test_image *out)
{
    exfat_test_image_build(out);
    uint8_t *root = out->bytes + TIMG_CLU_OFFSET * TIMG_SECTOR_SIZE;
    /* upcase dentry begins at root + 32; chksum at +4 */
    put_u32_le(root + 32 + 4, 0xDEADBEEFu);
}

void exfat_test_image_build_bitmap_too_small(exfat_test_image *out)
{
    exfat_test_image_build(out);
    /* Set bitmap.size = 0 — a clear "too small" → -EIO from load_bitmap. */
    uint8_t *root = out->bytes + TIMG_CLU_OFFSET * TIMG_SECTOR_SIZE;
    put_u64_le(root + 24, 0);   /* bitmap.size */
}
