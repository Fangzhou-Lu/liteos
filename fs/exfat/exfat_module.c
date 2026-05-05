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
/* exfat_module — bitmap-independent utilities: chksum / mount options /
 * upcase / vol_flags / boot sector parser. Cluster mgmt (FAT entry, bitmap,
 * alloc/free cluster) lives in exfat_cluster.c. */

#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "exfat.h"
#include "disk.h"
#include "los_memory.h"
#include "los_printf.h"
#include "securec.h"

#define UMASK_MAX_VALUE   0777u   /* 9-bit POSIX permission bits */
#define TIME_OFFSET_MIN   (-24 * 60)
#define TIME_OFFSET_MAX   ( 24 * 60)
#define LE32_TO_HOST(x) ((uint32_t)(x))
#define LE64_TO_HOST(x) ((uint64_t)(x))
#define EXFAT_UPCASE_MAX_BYTES   (65536u * 2u)   /* full BMP, 128 KiB */
#define HOST_TO_LE16(x)  ((uint16_t)(x))
#define EXFAT_MAIN_BOOT_SECTOR  0u
#define EXFAT_BOOT_WRITE_COUNT  1u
#define LE16_TO_HOST(x) ((uint16_t)(x))

static char g_iocharset_utf8[] = "utf8";

/* ----- merged from exfat_chksum.c ----- */

/*
 * exFAT-specific checksum (Microsoft exFAT specification §3.1.1.4 / §6.3.3 / §7.2.5).
 * Fixed algorithm: 1-bit right rotate + 8-bit byte add. NOT an IEEE 802.3 CRC32 —
 * do NOT replace with LOS_Crc32 (different polynomial, incompatible result).
 */

uint32_t exfat_calc_chksum32(const void *data, uint32_t len,
                             uint32_t chksum, int type)
{
    const uint8_t *c = (const uint8_t *)data;
    uint32_t i;

    for (i = 0; i < len; i++) {
        if (type == CS_BOOT_SECTOR && (i == 106u || i == 107u || i == 112u)) {
            continue;
        }
        chksum = ((chksum << 31) | (chksum >> 1)) + (uint32_t)c[i];
    }
    return chksum;
}

uint16_t exfat_calc_chksum16(const void *data, int len,
                             uint16_t chksum, int type)
{
    const uint8_t *c = (const uint8_t *)data;
    int i;

    for (i = 0; i < len; i++) {
        if (type == CS_DIR_ENTRY && (i == 2 || i == 3)) {
            continue;
        }
        chksum = (uint16_t)(((chksum << 15) | (chksum >> 1)) + (uint16_t)c[i]);
    }
    return chksum;
}

/* ----- merged from exfat_options.c ----- */

/* Static iocharset string — placed in .rodata; never freed (per Invariant
 * exfat-options-no-heap-leak). */

static int ParseDecU32(const char *s, size_t len, uint32_t *out)
{
    uint64_t v = 0;
    size_t i;

    if (len == 0) {
        return -EINVAL;
    }
    for (i = 0; i < len; i++) {
        char c = s[i];
        if (c < '0' || c > '9') {
            return -EINVAL;
        }
        v = v * 10u + (uint32_t)(c - '0');
        if (v > (uint64_t)0xFFFFFFFFu) {
            return -ERANGE;
        }
    }
    *out = (uint32_t)v;
    return 0;
}

static int ParseDecS32(const char *s, size_t len, int32_t *out)
{
    int negative = 0;
    uint32_t mag = 0;
    int err;

    if (len == 0) {
        return -EINVAL;
    }
    if (s[0] == '-') {
        negative = 1;
        s++;
        len--;
        if (len == 0) {
            return -EINVAL;
        }
    } else if (s[0] == '+') {
        s++;
        len--;
        if (len == 0) {
            return -EINVAL;
        }
    }
    err = ParseDecU32(s, len, &mag);
    if (err != 0) {
        return err;
    }
    if (negative) {
        if (mag > 0x80000000u) {
            return -ERANGE;
        }
        *out = -(int32_t)mag;
    } else {
        if (mag > 0x7FFFFFFFu) {
            return -ERANGE;
        }
        *out = (int32_t)mag;
    }
    return 0;
}

