#ifndef _HOST_STUB_FS_MOUNT_H
#define _HOST_STUB_FS_MOUNT_H

#include <stdint.h>
#include <sys/statfs.h>  /* host_stubs/sys/statfs.h shims macOS → sys/mount.h */
#include "vnode.h"

/* PATH_MAX: use system limit if available, else define a safe default. */
#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

struct Mount;

/* Mirror of the real fs/include/fs/mount.h MountOps shape. exfat_super.c
 * initialises g_exfatMountOps with these slots. The Mount/Unmount/Statfs/Sync
 * narratives never run during cmocka tests (only FS-internal helpers are
 * exercised), so the slots only need to be addressable for the static
 * initialiser to type-check. */
struct MountOps {
    int (*Mount)(struct Mount *mount, struct Vnode *vnode, const void *data);
    int (*Unmount)(struct Mount *mount, struct Vnode **blkdriver);
    int (*Statfs)(struct Mount *mount, struct statfs *sbp);
    int (*Sync)(struct Mount *mount);
};

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
