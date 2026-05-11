[PROMPT]
Provide complete `exfat_inode.c` addition that implements `VfsExfatRename`.
Only include `<exfat.h>` as the header; output a single C code block with
no other code. The block is appended to `fs/exfat/exfat_inode.c` (rename
joins lookup / create / mkdir / unlink / rmdir / getattr / seek in the
inode-management TU).

VfsExfatRename is the VnodeOps.Rename callback for the exFAT filesystem on
LiteOS-A. It supports the full Linux exfat_rename surface in v1: same-
directory rename, cross-directory move, and overwrite of an existing dst.

Domain knowledge:
- Linux fs/exfat/namei.c::exfat_rename (line 1329) is the canonical
  reference. Internal helper exfat_rename_file (line 1003) re-emits the
  primary File dentry verbatim at a new slot and then writes the new
  Stream + N×Name dentries using the new uniname. The old dentry-set is
  tombstoned (top-bit clear of every type byte) only AFTER the new set
  has been written successfully — this is the atomicity story.
- v1 strategy mirrors Linux but uses only already-approved helpers
  (exfat_alloc_dentry_slot + exfat_init_dir_entry + exfat_init_ext_entry
  + exfat_set_dentry_set), since there is NO buffer cache in LiteOS-A and
  every set_dentry call already does a full read-modify-write sector pass.
- Cluster reclamation policy for overwrite: when dst exists and is a
  regular file, the dst's data clusters MUST be released by VfsExfatRename
  (calling exfat_free_cluster) before the move completes — same eager
  reclaim philosophy as VfsExfatUnlink, recorded as invariant
  `exfat-rename-overwrite-clusters-released-eagerly`. When dst is an
  EMPTY directory, the dst's single data cluster is released — same
  policy as VfsExfatRmdir.
- Overwrite refusal: dst MUST be empty when dst is a directory. If dst is
  a non-empty directory → -ENOTEMPTY. If src and dst types disagree
  (src is dir, dst is file, or vice versa) → -EISDIR / -ENOTDIR per
  POSIX rename(2).
- The "rename onto itself" no-op case (src == dst by ino) returns 0
  without any disk mutation. POSIX requires success without action.
- Cross-directory move: when src->parent and dstParent differ, the
  *source* parent's num_subdirs is decremented and the *destination*
  parent's num_subdirs is incremented (only when src is a directory,
  matching mkdir's bookkeeping). Same-directory rename does not touch
  either parent's num_subdirs.
- VnodeOps.Rename signature differs from Linux: the LiteOS slot is
  `int (*Rename)(struct Vnode *src, struct Vnode *dstParent,
                 const char *srcName, const char *dstName)` — note that
  the *source parent* is reachable via `src->parent` (NOT a separate
  parameter), and the *target* (if any) is NOT pre-resolved by the VFS
  layer. VfsExfatRename therefore performs an internal dst lookup before
  the mutation phase.
- Internal helper exfat_rename_resolve_target performs the dst-name
  resolution within the dstParent's dentry list to determine whether
  the destination already exists (and if so, its dentry index + dentry
  set + on-disk inode info). This helper is INTERNAL to VfsExfatRename
  in v1 and does NOT export to common.header — it relies on
  exfat_get_dentry_set + exfat_uniname_cmp for the per-entry comparison.
- Lock discipline: single sbi->s_lock held for the entire on-disk
  mutation phase, mirroring unlink+rmdir Phase 1. Cluster release
  (Phase 2) runs lock-free to avoid s_lock → bitmap_lock partial
  ordering, per the free_cluster cross-stage invariant.
- VFS-side vnode handle for the SOURCE (src) stays valid across the
  call; rename only mutates src->data->{dir, entry, attr} to reflect
  the new location. Path-cache eviction / re-hashing the source vnode
  by its new path is OUT-OF-SCOPE — owned by the VFS layer's Rename
  post-processing.

## First Prompt

