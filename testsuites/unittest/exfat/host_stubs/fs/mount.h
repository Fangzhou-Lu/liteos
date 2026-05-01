#ifndef _HOST_STUB_FS_MOUNT_H
#define _HOST_STUB_FS_MOUNT_H

#include <stdint.h>
#include "vnode.h"

/* PATH_MAX: use system limit if available, else define a safe default. */
#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

struct MountOps { int _opaque; };

/* Minimal struct Mount — only the fields accessed by exfat VFS callbacks.
 * The LIST_HEAD/LIST_ENTRY fields come from vnode.h stub. */
struct Mount {
    LIST_ENTRY      mountList;
    const struct MountOps *ops;
    struct Vnode   *vnodeBeCovered;
    struct Vnode   *vnodeCovered;
    struct Vnode   *vnodeDev;
    LIST_HEAD       vnodeList;
    int             vnodeSize;
    LIST_HEAD       activeVnodeList;
    int             activeVnodeSize;
    void           *data;
    uint32_t        hashseed;
    unsigned long   mountFlags;
    char            pathName[PATH_MAX];
    char            devName[PATH_MAX];
};

#endif /* _HOST_STUB_FS_MOUNT_H */