static int ParseOctalMask(const char *s, size_t len, uint16_t *out)
{
    uint32_t v = 0;
    size_t i;

    if (len == 0) {
        return -EINVAL;
    }
    for (i = 0; i < len; i++) {
        char c = s[i];
        if (c < '0' || c > '7') {
            return -EINVAL;
        }
        v = (v << 3) | (uint32_t)(c - '0');
        if (v > UMASK_MAX_VALUE) {
            return -ERANGE;
        }
    }
    *out = (uint16_t)v;
    return 0;
}

static int KeyEquals(const char *k, size_t klen, const char *expect)
{
    size_t e = strlen(expect);
    if (klen != e) {
        return 0;
    }
    return memcmp(k, expect, e) == 0;
}

static int HandleErrors(const char *v, size_t vlen, exfat_mount_options *opts)
{
    if (KeyEquals(v, vlen, "continue")) {
        opts->errors = (uint8_t)EXFAT_ERRORS_CONT;
        return 0;
    }
    if (KeyEquals(v, vlen, "panic")) {
        opts->errors = (uint8_t)EXFAT_ERRORS_PANIC;
        return 0;
    }
    if (KeyEquals(v, vlen, "remount-ro")) {
        opts->errors = (uint8_t)EXFAT_ERRORS_RO;
        return 0;
    }
    return -EINVAL;
}

static int HandleIocharset(const char *v, size_t vlen, exfat_mount_options *opts)
{
    if (!KeyEquals(v, vlen, "utf8")) {
        return -EINVAL;
    }
    opts->iocharset = g_iocharset_utf8;
    opts->utf8 = 1;
    return 0;
}

/*
 * Apply one parsed key/value pair to opts. Returns 0 on success, -EINVAL on
 * unknown key or unparseable value, -ERANGE on numeric overflow.
 *
 * `v` may be NULL (open-flag style, e.g. "discard") iff the key permits it.
 */
static int ApplyKeyValue(const char *k, size_t klen,
                         const char *v, size_t vlen,
                         int has_value,
                         exfat_mount_options *opts)
{
    /* iocharset, errors — string-valued */
    if (KeyEquals(k, klen, "iocharset")) {
        if (!has_value) return -EINVAL;
        return HandleIocharset(v, vlen, opts);
    }
    if (KeyEquals(k, klen, "errors")) {
        if (!has_value) return -EINVAL;
        return HandleErrors(v, vlen, opts);
    }

    /* discard — switch only, no value */
    if (KeyEquals(k, klen, "discard")) {
        if (has_value) return -EINVAL;
        opts->discard = 1;
        return 0;
    }

    /* uid, gid — uint32 */
    if (KeyEquals(k, klen, "uid")) {
        if (!has_value) return -EINVAL;
        return ParseDecU32(v, vlen, &opts->fs_uid);
    }
    if (KeyEquals(k, klen, "gid")) {
        if (!has_value) return -EINVAL;
        return ParseDecU32(v, vlen, &opts->fs_gid);
    }

    /* umask, fmask, dmask, allow_utime — octal 9-bit */
    if (KeyEquals(k, klen, "umask")) {
        uint16_t m;
        int err;
        if (!has_value) return -EINVAL;
        err = ParseOctalMask(v, vlen, &m);
        if (err != 0) return err;
        opts->fs_fmask = m;
        opts->fs_dmask = m;
        return 0;
    }
    if (KeyEquals(k, klen, "fmask")) {
        if (!has_value) return -EINVAL;
        return ParseOctalMask(v, vlen, &opts->fs_fmask);
    }
    if (KeyEquals(k, klen, "dmask")) {
        if (!has_value) return -EINVAL;
        return ParseOctalMask(v, vlen, &opts->fs_dmask);
    }
    if (KeyEquals(k, klen, "allow_utime")) {
        if (!has_value) return -EINVAL;
        return ParseOctalMask(v, vlen, &opts->allow_utime);
    }

    /* time_offset — signed minutes in [-24*60, 24*60] */
    if (KeyEquals(k, klen, "time_offset")) {
        int32_t t;
        int err;
        if (!has_value) return -EINVAL;
        err = ParseDecS32(v, vlen, &t);
        if (err != 0) return err;
        if (t < TIME_OFFSET_MIN || t > TIME_OFFSET_MAX) {
            return -ERANGE;
        }
        opts->time_offset = t;
        return 0;
    }

    /* unknown key — strict rejection per Invariant exfat-options-strict-key-rejection */
    return -EINVAL;
}

