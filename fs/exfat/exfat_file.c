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
/* exfat_file — file IO (read/write), directory IO (readdir + dentry-set
 * read/write/alloc-slot), truncate (extend/shrink/dispatch).
 * Mirrors the union of Linux fs/exfat/{dir,file}.c. */

#include <dirent.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#include "exfat.h"
#include "disk.h"
#include "fs/dirent_fs.h"
#include "fs/mount.h"
#include "los_memory.h"
#include "los_mux.h"
#include "los_printf.h"
#include "securec.h"
#include "vnode.h"

#define LE16_TO_HOST(x) ((uint16_t)(x))
#define EXFAT_MAX_CHAIN_LEN 0x10000000u

/* ----- merged from exfat_dentry_iter.c ----- */

extern UINT8 *m_aucSysMem0;

/* Identity LE→host wrapper; mirrors exfat_dentry.c / exfat_fat_chain.c
 * (Invariant exfat-dentry-iter-le-host-only). */

/* ---------------------------------------------------------------------------
 * exfat_get_dentry
 *
 * Locate the cluster + sector holding the dentry at linear index `entry_idx`,
 * read that sector, and copy the 32B dentry into *out. ALLOC_NO_FAT_CHAIN
 * directories are addressed by simple addition (contiguous); ALLOC_FAT_CHAIN
 * directories require walking the FAT chain via exfat_get_next_cluster.
 *
 * Bounded: chain walk stops on EOF (-EIO if entry_idx not yet reached).
 * Leak-free: single LOS_MemAlloc + matching LOS_MemFree at out: label.
 * --------------------------------------------------------------------------- */
int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                     int entry_idx, struct exfat_dentry *out,
                     uint64_t *out_sector)
{
    uint8_t *buf = NULL;
    uint64_t byte_off;
    uint64_t sector_lba;
    uint32_t clu_offset;
    uint32_t byte_in_clu;
    uint32_t sector_in_clu;
    uint32_t byte_in_sector;
    uint32_t cur_clu;
    uint32_t i;
    int err = 0;

    if (sbi == NULL || dir == NULL || out == NULL || entry_idx < 0) {
        return -EINVAL;
    }
    if (dir->flags != (uint8_t)ALLOC_FAT_CHAIN &&
        dir->flags != (uint8_t)ALLOC_NO_FAT_CHAIN) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u || sbi->blocksize == 0u ||
        dir->dir < EXFAT_FIRST_CLUSTER) {
        return -EINVAL;
    }

    byte_off       = (uint64_t)(uint32_t)entry_idx * (uint64_t)DENTRY_SIZE;
    clu_offset     = (uint32_t)(byte_off / sbi->cluster_size);
    byte_in_clu    = (uint32_t)(byte_off % sbi->cluster_size);
    sector_in_clu  = byte_in_clu / sbi->blocksize;
    byte_in_sector = byte_in_clu % sbi->blocksize;

    /* Skip clu_offset clusters along dir's chain. */
    cur_clu = dir->dir;
    if (dir->flags == (uint8_t)ALLOC_NO_FAT_CHAIN) {
        cur_clu += clu_offset;
        if (cur_clu < EXFAT_FIRST_CLUSTER || cur_clu >= sbi->num_clusters) {
            return -EIO;
        }
    } else {
        for (i = 0; i < clu_offset; i++) {
            uint32_t next_clu = 0;
            err = exfat_get_next_cluster(sbi, cur_clu, &next_clu);
            if (err != 0) {
                return err;
            }
            if (next_clu == EXFAT_EOF_CLUSTER) {
                return -EIO;
            }
            cur_clu = next_clu;
        }
        err = 0;
    }

    sector_lba = exfat_clu_to_sector(sbi, cur_clu) + (uint64_t)sector_in_clu;

    buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->blocksize);
    if (buf == NULL) {
        return -ENOMEM;
    }

    if (los_part_read(sbi->part_id, buf, sector_lba, 1u, TRUE) < 0) {
        err = -EIO;
        goto out;
    }

    if (memcpy_s(out, sizeof(*out),
                 buf + byte_in_sector, sizeof(*out)) != EOK) {
        err = -EIO;
        goto out;
    }

    if (out_sector != NULL) {
        *out_sector = sector_lba;
    }

out:
    LOS_MemFree(m_aucSysMem0, buf);
    return err;
}

/* ---------------------------------------------------------------------------
 * exfat_get_dentry_set
 *
 * Pull a contiguous file dentry-set from the directory chain. Each dentry is
 * fetched by an independent exfat_get_dentry call so cluster-boundary cases
 * are handled transparently (Invariant exfat-dentry-iter-set-cluster-boundary
 * — alternative big-read implementations are explicitly forbidden by spec).
 *
 * On any failure, *num_entries is left undefined and the caller must treat
 * `set` as garbage (Invariant exfat-dentry-iter-set-not-mutated-on-failure).
 * --------------------------------------------------------------------------- */
int exfat_get_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
                         int start_entry, struct exfat_dentry *set,
                         int max_entries, int *num_entries)
{
    int total;
    int i;
    int err;

    if (sbi == NULL || dir == NULL || set == NULL || num_entries == NULL) {
        return -EINVAL;
    }
    if (start_entry < 0 || max_entries < 1 ||
        max_entries > (int)EXFAT_DENTRY_SET_MAX) {
        return -EINVAL;
    }

    /* Step 1: read the primary at start_entry. */
    err = exfat_get_dentry(sbi, dir, start_entry, &set[0], NULL);
    if (err != 0) {
        return err;
    }

    /* Step 2: validate it's an in-use file primary. */
    if (set[0].type != (uint8_t)EXFAT_FILE) {
        return -EIO;
    }

    /* Step 3: derive total count from primary.num_ext. */
    total = 1 + (int)set[0].dentry.file.num_ext;
    if (total > (int)EXFAT_DENTRY_SET_MAX) {
        return -EIO;   /* primary declares more secondaries than spec allows */
    }
    if (total > max_entries) {
        return -EIO;   /* caller buffer too small */
    }

    /* Step 4: read each secondary; validate bit7-set "in-use" + known type. */
    for (i = 1; i < total; i++) {
        uint8_t t;
        err = exfat_get_dentry(sbi, dir, start_entry + i, &set[i], NULL);
        if (err != 0) {
            return err;
        }
        t = set[i].type;
        /* in-use marker = bit7 set; v1 only accepts known secondary types. */
        if ((t & (uint8_t)0x80u) == 0u) {
            return -EIO;   /* not in-use */
        }
        if (t != (uint8_t)EXFAT_STREAM && t != (uint8_t)EXFAT_NAME) {
            return -EIO;   /* unknown / vendor secondary — v1 rejects */
        }
    }

    *num_entries = total;
    return 0;
}

