[PROMPT]
Provide complete `exfat_inode.c` implementation of `exfat_init_dir_entry`. Only
include `<exfat.h>` as the header; output a single C code block containing only
this function. No surrounding prose.

The function writes the FILE primary dentry (type 0x85) and STREAM secondary
dentry (type 0xC0) at slot `entry` and `entry+1` of directory chain `p_dir`,
respectively. It is called by `exfat_add_entry` after a free dentry slot has been
reserved via `exfat_alloc_dentry_slot`. On-disk persistence is handled by
`exfat_set_dentry` (Stage 4a); no buffer cache, no buffer_head. The `num_ext`,
`checksum`, `name_len`, and `name_hash` fields are left at zero — they are
patched in by the immediately following `exfat_init_ext_entry` call. Timestamps
are captured from the current system clock via `exfat_set_entry_time_now`.

[RELY]
```c
/* exfat_raw.h constants (available via exfat.h) */
#define EXFAT_FILE              0x85    /* primary dentry type: file or dir */
#define EXFAT_STREAM            0xC0    /* secondary dentry type: stream ext */
#define ATTR_SUBDIR             0x0010  /* on-disk attribute: directory */
#define ATTR_ARCHIVE            0x0020  /* on-disk attribute: regular file */
#define ALLOC_FAT_CHAIN         0x01u   /* stream.flags: FAT chain present */
#define ALLOC_NO_FAT_CHAIN      0x03u   /* stream.flags: contiguous, no FAT walk */
#define TYPE_DIR                0x0104  /* in-memory type: directory */
#define TYPE_FILE               0x011F  /* in-memory type: regular file */

/* exfat_raw.h on-disk dentry union — packed, accessed via fep.dentry.file.*
 * and sep.dentry.stream.* sub-structs. Fields of interest:
 *   file:   num_ext (u8), checksum (u16), attr (u16),
 *           create_{time,date,tz,time_cs}, modify_{time,date,tz,time_cs},
 *           access_{time,date,tz}
 *   stream: flags (u8), name_len (u8), name_hash (u16),
 *           valid_size (u64le), size (u64le), start_clu (u32le)        */
struct exfat_dentry;   /* defined in exfat_raw.h, included via exfat.h */

/* securec: zero a stack dentry before populating it.
 * Returns EOK (0) on success; any other value → treat as -EIO. */
errno_t memset_s(void *dest, size_t destMax, int c, size_t count);

// Write a single dentry at index entry_idx in directory chain *dir to disk.
// Caller holds no lock (s_lock bracket is the caller's responsibility).
// Returns 0 on success, negative POSIX errno on IO failure.
extern int exfat_set_dentry(const exfat_sb_info *sbi,
                            const exfat_chain   *dir,
                            int                  entry_idx,
                            const struct exfat_dentry *in);

// Capture current system time and fill all three timestamp fields
// (create / modify / access, including _tz and _cs sub-fields) in *fep.
// Static inline defined in exfat_inode.c; no IO; no lock required.
static inline void exfat_set_entry_time_now(struct exfat_dentry *fep);
```

[GUARANTEE]
```c
/*
 * Calling convention:
 *   - Caller holds sbi->s_lock (write bracket from exfat_add_entry).
 *   - No additional lock acquired inside; no memory allocation.
 *   - Returns 0 on success, negative POSIX errno on failure.
 * Side effects on success:
 *   - Slot `entry`   on disk: FILE dentry (0x85) with attr, timestamps written.
 *   - Slot `entry+1` on disk: STREAM dentry (0xC0) with flags, size, start_clu.
 *   - num_ext / checksum / name_len / name_hash are 0 (filled by init_ext_entry).
 * Failure: if exfat_set_dentry(entry) fails, returns that error immediately;
 *   if exfat_set_dentry(entry+1) fails, the FILE dentry already written is NOT
 *   rolled back (orphan is cleaned by fsck on next mount — same as Linux).
 */
int exfat_init_dir_entry(exfat_sb_info       *sbi,
                         const exfat_chain   *p_dir,
                         int                  entry,
                         uint32_t             type,
                         uint32_t             start_clu,
                         uint64_t             size);
```

[SPECIFICATION]

