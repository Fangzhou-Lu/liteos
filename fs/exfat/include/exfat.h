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

#ifndef _EXFAT_H
#define _EXFAT_H

#ifdef LOSCFG_FS_EXFAT

#include <stdint.h>
#include <stddef.h>
#include "los_typedef.h"
#include "los_mux.h"
#include "vnode.h"
#include "fs/mount.h"
#include "fs/file.h"
#include "exfat_raw.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- constants -------------------------------------------------------- */
#define EXFAT_SUPER_MAGIC          0x2011BAB0u
#define EXFAT_ROOT_INO             1u
#define EXFAT_MAX_NAME_LEN         255
#define EXFAT_DEFAULT_BLOCKSIZE    512u
#define EXFAT_HASH_BITS            8
#define EXFAT_HASH_SIZE            (1u << EXFAT_HASH_BITS)
#define EXFAT_CLUSTERS_UNTRACKED   (~0u)

/* type values mirrored from Linux exfat_fs.h (a subset; v1 mount only needs these) */
#define TYPE_UNUSED                0x0000
#define TYPE_DIR                   0x0104
#define TYPE_FILE                  0x011F
#define TYPE_BITMAP                0x0101
#define TYPE_UPCASE                0x0102
#define TYPE_VOLUME                0x0103
#define TYPE_STREAM                0x0201
#define TYPE_EXTEND                0x0202

/* ---- error policy ----------------------------------------------------- */
enum exfat_error_mode {
    EXFAT_ERRORS_CONT  = 0,
    EXFAT_ERRORS_PANIC = 1,
    EXFAT_ERRORS_RO    = 2,
};

/* ---- mount options ---------------------------------------------------- */
typedef struct exfat_mount_options {
    uint32_t fs_uid;
    uint32_t fs_gid;
    uint16_t fs_fmask;
    uint16_t fs_dmask;
    uint16_t allow_utime;
    uint8_t  utf8;
    uint8_t  discard;
    uint8_t  errors;
    int32_t  time_offset;
    char    *iocharset;
} exfat_mount_options;

/* ---- chain ------------------------------------------------------------ */
typedef struct exfat_chain {
    uint32_t dir;
    uint32_t size;
    uint8_t  flags;
} exfat_chain;

/* ---- super block in-memory ------------------------------------------- */
typedef struct exfat_sb_info {
    uint8_t  sect_size_bits;
    uint8_t  sect_per_clus_bits;
    uint8_t  num_fats;
    uint64_t partition_offset;
    uint64_t vol_length;
    uint32_t fat_offset;
    uint32_t fat_length;
    uint32_t fat2_offset;
    uint32_t clu_offset;
    uint32_t num_clusters;
    uint32_t root_dir;
    uint32_t cluster_size;
    uint32_t cluster_size_bits;
    uint32_t blocksize;
    uint32_t blocksize_bits;
    uint32_t dentries_per_clu;
    uint16_t vol_flags;
    uint16_t vol_flags_persistent;
    uint64_t s_maxbytes;
    uint32_t map_clu;
    uint32_t map_sectors;
    uint8_t *vol_amap;
    uint16_t *vol_utbl;
    uint64_t  vol_utbl_size;
    uint32_t  vol_utbl_clu;
    uint32_t clu_srch_ptr;
    uint32_t used_clusters;
    LosMux   s_lock;
    LosMux   bitmap_lock;
    LosMux   inode_hash_lock;
    uint8_t *boot_buf;
    INT32    part_id;
    exfat_mount_options options;
} exfat_sb_info;

/* ---- inode in-memory -------------------------------------------------- */
typedef struct exfat_inode_info {
    exfat_chain dir;
    int32_t  entry;
    uint32_t type;
    uint16_t attr;
    uint32_t start_clu;
    uint8_t  flags;
    uint64_t size;
    uint64_t valid_size;
    uint64_t i_size_ondisk;
    uint64_t i_pos;
    uint32_t version;
    uint32_t num_subdirs;
    LosMux   inode_lock;
} exfat_inode_info;

/* ---- public exports (defined in fs/exfat translation units; externed for mount glue) */
extern struct VnodeOps             g_exfatVops;
extern struct file_operations_vfs  g_exfatFops;
extern struct MountOps             g_exfatMountOps;

