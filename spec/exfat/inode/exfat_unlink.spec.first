[PROMPT]
Provide complete `exfat_inode.c` addition that implements `VfsExfatUnlink`.
Only include `<exfat.h>` as the header; output a single C code block with
no other code. The block is appended to `fs/exfat/exfat_inode.c` (unlink
is part of the inode-management TU together with lookup / mkdir / create /
open_close / getattr / seek), NOT written as a standalone file.

VfsExfatUnlink is the VnodeOps.Unlink callback for exFAT on LiteOS-A. It
removes a regular file's directory entry from the parent directory and
frees the file's cluster chain. It does NOT free the target Vnode itself —
VFS Reclaim (VfsExfatReclaim) handles that once the refcount drops to 0.

Domain knowledge:
- Linux exFAT defers cluster freeing to VFS truncate-on-iput; LiteOS-A
  has no equivalent eviction hook, so unlink frees clusters synchronously.
- An exFAT FILE dentry is followed by exactly one Stream dentry plus 1..17
  Name dentries (per EXFAT_DENTRY_SET_MAX = 19). exfat_get_dentry_set
  retrieves the whole set; exfat_validate_dentry_set checks the shape.
- Marking a dentry "deleted" on disk = clearing the top bit of the type
  byte: `type &= 0x7F`. Volume-dirty flag MUST bracket the disk mutation.
- Tombstone in memory: setting target_ei->dir.dir = DIR_DELETED prevents
  a racing reader from re-walking a stale chain to recover the file.
