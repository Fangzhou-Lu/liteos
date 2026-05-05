/* Host stub for fs/vfs/include/path_cache.h.
 * exfat_super.c only references VnodePathCacheFree as part of the Unmount
 * narrative (in comments + the actual VFS call); since Unmount isn't tested
 * from cmocka, we make it a no-op for compilation. */
#ifndef _HOST_STUB_PATH_CACHE_H
#define _HOST_STUB_PATH_CACHE_H

#include "vnode.h"

static inline void VnodePathCacheFree(struct Vnode *vp)
{
    (void)vp;
}

#endif /* _HOST_STUB_PATH_CACHE_H */
