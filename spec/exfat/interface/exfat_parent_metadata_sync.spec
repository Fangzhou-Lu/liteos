[PROMPT]
Provide complete `exfat_inode.c` addition that implements
`exfat_sync_parent_dir_metadata`. Only include `<exfat.h>` as the header;
output a single C code block with no other code. The block is appended to
`fs/exfat/exfat_inode.c` next to existing inode-metadata helpers.

`exfat_sync_parent_dir_metadata` is the v3 helper that closes the v2
limitation recorded as invariants `exfat-mkdir-no-parent-dentry-write`,
`exfat-rmdir-...` and `exfat-rename-... parent on-disk dentry sync OUT-OF-
SCOPE`. It is invoked by the five write-path VOPs (mkdir / create / unlink
/ rmdir / rename) AFTER each VOP's in-memory parent state mutation
completes, so that the parent's mtime / atime / iversion in its
on-disk File dentry tracks Linux exfat's `dir->i_mtime = current_time(dir)
+ exfat_truncate_atime + inode_inc_iversion` pattern from Linux
fs/exfat/namei.c::exfat_unlink/rmdir/rename/etc.

Domain knowledge:
- exFAT on-disk structure does NOT store `num_subdirs` in any dentry —
  it is recomputed on mount via directory scan. Therefore this helper
  does NOT persist num_subdirs; it ONLY persists the three timestamps
  (atime / mtime / ctime per File dentry layout) plus the
  `exfat-meta-version` counter via the existing inode_metadata_model
  helpers.
- Root directory is the special case: `parent_ei->entry == -1`. The root
  has no File dentry inside any parent (it is identified by `sbi->root_dir`
  cluster directly). This helper MUST return 0 without any I/O when
  `parent_ei->entry == -1`. Recorded as invariant
  `exfat-pmds-root-noop`.
- The helper sequence is:
    1. touch_mtime_ctime + bump_version (in-memory; uses already-approved
       inode_metadata_model helpers)
    2. fetch parent's own dentry-set from grand-parent dir
       (via exfat_get_dentry_set against `parent_ei->dir` +
       `parent_ei->entry`)
    3. validate it (exfat_validate_dentry_set)
    4. store_metadata into set[0] (the File primary)
    5. recompute SetChecksum (Linux exfat_update_dir_chksum analogue —
       BUT v1 exfat_set_dentry_set does NOT compute chksum, caller does)
    6. write back via exfat_set_dentry_set
- Caller (each VOP) holds sbi->s_lock for the WHOLE Phase 1; this helper
  MUST be invoked from within that lock window. The helper itself does
  NOT acquire any lock. Recorded as invariant `exfat-pmds-no-locks`.
- Errors from this helper are treated as soft by callers — a parent
  metadata sync failure does NOT roll back the primary operation
  (mkdir/unlink/etc.) that already mutated the parent's child list. The
  VOP logs PRINT_ERR and returns 0. Recorded as invariant
  `exfat-pmds-best-effort` in this spec and as `exfat-pmds-vop-soft-fail`
  in the callsite VOPs (added by SpecFine pass at the same time).
- This stage does NOT change any existing approved spec INVARIANTS —
  it ADDS a new helper that the existing `no-parent-dentry-write`
  invariants now WAIVE via the wording "the parent on-disk dentry is NOT
  written within this VOP; sync is delegated to a future stage" → the
  future stage IS this one. The deferred-sync invariants stay in the
  old specs verbatim as historical record; an addendum reference points
  callers here.

## First Prompt

[RELY]
```c
typedef struct exfat_sb_info    exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;
typedef struct exfat_chain      exfat_chain;
typedef struct exfat_dentry     exfat_dentry;

#define EXFAT_DENTRY_SET_MAX     19

/* dentry-set IO (already approved at dentry_iter / dentry_set_write). */
extern int exfat_get_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir, int start_entry,
                                struct exfat_dentry *set, int max_entries,
                                int *num_entries);
extern int exfat_validate_dentry_set(const struct exfat_dentry *set,
                                     int num_entries);
extern int exfat_set_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir, int start_entry,
                                const struct exfat_dentry *set, int num_entries);

/* chksum recompute (already approved at chksum). */
extern uint16_t exfat_calc_chksum16(const void *data, int len,
                                    uint16_t chksum, int type);

/* inode_metadata_model helpers (already approved). */
extern void exfat_inode_touch_mtime_ctime(exfat_inode_info *ei);
extern void exfat_inode_bump_version(exfat_inode_info *ei);
extern int  exfat_inode_store_metadata(const exfat_sb_info *sbi,
                                       const exfat_inode_info *ei,
                                       struct exfat_dentry *file_dentry);