/* ---------------------------------------------------------------------------
 * exfat_validate_dentry_set
 *
 * Pure compute. exfat_calc_chksum16 with CS_DIR_ENTRY skips bytes 2-3 of
 * set[0] (the SetChecksum field itself); the result is compared against
 * set[0].dentry.file.checksum after LE16 decode.
 * --------------------------------------------------------------------------- */
int exfat_validate_dentry_set(const struct exfat_dentry *set, int num_entries)
{
    uint16_t computed;
    uint16_t expected;

    if (set == NULL) {
        return -EINVAL;
    }
    if (num_entries < 1 || num_entries > (int)EXFAT_DENTRY_SET_MAX) {
        return -EINVAL;
    }
    if (set[0].type != (uint8_t)EXFAT_FILE) {
        return -EINVAL;
    }

    computed = exfat_calc_chksum16(set,
                                    num_entries * (int)DENTRY_SIZE,
                                    0,
                                    CS_DIR_ENTRY);
    expected = LE16_TO_HOST(set[0].dentry.file.checksum);

    if (computed != expected) {
        return -EIO;
    }
    return 0;
}

/* ----- merged from exfat_dentry_set_write.c ----- */

extern UINT8 *m_aucSysMem0;

/* ---------------------------------------------------------------------------
 * exfat_set_dentry
 *
 * Write one 32B dentry into the directory chain at linear index entry_idx.
 * Strictly mirrors exfat_get_dentry's chain-walk + sector-locator path; the
 * payload step replaces "memcpy from buf to caller's out" with
 * "memcpy from caller's in into buf, then los_part_write the sector back".
 *
 * Invariants honoured: no locks (callee), leak-free buf, cluster-boundary
 * correctness (recomputed per-call), no chksum touch, no vol_flags touch.
 * --------------------------------------------------------------------------- */
int exfat_set_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                     int entry_idx, const struct exfat_dentry *in)
{
    uint8_t *buf = NULL;
    uint64_t byte_off;
    uint64_t sector_lba;
    uint32_t clu_offset;
    uint32_t byte_in_clu;
    uint32_t sector_in_clu;
    uint32_t byte_in_sector;
    uint32_t cur_clu;
    uint32_t i;
    int err = 0;

    if (sbi == NULL || dir == NULL || in == NULL || entry_idx < 0) {
        return -EINVAL;
    }
    if (dir->flags != (uint8_t)ALLOC_FAT_CHAIN &&
        dir->flags != (uint8_t)ALLOC_NO_FAT_CHAIN) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u || sbi->blocksize == 0u ||
        dir->dir < EXFAT_FIRST_CLUSTER) {
        return -EINVAL;
    }

    byte_off       = (uint64_t)(uint32_t)entry_idx * (uint64_t)DENTRY_SIZE;
    clu_offset     = (uint32_t)(byte_off / sbi->cluster_size);
    byte_in_clu    = (uint32_t)(byte_off % sbi->cluster_size);
    sector_in_clu  = byte_in_clu / sbi->blocksize;
    byte_in_sector = byte_in_clu % sbi->blocksize;

    /* Walk to the target cluster — same logic as exfat_get_dentry. */
    cur_clu = dir->dir;
    if (dir->flags == (uint8_t)ALLOC_NO_FAT_CHAIN) {
        cur_clu += clu_offset;
        if (cur_clu < EXFAT_FIRST_CLUSTER || cur_clu >= sbi->num_clusters) {
            return -EIO;
        }
    } else {
        for (i = 0; i < clu_offset; i++) {
            uint32_t next_clu = 0;
            err = exfat_get_next_cluster(sbi, cur_clu, &next_clu);
            if (err != 0) {
                return err;
            }
            if (next_clu == EXFAT_EOF_CLUSTER) {
                return -EIO;
            }
            cur_clu = next_clu;
        }
        err = 0;
    }

    sector_lba = exfat_clu_to_sector(sbi, cur_clu) + (uint64_t)sector_in_clu;

    buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->blocksize);
    if (buf == NULL) {
        return -ENOMEM;
    }

    if (los_part_read(sbi->part_id, buf, sector_lba, 1u, TRUE) < 0) {
        err = -EIO;
        goto out;
    }

    if (memcpy_s(buf + byte_in_sector,
                 (size_t)sbi->blocksize - (size_t)byte_in_sector,
                 in, sizeof(*in)) != EOK) {
        err = -EIO;
        goto out;
    }

    if (los_part_write(sbi->part_id, buf, sector_lba, 1u) < 0) {
        err = -EIO;
        goto out;
    }

out:
    LOS_MemFree(m_aucSysMem0, buf);
    return err;
}

/* ---------------------------------------------------------------------------
 * exfat_set_dentry_set
 *
 * Loop over num_entries calling exfat_set_dentry. Per Invariant
 * exfat-set-dentry-set-no-rollback, partial commits are NOT undone — caller
 * must mark vol_flags VOLUME_DIRTY in the failure path.
 * --------------------------------------------------------------------------- */
int exfat_set_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
                         int start_entry, const struct exfat_dentry *set,
                         int num_entries)
{
    int i;
    int err;

    if (sbi == NULL || dir == NULL || set == NULL) {
        return -EINVAL;
    }
    if (start_entry < 0 || num_entries < 1 ||
        num_entries > (int)EXFAT_DENTRY_SET_MAX) {
        return -EINVAL;
    }

    for (i = 0; i < num_entries; i++) {
        err = exfat_set_dentry(sbi, dir, start_entry + i, &set[i]);
        if (err != 0) {
            return err;
        }
    }
    return 0;
}

/* ----- merged from exfat_alloc_dentry_slot.c ----- */

/* ---------------------------------------------------------------------------
 * exfat_alloc_dentry_slot
 *
 * Linear scan for `n_entries` contiguous "writable" slots in dir's chain.
 * "Writable" = either EXFAT_UNUSED (type 0x00, end-of-dir terminator) or
 * type with bit 7 cleared (deleted slot, e.g. 0x05 / 0x40 / 0x41).
 *
 * Hitting an EXFAT_UNUSED terminator extends the run all the way to the
 * end of the dir bound — Microsoft spec: every slot past terminator is also
 * unused. So once we hit a terminator we evaluate "remaining slots from
 * run_start" against n_entries and decide success or -ENOSPC right there
 * (no further IO).
 *
 * v1 does NOT grow the directory chain. -ENOSPC is final.
 * --------------------------------------------------------------------------- */
