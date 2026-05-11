[PROMPT]
Provide complete `exfat_super.c` definitions for the two remaining
`MountOps` callbacks: `VfsExfatStatfs` and `VfsExfatSync`. Only include
`<exfat.h>` as the header (`<sys/statfs.h>` is already available via the
existing `exfat_super.c` includes); output a single C code block with no
other code. The block is appended to `fs/exfat/exfat_super.c` next to the
existing `g_exfatMountOps` static-init.

`VfsExfatStatfs` fills a POSIX `struct statfs` with exFAT volume geometry
(magic / block-size / total-blocks / free-blocks / name-len). `VfsExfatSync`
is a v1 no-op — write-back is synchronous in every write-path stage (every
`exfat_set_dentry_set` / `exfat_set_volume_dirty` / `exfat_free_cluster`
issues a `los_part_write` synchronously), so there is no dirty-buffer queue
to flush. The slot exists purely for VFS contract compatibility.

Domain knowledge:
- The `struct statfs` layout LiteOS-A uses is the same one `<sys/statfs.h>`
  exposes for userspace. exFAT fills these fields:
    * `f_type    = EXFAT_SUPER_MAGIC` (0x2011BAB0u; identifies the FS to
      `/proc/mounts` ShowType lookup — see `fs/proc/os_adapt/mounts_proc.c`).
    * `f_bsize   = sbi->cluster_size` (allocation unit, matches Linux exfat).
    * `f_blocks  = sbi->num_clusters - 2` (subtract the two reserved
      cluster slots 0 and 1; matches Linux `sbi->num_clusters - 2`).
    * `f_bfree   = f_blocks - sbi->used_clusters` (saturating at 0).
    * `f_bavail  = f_bfree` (no per-user quotas in v1).
    * `f_namelen = EXFAT_MAX_NAME_LEN` (255 UTF-16 code units).
  All other fields are zeroed via `memset_s` before population.
- `f_files` / `f_ffree` are zero in v1 — exFAT has no fixed inode pool
  (inodes are allocated dynamically as dentries are encountered). Linux
  exfat sets f_files = num_clusters * dentries_per_clu but that's a
  conservative over-count; v1 returns 0 to mean "unbounded / not tracked"
  per POSIX-allowed behaviour.
- `used_clusters == EXFAT_CLUSTERS_UNTRACKED` (`(uint32_t)~0u`) is a
  legitimate transitional state: a not-yet-counted volume. v1 chooses to
  surface this as `f_bfree = 0` (conservative — "report no free blocks"
  rather than wrap-around arithmetic). Future stages may eagerly call
  exfat_count_used_clusters at mount time to populate it.
- `VfsExfatSync` returns 0 unconditionally. In v1 every write helper
  flushes synchronously; there is no buffer cache. The slot exists for
  POSIX `sync(2)` / `fsync(2)` traversal of mounted FS list — returning
  -ENOSYS would break those callers; returning 0 is the documented v1
  contract until a future write-back cache lands.
- Neither function acquires `sbi->s_lock`. `VfsExfatStatfs` reads
  geometric fields that are immutable post-mount (set during boot_sector
  parse and never written again) plus `used_clusters` which is updated
  by alloc/free_cluster paths under `bitmap_lock`; the spec accepts the
  small read-tear window (a single uint32 wide; LOR-free) as the cost of
  not adding cross-lock contention to a metadata-only read path.

[RELY]
```c
typedef struct exfat_sb_info exfat_sb_info;
struct Mount;
struct statfs;

/* Magic constants from common.header. */
#define EXFAT_SUPER_MAGIC       0x2011BAB0u
#define EXFAT_MAX_NAME_LEN      255
#define EXFAT_CLUSTERS_UNTRACKED (~0u)

/* Standard libsec — already used throughout exfat_super.c. */
extern int memset_s(void *dest, size_t destMax, int c, size_t count);

/* sbi accessor (mount->data points at exfat_sb_info; see mount.spec). */
```

[GUARANTEE]
```c
/* Calling convention: caller holds no exFAT lock. Both functions are
 * pure metadata readers (Statfs) or no-op (Sync); neither acquires
 * sbi->s_lock or sbi->bitmap_lock.
 *
 * Returns 0 on success or negative POSIX errno on failure.
 *
 * VfsExfatStatfs side effects:
 *   - *sbp is fully written: every field set to either a computed value
 *     (f_type / f_bsize / f_blocks / f_bfree / f_bavail / f_namelen) or
 *     zero (everything else, via memset_s).
 *   - sbi is not modified.
 *
 * VfsExfatSync side effects:
 *   - none (v1 is synchronous-write-only). */
static int VfsExfatStatfs(struct Mount *mount, struct statfs *sbp);
static int VfsExfatSync(struct Mount *mount);
```

[SPECIFICATION]
**Pre-Condition (VfsExfatStatfs)**:
  - `mount` is a valid Mount pointer; `mount->data` points to an
    initialised `exfat_sb_info` whose geometric fields (`cluster_size`,
    `num_clusters`) were filled during mount and never subsequently
    modified.
  - `sbp` is a non-NULL caller-provided `struct statfs` buffer.