int exfat_parse_options(const char *data, exfat_mount_options *opts)
{
    const char *p, *key_start, *value_start;
    size_t key_len, value_len;
    int has_value;
    int err;

    if (opts == NULL) {
        return -EINVAL;
    }
    if (data == NULL || data[0] == '\0') {
        return 0;
    }

    p = data;
    while (*p != '\0') {
        /* skip leading separators / whitespace */
        while (*p == ',' || *p == ' ' || *p == '\t') {
            p++;
        }
        if (*p == '\0') {
            break;
        }

        /* parse key up to '=' or ',' or end */
        key_start = p;
        while (*p != '\0' && *p != '=' && *p != ',') {
            p++;
        }
        key_len = (size_t)(p - key_start);
        if (key_len == 0) {
            return -EINVAL;
        }

        /* parse optional value */
        has_value = 0;
        value_start = NULL;
        value_len = 0;
        if (*p == '=') {
            p++;
            has_value = 1;
            value_start = p;
            while (*p != '\0' && *p != ',') {
                p++;
            }
            value_len = (size_t)(p - value_start);
            if (value_len == 0) {
                return -EINVAL;
            }
        }

        err = ApplyKeyValue(key_start, key_len, value_start, value_len,
                            has_value, opts);
        if (err != 0) {
            return err;
        }
    }
    return 0;
}

/* ----- merged from exfat_upcase.c ----- */

extern UINT8 *m_aucSysMem0;

int exfat_create_upcase_table(exfat_sb_info *sbi)
{
    struct exfat_dentry dentry;
    uint32_t tbl_clu;
    uint64_t tbl_size;
    uint32_t expected_chksum;
    uint32_t actual_chksum;
    uint32_t num_sectors;
    uint64_t buf_bytes;
    uint64_t data_sector;
    uint32_t sect_per_clus;
    uint8_t *buf = NULL;
    int err;

    if (sbi == NULL || sbi->vol_utbl != NULL) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u || sbi->blocksize == 0u ||
        sbi->num_clusters < EXFAT_RESERVED_CLUSTERS) {
        return -EINVAL;
    }

    err = exfat_find_root_dentry(sbi, EXFAT_UPCASE, &dentry);
    if (err != 0) {
        return err;
    }

    tbl_clu  = LE32_TO_HOST(dentry.dentry.upcase.start_clu);
    tbl_size = LE64_TO_HOST(dentry.dentry.upcase.size);
    expected_chksum = LE32_TO_HOST(dentry.dentry.upcase.checksum);

    if (tbl_clu < EXFAT_FIRST_CLUSTER || tbl_clu >= sbi->num_clusters) {
        return -EINVAL;
    }
    if (tbl_size == 0u || (tbl_size & 1u) != 0u || tbl_size > EXFAT_UPCASE_MAX_BYTES) {
        return -EINVAL;
    }

    num_sectors = (uint32_t)((tbl_size + sbi->blocksize - 1u) / sbi->blocksize);
    if (num_sectors == 0u) {
        return -EINVAL;
    }
    buf_bytes = (uint64_t)num_sectors * (uint64_t)sbi->blocksize;

    buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, (UINT32)buf_bytes);
    if (buf == NULL) {
        return -ENOMEM;
    }

    sect_per_clus = 1u << sbi->sect_per_clus_bits;
    data_sector = (uint64_t)sbi->clu_offset +
                  (uint64_t)(tbl_clu - EXFAT_FIRST_CLUSTER) * (uint64_t)sect_per_clus;

    if (los_part_read(sbi->part_id, buf, data_sector, num_sectors, TRUE) < 0) {
        err = -EIO;
        goto err_free;
    }

    actual_chksum = exfat_calc_chksum32(buf, (uint32_t)tbl_size, 0u, CS_DEFAULT);
    if (actual_chksum != expected_chksum) {
        PRINT_ERR("exfat: upcase checksum mismatch (calc=0x%08x stored=0x%08x)\n",
                  actual_chksum, expected_chksum);
        err = -EINVAL;
        goto err_free;
    }

    /* On LE host (LiteOS-A ARM), the byte buffer reinterprets cleanly as
     * uint16_t[] with no per-entry swap (Invariant exfat-upcase-byte-order). */
    sbi->vol_utbl       = (uint16_t *)buf;
    sbi->vol_utbl_clu   = tbl_clu;
    sbi->vol_utbl_size  = tbl_size;
    return 0;

