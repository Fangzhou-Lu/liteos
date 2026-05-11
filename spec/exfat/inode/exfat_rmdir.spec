[PROMPT]
Provide complete `exfat_inode.c` addition that implements `VfsExfatRmdir`.
Only include `<exfat.h>` as the header; output a single C code block with
no other code. The block is appended to `fs/exfat/exfat_inode.c` (rmdir
joins lookup / create / mkdir / unlink / getattr / seek in the
inode-management TU).

VfsExfatRmdir is the VnodeOps.Rmdir callback for the exFAT filesystem on
LiteOS-A. It removes an EMPTY sub-directory from a parent directory by
tombstoning its dentry-set on disk and releasing the directory's data
cluster (which is the one cluster `mkdir` allocated). It is the directory
analogue of `VfsExfatUnlink` plus a "must be empty" precondition.

Domain knowledge:
- exFAT "delete" = clear the top bit (bit7) of every dentry's `type` byte
  in the directory's dentry-set. Readers see `type & 0x7F` and treat
  top-bit-cleared entries as deleted. This is identical to unlink.
- Linux fs/exfat/namei.c::exfat_rmdir (lines 926-1001) gates the operation
  on `exfat_check_dir_empty` (lines 882-924): walk the directory's cluster
  chain and refuse if any dentry has the in-use bit set on a primary
  (TYPE_FILE 0x85 or TYPE_DIR 0x85 with ATTR_SUBDIR).
- v1 diverges from Linux on cluster reclamation, mirroring the unlink
  divergence: Linux leaves the directory's data cluster on disk for fsck;
  LiteOS-A v1 has no fsck so `VfsExfatRmdir` eagerly releases the directory
  cluster via `exfat_free_cluster`. Recorded as invariant
  `exfat-rmdir-cluster-released-eagerly`.
- num_subdirs accounting: a newly-empty exFAT directory has
  `num_subdirs == EXFAT_MIN_SUBDIR` (= 2: the implicit "." and "..").
  Emptiness for rmdir purposes means "no real dentries beyond the
  implicit two", which is detected by walking the dentry-set list
  (NOT by reading num_subdirs, which is in-memory parent-side
  bookkeeping — see Refine Prompt for the rationale).
