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
 * exFAT on-disk format (Microsoft Extensible File Allocation Table File System).
 * 这是盘上协议层定义——结构布局、对齐、签名值由微软文件系统规范确定，不属于
 * "Linux→LiteOS-A" 抽象范围。本文件由 plugin 在 Loop A 期间作为 frozen contract
 * 的一部分起草，Microsoft exFAT specification 是事实来源（Linux 的 fs/exfat/exfat_raw.h
 * 仅作为参照实现）。
 */

#ifndef _EXFAT_RAW_H
#define _EXFAT_RAW_H

#include <stdint.h>

#define BOOT_SIGNATURE          0xAA55u
#define EXBOOT_SIGNATURE        0xAA550000u
#define STR_EXFAT               "EXFAT   "    /* size MUST be 8 (with trailing spaces) */

#define EXFAT_MAX_FILE_LEN      255

/* vol_flags bits（boot sector + dirty volume tracking）*/
#define VOLUME_DIRTY            0x0002u
#define MEDIA_FAILURE           0x0004u

/* FAT entry sentinel values */
#define EXFAT_EOF_CLUSTER       0xFFFFFFFFu
#define EXFAT_BAD_CLUSTER       0xFFFFFFF7u
#define EXFAT_FREE_CLUSTER      0u
#define EXFAT_RESERVED_CLUSTERS 2u
#define EXFAT_FIRST_CLUSTER     2u
#define EXFAT_DATA_CLUSTER_COUNT(sbi) \
            ((sbi)->num_clusters - EXFAT_RESERVED_CLUSTERS)

/* exfat_dentry.flags bits（GeneralSecondaryFlags Field）*/
#define ALLOC_FAT_CHAIN         0x01u   /* AllocationPossible 置位 */
#define ALLOC_NO_FAT_CHAIN      0x03u   /* + NoFatChain 置位（连续簇）*/

#define DENTRY_SIZE             32      /* directory entry size */
#define DENTRY_SIZE_BITS        5
#define MAX_EXFAT_DENTRIES      8388608 /* 256 MiB / 32 */

/* dentry types */
#define EXFAT_UNUSED            0x00    /* end of directory marker */
#define EXFAT_DELETE            (~0x80) /* mask: type & 0x7F if deleted */
#define IS_EXFAT_DELETED(x)     ((x) < 0x80)
#define EXFAT_INVAL             0x80    /* invalid */
#define EXFAT_BITMAP            0x81    /* allocation bitmap */
#define EXFAT_UPCASE            0x82    /* upcase table */
#define EXFAT_VOLUME            0x83    /* volume label */
#define EXFAT_FILE              0x85    /* file or directory primary entry */
#define EXFAT_GUID              0xA0
#define EXFAT_PADDING           0xA1
#define EXFAT_ACLTAB            0xA2
#define EXFAT_STREAM            0xC0    /* stream extension secondary */
#define EXFAT_NAME              0xC1    /* file name secondary */
#define EXFAT_ACL               0xC2

/* checksum types — Microsoft exFAT spec §3.4 */
#define CS_DIR_ENTRY            0
#define CS_BOOT_SECTOR          1
#define CS_DEFAULT              2

/* file attributes（cf. FAT）*/
#define ATTR_READONLY           0x0001
#define ATTR_HIDDEN             0x0002
#define ATTR_SYSTEM             0x0004
#define ATTR_VOLUME             0x0008
#define ATTR_SUBDIR             0x0010
#define ATTR_ARCHIVE            0x0020

#define BOOTSEC_JUMP_BOOT_LEN   3
#define BOOTSEC_FS_NAME_LEN     8
#define BOOTSEC_OLDBPB_LEN      53

#define EXFAT_FILE_NAME_LEN     15      /* unicode chars per name dentry */

#define EXFAT_MIN_SECT_SIZE_BITS         9      /* 2^9  = 512 B */
#define EXFAT_MAX_SECT_SIZE_BITS         12     /* 2^12 = 4 KiB */
#define EXFAT_MAX_SECT_PER_CLUS_BITS(x)  (25 - (x)->sect_size_bits)

