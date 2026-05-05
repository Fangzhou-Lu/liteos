/* Host stub for fs/fs.h — provides the minimal struct shapes that
 * exfat_super.c (Mount/Unmount glue) needs:
 *   - struct drv_data + struct block_operations (block-device open/close)
 *   - LOS_OK constant for VfsHashInsert success comparison
 *
 * These types are never actually exercised on the cmocka host harness —
 * VfsExfatMount itself isn't called from any test. The stubs exist purely
 * so the file compiles and contributes its FS-private helpers (which are
 * exercised by tests via the inode/file/cluster/module surface). */
#ifndef _HOST_STUB_FS_FS_H
#define _HOST_STUB_FS_FS_H

#include "fs/file.h"
#include "fs/mount.h"

#ifndef LOS_OK
#define LOS_OK 0
#endif

struct block_operations {
    int (*open)(struct Vnode *node);
    int (*close)(struct Vnode *node);
    int (*read)(struct Vnode *node, char *buf, size_t len);
    int (*write)(struct Vnode *node, const char *buf, size_t len);
    int (*geometry)(struct Vnode *node, void *geo);
    int (*ioctl)(struct Vnode *node, int cmd, unsigned long arg);
    int (*unlink)(struct Vnode *node);
};

struct drv_data {
    const void *ops;     /* points to struct block_operations on real LiteOS-A */
    int         mode;
    void       *priv;
};

#endif /* _HOST_STUB_FS_FS_H */