int exfat_alloc_dentry_slot(const exfat_sb_info *sbi, const exfat_chain *dir,
                            int n_entries, int *slot_idx_out)
{
    int max_dentries;
    int run_start;
    int i;
    struct exfat_dentry probe;
    int err;

    if (sbi == NULL || dir == NULL || slot_idx_out == NULL) {
        return -EINVAL;
    }
    if (n_entries < 1 || n_entries > (int)EXFAT_DENTRY_SET_MAX) {
        return -EINVAL;
    }
    if (dir->flags != (uint8_t)ALLOC_FAT_CHAIN &&
        dir->flags != (uint8_t)ALLOC_NO_FAT_CHAIN) {
        return -EINVAL;
    }
    if (dir->dir < EXFAT_FIRST_CLUSTER) {
        return -EINVAL;
    }
    if (sbi->dentries_per_clu == 0u || dir->size == 0u) {
        return -EINVAL;
    }

    max_dentries = (int)((uint64_t)dir->size * (uint64_t)sbi->dentries_per_clu);
    run_start = -1;

    for (i = 0; i < max_dentries; i++) {
        err = exfat_get_dentry(sbi, dir, i, &probe, NULL);
        if (err != 0) {
            return err;
        }

        /* Terminator: every slot from `i` onward is unused. */
        if (probe.type == EXFAT_UNUSED) {
            if (run_start < 0) {
                run_start = i;
            }
            if ((max_dentries - run_start) >= n_entries) {
                *slot_idx_out = run_start;
                return 0;
            }
            return -ENOSPC;
        }

        /* Deleted slot: bit 7 cleared but not the terminator. Reusable. */
        if ((probe.type & (uint8_t)0x80u) == 0u) {
            if (run_start < 0) {
                run_start = i;
            }
            if ((i - run_start + 1) >= n_entries) {
                *slot_idx_out = run_start;
                return 0;
            }
            continue;
        }

        /* In-use slot: reset run. */
        run_start = -1;
    }

    return -ENOSPC;
}

/* ----- merged from exfat_readdir.c ----- */

extern UINT8 *m_aucSysMem0;

/* Identity LE→host on LiteOS-A ARMv7-A LE host (Invariant
 * exfat-readdir-le-host-only). */

/* ---------------------------------------------------------------------------
 * ExfatReaddirExtractName8 — pull UTF-16 filename from a validated dentry-set
 * and convert to UTF-8 in-place into the caller-supplied buffer.
 *
 * Returns 0 on success with NUL-terminated UTF-8 name in `out`. Returns
 * -EINVAL if set shape is malformed or UTF-8 output would overflow `out_max`.
 * Pure compute (no IO/lock/alloc).
 * --------------------------------------------------------------------------- */
