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
#include <stdlib.h>
#include "los_memory.h"
#include "los_mux.h"
#include "los_printf.h"

extern UINT8 *m_aucSysMem0;

/* ---------------------------------------------------------------------------
 * exfat_inode_alloc
 *
 * zalloc(sizeof(exfat_inode_info)) → init LosMuxAttr (PRIO_INHERIT) → LOS_MuxInit.
 *
 * Failure stack (reverse-LIFO cleanup; Invariant exfat-inode-alloc-leak-free):
 *   - zalloc failure                    → -ENOMEM, no allocation made.
 *   - LOS_MuxAttrInit / SetProtocol     → -EIO, free zalloced ei.
 *   - LOS_MuxInit                       → -EIO, destroy attr, free ei.
 * On success the local LosMuxAttr is destroyed (ei->inode_lock has copied the
 * relevant fields) before returning.
 *
 * Invariant exfat-inode-alloc-prio-inherit: PRIO_INHERIT must be set via
 * LOS_MuxAttrSetProtocol — never via direct field assignment, never NULL attr.
 * --------------------------------------------------------------------------- */
int exfat_inode_alloc(exfat_inode_info **out)
{
    exfat_inode_info *ei = NULL;
    LosMuxAttr        attr;
    UINT32            ret;

    if (out == NULL) {
        return -EINVAL;
    }

    ei = (exfat_inode_info *)zalloc(sizeof(exfat_inode_info));
    if (ei == NULL) {
        return -ENOMEM;
    }

    ret = LOS_MuxAttrInit(&attr);
    if (ret != LOS_OK) {
        PRINT_ERR("[%s] LOS_MuxAttrInit failed: 0x%x\n", __func__, ret);
        LOS_MemFree(m_aucSysMem0, ei);
        return -EIO;
    }

    ret = LOS_MuxAttrSetProtocol(&attr, LOS_MUX_PRIO_INHERIT);
    if (ret != LOS_OK) {
        PRINT_ERR("[%s] LOS_MuxAttrSetProtocol failed: 0x%x\n", __func__, ret);
        (void)LOS_MuxAttrDestroy(&attr);
        LOS_MemFree(m_aucSysMem0, ei);
        return -EIO;
    }

    ret = LOS_MuxInit(&ei->inode_lock, &attr);
    if (ret != LOS_OK) {
        PRINT_ERR("[%s] LOS_MuxInit failed: 0x%x\n", __func__, ret);
        (void)LOS_MuxAttrDestroy(&attr);
        LOS_MemFree(m_aucSysMem0, ei);
        return -EIO;
    }

    /* attr is a stack local; LOS_MuxInit copies the protocol bits it needs
     * into the LosMux body, so it's safe to destroy the attr now. */
    (void)LOS_MuxAttrDestroy(&attr);

    *out = ei;
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_inode_free
 *
 * NULL-safe destruct + free. Caller is responsible for ensuring no thread
 * currently holds ei->inode_lock (Invariant exfat-inode-free-idempotent-null-safe
 * does NOT cover double-free of the same non-NULL pointer; caller must NULL
 * out the pointer after calling).
 * --------------------------------------------------------------------------- */
void exfat_inode_free(exfat_inode_info *ei)
{
    if (ei == NULL) {
        return;
    }
    (void)LOS_MuxDestroy(&ei->inode_lock);
    LOS_MemFree(m_aucSysMem0, ei);
}

/* ---------------------------------------------------------------------------
 * exfat_inode_init_dir_chain
 *
 * Pure five-field assignment for a freshly-allocated exfat_inode_info that
 * represents a directory inode (per spec Post-Condition). Caller guarantees
 * start_clu >= EXFAT_FIRST_CLUSTER (Invariant exfat-inode-init-dir-chain-pure;
 * function does not validate).
 * --------------------------------------------------------------------------- */
void exfat_inode_init_dir_chain(exfat_inode_info *ei, uint32_t start_clu)
{
    ei->dir.dir   = start_clu;
    ei->dir.flags = (uint8_t)ALLOC_FAT_CHAIN;
    ei->dir.size  = 0;
    ei->type      = (uint32_t)TYPE_DIR;
    ei->start_clu = start_clu;
}

#endif /* LOSCFG_FS_EXFAT */
