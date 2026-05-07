[PROMPT]
Provide complete `exfat_create.c` that implements `VfsExfatCreate`. Only include
`<exfat.h>` as the header; output a single C code block with no other code.

VfsExfatCreate is the VnodeOps.Create callback for the exFAT filesystem on
LiteOS-A. It creates a new empty regular file under an existing parent
directory. Unlike mkdir, no data cluster is allocated for the file: an empty
exFAT file is represented by a File+Stream+Name dentry triplet with
start_clu = EXFAT_EOF_CLUSTER and ValidDataLength = 0; the first cluster is
allocated lazily on the first write.

Domain knowledge:
- exFAT create = alloc_dentry_slot + init_dir_entry (TYPE_FILE) +
  init_ext_entry + vnode creation. NO new cluster is allocated; the
  alloc_new_dir step is skipped. Helpers are shared with mkdir.
- Linux fs/exfat/namei.c::exfat_add_entry uses `if (type == TYPE_DIR)`
  to gate the cluster allocation; LiteOS-A v0.4 mirrors this.
- Empty file invariants (Linux exfat_add_entry tail):
    info->attr        = ATTR_ARCHIVE   (regular file marker)
    info->start_clu   = EXFAT_EOF_CLUSTER  (no cluster yet)
    info->flags       = ALLOC_NO_FAT_CHAIN
    info->size        = 0
    info->num_subdirs = 0
- Parent directory's in-memory num_subdirs is NOT incremented for files (only
  subdirectories count toward exFAT subdir tally).
- vnode->mode = S_IFREG | (0644 & ~sbi->options.fs_fmask) — files use fmask,
  not dmask.
- VfsHashInsert key for an empty file: start_clu is EXFAT_EOF_CLUSTER (same
  for every empty file), so use the dentry index in the parent — `(uint32_t)
  info.entry` — which matches the lookup-side convention in exfat_inode.c
  line 467 `VfsHashInsert(vp, (uint32_t)ei->i_pos)`. This makes
  create-then-lookup observable.

## First Prompt

[RELY]
```c
/* Types from common.header (reproduced for clarity; do not redeclare). */
typedef struct exfat_sb_info    exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;
typedef struct exfat_chain      exfat_chain;
struct Vnode;
struct Mount;

struct exfat_dir_entry {
    exfat_chain dir;       /* parent cluster chain */
    int32_t     entry;     /* linear dentry index in parent */
    uint32_t    type;      /* TYPE_FILE */
    uint16_t    attr;      /* ATTR_ARCHIVE for files */
    uint32_t    start_clu; /* EXFAT_EOF_CLUSTER for empty file */
    uint8_t     flags;     /* ALLOC_NO_FAT_CHAIN */
    uint64_t    size;      /* 0 for empty file */
    uint32_t    num_subdirs; /* 0 for files */
};

// Composes dentry-set for TYPE_FILE: resolves UTF-8→UTF-16, reserves dentry
// slot, writes File+Stream+Name dentries at slot. Skips cluster allocation
// for TYPE_FILE (returns info with start_clu = EXFAT_EOF_CLUSTER, size = 0).
// Caller must hold sbi->s_lock. Returns 0 or negative POSIX errno.
extern int exfat_add_entry(exfat_sb_info *sbi, struct Vnode *parent_vp,
                           const char *name, uint32_t type,
                           struct exfat_dir_entry *info);

extern int exfat_set_volume_dirty(exfat_sb_info *sbi);
extern int exfat_clear_volume_dirty(exfat_sb_info *sbi);
extern int exfat_inode_alloc(exfat_inode_info **out);
extern void exfat_inode_free(exfat_inode_info *ei);
```

[GUARANTEE]
```c
/* Calling convention: caller holds no lock; VfsExfatCreate acquires
 * sbi->s_lock internally, releases it after add_entry, then performs
 * vnode creation lock-free. Returns 0 on success or negative POSIX errno.
 * Side effects on success: a new Vnode (*vpp) is created and linked into
 * the VFS hash; parent_vp->data->num_subdirs is NOT modified (file, not
 * subdirectory). */
int VfsExfatCreate(struct Vnode *parent_vp, const char *name,
                   int mode, struct Vnode **vpp);
```

