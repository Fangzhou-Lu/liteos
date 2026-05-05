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
/* exfat_nls — UTF-16 ↔ UTF-8 conversion + Microsoft upcase compare.
 * Mirrors Linux fs/exfat/nls.c (LiteOS strict RFC 3629 UTF-8). */

#include <errno.h>
#include <stddef.h>
#include <stdint.h>

#include "exfat.h"

#define UTF16_HIGH_SURROGATE_MIN  0xD800u
#define UTF16_HIGH_SURROGATE_MAX  0xDBFFu
#define UTF16_LOW_SURROGATE_MIN   0xDC00u
#define UTF16_LOW_SURROGATE_MAX   0xDFFFu
#define UNICODE_MAX               0x10FFFFu
#define UNICODE_BMP_MAX           0xFFFFu

/* ----- merged from exfat_nls_utf16.c ----- */

/* UTF-16 / UTF-8 boundary constants. */

/* Helpers (file-static, no external linkage). */
static int IsHighSurrogate(uint16_t u)
{
    return (u >= UTF16_HIGH_SURROGATE_MIN && u <= UTF16_HIGH_SURROGATE_MAX);
}
static int IsLowSurrogate(uint16_t u)
{
    return (u >= UTF16_LOW_SURROGATE_MIN && u <= UTF16_LOW_SURROGATE_MAX);
}
static int IsAnySurrogate(uint16_t u)
{
    return (u >= UTF16_HIGH_SURROGATE_MIN && u <= UTF16_LOW_SURROGATE_MAX);
}

/* ---------------------------------------------------------------------------
 * exfat_uni_to_utf8
 *
 * Decodes UTF-16 code units (host endianness) to UTF-8 bytes.
 * Handles surrogate pairs (high + low → 4-byte supplementary plane).
 * NUL (uni[i] == 0x0000) emitted as 1-byte 0x00 (matches Linux exfat;
 * Invariant exfat-nls-utf16-no-modified-utf8).
 * --------------------------------------------------------------------------- */
int exfat_uni_to_utf8(const uint16_t *uni, int uni_len,
                      char *out, int out_max)
{
    int written = 0;
    int i;

    if (out == NULL || out_max < 1) {
        return -EINVAL;
    }
    if (uni_len < 0 || uni_len > EXFAT_MAX_NAME_LEN) {
        return -EINVAL;
    }
    if (uni == NULL && uni_len > 0) {
        return -EINVAL;
    }

    for (i = 0; i < uni_len; i++) {
        uint32_t cp;
        uint16_t u = uni[i];

        if (IsHighSurrogate(u)) {
            uint16_t lo;
            if (i + 1 >= uni_len) {
                return -EINVAL;   /* lone high surrogate */
            }
            lo = uni[i + 1];
            if (!IsLowSurrogate(lo)) {
                return -EINVAL;   /* high not followed by low */
            }
            cp = 0x10000u
                + (uint32_t)(u - UTF16_HIGH_SURROGATE_MIN) * 0x400u
                + (uint32_t)(lo - UTF16_LOW_SURROGATE_MIN);
            i++;   /* consume both halves */
        } else if (IsLowSurrogate(u)) {
            return -EINVAL;       /* lone low surrogate */
        } else {
            cp = (uint32_t)u;
        }

        /* Encode cp to UTF-8. */
        if (cp < 0x80u) {
            if (written + 1 > out_max) {
                return -ENAMETOOLONG;
            }
            out[written++] = (char)(uint8_t)cp;
        } else if (cp < 0x800u) {
            if (written + 2 > out_max) {
                return -ENAMETOOLONG;
            }
            out[written++] = (char)(uint8_t)(0xC0u | (cp >> 6));
            out[written++] = (char)(uint8_t)(0x80u | (cp & 0x3Fu));
        } else if (cp <= UNICODE_BMP_MAX) {
            if (written + 3 > out_max) {
                return -ENAMETOOLONG;
            }
            out[written++] = (char)(uint8_t)(0xE0u | (cp >> 12));
            out[written++] = (char)(uint8_t)(0x80u | ((cp >> 6) & 0x3Fu));
            out[written++] = (char)(uint8_t)(0x80u | (cp & 0x3Fu));
        } else {
            /* supplementary plane → 4-byte UTF-8 */
            if (written + 4 > out_max) {
                return -ENAMETOOLONG;
            }
            out[written++] = (char)(uint8_t)(0xF0u | (cp >> 18));
            out[written++] = (char)(uint8_t)(0x80u | ((cp >> 12) & 0x3Fu));
            out[written++] = (char)(uint8_t)(0x80u | ((cp >> 6) & 0x3Fu));
            out[written++] = (char)(uint8_t)(0x80u | (cp & 0x3Fu));
        }
    }

    return written;
}

/* ---------------------------------------------------------------------------
 * exfat_utf8_to_uni
 *
 * Encodes UTF-8 bytes to UTF-16 code units. RFC 3629 strict: rejects overlong
 * forms, raw surrogate codepoints in input, and > 0x10FFFF (Invariant
 * exfat-nls-utf16-utf8-rfc3629-strict).
 *
 * Supplementary plane codepoints (>0xFFFF) emitted as a high+low surrogate
 * pair occupying 2 uint16 slots.
 * --------------------------------------------------------------------------- */
