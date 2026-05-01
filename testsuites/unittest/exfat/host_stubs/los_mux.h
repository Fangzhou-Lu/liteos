/* Host stub: LosMux is opaque; init/destroy/lock/unlock are no-ops returning LOS_OK. */
#ifndef _HOST_STUB_LOS_MUX_H
#define _HOST_STUB_LOS_MUX_H

#include "los_typedef.h"

typedef struct {
    int _opaque;
} LosMux;

typedef struct {
    int _opaque;
} LosMuxAttr;

#define LOS_WAIT_FOREVER 0xFFFFFFFFu

static inline UINT32 LOS_MuxInit(LosMux *m, const LosMuxAttr *a)    { (void)m; (void)a; return LOS_OK; }
static inline UINT32 LOS_MuxDestroy(LosMux *m)                       { (void)m; return LOS_OK; }
static inline UINT32 LOS_MuxLock(LosMux *m, UINT32 t)                { (void)m; (void)t; return LOS_OK; }
static inline UINT32 LOS_MuxUnlock(LosMux *m)                        { (void)m; return LOS_OK; }

#endif