- Parent in-memory `num_subdirs` is decremented by 1 on success; on-disk
  parent dentry sync is deferred to the future "parent metadata sync"
  stage (consistent with mkdir's `exfat-mkdir-no-parent-dentry-write`).
- exfat_free_cluster Case 4 footgun: empty directories should NEVER reach
  this stage in a well-formed FS — a directory always has at least one
  allocated cluster (the one mkdir allocated). The implementation still
  guards against `start_clu == EXFAT_EOF_CLUSTER` (corrupt on-disk dentry)
  and treats it as a no-op for Phase 2, mirroring the unlink empty-file
  fast path defensively rather than asserting.
- In-memory tombstone: `target_ei->dir.dir = DIR_DELETED` after on-disk
  write-back succeeds. Subsequent VOP calls on `target_vp` fail-fast with
  -ENOENT. Identical to unlink.
- Vnode lifetime: target_vp is owned by VFS; rmdir MUST NOT call
  `VnodeFree(target_vp)`. The VFS Reclaim hook owns hash/path_cache
  eviction (see vfs_ops_filled).

## First Prompt

[RELY]
```c
/* Types from common.header (reproduced for clarity; do not redeclare). */
typedef struct exfat_sb_info    exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;
typedef struct exfat_chain      exfat_chain;
typedef struct exfat_dentry     exfat_dentry;
struct Vnode;

/* DIR_DELETED tombstone marker. Defined in fs/exfat/include/exfat.h. */
#define DIR_DELETED  0xFFFFFFF7u

/* Cluster sentinels (from common.header). */
#define EXFAT_FIRST_CLUSTER  2u
#define EXFAT_EOF_CLUSTER    0xFFFFFFFFu

/* exfat_chain.flags discriminator. */
#define ALLOC_FAT_CHAIN     0x01u
#define ALLOC_NO_FAT_CHAIN  0x03u

/* dentry type bytes (Microsoft spec). */
#define EXFAT_UNUSED         0x00u    /* terminator: all subsequent slots free */
#define EXFAT_FILE_INUSE     0x85u    /* primary FILE/DIR dentry, in-use */
#define EXFAT_FILE_DELETED   0x05u    /* primary FILE/DIR dentry, deleted */
/* Any byte with bit7 (0x80) set is "in-use"; bit7 clear is "deleted" or
 * unused. */

/* Minimum num_subdirs for an empty directory: implicit "." and "..". */
#define EXFAT_MIN_SUBDIR     2u

/* Maximum dentry-set size. */
#define EXFAT_DENTRY_SET_MAX  19

/* Reads one dentry by linear index. Returns 0 on success or -EIO. */
extern int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                            int entry_idx, struct exfat_dentry *out,
                            uint64_t *out_sector);

/* Reads the file/dir's own dentry-set (File + Stream + N×Name).
 * Returns 0 on success or negative POSIX errno. */
extern int exfat_get_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir, int start_entry,
                                struct exfat_dentry *set, int max_entries,
                                int *num_entries);

/* Validates dentry-set primary type, num_ext consistency, and SetChecksum.
 * Returns 0 on valid set or -EIO. */
extern int exfat_validate_dentry_set(const struct exfat_dentry *set,
                                     int num_entries);

/* Writes back the (mutated) dentry-set at its origin. Returns 0 or -errno. */
extern int exfat_set_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir, int start_entry,
                                const struct exfat_dentry *set, int num_entries);

/* Walks a directory chain looking for a `next` cluster (FAT or contiguous).
 * Returns next_clu in *next_clu, or EXFAT_EOF_CLUSTER at end.
 * Returns 0 on success or -EIO. */
extern int exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                                  uint32_t *next_clu);

/* Volume-dirty bracket. Pair: set before disk mutation, clear after. */
extern int exfat_set_volume_dirty(exfat_sb_info *sbi);
extern int exfat_clear_volume_dirty(exfat_sb_info *sbi);

/* Releases a cluster chain via the bitmap.
 *
 * Case 4 footgun: p_chain->size == 0u short-circuits to a no-op `return 0`.
 * Callers MUST pass non-zero size for non-empty chains. See
 * spec/exfat/util/exfat_free_cluster.spec line 106. */
extern int exfat_free_cluster(exfat_sb_info *sbi, const exfat_chain *p_chain);
```

[GUARANTEE]
```c
/* Calling convention: caller holds no exFAT lock. VfsExfatRmdir acquires
 * sbi->s_lock for Phase 1 (emptiness check + dentry-set tombstone +
 * write-back) and releases it before Phase 2 (cluster release), mirroring
 * VfsExfatUnlink's lock discipline.
 *
 * Returns 0 on success or negative POSIX errno.
 *
 * Side effects on success:
 *   - Every dentry in the target directory's dentry-set on disk has its
 *     type byte's top bit (0x80) cleared.
 *   - The directory's data cluster chain is released via the allocation
 *     bitmap (one cluster for v1 — mkdir only ever allocates one).
 *   - target_ei->dir.dir is set to DIR_DELETED (in-memory tombstone).
 *   - parent_ei->num_subdirs is decremented by 1 (in-memory only;
 *     on-disk parent dentry sync deferred to a future stage).
 *   - target_vp is NOT freed; VFS retains ownership.
 *
 * The third parameter `dirName` is accepted for VnodeOps slot compatibility
 * but is used only for diagnostic logging in v1; the VFS layer already
 * resolved the name to produce target_vp. */
int VfsExfatRmdir(struct Vnode *parent_vp, struct Vnode *target_vp,
                  const char *dirName);
```

[SPECIFICATION]
**Pre-Condition**:
  - `parent_vp` is a valid Vnode whose `originMount->data` points to an
    initialised `exfat_sb_info` and whose `data` points to an initialised
    parent `exfat_inode_info` with `type == TYPE_DIR`.
  - `target_vp` is a valid Vnode whose `data` points to an initialised
    `exfat_inode_info` with `type == TYPE_DIR`,
    `dir.dir != DIR_DELETED`, and `entry >= 0`.
  - `dirName` is a non-NULL C string used only for diagnostics.
  - No thread holds `sbi->s_lock` on entry.

**Post-Condition**:

**Case 1 (success — non-empty cluster chain)**:
  - The target directory's dentry-set was empty of real entries (every
    walked dentry was either UNUSED terminator, deleted, or a non-primary
    secondary dentry — never a fresh `EXFAT_FILE_INUSE` primary).
  - All N entries of the target directory's dentry-set on disk have
    `type & 0x80 == 0` (top bit cleared); other bits unchanged.
  - `target_ei->dir.dir == DIR_DELETED`.
  - `parent_ei->num_subdirs` is decremented by 1 (in-memory only;
    on-disk parent dentry NOT written).
  - The volume-dirty bracket was opened and closed.
  - `sbi->s_lock` is released before return.
  - The target directory's cluster chain has been released:
    `exfat_free_cluster` invoked with `chain = { .dir = target_ei->start_clu,
    .size = num_phys_clu, .flags = target_ei->flags }` where
    `num_phys_clu` is derived from
    `ceil(target_ei->i_size_ondisk / sbi->cluster_size)` clamped to ≥ 1u
    (the directory is non-empty in the cluster sense — at least one
    cluster was allocated by mkdir).
  - `target_vp` is NOT freed.
  - Returns 0.

