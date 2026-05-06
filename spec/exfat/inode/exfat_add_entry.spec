[PROMPT]
Provide complete `exfat_add_entry` implementation in `fs/exfat/exfat_inode.c`. Only
include `<exfat.h>` (the in-tree umbrella header); output a single C code block
containing only this function, no surrounding declarations.

`exfat_add_entry` is the mkdir-core composer for the exFAT port on LiteOS-A. It
is called by `VfsExfatMkdir` (which holds `sbi->s_lock` and has already opened the
`vol_dirty` bracket). The function converts the caller-supplied UTF-8 leaf name to
a UTF-16 `exfat_uni_name`, computes the dentry-set count, reserves a contiguous
slot in the parent directory, allocates and zeros a new data cluster (TYPE_DIR only;
TYPE_FILE returns -ENOSYS in v1), writes the file+stream dentry pair and all name
extension dentries, then hands the fully-initialized `exfat_dir_entry` info struct
back to the caller. It holds no locks itself — `bitmap_lock` is taken internally by
`exfat_alloc_new_dir`; `s_lock` belongs to the caller (VOP tier).

## First Prompt

[RELY]
```c
/* ---- on-disk / in-memory constants ---- */
#define EXFAT_EOF_CLUSTER      0xFFFFFFFFu
#define EXFAT_MIN_SUBDIR       2u
#define EXFAT_MAX_NAME_LEN     255
#define TYPE_DIR               0x00000010u
#define TYPE_FILE              0x00000020u
#define ATTR_SUBDIR            0x0010u
#define ALLOC_NO_FAT_CHAIN     0x03u
#define CS_DEFAULT             0        /* chksum16 seed for name_hash */

// Convert UTF-8 byte stream to UTF-16LE. utf8_len=-1 means strlen. Returns 0
// on success, -EINVAL if sequence is invalid, -ENAMETOOLONG if result > uni_max.
// *uni_len receives the number of UTF-16 code units written.
extern int  exfat_utf8_to_uni(const char *utf8, int utf8_len,
                               uint16_t *uni, int uni_max, int *uni_len);

// Compute 16-bit checksum over byte stream [data, data+len) with given seed.
// type=CS_DEFAULT: basic additive roll; type=CS_DIR_ENTRY: skips bytes 2-3.
extern uint16_t exfat_calc_chksum16(const void *data, int len,
                                     uint16_t chksum, int type);

// Return number of dentry slots needed for p_uniname (2 + ceil(name_len/15)).
// Returns -EINVAL if name_len==0 or overflows.
extern int  exfat_calc_num_entries(const struct exfat_uni_name *p_uniname);

// Scan p_dir for n_entries consecutive free dentry slots. Returns slot index
// (>=0) on success, negative errno on failure. May extend p_dir if volume
// has free clusters. Lock-free; caller holds s_lock.
extern int  exfat_alloc_dentry_slot(const exfat_sb_info *sbi,
                                     const exfat_chain *p_dir,
                                     int n_entries, int *slot_idx_out);

// Allocate a single new cluster, zero-fill it via los_part_write, and return
// the one-cluster chain in *clu_out (flags=ALLOC_NO_FAT_CHAIN, size=1).
// Takes bitmap_lock internally. Returns 0 or negative errno.
extern int  exfat_alloc_new_dir(exfat_sb_info *sbi, exfat_chain *clu_out);

// Write the file-entry + stream-extension dentry pair at slot [entry] inside
// p_dir. Sets type, start_clu, size, timestamps to epoch-zero. Lock-free.
extern int  exfat_init_dir_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                                  int entry, uint32_t type,
                                  uint32_t start_clu, uint64_t size);

// Append num_entries-2 name-extension dentries starting at slot [entry+2] and
// update the set checksum in the stream dentry. Lock-free.
extern int  exfat_init_ext_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                                  int entry, int num_entries,
                                  const struct exfat_uni_name *p_uniname);

/* ---- value types ---- */
struct exfat_uni_name {
    uint16_t name[EXFAT_MAX_NAME_LEN + 1];
    uint16_t name_hash;   /* CS_DEFAULT chksum16 over UTF-16 byte stream */
    uint8_t  name_len;    /* UTF-16 code-unit count, 1..EXFAT_MAX_NAME_LEN */
};

struct exfat_dir_entry {
    exfat_chain dir;        /* parent directory chain (copy) */
    int32_t     entry;      /* dentry slot index of the file-entry */
    uint32_t    type;       /* TYPE_DIR | TYPE_FILE */
    uint16_t    attr;       /* ATTR_SUBDIR or ATTR_ARCHIVE */
    uint32_t    start_clu;  /* first data cluster of new entry */
    uint8_t     flags;      /* ALLOC_NO_FAT_CHAIN */
    uint64_t    size;       /* bytes; cluster_size for TYPE_DIR */
    uint32_t    num_subdirs;/* EXFAT_MIN_SUBDIR for TYPE_DIR */
};
```

