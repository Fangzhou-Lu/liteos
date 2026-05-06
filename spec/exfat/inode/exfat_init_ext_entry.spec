[PROMPT]
Provide complete addition to `exfat_inode.c` that implements `exfat_init_ext_entry`.
Only include `exfat.h` as the header (already included by the surrounding translation
unit). Output a single C code block; no surrounding boilerplate.

`exfat_init_ext_entry` is called by `exfat_add_entry` after `exfat_init_dir_entry`.
It completes the on-disk dentry-set: patches `num_ext` in the file dentry, patches
`name_len`/`name_hash` in the stream dentry, writes num_entries-2 EXFAT_NAME (0xC1)
dentries (15 UTF-16 units each, zero-padded), then computes the SetChecksum over all
num_entries dentries and writes it back into the file dentry's `checksum` field.
Caller holds `sbi->s_lock`; helper is lock-free. No rollback on partial failure (Q3=a).

[RELY]
```c
/* Selected declarations from common.header; see full list there. */

typedef struct exfat_sb_info    exfat_sb_info;
typedef struct exfat_chain      exfat_chain;
struct exfat_dentry;            /* 32-byte union, defined in exfat_raw.h */

struct exfat_uni_name {
    uint16_t name[EXFAT_MAX_NAME_LEN + 1]; /* UTF-16LE sequence + NUL */
    uint16_t name_hash;                     /* chksum16(CS_DEFAULT) over name bytes */
    uint8_t  name_len;                      /* code-unit count, [1, 255] */
};

#define DENTRY_SIZE           32u
#define EXFAT_NAME            0xC1u   /* secondary name-extension dentry type */
#define EXFAT_FILE_NAME_LEN   15      /* UTF-16 units per EXFAT_NAME dentry */
#define EXFAT_DENTRY_SET_MAX  19      /* max dentries per set (common.header) */
#define EXFAT_MAX_NAME_LEN    255
#define CS_DIR_ENTRY          0       /* chksum type: skip bytes 2-3 of file dentry */
#define CS_DEFAULT            2       /* chksum type: include all bytes */

// Read one 32-byte dentry from on-disk slot entry_idx within dir chain.
// out_sector may be NULL. Returns 0 on success, negative errno on failure.
extern int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                            int entry_idx, struct exfat_dentry *out,
                            uint64_t *out_sector);

// Write one 32-byte dentry to on-disk slot entry_idx within dir chain.
// Returns 0 on success, negative errno on failure.
extern int exfat_set_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                            int entry_idx, const struct exfat_dentry *in);

// Compute exFAT dentry-set checksum over len bytes.
// type CS_DIR_ENTRY skips bytes 2-3 (the checksum field itself) on the first
// call; type CS_DEFAULT includes all bytes.  chksum is the running seed.
extern uint16_t exfat_calc_chksum16(const void *data, int len,
                                    uint16_t chksum, int type);
```

[GUARANTEE]
```c
/*
 * Calling convention:
 *   - Caller holds sbi->s_lock; this function is lock-free.
 *   - Must be called after exfat_init_dir_entry has successfully written
 *     the file dentry (slot entry) and stream dentry (slot entry+1).
 *   - p_uniname->name_len must equal the number of UTF-16 code units in
 *     p_uniname->name[] (not including any NUL terminator).
 *   - On success: slots entry..entry+num_entries-1 are fully written and
 *     their SetChecksum is consistent; returns 0.
 *   - On failure: returns negative errno; slots written before the error
 *     are not rolled back (Q3=a); checksum field may be absent or stale.
 */
int exfat_init_ext_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                         int entry, int num_entries,
                         const struct exfat_uni_name *p_uniname);
```

[SPECIFICATION]

**Pre-Condition**:
- `sbi != NULL`; `p_dir != NULL`; `p_uniname != NULL`.
- `3 <= num_entries <= EXFAT_DENTRY_SET_MAX` (i.e. [3, 19]).
- `entry >= 0`.
- `1 <= p_uniname->name_len <= EXFAT_MAX_NAME_LEN`.
- Caller holds `sbi->s_lock`.
- Slots `entry` (file dentry) and `entry+1` (stream dentry) have already
  been written by `exfat_init_dir_entry` with `num_ext = 0`, `checksum = 0`,
  `name_len = 0`, `name_hash = 0` as placeholders.