**Pre-Condition**:
- `sbi != NULL`, `p_dir != NULL`, `entry >= 0`.
- `type` is exactly `TYPE_DIR` or `TYPE_FILE`; any other value is rejected.
- `p_dir` has at least `entry + 2` dentry slots reachable (ensured by the
  prior `exfat_alloc_dentry_slot` call in `exfat_add_entry`).
- Caller holds `sbi->s_lock` to serialise concurrent dentry writes.

**Post-Condition**:
**Case 1 (success)**:
- `exfat_set_dentry(sbi, p_dir, entry, &fep)` returned 0: slot `entry` on disk
  contains a FILE dentry (type byte `0x85`) with:
  - `attr` = `ATTR_SUBDIR` (0x0010) when `type == TYPE_DIR`;
    `ATTR_ARCHIVE` (0x0020) when `type == TYPE_FILE`.
  - `num_ext = 0`, `checksum = 0` (placeholder, overwritten by `init_ext_entry`).
  - `create_time`, `modify_time`, `access_time` (and their `_tz`, `_cs`
    sub-fields) set from a single system-time snapshot via
    `exfat_set_entry_time_now`; all three reflect the same instant.
- `exfat_set_dentry(sbi, p_dir, entry + 1, &sep)` returned 0: slot `entry+1`
  on disk contains a STREAM dentry (type byte `0xC0`) with:
  - `flags` = `ALLOC_FAT_CHAIN` (0x01) when `type == TYPE_FILE`;
    `ALLOC_NO_FAT_CHAIN` (0x03) when `type == TYPE_DIR`.
  - `valid_size = size`, `size = size`, `start_clu = start_clu`.
  - `name_len = 0`, `name_hash = 0` (placeholder, overwritten by `init_ext_entry`).
- Returns 0.

**Case 2 (argument validation failure)**:
- `sbi == NULL` OR `p_dir == NULL` OR `entry < 0` OR `type` is neither
  `TYPE_DIR` nor `TYPE_FILE`.
- No dentry is written; no IO attempted.
- Returns `-EINVAL`.

**Case 3 (memset_s failure on FILE dentry)**:
- `memset_s(&fep, …)` returns non-`EOK`.
- No dentry is written; no IO attempted.
- Returns `-EIO`.

**Case 4 (exfat_set_dentry failure on FILE dentry, slot `entry`)**:
- `exfat_set_dentry(sbi, p_dir, entry, &fep)` returns a negative errno `ret`.
- Slot `entry+1` is not written (early return).
- Returns `ret`.

**Case 5 (memset_s or exfat_set_dentry failure on STREAM dentry, slot `entry+1`)**:
- FILE dentry at slot `entry` has already been written and is NOT rolled back.
- Returns the negative errno from the failing call.

**Invariant** (id=exfat-init-dir-entry-time-set-on-success):
  On Case 1 success paths, the file dentry's create_time / modify_time /
access_time fields (including _tz and _cs sub-fields) are written via
`exfat_set_entry_time` from a current system-time snapshot. timestamp == 0
is NOT a legal success state (matches Linux behavior).

**Invariant** (id=exfat-init-dir-entry-stream-flags-by-type):
  Stream dentry's flags field is strictly type-determined: TYPE_DIR →
`ALLOC_NO_FAT_CHAIN`; TYPE_FILE → `ALLOC_FAT_CHAIN`. Rule mirrors Linux
`exfat_init_dir_entry` lines 484-486; TYPE_DIR single-cluster directories
don't need FAT walk (Linux uses NO_FAT meaning "size-derived"); TYPE_FILE
defaults to FAT chain to support truncate-extend growth.

**Invariant** (id=exfat-init-dir-entry-no-rollback):
  If writing slot `entry` succeeds but slot `entry+1` fails (Case 5), the FILE
dentry at slot `entry` is intentionally left on disk without rollback. Mirrors
Linux `exfat_init_dir_entry`; fsck recovery on next mount is the remediation.

**Invariant** (id=exfat-init-dir-entry-placeholder-zeros):
  `num_ext`, `checksum` (FILE) and `name_len`, `name_hash` (STREAM) are written as
0 by this function; they remain invalid until `exfat_init_ext_entry` patches them.
