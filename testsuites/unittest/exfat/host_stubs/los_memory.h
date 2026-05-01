/* Host stub: LOS_MemAlloc/Free → libc; m_aucSysMem0 is a sentinel pointer. */
#ifndef _HOST_STUB_LOS_MEMORY_H
#define _HOST_STUB_LOS_MEMORY_H

#include "los_typedef.h"
#include <stdlib.h>
#include <string.h>

extern UINT8 *m_aucSysMem0;

static inline VOID *LOS_MemAlloc(VOID *pool, UINT32 size)
{
    (void)pool;
    return malloc(size);
}

static inline UINT32 LOS_MemFree(VOID *pool, VOID *ptr)
{
    (void)pool;
    free(ptr);
    return LOS_OK;
}

static inline VOID *zalloc(size_t size)
{
    return calloc(1, size);
}

#endif