**Post-Condition**
**Case 1 (success)**:
- Slot `entry` (file dentry): `num_ext` = `num_entries - 1`; `checksum` =
  the SetChecksum computed in Step 4 below; all other fields unchanged.
- Slot `entry+1` (stream dentry): `name_len` = `p_uniname->name_len`;
  `name_hash` = `p_uniname->name_hash`; all other fields unchanged.
- Slots `entry+2` .. `entry+num_entries-1`: each is an EXFAT_NAME dentry
  with `type = 0xC1`, `flags = 0`, and `unicode_0_14[k]` equal to
  `p_uniname->name[(i-2)*15 + k]` for k ∈ [0, 15); code units beyond
  `name_len` are zero-padded (`0x0000`).
- SetChecksum covers all `num_entries` dentries: seed =
  `exfat_calc_chksum16(&file_dentry, 32, 0, CS_DIR_ENTRY)`; then for
  i = 1 .. num_entries-1: seed =
  `exfat_calc_chksum16(&dentry[i], 32, seed, CS_DEFAULT)`.
- Returns 0.

**Case 2 (-EINVAL)**:
- Any pointer is NULL, `num_entries` outside [3, 19], `entry < 0`, or
  `p_uniname->name_len == 0` — returns -EINVAL; no IO performed.

**Case 3 (-EIO or -ENOMEM, mid-operation)**:
- Any `exfat_get_dentry` or `exfat_set_dentry` call returns a non-zero
  error: the error is propagated immediately; slots already written are
  not restored (Q3=a).
- If the failure occurs before Step 4 completes, the file dentry's
  `checksum` field remains 0 (the placeholder written by
  `exfat_init_dir_entry`); the next `validate_dentry_set` call will
  reject this dentry-set with a checksum mismatch.
- The vol_dirty bracket in `VfsExfatMkdir` is the caller-level signal
  for fsck to repair any resulting orphan state.

**System Algorithm**:
Step 1 — patch file dentry: get slot `entry` → set `num_ext = num_entries-1`
  → write back. Error → return.

Step 2 — patch stream dentry: get slot `entry+1` → set `name_len` and
  `name_hash` from `p_uniname` → write back. Error → return.

Step 3 — write NAME dentries (i = 2..num_entries-1): zero-init a local
  `exfat_dentry`; set `type = EXFAT_NAME`, `flags = 0`; fill
  `unicode_0_14[k]` from `p_uniname->name[(i-2)*15 + k]` (zero if
  beyond name_len); write to slot `entry+i`. Error → return.

Step 4 — compute SetChecksum: re-read slot `entry`; seed =
  `exfat_calc_chksum16(&probe, 32, 0, CS_DIR_ENTRY)`; for i=1..num_entries-1:
  re-read slot `entry+i`; seed = `exfat_calc_chksum16(&probe, 32, seed,
  CS_DEFAULT)`. Error in any re-read → return.

Step 5 — write checksum: re-read slot `entry` → set `checksum = seed`
  → write back. Error → return.

**Invariant** (id=exfat-init-ext-entry-chksum-spans-all-N):
  `init_ext_entry`'s final chksum16 MUST span ALL `num_entries` dentries —
  seed from `exfat_calc_chksum16(fep, 32, 0, CS_DIR_ENTRY)`, then
  `i = 1..num_entries-1` accumulates with `CS_DEFAULT`. Missing any entry
  causes `validate_dentry_set` to flag chksum mismatch on next lookup, making
  the new directory inaccessible. This invariant locks down chksum coverage.

**Invariant** (id=exfat-init-ext-entry-name-chunk-zero-pad):
  Each EXFAT_NAME dentry's `unicode_0_14[k]` field MUST be `0x0000` for
  any k where `(i-2)*15 + k >= p_uniname->name_len`. The exFAT specification
  requires unused slots within a NAME dentry to be NUL — implementations that
  leave them uninitialized produce dentries whose bytes feed into the
  SetChecksum, causing irreproducible chksum values across writers.

**Invariant** (id=exfat-init-ext-entry-no-rollback):
  On any error in Steps 1–5, previously written dentry slots are not
  restored. The file dentry's `checksum` field being 0 or stale is the
  observable corruption signal; it is detectable by `validate_dentry_set`
  and repairable by fsck. This matches the no-rollback policy (Q3=a)
  established for `exfat_add_entry` and documented in the mkdir spec.
