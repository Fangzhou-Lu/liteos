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

/*
 * exfat_mkdir.v04draft.c — SpecCompiler v0.4 Loop B experiment output.
 *
 * Contains ONLY VfsExfatMkdir. All helper functions listed in [RELY]
 * (exfat_add_entry, exfat_set_volume_dirty, exfat_clear_volume_dirty,
 * exfat_inode_alloc, exfat_inode_free, exfat_inode_init_dir_chain) are
 * defined in fs/exfat/exfat_inode.c and are NOT reproduced here.
 *
 * DO NOT include in any build target; draft file only.
 */

#include "exfat.h"

#ifdef LOSCFG_FS_EXFAT

/* ---------------------------------------------------------------------------
 * VfsExfatMkdir — VnodeOps.Mkdir callback.
 *
 * Creates a new sub-directory under parent_vp. The operation is bracketed by
 * exfat_set_volume_dirty / exfat_clear_volume_dirty so a crash mid-mkdir
 * leaves the volume marked dirty for next-mount fsck.
 *
 * Locking discipline (spec Refine Prompt Phase 1 / Phase 2):
 *   Phase 1 (mutation): LOS_MuxLock(&sbi->s_lock) → set_volume_dirty →
 *     exfat_add_entry → clear_volume_dirty → LOS_MuxUnlock.
 *   Phase 2 (vnode creation): lock-free; uses only heap + VFS helpers.
 *
 * Error policy:
 *   Case 1 (success):             returns 0; *vpp set.
 *   Case 2 (add_entry failure):   s_lock released; *vpp untouched; returns errno.
 *   Case 3 (vnode alloc failure): s_lock already released; orphan dentry+cluster
 *                                 left on disk (v1 no-rollback policy); returns -ENOMEM.
 * --------------------------------------------------------------------------- */
int VfsExfatMkdir(struct Vnode *parent_vp, const char *name,
                  mode_t mode, struct Vnode **vpp)
{
    exfat_sb_info       *sbi      = NULL;
    exfat_inode_info    *parent_ei = NULL;
    exfat_inode_info    *new_ei   = NULL;
    struct Vnode        *new_vp   = NULL;
    struct exfat_dir_entry info;
    errno_t serr;
    int     sd_ret;
    int     cd_ret;
    int     err = 0;

    (void)mode;   /* exFAT carries no per-file permission bits */

    /* Parameter validation — no lock held. */
    if (parent_vp == NULL || name == NULL || vpp == NULL) {
        return -EINVAL;
    }
    if (parent_vp->originMount == NULL ||
        parent_vp->originMount->data == NULL ||
        parent_vp->data == NULL) {
        return -EINVAL;
    }
    if (name[0] == '\0') {
        return -EINVAL;
    }

    sbi       = (exfat_sb_info *)parent_vp->originMount->data;
    parent_ei = (exfat_inode_info *)parent_vp->data;

    serr = memset_s(&info, sizeof(info), 0, sizeof(info));
    if (serr != EOK) {
        return -EIO;
    }

    /* ---- Phase 1: mutation bracket ---------------------------------------- */

    (void)LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER);

    sd_ret = exfat_set_volume_dirty(sbi);
    if (sd_ret != 0) {
        PRINT_ERR("[%s] exfat_set_volume_dirty: %d\n", __func__, sd_ret);
    }

    err = exfat_add_entry(sbi, parent_vp, name, TYPE_DIR, &info);

    cd_ret = exfat_clear_volume_dirty(sbi);
    if (cd_ret != 0) {
        PRINT_ERR("[%s] exfat_clear_volume_dirty: %d\n", __func__, cd_ret);
    }

    (void)LOS_MuxUnlock(&sbi->s_lock);

    if (err != 0) {
        return err;   /* Case 2: propagate add_entry errno directly */
    }

    /* ---- Phase 2: vnode creation (lock-free) -------------------------------- */

    err = exfat_inode_alloc(&new_ei);
    if (err != 0) {
        PRINT_ERR("[%s] exfat_inode_alloc failed (%d) — orphan on disk\n",
                  __func__, err);
        return -ENOMEM;   /* Case 3 */
    }

    /* Populate ei from the info handoff struct. */
    new_ei->dir           = info.dir;
    new_ei->entry         = info.entry;
    new_ei->type          = info.type;
    new_ei->attr          = info.attr;
    new_ei->start_clu     = info.start_clu;
    new_ei->flags         = info.flags;
    new_ei->size          = info.size;
    new_ei->valid_size    = info.size;
    new_ei->i_size_ondisk = info.size;
    new_ei->num_subdirs   = info.num_subdirs;   /* == EXFAT_MIN_SUBDIR (2) */
    new_ei->i_pos         = ((uint64_t)info.start_clu << 32) |
                            (uint32_t)info.entry;

    /* exfat_inode_init_dir_chain sets dir.{dir,flags,size}, type, start_clu
     * from the new directory cluster; call after manual copy so its values win
     * (matches the approved lookup pattern in exfat_inode.c). */
    exfat_inode_init_dir_chain(new_ei, info.start_clu);
    /* Restore fields that init_dir_chain does not cover. */
    new_ei->size          = info.size;
    new_ei->valid_size    = info.size;
    new_ei->i_size_ondisk = info.size;
    new_ei->attr          = info.attr;
    new_ei->num_subdirs   = info.num_subdirs;

    err = VnodeAlloc(&g_exfatVops, &new_vp);
    if (err != 0) {
        exfat_inode_free(new_ei);
        PRINT_ERR("[%s] VnodeAlloc failed — orphan on disk\n", __func__);
        return -ENOMEM;   /* Case 3 */
    }

    new_vp->type        = VNODE_TYPE_DIR;
    new_vp->vop         = &g_exfatVops;
    new_vp->fop         = &g_exfatFops;
    new_vp->data        = new_ei;
    new_vp->parent      = parent_vp;
    new_vp->originMount = parent_vp->originMount;
    new_vp->uid         = sbi->options.fs_uid;
    new_vp->gid         = sbi->options.fs_gid;
    new_vp->mode        = S_IFDIR | (mode_t)(0755u & ~sbi->options.fs_dmask);

    err = VfsHashInsert(new_vp, (uint32_t)info.start_clu);
    if (err != 0) {
        new_vp->data = NULL;
        (void)VnodeFree(new_vp);
        exfat_inode_free(new_ei);
        PRINT_ERR("[%s] VfsHashInsert failed — orphan on disk\n", __func__);
        return -ENOMEM;   /* Case 3 */
    }

    /* Increment parent num_subdirs in memory only (on-disk sync deferred). */
    parent_ei->num_subdirs++;

    *vpp = new_vp;
    return 0;
}

#endif /* LOSCFG_FS_EXFAT */

/* Assumptions made:
 * A1. exfat_add_entry already returns a negative POSIX errno on failure, so
 *     it is forwarded verbatim without sign-flip (no +ret/-ret inversion needed).
 *     This matches the approved VfsExfatMkdir in exfat_inode.c (err = add_entry;
 *     goto unlock; return err).
 * A2. VfsHashInsert failure is treated as Case 3 (-ENOMEM) per spec Refine
 *     Prompt; the spec text lists only inode_alloc and VnodeAlloc explicitly,
 *     but VfsHashInsert is the final step before success and its failure leaves
 *     the same orphan state.
 * A3. exfat_inode_init_dir_chain is called after the manual field copy and
 *     the overridden fields (size, valid_size, i_size_ondisk, attr, num_subdirs)
 *     are restored afterwards, matching the approved implementation pattern.
 */