err_free:
    LOS_MemFree(m_aucSysMem0, buf);
    return err;
}

void exfat_free_upcase_table(exfat_sb_info *sbi)
{
    if (sbi == NULL) {
        return;
    }
    if (sbi->vol_utbl != NULL) {
        LOS_MemFree(m_aucSysMem0, sbi->vol_utbl);
        sbi->vol_utbl = NULL;
    }
    sbi->vol_utbl_size = 0u;
}

/* ----- merged from exfat_vol_flags.c ----- */

/* Identity host->LE on LiteOS-A ARM (LE host); kept as wrapper so a future BE
 * port can replace this single macro instead of every call site. Mirrors the
 * convention in fs/exfat/util/exfat_fat_chain.c (Invariant
 * exfat-vol-flags-le16-on-disk). */

/* ---------------------------------------------------------------------------
 * exfat_set_vol_flags — internal helper, mirrors Linux fs/exfat/super.c
 * exfat_set_vol_flags. Caller MUST hold sbi->s_lock; this routine itself does
 * not acquire / release any LiteOS-A primitive (Invariant
 * exfat-vol-flags-no-lock-acquire) and does not allocate (Invariant
 * exfat-vol-flags-no-realloc).
 *
 * Returns 0 on success (including the no-op case where final_flags equals the
 * current sbi->vol_flags) or -EIO when the synchronous boot-sector write-back
 * fails. -EINVAL is reserved for the public-API guards above this helper.
 * --------------------------------------------------------------------------- */