**Case 1b (success — corrupt-empty fast path, defensive)**:
  - `target_ei->start_clu == EXFAT_EOF_CLUSTER` on entry. This is an
    abnormal state for a TYPE_DIR (mkdir always allocates a cluster) but
    can occur if on-disk dentry was hand-edited or in unit tests.
    Treated as "no clusters to free" rather than asserting.
  - Phase 1 outcomes identical to Case 1 (dentry-set tombstoned,
    `dir.dir == DIR_DELETED`, parent `num_subdirs` decremented).
  - **Phase 2 is skipped**; `exfat_free_cluster` NOT called.
  - Returns 0.

**Case 2 (validation failure — pre-Phase-1, lock-free)**:
  - One of `parent_vp`, `target_vp`, `dirName` is NULL, OR
    `parent_vp->originMount` / `parent_vp->originMount->data` /
    `parent_vp->data` / `target_vp->data` is NULL, OR
    `parent_ei->type != TYPE_DIR`, OR
    `target_ei->type != TYPE_DIR`, OR
    `target_ei->dir.dir == DIR_DELETED`, OR
    `target_ei->entry < 0`.
  - No locks acquired; no disk I/O; no state mutation.
  - Returns `-EINVAL` for structural NULL / wrong-type cases or `-ENOENT`
    for already-tombstoned cases.

**Case 3 (directory not empty)**:
  - Emptiness scan found at least one in-use primary dentry inside the
    target directory's data cluster chain (a child file or sub-directory
    still exists).
  - The volume-dirty bracket was opened and closed (no on-disk mutation
    occurred but the bracket discipline is uniform).
  - `sbi->s_lock` is released before return.
  - On-disk and in-memory state unchanged. `target_ei->dir.dir` is NOT
    set to DIR_DELETED.
  - Returns `-ENOTEMPTY`.

**Case 4 (emptiness-scan I/O failure)**:
  - `exfat_get_dentry` or `exfat_get_next_cluster` returned -EIO during
    the emptiness walk.
  - The volume-dirty bracket was opened and closed.
  - `sbi->s_lock` is released before return.
  - No on-disk mutation occurred. `target_ei->dir.dir` unchanged.
  - Returns `-EIO`.