/* ---- helper externs --------------------------------------------------- */
/* options parser — fs/exfat/util/exfat_options.c (future Loop B output) */
int  exfat_parse_options(const char *data, exfat_mount_options *opts);

/* boot sector parser — fs/exfat/exfat_dentry.c (future Loop B output) */
int  exfat_parse_boot_sector(exfat_sb_info *sbi,
                             const struct exfat_boot_sector *bs,
                             uint32_t logical_sector_size);

/* exFAT-specific CRC32 (polynomial 0x04C11DB7 reversed; NOT IEEE) */
uint32_t exfat_calc_chksum32(const void *data, uint32_t len,
                             uint32_t chksum, int type);
/* CRC-16 ('SetChecksum'); CS_DIR_ENTRY skips bytes 2-3 (the chksum field). */
uint16_t exfat_calc_chksum16(const void *data, int len,
                             uint16_t chksum, int type);

/* upcase table — fs/exfat/util/exfat_upcase.c */
int  exfat_create_upcase_table(exfat_sb_info *sbi);
void exfat_free_upcase_table(exfat_sb_info *sbi);

/* allocation bitmap — fs/exfat/exfat_balloc.c */
int  exfat_load_bitmap(exfat_sb_info *sbi);
void exfat_free_bitmap(exfat_sb_info *sbi);
/* const sbi: count is mem-only (Invariant exfat-balloc-count-mem-only). */
int  exfat_count_used_clusters(const exfat_sb_info *sbi, uint32_t *ret_count);

/* find dentry of given type in root chain — fs/exfat/exfat_dentry.c
 * const sbi: helper does not mutate any sbi field (Invariant exfat-dentry-find-readonly). */
int  exfat_find_root_dentry(const exfat_sb_info *sbi, uint8_t type,
                            struct exfat_dentry *out);

/* ---- FAT chain traversal — fs/exfat/util/exfat_fat_chain.c -------------
 * Public helpers replacing dentry stage's private ReadFatEntry. Read-only,
 * lock-free. Spinlock-safe ranking:
 *   exfat_clu_to_sector       — pure compute, spinlock-safe.
 *   exfat_get_next_cluster    — alloc + IO, NOT spinlock-safe.
 *   exfat_chain_walk          — calls get_next_cluster, NOT spinlock-safe.
 * See spec/exfat/util/exfat_fat_chain.spec ## Refine Prompt for full lock contract.
 */

/* visitor callback contract (used by exfat_chain_walk):
 *   return 0  → continue to next cluster
 *   return 1  → stop walk successfully
 *   return <0 → stop walk, propagate error code
 */
typedef int (*exfat_chain_visitor_t)(uint32_t clu, void *ctx);

/* Cluster → partition-relative data sector LBA.
 *
 * Pre: sbi != NULL && clu >= EXFAT_FIRST_CLUSTER && sbi->clu_offset and
 *      sbi->sect_per_clus_bits already populated by exfat_parse_boot_sector.
 *
 * Pure compute; safe to call under any lock (incl. spinlock).
 * Invariant exfat-fat-chain-clu-to-sector-overflow-safe: all arithmetic in
 * uint64_t to avoid 32-bit wrap into boot/FAT region.
 */
static inline uint64_t exfat_clu_to_sector(const exfat_sb_info *sbi,
                                           uint32_t clu)
{
    return (uint64_t)sbi->clu_offset +
           (uint64_t)(clu - EXFAT_FIRST_CLUSTER) *
           (uint64_t)(1u << sbi->sect_per_clus_bits);
}

/* Read FAT[cur_clu]; on success *next_clu ∈ {EXFAT_EOF_CLUSTER} ∪
 * [EXFAT_FIRST_CLUSTER, sbi->num_clusters). Linux __exfat_ent_get parity:
 * raw > EXFAT_BAD_CLUSTER mapped to EXFAT_EOF_CLUSTER prior to validation.
 * Returns 0 / -EIO / -ENOMEM / -EINVAL. fat_buf released on every path.
 */
int  exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                            uint32_t *next_clu);

/* Bounded (≤ sbi->num_clusters) FAT chain walk; visitor invoked per cluster
 * starting from start_clu. Empty chain (start_clu == EXFAT_EOF_CLUSTER) is
 * a successful no-op. Bound exceeded → -EIO (FAT cycle defense).
 */