static int exfat_set_vol_flags(exfat_sb_info *sbi, uint16_t new_flags)
{
    struct exfat_boot_sector *p_boot;
    uint16_t final_flags;
    int ret;

    final_flags = (uint16_t)(new_flags | sbi->vol_flags_persistent);
    if (sbi->vol_flags == final_flags) {
        return 0;
    }

    sbi->vol_flags = final_flags;
    p_boot = (struct exfat_boot_sector *)sbi->boot_buf;
    p_boot->vol_flags = HOST_TO_LE16(final_flags);

    ret = los_part_write(sbi->part_id, sbi->boot_buf,
                         (UINT64)EXFAT_MAIN_BOOT_SECTOR,
                         (UINT32)EXFAT_BOOT_WRITE_COUNT);
    if (ret != 0) {
        PRINT_ERR("[%s] part_write failed: %d\n", __func__, ret);
        return -EIO;
    }
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_set_volume_dirty — sets VOLUME_DIRTY in sbi->vol_flags and writes the
 * main boot sector back. Wave B write paths (create / unlink / rename / write
 * / truncate) call this BEFORE staging on-disk mutations so that a power loss
 * mid-write leaves a recoverable marker.
 *
 * Caller must hold sbi->s_lock (LosMux). Returns 0 on success, -EINVAL when
 * sbi state is incomplete (NULL sbi, NULL boot_buf, zero blocksize), or -EIO
 * if los_part_write fails after the in-memory bookkeeping has been updated.
 * --------------------------------------------------------------------------- */
int exfat_set_volume_dirty(exfat_sb_info *sbi)
{
    if (sbi == NULL || sbi->boot_buf == NULL || sbi->blocksize == 0u) {
        return -EINVAL;
    }
    return exfat_set_vol_flags(sbi, (uint16_t)(sbi->vol_flags | VOLUME_DIRTY));
}

/* ---------------------------------------------------------------------------
 * exfat_clear_volume_dirty — clears VOLUME_DIRTY in sbi->vol_flags. The
 * persistent-merge inside exfat_set_vol_flags ensures MEDIA_FAILURE (and any
 * latched VOLUME_DIRTY in vol_flags_persistent) survives this call (Invariant
 * exfat-vol-flags-persistent-merged).
 *
 * Caller must hold sbi->s_lock. Same return contract as
 * exfat_set_volume_dirty.
 * --------------------------------------------------------------------------- */
int exfat_clear_volume_dirty(exfat_sb_info *sbi)
{
    if (sbi == NULL || sbi->boot_buf == NULL || sbi->blocksize == 0u) {
        return -EINVAL;
    }
    return exfat_set_vol_flags(sbi, (uint16_t)(sbi->vol_flags & ~VOLUME_DIRTY));
}

/* ===== boot sector parser (was exfat_dentry.c) ==========================  */
/* ----- merged from exfat_dentry.c ----- */

extern UINT8 *m_aucSysMem0;

/* Identity LE→host on LiteOS-A ARM (LE host); kept as wrappers for BE port. */

/* ---------------------------------------------------------------------------
 * exfat_parse_boot_sector
 * --------------------------------------------------------------------------- */
int exfat_parse_boot_sector(exfat_sb_info *sbi,
                            const struct exfat_boot_sector *bs,
                            uint32_t logical_sector_size)
{
    static const uint8_t exfat_fs_name[BOOTSEC_FS_NAME_LEN] = {
        'E', 'X', 'F', 'A', 'T', ' ', ' ', ' '
    };
    uint32_t i;
    uint32_t sect_size;
    uint64_t fat_bytes;

    if (sbi == NULL || bs == NULL || logical_sector_size == 0) {
        return -EINVAL;
    }

    if (LE16_TO_HOST(bs->signature) != BOOT_SIGNATURE) {
        return -EINVAL;
    }
    if (memcmp(bs->fs_name, exfat_fs_name, BOOTSEC_FS_NAME_LEN) != 0) {
        return -EINVAL;
    }
    for (i = 0; i < BOOTSEC_OLDBPB_LEN; i++) {
        if (bs->must_be_zero[i] != 0) {
            return -EINVAL;
        }
    }
    if (bs->num_fats != 1 && bs->num_fats != 2) {
        return -EINVAL;
    }
    if (bs->sect_size_bits < EXFAT_MIN_SECT_SIZE_BITS ||
        bs->sect_size_bits > EXFAT_MAX_SECT_SIZE_BITS) {
        return -EINVAL;
    }
    if (bs->sect_per_clus_bits > (uint8_t)(25 - bs->sect_size_bits)) {
        return -EINVAL;
    }
    sect_size = 1u << bs->sect_size_bits;
    if (logical_sector_size != sect_size) {
        return -EINVAL;
    }

    /* —— Geometry —— all multi-byte fields go through LE*_TO_HOST per
     *    Invariant exfat-dentry-parse-byte-order. */
    sbi->sect_size_bits      = bs->sect_size_bits;
    sbi->sect_per_clus_bits  = bs->sect_per_clus_bits;
    sbi->num_fats            = bs->num_fats;
    sbi->partition_offset    = LE64_TO_HOST(bs->partition_offset);
    sbi->vol_length          = LE64_TO_HOST(bs->vol_length);
    sbi->fat_offset          = LE32_TO_HOST(bs->fat_offset);
    sbi->fat_length          = LE32_TO_HOST(bs->fat_length);
    sbi->fat2_offset         = (bs->num_fats == 2)
                               ? sbi->fat_offset + sbi->fat_length
                               : sbi->fat_offset;
    sbi->clu_offset          = LE32_TO_HOST(bs->clu_offset);
    sbi->num_clusters        = LE32_TO_HOST(bs->clu_count) + EXFAT_RESERVED_CLUSTERS;
    sbi->root_dir            = LE32_TO_HOST(bs->root_cluster);
    sbi->cluster_size_bits   = (uint32_t)bs->sect_size_bits + (uint32_t)bs->sect_per_clus_bits;
    sbi->cluster_size        = 1u << sbi->cluster_size_bits;
    sbi->blocksize           = sect_size;
    sbi->blocksize_bits      = (uint32_t)bs->sect_size_bits;
    sbi->dentries_per_clu    = sbi->cluster_size >> DENTRY_SIZE_BITS;
    sbi->vol_flags           = LE16_TO_HOST(bs->vol_flags);
    sbi->vol_flags_persistent = sbi->vol_flags & (uint16_t)(VOLUME_DIRTY | MEDIA_FAILURE);
    sbi->s_maxbytes          = (uint64_t)(sbi->num_clusters - EXFAT_RESERVED_CLUSTERS)
                               << sbi->cluster_size_bits;
    sbi->clu_srch_ptr        = EXFAT_FIRST_CLUSTER;
    sbi->used_clusters       = EXFAT_CLUSTERS_UNTRACKED;

    /* —— Cross-field consistency (mirrors Linux exfat_read_boot_sector tail) —— */
    fat_bytes = (uint64_t)sbi->fat_length << bs->sect_size_bits;
    if (fat_bytes < (uint64_t)sbi->num_clusters * 4u) {
        return -EINVAL;
    }
    if ((uint64_t)sbi->clu_offset <
        (uint64_t)sbi->fat_offset +
        (uint64_t)sbi->fat_length * (uint64_t)bs->num_fats) {
        return -EINVAL;
    }

    return 0;
}

/* ---------------------------------------------------------------------------
 * Internal: read a single FAT entry (FAT[clu]) into *next_clu.
 *
 * Returns 0 on success, -EIO on los_part_read failure, -ENOMEM on alloc fail.
 * --------------------------------------------------------------------------- */
static int ReadFatEntry(const exfat_sb_info *sbi, uint32_t clu, uint32_t *next_clu)
{
    uint64_t fat_byte_off;
    uint64_t fat_sector;
    uint32_t in_sector_off;
    uint8_t *fat_buf;
    int err = 0;

    fat_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->blocksize);
    if (fat_buf == NULL) {
        return -ENOMEM;
    }

    fat_byte_off  = (uint64_t)clu * 4u;
    fat_sector    = (uint64_t)sbi->fat_offset + (fat_byte_off / sbi->blocksize);
    in_sector_off = (uint32_t)(fat_byte_off % sbi->blocksize);

    if (los_part_read(sbi->part_id, fat_buf, fat_sector, 1u, TRUE) < 0) {
        err = -EIO;
        goto out;
    }

    {
        uint32_t raw = 0;
        (void)memcpy_s(&raw, sizeof(raw), fat_buf + in_sector_off, sizeof(raw));
        *next_clu = LE32_TO_HOST(raw);
    }

out:
    LOS_MemFree(m_aucSysMem0, fat_buf);
    return err;
}