[RELY]
```c
/* Types from common.header (reproduced for clarity; do not redeclare). */
typedef struct exfat_sb_info    exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;
typedef struct exfat_chain      exfat_chain;
typedef struct exfat_dentry     exfat_dentry;
struct Vnode;

#define DIR_DELETED              0xFFFFFFF7u
#define EXFAT_EOF_CLUSTER        0xFFFFFFFFu
#define EXFAT_FIRST_CLUSTER      2u
#define EXFAT_UNUSED             0x00u
#define EXFAT_FILE               0x85u    /* in-use primary FILE/DIR dentry */
#define EXFAT_DENTRY_SET_MAX     19
#define EXFAT_MIN_SUBDIR         2u
#define ATTR_SUBDIR              0x0010u
#define ATTR_ARCHIVE             0x0020u

/* dentry-set IO. */
extern int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                            int entry_idx, struct exfat_dentry *out,
                            uint64_t *out_sector);
extern int exfat_get_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir, int start_entry,
                                struct exfat_dentry *set, int max_entries,
                                int *num_entries);
extern int exfat_validate_dentry_set(const struct exfat_dentry *set,
                                     int num_entries);
extern int exfat_set_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir, int start_entry,
                                const struct exfat_dentry *set, int num_entries);

/* dentry-slot allocation in target dir. */
extern int exfat_alloc_dentry_slot(const exfat_sb_info *sbi,
                                   const exfat_chain *dir,
                                   int n_entries, int *slot_idx_out);

/* uniname machinery (used to compute new name's hash + length + dentry count). */
extern int exfat_utf8_to_uni(const char *utf8, int utf8_len,
                             uint16_t *uni, int uni_max, int *uni_len);
extern int exfat_uniname_cmp(const exfat_sb_info *sbi,
                             const uint16_t *a, int a_len,
                             const uint16_t *b, int b_len);
extern int exfat_calc_num_entries(const struct exfat_uni_name *p_uniname);

/* dentry initialisers (write to disk via set_dentry under the hood). */
extern int exfat_init_dir_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                                int entry, uint32_t type, uint32_t start_clu,
                                uint64_t size);
extern int exfat_init_ext_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                                int entry, int num_entries,
                                const struct exfat_uni_name *p_uniname);

/* uniname hash helper (CS_DEFAULT chksum16 over raw UTF-16 bytes). */
extern uint16_t exfat_calc_chksum16(const void *data, int len,
                                    uint16_t chksum, int type);

/* vol_dirty bracket. */
extern int exfat_set_volume_dirty(exfat_sb_info *sbi);
extern int exfat_clear_volume_dirty(exfat_sb_info *sbi);

/* Cluster release for overwrite. Case 4 footgun: size==0 short-circuits
 * to a no-op. */
extern int exfat_free_cluster(exfat_sb_info *sbi, const exfat_chain *p_chain);
extern int exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                                  uint32_t *next_clu);
```

[GUARANTEE]
```c
/* Calling convention: caller holds no exFAT lock. VfsExfatRename acquires
 * sbi->s_lock for the entire mutation phase. Cluster reclamation (when
 * overwriting an existing dst) runs lock-free w.r.t. s_lock to avoid the
 * s_lock → bitmap_lock partial ordering — same discipline as unlink/rmdir.
 *
 * Returns 0 on success or negative POSIX errno.
 *
 * Side effects on success:
 *   - src is reachable via dstParent + dstName on the next lookup. The
 *     old (srcParent, srcName) entry is tombstoned.
 *   - When src is a directory and srcParent != dstParent: srcParent->data
 *     num_subdirs is decremented and dstParent->data num_subdirs is
 *     incremented (in memory only).
 *   - When dst pre-existed: dst's dentry-set is tombstoned and its data
 *     clusters released. (Linux defers cluster reclaim to fsck; v1 does
 *     not have fsck, hence eager reclaim.)
 *   - src->data->{dir, entry} are updated to point at the new dentry-set
 *     location. src->data->dir.dir == DIR_DELETED is NEVER set on the
 *     source on a successful rename (only on dst-overwrite).
 *
 * The VFS layer is responsible for rehashing src under its new path and
 * for evicting cached path entries for the old (srcParent, srcName) and
 * the displaced dst (when overwrite occurred). */
int VfsExfatRename(struct Vnode *src, struct Vnode *dstParent,
                   const char *srcName, const char *dstName);
```

