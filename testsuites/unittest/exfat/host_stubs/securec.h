/* Host stub for libsec — minimal memcpy_s/memset_s/strncpy_s.
 * Returns EOK on success, non-zero on bounds error. */
#ifndef _HOST_STUB_SECUREC_H
#define _HOST_STUB_SECUREC_H

#include <stddef.h>
#include <string.h>

typedef int errno_t;
#define EOK 0

static inline errno_t memcpy_s(void *dest, size_t destMax, const void *src, size_t count)
{
    if (dest == NULL || src == NULL || count > destMax) return -1;
    memcpy(dest, src, count);
    return EOK;
}

static inline errno_t memset_s(void *dest, size_t destMax, int c, size_t count)
{
    if (dest == NULL || count > destMax) return -1;
    memset(dest, c, count);
    return EOK;
}

static inline errno_t strncpy_s(char *dest, size_t destMax, const char *src, size_t count)
{
    if (dest == NULL || src == NULL || destMax == 0) return -1;
    size_t n = 0;
    while (n < count && src[n] != '\0') n++;
    if (n + 1 > destMax) return -1;
    memcpy(dest, src, n);
    dest[n] = '\0';
    return EOK;
}

#endif