/* ---------------------------------------------------------------------------
 * exfat_find_root_dentry
 *
 * FAT chain walk with bounded iterations (per Invariant
 * exfat-dentry-fat-traversal-bounded). All allocations released in
 * reverse-LIFO on every return path (per *-buf-leak-free).
 * --------------------------------------------------------------------------- */
int exfat_find_root_dentry(const exfat_sb_info *sbi, uint8_t type,
                           struct exfat_dentry *out)
{
    uint8_t *clu_buf = NULL;
    uint32_t cur_clu;
    uint32_t iter = 0;
    uint32_t sect_per_clus;
    int err = 0;

    if (sbi == NULL || out == NULL) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u || sbi->blocksize == 0u ||
        sbi->root_dir < EXFAT_FIRST_CLUSTER) {
        return -EINVAL;
    }

    sect_per_clus = 1u << sbi->sect_per_clus_bits;
    cur_clu = sbi->root_dir;

    clu_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->cluster_size);
    if (clu_buf == NULL) {
        return -ENOMEM;
    }

    while (iter < sbi->num_clusters) {
        uint64_t data_sector;
        uint32_t i;
        uint32_t next_clu = 0;

        if (cur_clu == EXFAT_EOF_CLUSTER) {
            err = -ENOENT;
            goto out;
        }
        if (cur_clu == EXFAT_BAD_CLUSTER || cur_clu == EXFAT_FREE_CLUSTER ||
            cur_clu < EXFAT_FIRST_CLUSTER || cur_clu >= sbi->num_clusters) {
            err = -EIO;
            goto out;
        }

        data_sector = (uint64_t)sbi->clu_offset +
                      (uint64_t)(cur_clu - EXFAT_FIRST_CLUSTER) * (uint64_t)sect_per_clus;
        if (los_part_read(sbi->part_id, clu_buf, data_sector, sect_per_clus, TRUE) < 0) {
            err = -EIO;
            goto out;
        }

        for (i = 0; i < sbi->dentries_per_clu; i++) {
            const struct exfat_dentry *ep =
                (const struct exfat_dentry *)(clu_buf + ((uint32_t)i << DENTRY_SIZE_BITS));
            if (ep->type == EXFAT_UNUSED) {
                err = -ENOENT;
                goto out;
            }
            if (ep->type == type) {
                if (memcpy_s(out, sizeof(*out), ep, sizeof(*ep)) != EOK) {
                    err = -EINVAL;
                } else {
                    err = 0;
                }
                goto out;
            }
        }

        err = ReadFatEntry(sbi, cur_clu, &next_clu);
        if (err != 0) {
            goto out;
        }
        cur_clu = next_clu;
        iter++;
    }

    /* Exceeded num_clusters iterations — corrupt FAT chain (loop). */
    err = -EIO;

out:
    LOS_MemFree(m_aucSysMem0, clu_buf);
    return err;
}