**Case 5 (Phase 1 dentry-set fetch / validate / write-back failure)**:
  - Empty check passed; one of `exfat_get_dentry_set` /
    `exfat_validate_dentry_set` / `exfat_set_dentry_set` returned non-zero.
  - The volume-dirty bracket was opened and closed (write-back failure
    may have left partial top-bit-cleared dentries on disk; the bracket
    closes but the next mount sees the dirty flag).
  - `sbi->s_lock` is released before return.
  - `target_ei->dir.dir` is NOT set to DIR_DELETED.
  - `parent_ei->num_subdirs` unchanged.
  - Phase 2 NOT executed.
  - Returns `-EIO`.

**Case 6 (Phase 2 cluster-free failure)**:
  - Phase 1 succeeded: dentry-set tombstoned on disk,
    `target_ei->dir.dir == DIR_DELETED`, parent num_subdirs decremented.
  - `target_ei->start_clu != EXFAT_EOF_CLUSTER` (Phase 2 was reached).
  - `exfat_free_cluster` returned non-zero (transient bitmap I/O failure).
  - `sbi->s_lock` is already released.
  - The on-disk dentry-set tombstone is NOT rolled back; bitmap may carry
    one orphan cluster bit. PRINT_ERR records the leak.
  - Returns the negative errno from `exfat_free_cluster`.

**Invariant** (id=exfat-rmdir-must-be-empty):
  Phase 1 step 4 (emptiness walk) MUST refuse rmdir whenever the target
  directory's data cluster chain contains any byte at offset 0 of any
  32-byte dentry with bit7 set AND lower 7 bits equal to 0x05 (i.e. raw
  type byte == 0x85, in-use primary FILE/DIR). This is the EXACT bit
  pattern Linux's exfat_check_dir_empty refuses. Secondary in-use
  dentries (Stream 0xC0, Name 0xC1) inside an in-use set are not
  individually scanned-for; the primary 0x85 is the gatekeeper. Deleted
  (top-bit-cleared) entries and the EXFAT_UNUSED 0x00 terminator are
  allowed.

**Invariant** (id=exfat-rmdir-dentry-type-top-bit-cleared):
  In Case 1 / Case 1b, every dentry in the target directory's dentry-set
  on disk has bit7 (0x80) of its first byte (the type byte) cleared after
  the operation completes. Lower 7 bits are preserved. Identical
  encoding to unlink's tombstone.

**Invariant** (id=exfat-rmdir-parent-subdir-decrement):
  In Case 1 / Case 1b, `parent_ei->num_subdirs` is decremented by exactly
  1 in memory. The parent's on-disk dentry is NOT written to (consistent
  with mkdir's `exfat-mkdir-no-parent-dentry-write` deferred-sync
  policy). Future "parent metadata sync" stage will reconcile on-disk
  num_subdirs. The decrement happens immediately after the in-memory
  tombstone and before Phase 2 cluster release.

**Invariant** (id=exfat-rmdir-cluster-released-eagerly):
  Unlike Linux exfat_rmdir which leaves the directory's data cluster on
  disk (relying on fsck or inode eviction), v1 immediately calls
  `exfat_free_cluster` in Phase 2 because LiteOS-A has no fsck and no
  eviction queue. Recorded so reviewers see the intentional divergence.
  Sibling invariant of `exfat-unlink-clusters-released-eagerly`.