[GUARANTEE]
```c
/* Caller holds sbi->s_lock and has called exfat_set_volume_dirty.
 * Returns 0 on success or -errno on any failure path.
 * On success *info is fully populated; on failure *info contents are undefined.
 * Does NOT acquire s_lock; bitmap_lock is managed internally by exfat_alloc_new_dir. */
int exfat_add_entry(exfat_sb_info *sbi, struct Vnode *parent_vp,
                    const char *name, uint32_t type,
                    struct exfat_dir_entry *info);
```

[SPECIFICATION]
**Pre-Condition**:
  - `sbi`, `parent_vp`, `parent_vp->data`, `name`, `info` are all non-NULL.
  - `name` is a non-empty NUL-terminated UTF-8 string of at most
    `EXFAT_MAX_NAME_LEN * 4` bytes (loose byte bound before UTF-16 conversion).
  - `type` is exactly `TYPE_DIR` or `TYPE_FILE`.
  - Caller holds `sbi->s_lock`; `exfat_set_volume_dirty` has been called.
  - `parent_vp->data` points to a valid `exfat_inode_info` for an existing directory.

**Post-Condition**:

**Case 1 (success — TYPE_DIR)**:
  - A UTF-16 name has been stored in a zeroed `exfat_uni_name`; `name_len` and
    `name_hash` (CS_DEFAULT, seed 0) are set.
  - `num_entries = exfat_calc_num_entries(&uniname)` contiguous free dentry slots
    have been reserved at index `dentry_idx` in `p_dir`.
  - A new cluster `new_clu` has been allocated and zeroed on disk.
  - The file+stream dentry pair at `p_dir[dentry_idx]` reflects `type=TYPE_DIR`,
    `start_clu=new_clu.dir`, `size=cluster_size`, timestamps zero.
  - Name-extension dentries at slots `[dentry_idx+2 .. dentry_idx+num_entries-1]`
    carry the UTF-16 name; set checksum is correct.
  - `*info` is populated: `dir=p_dir`, `entry=dentry_idx`, `type=TYPE_DIR`,
    `attr=ATTR_SUBDIR`, `start_clu=new_clu.dir`, `flags=ALLOC_NO_FAT_CHAIN`,
    `size=cluster_size`, `num_subdirs=EXFAT_MIN_SUBDIR`.
  - Returns **0**.

**Case 2 (TYPE_FILE — not yet implemented)**:
  - No on-disk state is modified.
  - Returns **-ENOSYS**.

**Case 3 (argument validation failure)**:
  - Any NULL pointer arg, empty name, or invalid type triggers early return.
  - No on-disk state is modified.
  - Returns **-EINVAL** or **-ENAMETOOLONG** (name exceeds loose byte bound).

**Case 4 (UTF-8 conversion failure)**:
  - `exfat_utf8_to_uni` returns non-zero (malformed sequence or name too long).
  - No dentry slot has been allocated; no cluster has been allocated.
  - Returns the error code from `exfat_utf8_to_uni`.

**Case 5 (dentry slot allocation failure)**:
  - `exfat_alloc_dentry_slot` returns negative errno (e.g. -ENOSPC, -EIO).
  - No cluster has been allocated.
  - Returns that errno.

