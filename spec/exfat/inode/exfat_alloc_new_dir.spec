[PROMPT]
Provide complete `exfat_alloc_new_dir.c` file that implements `exfat_alloc_new_dir`.
Only include `<exfat.h>`; output a single C code block.

`exfat_alloc_new_dir` allocates one fresh data cluster for a newly created
directory and zeroes its entire content before returning. It is called by
`exfat_add_entry` immediately before the dentry-set for the new directory
is written to disk, so the cluster must be fully zeroed and its bitmap bit
set before the caller proceeds. On success the caller promotes the chain
flags from ALLOC_FAT_CHAIN to ALLOC_NO_FAT_CHAIN: a one-cluster directory
never needs a FAT walk.

The alloc + zero pair is treated as one atomic action: if zeroing fails the
cluster must be released before returning, leaving the bitmap and FAT table
in the state they were in before the call.

[RELY]
```c
#define ALLOC_FAT_CHAIN    0x01
#define ALLOC_NO_FAT_CHAIN 0x03
#define EXFAT_EOF_CLUSTER  0xFFFFFFFF
#define EXFAT_FREE_CLUSTER 0x00000000
#define EXFAT_CLUSTERS_UNTRACKED (~0u)

// Allocate num_alloc clusters, threading them into the FAT chain described
// by p_chain.  p_chain.flags MUST be ALLOC_FAT_CHAIN on entry.  On success
// p_chain.dir holds the first allocated cluster and p_chain.size is
// incremented by num_alloc.  Acquires and releases sbi->bitmap_lock
// internally; caller must NOT hold bitmap_lock.  Returns 0 or -errno.
extern int exfat_alloc_cluster(exfat_sb_info *sbi,
                                uint32_t num_alloc, exfat_chain *p_chain);

// Write zeros to every byte of cluster clu (sbi->cluster_size bytes) via
// los_part_write.  Does NOT hold bitmap_lock.  Returns 0 or -EIO.
extern int exfat_zeroed_cluster(exfat_sb_info *sbi, uint32_t clu);

// Clear the allocation-bitmap bit for cluster clu in sbi->vol_amap and
// persist the change to disk.  Caller MUST already hold sbi->bitmap_lock
// OR ensure no concurrent allocator is running (rollback context only).
// Returns 0 or -errno.
extern int exfat_clear_bitmap(exfat_sb_info *sbi, uint32_t clu);

// Write a single FAT entry: sets FAT[loc] = value on disk.
// Does not touch bitmap.  Returns 0 or -errno.
extern int exfat_ent_set(const exfat_sb_info *sbi,
                          uint32_t loc, uint32_t value);
```

[GUARANTEE]
```c
/* Calling convention: caller holds no lock.  bitmap_lock is acquired and
 * released internally by exfat_alloc_cluster; the rollback path also runs
 * without bitmap_lock (clear_bitmap is safe because the cluster is known to
 * the caller alone at that point — no concurrent allocator can see it).
 * Returns 0 on success; negative POSIX errno on failure.
 * On success: clu_out->dir = allocated cluster number,
 *             clu_out->size = 1,
 *             clu_out->flags = ALLOC_NO_FAT_CHAIN.
 * On failure: clu_out->dir = EXFAT_EOF_CLUSTER,
 *             clu_out->size = 0,
 *             clu_out->flags = ALLOC_NO_FAT_CHAIN. */
int exfat_alloc_new_dir(exfat_sb_info *sbi, exfat_chain *clu_out);
```

[SPECIFICATION]
**Pre-Condition**:
  - `sbi` is a valid, fully initialised `exfat_sb_info`.
  - `clu_out` is a valid writable pointer to an `exfat_chain`; its initial
    contents are undefined (function initialises them).
  - Caller holds no lock (in particular: does NOT hold `sbi->bitmap_lock`).
  - The volume has at least one free cluster (`sbi->used_clusters <
    sbi->num_clusters - EXFAT_RESERVED_CLUSTERS`, or
    `used_clusters == EXFAT_CLUSTERS_UNTRACKED`).

**Post-Condition**:

**Case 1 (success — cluster allocated and zeroed)**:
  - Exactly one cluster has been allocated; its index is `clu_out->dir`.
  - The allocation-bitmap bit for `clu_out->dir` is set on disk.
  - Every byte of cluster `clu_out->dir` on disk is zero.
  - `clu_out->size  == 1`.
  - `clu_out->flags == ALLOC_NO_FAT_CHAIN` (single-cluster chain needs no
    FAT walk; promotion is done by this function, not by the caller).
  - `sbi->used_clusters` is incremented by 1 (unless it was
    `EXFAT_CLUSTERS_UNTRACKED`, in which case it remains unchanged).
  - Returns `0`.

**Case 2 (alloc failure — exfat_alloc_cluster returns non-zero)**:
  - No cluster was allocated; the bitmap is unchanged.
  - `clu_out->dir   == EXFAT_EOF_CLUSTER`.
  - `clu_out->size  == 0`.
  - `clu_out->flags == ALLOC_NO_FAT_CHAIN`.
  - Returns the negative errno forwarded from `exfat_alloc_cluster`.

**Case 3 (zero failure — exfat_zeroed_cluster returns non-zero)**:
  - `exfat_alloc_cluster` succeeded; the cluster bit was set in the bitmap.
  - Rollback is performed inline:
      1. `exfat_clear_bitmap(sbi, allocated_clu)` clears the bitmap bit.
      2. `exfat_ent_set(sbi, allocated_clu, EXFAT_FREE_CLUSTER)` resets the
         FAT entry.
      3. If `sbi->used_clusters != EXFAT_CLUSTERS_UNTRACKED` and
         `sbi->used_clusters > 0`, `sbi->used_clusters` is decremented by 1.
  - `exfat_free_cluster` is NOT called (it would re-acquire `bitmap_lock`
    and deadlock; see Invariant `exfat-alloc-new-dir-rollback-on-zero-fail`).
  - After rollback the bitmap and FAT are in the same state as before the
    call (net effect: no cluster consumed).
  - `clu_out->dir   == EXFAT_EOF_CLUSTER`.
  - `clu_out->size  == 0`.
  - `clu_out->flags == ALLOC_NO_FAT_CHAIN`.
  - Returns the negative errno forwarded from `exfat_zeroed_cluster`.

**Invariant** (id=exfat-alloc-new-dir-rollback-on-zero-fail):
  `alloc_new_dir` is the ONLY
rollback-bearing step inside `add_entry` — if `alloc_cluster` succeeded but
`zeroed_cluster` failed, MUST call `clear_bitmap` + `ent_set(FREE)` to
release the cluster. This is an intentional exception to `add_entry`'s
no-rollback policy: alloc-then-zero is conceptually one atomic action; other
failure paths do NOT roll back. The rollback MUST be done inline via
`clear_bitmap` + `ent_set` — calling `exfat_free_cluster` is forbidden here
because it re-acquires `bitmap_lock`, causing a self-deadlock.

**exfat-alloc-new-dir-no-fat-chain-on-exit**: On every exit path (success
or failure) `clu_out->flags` is `ALLOC_NO_FAT_CHAIN`. On success this
reflects the semantic that a one-cluster directory is fully described by
its start cluster; the FAT entry at `clu_out->dir` is not consulted during
traversal.

**System Algorithm**:

Phase 1 — Initialise chain and allocate.
  Set `clu_out = {EXFAT_EOF_CLUSTER, 0, ALLOC_FAT_CHAIN}`.
  (`exfat_alloc_cluster` requires `ALLOC_FAT_CHAIN` on entry.)
  Call `exfat_alloc_cluster(sbi, 1, clu_out)`.
  On failure: reset flags to `ALLOC_NO_FAT_CHAIN`, return error (Case 2).

Phase 2 — Zero the cluster.
  Capture `allocated_clu = clu_out->dir`.
  Call `exfat_zeroed_cluster(sbi, allocated_clu)`.
  On failure: perform rollback sequence (Case 3) and return error.

Phase 3 — Promote chain flags.
  Set `clu_out->flags = ALLOC_NO_FAT_CHAIN`.
  Return 0 (Case 1).