static int ExfatReaddirExtractName8(const struct exfat_dentry *set, int num_entries,
                                    uint16_t *uni, int uni_max,
                                    char *out, int out_max)
{
    int nm_len;
    int needed;
    int written = 0;
    int u8_len;
    int i;
    int j;
    int chunk;

    if (set == NULL || uni == NULL || out == NULL || num_entries < 2 ||
        uni_max <= 0 || out_max <= 1) {
        return -EINVAL;
    }
    if (set[0].type != EXFAT_FILE || set[1].type != EXFAT_STREAM) {
        return -EINVAL;
    }

    nm_len = (int)set[1].dentry.stream.name_len;
    if (nm_len <= 0 || nm_len > uni_max) {
        return -EINVAL;
    }
    needed = (nm_len + EXFAT_FILE_NAME_LEN - 1) / EXFAT_FILE_NAME_LEN;
    if (num_entries < 2 + needed) {
        return -EINVAL;
    }

    for (i = 0; i < needed; i++) {
        const struct exfat_dentry *nd = &set[2 + i];

        if (nd->type != EXFAT_NAME) {
            return -EINVAL;
        }
        chunk = (i == needed - 1)
                    ? (nm_len - i * EXFAT_FILE_NAME_LEN)
                    : EXFAT_FILE_NAME_LEN;
        for (j = 0; j < chunk; j++) {
            uni[written++] = LE16_TO_HOST(nd->dentry.name.unicode_0_14[j]);
        }
    }

    /* exfat_uni_to_utf8 returns bytes written excluding NUL on success, or
     * -ENAMETOOLONG / negative on failure. Reserve 1 byte for trailing NUL. */
    u8_len = exfat_uni_to_utf8(uni, nm_len, out, out_max - 1);
    if (u8_len < 0) {
        return -EINVAL;
    }
    if (u8_len >= out_max) {
        return -EINVAL;
    }
    out[u8_len] = '\0';
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatOpendir — vop->Opendir callback. Initializes per-DIR cursor.
 * No exfat lock acquired (idir is per-fd private; no shared state touched).
 * --------------------------------------------------------------------------- */
int VfsExfatOpendir(struct Vnode *vp, struct fs_dirent_s *idir)
{
    if (vp == NULL || idir == NULL) {
        return -EINVAL;
    }
    if (vp->type != VNODE_TYPE_DIR) {
        return -EINVAL;
    }

    idir->fd_int_offset = 0;
    idir->fd_position = 0;
    idir->u.fs_dir = NULL;
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatReaddir — vop->Readdir callback. Linux-faithful s_lock model
 * (fs/exfat/dir.c::exfat_iterate at lines 229/285).
 *
 * Continues from idir->fd_int_offset (entry_idx), filling at most
 * idir->read_cnt entries into idir->fd_dir[]. Returns number filled
 * (>= 0; 0 = end-of-dir / read_cnt==0). Negative = hard error.
 *
 * Two-step scan (peek primary, then full set fetch on EXFAT_FILE) mirrors
 * lookup. Single-entry IO errors skip-and-advance; only zero-fill+at-least-
 * one-IO-err returns -EIO.
 * --------------------------------------------------------------------------- */
int VfsExfatReaddir(struct Vnode *vp, struct fs_dirent_s *idir)
{
    exfat_sb_info       *sbi = NULL;
    exfat_inode_info    *ei = NULL;
    uint16_t            *uni_buf = NULL;
    struct exfat_dentry *set_buf = NULL;
    exfat_chain          dir;
    int                  max_dentries;
    int                  entry_idx;
    int                  filled = 0;
    int                  had_io_err = 0;
    int                  locked = 0;
    int                  ret = 0;
    int                  err;

    /* Parameter-level checks (no s_lock). */
    if (vp == NULL || idir == NULL) {
        return -EINVAL;
    }
    if (vp->type != VNODE_TYPE_DIR || vp->data == NULL ||
        vp->originMount == NULL || vp->originMount->data == NULL) {
        return -EINVAL;
    }
    if (idir->read_cnt <= 0) {
        return 0; /* No request → no work, no lock. */
    }

    sbi = (exfat_sb_info *)vp->originMount->data;
    ei = (exfat_inode_info *)vp->data;
    if (sbi->cluster_size == 0u || sbi->dentries_per_clu == 0u) {
        return -EIO;
    }

    /* Allocate working buffers. uni_buf for UTF-16 staging, set_buf for one
     * dentry-set. Total ~1.1 KiB. */
    uni_buf = (uint16_t *)LOS_MemAlloc(m_aucSysMem0,
        (uint32_t)(EXFAT_MAX_NAME_LEN * sizeof(uint16_t)));
    if (uni_buf == NULL) {
        ret = ENOMEM;
        goto ERROR_EXIT;
    }
    set_buf = (struct exfat_dentry *)LOS_MemAlloc(m_aucSysMem0,
        (uint32_t)(EXFAT_DENTRY_SET_MAX * sizeof(struct exfat_dentry)));
    if (set_buf == NULL) {
        ret = ENOMEM;
        goto ERROR_FREE_UNI;
    }

    /* Acquire FS-global lock — Linux exfat_iterate discipline. */
    if (LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER) != LOS_OK) {
        ret = EIO;
        goto ERROR_FREE_SET;
    }
    locked = 1;

    /* Build parent chain. */
    dir.dir = ei->start_clu;
    dir.flags = ei->flags;
    if (ei->size > 0u) {
        dir.size = (uint32_t)((ei->size + sbi->cluster_size - 1u) /
                              sbi->cluster_size);
    } else {
        dir.size = 0u; /* root with implicit size — stop-on-unused gates us. */
    }

    /* Compute scan upper bound (Invariant exfat-readdir-bounded). */
    max_dentries = MAX_EXFAT_DENTRIES;
    if (dir.size != 0u) {
        uint64_t bound = (uint64_t)dir.size * (uint64_t)sbi->dentries_per_clu;

        if (bound < (uint64_t)MAX_EXFAT_DENTRIES) {
            max_dentries = (int)bound;
        }
    }

    /* Continue from saved cursor. */
    entry_idx = (int)idir->fd_int_offset;
    if (entry_idx < 0) {
        entry_idx = 0;
    }

    while (entry_idx < max_dentries && filled < idir->read_cnt) {
        struct exfat_dentry primary;
        struct dirent *dirp;
        int num_entries = 0;
        int dst_max;

        err = exfat_get_dentry(sbi, &dir, entry_idx, &primary, NULL);
        if (err == -EIO) {
            had_io_err = 1;
            entry_idx++;
            continue;
        }
        if (err != 0) {
            ret = -err;
            goto ERROR_UNLOCK;
        }

        if (primary.type == EXFAT_UNUSED) {
            break; /* End of directory. */
        }
        if (primary.type != EXFAT_FILE) {
            entry_idx++;
            continue;
        }

        err = exfat_get_dentry_set(sbi, &dir, entry_idx, set_buf,
                                   EXFAT_DENTRY_SET_MAX, &num_entries);
        if (err == -EIO) {
            had_io_err = 1;
            entry_idx++;
            continue;
        }
        if (err != 0 || num_entries <= 0) {
            entry_idx++;
            continue;
        }

        if (exfat_validate_dentry_set(set_buf, num_entries) != 0) {
            had_io_err = 1;
            entry_idx += num_entries;
            continue;
        }

        /* Decode name into the destination dirent slot directly. */
        dirp = &idir->fd_dir[filled];
        dst_max = (int)sizeof(dirp->d_name);
        if (ExfatReaddirExtractName8(set_buf, num_entries, uni_buf,
                                     EXFAT_MAX_NAME_LEN,
                                     dirp->d_name, dst_max) != 0) {
            /* Name decode failure (invalid UTF-16 or > d_name capacity) —
             * skip this entry per Invariant exfat-readdir-utf8-only. */
            entry_idx += num_entries;
            continue;
        }

        {
            uint16_t attr = LE16_TO_HOST(set_buf[0].dentry.file.attr);

            dirp->d_type   = (attr & ATTR_SUBDIR) ? DT_DIR : DT_REG;
            dirp->d_reclen = (uint16_t)sizeof(struct dirent);
            idir->fd_position++;
            dirp->d_off = idir->fd_position;
        }

        entry_idx += num_entries;
        filled++;
    }

    /* Persist cursor for next call. */
    idir->fd_int_offset = entry_idx;

    /* Decide return code. */
    if (filled > 0) {
        ret = 0;
        LOS_MuxUnlock(&sbi->s_lock);
        locked = 0;
        LOS_MemFree(m_aucSysMem0, set_buf);
        LOS_MemFree(m_aucSysMem0, uni_buf);
        return filled;
    }
    if (had_io_err) {
        ret = EIO;
        goto ERROR_UNLOCK;
    }
    /* End-of-directory: filled == 0, no IO error. */
    LOS_MuxUnlock(&sbi->s_lock);
    locked = 0;
    LOS_MemFree(m_aucSysMem0, set_buf);
    LOS_MemFree(m_aucSysMem0, uni_buf);
    return 0;

ERROR_UNLOCK:
    if (locked) {
        LOS_MuxUnlock(&sbi->s_lock);
        locked = 0;
    }
ERROR_FREE_SET:
    LOS_MemFree(m_aucSysMem0, set_buf);
ERROR_FREE_UNI:
    LOS_MemFree(m_aucSysMem0, uni_buf);
ERROR_EXIT:
    return -ret;
}

/* ---------------------------------------------------------------------------
 * VfsExfatClosedir — vop->Closedir callback. No-op (Opendir didn't allocate).
 * --------------------------------------------------------------------------- */
int VfsExfatClosedir(struct Vnode *vp, struct fs_dirent_s *idir)
{
    if (vp == NULL || idir == NULL) {
        return -EINVAL;
    }
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatRewinddir — vop->Rewinddir callback. Reset cursor to 0.
 * --------------------------------------------------------------------------- */
int VfsExfatRewinddir(struct Vnode *vp, struct fs_dirent_s *idir)
{
    if (vp == NULL || idir == NULL) {
        return -EINVAL;
    }
    idir->fd_int_offset = 0;
    idir->fd_position = 0;
    return 0;
}

/* ----- merged from exfat_file.c ----- */

extern UINT8 *m_aucSysMem0;

/* ---------------------------------------------------------------------------
 * ExfatGetClusterAt — walk fat chain idx steps from start_clu.
 *
 * Caller already holds ei->inode_lock (Invariant exfat-read-fat-chain-walk).
 * No additional lock taken by this helper.
 *
 *   ALLOC_NO_FAT_CHAIN  → contiguous run, *out = start_clu + idx (linear).
 *   ALLOC_FAT_CHAIN     → step idx times via exfat_get_next_cluster.
 *
 * Returns 0 / -EINVAL on chain breakage / -EIO from get_next_cluster.
 * Mid-walk EOF / FREE / out-of-range cluster numbers map to -EINVAL because
 * the spec forbids idx falling outside the chain (caller has clamped to_read
 * by ei->size, so the offset must lie inside the allocated chain).
 * --------------------------------------------------------------------------- */
static int ExfatGetClusterAt(const exfat_sb_info *sbi, uint32_t start_clu,
                             uint8_t flags, uint32_t idx, uint32_t *out_clu)
{
    uint32_t cur = start_clu;
    uint32_t next;
    uint32_t i;
    int ret;

    if (start_clu < EXFAT_FIRST_CLUSTER ||
        start_clu >= sbi->num_clusters + EXFAT_FIRST_CLUSTER) {
        return -EINVAL;
    }

    if (flags == ALLOC_NO_FAT_CHAIN) {
        cur = start_clu + idx;
        if (cur < EXFAT_FIRST_CLUSTER ||
            cur >= sbi->num_clusters + EXFAT_FIRST_CLUSTER) {
            return -EINVAL;
        }
        *out_clu = cur;
        return 0;
    }

    /* ALLOC_FAT_CHAIN — step idx times. */
    for (i = 0; i < idx; i++) {
        ret = exfat_get_next_cluster(sbi, cur, &next);
        if (ret != 0) {
            return ret;
        }
        if (next == EXFAT_EOF_CLUSTER || next == EXFAT_FREE_CLUSTER ||
            next < EXFAT_FIRST_CLUSTER ||
            next >= sbi->num_clusters + EXFAT_FIRST_CLUSTER) {
            return -EINVAL;
        }
        cur = next;
    }

    *out_clu = cur;
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatRead — file_operations_vfs.read callback.
 *
 * Linux-faithful lock model (Invariant exfat-read-linux-i-rwsem-faithful):
 * holds ei->inode_lock for the entire body, single-point release. Mirrors
 * Linux upper-layer VFS auto-acquired inode->i_rwsem (shared) during vfs_read.
 * Does NOT take sbi->s_lock (Linux exfat_get_block on read path doesn't).
 * Does NOT take sbi->bitmap_lock (no allocation).
 *
 * Short-read semantics (Invariant exfat-read-pos-advance-exact):
 *   On IO failure mid-loop, if any bytes already copied → return copied (>0)
 *   and advance f_pos accordingly; otherwise return negative errno and leave
 *   f_pos untouched.
 * --------------------------------------------------------------------------- */
ssize_t VfsExfatRead(struct file *filep, char *buf, size_t len)
{
    struct Vnode *vp;
    struct Mount *mount;
    exfat_sb_info *sbi;
    exfat_inode_info *ei;
    uint8_t *cluster_buf = NULL;
    uint64_t cur_off;
    uint64_t to_read;
    size_t   copied = 0;
    int      ret;
    int      err = 0;

    if (len == 0) {
        return 0;
    }
    if (filep == NULL || buf == NULL) {
        return -EINVAL;
    }

    vp = filep->f_vnode;
    if (vp == NULL || vp->originMount == NULL || vp->data == NULL) {
        return -EINVAL;
    }
    mount = vp->originMount;
    sbi = (exfat_sb_info *)mount->data;
    ei  = (exfat_inode_info *)vp->data;
    if (sbi == NULL) {
        return -EINVAL;
    }

    /* Invariant exfat-read-isdir-rejected. */
    if (ei->type != TYPE_FILE) {
        return -EISDIR;
    }

    (VOID)LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER);

    /* Invariant exfat-read-eof-returns-zero. */
    if ((uint64_t)filep->f_pos >= ei->size) {
        (VOID)LOS_MuxUnlock(&ei->inode_lock);
        return 0;
    }

    /* Invariant exfat-read-clamp-by-size. */
    to_read = ei->size - (uint64_t)filep->f_pos;
    if (to_read > (uint64_t)len) {
        to_read = (uint64_t)len;
    }

    cluster_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->cluster_size);
    if (cluster_buf == NULL) {
        PRINT_ERR("[%s] alloc cluster_buf failed (cluster_size=%u)\n",
                  __func__, sbi->cluster_size);
        err = -ENOMEM;
        goto UNLOCK;
    }

    cur_off = (uint64_t)filep->f_pos;
    while ((uint64_t)copied < to_read) {
        uint32_t clu_idx     = (uint32_t)(cur_off >> sbi->cluster_size_bits);
        uint32_t byte_in_clu = (uint32_t)(cur_off & (sbi->cluster_size - 1u));
        uint32_t cur_clu;
        uint64_t sect;
        uint32_t nr_sect;
        size_t   chunk;
        size_t   remain;

        ret = ExfatGetClusterAt(sbi, ei->start_clu, ei->flags, clu_idx, &cur_clu);
        if (ret != 0) {
            PRINT_ERR("[%s] cluster walk failed at idx=%u: %d\n",
                      __func__, clu_idx, ret);
            err = ret;
            goto IO_ERR;
        }

        sect    = exfat_clu_to_sector(sbi, cur_clu);
        nr_sect = sbi->cluster_size >> sbi->blocksize_bits;

        ret = los_part_read(sbi->part_id, cluster_buf, sect, nr_sect, TRUE);
        if (ret != 0) {
            PRINT_ERR("[%s] los_part_read failed clu=%u sect=%llu: %d\n",
                      __func__, cur_clu, sect, ret);
            err = -EIO;
            goto IO_ERR;
        }

        remain = (size_t)(to_read - (uint64_t)copied);
        chunk  = (size_t)(sbi->cluster_size - byte_in_clu);
        if (chunk > remain) {
            chunk = remain;
        }

        ret = memcpy_s(buf + copied, remain,
                       cluster_buf + byte_in_clu, chunk);
        if (ret != EOK) {
            PRINT_ERR("[%s] memcpy_s failed: %d\n", __func__, ret);
            err = -EIO;
            goto IO_ERR;
        }

        copied  += chunk;
        cur_off += chunk;
    }

    (void)LOS_MemFree(m_aucSysMem0, cluster_buf);
    filep->f_pos += (loff_t)copied;
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)copied;

IO_ERR:
    (void)LOS_MemFree(m_aucSysMem0, cluster_buf);
    /* Short-read semantics: report bytes already copied, ignore err. */
    if (copied > 0) {
        filep->f_pos += (loff_t)copied;
        (VOID)LOS_MuxUnlock(&ei->inode_lock);
        return (ssize_t)copied;
    }
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)err;

UNLOCK:
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)err;
}

