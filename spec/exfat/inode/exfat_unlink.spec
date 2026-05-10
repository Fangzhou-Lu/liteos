[PROMPT]
Provide complete `exfat_unlink.c` that implements `VfsExfatUnlink`. Only include
`<exfat.h>` as the header; output a single C code block with no other code.

VfsExfatUnlink is the VnodeOps.Unlink callback for the exFAT filesystem on
LiteOS-A. It removes a regular file from a parent directory by tombstoning
its dentry-set on disk and releasing the file's data clusters.

Domain knowledge:
- exFAT "delete" = clear the top bit (bit7) of every dentry's `type` byte in
  the file's dentry-set. Readers see `type & 0x7F` and treat top-bit-cleared
  entries as deleted. The dentry-set storage itself is NOT reclaimed; it is
  available for reuse by a future create/mkdir on the same parent.
- Linux fs/exfat/namei.c::exfat_unlink (line 774, comment "/* remove an entry,
  BUT don't truncate */") deliberately leaves data clusters on disk and
  delegates cluster release to fsck or to inode eviction. LiteOS-A v1 has no
  fsck and no inode eviction queue; leaving clusters allocated leaks them
  permanently. **v1 diverges from Linux**: VfsExfatUnlink immediately
  releases the file's cluster chain via `exfat_free_cluster`, mirroring
  Linux exfat_evict_inode + truncate-to-0 collapsed into one VOP. This
  is recorded as invariant `exfat-unlink-clusters-released-eagerly` so
  future stages (and reviewers) see the deviation.
- exfat_free_cluster Case 4 footgun: `p_chain.size == 0` short-circuits to a
  no-op `return 0` per spec/exfat/util/exfat_free_cluster.spec line 106.
  Phase 2 MUST NOT pass size=0 for non-empty files; it computes a non-zero
  cluster count from `ei->i_size_ondisk / sbi->cluster_size` (rounded up).
  For the empty-file case (`start_clu == EXFAT_EOF_CLUSTER`) Phase 2 is
  skipped entirely. Reference: truncate_shrink (fs/exfat/exfat_file.c:1395-
  1397, 1456-1458) is the canonical pattern.
- In-memory tombstone: `target_ei->dir.dir = DIR_DELETED` (= 0xFFFFFFF7,
  defined in fs/exfat/include/exfat.h). Subsequent VOP calls on `target_vp`
  fail fast with -ENOENT before touching disk. This protects against
  late-arriving lookups via stale cached vnodes.
- Vnode lifetime: target_vp is owned by the VFS layer (path_cache evicts
  via VfsHashRemove on its own schedule). VfsExfatUnlink MUST NOT call
  VnodeFree(target_vp). Touching only its `data` (the exfat_inode_info)
  is in-bounds. **OUT-OF-SCOPE**: VFS hash / path_cache eviction of
  `target_vp` is owned by the VFS layer's Reclaim hook (see
  spec/exfat/interface/exfat_vfs_ops_filled.spec), not by VfsExfatUnlink.
- v1 OUT-OF-SCOPE: parent directory mtime/atime persistence to the parent's
  dentry-set on disk. Linux exfat_unlink calls inode_inc_iversion(dir) +
  exfat_truncate_atime(&dir->i_atime) + mark_inode_dirty(dir); LiteOS-A
  v1 has no dentry write-back path for parent metadata (vfs_ops_filled is
  read-only stat). Deferred to a future "parent metadata sync" stage.

## First Prompt

