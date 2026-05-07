[PROMPT]
Provide complete `exfat_mkdir.c` that implements `VfsExfatMkdir`. Only include
`<exfat.h>` as the header; output a single C code block with no other code.

VfsExfatMkdir is the VnodeOps.Mkdir callback for the exFAT filesystem on
LiteOS-A. It creates a new sub-directory under an existing parent directory.
The operation allocates one fresh data cluster for the new directory, zeroes it,
inserts a File+Stream+Name dentry triplet at the parent's tail, then allocates
and initialises a Vnode backed by a new exfat_inode_info.

Domain knowledge:
- exFAT mkdir = alloc_dentry_slot + alloc_new_dir (zeroed cluster) +
  init_dir_entry + init_ext_entry + vnode creation; all five helpers are
  already approved siblings — treat them as defined APIs.
- A freshly created directory has num_subdirs = EXFAT_MIN_SUBDIR (2): the
  implicit "." and ".." conceptual entries.
- The entire mutation is bracketed by exfat_set_volume_dirty /
  exfat_clear_volume_dirty so a crash between any two disk writes leaves the
  volume marked dirty for next-mount fsck.
- Parent's in-memory num_subdirs is incremented; on-disk parent dentry sync is
  deferred to a future sync helper (v1 limitation consistent with truncate VOP).
- VfsExfatMkdir acquires sbi->s_lock for the full add_entry + vnode-creation
  span; see Refine Prompt for the locking discipline.

## First Prompt

[RELY]
```c
/* Types from common.header (reproduced for clarity; do not redeclare). */
typedef struct exfat_sb_info    exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;
typedef struct exfat_chain      exfat_chain;
struct Vnode;
struct Mount;

struct exfat_uni_name {
    uint16_t name[EXFAT_MAX_NAME_LEN + 1]; /* UTF-16 leaf name + NUL */
    uint16_t name_hash;                    /* upcase chksum16 */
    uint8_t  name_len;                     /* code-unit count, no NUL */
};

struct exfat_dir_entry {
    exfat_chain dir;      /* parent cluster chain */
    int32_t     entry;    /* linear dentry index in parent */
    uint32_t    type;     /* TYPE_DIR */
    uint16_t    attr;     /* ATTR_SUBDIR */
    uint32_t    start_clu;
    uint8_t     flags;    /* ALLOC_NO_FAT_CHAIN for single-cluster dir */
    uint64_t    size;     /* cluster_size bytes */
    uint32_t    num_subdirs; /* EXFAT_MIN_SUBDIR (2) */
};

// Returns number of Name dentries needed for uniname (>= 1). Returns -EINVAL
// if name_len is 0 or exceeds EXFAT_MAX_NAME_LEN. Lock-free.
extern int exfat_calc_num_entries(const struct exfat_uni_name *p_uniname);

// Allocates one cluster via exfat_alloc_cluster and writes one cluster's worth
// of zero bytes to disk. Fills clu_out. Requires sbi->s_lock NOT held by
// caller (alloc path may sleep on bitmap_lock).
extern int exfat_zeroed_cluster(exfat_sb_info *sbi, uint32_t clu);

// Allocates one cluster, zeroes it, and fills clu_out. Wraps zeroed_cluster.
// Called under sbi->s_lock held by VfsExfatMkdir.
extern int exfat_alloc_new_dir(exfat_sb_info *sbi, exfat_chain *clu_out);

// Writes File dentry at entry_idx in p_dir: sets type, start_clu, size, attr.
// Requires sbi->s_lock held.
extern int exfat_init_dir_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                                int entry_idx, uint32_t type,
                                uint32_t start_clu, uint64_t size);

// Writes Stream + Name dentries at entry_idx+1..+num_entries in p_dir.
// Requires sbi->s_lock held.
extern int exfat_init_ext_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                                int entry_idx, int num_entries,
                                const struct exfat_uni_name *p_uniname);

// Converts leaf name to uni_name, finds a free dentry slot, allocates a new
// dir cluster (TYPE_DIR), writes dentry triplet, and fills info. Requires
// sbi->s_lock held by caller. Returns 0 or negative POSIX errno.
extern int exfat_add_entry(exfat_sb_info *sbi, struct Vnode *parent_vp,
                           const char *name, uint32_t type,
                           struct exfat_dir_entry *info);

// Marks volume dirty (VOLUME_DIRTY bit) before any on-disk mutation.
extern int exfat_set_volume_dirty(exfat_sb_info *sbi);

// Clears VOLUME_DIRTY after mutation sequence completes (or fails cleanly).
extern int exfat_clear_volume_dirty(exfat_sb_info *sbi);

// Allocates and initialises a blank exfat_inode_info from m_aucSysMem0.
extern int exfat_inode_alloc(exfat_inode_info **out);

// Frees an exfat_inode_info allocated by exfat_inode_alloc.
extern void exfat_inode_free(exfat_inode_info *ei);

// UTF-8 → UTF-16 conversion; fills uni[0..uni_max-1], sets *uni_len.
extern int exfat_utf8_to_uni(const char *utf8, int utf8_len,
                              uint16_t *uni, int uni_max, int *uni_len);
```