int  exfat_chain_walk(const exfat_sb_info *sbi, uint32_t start_clu,
                      exfat_chain_visitor_t visitor, void *ctx);

/* Write FAT[loc] = value (with FAT2 mirror when num_fats == 2). Synchronous
 * IO; takes no locks (caller holds ei->inode_lock, sbi->bitmap_lock, or
 * sbi->s_lock per the lock matrix in spec/exfat/util/exfat_ent_set.spec).
 * value ∈ {EOF, FREE} ∪ [FIRST, num_clusters); BAD rejected with -EINVAL.
 * Returns 0 / -EINVAL / -EIO / -ENOMEM. fat_buf released on every path.
 */
int  exfat_ent_set(const exfat_sb_info *sbi, uint32_t loc, uint32_t value);

/* ---- Allocation bitmap mutation — fs/exfat/util/{exfat_free_cluster,
 *                                  exfat_alloc_cluster}.c ---------------
 * Caller holds sbi->bitmap_lock for both helpers. set/clear modify in-memory
 * sbi->vol_amap and synchronously write the affected sector via
 * los_part_write. find_free_bitmap is pure compute (no IO).
 */
int  exfat_set_bitmap(exfat_sb_info *sbi, uint32_t clu);
int  exfat_clear_bitmap(exfat_sb_info *sbi, uint32_t clu);
int  exfat_find_free_bitmap(const exfat_sb_info *sbi, uint32_t hint_clu,
                            uint32_t *out_clu);
int  exfat_free_cluster(exfat_sb_info *sbi, const exfat_chain *p_chain);
int  exfat_alloc_cluster(exfat_sb_info *sbi, uint32_t num_alloc,
                         exfat_chain *p_chain);

/* ---- vol_flags helpers — fs/exfat/util/exfat_vol_flags.c ---------------
 * Caller holds sbi->s_lock. set/clear toggle the VOLUME_DIRTY bit in
 * sbi->vol_flags + write back the main boot sector via los_part_write.
 */
int  exfat_set_volume_dirty(exfat_sb_info *sbi);
int  exfat_clear_volume_dirty(exfat_sb_info *sbi);

/* ---- truncate_extend — fs/exfat/exfat_truncate_extend.c -----------------
 * Extend a file's logical size to new_size, allocating clusters as needed
 * and linking them onto ei's existing chain. Caller holds ei->inode_lock.
 * v1 requires ei->flags == ALLOC_FAT_CHAIN.
 */
int  exfat_truncate_extend(exfat_sb_info *sbi, exfat_inode_info *ei,
                           uint64_t new_size);

/* ---- truncate_shrink — fs/exfat/exfat_truncate_shrink.c -----------------
 * Shrink a file's logical size to new_size, breaking the FAT chain at the
 * new tail and releasing the discarded clusters. Caller holds ei->inode_lock.
 * v1 requires ei->flags == ALLOC_FAT_CHAIN. Pairs with exfat_truncate_extend
 * to form the truncate VOP.
 */
int  exfat_truncate_shrink(exfat_sb_info *sbi, exfat_inode_info *ei,
                           uint64_t new_size);

/* ---- truncate VOP — fs/exfat/exfat_truncate_vop.c -----------------------
 * VFS-edge dispatcher. Acquires ei->inode_lock, compares len vs ei->size,
 * calls exfat_truncate_extend / exfat_truncate_shrink as needed, releases
 * the lock and forwards the helper's negative POSIX errno verbatim. Does
 * NOT update on-disk dentry — v1 known limitation; sync helper deferred.
 */
struct Vnode;
int VfsExfatTruncate(struct Vnode *vp, off_t len);
int VfsExfatTruncate64(struct Vnode *vp, off64_t len);

/* ---- inode_info lifecycle — fs/exfat/exfat_inode_alloc.c ---------------
 * Memory-only helpers; no IO, no disk read/write. inode_lock initialised
 * with LOS_MUX_PRIO_INHERIT protocol to avoid priority inversion on the
 * per-inode lock. Spinlock-safe ranking:
 *   exfat_inode_init_dir_chain — pure assignment, spinlock-safe.
 *   exfat_inode_alloc / _free  — zalloc + LOS_MuxInit/Destroy, NOT spinlock-safe.
 */