[RELY]
```c
/* Types from common.header (reproduced for clarity; do not redeclare). */
typedef struct exfat_sb_info    exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;
typedef struct exfat_chain      exfat_chain;
typedef struct exfat_dentry     exfat_dentry;
struct Vnode;

/* DIR_DELETED tombstone marker for exfat_inode_info::dir.dir.
 * Defined in fs/exfat/include/exfat.h; reproduced here for spec clarity. */
#define DIR_DELETED  0xFFFFFFF7u

/* Empty-file sentinel: start_clu == EXFAT_EOF_CLUSTER means no data clusters
 * have been allocated yet (lazy allocation; see exfat_create.spec invariant
 * exfat-create-no-cluster-on-empty). */
#define EXFAT_EOF_CLUSTER  0xFFFFFFFFu

/* exfat_chain.flags discriminator: ALLOC_FAT_CHAIN = walk via FAT (size hint
 * unused beyond Case 4 zero check), ALLOC_NO_FAT_CHAIN = contiguous
 * [dir, dir+size). Empty files use ALLOC_NO_FAT_CHAIN with start_clu=EOF. */
#define ALLOC_FAT_CHAIN     0x01u
#define ALLOC_NO_FAT_CHAIN  0x03u

/* Maximum dentry-set size (File + Stream + N×Name); from common.header. */
#define EXFAT_DENTRY_SET_MAX  19

/* Reads the file's dentry-set (File + Stream + Name) into `set`.
 * Returns 0 on success or negative POSIX errno. set is undefined on failure. */
extern int exfat_get_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir, int start_entry,
                                struct exfat_dentry *set, int max_entries,
                                int *num_entries);

/* Validates a dentry-set's primary type, num_ext consistency, and SetChecksum.
 * Returns 0 on valid set, -EIO on any inconsistency. */
extern int exfat_validate_dentry_set(const struct exfat_dentry *set,
                                     int num_entries);

/* Writes the supplied dentry-set back to disk at the same location it was
 * read from. Returns 0 on success or negative POSIX errno. Partial writes
 * may have hit disk on -EIO (caller treats as on-disk damage). */
extern int exfat_set_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir, int start_entry,
                                const struct exfat_dentry *set, int num_entries);

/* Marks/clears the on-disk volume-dirty flag bracket. Pair: any mutator
 * must call set before modifying disk and clear after (success or fail). */
extern int exfat_set_volume_dirty(exfat_sb_info *sbi);
extern int exfat_clear_volume_dirty(exfat_sb_info *sbi);

/* Releases a cluster chain via the bitmap. Does NOT mutate FAT entries
 * (per free_cluster.spec invariant).
 *
 * IMPORTANT — Case 4 footgun: when `p_chain->size == 0u` (or dir is
 * FREE/EOF/<FIRST_CLUSTER) this function returns 0 immediately with no
 * bitmap mutation. Callers that intend to release clusters MUST pass a
 * non-zero size. For ALLOC_FAT_CHAIN the size acts as a Case-4 guard +
 * defensive upper bound on the FAT walk; for ALLOC_NO_FAT_CHAIN the size
 * is the exact cluster count.
 *
 * Returns 0 on success or negative errno. */
extern int exfat_free_cluster(exfat_sb_info *sbi, const exfat_chain *p_chain);
```

[GUARANTEE]
```c
/* Calling convention: caller holds no exFAT lock. VfsExfatUnlink acquires
 * sbi->s_lock for Phase 1 (dentry-set tombstone + write-back), releases it,
 * then performs Phase 2 (cluster chain release) lock-free w.r.t. s_lock.
 * Returns 0 on success or negative POSIX errno.
 *
 * Side effects on success:
 *   - File's dentry-set on disk has every entry's type byte's top bit cleared
 *     (exFAT "deleted" marker).
 *   - For non-empty files: the file's cluster chain is released via the
 *     allocation bitmap. For empty files (start_clu == EXFAT_EOF_CLUSTER):
 *     no bitmap mutation occurs.
 *   - target_ei->dir.dir is set to DIR_DELETED (in-memory tombstone).
 *   - target_vp is NOT freed; VFS retains ownership.
 */
int VfsExfatUnlink(struct Vnode *parent_vp, struct Vnode *target_vp,
                   const char *fileName);
```

[SPECIFICATION]
**Pre-Condition**:
  - `parent_vp` is a valid Vnode whose `originMount->data` points to an
    initialised `exfat_sb_info`.
  - `target_vp` is a valid Vnode whose `data` points to an initialised
    `exfat_inode_info` with `type == TYPE_FILE` and `dir.dir != DIR_DELETED`
    and `entry >= 0`.
  - `fileName` is a non-NULL C string (used only for diagnostic logging in
    v1; resolution is delegated to the VFS layer which already produced
    `target_vp`).
  - `*vpp` is unused — VfsExfatUnlink does not produce a new Vnode.

**Post-Condition**:

**Case 1 (success — non-empty file)**:
  - `target_ei->start_clu != EXFAT_EOF_CLUSTER` on entry.
  - All N entries of the target file's dentry-set on disk have
    `type & 0x80 == 0` (top bit cleared); other bits unchanged.
  - The volume-dirty bracket has been opened (`set_volume_dirty`) and closed
    (`clear_volume_dirty`).
  - `target_ei->dir.dir == DIR_DELETED`.
  - The cluster chain rooted at `target_ei->start_clu` has been released:
    every cluster's bitmap bit is 0; FAT entries are not modified
    (per free_cluster.spec). Phase 2 invoked `exfat_free_cluster` with
    `chain.size == ceil(target_ei->i_size_ondisk / sbi->cluster_size)`
    (≥1) and `chain.flags == target_ei->flags`.
  - `parent_vp` is unmodified; `parent_ei->num_subdirs` is unchanged
    (the file was not a subdirectory).
  - `target_vp` is NOT freed; its lifetime stays with VFS.
  - Returns 0.

