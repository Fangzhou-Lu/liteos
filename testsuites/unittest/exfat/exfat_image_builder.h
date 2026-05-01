/*
 * exfat_image_builder — synthesize a minimal but well-formed exFAT image
 * in memory at test setup. The image is hand-rolled (not produced by
 * mkfs.exfat) so the cmocka run does not depend on the host having
 * exfat-utils / exfatprogs installed.
 *
 * Layout (12 sectors of 512 bytes = 6 KiB):
 *   sector 0          main boot sector (signature 0xAA55, valid geometry)
 *   sectors 1..7      extended boot sectors (zero — NOT crc-verified by
 *                     cmocka; mount-time strict CRC is exercised by QEMU LTP)
 *   sector 8          FAT (single-FAT layout, num_fats=1)
 *   sector 9          root cluster (cluster #2): BITMAP + UPCASE + UNUSED
 *   sector 10         bitmap cluster (cluster #3)
 *   sector 11         upcase cluster (cluster #4) — 256 byte identity table
 *
 * Geometry constants (MUST match what parse_boot_sector will derive):
 *   sect_size_bits         9   (512 B)
 *   sect_per_clus_bits     0   (1 sector per cluster)
 *   num_fats               1
 *   fat_offset             8
 *   fat_length             1
 *   clu_offset             9
 *   clu_count              14   (clusters 2..15 reachable; small but valid)
 *   root_cluster           2
 *
 * The builder also returns the expected upcase chksum32 so test_upcase
 * can verify the create path matches.
 */
#ifndef EXFAT_IMAGE_BUILDER_H
#define EXFAT_IMAGE_BUILDER_H

#include <stddef.h>
#include <stdint.h>

#define TIMG_SECTOR_SIZE       512u
#define TIMG_NUM_SECTORS       12u
#define TIMG_TOTAL_BYTES       (TIMG_SECTOR_SIZE * TIMG_NUM_SECTORS)

#define TIMG_FAT_OFFSET        8u
#define TIMG_CLU_OFFSET        9u
#define TIMG_NUM_CLUSTERS      14u           /* clu_count from boot sector  */
#define TIMG_ROOT_CLUSTER      2u
#define TIMG_BITMAP_CLUSTER    3u
#define TIMG_UPCASE_CLUSTER    4u

#define TIMG_BITMAP_SIZE       2u            /* 14 clusters / 8 → 2 bytes */
#define TIMG_UPCASE_SIZE       256u          /* identity for first 128 chars */

typedef struct exfat_test_image {
    uint8_t  bytes[TIMG_TOTAL_BYTES];
    uint32_t expected_upcase_chksum32;       /* computed at build time */
} exfat_test_image;

/* Build the image; chksum is computed using the very same algorithm
 * (1-bit ROR + add) the production code uses. */
void exfat_test_image_build(exfat_test_image *out);

/* Variants for negative-path tests. */
void exfat_test_image_build_bad_signature(exfat_test_image *out);
void exfat_test_image_build_bad_fs_name(exfat_test_image *out);
void exfat_test_image_build_no_bitmap_dentry(exfat_test_image *out);
void exfat_test_image_build_corrupt_upcase_chksum(exfat_test_image *out);
void exfat_test_image_build_bitmap_too_small(exfat_test_image *out);

#endif