```

[GUARANTEE]
```c
/* Calling convention: caller holds sbi->s_lock for the entire Phase 1
 * window of the VOP that invokes this helper (mkdir/create/unlink/rmdir/
 * rename). This helper does NOT acquire any lock and does NOT touch
 * bitmap_lock.
 *
 * Returns 0 on success or negative POSIX errno on failure. Caller MUST
 * treat the return value as soft: log PRINT_ERR on non-zero and continue
 * (do NOT propagate the error to the VFS caller — the primary write
 * already succeeded on disk).
 *
 * Side effects on success:
 *   - parent_ei->atime_sec / mtime_sec / ctime_sec / version updated
 *     in memory (via the inode_metadata_model touch + bump helpers).
 *   - The parent directory's own File dentry on disk (located at
 *     parent_ei->dir / parent_ei->entry inside the grand-parent
 *     directory) has its time + chksum fields updated to match.
 *   - The Stream and Name dentries in parent's set are NOT touched.
 *
 * No-op cases (return 0, no I/O, no in-memory change):
 *   - parent_ei == NULL or sbi == NULL.
 *   - parent_ei->entry < 0 (root directory; no File dentry exists for
 *     root). */
int exfat_sync_parent_dir_metadata(exfat_sb_info *sbi,
                                   exfat_inode_info *parent_ei);
