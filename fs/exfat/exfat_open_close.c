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
#ifdef LOSCFG_FS_EXFAT

#include <errno.h>
#include <stddef.h>
#include "vnode.h"

/* ---------------------------------------------------------------------------
 * VfsExfatOpen — file_operations_vfs.open callback (Wave A stub).
 *
 * Mirrors Linux's default .open = generic_file_open (exfat doesn't override).
 * No allocation, no lock, no IO — just type-check the vnode and let read take
 * over. All per-file metadata is stored on the vnode (vp->data), so no
 * per-open state lives in filep->f_priv.
 *
 * Returns 0 / -EISDIR / -EINVAL. Never modifies sbi, ei, vp, or filep state.
 * --------------------------------------------------------------------------- */
int VfsExfatOpen(struct file *filep)
{
    struct Vnode *vp;
    exfat_inode_info *ei;

    if (filep == NULL) {
        return -EINVAL;
    }
    vp = filep->f_vnode;
    if (vp == NULL || vp->originMount == NULL || vp->data == NULL) {
        return -EINVAL;
    }

    ei = (exfat_inode_info *)vp->data;

    /* Invariant exfat-open-rejects-dir: directory should go through the
     * Opendir/Readdir bundle on g_exfatVops, not the file open path. */
    if (vp->type == VNODE_TYPE_DIR || ei->type == TYPE_DIR) {
        return -EISDIR;
    }
    if (vp->type != VNODE_TYPE_REG || ei->type != TYPE_FILE) {
        return -EINVAL;
    }

    /* Invariant exfat-open-pos-untouched / exfat-open-no-alloc:
     * sys_open guarantees f_pos == 0 and f_priv == NULL on entry; leave both. */
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatClose — file_operations_vfs.close callback (Wave A stub).
 *
 * Mirrors Linux exfat's choice to leave .release unimplemented (VFS skips the
 * FS-level callback). Wave A allocates no per-open state and the FS is
 * read-only, so there is nothing to release or flush. Always returns 0.
 *
 * Wave B (write path) will extend this to acquire sbi->s_lock and call the
 * future fsync/truncate-on-close logic; the spec invariants reserve room.
 * --------------------------------------------------------------------------- */
int VfsExfatClose(struct file *filep)
{
    (void)filep;
    return 0;
}

#endif /* LOSCFG_FS_EXFAT */
