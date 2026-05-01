/* Host stub for fs/include/fs/dirent_fs.h.
 * Provides the minimal struct fs_dirent_s shape used by exfat_readdir.c:
 *   fd_int_offset, fd_position, u.fs_dir, read_cnt, fd_dir[].
 * All other fs-specific union members are omitted. */
#ifndef _HOST_STUB_FS_DIRENT_FS_H
#define _HOST_STUB_FS_DIRENT_FS_H

#include <dirent.h>
#include <sys/types.h>
#include <stdint.h>
#include "vnode.h"

/* Match the kernel LOSCFG_ENABLE_READ_BUFFER=y layout: fd_dir[MAX_DIRENT_NUM].
 * Without the flag the kernel uses fd_dir[1]; for tests we use a small fixed
 * array of 16 which is enough for any readdir batch test. */
#define HOST_STUB_MAX_DIRENT_NUM 16

typedef void *fs_dir_s;

struct fs_dirent_s {
    struct Vnode   *fd_root;
    unsigned int    fd_flags;
    off_t           fd_position;
    off_t           fd_int_offset;
    struct {
        void       *pseudo;   /* fs_pseudodir_s placeholder */
        fs_dir_s    fs_dir;
    } u;
    struct dirent   fd_dir[HOST_STUB_MAX_DIRENT_NUM];
    int16_t         cur_pos;
    int16_t         end_pos;
    int32_t         read_cnt;
    int             fd_status;
};

#endif /* _HOST_STUB_FS_DIRENT_FS_H */