/* ----- merged from exfat_write.c ----- */

extern UINT8 *m_aucSysMem0;

/* ---------------------------------------------------------------------------
 * ExfatWriteGetClusterAt — walk fat chain idx steps from start_clu.
 *
 * Same semantics as exfat_file.c::ExfatGetClusterAt (Invariant
 * exfat-write-helper-duplication-temp). Wave B Stage 2 (truncate) will
 * promote the duplicated logic to fs/exfat/util/exfat_fat_chain.c as
 * exfat_pos_to_cluster, then delete both static copies.
 *
 * Caller already holds ei->inode_lock; this helper takes no additional lock.
 * --------------------------------------------------------------------------- */
static int ExfatWriteGetClusterAt(const exfat_sb_info *sbi, uint32_t start_clu,
                                  uint8_t flags, uint32_t idx, uint32_t *out_clu)
{
    uint32_t cur = start_clu;
    uint32_t next;
    uint32_t i;
    int ret;

    if (start_clu < EXFAT_FIRST_CLUSTER ||
        start_clu >= sbi->num_clusters + EXFAT_FIRST_CLUSTER) {
        return -EINVAL;
    }

    if (flags == ALLOC_NO_FAT_CHAIN) {
        cur = start_clu + idx;
        if (cur < EXFAT_FIRST_CLUSTER ||
            cur >= sbi->num_clusters + EXFAT_FIRST_CLUSTER) {
            return -EINVAL;
        }
        *out_clu = cur;
        return 0;
    }

    /* ALLOC_FAT_CHAIN — step idx times. */
    for (i = 0; i < idx; i++) {
        ret = exfat_get_next_cluster(sbi, cur, &next);
        if (ret != 0) {
            return ret;
        }
        if (next == EXFAT_EOF_CLUSTER || next == EXFAT_FREE_CLUSTER ||
            next < EXFAT_FIRST_CLUSTER ||
            next >= sbi->num_clusters + EXFAT_FIRST_CLUSTER) {
            return -EINVAL;
        }
        cur = next;
    }

    *out_clu = cur;
    return 0;
}