**Pre-Condition (VfsExfatSync)**:
  - `mount` is any value (NULL or valid); v1 ignores it.

**Post-Condition**:

**Case 1 (Statfs success)**:
  - `*sbp` is fully populated:
      * `sbp->f_type    == EXFAT_SUPER_MAGIC`
      * `sbp->f_bsize   == sbi->cluster_size`
      * `sbp->f_blocks  == (sbi->num_clusters >= 2u)
                            ? sbi->num_clusters - 2u : 0u`
      * `sbp->f_bfree   == sbp->f_blocks > sbi->used_clusters
                            ? sbp->f_blocks - sbi->used_clusters : 0u`
        when `sbi->used_clusters != EXFAT_CLUSTERS_UNTRACKED`;
        otherwise `sbp->f_bfree == 0` (untracked → "no free reported")
      * `sbp->f_bavail  == sbp->f_bfree`
      * `sbp->f_namelen == EXFAT_MAX_NAME_LEN`
      * Every other field is zero (via memset_s before population).
  - sbi is not modified.
  - Returns 0.

**Case 2 (Statfs validation failure)**:
  - `mount == NULL`, OR `mount->data == NULL`, OR `sbp == NULL`.
  - No I/O; no memory mutation (the bug-tier `memset_s` does NOT run on
    a NULL `sbp`).
  - Returns `-EINVAL`.

**Case 3 (Sync no-op)**:
  - VfsExfatSync ignores its argument and returns 0 unconditionally.
  - No side effects. No I/O.
  - Returns 0.

**Invariant** (id=exfat-mount-ops-rest-no-locks):
  Neither VfsExfatStatfs nor VfsExfatSync acquires sbi->s_lock,
  sbi->bitmap_lock, or any other LiteOS-A primitive. They are
  spinlock-safe but not strictly required to be called from spinlock
  context — typical callers are syscall handlers (statfs(2), sync(2))
  that already run in process context.

**Invariant** (id=exfat-mount-ops-rest-readonly-sbi):
  VfsExfatStatfs treats sbi as read-only. No field of sbi is written
  to. The `used_clusters` field may be observed in a torn state (single
  uint32 read while another thread updates it via alloc_cluster /
  free_cluster under bitmap_lock); v1 accepts this as a transient
  inaccuracy in the reported f_bfree, not a correctness bug.

**Invariant** (id=exfat-mount-ops-rest-untracked-used-as-zero-free):
  When `sbi->used_clusters == EXFAT_CLUSTERS_UNTRACKED` (~0u, the
  sentinel for "not yet counted"), VfsExfatStatfs reports
  `f_bfree == 0` (and therefore `f_bavail == 0`) rather than
  performing the wrap-around arithmetic
  `f_blocks - (uint32_t)~0u`. This protects callers from a misleading
  "huge free space" report during the transient window before
  exfat_count_used_clusters is invoked. Future stages MAY eagerly
  populate used_clusters at mount time; the invariant exists to make
  the current behaviour explicit.

**Invariant** (id=exfat-mount-ops-rest-zeroed-statfs):
  Before populating, VfsExfatStatfs `memset_s`-zeroes the entire
  `*sbp` buffer. Fields not explicitly set by exFAT (f_files / f_ffree
  / f_fsid / f_frsize / f_flags / f_spare) are guaranteed to be 0.
  This matches Linux exfat's `generic_fillattr` zeroing convention and
  prevents stale-stack-data leaks to userspace.

**Invariant** (id=exfat-mount-ops-rest-sync-noop-stable):
  VfsExfatSync's `return 0` is a stable contract for v1. Any future
  evolution that introduces a write-back buffer cache MUST flip this
  to a real flush WITHOUT changing the function signature or the
  static-init in `g_exfatMountOps`. The invariant exists to flag the
  v1 limitation explicitly to future maintainers.

**System Algorithm**:

**VfsExfatStatfs**:
1. Validate `mount`, `mount->data`, `sbp` non-NULL. On any NULL return
   -EINVAL (Case 2).
2. Cast `sbi = (exfat_sb_info *)mount->data`.
3. `memset_s(sbp, sizeof(*sbp), 0, sizeof(*sbp))` — zero the entire
   buffer per invariant `exfat-mount-ops-rest-zeroed-statfs`.
4. Populate:
   - `sbp->f_type    = EXFAT_SUPER_MAGIC`.
   - `sbp->f_bsize   = sbi->cluster_size`.
   - `sbp->f_blocks  = (sbi->num_clusters >= 2u) ?
                        (sbi->num_clusters - 2u) : 0u`.
   - If `sbi->used_clusters == EXFAT_CLUSTERS_UNTRACKED`:
       `sbp->f_bfree = 0`.
     Else:
       `sbp->f_bfree = (sbp->f_blocks > sbi->used_clusters) ?
                       (sbp->f_blocks - sbi->used_clusters) : 0u`.
   - `sbp->f_bavail  = sbp->f_bfree`.
   - `sbp->f_namelen = EXFAT_MAX_NAME_LEN`.
5. Return 0 (Case 1).

**VfsExfatSync**:
1. Cast `mount` to void to silence unused-parameter warnings.
2. Return 0 (Case 3).