[SPECIFICATION]
**Pre-Condition**:
  - `src` is a valid Vnode whose `data` points to an initialised
    `exfat_inode_info` with `dir.dir != DIR_DELETED` and `entry >= 0`.
    `src->parent` is the source parent Vnode (NOT NULL); its `data`
    points to an initialised parent `exfat_inode_info` with
    `type == TYPE_DIR`.
  - `dstParent` is a valid Vnode whose `originMount->data` points to
    the same `exfat_sb_info` as `src->parent->originMount->data` (no
    cross-filesystem rename in v1). `dstParent->data` points to an
    initialised `exfat_inode_info` with `type == TYPE_DIR` and
    `dir.dir != DIR_DELETED`.
  - `srcName` is non-NULL but used only for diagnostics in v1 (src is
    already resolved). `dstName` is a non-NULL UTF-8 NUL-terminated
    string of length 1..255 bytes.
  - No thread holds `sbi->s_lock` on entry.

**Post-Condition**:

**Case 1 (success — same parent, no overwrite)**:
  - `src->parent == dstParent` and dstName resolves to NO existing
    dentry in dstParent.
  - A fresh dentry-set with the new uniname is written to a new slot in
    dstParent at index `new_slot`.
  - The old dentry-set (at src->data->dir + src->data->entry) is
    tombstoned (every type byte top-bit cleared).
  - `src->data->dir` unchanged (same parent chain); `src->data->entry =
    new_slot`; `src->data->i_pos = ((uint64_t)src->data->start_clu << 32) |
    new_slot` (when start_clu != EOF; for empty files i_pos = new_slot).
  - Neither parent's num_subdirs changes (same-dir rename).
  - The volume-dirty bracket was opened and closed.
  - `sbi->s_lock` is released before return.
  - Returns 0.

**Case 2 (success — cross-dir move, no overwrite)**:
  - `src->parent != dstParent`. dstName resolves to NO existing dentry in
    dstParent.
  - A fresh dentry-set with the new uniname is written to a new slot in
    dstParent.
  - The old dentry-set in srcParent is tombstoned.
  - `src->data->dir = dstParent_ei->chain_describing_dstParent` (rebuilt
    via existing chain helpers); `src->data->entry = new_slot`;
    `src->data->i_pos` recomputed.
  - When src is TYPE_DIR: srcParent_ei->num_subdirs decremented by 1;
    dstParent_ei->num_subdirs incremented by 1 (both in memory only).
    When src is TYPE_FILE: neither parent's num_subdirs is touched.
  - Returns 0.

**Case 3 (success — overwrite of regular dst, any parent)**:
  - dstName resolves to an existing TYPE_FILE dentry-set in dstParent.
  - dst's dentry-set is tombstoned (its in-memory analogue does NOT exist
    in v1: VFS may or may not have a vnode for it — VfsExfatRename
    treats the dst as a "disk-only" object).
  - dst's data clusters (if any; start_clu != EXFAT_EOF_CLUSTER) are
    released via exfat_free_cluster.
  - A fresh dentry-set with the new uniname is written. v1 chooses to
    write the new set at a *fresh* slot allocated by alloc_dentry_slot
    rather than overlaying the freed slot — this avoids race windows
    where a concurrent reader sees a half-overwritten set. The dst's
    just-freed slot transitions cleanly to "deleted" state.
  - src's old dentry-set is tombstoned. src->data->{dir, entry}
    updated as Case 1/2.
  - Cross-dir num_subdirs accounting per Case 1/2.
  - Returns 0.