[SPECIFICATION]
**Pre-Condition**:
  - `parent_vp` is a valid Vnode whose `data` points to an initialised
    `exfat_inode_info` with `type == TYPE_DIR`.
  - `name` is a non-NULL, NUL-terminated UTF-8 string, 1–255 bytes.
  - `mode` carries the permission bits; exFAT applies fs_fmask to clamp
    the actual mode stored on the new vnode (no per-file mode bits on disk).
  - `*vpp` is an out-parameter; its initial value is unspecified.
  - No thread holds `sbi->s_lock` on entry.

**Post-Condition**:

**Case 1 (success)**:
  - A File + Stream + Name dentry triplet is written to the parent directory,
    marking a regular file with ATTR_ARCHIVE, start_clu = EXFAT_EOF_CLUSTER,
    size 0.
  - NO new data cluster is allocated (lazy: first write triggers allocation).
  - A new `Vnode` is created; `(*vpp)->type == VNODE_TYPE_REG`;
    `(*vpp)->data` points to a fresh `exfat_inode_info` with
    `type == TYPE_FILE`, `attr == ATTR_ARCHIVE`, `start_clu ==
    EXFAT_EOF_CLUSTER`, `size == 0`, `num_subdirs == 0`.
  - `parent_vp->data->num_subdirs` is NOT incremented (files do not count).
  - `sbi->s_lock` is released before return.
  - Returns 0.

**Case 2 (add_entry failure)**:
  - `exfat_add_entry` returned a negative errno (-ENOSPC, -EIO, -ENOMEM,
    -ENAMETOOLONG, etc.).
  - No dentry is written (or partial write left on disk if EIO mid-step;
    vol_dirty bracket leaves fsck signal).
  - `*vpp` is not set.
  - `sbi->s_lock` is released before return.
  - Returns the negative errno from `exfat_add_entry`.

**Case 3 (in-memory inode or vnode allocation failure)**:
  - `exfat_add_entry` succeeded (dentry written to disk).
  - `exfat_inode_alloc` or `VnodeAlloc` failed.
  - The on-disk dentry is NOT rolled back (orphan on disk; mirrors mkdir
    Case 3 + Linux v1 no-rollback policy).
  - `sbi->s_lock` is already released at this point.
  - `*vpp` is not set.
  - Returns -ENOMEM.

**Case 4 (VFS hash-insert failure)**:
  - All earlier steps succeeded; `VfsHashInsert` failed.
  - Cleanup MUST: clear `(*vpp)->data` to NULL before `VnodeFree(*vpp)`
    (so reclaim does not double-free), then call `exfat_inode_free(ei)`.
  - On-disk dentry is NOT rolled back.
  - `*vpp` is NOT set on return.
  - Returns -ENOMEM.

**Invariant** (id=exfat-create-no-cluster-on-empty):
  After a successful VfsExfatCreate, `(*vpp)->data->start_clu ==
  EXFAT_EOF_CLUSTER` and `size == 0`. NO data cluster is allocated. The
  bitmap usage count is unchanged. First write triggers lazy alloc.

**Invariant** (id=exfat-create-attr-archive):
  The on-disk File dentry has `attr & ATTR_ARCHIVE != 0` and
  `attr & ATTR_SUBDIR == 0`. Tools that classify entries by attr (mkfs.exfat
  fsck, Linux exfat) MUST classify the result as a regular file.

**Invariant** (id=exfat-create-vol-dirty-bracketed):
  VfsExfatCreate wraps `exfat_add_entry` strictly with
  `set_volume_dirty` / `clear_volume_dirty`. Even on `add_entry` failure
  `clear_volume_dirty` MUST be called.

**Invariant** (id=exfat-create-no-parent-subdir-bump):
  `parent_ei->num_subdirs` is NEITHER read NOR written by this VOP.
  Files are not directories; the subdir tally must remain unchanged.
  Linux fs/exfat/namei.c::exfat_create matches this (no inc_subdirs call).

**Invariant** (id=exfat-create-vfs-hash-insert-after-data-set):
  `VfsHashInsert` MUST be called only after `(*vpp)->data = new_ei`.
  Same NULL-deref hazard as mkdir.