/* ---------------------------------------------------------------------------
 * VfsExfatWrite — file_operations_vfs.write callback (Wave B Stage 1).
 *
 * In-place overwrite only; clamped to [filep->f_pos, ei->size). No allocation,
 * no extending, no dentry persistence (deferred to Wave B2 / B6).
 *
 * Cluster-level read-modify-write: read whole cluster, memcpy_s the user data
 * into the affected byte range, write whole cluster back. Sector-level RMW
 * is a future Wave B optimization.
 *
 * Linux-faithful lock model: holds ei->inode_lock for the entire body
 * (mirroring upper-layer vfs_write's exclusive inode->i_rwsem). Does NOT
 * take sbi->s_lock (Linux exfat_get_block(create=0) doesn't either) nor
 * bitmap_lock (no allocation in B1).
 *
 * Short-write semantics: if any byte already written when an IO error hits,
 * return the short count (mirror Linux generic_file_write_iter). Only
 * zero-byte path returns negative errno.
 * --------------------------------------------------------------------------- */
ssize_t VfsExfatWrite(struct file *filep, const char *buf, size_t len)
{
    struct Vnode *vp;
    struct Mount *mount;
    exfat_sb_info *sbi;
    exfat_inode_info *ei;
    uint8_t *cluster_buf = NULL;
    uint64_t cur_off;
    uint64_t to_write;
    size_t   written = 0;
    int      ret;
    int      err = 0;

    if (len == 0) {
        return 0;                                       /* exfat-write-zero-len-fast-path */
    }
    if (filep == NULL || buf == NULL) {
        return -EINVAL;
    }

    vp = filep->f_vnode;
    if (vp == NULL || vp->originMount == NULL || vp->data == NULL) {
        return -EINVAL;
    }
    mount = vp->originMount;
    sbi = (exfat_sb_info *)mount->data;
    ei  = (exfat_inode_info *)vp->data;
    if (sbi == NULL) {
        return -EINVAL;
    }

    if (ei->type != TYPE_FILE) {
        return -EISDIR;                                 /* exfat-write-isdir-rejected */
    }

    (VOID)LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER);

    /* Invariant exfat-write-no-extend: do not extend past ei->size in B1. */
    if ((uint64_t)filep->f_pos >= ei->size) {
        (VOID)LOS_MuxUnlock(&ei->inode_lock);
        return 0;
    }

    /* Invariant exfat-write-clamp-by-size. */
    to_write = ei->size - (uint64_t)filep->f_pos;
    if (to_write > (uint64_t)len) {
        to_write = (uint64_t)len;
    }

    cluster_buf = (uint8_t *)LOS_MemAlloc(m_aucSysMem0, sbi->cluster_size);
    if (cluster_buf == NULL) {
        PRINT_ERR("[%s] alloc cluster_buf failed (cluster_size=%u)\n",
                  __func__, sbi->cluster_size);
        err = -ENOMEM;
        goto UNLOCK;
    }

    cur_off = (uint64_t)filep->f_pos;
    while ((uint64_t)written < to_write) {
        uint32_t clu_idx     = (uint32_t)(cur_off >> sbi->cluster_size_bits);
        uint32_t byte_in_clu = (uint32_t)(cur_off & (sbi->cluster_size - 1u));
        uint32_t cur_clu;
        uint64_t sect;
        uint32_t nr_sect;
        size_t   chunk;
        size_t   remain;

        ret = ExfatWriteGetClusterAt(sbi, ei->start_clu, ei->flags, clu_idx, &cur_clu);
        if (ret != 0) {
            PRINT_ERR("[%s] cluster walk failed at idx=%u: %d\n",
                      __func__, clu_idx, ret);
            err = ret;
            goto IO_ERR;
        }

        sect    = exfat_clu_to_sector(sbi, cur_clu);
        nr_sect = sbi->cluster_size >> sbi->blocksize_bits;

        /* RMW step 1: read existing cluster. Invariant
         * exfat-write-rmw-read-failure-aborts. */
        ret = los_part_read(sbi->part_id, cluster_buf, sect, nr_sect, TRUE);
        if (ret != 0) {
            PRINT_ERR("[%s] los_part_read failed clu=%u sect=%llu: %d\n",
                      __func__, cur_clu, sect, ret);
            err = -EIO;
            goto IO_ERR;
        }

        remain = (size_t)(to_write - (uint64_t)written);
        chunk  = (size_t)(sbi->cluster_size - byte_in_clu);
        if (chunk > remain) {
            chunk = remain;
        }

        /* RMW step 2: patch bytes in cluster_buf. */
        ret = memcpy_s(cluster_buf + byte_in_clu,
                       sbi->cluster_size - byte_in_clu,
                       buf + written, chunk);
        if (ret != EOK) {
            PRINT_ERR("[%s] memcpy_s failed: %d\n", __func__, ret);
            err = -EIO;
            goto IO_ERR;
        }

        /* RMW step 3: write whole cluster back. */
        ret = los_part_write(sbi->part_id, cluster_buf, sect, nr_sect);
        if (ret != 0) {
            PRINT_ERR("[%s] los_part_write failed clu=%u sect=%llu: %d\n",
                      __func__, cur_clu, sect, ret);
            err = -EIO;
            goto IO_ERR;
        }

        written += chunk;
        cur_off += chunk;
    }

    (void)LOS_MemFree(m_aucSysMem0, cluster_buf);
    filep->f_pos += (loff_t)written;
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)written;

