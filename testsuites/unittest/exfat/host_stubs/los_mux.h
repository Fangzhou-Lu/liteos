/* Host stub: LosMux / LosMuxAttr are opaque no-ops returning LOS_OK.
 * Covers: init/destroy/lock/unlock + attr lifecycle + protocol constants. */
#ifndef _HOST_STUB_LOS_MUX_H
#define _HOST_STUB_LOS_MUX_H

#include "los_typedef.h"

typedef struct { int _opaque; } LosMux;
typedef struct { int _opaque; } LosMuxAttr;

#define LOS_WAIT_FOREVER         0xFFFFFFFFu
#define LOS_MUX_PRIO_INHERIT     1
#define LOS_MUX_PRIO_PROTECT     2
#define LOS_MUX_DEFAULT_PROTOCOL 0

static inline UINT32 LOS_MuxInit(LosMux *m, const LosMuxAttr *a)
    { (void)m; (void)a; return LOS_OK; }
static inline UINT32 LOS_MuxDestroy(LosMux *m)
    { (void)m; return LOS_OK; }
static inline UINT32 LOS_MuxLock(LosMux *m, UINT32 t)
    { (void)m; (void)t; return LOS_OK; }
static inline UINT32 LOS_MuxUnlock(LosMux *m)
    { (void)m; return LOS_OK; }

/* LosMuxAttr lifecycle — used by exfat_inode_alloc. */
static inline UINT32 LOS_MuxAttrInit(LosMuxAttr *a)
    { (void)a; return LOS_OK; }
static inline UINT32 LOS_MuxAttrDestroy(LosMuxAttr *a)
    { (void)a; return LOS_OK; }
static inline UINT32 LOS_MuxAttrSetProtocol(LosMuxAttr *a, int proto)
    { (void)a; (void)proto; return LOS_OK; }
static inline UINT32 LOS_MuxAttrGetProtocol(const LosMuxAttr *a, int *proto)
    { (void)a; if (proto) { *proto = LOS_MUX_PRIO_INHERIT; } return LOS_OK; }

#endif /* _HOST_STUB_LOS_MUX_H */
