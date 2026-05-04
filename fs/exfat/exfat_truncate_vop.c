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
#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
#include "los_printf.h"
#include "los_mux.h"
#include "vnode.h"

int VfsExfatTruncate64(struct Vnode *vp, off64_t len)
{
    struct Mount     *mount;
    exfat_sb_info    *sbi;
    exfat_inode_info *ei;
    uint64_t          new_size;
    int               ret;

    if (vp == NULL || vp->originMount == NULL || vp->data == NULL) {
        return -EINVAL;
    }
    if (len < 0) {
        return -EINVAL;
    }
    mount = vp->originMount;
    sbi   = (exfat_sb_info *)mount->data;
    ei    = (exfat_inode_info *)vp->data;
    if (sbi == NULL) {
        return -EINVAL;
    }

    (void)LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER);

    new_size = (uint64_t)len;
    if (new_size == ei->size) {
        ret = 0;
        goto unlock_out;
    }
    if (new_size > ei->size) {
        ret = exfat_truncate_extend(sbi, ei, new_size);
    } else {
        ret = exfat_truncate_shrink(sbi, ei, new_size);
    }

unlock_out:
    (void)LOS_MuxUnlock(&ei->inode_lock);
    return ret;
}

int VfsExfatTruncate(struct Vnode *vp, off_t len)
{
    return VfsExfatTruncate64(vp, (off64_t)len);
}

#endif /* LOSCFG_FS_EXFAT */
