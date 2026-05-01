#ifndef _HOST_STUB_FS_FILE_H
#define _HOST_STUB_FS_FILE_H

#include <sys/types.h>
#include <stdint.h>

/* loff_t is a 64-bit file offset; defined by glibc in large-file mode but
 * not guaranteed available without _LARGEFILE64_SOURCE. Define it here so
 * the struct file layout matches the kernel definition. */
#ifndef _LOFF_T_DECLARED
typedef int64_t loff_t;
#define _LOFF_T_DECLARED
#endif

struct Vnode;

/* Minimal struct file — only the fields accessed by exfat VFS callbacks. */
struct file {
    unsigned int  f_magicnum;
    int           f_oflags;
    struct Vnode *f_vnode;
    loff_t        f_pos;
    unsigned long f_refcount;
    char         *f_path;
    void         *f_priv;
    const char   *f_relpath;
    void         *f_mapping;
    void         *f_dir;
    const struct file_operations_vfs *ops;
    int           fd;
};

struct file_operations_vfs { int _opaque; };

#endif /* _HOST_STUB_FS_FILE_H */
