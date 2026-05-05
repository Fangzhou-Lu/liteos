/*
 * Copyright (c) 2024-2026 Huawei Device Co., Ltd. All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without modification,
 * are permitted provided that the following conditions are met:
 *
 * 1. Redistributions of source code must retain the above copyright notice, this list of
 *    conditions and the following disclaimer.
 *
 * 2. Redistributions in binary form must reproduce the above copyright notice, this list
 *    of conditions and the following disclaimer in the documentation and/or other materials
 *    provided with the distribution.
 *
 * 3. Neither the name of the copyright holder nor the names of its contributors may be used
 *    to endorse or promote products derived from this software without specific prior written
 *    permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS
 * OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
 * COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
 * GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
 * AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
 * NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
 * OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "exfat.h"

/*
 * exFAT VFS operations tables — Linux-style static initialization.
 *
 * Mirrors Linux's pattern in fs/exfat/{namei,file,dir}.c::
 *     const struct inode_operations exfat_dir_inode_operations = {
 *         .lookup  = exfat_lookup,
 *         .create  = exfat_create,
 *         ...
 *     };
 *
 * Slots intentionally left NULL are not yet implemented; LiteOS-A VFS returns
 * -ENOSYS for NULL ops. NEVER patch these tables at mount-time — that historic
 * pattern caused the umount panic via NULL Reclaim hook.
 *
 * Wave A (read path) populates: Reclaim, Lookup, Opendir, Readdir, Closedir,
 * Rewinddir. Wave B (write path) will add: Create, Mkdir, Unlink, Rmdir,
 * Rename, Truncate, Open, Close, Read, Write — by editing this file.
 */
struct VnodeOps g_exfatVops = {
    .Lookup     = VfsExfatLookup,
    .Reclaim    = VfsExfatReclaim,
    .Opendir    = VfsExfatOpendir,
    .Readdir    = VfsExfatReaddir,
    .Closedir   = VfsExfatClosedir,
    .Rewinddir  = VfsExfatRewinddir,
    .Getattr    = VfsExfatGetattr,
    .Mkdir      = VfsExfatMkdir,
    .Truncate   = VfsExfatTruncate,
    .Truncate64 = VfsExfatTruncate64,
};

/*
 * exFAT file_operations_vfs — Linux-style static initialization. Mirrors
 * Linux's `const struct file_operations exfat_file_operations = { ... };`
 * in fs/exfat/file.c, with read implemented via fat-chain + los_part_read
 * (no page cache equivalent in LiteOS-A). Other slots filled in subsequent
 * Wave A stages: open / close (Stage 8), seek (Stage 9 vfs_ops_filled refine).
 */
struct file_operations_vfs g_exfatFops = {
    .open  = VfsExfatOpen,
    .close = VfsExfatClose,
    .read  = VfsExfatRead,
    .write = VfsExfatWrite,
    .seek  = VfsExfatSeek,
};
