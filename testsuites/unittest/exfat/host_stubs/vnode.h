/* Host stub for fs/vfs/include/vnode.h.
 * Provides the minimal struct shapes used by VFS callback layer TUs:
 *   exfat_open_close.c / exfat_attr.c / exfat_lookup.c /
 *   exfat_file.c / exfat_readdir.c
 * VFS service functions (VnodeAlloc, VnodeFree, VfsHashInsert) are
 * stubbed as simple heap wrappers — sufficient for unit-test isolation. */
#ifndef _HOST_STUB_VNODE_H
#define _HOST_STUB_VNODE_H

#include <stdint.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/types.h>

/* macOS doesn't expose off64_t; LiteOS-A musl + glibc do. Aliasing keeps
 * VOP signatures (VfsExfatTruncate64) source-compatible on the host. */
#if defined(__APPLE__) && !defined(_OFF64_T_DEFINED)
typedef int64_t off64_t;
#define _OFF64_T_DEFINED
#endif

/* Minimal doubly-linked list node (used by Vnode struct fields). */
typedef struct _list_entry { struct _list_entry *pstNext, *pstPrev; } LOS_DL_LIST;
typedef LOS_DL_LIST LIST_HEAD;
typedef LOS_DL_LIST LIST_ENTRY;

/* Forward declarations. */
struct VnodeOps;
struct file_operations_vfs;
struct Mount;
struct page_mapping { int _opaque; };

enum VnodeType {
    VNODE_TYPE_UNKNOWN = 0,
    VNODE_TYPE_REG,
    VNODE_TYPE_DIR,
    VNODE_TYPE_BLK,
    VNODE_TYPE_CHR,
    VNODE_TYPE_FIFO,
    VNODE_TYPE_LNK,
};

struct Vnode {
    enum VnodeType          type;
    int                     useCount;
    uint32_t                hash;
    unsigned int            uid;
    unsigned int            gid;
    mode_t                  mode;
    LIST_HEAD               parentPathCaches;
    LIST_HEAD               childPathCaches;
    struct Vnode           *parent;
    struct VnodeOps        *vop;
    struct file_operations_vfs *fop;
    void                   *data;
    uint32_t                flag;
    LIST_ENTRY              hashEntry;
    LIST_ENTRY              actFreeEntry;
    struct Mount           *originMount;
    struct Mount           *newMount;
    char                   *filePath;
    struct page_mapping     mapping;
};

/* Mirror of fs/vfs/include/vnode.h::struct VnodeOps. exfat_ops.c statically
 * initialises g_exfatVops with these slots; cmocka tests never invoke them,
 * so the slots only need to be addressable for the static initialiser to
 * type-check. */
struct fs_dirent_s;
struct IATTR;
struct VnodeOps {
    int (*Create)(struct Vnode *parent, const char *name, int mode, struct Vnode **vnode);
    int (*Lookup)(struct Vnode *parent, const char *name, int len, struct Vnode **vnode);
    int (*Open)(struct Vnode *vnode, int fd, int mode, int flags);
    ssize_t (*ReadPage)(struct Vnode *vnode, char *buffer, off_t pos);
    ssize_t (*WritePage)(struct Vnode *vnode, char *buffer, off_t pos, size_t buflen);
    int (*Close)(struct Vnode *vnode);
    int (*Reclaim)(struct Vnode *vnode);
    int (*Unlink)(struct Vnode *parent, struct Vnode *vnode, const char *fileName);
    int (*Rmdir)(struct Vnode *parent, struct Vnode *vnode, const char *dirName);
    int (*Mkdir)(struct Vnode *parent, const char *dirName, mode_t mode, struct Vnode **vnode);
    int (*Readdir)(struct Vnode *vnode, struct fs_dirent_s *dir);
    int (*Opendir)(struct Vnode *vnode, struct fs_dirent_s *dir);
    int (*Rewinddir)(struct Vnode *vnode, struct fs_dirent_s *dir);
    int (*Closedir)(struct Vnode *vnode, struct fs_dirent_s *dir);
    int (*Getattr)(struct Vnode *vnode, struct stat *st);
    int (*Setattr)(struct Vnode *vnode, struct stat *st);
    int (*Chattr)(struct Vnode *vnode, struct IATTR *attr);
    int (*Rename)(struct Vnode *src, struct Vnode *dstParent, const char *srcName, const char *dstName);
    int (*Truncate)(struct Vnode *vnode, off_t len);
    int (*Truncate64)(struct Vnode *vnode, off64_t len);
    int (*Fscheck)(struct Vnode *vnode, struct fs_dirent_s *dir);
    int (*Link)(struct Vnode *src, struct Vnode *dstParent, struct Vnode **dst, const char *dstName);
    int (*Symlink)(struct Vnode *parentVnode, struct Vnode **newVnode, const char *path, const char *target);
    ssize_t (*Readlink)(struct Vnode *vnode, char *buffer, size_t bufLen);
};

/* VFS service stubs: allocate a zeroed Vnode from heap (no global lists).
 * VfsHashInsert is a no-op (returns 0) — sufficient for unit tests that
 * only verify the callback's own logic, not the VFS hash table. */
static inline int VnodeAlloc(struct VnodeOps *vop, struct Vnode **out)
{
    struct Vnode *vp;
    (void)vop;
    if (out == NULL) { return -1; }
    vp = (struct Vnode *)calloc(1, sizeof(struct Vnode));
    if (vp == NULL) { return -1; }
    *out = vp;
    return 0;
}

static inline int VnodeFree(struct Vnode *vp)
{
    free(vp);
    return 0;
}

static inline int VfsHashInsert(struct Vnode *vp, uint32_t hash)
{
    (void)vp; (void)hash;
    return 0;
}

static inline int VfsHashGet(const struct Mount *mount, uint32_t hash,
                             struct Vnode **vnode, void *fun, void *arg)
{
    (void)mount; (void)hash; (void)fun; (void)arg;
    if (vnode) { *vnode = NULL; }
    return 0;
}

#endif /* _HOST_STUB_VNODE_H */
