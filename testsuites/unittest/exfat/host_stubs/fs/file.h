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

/* Mirror of fs/include/fs/file.h::struct file_operations_vfs. exfat_ops.c
 * statically initialises g_exfatFops with these slots; cmocka tests never
 * invoke them, so the slots only need to be addressable for the static
 * initialiser to type-check. */
struct VmMapRegion;
struct stat;
typedef struct poll_table poll_table;
#ifndef _OFF64_T_DECLARED
typedef int64_t off64_t;
#define _OFF64_T_DECLARED
#endif
struct file_operations_vfs {
    int     (*open)(struct file *filep);
    int     (*close)(struct file *filep);
    ssize_t (*read)(struct file *filep, char *buffer, size_t buflen);
    ssize_t (*write)(struct file *filep, const char *buffer, size_t buflen);
    off_t   (*seek)(struct file *filep, off_t offset, int whence);
    int     (*ioctl)(struct file *filep, int cmd, unsigned long arg);
    int     (*mmap)(struct file *filep, struct VmMapRegion *region);
    int     (*poll)(struct file *filep, poll_table *fds);
    int     (*stat)(struct file *filep, struct stat *st);
    int     (*fallocate)(struct file *filep, int mode, off_t offset, off_t len);
    int     (*fallocate64)(struct file *filep, int mode, off64_t offset, off64_t len);
    int     (*fsync)(struct file *filep);
    ssize_t (*readpage)(struct file *filep, char *buffer, size_t buflen);
    int     (*unlink)(struct Vnode *vnode);
};

#endif /* _HOST_STUB_FS_FILE_H */