/* Allocate + zero an exfat_inode_info, init inode_lock (PRIO_INHERIT).
 * Success: 0, *out written. Failure: -ENOMEM / -EIO; *out untouched; no leak.
 */
int  exfat_inode_alloc(exfat_inode_info **out);

/* Destroy inode_lock and free heap. NULL-safe (ei == NULL is no-op).
 * Caller must guarantee inode_lock is currently unlocked.
 */
void exfat_inode_free(exfat_inode_info *ei);

/* Set dir.{dir,flags,size}, type, start_clu — five fields — for a freshly
 * alloced ei representing a directory inode. Pure assignment; no IO/lock.
 */
void exfat_inode_init_dir_chain(exfat_inode_info *ei, uint32_t start_clu);

/* ---- dentry iteration — fs/exfat/exfat_dentry_iter.c -------------------
 * Public dentry IO + dentry-set parsing. All read-only, lock-free. Read of
 * one dentry-set is bounded by EXFAT_DENTRY_SET_MAX entries. Validate uses
 * exfat_calc_chksum16(..., CS_DIR_ENTRY) to skip the SetChecksum field per
 * Microsoft spec. See spec/exfat/dentry/exfat_dentry_iter.spec for the full
 * contract incl. cluster-boundary correctness rule.
 */
#define EXFAT_DENTRY_SET_MAX  19  /* 1 primary + 1 stream + 17 name */

/* Read one 32B dentry at linear index entry_idx in dir's chain. Returns
 * 0 / -EINVAL / -EIO / -ENOMEM. out_sector may be NULL.
 */
int  exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                      int entry_idx, struct exfat_dentry *out,
                      uint64_t *out_sector);

/* Read primary + secondaries of a file dentry-set starting at start_entry.
 * On success *num_entries = 1 + primary.num_ext. Errors per spec.
 */
int  exfat_get_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
                          int start_entry, struct exfat_dentry *set,
                          int max_entries, int *num_entries);

/* Pure-compute chksum16 verify of an in-memory dentry-set. Returns 0 if
 * SetChecksum matches, -EIO if mismatch, -EINVAL if set[0] is not a primary.
 */
int  exfat_validate_dentry_set(const struct exfat_dentry *set, int num_entries);

/* ---- UTF-16 / UTF-8 conversion + upcase compare — fs/exfat/util/exfat_nls_utf16.c
 * Pure compute (spinlock-safe). RFC 3629-strict UTF-8: rejects overlong,
 * surrogate-half input, > 0x10FFFF. Surrogate pairs in cmp are bit-exact
 * (Microsoft upcase only covers BMP).
 */
int  exfat_uni_to_utf8(const uint16_t *uni, int uni_len,
                       char *out, int out_max);
int  exfat_utf8_to_uni(const char *utf8, int utf8_len,
                       uint16_t *uni, int uni_max, int *uni_len);
int  exfat_uniname_cmp(const exfat_sb_info *sbi,
                       const uint16_t *a, int a_len,
                       const uint16_t *b, int b_len);

/* ---- VFS Lookup callback — fs/exfat/exfat_lookup.c --------------------
 * Faithful to Linux fs/exfat/namei.c::exfat_lookup: takes sbi->s_lock for
 * the entire body, no per-inode lock, no bitmap_lock. Decodes UTF-8 name,
 * walks parent's dentry stream, validates SetChecksum, compares via
 * sbi->vol_utbl, builds inode + Vnode + VfsHashInsert on match.
 * Returns 0 / -ENOENT / -EINVAL / -ENAMETOOLONG / -ENOMEM / -EIO. See
 * spec/exfat/interface/exfat_lookup.spec for the full contract.
 */
int VfsExfatLookup(struct Vnode *parent, const char *name, int len,
                   struct Vnode **vpp);

/* ---- VFS Reclaim — fs/exfat/exfat_super.c ----------------------------
 * vop->Reclaim handler. Called by VFS framework's VnodeFree() AFTER
 * VnodePathCacheFree() walks the vnode's path_cache lists and BEFORE the
 * vnode struct is recycled. Releases FS-private inode_info attached to
 * vnode->data. Wired into g_exfatVops at static-init in exfat_ops.c.
 */
int VfsExfatReclaim(struct Vnode *vnode);