int exfat_utf8_to_uni(const char *utf8, int utf8_len,
                      uint16_t *uni, int uni_max, int *uni_len_out)
{
    int u_count = 0;
    int i = 0;

    if (uni == NULL || uni_max < 1 || uni_len_out == NULL) {
        return -EINVAL;
    }
    if (utf8_len < 0) {
        return -EINVAL;
    }
    if (utf8 == NULL && utf8_len > 0) {
        return -EINVAL;
    }

    while (i < utf8_len) {
        uint8_t  b0 = (uint8_t)utf8[i];
        uint32_t cp;
        uint32_t min_cp;
        int extra;
        int j;

        if (b0 < 0x80u) {
            cp     = (uint32_t)b0;
            extra  = 0;
            min_cp = 0u;
        } else if ((b0 & 0xE0u) == 0xC0u) {
            cp     = (uint32_t)(b0 & 0x1Fu);
            extra  = 1;
            min_cp = 0x80u;
        } else if ((b0 & 0xF0u) == 0xE0u) {
            cp     = (uint32_t)(b0 & 0x0Fu);
            extra  = 2;
            min_cp = 0x800u;
        } else if ((b0 & 0xF8u) == 0xF0u) {
            cp     = (uint32_t)(b0 & 0x07u);
            extra  = 3;
            min_cp = 0x10000u;
        } else {
            return -EINVAL;   /* illegal start byte: 0x80..0xBF, 0xC0/C1, 0xF5..0xFF */
        }

        /* Linux exfat reject: 0xC0 (overlong-NUL header), 0xC1 — caught by
         * min_cp check below since their decoded cp would be < 0x80. */

        if (i + 1 + extra > utf8_len) {
            return -EINVAL;   /* truncated multi-byte sequence */
        }
        i++;

        for (j = 0; j < extra; j++) {
            uint8_t bn = (uint8_t)utf8[i++];
            if ((bn & 0xC0u) != 0x80u) {
                return -EINVAL;   /* malformed continuation byte */
            }
            cp = (cp << 6) | (uint32_t)(bn & 0x3Fu);
        }

        /* RFC 3629 rejection set. */
        if (cp < min_cp) {
            return -EINVAL;       /* overlong encoding */
        }
        if (cp > UNICODE_MAX) {
            return -EINVAL;       /* > U+10FFFF */
        }
        if (cp >= UTF16_HIGH_SURROGATE_MIN && cp <= UTF16_LOW_SURROGATE_MAX) {
            return -EINVAL;       /* raw surrogate codepoint */
        }

        /* Emit UTF-16. */
        if (cp <= UNICODE_BMP_MAX) {
            if (u_count + 1 > uni_max) {
                return -ENAMETOOLONG;
            }
            uni[u_count++] = (uint16_t)cp;
        } else {
            uint32_t adj = cp - 0x10000u;
            if (u_count + 2 > uni_max) {
                return -ENAMETOOLONG;
            }
            uni[u_count++] = (uint16_t)(UTF16_HIGH_SURROGATE_MIN + (adj >> 10));
            uni[u_count++] = (uint16_t)(UTF16_LOW_SURROGATE_MIN + (adj & 0x3FFu));
        }
    }

    *uni_len_out = u_count;
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_uniname_cmp
 *
 * Case-insensitive compare via sbi->vol_utbl upcase table on BMP code units.
 * Surrogate halves bypass the table (Invariant
 * exfat-nls-utf16-surrogate-pair-bit-exact); they are compared as raw u16.
 *
 * Length-mismatch tie-breaker: shorter prefix is "less" (Invariant
 * exfat-nls-utf16-cmp-prefix-rule).
 * --------------------------------------------------------------------------- */
int exfat_uniname_cmp(const exfat_sb_info *sbi,
                      const uint16_t *a, int a_len,
                      const uint16_t *b, int b_len)
{
    int min_len;
    int i;

    if (sbi == NULL || sbi->vol_utbl == NULL) {
        return -EINVAL;
    }
    if (a_len < 0 || b_len < 0) {
        return -EINVAL;
    }
    if ((a == NULL && a_len > 0) || (b == NULL && b_len > 0)) {
        return -EINVAL;
    }

    min_len = (a_len < b_len) ? a_len : b_len;

    for (i = 0; i < min_len; i++) {
        uint16_t ai  = a[i];
        uint16_t bi  = b[i];
        uint16_t a_up = IsAnySurrogate(ai) ? ai : sbi->vol_utbl[ai];
        uint16_t b_up = IsAnySurrogate(bi) ? bi : sbi->vol_utbl[bi];

        if (a_up < b_up) {
            return -1;
        }
        if (a_up > b_up) {
            return 1;
        }
    }

    if (a_len < b_len) {
        return -1;
    }
    if (a_len > b_len) {
        return 1;
    }
    return 0;
}
