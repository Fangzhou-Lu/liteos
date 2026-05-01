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

#ifndef _EXFAT_H
#define _EXFAT_H

#ifdef LOSCFG_FS_EXFAT

#include <stdint.h>
#include <stddef.h>
#include "los_typedef.h"
#include "los_mux.h"
#include "vnode.h"
#include "fs/mount.h"
#include "fs/file.h"
#include "exfat_raw.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- constants -------------------------------------------------------- */
#define EXFAT_SUPER_MAGIC          0x2011BAB0u
#define EXFAT_ROOT_INO             1u
#define EXFAT_MAX_NAME_LEN         255
#define EXFAT_DEFAULT_BLOCKSIZE    512u
#define EXFAT_HASH_BITS            8
#define EXFAT_HASH_SIZE            (1u << EXFAT_HASH_BITS)
#define EXFAT_CLUSTERS_UNTRACKED   (~0u)

/* type values mirrored from Linux exfat_fs.h (a subset; v1 mount only needs these) */
#define TYPE_UNUSED                0x0000
#define TYPE_DIR                   0x0104
#define TYPE_FILE                  0x011F
#define TYPE_BITMAP                0x0101
#define TYPE_UPCASE                0x0102
#define TYPE_VOLUME                0x0103
#define TYPE_STREAM                0x0201
#define TYPE_EXTEND                0x0202

/* ---- error policy ----------------------------------------------------- */
enum exfat_error_mode {
    EXFAT_ERRORS_CONT  = 0,
    EXFAT_ERRORS_PANIC = 1,
    EXFAT_ERRORS_RO    = 2,
};

/* ---- mount options ---------------------------------------------------- */
typedef struct exfat_mount_options {
    uint32_t fs_uid;
    uint32_t fs_gid;
    uint16_t fs_fmask;
    uint16_t fs_dmask;
    uint16_t allow_utime;
    uint8_t  utf8;
    uint8_t  discard;
    uint8_t  errors;
    int32_t  time_offset;
    char    *iocharset;
} exfat_mount_options;

/* ---- chain ------------------------------------------------------------ */
typedef struct exfat_chain {
    uint32_t dir;
    uint32_t size;
    uint8_t  flags;
} exfat_chain;

/* ---- super block in-memory ------------------------------------------- */
typedef struct exfat_sb_info {
    uint8_t  sect_size_bits;
    uint8_t  sect_per_clus_bits;
    uint8_t  num_fats;
    uint64_t partition_offset;
    uint64_t vol_length;
    uint32_t fat_offset;
    uint32_t fat_length;
    uint32_t fat2_offset;
    uint32_t clu_offset;
    uint32_t num_clusters;
    uint32_t root_dir;
    uint32_t cluster_size;
    uint32_t cluster_size_bits;
    uint32_t blocksize;
    uint32_t blocksize_bits;
    uint32_t dentries_per_clu;
    uint16_t vol_flags;
    uint16_t vol_flags_persistent;
    uint64_t s_maxbytes;
    uint32_t map_clu;
    uint32_t map_sectors;
    uint8_t *vol_amap;
    uint16_t *vol_utbl;
    uint64_t  vol_utbl_size;
    uint32_t  vol_utbl_clu;
    uint32_t clu_srch_ptr;
    uint32_t used_clusters;
    LosMux   s_lock;
    LosMux   bitmap_lock;
    LosMux   inode_hash_lock;
    uint8_t *boot_buf;
    INT32    part_id;
    exfat_mount_options options;
} exfat_sb_info;

/* ---- inode in-memory -------------------------------------------------- */
typedef struct exfat_inode_info {
    exfat_chain dir;
    int32_t  entry;
    uint32_t type;
    uint16_t attr;
    uint32_t start_clu;
    uint8_t  flags;
    uint64_t size;
    uint64_t valid_size;
    uint64_t i_size_ondisk;
    uint64_t i_pos;
    uint32_t version;
    uint32_t num_subdirs;
    LosMux   inode_lock;
} exfat_inode_info;

/* ---- public exports (defined in fs/exfat translation units; externed for mount glue) */
extern struct VnodeOps             g_exfatVops;
extern struct file_operations_vfs  g_exfatFops;
extern struct MountOps             g_exfatMountOps;

/* ---- helper externs --------------------------------------------------- */
/* options parser — fs/exfat/util/exfat_options.c (future Loop B output) */
int  exfat_parse_options(const char *data, exfat_mount_options *opts);

/* boot sector parser — fs/exfat/exfat_dentry.c (future Loop B output) */
int  exfat_parse_boot_sector(exfat_sb_info *sbi,
                             const struct exfat_boot_sector *bs,
                             uint32_t logical_sector_size);

/* exFAT-specific CRC32 (polynomial 0x04C11DB7 reversed; NOT IEEE) */
uint32_t exfat_calc_chksum32(const void *data, uint32_t len,
                             uint32_t chksum, int type);
#define CS_BOOT_SECTOR   1
#define CS_DEFAULT       2

/* upcase table — fs/exfat/util/exfat_upcase.c */
int  exfat_create_upcase_table(exfat_sb_info *sbi);
void exfat_free_upcase_table(exfat_sb_info *sbi);

/* allocation bitmap — fs/exfat/exfat_balloc.c */
int  exfat_load_bitmap(exfat_sb_info *sbi);
void exfat_free_bitmap(exfat_sb_info *sbi);
/* const sbi: count is mem-only (Invariant exfat-balloc-count-mem-only). */
int  exfat_count_used_clusters(const exfat_sb_info *sbi, uint32_t *ret_count);

/* find dentry of given type in root chain — fs/exfat/exfat_dentry.c
 * const sbi: helper does not mutate any sbi field (Invariant exfat-dentry-find-readonly). */
int  exfat_find_root_dentry(const exfat_sb_info *sbi, uint8_t type,
                            struct exfat_dentry *out);

#ifdef __cplusplus
}
#endif

#endif /* LOSCFG_FS_EXFAT */
#endif /* _EXFAT_H */