**Case 4 (success — overwrite of empty dir dst, any parent)**:
  - dstName resolves to an existing TYPE_DIR dentry-set in dstParent
    whose data cluster is empty (no in-use 0x85 primary inside).
  - dst's dentry-set tombstoned + dst's single allocated cluster
    released (same as rmdir Phase 2).
  - When src is TYPE_DIR: net num_subdirs change for dstParent is 0
    (-1 for dst removal, +1 for src arrival). srcParent loses 1
    (when srcParent != dstParent).
  - Returns 0.

**Case 5 (no-op — src is dst by ino)**:
  - dstName resolves to an existing dentry-set whose primary's
    start_clu+entry equals src->data->{start_clu, entry} (rename onto
    itself).
  - NO disk mutation; vol_dirty bracket NOT opened.
  - `sbi->s_lock` is acquired and released to serialize against
    concurrent renamers but no state changes.
  - Returns 0.

**Case 6 (validation failure — pre-lock)**:
  - One of `src`, `dstParent`, `srcName`, `dstName` is NULL, OR
    `src->originMount` / `src->originMount->data` /
    `src->parent` / `src->parent->data` / `src->data` /
    `dstParent->originMount` / `dstParent->originMount->data` /
    `dstParent->data` is NULL, OR
    `src->originMount->data != dstParent->originMount->data`
    (cross-filesystem rename, refused), OR
    parent or dstParent ei->type != TYPE_DIR, OR
    src ei->dir.dir == DIR_DELETED, OR src ei->entry < 0, OR
    `dstName[0] == '\0'`, OR `strlen(dstName) > 255 * 4` (UTF-8
    coarse upper bound; precise check via exfat_utf8_to_uni in
    Phase 1).
  - No locks acquired; no I/O.
  - Returns -EINVAL.

**Case 7 (dstName too long after UTF-8 → UTF-16 conversion)**:
  - exfat_utf8_to_uni returned -ENAMETOOLONG (uni_len > 255).
  - sbi->s_lock was acquired then released (Phase 1 entered).
  - vol_dirty bracket opened then closed.
  - Returns -ENAMETOOLONG.

**Case 8 (dst type mismatch with src)**:
  - dstName resolves to existing dentry-set whose primary has
    `(attr & ATTR_SUBDIR) != (src_ei->attr & ATTR_SUBDIR)`.
    POSIX rename(2): cannot rename a file ONTO a directory or vice versa.
  - sbi->s_lock released. No on-disk mutation.
  - Returns -EISDIR (when src is file, dst is dir) or -ENOTDIR (when
    src is dir, dst is file).

**Case 9 (dst is non-empty directory)**:
  - dstName resolves to existing TYPE_DIR dentry-set; emptiness scan
    finds at least one in-use 0x85 primary inside dst's data cluster.
  - sbi->s_lock released. No on-disk mutation.
  - Returns -ENOTEMPTY.

**Case 10 (alloc_dentry_slot failure)**:
  - dstParent has no contiguous free run >= `num_new_entries` for the
    new uniname. exfat_alloc_dentry_slot returned -ENOSPC (v1 does not
    auto-extend dstParent's cluster chain — same policy as mkdir/create).
  - sbi->s_lock released. No on-disk mutation. (When this happens
    after a Case-3/4 overwrite tombstoned dst, the dst tombstone is NOT
    rolled back; PRINT_ERR records the partial state. Future Stage
    'rename-rollback' may add rollback; v1 accepts orphan-on-disk for
    simplicity — sibling of unlink Case 5 / rmdir Case 6 policy.)
  - Returns -ENOSPC.

**Case 11 (write-back failure — new set on dst side)**:
  - exfat_init_dir_entry / exfat_init_ext_entry / exfat_set_dentry_set
    returned -EIO while writing the new dentry-set at the dst slot.
  - sbi->s_lock released. PRINT_ERR. dst-side may have partial disk
    state. The src's old dentry-set was NOT tombstoned (src is still
    reachable). VOL_DIRTY bit persists across this failure to flag the
    inconsistency for fsck (when one exists).
  - Returns -EIO.