/* boot sector — 512 bytes, packed, LE32/LE16/LE64 fields */
struct exfat_boot_sector {
    uint8_t  jmp_boot[BOOTSEC_JUMP_BOOT_LEN];   /* 0   */
    uint8_t  fs_name[BOOTSEC_FS_NAME_LEN];      /* 3   "EXFAT   " */
    uint8_t  must_be_zero[BOOTSEC_OLDBPB_LEN];  /* 11  防 FAT 卷误识别 */
    uint64_t partition_offset;                  /* 64  LE64 LBA */
    uint64_t vol_length;                        /* 72  LE64 卷扇区数 */
    uint32_t fat_offset;                        /* 80  LE32 FAT1 起始扇区 */
    uint32_t fat_length;                        /* 84  LE32 单 FAT 扇区数 */
    uint32_t clu_offset;                        /* 88  LE32 数据区起始扇区 */
    uint32_t clu_count;                         /* 92  LE32 簇数 */
    uint32_t root_cluster;                      /* 96  LE32 根目录簇 */
    uint32_t vol_serial;                        /* 100 */
    uint8_t  fs_revision[2];                    /* 104 [minor, major] */
    uint16_t vol_flags;                         /* 106 LE16 ★ chksum 跳过 */
    uint8_t  sect_size_bits;                    /* 108 */
    uint8_t  sect_per_clus_bits;                /* 109 */
    uint8_t  num_fats;                          /* 110 1 或 2 */
    uint8_t  drv_sel;                           /* 111 INT 13h drive number */
    uint8_t  percent_in_use;                    /* 112 ★ chksum 跳过 */
    uint8_t  reserved[7];                       /* 113 */
    uint8_t  boot_code[390];                    /* 120 */
    uint16_t signature;                         /* 510 LE16 0xAA55 */
} __attribute__((packed));

/* dentry — 32 字节 union by type */
struct exfat_dentry {
    uint8_t type;                               /* 0  EXFAT_* 类型字节 */
    union {
        struct {                                /* type=0x85 file primary */
            uint8_t  num_ext;
            uint16_t checksum;
            uint16_t attr;
            uint16_t reserved1;
            uint16_t create_time;
            uint16_t create_date;
            uint16_t modify_time;
            uint16_t modify_date;
            uint16_t access_time;
            uint16_t access_date;
            uint8_t  create_time_cs;
            uint8_t  modify_time_cs;
            uint8_t  create_tz;
            uint8_t  modify_tz;
            uint8_t  access_tz;
            uint8_t  reserved2[7];
        } __attribute__((packed)) file;

        struct {                                /* type=0xC0 stream */
            uint8_t  flags;
            uint8_t  reserved1;
            uint8_t  name_len;
            uint16_t name_hash;
            uint16_t reserved2;
            uint64_t valid_size;
            uint32_t reserved3;
            uint32_t start_clu;
            uint64_t size;
        } __attribute__((packed)) stream;

        struct {                                /* type=0xC1 file name */
            uint8_t  flags;
            uint16_t unicode_0_14[EXFAT_FILE_NAME_LEN];
        } __attribute__((packed)) name;

        struct {                                /* type=0x81 allocation bitmap */
            uint8_t  flags;
            uint8_t  reserved[18];
            uint32_t start_clu;
            uint64_t size;
        } __attribute__((packed)) bitmap;

        struct {                                /* type=0x82 upcase table */
            uint8_t  reserved1[3];
            uint32_t checksum;
            uint8_t  reserved2[12];
            uint32_t start_clu;
            uint64_t size;
        } __attribute__((packed)) upcase;
    } __attribute__((packed)) dentry;
} __attribute__((packed));

#define EXFAT_TZ_VALID          (1 << 7)

#endif /* !_EXFAT_RAW_H */
