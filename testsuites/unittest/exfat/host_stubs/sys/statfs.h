#ifndef _HOST_STUB_SYS_STATFS_H
#define _HOST_STUB_SYS_STATFS_H

/* Host harness shim for `struct statfs`.
 *
 * exfat_super.c writes Linux-shape fields (f_type, f_bsize, f_blocks,
 * f_bfree, f_bavail, f_files, f_ffree, f_namelen). macOS / *BSD's
 * <sys/mount.h> uses incompatible field names (e.g. f_namemax instead of
 * f_namelen, plus extras like f_owner). To keep production code unchanged,
 * we provide a Linux-shape stub on Apple/BSD; on Linux we delegate to the
 * real header via #include_next.
 *
 * cmocka tests exercise FS-internal helpers only — no real statfs() syscall
 * is invoked, so the stub need only be addressable. */

#if defined(__APPLE__) || defined(__FreeBSD__)

#include <stdint.h>

typedef struct {
    int val[2];
} __host_fsid_t;

struct statfs {
    long           f_type;
    long           f_bsize;
    uint64_t       f_blocks;
    uint64_t       f_bfree;
    uint64_t       f_bavail;
    uint64_t       f_files;
    uint64_t       f_ffree;
    __host_fsid_t  f_fsid;
    long           f_namelen;
    long           f_frsize;
    long           f_flags;
    long           f_spare[4];
};

#else  /* Linux/glibc */
#include_next <sys/statfs.h>
#endif

#endif /* _HOST_STUB_SYS_STATFS_H */