**Case 12 (tombstone failure on src after new set is in place)**:
  - New dst-side set was successfully written. Old src-side tombstone
    write (set_dentry_set with top-bit-cleared types) returned -EIO.
  - At this point disk has TWO copies of the same logical entry. PRINT_ERR.
    src->data is NOT updated (caller sees rename as "failed" but new dst
    is visible to anyone walking dstParent). Eventual reconciliation
    requires fsck. v1 accepts this as known limitation, recorded as
    invariant `exfat-rename-no-rollback-after-dst-written`.
  - Returns -EIO.

**Invariant** (id=exfat-rename-new-set-before-tombstone):
  The new dentry-set at dstParent + new_slot MUST be FULLY written to
  disk (init_dir_entry succeeded AND init_ext_entry succeeded AND
  set_dentry_set succeeded) BEFORE the old src dentry-set is tombstoned.
  This is the atomicity invariant — without it, a crash mid-rename could
  leave src unreachable AND new dst not yet present, losing the entry
  entirely. Mirrors Linux exfat_rename_file's "write new before clear
  old" ordering.

**Invariant** (id=exfat-rename-overwrite-clusters-released-eagerly):
  When Case 3 (file overwrite) or Case 4 (empty-dir overwrite) applies,
  the dst's data cluster chain is released via exfat_free_cluster as
  part of the rename operation. Linux defers reclaim to fsck/eviction;
  v1 reclaims eagerly because LiteOS-A has no fsck. Sibling invariant of
  `exfat-unlink-clusters-released-eagerly` and `exfat-rmdir-cluster-
  released-eagerly`.

**Invariant** (id=exfat-rename-subdir-accounting-cross-dir):
  When src->parent != dstParent AND src->data->type == TYPE_DIR (i.e. a
  cross-directory move of a directory), srcParent_ei->num_subdirs is
  decremented by 1 AND dstParent_ei->num_subdirs is incremented by 1
  (both in memory only). When the move is same-parent OR src is a file,
  no parent num_subdirs is touched. Overwrite of an existing TYPE_DIR dst
  cancels out the +1 (Case 4 net change is 0 for dstParent when src
  is also dir). Overwrite of a file by a file does not touch num_subdirs.
  This accounting matches Linux exfat_rename + dec/inc_nlink semantics.

**Invariant** (id=exfat-rename-no-rollback-after-dst-written):
  Case 12 (tombstone of src failed after dst-side new set was written
  successfully) does NOT roll back the dst-side write. This is an
  intentional v1 limitation matching the same-philosophy in unlink Case 5
  / rmdir Case 6 / mkdir Case 3 — disk-state partial commit policy is
  "PRINT_ERR + return errno; leave partial on disk; fsck (future) will
  reconcile". Rollback semantics are deferred to a future stage.

**Invariant** (id=exfat-rename-self-noop-no-disk-touch):
  Case 5 (rename onto itself by ino — src's (start_clu, entry) matches
  the dst-resolved dentry) returns 0 WITHOUT calling
  exfat_set_volume_dirty, exfat_alloc_dentry_slot, exfat_init_*,
  exfat_set_dentry_set, or any cluster mutator. sbi->s_lock is still
  acquired/released to serialize against concurrent renames.

**Invariant** (id=exfat-rename-cross-fs-refused):
  When `src->originMount->data != dstParent->originMount->data`, return
  -EINVAL without acquiring any lock or performing any I/O. v1 does not
  support cross-filesystem rename (matches Linux EXDEV semantics modulo
  the errno choice — POSIX recommends -EXDEV but LiteOS-A FAT/exFAT
  callers consistently return -EINVAL for this; we match the local
  convention).