IO_ERR:
    (void)LOS_MemFree(m_aucSysMem0, cluster_buf);
    /* Invariant exfat-write-short-write-on-mid-failure. */
    if (written > 0) {
        filep->f_pos += (loff_t)written;
        (VOID)LOS_MuxUnlock(&ei->inode_lock);
        return (ssize_t)written;
    }
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)err;

UNLOCK:
    (VOID)LOS_MuxUnlock(&ei->inode_lock);
    return (ssize_t)err;
}

/* ----- merged from exfat_truncate_extend.c ----- */

static inline uint64_t bytes_to_cluster_count(uint64_t bytes, uint32_t cluster_size)
{
    if (cluster_size == 0u) {
        return 0;
    }
    return (bytes + cluster_size - 1u) / cluster_size;
}

int exfat_truncate_extend(exfat_sb_info *sbi, exfat_inode_info *ei,
                          uint64_t new_size)
{
    uint64_t num_new_clu_64;
    uint64_t num_phys_clu_64;
    uint32_t num_new_clu;
    uint32_t num_phys_clu;
    uint32_t num_to_alloc;
    uint32_t last_existing_clu;
    int      ret;
    int      vd_ret;

    if (sbi == NULL || ei == NULL) {
        return -EINVAL;
    }
    if (new_size <= ei->size) {
        return -EINVAL;
    }
    if (ei->flags != ALLOC_FAT_CHAIN) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u) {
        return -EINVAL;
    }
    if (new_size > sbi->s_maxbytes) {
        return -EFBIG;
    }

    num_new_clu_64  = bytes_to_cluster_count(new_size, sbi->cluster_size);
    num_phys_clu_64 = bytes_to_cluster_count(ei->i_size_ondisk, sbi->cluster_size);
    if (num_new_clu_64 > 0xFFFFFFFFull) {
        return -EFBIG;
    }
    num_new_clu  = (uint32_t)num_new_clu_64;
    num_phys_clu = (uint32_t)num_phys_clu_64;

    ret = exfat_set_volume_dirty(sbi);
    if (ret != 0) {
        PRINT_ERR("[%s] set_volume_dirty failed: %d\n", __func__, ret);
        return ret;
    }

    if (num_new_clu <= num_phys_clu) {
        ei->size = new_size;
        goto clear_dirty_out;
    }

    num_to_alloc = num_new_clu - num_phys_clu;

    last_existing_clu = EXFAT_EOF_CLUSTER;
    if (num_phys_clu > 0u && ei->start_clu != EXFAT_EOF_CLUSTER &&
        ei->start_clu != EXFAT_FREE_CLUSTER) {
        uint32_t cur   = ei->start_clu;
        uint32_t guard = 0u;
        uint32_t next  = 0u;
        while (guard + 1u < num_phys_clu && guard < EXFAT_MAX_CHAIN_LEN) {
            ret = exfat_get_next_cluster(sbi, cur, &next);
            if (ret != 0) {
                PRINT_ERR("[%s] walk to tail failed at clu=%u: %d\n",
                          __func__, cur, ret);
                goto clear_dirty_eio;
            }
            if (next == EXFAT_EOF_CLUSTER) {
                break;
            }
            cur = next;
            guard++;
        }
        last_existing_clu = cur;
    }

    {
        exfat_chain new_chain;
        new_chain.dir   = EXFAT_EOF_CLUSTER;
        new_chain.size  = 0u;
        new_chain.flags = ALLOC_FAT_CHAIN;

        ret = exfat_alloc_cluster(sbi, num_to_alloc, &new_chain);
        if (ret != 0) {
            vd_ret = exfat_clear_volume_dirty(sbi);
            if (vd_ret != 0) {
                PRINT_ERR("[%s] clear_volume_dirty after alloc fail: %d\n",
                          __func__, vd_ret);
            }
            return ret;
        }

        if (last_existing_clu == EXFAT_EOF_CLUSTER) {
            ei->start_clu = new_chain.dir;
        } else {
            ret = exfat_ent_set(sbi, last_existing_clu, new_chain.dir);
            if (ret != 0) {
                PRINT_ERR("[%s] link old tail %u -> new head %u failed: %d\n",
                          __func__, last_existing_clu, new_chain.dir, ret);
                (void)exfat_free_cluster(sbi, &new_chain);
                vd_ret = exfat_clear_volume_dirty(sbi);
                if (vd_ret != 0) {
                    PRINT_ERR("[%s] clear_volume_dirty after link fail: %d\n",
                              __func__, vd_ret);
                }
                return -EIO;
            }
        }

        ei->size          = new_size;
        ei->i_size_ondisk = (uint64_t)num_new_clu * (uint64_t)sbi->cluster_size;
    }

clear_dirty_out:
    vd_ret = exfat_clear_volume_dirty(sbi);
    if (vd_ret != 0) {
        PRINT_ERR("[%s] clear_volume_dirty (success path): %d\n",
                  __func__, vd_ret);
    }
    return 0;