- This stage is for regular files only. Directories are removed by
  VfsExfatRmdir (separate stage; not in this spec's scope).

This spec includes a `Refine Prompt` section because unlink mutates two
on-disk regions (parent dentry set, allocation bitmap) under different
locks (s_lock, bitmap_lock) and must avoid the concurrent-lookup-after-
removal race.

## First Prompt

[RELY]
```c
typedef struct exfat_sb_info    exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;
typedef struct exfat_chain      exfat_chain;
struct Vnode;
struct exfat_dentry;

/* Tombstone marker for target_ei->dir.dir after a successful unlink.
 * Defined in <exfat.h>. */
#define DIR_DELETED       0xFFFFFFF7u

/* Read the dentry-set (FILE + Stream + 1..17 Name) starting at start_entry.
 * Fills `set[0..*num_entries-1]`. Returns 0 / -EIO. */
extern int exfat_get_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir, int start_entry,
                                struct exfat_dentry *set, int max_entries,
                                int *num_entries);

/* Validate that set[0] is a FILE dentry, set[1] is a Stream, set[2..] are
 * Name dentries, and chksum16 over the byte stream matches. Returns
 * 0 / -EIO. */
extern int exfat_validate_dentry_set(const struct exfat_dentry *set,
                                     int num_entries);

/* Write back num_entries dentries starting at start_entry. Returns 0 / -EIO. */
extern int exfat_set_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir, int start_entry,
                                const struct exfat_dentry *set,
                                int num_entries);

/* Volume-dirty bracket. Set before any on-disk mutation; clear after. */
extern int exfat_set_volume_dirty(exfat_sb_info *sbi);
extern int exfat_clear_volume_dirty(exfat_sb_info *sbi);

/* Free every cluster reachable from p_chain by clearing its bitmap bit.
 * Uses bitmap_lock internally; callers must NOT hold s_lock when invoking
 * (s_lock release ordering is enforced by the locking phase contract). */
extern int exfat_free_cluster(exfat_sb_info *sbi, const exfat_chain *p_chain);
```

[GUARANTEE]
```c
/* Calling convention: returns 0 on success or a negative POSIX errno.
 * Side effects on success:
 *   - Parent's on-disk dentry set for the target is marked deleted
 *     (each dentry's type-byte top bit cleared).
 *   - target_vp's cluster chain is released to the allocation bitmap.
 *   - target_ei->dir.dir = DIR_DELETED in memory (tombstone).
 *   - Does NOT VnodeFree target_vp or exfat_inode_free target_ei —
 *     VFS Reclaim handles that.
 * (Lock-state convention belongs to Phase 2.) */
int VfsExfatUnlink(struct Vnode *parent_vp, struct Vnode *target_vp,
                   const char *fileName);
```

[SPECIFICATION]

**Pre-Condition**:
  - `parent_vp` is a valid Vnode whose `data` points to an initialised
    `exfat_inode_info` with `type == TYPE_DIR`.
  - `target_vp` is a valid Vnode whose `data` points to an initialised
    `exfat_inode_info` referencing the file being unlinked.
  - `target_ei->type == TYPE_FILE` (regular file; rmdir is separate).
  - `target_ei->entry >= 0` and `target_ei->dir.dir != DIR_DELETED`
    (file has a live on-disk dentry).
  - `fileName` is the leaf name string (used for log/diagnostics only;
    on-disk identity is `target_ei->dir + target_ei->entry`).

**Post-Condition**:

**Case 1 (success)**:
  - The parent dentry-set covering `target_ei->entry` (FILE + Stream +
    1..N Name dentries) is rewritten with each `type` byte's top bit
    cleared on disk.
  - `target_ei->dir.dir == DIR_DELETED` (in-memory tombstone).
  - All clusters previously reachable from `target_ei->start_clu` (per
    `target_ei->flags`) are returned to the allocation bitmap.
  - Returns 0.

**Case 2 (argument or precondition failure)**:
  - `parent_vp == NULL || target_vp == NULL || fileName == NULL`, OR
    `target_ei->type != TYPE_FILE`, OR `target_ei->dir.dir == DIR_DELETED`.
  - On-disk state untouched; cluster bitmap untouched.
  - Returns -EINVAL on null/invalid args or non-file target; -ENOENT
    on already-tombstoned target.

**Case 3 (dentry-set fetch / validate failure)**:
  - `exfat_get_dentry_set` or `exfat_validate_dentry_set` returned a
    negative errno.
  - `exfat_set_volume_dirty` was called; `exfat_clear_volume_dirty` is
    called before unlock to close the bracket.
  - target_ei is NOT tombstoned; clusters are NOT freed.
  - Returns -EIO.

**Case 4 (dentry-set write-back failure)**:
  - `exfat_set_dentry_set` returned -EIO. The dentry set may be
    partially rewritten on disk; the volume-dirty bracket is closed;
    target_ei is NOT tombstoned; clusters are NOT freed. Next mount's
    fsck sees VOLUME_DIRTY and may flag the file.
  - Returns -EIO.

**Case 5 (cluster-free post-tombstone failure)**:
  - Dentry set rewrite + tombstone succeeded, BUT `exfat_free_cluster`
    returned a negative errno (e.g., FAT-chain read error). The dentry
    removal IS persisted; the file is unreachable from the namespace.
    Bitmap may carry orphaned clusters until fsck.
  - Returns the negative errno from `exfat_free_cluster`.

**Invariant** (id=exfat-unlink-dentry-type-top-bit-cleared):
  After a successful VfsExfatUnlink, every dentry in the
  [target_ei->entry .. target_ei->entry + num_entries) range on disk has
  its `type` byte's top bit cleared. exFAT readers MUST treat such
  dentries as deleted.

**Invariant** (id=exfat-unlink-tombstone-prevents-reuse):
  Setting `target_ei->dir.dir = DIR_DELETED` ensures any concurrent
  reader that retrieved `target_vp` before unlink completed cannot
  re-read the same on-disk slot to recover the file. The tombstone is
  in-memory only; the on-disk truth is the cleared dentry type bytes.

**Invariant** (id=exfat-unlink-vol-dirty-bracketed):
  `exfat_set_volume_dirty` MUST be called immediately before the
  dentry-set write-back; `exfat_clear_volume_dirty` MUST be called
  regardless of whether write-back succeeded. fsck uses the persisted
  VOLUME_DIRTY bit to detect partially-applied unlink.

**Invariant** (id=exfat-unlink-free-after-tombstone):
  Cluster freeing happens AFTER the dentry-set write-back AND AFTER the
  tombstone is set. If freeing crashes mid-way, the dentry is already
  marked deleted, so a subsequent mount will not see the file. The
  bitmap may then carry orphaned clusters until fsck.

**Invariant** (id=exfat-unlink-no-vnode-free):
  VfsExfatUnlink MUST NOT call `VnodeFree` or `exfat_inode_free` on
  `target_vp` / `target_ei`. VFS retains `target_vp` until its refcount
  drops to 0, at which point `VfsExfatReclaim` runs and frees
  `target_ei` via `exfat_inode_free`.

**Invariant** (id=exfat-unlink-not-for-directories):
  This stage handles regular files only. Directories are removed by
  `VfsExfatRmdir` (separate stage). Pre-Condition mandates
  `target_ei->type == TYPE_FILE`; violating it returns -EINVAL.

**System Algorithm**:
1. Validate args; reject NULL pointers, directories, and already-tombstoned
   targets.
2. Phase 1 (dentry-set mutation): under `sbi->s_lock`, set volume-dirty,
   read the dentry set, validate shape, flip the top bit of each
   `type` byte, write back, tombstone `target_ei->dir.dir = DIR_DELETED`,
   clear volume-dirty.
3. Phase 2 (cluster release): outside `sbi->s_lock`, build a chain
   `{dir = target_ei->start_clu, size = 0, flags = target_ei->flags}`
   and call `exfat_free_cluster` (which takes `bitmap_lock` internally).
4. Return 0 on success; surface phase-specific errno per Cases 3/4/5.

## Refine Prompt

[RELY]
```c
extern int LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern int LOS_MuxUnlock(LosMux *mutex);
#define LOS_WAIT_FOREVER 0xFFFFFFFFu
```

[SPECIFICATION of VfsExfatUnlink]
**Pre-Condition (lock state)**: No locks held on entry.

**Post-Condition (lock state)**:
  - Case 1 (success): `sbi->s_lock` released; `bitmap_lock` not held;
    returns 0.
  - Case 2 (validation): no lock acquired; returns -EINVAL / -ENOENT.
  - Case 3 / Case 4 (Phase-1 failure): `sbi->s_lock` released after
    `exfat_clear_volume_dirty`; returns -EIO.
  - Case 5 (Phase-2 failure): `sbi->s_lock` already released at end
    of Phase 1; returns the errno from `exfat_free_cluster`.

**System Algorithm (locking phases)**:

**Phase 1: Dentry-set mutation (under s_lock)**
  - Goal: atomically clear the dentry-set type-byte top bits and
    tombstone target_ei in memory.
  - Algorithm:
    1. `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)`.
    2. `exfat_set_volume_dirty(sbi)`.
    3. `exfat_get_dentry_set(sbi, &target_ei->dir, target_ei->entry,
       set, EXFAT_DENTRY_SET_MAX, &num_entries)`.
    4. `exfat_validate_dentry_set(set, num_entries)` — verify FILE +
       Stream + Name shape and chksum.
    5. For `i` in `[0 .. num_entries-1]`: `set[i].type &= 0x7Fu`.
    6. `exfat_set_dentry_set(sbi, &target_ei->dir, target_ei->entry,
       set, num_entries)` — write back.
    7. On success of step 6: `target_ei->dir.dir = DIR_DELETED`.
    8. `exfat_clear_volume_dirty(sbi)` — ALWAYS, even on step 3/4/6
       failure (closes the volume-dirty bracket).
    9. `LOS_MuxUnlock(&sbi->s_lock)`.
  - Post-condition: `sbi->s_lock` not held; on success the on-disk
    dentry-set is marked deleted and `target_ei` carries the in-memory
    tombstone.
  - Error Handling: any of step 3 / 4 / 6 failures still execute
    steps 8 / 9, then return the captured errno.

**Phase 2: Cluster release (lock-free with respect to s_lock)**
  - Goal: free the bitmap bits for target's cluster chain.
  - Algorithm:
    1. Compose `chain = {.dir = target_ei->start_clu, .size = 0,
       .flags = target_ei->flags}`.
    2. `exfat_free_cluster(sbi, &chain)` — internally takes
       `bitmap_lock` per cluster.
  - Post-condition: chain's bitmap bits cleared; `bitmap_lock` not held.
  - Error Handling: surface the errno as Case 5; the dentry removal
    from Phase 1 is already persisted, so the file is gone from the
    namespace even on this failure.

**Deadlock note**:
VfsExfatUnlink takes `sbi->s_lock` first, then later `bitmap_lock`
(transitively via `exfat_free_cluster`). The two locks are acquired in
disjoint critical sections — `s_lock` is released before `bitmap_lock`
is taken. No sibling functions hold this pair simultaneously, so there
is no AA / AB deadlock potential.
