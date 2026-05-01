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

#include "exfat.h"
#ifdef LOSCFG_FS_EXFAT

#include <errno.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define UMASK_MAX_VALUE   0777u   /* 9-bit POSIX permission bits */
#define TIME_OFFSET_MIN   (-24 * 60)
#define TIME_OFFSET_MAX   ( 24 * 60)

/* Static iocharset string — placed in .rodata; never freed (per Invariant
 * exfat-options-no-heap-leak). */
static char g_iocharset_utf8[] = "utf8";

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

#endif /* LOSCFG_FS_EXFAT */