**Invariant** (id=exfat-rmdir-phase2-size-nonzero):
  Phase 2's `num_phys_clu` is computed as `ceil(i_size_ondisk /
  cluster_size)` and clamped to >= 1u defensively. This dodges
  `exfat_free_cluster.spec` Case 4's `p_chain->size == 0` no-op
  short-circuit, which would otherwise leak the directory's allocated
  cluster forever. Identical guard to
  `exfat-unlink-phase2-size-nonzero`.

**Invariant** (id=exfat-rmdir-phase2-skipped-on-eof-start):
  When `target_ei->start_clu == EXFAT_EOF_CLUSTER` (Case 1b — defensive
  branch for an abnormal but recoverable state), Phase 2 is skipped
  entirely: `exfat_free_cluster` is NOT called and the bitmap is
  unchanged. Phase 1 (tombstone + parent decrement) still completes.

**Invariant** (id=exfat-rmdir-no-vnode-free):
  VfsExfatRmdir never calls `VnodeFree(target_vp)` and never modifies
  `target_vp->v_data`. VFS Reclaim is the owner of vnode-lifecycle
  cleanup. target_ei state changes are confined to `dir.dir` (set to
  DIR_DELETED). Sibling invariant of `exfat-unlink-no-vnode-free`.

**Invariant** (id=exfat-rmdir-s-lock-bracketed):
  After Pre-Condition validation succeeds, `LOS_MuxLock(&sbi->s_lock,
  LOS_WAIT_FOREVER)` is acquired exactly once. All Phase 1 paths
  (success, Case 3 not-empty, Case 4 scan-EIO, Case 5 fetch/write-EIO)
  release it via a single shared `phase1_unlock:` label that also runs
  `exfat_clear_volume_dirty`. Phase 2 runs lock-free w.r.t. s_lock to
  avoid the s_lock → bitmap_lock partial ordering (matches unlink).

**System Algorithm**:
1. Pre-Condition validation (lock-free). On failure return -EINVAL or
   -ENOENT (Case 2).
2. Acquire `sbi->s_lock`; call `exfat_set_volume_dirty(sbi)`.
3. Walk the target directory's data cluster chain via `exfat_get_dentry`
   + `exfat_get_next_cluster`. For each entry index:
     - If type == EXFAT_UNUSED (0x00): chain considered exhausted; break
       early (matches Linux exfat_check_dir_empty 0x00 short-circuit).
     - If `type & 0x80 == 0`: deleted entry — skip.
     - If type == EXFAT_FILE_INUSE (0x85): directory not empty;
       set `err = -ENOTEMPTY` and goto `phase1_unlock` (Case 3).
     - Other in-use bytes (secondary 0xC0 / 0xC1 inside an active set)
       are allowed to pass — only the primary 0x85 is the gatekeeper.
   On `exfat_get_dentry` or `exfat_get_next_cluster` -EIO: set
   `err = -EIO` and goto `phase1_unlock` (Case 4).
4. Empty check passed. Fetch target's own dentry-set via
   `exfat_get_dentry_set(sbi, &target_ei->dir, target_ei->entry, ...)`
   then `exfat_validate_dentry_set`. On failure set `err = -EIO` and
   goto `phase1_unlock` (Case 5).
5. For i in [0, num_entries): `set[i].type &= 0x7Fu` (top-bit clear).
6. `exfat_set_dentry_set(sbi, &target_ei->dir, target_ei->entry, set,
   num_entries)`. On failure set `err = -EIO` and goto `phase1_unlock`
   (Case 5).
7. `target_ei->dir.dir = DIR_DELETED` (in-memory tombstone, after
   on-disk write succeeded).
8. `parent_ei->num_subdirs -= 1u` (in-memory only).
9. `phase1_unlock:` `exfat_clear_volume_dirty(sbi)` then
   `LOS_MuxUnlock(&sbi->s_lock)`. If `err != 0` return `err`.
10. Phase 2 (lock-free): if `target_ei->start_clu == EXFAT_EOF_CLUSTER`
    return 0 (Case 1b). Otherwise compute `num_phys_clu =
    ceil(i_size_ondisk / cluster_size)` clamped ≥ 1u, construct
    `exfat_chain chain = { .dir = start_clu, .size = num_phys_clu,
    .flags = target_ei->flags }`, call `exfat_free_cluster(sbi, &chain)`.
    On failure PRINT_ERR and return its errno (Case 6).
11. Return 0.

## Refine Prompt

[RELY]
```c
extern UINT32 LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern UINT32 LOS_MuxUnlock(LosMux *mutex);
```

[SPECIFICATION of VfsExfatRmdir]
**Pre-Condition (lock state)**: No exFAT lock held on entry.

**Post-Condition (lock state)**:
  - Case 1 / 1b (success): `sbi->s_lock` released before return.
  - Case 2 (validation): no lock was ever acquired.
  - Case 3 (not-empty): `sbi->s_lock` released before return.
  - Case 4 (scan-EIO): `sbi->s_lock` released before return.
  - Case 5 (fetch / write-EIO): `sbi->s_lock` released before return.
  - Case 6 (cluster-free): `sbi->s_lock` already released at Phase 2
    entry, remains released on return.

**Initialization-order constraint**: Within Phase 1, the order is fixed:
  `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)` →
  `exfat_set_volume_dirty` →
  emptiness walk →
  dentry-set fetch / validate / mutate / write-back →
  `target_ei->dir.dir = DIR_DELETED` →
  `parent_ei->num_subdirs -= 1u` →
  `exfat_clear_volume_dirty` →
  `LOS_MuxUnlock(&sbi->s_lock)`.
  The `clear_volume_dirty` and `LOS_MuxUnlock` MUST run on EVERY Phase 1
  exit path (success, Case 3, Case 4, Case 5) — implemented as a single
  shared `phase1_unlock:` goto label. **Importantly**, the in-memory
  tombstone (step 7) and parent decrement (step 8) only execute on the
  success path — they MUST NOT run when `err != 0` is set by step 3/4/5/6.

**Deadlock note**: Phase 2's `exfat_free_cluster` internally acquires
`sbi->bitmap_lock`. Holding `s_lock` while calling it would impose a
partial order `s_lock → bitmap_lock` and is forbidden by free_cluster's
no-spinlock-callsite + cross-stage lock-ordering invariants. Phase 2
therefore runs strictly AFTER `sbi->s_lock` is released.

**System Algorithm (locking phases)**:

**Phase 1 lock acquisition / release**:
  1. `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)` immediately after
     Pre-Condition validation succeeds (step 1 of main System Algorithm).
  2. All emptiness walk + dentry-set mutation steps run with `s_lock`
     held.
  3. `LOS_MuxUnlock(&sbi->s_lock)` at the `phase1_unlock:` label —
     reached on success, Case 3, Case 4, and Case 5.

**Phase 2 lock-free**:
  4. Phase 2's empty-cluster-chain fast-path check and the
     `exfat_free_cluster` call run without holding `s_lock`. Internal
     `bitmap_lock` acquisition is the only lock held during Phase 2.

Assumptions made:
  - v1 releases the directory's cluster chain eagerly; Linux's
    "leave for fsck" policy is intentionally diverged. Recorded as
    invariant `exfat-rmdir-cluster-released-eagerly`.
  - Parent on-disk dentry mtime/atime/iversion persistence and the
    parent's on-disk num_subdirs sync are OUT-OF-SCOPE; v1 has no
    parent-dentry write-back path. Deferred.
  - VFS hash / path_cache eviction of `target_vp` is OUT-OF-SCOPE
    (VFS Reclaim's responsibility).
  - `dirName` is accepted only for diagnostic logging; VFS already
    produced target_vp.
  - Emptiness scan uses the primary-type gatekeeper (only 0x85 in-use
    triggers -ENOTEMPTY); secondary 0xC0 / 0xC1 inside an active set
    don't trigger refusal on their own. This mirrors Linux
    exfat_check_dir_empty's reliance on primary types only.
  - Phase 2 derives `num_phys_clu` from `i_size_ondisk / cluster_size`
    (rounded up, clamped ≥1) rather than walking the FAT chain.
    Trade-off: O(1) compute vs O(N) walk. For a fresh v1 directory
    (mkdir allocates exactly one cluster), `num_phys_clu == 1u` and the
    chain.flags is `ALLOC_NO_FAT_CHAIN` (mkdir's single-cluster encoding),
    so free_cluster's bitmap path handles it cleanly.