**Case 6 (new-dir cluster allocation failure)**:
  - `exfat_alloc_new_dir` returns negative errno.
  - The reserved dentry slot (`dentry_idx`) is NOT released — the slot is left
    as-is; `vol_dirty` marks the volume for fsck on next mount.
  - Returns the errno from `exfat_alloc_new_dir`.

**Case 7 (init_dir_entry or init_ext_entry failure)**:
  - One or both dentry-write helpers fail.
  - The allocated cluster and the reserved dentry slot are NOT rolled back.
  - `vol_dirty` marks the volume; next mount fsck repairs the orphaned cluster
    and partial dentry-set.
  - Returns the failing helper's errno.

**Invariant** (id=exfat-add-entry-uniname-hash-cs-default):
  `name_hash` is computed with
  `exfat_calc_chksum16` over the raw UTF-16 byte stream (`name_len * sizeof(uint16_t)`
  bytes), seed `0`, type `CS_DEFAULT`. Upcase normalization is NOT applied here;
  lookup re-applies upcase on mismatching characters. This invariant locks the
  hash formula's pairing with Wave A lookup — both must use the same formula or
  mkdir-then-lookup misses.

**Invariant** (id=exfat-add-entry-no-cluster-rollback-on-init-fail):
  If `exfat_init_dir_entry`
  or `exfat_init_ext_entry` fails, the already-allocated dir cluster and the
  located dentry slot are NOT rolled back (Q3=a no-rollback); `vol_dirty` bracket
  marks the volume; next mount fsck repairs orphaned cluster and partial dentry-set.
  This locks `add_entry`'s failure semantics, mirrored on Linux `namei.c::exfat_add_entry`'s
  `goto out` order (Linux also no-rollback, relying on buffer cache + journal-equivalent;
  here `vol_dirty` carries the same role).

**Invariant** (id=exfat-add-entry-no-self-cluster-double-free):
  `exfat_alloc_new_dir`'s internal
  rollback path (Case 3 inside `alloc_cluster`) does NOT call the public
  `exfat_free_cluster` API — that would self-deadlock with `bitmap_lock` already
  held by `alloc_cluster`. Rollback inlines `exfat_clear_bitmap` + `exfat_ent_set`
  directly (same spirit as Stage 2c invariant `exfat-alloc-cluster-rollback-on-error`).

**System Algorithm**:
The composition proceeds in seven ordered phases. No phase may be reordered
because each produces a value consumed by the next.

1. **Resolve name**: `memset_s` the `exfat_uni_name` to zero; count raw UTF-8
   bytes (bound: `> EXFAT_MAX_NAME_LEN * 4` → -ENAMETOOLONG); call
   `exfat_utf8_to_uni`; validate `uni_len` in `[1, EXFAT_MAX_NAME_LEN]`;
   store `name_len` and compute `name_hash` via `exfat_calc_chksum16` with
   seed 0 and type `CS_DEFAULT`.

2. **Calc entries**: call `exfat_calc_num_entries(&uniname)` → `num_entries`.
   Propagate negative return as error.

3. **Build parent chain**: derive `p_dir` from `parent_ei->start_clu`,
   `parent_ei->flags`, and a size-rounded-up cluster count from `parent_ei->size`
   (default 1 cluster when size is zero — single-cluster root assumption for v1).

4. **Alloc dentry slot**: call `exfat_alloc_dentry_slot(sbi, &p_dir, num_entries,
   &dentry_idx)`. Propagate failure immediately (no cluster yet allocated).

5. **Alloc new dir cluster** (TYPE_DIR only): `memset_s` a local `exfat_chain` to
   zero; call `exfat_alloc_new_dir(sbi, &new_clu)`; capture `start_clu` and
   `clu_size`. On failure return immediately without releasing `dentry_idx`
   (see invariant `exfat-add-entry-no-cluster-rollback-on-init-fail`).

6. **Init dentry pair**: call `exfat_init_dir_entry` then `exfat_init_ext_entry`.
   On either failure return immediately without rollback (same invariant).

7. **Fill info**: populate all fields of `*info` from the values accumulated in
   phases 1-6; return 0.