clear_dirty_eio:
    vd_ret = exfat_clear_volume_dirty(sbi);
    if (vd_ret != 0) {
        PRINT_ERR("[%s] clear_volume_dirty (eio path): %d\n",
                  __func__, vd_ret);
    }
    return -EIO;
}

/* ----- merged from exfat_truncate_shrink.c ----- */

/* bytes_to_cluster_count: defined earlier (in the truncate-extend section above). */

static int walk_to_new_tail(const exfat_sb_info *sbi, uint32_t start_clu,
                            uint32_t hops, uint32_t *new_tail_out)
{
    uint32_t cur   = start_clu;
    uint32_t next  = 0u;
    uint32_t guard = 0u;
    int      ret;

    while (guard < hops) {
        if (guard >= EXFAT_MAX_CHAIN_LEN) {
            return -EIO;
        }
        ret = exfat_get_next_cluster(sbi, cur, &next);
        if (ret != 0) {
            return ret;
        }
        if (next == EXFAT_EOF_CLUSTER || next == EXFAT_FREE_CLUSTER) {
            return -EIO;
        }
        cur = next;
        guard++;
    }
    *new_tail_out = cur;
    return 0;
}

int exfat_truncate_shrink(exfat_sb_info *sbi, exfat_inode_info *ei,
                          uint64_t new_size)
{
    uint64_t num_new_clu_64;
    uint64_t num_phys_clu_64;
    uint32_t num_new_clu;
    uint32_t num_phys_clu;
    int      ret;
    int      vd_ret;

    if (sbi == NULL || ei == NULL) {
        return -EINVAL;
    }
    if (new_size >= ei->size) {
        return -EINVAL;
    }
    if (ei->flags != ALLOC_FAT_CHAIN) {
        return -EINVAL;
    }
    if (sbi->cluster_size == 0u) {
        return -EINVAL;
    }

    num_new_clu_64  = bytes_to_cluster_count(new_size, sbi->cluster_size);
    num_phys_clu_64 = bytes_to_cluster_count(ei->i_size_ondisk, sbi->cluster_size);
    num_new_clu  = (uint32_t)num_new_clu_64;
    num_phys_clu = (uint32_t)num_phys_clu_64;

    ret = exfat_set_volume_dirty(sbi);
    if (ret != 0) {
        PRINT_ERR("[%s] set_volume_dirty failed: %d\n", __func__, ret);
        return ret;
    }

    if (num_new_clu >= num_phys_clu) {
        ei->size = new_size;
        if (ei->valid_size > new_size) {
            ei->valid_size = new_size;
        }
        goto clear_dirty_out;
    }

    if (num_new_clu == 0u) {
        exfat_chain whole_chain;
        whole_chain.dir   = ei->start_clu;
        whole_chain.size  = num_phys_clu;
        whole_chain.flags = ALLOC_FAT_CHAIN;

        ei->size          = 0u;
        ei->i_size_ondisk = 0u;
        ei->valid_size    = 0u;
        ei->start_clu     = EXFAT_EOF_CLUSTER;

        ret = exfat_free_cluster(sbi, &whole_chain);
        if (ret != 0) {
            PRINT_ERR("[%s] free entire chain (start=%u, n=%u) failed: %d\n",
                      __func__, whole_chain.dir, num_phys_clu, ret);
            vd_ret = exfat_clear_volume_dirty(sbi);
            if (vd_ret != 0) {
                PRINT_ERR("[%s] clear_volume_dirty after free fail: %d\n",
                          __func__, vd_ret);
            }
            return -EIO;
        }
        goto clear_dirty_out;
    }

    {
        uint32_t new_tail_clu      = EXFAT_EOF_CLUSTER;
        uint32_t first_discard_clu = EXFAT_EOF_CLUSTER;

        ret = walk_to_new_tail(sbi, ei->start_clu, num_new_clu - 1u, &new_tail_clu);
        if (ret != 0) {
            PRINT_ERR("[%s] walk to new tail failed: %d\n", __func__, ret);
            goto clear_dirty_eio;
        }

        ret = exfat_get_next_cluster(sbi, new_tail_clu, &first_discard_clu);
        if (ret != 0) {
            PRINT_ERR("[%s] get first_discard_clu failed at %u: %d\n",
                      __func__, new_tail_clu, ret);
            goto clear_dirty_eio;
        }
        if (first_discard_clu == EXFAT_EOF_CLUSTER ||
            first_discard_clu == EXFAT_FREE_CLUSTER) {
            PRINT_ERR("[%s] no discard tail at %u (next=%u)\n",
                      __func__, new_tail_clu, first_discard_clu);
            goto clear_dirty_eio;
        }

        ret = exfat_ent_set(sbi, new_tail_clu, EXFAT_EOF_CLUSTER);
        if (ret != 0) {
            PRINT_ERR("[%s] ent_set(EOF) at %u failed: %d\n",
                      __func__, new_tail_clu, ret);
            goto clear_dirty_eio;
        }

        ei->size          = new_size;
        ei->i_size_ondisk = (uint64_t)num_new_clu * (uint64_t)sbi->cluster_size;
        if (ei->valid_size > new_size) {
            ei->valid_size = new_size;
        }

        {
            exfat_chain discard_chain;
            discard_chain.dir   = first_discard_clu;
            discard_chain.size  = num_phys_clu - num_new_clu;
            discard_chain.flags = ALLOC_FAT_CHAIN;
            ret = exfat_free_cluster(sbi, &discard_chain);
            if (ret != 0) {
                PRINT_ERR("[%s] free discard chain (start=%u, n=%u) failed: %d\n",
                          __func__, first_discard_clu,
                          num_phys_clu - num_new_clu, ret);
                vd_ret = exfat_clear_volume_dirty(sbi);
                if (vd_ret != 0) {
                    PRINT_ERR("[%s] clear_volume_dirty after free fail: %d\n",
                              __func__, vd_ret);
                }
                return -EIO;
            }
        }
    }

clear_dirty_out:
    vd_ret = exfat_clear_volume_dirty(sbi);
    if (vd_ret != 0) {
        PRINT_ERR("[%s] clear_volume_dirty (success path): %d\n",
                  __func__, vd_ret);
    }
    return 0;

clear_dirty_eio:
    vd_ret = exfat_clear_volume_dirty(sbi);
    if (vd_ret != 0) {
        PRINT_ERR("[%s] clear_volume_dirty (eio path): %d\n",
                  __func__, vd_ret);
    }
    return -EIO;
}

/* ----- merged from exfat_truncate_vop.c ----- */

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