**Invariant** (id=exfat-rename-archive-bit-set-on-file):
  When src is TYPE_FILE, the freshly written File primary at the new
  slot has ATTR_ARCHIVE bit set (regardless of whether it was set in
  the old dentry). This matches Linux exfat_rename_file line 1027.
  Reason: a successful rename of a file marks the file as "modified"
  for backup tools that observe ATTR_ARCHIVE.

**Invariant** (id=exfat-rename-s-lock-bracketed):
  All Phase 1 paths (Case 1/2/3/4 success, Case 5 self-noop, Case 7-12
  failures) release sbi->s_lock via a single `phase1_unlock:` label
  that also closes the vol_dirty bracket (when it was opened — Case 5
  / Case 6 skip the bracket open entirely). Phase 2 (overwrite cluster
  release for Case 3/4) runs lock-free w.r.t. s_lock, mirroring unlink
  Phase 2 and rmdir Phase 2.

**System Algorithm**:

1. **Pre-Condition validation (lock-free)**. On failure return -EINVAL
   (Case 6) or -ENAMETOOLONG (Case 7's pre-utf8 length cap).

2. **Convert dstName to uniname**. Allocate stack uni buffer (256 uint16);
   call exfat_utf8_to_uni. On -EINVAL / -ENAMETOOLONG return that errno
   (Case 7).

3. **Compute hash + num_new_entries**. Hash via exfat_calc_chksum16 over
   raw UTF-16 bytes with type CS_DEFAULT. num_new_entries via
   exfat_calc_num_entries (returns 3 + ceil((uni_len-15)/15) for
   uni_len > 15; otherwise 3 for the File + Stream + 1 Name).

4. **Acquire sbi->s_lock**. Call exfat_set_volume_dirty.

5. **Resolve dst** by scanning dstParent's dentry chain for a primary
   matching the new uniname. Internal helper performs:
   for each entry index in dstParent (bounded by dstParent->size *
   dentries_per_clu):
     - exfat_get_dentry(sbi, &dstParent_chain, idx, &one, NULL).
     - If one.type == EXFAT_UNUSED: break (terminator).
     - If one.type != EXFAT_FILE: continue (skip deleted / non-primary).
     - exfat_get_dentry_set + exfat_validate_dentry_set + extract uniname
       via reading Name dentries; compare with new uniname via
       exfat_uniname_cmp (case-insensitive per exFAT semantics).
     - If matched: record dst_entry_idx, dst_num_entries, dst_set[],
       dst_primary_attr, dst_primary_start_clu, dst_primary_size,
       dst_primary_flags. Break with `dst_found = true`.

6. **Self-noop check**: if dst_found AND (dst_primary_start_clu ==
   src_ei->start_clu AND dst_entry_idx == src_ei->entry AND
   dst_dir_chain == src_ei->dir), return 0 with bracket close +
   unlock (Case 5). NOTE: self-noop is detected by (start_clu, entry)
   identity, not by string comparison — Linux uses the same test.

7. **Dst type-mismatch check**: if dst_found, compare
   `(dst_primary_attr & ATTR_SUBDIR)` with
   `(src_ei->attr & ATTR_SUBDIR)`. On mismatch, set err = -EISDIR or
   -ENOTDIR and goto phase1_unlock (Case 8).

8. **Dst non-empty-dir check**: if dst_found AND dst is DIR, walk dst's
   data cluster (similar to rmdir's emptiness walk). If any in-use 0x85
   primary inside, set err = -ENOTEMPTY and goto phase1_unlock (Case 9).

9. **If dst_found: tombstone dst + free dst clusters**.
     - In dst_set[i].type &= 0x7Fu for i in [0, dst_num_entries).
     - exfat_set_dentry_set(sbi, &dstParent_chain, dst_entry_idx,
       dst_set, dst_num_entries). On failure goto phase1_unlock (Case 11
       variant — partial dst tombstone may exist).
     - Defer dst cluster release to Phase 2 (after lock is released).

10. **Allocate new slot for src in dstParent**.
    exfat_alloc_dentry_slot(sbi, &dstParent_chain, num_new_entries,
    &new_slot). On -ENOSPC goto phase1_unlock (Case 10).

11. **Write new dentry-set**. Build new struct exfat_uni_name from
    converted uni buffer + hash + uni_len. Use exfat_init_dir_entry to
    write the File primary at new_slot (using src_ei->start_clu, size,
    and `attr | ATTR_ARCHIVE` per invariant exfat-rename-archive-bit-
    set-on-file when src is TYPE_FILE). Then exfat_init_ext_entry to
    write the Stream + N×Name dentries at new_slot + 1 .. + num_new_entries
    - 1. Either failure: goto phase1_unlock (Case 11).

12. **Tombstone old src dentry-set**.
    exfat_get_dentry_set(sbi, &src_ei->dir, src_ei->entry, old_set,
    EXFAT_DENTRY_SET_MAX, &num_old). On failure (rare — src was just
    valid): goto phase1_unlock (Case 12).
    exfat_validate_dentry_set(old_set, num_old). On failure: same.
    Clear top bit of every type in old_set.
    exfat_set_dentry_set(sbi, &src_ei->dir, src_ei->entry, old_set,
    num_old). On failure: PRINT_ERR + set err = -EIO and goto
    phase1_unlock (Case 12).

13. **Update src->data in memory**:
    - src_ei->entry = new_slot.
    - src_ei->dir = dstParent_chain (when cross-dir; same chain
      otherwise).
    - src_ei->i_pos = ((uint64_t)src_ei->start_clu << 32) | new_slot.
    - When src_ei->type == TYPE_FILE: src_ei->attr |= ATTR_ARCHIVE.
    - Cross-dir directory move accounting: when src_ei->type == TYPE_DIR
      AND src->parent != dstParent:
        src->parent_ei->num_subdirs--;
        dstParent_ei->num_subdirs++;
      When overwrite of TYPE_DIR dst happened (Case 4) AND src is also
      TYPE_DIR: dstParent_ei->num_subdirs does NOT increment further
      (the -1 from dst removal + the +1 from src arrival cancel; in
      same-parent overwrite the net is 0; in cross-dir overwrite the
      src parent loses 1 and dst parent stays the same). The
      implementation captures this as: increment dstParent ONLY when
      src is dir AND no TYPE_DIR was overwritten.

14. **phase1_unlock**: exfat_clear_volume_dirty(sbi) THEN
    LOS_MuxUnlock(&sbi->s_lock).
    If err != 0: return err.

15. **Phase 2 (lock-free)**: if dst was found AND dst had a cluster chain
    to free (dst_primary_start_clu != EXFAT_EOF_CLUSTER):
    construct chain = { .dir = dst_primary_start_clu,
    .size = ceil(dst_primary_size / cluster_size) clamped >= 1u,
    .flags = dst_primary_flags },
    call exfat_free_cluster(sbi, &chain). On failure: PRINT_ERR (bitmap
    may carry orphan bits — sibling of unlink/rmdir partial-state policy).
    DO NOT return non-zero here; rename is logically complete on disk —
    only the bitmap is leaked, which fsck (future) can reclaim. Return 0.

16. Return 0.

## Refine Prompt

[RELY]
```c
extern UINT32 LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern UINT32 LOS_MuxUnlock(LosMux *mutex);
```

[SPECIFICATION of VfsExfatRename]
**Pre-Condition (lock state)**: No exFAT lock held on entry.

**Post-Condition (lock state)**:
  - Case 1/2/3/4 (success): sbi->s_lock released before return; bitmap_lock
    transiently held inside exfat_free_cluster during Phase 2 (Case 3/4
    only).
  - Case 5 (self-noop): sbi->s_lock acquired then released; no other
    lock touched.
  - Case 6 (validation): no lock ever acquired.
  - Case 7-12 (Phase 1 errors): sbi->s_lock acquired then released via
    phase1_unlock.

**Initialization-order constraint**: Within Phase 1:
  LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER) →
  exfat_set_volume_dirty →
  dst resolution scan →
  [self-noop early-exit OR type-mismatch OR not-empty: skip the
   rest, fall through to phase1_unlock] →
  dst tombstone write-back (when dst_found) →
  alloc new slot →
  init_dir_entry + init_ext_entry at new slot →
  tombstone src on-disk →
  src->data in-memory update + parent num_subdirs accounting →
  exfat_clear_volume_dirty → LOS_MuxUnlock(&sbi->s_lock).
  All Phase 1 exit paths go through phase1_unlock for the unlock + bracket
  close. The in-memory mutations (step 13) only execute on the success
  path — they MUST NOT run if err != 0 was set earlier.

**Deadlock note**: Phase 2's exfat_free_cluster acquires bitmap_lock
internally; holding s_lock during Phase 2 would impose s_lock →
bitmap_lock and is forbidden by the cross-stage lock-ordering invariant
(same constraint as unlink/rmdir Phase 2). Phase 2 therefore runs
strictly AFTER s_lock is released.

**Single-lock policy (v1)**: src->data->inode_lock and
dstParent_ei->inode_lock are NOT acquired in v1. sbi->s_lock as the
FS-wide namei lock provides sufficient serialization. This matches
Linux's exfat_rename — that function acquires only EXFAT_SB(sb)->s_lock
(no per-inode lock). Future v2 may evolve into a per-inode strategy if
namei throughput becomes a bottleneck.

**System Algorithm (locking phases)**:

**Phase 1 lock acquisition / release**:
  1. LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER) immediately after the
     Pre-Condition validation succeeds (main step 1) and the UTF-8 →
     UTF-16 conversion completes (main steps 2-3).
  2. All disk-mutating + in-memory accounting steps run with s_lock held.
  3. LOS_MuxUnlock(&sbi->s_lock) at phase1_unlock — reached on success,
     Case 5 self-noop, and Cases 8-12.