```

[SPECIFICATION]
**Pre-Condition**:
  - `sbi != NULL && parent_ei != NULL`. NULL inputs are NOT errors but
    are treated as no-ops (Case 0 below).
  - parent_ei is the in-memory inode_info for a directory whose
    `type == TYPE_DIR` (caller responsibility; this helper does NOT
    type-check defensively because exfat_inode_info fields are private
    to the VOPs that call this helper).
  - When `parent_ei->entry >= 0`: parent_ei->dir.dir is the grand-parent's
    cluster chain start, and parent_ei->entry is the linear dentry index
    of parent's own File dentry inside that grand-parent.
  - When `parent_ei->entry == -1`: parent is the root directory; no
    File dentry exists; helper returns 0 without I/O.
  - Caller holds sbi->s_lock.

**Post-Condition**:

**Case 0 (no-op — NULL or root)**:
  - `sbi == NULL`, OR `parent_ei == NULL`, OR `parent_ei->entry < 0`.
  - No memory mutation; no I/O.
  - Returns 0.

**Case 1 (success)**:
  - parent_ei->entry >= 0; sbi->s_lock held.
  - Step 1: in-memory `parent_ei->mtime_sec = parent_ei->ctime_sec =
    exfat_now_seconds()` (via exfat_inode_touch_mtime_ctime).
    parent_ei->version += 1 (via exfat_inode_bump_version).
  - Step 2: dentry-set fetched + validated from
    `(parent_ei->dir, parent_ei->entry)`.
  - Step 3: set[0] (File primary) populated by exfat_inode_store_metadata
    (which writes ATTR, mtime/atime/ctime fields, NOT the chksum).
  - Step 4: SetChecksum recomputed via exfat_calc_chksum16 with type
    CS_DIR_ENTRY (skips offsets 2-3) over the whole set; result written
    into set[0].dentry.file.checksum.
  - Step 5: full set written back via exfat_set_dentry_set.
  - Returns 0.

**Case 2 (fetch failure)**:
  - exfat_get_dentry_set returned non-zero (corrupt parent or transient
    -EIO).
  - In-memory parent_ei time fields ARE already updated (Step 1 ran
    before fetch). This is intentional: the next time someone fetches
    parent's metadata they will see fresh timestamps. The on-disk
    persistence is best-effort.
  - No on-disk mutation.
  - Returns the negative errno from exfat_get_dentry_set.

**Case 3 (validate failure)**:
  - exfat_validate_dentry_set returned non-zero. Parent's dentry-set is
    corrupt — chksum mismatch or wrong primary type.
  - In-memory time fields already updated.
  - No on-disk mutation.
  - Returns -EIO.

**Case 4 (write-back failure)**:
  - exfat_set_dentry_set returned non-zero. Partial dentry-set may exist
    on disk (the first sectors got written, later ones did not). PRINT_ERR.
  - In-memory time fields already updated.
  - Returns the negative errno from exfat_set_dentry_set.

**Invariant** (id=exfat-pmds-root-noop):
  When `parent_ei->entry == -1` (root directory), the helper returns 0
  IMMEDIATELY without calling any inode_metadata_model helper, without
  performing any I/O, and without modifying any field of parent_ei. The
  root directory has no on-disk File dentry; persistence is unrepresentable.
  This invariant protects mkdir at root level (the only level where
  root can be a parent) from a spurious "fetch dentry at -1" error.

**Invariant** (id=exfat-pmds-no-locks):
  The helper itself does NOT acquire sbi->s_lock, sbi->bitmap_lock, or
  parent_ei->inode_lock. Caller (always one of the 5 write-path VOPs)
  holds sbi->s_lock; this helper runs strictly within that critical
  section. Inheriting the no-lock policy of the dentry_set_write
  layer it builds on.

**Invariant** (id=exfat-pmds-best-effort):
  Errors from Cases 2/3/4 do NOT roll back the in-memory time updates
  (Step 1 ran before any potentially-failing step). The rationale is
  that the next mount will re-derive timestamps from on-disk anyway,
  and in-memory consistency matters more than the brief window where
  in-memory and on-disk disagree. The on-disk parent stays at its
  pre-helper state on any error path EXCEPT Case 4 where partial writes
  may persist (volume-dirty flag, set by the calling VOP, will surface
  this for a future fsck pass).

**Invariant** (id=exfat-pmds-chksum-recompute):
  Step 4 MUST recompute the SetChecksum over the entire dentry-set
  (num_entries dentries × DENTRY_SIZE bytes) AFTER store_metadata mutates
  the File primary's time fields, but BEFORE the write-back. The chksum
  type is CS_DIR_ENTRY (=2), matching the validate path. Skipping this
  step would cause subsequent exfat_validate_dentry_set on the same
  dentry to return -EIO, effectively orphaning the dentry-set even
  though the mtime update succeeded.

**Invariant** (id=exfat-pmds-time-fields-only):
  Step 3 (store_metadata) updates ONLY the time fields and ATTR byte of
  set[0].dentry.file. It MUST NOT touch start_clu, valid_size, size,
  num_ext, or any other byte. The helper inherits inode_metadata_model's
  guarantee that `exfat_inode_store_metadata` is the precise time-only
  writer (cross-referenced to `exfat-meta-store-fields-narrow` if
  present, otherwise this invariant stands alone as the contract).

**Invariant** (id=exfat-pmds-stream-name-untouched):
  Steps 2-5 read AND write the full dentry-set (num_entries dentries)
  but the only logical change is in set[0]. Stream (set[1]) and Name
  (set[2..]) dentries are written back byte-identical to what was read.
  This is structurally enforced by reading the full set before mutation,
  mutating only set[0], and writing the same set buffer back. v3 readers
  MAY rely on the property that parent metadata sync never alters the
  name representation of the parent's File entry.

**Invariant** (id=exfat-pmds-callers-vop-soft-fail):
  Callers (VfsExfatMkdir / VfsExfatCreate / VfsExfatUnlink /
  VfsExfatRmdir / VfsExfatRename) MUST invoke this helper after their
  Phase 1 in-memory mutation completes, MUST capture the return value
  into a local rc variable, MUST log PRINT_ERR when rc != 0, and MUST
  NOT propagate rc to their own caller — the primary operation has
  already succeeded on disk; demoting it to a failure because of a
  parent metadata sync hiccup would surprise callers. This invariant
  lives in this spec for traceability; the 5 VOPs are expected to
  enforce it via inspection during their next SpecFine pass.

**System Algorithm**:

1. If `sbi == NULL || parent_ei == NULL`: return 0 (Case 0).
2. If `parent_ei->entry < 0`: return 0 (Case 0 — root).
3. **Step 1 (in-memory)**:
   `exfat_inode_touch_mtime_ctime(parent_ei)`;
   `exfat_inode_bump_version(parent_ei)`.
4. **Step 2 (fetch parent's own dentry-set from grand-parent)**:
   `exfat_get_dentry_set(sbi, &parent_ei->dir, parent_ei->entry,
                         set, EXFAT_DENTRY_SET_MAX, &num_entries)`.
   On failure: PRINT_ERR; return negative errno (Case 2).
5. **Step 3 (validate)**: `exfat_validate_dentry_set(set, num_entries)`.
   On failure: PRINT_ERR; return -EIO (Case 3).
6. **Step 4 (store metadata into File primary)**:
   `exfat_inode_store_metadata(sbi, parent_ei, &set[0])`. This writes
   time fields + ATTR byte but NOT chksum.
7. **Step 5 (recompute chksum)**:
   `set[0].dentry.file.checksum = 0` first (per Microsoft chksum
   convention — the field is treated as 0 during chksum compute,
   then the result is stored back).
   `chksum = exfat_calc_chksum16(set, num_entries * DENTRY_SIZE,
                                  0, CS_DIR_ENTRY)`.
   `set[0].dentry.file.checksum = chksum`.
8. **Step 6 (write back)**:
   `exfat_set_dentry_set(sbi, &parent_ei->dir, parent_ei->entry,
                         set, num_entries)`. On failure: PRINT_ERR;
   return negative errno (Case 4).
9. Return 0.

Assumptions made:
  - inode_metadata_model's existing `exfat_inode_store_metadata` writes
    File-primary time fields without touching chksum. This is verified
    by reading its spec (spec/exfat/interface/exfat_inode_metadata_model.spec)
    before invoking — the spec confirms this is its narrow contract.
  - The 5 calling VOPs will be updated in a separate, minimal commit
    (NOT via SpecFine — just code-only callsite wiring) to invoke this
    helper at the documented insertion point. Their existing specs'
    "no-parent-dentry-write" invariants stay as historical record; the
    addendum in each spec PROMPT acknowledges v3 added this sync without
    breaking them (because the deferred-sync language was always
    forward-compatible with this helper).
  - Same v1 limitation as the existing write VOPs: no rollback of
    parent metadata sync errors. Recorded as `exfat-pmds-best-effort`.