**Case 1b (success — empty file fast path)**:
  - `target_ei->start_clu == EXFAT_EOF_CLUSTER` on entry (file never had
    data clusters allocated; common after `VfsExfatCreate` followed by
    immediate unlink without a write).
  - All N entries of the target file's dentry-set on disk have top bit
    cleared (same as Case 1).
  - `target_ei->dir.dir == DIR_DELETED`.
  - **Phase 2 is skipped**; `exfat_free_cluster` is NOT called. The
    bitmap is unchanged (no clusters were ever allocated to free).
  - `target_vp` is NOT freed.
  - Returns 0.

**Case 2 (validation failure — pre-Phase-1, lock-free)**:
  - One of `parent_vp`, `target_vp`, `fileName` is NULL, OR
    `parent_vp->originMount` / `parent_vp->originMount->data` /
    `target_vp->data` is NULL, OR
    `target_ei->type != TYPE_FILE`, OR
    `target_ei->dir.dir == DIR_DELETED`, OR
    `target_ei->entry < 0`.
  - No locks acquired; no disk I/O; no state mutation.
  - Returns `-EINVAL` for structural NULL / wrong-type cases or `-ENOENT` for
    already-tombstoned cases.

**Case 3 (Phase 1 dentry-set fetch / validate failure)**:
  - `exfat_get_dentry_set` or `exfat_validate_dentry_set` returned non-zero.
  - The volume-dirty bracket has been opened and closed.
  - `sbi->s_lock` has been released.
  - No disk write occurred; in-memory state unchanged.
  - `target_ei->dir.dir` is unchanged (NOT set to DIR_DELETED).
  - Returns `-EIO`.

**Case 4 (Phase 1 dentry-set write-back failure)**:
  - `exfat_set_dentry_set` returned non-zero. Some dentries may be on disk
    with the top bit already cleared (partial write); the volume-dirty flag
    persists across the failure (the bracket-clear still ran but the on-disk
    boot vol_flags carries a fsck signal indirectly via the failed write
    leaving partial state).
  - The volume-dirty bracket has been closed.
  - `sbi->s_lock` has been released.
  - `target_ei->dir.dir` is unchanged.
  - Phase 2 (cluster release) is NOT executed.
  - Returns `-EIO`.

**Case 5 (Phase 2 cluster-free failure)**:
  - Phase 1 succeeded: dentry-set is tombstoned on disk and
    `target_ei->dir.dir == DIR_DELETED`.
  - `target_ei->start_clu != EXFAT_EOF_CLUSTER` (Phase 2 was reached).
  - `exfat_free_cluster` returned non-zero (transient bitmap I/O failure).
  - `sbi->s_lock` is already released.
  - The on-disk dentry-set tombstone is NOT rolled back. Bitmap may carry
    orphan bits the next fsck would reclaim. PRINT_ERR records the leak.
  - Returns the negative errno from `exfat_free_cluster`.

**Invariant** (id=exfat-unlink-dentry-type-top-bit-cleared):
  In Case 1 and Case 1b, every byte at offset 0 of every dentry in the
  target file's dentry-set has its top bit (0x80) cleared. Lower 7 bits
  are preserved. This is the exFAT specification's encoding of a deleted
  entry; readers treat such entries as not present.

**Invariant** (id=exfat-unlink-tombstone-prevents-reuse):
  `target_ei->dir.dir = DIR_DELETED` is set ONLY after `exfat_set_dentry_set`
  returns success. On Case 3 / Case 4 the in-memory tombstone is NOT
  applied. This guarantees: any subsequent VOP that observes the tombstone
  can safely assume the on-disk dentry-set is also tombstoned.

**Invariant** (id=exfat-unlink-vol-dirty-bracketed):
  `exfat_set_volume_dirty` and `exfat_clear_volume_dirty` envelope every
  Phase 1 disk-mutating step. `clear_volume_dirty` MUST run on every Phase 1
  exit path including Case 3 and Case 4. Mirrors the create/mkdir bracket
  policy; future fsck depends on this signal.

**Invariant** (id=exfat-unlink-free-after-tombstone):
  Phase 2 (`exfat_free_cluster`) executes ONLY after Phase 1 succeeded
  (dentry-set tombstoned and `DIR_DELETED` applied). Reverse order would
  leave a window where readers can still observe a live dentry-set whose
  data clusters are already on the free list — a cross-process double-free
  hazard.