**Phase 2 lock-free**:
  4. Phase 2's overwrite-cluster-free runs lock-free w.r.t. s_lock.
     Internal bitmap_lock acquisition (per cluster batch) is the only
     lock held during Phase 2 — same as unlink/rmdir Phase 2.

Assumptions made:
  - v1 supports the full Linux exfat_rename surface: same-dir rename,
    cross-dir move, overwrite of file or empty dir. Non-empty dir
    overwrite returns -ENOTEMPTY. Type mismatch returns -EISDIR /
    -ENOTDIR.
  - v1 reclaims overwritten dst's clusters eagerly; Linux defers to
    fsck. Recorded as exfat-rename-overwrite-clusters-released-eagerly.
  - v1 does NOT roll back partial disk state on Case 11/12 failure;
    fsck (future) will reconcile. Recorded as
    exfat-rename-no-rollback-after-dst-written. Sibling of unlink/rmdir
    partial-state policy.
  - Cross-filesystem rename returns -EINVAL (matching LiteOS-A FAT
    convention) rather than -EXDEV. Recorded as
    exfat-rename-cross-fs-refused.
  - Parent on-disk dentry mtime/atime/iversion/num_subdirs persistence
    is OUT-OF-SCOPE (no parent metadata sync stage yet). Same v1
    limitation as mkdir/unlink/rmdir.
  - VFS hash / path_cache eviction of src under the old path and of
    overwritten dst is OUT-OF-SCOPE (VFS Reclaim's responsibility).
  - src is a value-semantics handle: src_ei->dir / entry / i_pos /
    attr fields are updated in memory; src vnode struct itself is not
    re-hashed or re-keyed (VFS layer post-processing handles that).
  - srcName is accepted only for diagnostic logging.
  - Phase 2 derives overwritten dst's num_phys_clu from
    dst_primary_size / cluster_size (rounded up, clamped ≥ 1u for
    non-empty dst). Same compute as unlink/rmdir.
  - Self-noop is detected by (start_clu, entry) identity not string
    comparison — same test as Linux.