**Invariant** (id=exfat-create-s-lock-bracketed):
  `sbi->s_lock` is held ONLY for Phase 1 (the add_entry mutation +
  vol_dirty bracket). Phase 2 (inode_alloc + VnodeAlloc + VfsHashInsert)
  runs lock-free. All exit paths release the lock before returning.

**Invariant** (id=exfat-create-leak-free-success):
  Same ownership rules as mkdir. On failure paths after Phase 1,
  `exfat_inode_free` is called for any `new_ei` that was allocated but
  not yet handed to a vnode. On hash-insert failure, the explicit Case 4
  cleanup sequence (clear vnode->data, VnodeFree, then inode_free) runs.

**Invariant** (id=exfat-create-uniname-hash-cs-default):
  Same as mkdir: `name_hash` in the stream dentry MUST be
  `exfat_calc_chksum16(name, name_len * sizeof(uint16_t), 0, CS_DEFAULT)`.
  Cross-module pairing: this hash MUST match Wave A lookup; otherwise
  create-then-open silently fails.

**System Algorithm**:
1. Acquire `sbi->s_lock`; call `exfat_set_volume_dirty`.
2. Call `exfat_add_entry(sbi, parent_vp, name, TYPE_FILE, &info)` — internally
   resolves UTF-16 name, reserves dentry slot, writes File+Stream+Name dentry
   triplet. NO cluster allocation (TYPE_FILE branch).
3. Call `exfat_clear_volume_dirty`; release `sbi->s_lock`.
4. If step 2 failed, return its errno.
5. Allocate and initialise a new `exfat_inode_info` from `info`. DO NOT bump
   parent num_subdirs.
6. Allocate a new Vnode (VNODE_TYPE_REG), attach inode, set mode S_IFREG |
   (0644 & ~fs_fmask), call `VfsHashInsert(*vpp, (uint32_t)info.entry)`.
7. Return 0.

## Refine Prompt

[RELY]
```c
extern int LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern int LOS_MuxUnlock(LosMux *mutex);
```

[SPECIFICATION of VfsExfatCreate]
**Pre-Condition**: No locks held on entry.
**Post-Condition**:
  - Case 1 (success): `sbi->s_lock` released; `*vpp` set; returns 0.
  - Case 2 (add_entry failure): `sbi->s_lock` released; returns negative errno.
  - Case 3 (inode/vnode alloc failure): lock already released; returns -ENOMEM.
  - Case 4 (VfsHashInsert failure): lock already released; returns -ENOMEM.

**System Algorithm**:

**Phase 1: Mutation bracket**
  - Goal: atomically write the dentry triplet under the volume-dirty flag.
  - Algorithm:
    1. `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)`.
    2. `exfat_set_volume_dirty(sbi)`.
    3. `exfat_add_entry(sbi, parent_vp, name, TYPE_FILE, &info)`.
    4. `exfat_clear_volume_dirty(sbi)`.
    5. `LOS_MuxUnlock(&sbi->s_lock)`.
  - Post-condition: `sbi->s_lock` not held; `info` valid iff step 3 succeeded.
  - Error Handling: if step 3 fails, still run steps 4-5, then return errno.

**Phase 2: Vnode creation (lock-free)**
  - Goal: create the VFS Vnode representing the new file.
  - Algorithm:
    1. `exfat_inode_alloc(&ei)`.
    2. Populate `ei` from `info` (type=TYPE_FILE, attr=ATTR_ARCHIVE,
       start_clu=EXFAT_EOF_CLUSTER, size=0, num_subdirs=0,
       valid_size=0, i_size_ondisk=0). NO init_dir_chain call (file is
       not a directory chain).
    3. Allocate Vnode via `VnodeAlloc`; type = VNODE_TYPE_REG;
       mode = S_IFREG | (0644 & ~sbi->options.fs_fmask).
    4. `VfsHashInsert(vp, (uint32_t)info.entry)`. On failure: clear
       vp->data, VnodeFree(vp), exfat_inode_free(ei), return -ENOMEM.
    5. Assign `*vpp = vnode`; return 0.
  - Error Handling: same orphan-on-disk policy as mkdir.