/* ---- VFS Readdir bundle — fs/exfat/exfat_readdir.c --------------------
 * Four directory-traversal callbacks. Linux-faithful s_lock model: only
 * Readdir takes sbi->s_lock for the entire body (mirroring exfat_iterate);
 * Opendir / Closedir / Rewinddir touch only per-DIR fields and take no lock.
 * Cursor lives in idir->fd_int_offset (entry_idx); idir->u.fs_dir stays NULL
 * — no per-DIR allocation in this stage. See spec/exfat/interface/
 * exfat_readdir.spec.
 */
struct fs_dirent_s;
int VfsExfatOpendir(struct Vnode *vp, struct fs_dirent_s *idir);
int VfsExfatReaddir(struct Vnode *vp, struct fs_dirent_s *idir);
int VfsExfatClosedir(struct Vnode *vp, struct fs_dirent_s *idir);
int VfsExfatRewinddir(struct Vnode *vp, struct fs_dirent_s *idir);

/* ---- VFS Read callback — fs/exfat/exfat_file.c ------------------------
 * file_operations_vfs.read handler. Linux-faithful lock model: holds
 * ei->inode_lock for the entire body (mirrors Linux upper-layer auto-acquired
 * inode->i_rwsem); does NOT take sbi->s_lock (Linux exfat_get_block on read
 * path doesn't either) nor bitmap_lock. Walks fat chain per cluster, stages
 * IO through one cluster_size buffer, copies into user buf via memcpy_s.
 * Short-read on mid-loop IO failure mirrors Linux generic_file_read_iter.
 * Wired into g_exfatFops at static-init in exfat_ops.c. See
 * spec/exfat/interface/exfat_read.spec.
 */
ssize_t VfsExfatRead(struct file *filep, char *buf, size_t len);

/* ---- VFS Write callback — fs/exfat/exfat_write.c (Wave B Stage 1) ----
 * file_operations_vfs.write handler. In-place overwrite only; clamped to
 * [filep->f_pos, ei->size). No allocation, no extending, no dentry persistence
 * (deferred to Wave B2 truncate / B6 fsync). Cluster-level read-modify-write
 * via los_part_read + memcpy_s + los_part_write. Holds ei->inode_lock for the
 * entire body (mirrors Linux upper-layer vfs_write's exclusive inode->i_rwsem);
 * does NOT take sbi->s_lock nor bitmap_lock. Short-write semantics on mid-loop
 * IO failure (mirror Linux generic_file_write_iter). Wired into g_exfatFops at
 * static-init in exfat_ops.c. See spec/exfat/interface/exfat_write.spec.
 */
ssize_t VfsExfatWrite(struct file *filep, const char *buf, size_t len);

/* ---- VFS Open / Close callbacks — fs/exfat/exfat_open_close.c --------
 * file_operations_vfs.open / .close handlers (Wave A stubs). Mirror Linux
 * exfat's choice of generic_file_open / no .release: no allocation, no lock,
 * no IO. Open type-checks the vnode (returns -EISDIR for directories,
 * -EINVAL for malformed); Close always returns 0. Wired into g_exfatFops at
 * static-init in exfat_ops.c. See spec/exfat/interface/exfat_open_close.spec.
 */
int VfsExfatOpen(struct file *filep);
int VfsExfatClose(struct file *filep);

/* ---- VFS Getattr / Seek callbacks — fs/exfat/exfat_attr.c ------------
 * VnodeOps.Getattr fills struct stat from in-memory ei + sbi geometry
 * (Wave A: timestamps zero, populated in Wave B after dentry CrtTime/MtimeOff
 * parsing). file_operations_vfs.seek implements POSIX lseek with SEEK_SET /
 * SEEK_CUR / SEEK_END, allowing seek past EOF; rejects negative new pos with
 * -EINVAL, off_t overflow with -EOVERFLOW. Both take ei->inode_lock briefly
 * for ei->size snapshot only; do NOT take sbi->s_lock. See
 * spec/exfat/interface/exfat_vfs_ops_filled.spec.
 */
struct stat;
int   VfsExfatGetattr(struct Vnode *vp, struct stat *st);
off_t VfsExfatSeek(struct file *filep, off_t offset, int whence);

#ifdef __cplusplus
}
#endif

#endif /* LOSCFG_FS_EXFAT */
#endif /* _EXFAT_H */
