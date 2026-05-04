/*
 * Host stub for <dirent.h>:
 *   - macOS provides `d_seekoff` rather than Linux's `d_off`; without this stub,
 *     fs/exfat/exfat_readdir.c's `dirp->d_off = ...` write fails to compile on
 *     Darwin. Linux glibc has both via _GNU_SOURCE.
 *   - We don't actually call any real `opendir/readdir` on the host harness —
 *     the FS code receives a `struct dirent` only as a write target; mock_disk
 *     never goes through the system VFS.
 *
 * Layout chosen to be Linux-glibc compatible (`d_off` as 8-byte signed) so
 * remote (Linux x86_64) and local (macOS arm64) tests build identically.
 */
#ifndef _HOST_STUB_DIRENT_H
#define _HOST_STUB_DIRENT_H

#include <stdint.h>
#include <sys/types.h>

#ifndef NAME_MAX
#define NAME_MAX 255
#endif

#ifndef _DIRENT_HAVE_D_OFF
#define _DIRENT_HAVE_D_OFF 1
#endif

/* Minimum fields the exfat readdir path touches: d_ino / d_off / d_reclen /
 * d_type / d_name. Other glibc fields omitted. */
struct dirent {
    uint64_t d_ino;
    int64_t  d_off;
    uint16_t d_reclen;
    uint8_t  d_type;
    char     d_name[NAME_MAX + 1];
};

/* d_type values (Linux glibc compatible). */
#ifndef DT_UNKNOWN
#define DT_UNKNOWN  0
#endif
#ifndef DT_FIFO
#define DT_FIFO     1
#endif
#ifndef DT_CHR
#define DT_CHR      2
#endif
#ifndef DT_DIR
#define DT_DIR      4
#endif
#ifndef DT_BLK
#define DT_BLK      6
#endif
#ifndef DT_REG
#define DT_REG      8
#endif
#ifndef DT_LNK
#define DT_LNK      10
#endif
#ifndef DT_SOCK
#define DT_SOCK     12
#endif

/* Opaque DIR; never instantiated in the host harness — mock_disk supplies
 * raw FAT/dentry bytes and the test never calls libc opendir. */
typedef struct __DIR DIR;

#endif /* _HOST_STUB_DIRENT_H */