[GUARANTEE]
```c
/* Calling convention: caller holds no lock; VfsExfatMkdir acquires
 * sbi->s_lock internally and releases it before returning.
 * Returns 0 on success or a negative POSIX errno on failure.
 * Side effects on success: a new Vnode (*vpp) is created and linked into the
 * VFS hash; parent_vp->data->num_subdirs is incremented in memory. */
int VfsExfatMkdir(struct Vnode *parent_vp, const char *name,
                  mode_t mode, struct Vnode **vpp);
```

[SPECIFICATION]
**Pre-Condition**:
  - `parent_vp` is a valid, locked (by VFS) Vnode whose `data` points to an
    initialised `exfat_inode_info` with `type == TYPE_DIR`.
  - `name` is a non-NULL, NUL-terminated UTF-8 string, 1–255 bytes.
  - `mode` carries the permission bits; exFAT ignores them beyond type
    selection (directory is implicit from the VOP slot).
  - `*vpp` is an out-parameter; its initial value is unspecified.
  - No thread holds `sbi->s_lock` on entry.

**Post-Condition**:

**Case 1 (success)**:
  - A new directory cluster is allocated and zeroed on disk.
  - A File + Stream + Name dentry triplet is written to the parent directory.
  - A new `Vnode` is created; `(*vpp)->data` points to a fresh
    `exfat_inode_info` with `type == TYPE_DIR`, `num_subdirs ==
    EXFAT_MIN_SUBDIR`, `start_clu` set to the new cluster, and `dir` chain
    populated from the parent's cluster info.
  - `parent_vp->data->num_subdirs` is incremented by 1 (in memory only; not
    yet flushed to the parent's on-disk dentry).
  - `sbi->s_lock` is released before return.
  - Returns 0.

**Case 2 (name-resolution or slot-allocation failure)**:
  - `exfat_add_entry` returned a negative errno (e.g. -ENOSPC, -EIO, -ENOMEM).
  - No new cluster is allocated; no dentry is written; `*vpp` is not set.
  - `sbi->s_lock` is released before return.
  - Returns the negative errno from `exfat_add_entry`.

**Case 3 (in-memory inode or vnode allocation failure after dentry written)**:
  - `exfat_add_entry` succeeded (cluster + dentry written to disk).
  - `exfat_inode_alloc` or `VnodeAlloc` failed.
  - The on-disk dentry and cluster are NOT rolled back (orphan left for fsck;
    consistent with Linux exfat_mkdir and the v1 no-rollback policy).
  - `sbi->s_lock` is released before return.
  - Returns -ENOMEM.

**Case 4 (VFS hash-insert failure after vnode allocated)**:
  - `exfat_add_entry`, `exfat_inode_alloc`, and `VnodeAlloc` all succeeded.
  - `VfsHashInsert(*vpp, key)` failed (hash collision or insertion error).
  - Cleanup MUST: clear `(*vpp)->data` to NULL before `VnodeFree(*vpp)` (so
    VnodeFree's reclaim path does not double-free the inode_info), then call
    `exfat_inode_free(ei)` directly.
  - The on-disk dentry and cluster are NOT rolled back (same orphan policy
    as Case 3).
  - `*vpp` is NOT set on return.
  - `sbi->s_lock` is released before return.
  - Returns the negative errno from `VfsHashInsert`.

**Invariant** (id=exfat-mkdir-subdir-count):
  After a successful VfsExfatMkdir, the new directory's
  `exfat_inode_info.num_subdirs == EXFAT_MIN_SUBDIR` (2), reflecting the two
  implicit dot-entries required for exFAT emptiness checks.

**Invariant** (id=exfat-mkdir-vol-dirty-bracketed):
  VfsExfatMkdir wraps `exfat_add_entry` strictly with
  `set_volume_dirty` / `clear_volume_dirty`. Even on failure of `add_entry`,
  `clear_volume_dirty` MUST be called. Volume-dirty bracketing is the only
  fsck signal for partially-committed disk state under v1 no-rollback policy.

**Invariant** (id=exfat-mkdir-vfs-hash-insert-after-data-set):
  `VfsHashInsert` MUST be called only after `(*vpp)->data = new_ei` —
  concurrent lookup that retrieves `*vpp` after hash insert must observe a
  fully-initialised `data`. Inversion would cause NULL-deref.

**Invariant** (id=exfat-mkdir-no-parent-dentry-write):
  This stage does NOT write the parent directory's own on-disk dentry.
  `parent_ei->num_subdirs` is incremented in memory only; the parent's
  ValidDataLength field on disk retains its old value. Sync helper
  persists this in a future stage (v1 known limitation, mirrors Wave B
  truncate VOP).

**Invariant** (id=exfat-mkdir-s-lock-bracketed):
  After acquiring `sbi->s_lock`, ALL exit paths (success and every failure
  case) release it via `LOS_MuxUnlock(&sbi->s_lock)`. Lock depth is 1; no
  re-entry. Helpers run lock-free; `exfat_alloc_cluster` takes
  `bitmap_lock` (not `s_lock`) so no self-recursion. The early-EINVAL
  return (bad name) does NOT acquire the lock.

**Invariant** (id=exfat-mkdir-leak-free-success):
  On the Case 1 success path, all allocations are owned downstream:
  stack-local `uniname` and `info` follow the function frame; `new_ei`
  and `new_vp` are owned by VFS reclaim. On failure, `new_ei` allocated
  before `VnodeAlloc` failure MUST be freed via `exfat_inode_free`; on
  hash-insert failure (Case 4) cleanup follows the explicit Case 4 path.

**Invariant** (id=exfat-mkdir-uniname-hash-cs-default):
  The `name_hash` written into the new directory's stream dentry MUST be
  computed with `exfat_calc_chksum16` over the raw UTF-16 byte stream
  (`name_len * sizeof(uint16_t)` bytes), seed `0`, type `CS_DEFAULT`. No
  upcase normalization is applied during write; lookup re-applies upcase
  on mismatching characters. Cross-module pairing: this hash MUST match
  the formula used by Wave A `VfsExfatLookup`, otherwise mkdir-then-lookup
  silently misses. Internal helper choice (where exactly the hash is
  computed) is unconstrained.

**System Algorithm**:
1. Acquire `sbi->s_lock`; call `exfat_set_volume_dirty`.
2. Call `exfat_add_entry(sbi, parent_vp, name, TYPE_DIR, &info)` — this
   internally resolves the name to UTF-16, finds a free dentry slot, allocates
   and zeroes a new cluster, and writes the dentry triplet.
3. Call `exfat_clear_volume_dirty`; release `sbi->s_lock`.
4. If step 2 failed, return its errno.
5. Allocate and initialise a new `exfat_inode_info` from `info`; increment
   `parent_ei->num_subdirs`.
6. Allocate a new Vnode via VFS helpers, attach the `exfat_inode_info` as
   `vnode->data`, and assign `*vpp`.
7. Return 0.

## Refine Prompt

[RELY]
```c
// Acquire sbi->s_lock (LosMux, may sleep). UINT32_MAX = wait forever.
extern int LOS_MuxLock(LosMux *mutex, UINT32 timeout);
// Release sbi->s_lock.
extern int LOS_MuxUnlock(LosMux *mutex);
```

[SPECIFICATION of VfsExfatMkdir]
**Pre-Condition**: No locks held on entry.
**Post-Condition**:
  - Case 1 (success): `sbi->s_lock` released; `*vpp` set; returns 0.
  - Case 2 (add_entry failure): `sbi->s_lock` released; returns negative errno.
  - Case 3 (vnode alloc failure): `sbi->s_lock` already released at step 3;
    returns -ENOMEM. No lock is held on any error return.

**System Algorithm**:

**Phase 1: Mutation bracket**
  - Goal: atomically write cluster + dentry triplet under the volume-dirty flag.
  - Algorithm:
    1. `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)`.
    2. `exfat_set_volume_dirty(sbi)`.
    3. `exfat_add_entry(sbi, parent_vp, name, TYPE_DIR, &info)`.
    4. `exfat_clear_volume_dirty(sbi)`.
    5. `LOS_MuxUnlock(&sbi->s_lock)`.
  - Post-condition: `sbi->s_lock` not held; `info` valid iff step 3 succeeded.
  - Error Handling: if step 3 fails, still execute steps 4-5, then return errno.

**Phase 2: Vnode creation (lock-free)**
  - Goal: create the VFS Vnode representing the new directory.
  - Algorithm:
    1. `exfat_inode_alloc(&ei)` — allocate from m_aucSysMem0.
    2. Populate `ei` fields from `info` (type, attr, start_clu, flags, size,
       num_subdirs, dir chain, entry index).
    3. Allocate Vnode via `VnodeAlloc`; attach `ei` as `vnode->data`; set
       vnode type to `VNODE_TYPE_DIR`.
    4. Increment `parent_ei->num_subdirs` (in-memory only).
    5. Assign `*vpp = vnode`; return 0.
  - Error Handling: if inode_alloc or VnodeAlloc fails, call `exfat_inode_free`
    if allocated, leave on-disk state as-is, return -ENOMEM.