**Invariant** (id=exfat-unlink-phase2-size-nonzero):
  Whenever Phase 2 is invoked, `chain.size != 0u`. Specifically, Phase 2
  derives `num_phys_clu = (target_ei->i_size_ondisk + cluster_size - 1) /
  cluster_size` clamped to `>= 1u`, then sets `chain.size = num_phys_clu`.
  This avoids `exfat_free_cluster.spec` Case 4's `size == 0` short-circuit
  (line 106) which would make Phase 2 a silent no-op and permanently leak
  the file's data clusters in the bitmap.

**Invariant** (id=exfat-unlink-phase2-skipped-when-empty):
  When `target_ei->start_clu == EXFAT_EOF_CLUSTER` (empty file from lazy
  allocation; see exfat-create-no-cluster-on-empty), Phase 2 is **skipped
  entirely** — no `exfat_free_cluster` call is made. There are no
  clusters to release; calling free_cluster with an EOF start would either
  hit free_cluster Case 4 (no-op, harmless) or Case 5 (-EIO, false
  failure). Skipping is the only correct behavior.

**Invariant** (id=exfat-unlink-no-vnode-free):
  VfsExfatUnlink does not call `VnodeFree` on `target_vp`. Vnode lifetime is
  owned by the VFS path-cache layer. Touching `target_vp->data` (the
  exfat_inode_info) is permitted; freeing the Vnode itself is forbidden.
  Path-cache eviction of `target_vp` is the VFS layer's responsibility
  (Reclaim hook), not VfsExfatUnlink's.

**Invariant** (id=exfat-unlink-not-for-directories):
  `target_ei->type` MUST be `TYPE_FILE`. Directory removal is the
  separate `rmdir` VOP (subdirectory invariants — empty check,
  parent num_subdirs decrement — do not apply here). Pre-Condition
  rejects `TYPE_DIR` with `-EINVAL`.

**Invariant** (id=exfat-unlink-clusters-released-eagerly):
  v1 diverges from Linux fs/exfat/namei.c::exfat_unlink. Linux leaves
  data clusters allocated and delegates reclaim to fsck or
  exfat_evict_inode. LiteOS-A v1 has no fsck and no eviction queue; this
  VOP releases the cluster chain immediately in Phase 2. Future evolution
  may reintroduce delayed reclaim; the present spec freezes
  eager-release semantics.

**System Algorithm**:

**Phase 1: Validate + tombstone dentry-set (under sbi->s_lock)**
  - Goal: atomically replace the on-disk dentry-set with its tombstoned
    version under the volume-dirty bracket.
  - Algorithm:
    1. Lock-free Pre-Condition validation. On any failure: return -EINVAL
       or -ENOENT (Case 2).
    2. Acquire `sbi->s_lock` (Phase 2 trigger; see ## Refine Prompt).
    3. `exfat_set_volume_dirty(sbi)`.
    4. `exfat_get_dentry_set(sbi, &target_ei->dir, target_ei->entry,
       set, EXFAT_DENTRY_SET_MAX, &num_entries)`.
    5. `exfat_validate_dentry_set(set, num_entries)`.
       Steps 4 and 5 failures route to Case 3.
    6. For i in [0, num_entries): `set[i].type &= 0x7Fu`.
    7. `exfat_set_dentry_set(sbi, &target_ei->dir, target_ei->entry,
       set, num_entries)`. Failure routes to Case 4.
    8. `target_ei->dir.dir = DIR_DELETED`.
  - Post-condition: the on-disk dentry-set is tombstoned AND the in-memory
    tombstone marker is set (Case 1 / 1b path); OR neither has been
    applied (Case 3); OR partial on-disk write may exist but in-memory
    marker is NOT set (Case 4).
  - Error Handling: Cases 3 and 4 must still run `exfat_clear_volume_dirty`
    and release `sbi->s_lock` before returning.

**Phase 2: Release cluster chain (lock-free w.r.t. sbi->s_lock)**
  - Goal: free the file's data clusters via the allocation bitmap.
  - Algorithm:
    1. **Empty-file fast path**: if `target_ei->start_clu ==
       EXFAT_EOF_CLUSTER`, return 0 (Case 1b — no clusters to free).
    2. Compute `num_phys_clu = (uint32_t)((target_ei->i_size_ondisk +
       (uint64_t)sbi->cluster_size - 1u) / (uint64_t)sbi->cluster_size)`.
       Clamp to `>= 1u` defensively (a file with start_clu != EOF but
       i_size_ondisk == 0 is an internal-state anomaly; treat as 1
       cluster to surface via free_cluster's normal walk rather than
       silent no-op).
    3. Construct `exfat_chain chain = { .dir = target_ei->start_clu,
       .size = num_phys_clu, .flags = target_ei->flags }`.
    4. `exfat_free_cluster(sbi, &chain)`.
  - Post-condition: every cluster previously allocated to the file is
    free in the bitmap (Case 1); or no bitmap mutation (Case 1b); or
    PRINT_ERR-logged orphan clusters remain in the bitmap with the
    dentry-set still tombstoned on disk (Case 5).
  - Error Handling: do NOT roll back the Phase 1 tombstone; PRINT_ERR
    records the leak; return the upstream errno.

## Refine Prompt

[RELY]
```c
extern UINT32 LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern UINT32 LOS_MuxUnlock(LosMux *mutex);
```

[SPECIFICATION of VfsExfatUnlink]
**Pre-Condition (lock state)**: No exFAT lock held on entry.

**Post-Condition (lock state)**:
  - Case 1 / Case 1b (success): `sbi->s_lock` released before return.
  - Case 2 (validation): no lock was ever acquired.
  - Case 3 (fetch / validate): `sbi->s_lock` released before return.
  - Case 4 (write-back): `sbi->s_lock` released before return.
  - Case 5 (cluster-free): `sbi->s_lock` already released at Phase 2 entry,
    remains released on return.

**Initialization-order constraint**: Within Phase 1, the order is fixed:
  `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)` →
  `exfat_set_volume_dirty` →
  dentry-set fetch / validate / mutate / write-back →
  `target_ei->dir.dir = DIR_DELETED` (only on write-back success) →
  `exfat_clear_volume_dirty` →
  `LOS_MuxUnlock(&sbi->s_lock)`.
  The `clear_volume_dirty` and `LOS_MuxUnlock` MUST run on EVERY Phase 1
  exit path (success and failure) — implemented as a single shared
  `phase1_unlock:` goto label.

**Deadlock note**: Phase 2's `exfat_free_cluster` internally acquires
`sbi->bitmap_lock` (mux). Calling it while still holding `sbi->s_lock`
would impose a partial order `s_lock → bitmap_lock` and is forbidden by
the cross-stage lock-ordering invariant for free_cluster (which assumes
`bitmap_lock` is the only lock held). Phase 2 therefore runs strictly
AFTER `sbi->s_lock` is released.

**System Algorithm (locking phases)**:

**Phase 1 lock acquisition / release**:
  1. `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)` immediately after
     Pre-Condition validation succeeds.
  2. All Phase 1 disk-mutating steps run with `s_lock` held.
  3. `LOS_MuxUnlock(&sbi->s_lock)` at the `phase1_unlock:` label —
     reached on success, Case 3, and Case 4.

**Phase 2 lock-free**:
  4. Phase 2's empty-file fast-path check (`start_clu == EXFAT_EOF_CLUSTER`)
     and the `exfat_free_cluster` call run without holding `s_lock`.
     Internal `bitmap_lock` acquisition (transitively, per cluster batch)
     is the only lock held during Phase 2; `exfat_free_cluster.spec`
     no-spinlock-callsite invariant continues to apply.

Assumptions made:
  - v1 releases the file's cluster chain eagerly; Linux's "leave clusters
    for fsck" policy is intentionally diverged because LiteOS-A has no
    fsck. Recorded as invariant `exfat-unlink-clusters-released-eagerly`.
  - Parent directory mtime/atime/iversion persistence is OUT-OF-SCOPE; v1
    has no parent-dentry write-back path. Deferred to a future
    "parent metadata sync" stage.
  - VFS hash / path_cache eviction of `target_vp` is OUT-OF-SCOPE — owned
    by the VFS Reclaim hook (vfs_ops_filled), not VfsExfatUnlink.
  - `fileName` is accepted but only used for diagnostic logging; the VFS
    layer already produced `target_vp` so name resolution does not happen
    here.
  - Phase 2 derives `num_phys_clu` from `i_size_ondisk / cluster_size`
    (rounded up, clamped ≥1) rather than walking the FAT chain. Trade-off:
    O(1) compute vs O(N) walk; ALLOC_FAT_CHAIN's free_cluster.spec Case 7
    walks the actual FAT chain regardless of size hint, so the size value
    is used only to dodge Case 4's no-op short-circuit.
