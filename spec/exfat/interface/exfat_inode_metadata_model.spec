[PROMPT]

Provide complete additions to `fs/exfat/exfat_inode.c`,
`fs/exfat/include/exfat.h`, and `fs/exfat/include/exfat_raw.h` that
implement the inode-level metadata model used by all
create / unlink / rmdir / rename / mkdir / truncate paths. Helpers
MUST live in `fs/exfat/exfat_inode.c` (per user clarification — no
new TU like `fs/exfat/util/exfat_time.c`). Output a single C code
block per file; each block is APPENDED to the existing TU. This is
a HELPER stage: no VFS callback is wired here. `VfsExfatSetattr` is
explicitly out of scope.

Intent: replace the ad-hoc static `exfat_set_entry_time_now` helper
currently embedded in `fs/exfat/exfat_inode.c` (lines 703-761) with a
spec-anchored model that exposes per-field encode/decode plus six
touch primitives + a version bump primitive. This unlocks the new
`unlink`, `rename`, and (later) `setattr` stages, all of which need
consistent metadata mutation under exactly one `version` bump.

Struct surgery in scope: the stage extends `exfat_inode_info` (in
`fs/exfat/include/exfat.h`) by APPENDING three new fields
(`atime_sec`, `mtime_sec`, `ctime_sec`) AFTER the existing
`inode_lock` field — preserving every pre-existing field offset.
Header surgery in scope: the stage extends `fs/exfat/include/exfat_raw.h`
by adding two epoch boundary constants
(`EXFAT_MIN_TIMESTAMP_SECS`, `EXFAT_MAX_TIMESTAMP_SECS`) so they
are reachable by callers without re-opening common.header.

The pre-existing `exfat_inode_info::version` field is REUSED (not
renamed, not duplicated) as the in-memory monotonic counter — locked
in by user clarification 1 of this session.

[RELY]
```c
/* On-disk layout — already approved in dentry stage common.header.
 *
 * Two 16-bit fields encode an exFAT timestamp:
 *   date  = (year-1980 << 9) | (month << 5) | mday
 *   time  = (hour << 11) | (min << 5) | (sec / 2)
 * cs (10ms increment, 0..199) is present for create / modify only —
 *   exFAT spec states access_time has no cs slot.
 * tz: signed 7-bit offset in 15-min units, plus EXFAT_TZ_VALID bit. */
struct exfat_dentry;                       // defined in exfat_raw.h

#define EXFAT_TZ_VALID                  (1u << 7)

/* In-memory inode is frozen in common.header at the start of this
 * session. This stage's invariants make struct extension safe by
 * appending fields at the END (see exfat-meta-struct-extension-end-append). */
typedef struct exfat_inode_info exfat_inode_info;

/* Mount options carry the user-supplied UTC offset used by Linux
 * exfat when EXFAT_TZ_VALID is clear in a dentry tz byte. Already
 * frozen in common.header. */
typedef struct exfat_sb_info exfat_sb_info;
/* exfat_sb_info::options.time_offset is in minutes,
 * range [-24*60, 24*60]. */

/* POSIX wall-clock entry point — LiteOS-A compat/posix/src/time.c:1203
 * already implements this on top of gettimeofday(). Returns -1 on
 * failure; positive seconds-since-Unix-epoch on success. */
extern time_t time(time_t *t);             // posix/include/time.h
```

[GUARANTEE]
```c
/* ---------------------------------------------------------------------
 * On-disk timestamp encode/decode primitives. Mirror Linux exfat
 * misc.c::exfat_get_entry_time / exfat_set_entry_time but use uint64_t
 * epoch-seconds (NOT timespec64) per user clarification 3.
 *
 * Calling convention (all primitives below):
 *   - Pure: no IO, no allocation, no lock acquisition.
 *   - Out-of-range epoch values are CLAMPED to
 *     [EXFAT_MIN_TIMESTAMP_SECS, EXFAT_MAX_TIMESTAMP_SECS] before
 *     packing — callers never see < 1980 or > 2107.
 *   - Three encode flavors split on whether sub-second info is
 *     preservable on disk:
 *       atime  → no cs (Microsoft spec: access has no 10ms field)
 *               + round_down(sec, 2);
 *       mtime  → cs encodes (sec & 1) ? 100 : 0 (preserves odd sec);
 *       ctime  → cs encodes (sec & 1) ? 100 : 0 (preserves odd sec).
 *   - All three encoders emit EXFAT_TZ_VALID with offset 0 in v1
 *     ("stored time IS UTC"). The sbi parameter is accepted for
 *     forward-compat symmetry per audit finding 001.
 *
 *   - Decoder is Linux-faithful (fs/exfat/misc.c:94-99):
 *       if (tz & EXFAT_TZ_VALID): subtract 7-bit signed offset *
 *                                 15 * 60 seconds (sign-extended);
 *       else:                     subtract sbi->options.time_offset
 *                                 minutes (Linux fall-back path). */

void exfat_encode_atime(const exfat_sb_info *sbi, uint64_t epoch_sec,
                        uint16_t *time_out, uint16_t *date_out,
                        uint8_t  *tz_out);

void exfat_encode_mtime(const exfat_sb_info *sbi, uint64_t epoch_sec,
                        uint16_t *time_out, uint16_t *date_out,
                        uint8_t  *cs_out,   uint8_t  *tz_out);

void exfat_encode_ctime(const exfat_sb_info *sbi, uint64_t epoch_sec,
                        uint16_t *time_out, uint16_t *date_out,
                        uint8_t  *cs_out,   uint8_t  *tz_out);

uint64_t exfat_decode_entry_time(const exfat_sb_info *sbi,
                                 uint16_t time_le, uint16_t date_le,
                                 uint8_t  cs,       uint8_t  tz);

/* ---------------------------------------------------------------------
 * Wall-clock now() helper. Pulls POSIX time(NULL); on failure (RTC
 * not initialized → returns -1 or 0) returns EXFAT_MIN_TIMESTAMP_SECS
 * so the value stays in-range and the date packing is well-defined.
 * Side effects: only the POSIX time() spinlock-protected globals.
 * Safe to call from any non-spinlock context. */
uint64_t exfat_now_seconds(void);

/* atime granularity helper — rounds DOWN to even seconds (mirrors
 * Linux exfat_truncate_atime: "atime has 2-second resolution"). Pure. */
uint64_t exfat_truncate_atime_seconds(uint64_t epoch_sec);

/* ---------------------------------------------------------------------
 * In-memory metadata "touch" primitives. Each updates one or more
 * named field(s) of `ei` to the current wall-clock time and bumps
 * ei->version EXACTLY ONCE per call.
 *
 * The six touch flavors cover every Linux namei mutation pattern
 * surveyed (per audit finding 003):
 *
 *   touch_atime       — atime + ctime         (read-side metadata refresh)
 *   touch_mtime       — mtime + ctime         (data write completion)
 *   touch_ctime       — ctime only            (mode/owner change without data)
 *   touch_atime_mtime — atime + mtime, NO ctime
 *                       (Linux unlink/rmdir parent + victim pattern)
 *   touch_mtime_ctime — mtime + ctime, NO atime
 *                       (Linux rename old_dir pattern)
 *   touch_now         — atime + mtime + ctime (create/mkdir new inode +
 *                       Linux rename new_dir)
 *
 * Calling convention:
 *   - Caller MUST hold ei->inode_lock OR have proven ei is private
 *     (e.g., still inside exfat_inode_alloc before publication).
 *   - No IO, no allocation, no recursive lock acquisition; helpers
 *     do not call LOS_Mux*.
 *   - Single-bumper guarantee: each call increments ei->version
 *     exactly once even when writing multiple fields.
 *   - Atime values pass through exfat_truncate_atime_seconds before
 *     storing (invariant exfat-meta-atime-2s-granularity). Mtime and
 *     ctime are stored at full second precision. */

void exfat_inode_touch_atime      (exfat_inode_info *ei);
void exfat_inode_touch_mtime      (exfat_inode_info *ei);
void exfat_inode_touch_ctime      (exfat_inode_info *ei);
void exfat_inode_touch_atime_mtime(exfat_inode_info *ei);
void exfat_inode_touch_mtime_ctime(exfat_inode_info *ei);
void exfat_inode_touch_now        (exfat_inode_info *ei);

/* Version-only bump. Use case: namespace mutations
 * (unlink/rmdir/rename) that already touched timestamps via one of
 * the touch_* helpers and additionally need to invalidate cached
 * lookups. Do NOT pair with another touch_* on the same ei in the
 * same critical section — that double-bumps. Caller MUST hold
 * ei->inode_lock. */
void exfat_inode_bump_version(exfat_inode_info *ei);

/* ---------------------------------------------------------------------
 * On-disk → in-memory metadata load. Reads the eleven timestamp bytes
 * from a FILE primary dentry into ei->atime_sec / mtime_sec / ctime_sec.
 * Called by lookup / readdir result inode-load paths and by the
 * future getattr ei-refresh path.
 *
 * Pure: no IO, no alloc, no locks. Returns 0; never fails — out-of-
 * range / malformed packed values are silently clamped to
 * EXFAT_MIN_TIMESTAMP_SECS, since dentry-set integrity has already
 * been validated by exfat_validate_dentry_set upstream. ei->version
 * is NEVER mutated by load_metadata (pure read-back). */
int exfat_inode_load_metadata(const exfat_sb_info *sbi,
                              exfat_inode_info *ei,
                              const struct exfat_dentry *file_dentry);

/* In-memory → on-disk metadata store. Writes ei->atime_sec / mtime_sec
 * / ctime_sec into the FILE primary dentry's eleven timestamp bytes.
 *
 * ctime → create_time slot mapping (see invariant
 * exfat-meta-ctime-on-create-slot and the Note in [SPECIFICATION]).
 * Pure. Returns 0; never fails.
 *
 * The dentry's `checksum` field is NOT updated here — set-checksum is
 * the dentry-set-write stage's responsibility. */
int exfat_inode_store_metadata(const exfat_sb_info *sbi,
                               const exfat_inode_info *ei,
                               struct exfat_dentry *file_dentry);

/* Derived nlink. exFAT has no on-disk nlink field per Microsoft spec.
 *
 * Files always return 1; directories return MAX(ei->num_subdirs, 2u)
 * — `num_subdirs` is already initialized to EXFAT_MIN_SUBDIR (=2) at
 * directory inode creation per the inode_alloc / mkdir stages, and
 * is incremented per child directory by future evolve stages, so the
 * field IS the link count. The MAX() is purely defensive against a
 * malformed inode that escaped inode_alloc's invariant
 * (per audit finding 005, previous draft's `2 + num_subdirs` would
 * have double-counted). Pure. */
uint32_t exfat_inode_get_nlink(const exfat_inode_info *ei);
```

[SPECIFICATION]

**Pre-Condition**:
  - `ei != NULL`; `ei->inode_lock` is initialized.
  - For touch_*/bump_version: caller holds `ei->inode_lock` OR
    proves `ei` is private to the caller (unpublished allocation).
  - For load_metadata / store_metadata: `file_dentry != NULL` and
    `file_dentry->type == EXFAT_FILE` is the caller's contract; this
    layer does not re-validate (dentry_iter
    `exfat_validate_dentry_set` already does).
  - For all helpers taking `sbi`: `sbi != NULL`; `sbi->options.time_offset`
    is well-formed (set by mount / options stages).

**Note on ctime → create-slot mapping (v1 design)**:
  exFAT has no dedicated ctime field. v1 stores `ei->ctime_sec` into the
  FILE primary dentry's `create_time` slot. Linux mounters will read this
  as the wrong birth time. Acceptable because LiteOS-A `struct stat`
  surfaces no `st_birthtime`. A future setattr stage that adds an explicit
  on-inode `crtime_sec` field MUST revisit this. See invariant
  `exfat-meta-ctime-on-create-slot` for the binding rule.

**Post-Condition**:

  **Case 1 (encode/decode round-trip)**:
    - For any in-range even epoch `s`, `decode(encode_*(...).time, .date,
      .cs, .tz)` returns `s` (atime path on even s, mtime/ctime path).
    - For odd `s` on mtime/ctime path, round-trip preserves `s` via the
      cs parity bit (`(s & 1) * 100`); decode adds `cs / 100` back.
    - For atime path on odd `s`, round-trip returns `round_down(s, 2)`
      (atime has no cs slot; Linux-faithful).
    - For out-of-range `s`, round-trip returns `clamp(s, MIN, MAX)`
      rounded by slot semantics.
    - Encode always emits `tz_out == EXFAT_TZ_VALID` with 7-bit offset 0
      ("stored time IS UTC").

  **Case 2 (touch_atime)**:
    - `ei->atime_sec = exfat_truncate_atime_seconds(now_s)` where
      `now_s = exfat_now_seconds()`.
    - `ei->ctime_sec = now_s` (POSIX: any metadata change bumps ctime).
    - `ei->version` increments by 1 (mod 2^32).
    - `ei->mtime_sec` is unchanged.
    Returns: void.

  **Case 3 (touch_mtime)**:
    - `ei->mtime_sec = now_s`; `ei->ctime_sec = now_s`.
    - `ei->version` increments by 1.
    - `ei->atime_sec` is unchanged.
    Returns: void.

  **Case 4 (touch_ctime)**:
    - `ei->ctime_sec = now_s`; no atime/mtime change.
    - `ei->version` increments by 1.
    Returns: void.

  **Case 5 (touch_atime_mtime)**:
    - `ei->atime_sec = exfat_truncate_atime_seconds(now_s)`.
    - `ei->mtime_sec = now_s`.
    - `ei->ctime_sec` is **unchanged** (mirrors Linux unlink/rmdir on
      parent dir and on the victim — Linux does not bump ctime when only
      the directory entry table mutates).
    - `ei->version` increments by 1.
    Returns: void.

  **Case 6 (touch_mtime_ctime)**:
    - `ei->mtime_sec = now_s`; `ei->ctime_sec = now_s`.
    - `ei->atime_sec` is **unchanged** (mirrors Linux rename on old_dir).
    - `ei->version` increments by 1.
    Returns: void.

  **Case 7 (touch_now)**:
    - `ei->atime_sec = exfat_truncate_atime_seconds(now_s)`.
    - `ei->mtime_sec = now_s`; `ei->ctime_sec = now_s`.
    - `ei->version` increments by 1.
    Returns: void.

  **Case 8 (bump_version)**:
    - `ei->version` increments by 1 (mod 2^32). No timestamp field is
      written.
    Returns: void.

  **Case 9 (load_metadata)**:
    - `ei->atime_sec = decode(.access_time, .access_date, /*cs=*/0,
      .access_tz)`.
    - `ei->mtime_sec = decode(.modify_time, .modify_date,
      .modify_time_cs, .modify_tz)`.
    - `ei->ctime_sec = decode(.create_time, .create_date,
      .create_time_cs, .create_tz)` (ctime → create-slot, see Note above).
    - `ei->version` is NOT changed (read-back, not mutation).
    Returns: 0 (never fails; clamping handles malformed inputs).

  **Case 10 (store_metadata)**:
    - `.access_time/.access_date/.access_tz` ←
      `exfat_encode_atime(sbi, ei->atime_sec, ...)`.
    - `.modify_time/.modify_date/.modify_time_cs/.modify_tz` ←
      `exfat_encode_mtime(sbi, ei->mtime_sec, ...)`.
    - `.create_time/.create_date/.create_time_cs/.create_tz` ←
      `exfat_encode_ctime(sbi, ei->ctime_sec, ...)` (ctime → create-slot).
    - `file_dentry->dentry.file.checksum` is **not** updated here.
    - All three `*_tz` bytes get `EXFAT_TZ_VALID`; create/modify `*_cs`
      get `(sec & 1) ? 100 : 0`; access has no cs.
    Returns: 0.

  **Case 11 (get_nlink)**:
    - `ei->type == TYPE_FILE` → return 1.
    - `ei->type == TYPE_DIR` → return `MAX(ei->num_subdirs, 2u)`.
      Rationale: `num_subdirs` is initialized to `EXFAT_MIN_SUBDIR` (=2)
      by inode_alloc/mkdir and incremented per-child by future evolve
      stages, so it IS the link count; the MAX clamp is defensive only.
    - Any other type → return 1 (defensive default; should not occur
      post inode_alloc).

**Invariant** (id=`exfat-meta-encode-decode-roundtrip`):
  For any `s ∈ [EXFAT_MIN_TIMESTAMP_SECS, EXFAT_MAX_TIMESTAMP_SECS]`:
    - mtime/ctime path: `decode(sbi, encode_mtime(sbi, s).time,
      .date, .cs, .tz) == s` for both even and odd `s`.
    - atime path: `decode(sbi, encode_atime(sbi, s).time, .date,
      cs=0, .tz) == round_down(s, 2)` (sub-second info intentionally
      lost — Linux-faithful, mirrors `exfat_truncate_atime`).
  Out-of-range `s` clamps to MIN/MAX, then round-trips per slot
  semantics. The encoder always emits `EXFAT_TZ_VALID | offset=0` so
  decoding the same dentry on a future system that interprets tz
  literally still recovers UTC.

**Invariant** (id=`exfat-meta-atime-2s-granularity`):
  Whenever atime is written (encode_atime, touch_atime,
  touch_atime_mtime, touch_now), the persisted second value is
  `round_down(s, 2)`. This mirrors Linux exfat_truncate_atime() and
  the exFAT spec stating access time has no 10ms increment field.
  mtime and ctime are stored at full 1-second precision via the cs
  parity bit.

**Invariant** (id=`exfat-meta-tz-encode-utc`):
  v1 always emits `EXFAT_TZ_VALID` with the 7-bit signed offset payload
  set to 0 ("stored time is UTC"). The encode path does NOT consult
  `sbi->options.time_offset` — only the decode path uses it (and only
  when EXFAT_TZ_VALID is clear in the read tz byte). The asymmetry is
  Linux-faithful: encode side writes well-formed tz=VALID, decode side
  honors whatever tz the on-disk dentry carries. A later setattr
  stage may extend encode to honor `options.time_offset` once
  symmetric setattr semantics are needed.

**Invariant** (id=`exfat-meta-tz-decode-linux-faithful`):
  `exfat_decode_entry_time` mirrors Linux fs/exfat/misc.c:94-99
  EXACTLY:
    - if `(tz & EXFAT_TZ_VALID)`: subtract the 7-bit signed offset
      (sign-extended: 0x40..0x7F → negative,  0x00..0x3F → positive)
      times `15 * 60` seconds.
    - else: subtract `sbi->options.time_offset * 60` seconds (Linux
      `exfat_get_entry_time` else branch).
  Any out-of-range result is clamped to
  `[EXFAT_MIN_TIMESTAMP_SECS, EXFAT_MAX_TIMESTAMP_SECS]` so callers
  always see well-formed dates.

**Invariant** (id=`exfat-meta-version-monotonic-singlebump`):
  Every touch_* call AND every bump_version call increments
  `ei->version` by exactly 1 (mod 2^32). When a touch_* writes
  multiple time fields, the bump still happens exactly once. Read
  paths (load_metadata, get_nlink, decoders, encoders) NEVER mutate
  version. Wraparound at 2^32 is acceptable for a single-host
  monotonic counter — collision over a full uint32 cycle requires
  more than 2^32 mutations on the same inode without unmount, which
  exceeds practical kernel lifetimes.

**Invariant** (id=`exfat-meta-no-io-no-alloc`):
  None of this stage's exports issues `los_part_*` / `los_disk_*` IO,
  calls `LOS_MemAlloc` / `LOS_MemFree`, or acquires any lock. Locks
  are caller's responsibility (consistent with the helper-layer
  pattern in `dentry_iter`, `fat_chain`, `inode_alloc`, `vol_flags`
  stages). The two-phase trigger therefore does NOT fire here — this
  spec has no `## Refine Prompt` section by design.

**Invariant** (id=`exfat-meta-rtc-fallback`):
  `exfat_now_seconds()` returns `EXFAT_MIN_TIMESTAMP_SECS`
  (1980-01-01 00:00:00Z) when `time(NULL)` returns a value `<= 0`
  (POSIX failure path or RTC not yet initialized). This keeps the
  encoded date-bits inside the exFAT spec range
  `[EXFAT_MIN_TIMESTAMP_SECS, EXFAT_MAX_TIMESTAMP_SECS]`. The
  fallback is silent (no PRINT_ERR), matching FatFs GET_FATTIME
  behavior on systems without an RTC.

**Invariant** (id=`exfat-meta-nlink-derived-not-stored`):
  `exfat_inode_get_nlink` is a pure derivation from `ei->type` and
  `ei->num_subdirs`; nlink is NEVER stored in `ei` because exFAT has
  no on-disk nlink field per Microsoft spec. Stages that need nlink
  for `struct stat` MUST call this helper, NEVER read `ei->nlink`
  (no such member). For directories, `ei->num_subdirs` already
  encodes EXFAT_MIN_SUBDIR (=2) at allocation and is incremented per
  child; the `MAX(.., 2u)` in the helper is a defensive clamp only.

**Invariant** (id=`exfat-meta-ctime-on-create-slot`):
  Because exFAT has no dedicated ctime field, this stage encodes
  `ei->ctime_sec` into the FILE primary dentry's create_time /
  create_date / create_time_cs / create_tz slot. Re-loading via
  load_metadata then maps create_time → ctime_sec. Linux exFAT keeps
  a separate `i_crtime` field; the divergence is acceptable in v1
  because LiteOS-A `struct stat` surfaces no birth time. A later
  setattr stage MUST revisit and add an explicit on-inode `crtime_sec`
  field. See the "Note on ctime → create-slot mapping" in
  [SPECIFICATION] for the v1 trade-off rationale.

**Invariant** (id=`exfat-meta-struct-extension-end-append`):
  `exfat_inode_info` (frozen in common.header) has the layout:
    `dir / entry / type / attr / start_clu / flags / size /
     valid_size / i_size_ondisk / i_pos / version / num_subdirs /
     inode_lock`.
  This stage appends `atime_sec` / `mtime_sec` / `ctime_sec` (each
  uint64_t) AFTER `inode_lock`, at the END of the struct. No
  pre-existing field's offset is shifted. The common.header `[RELY]`
  segment will be evolved in lockstep when this stage's
  code_gen_approve runs; per server-side `_sync_common_header`, any
  consumer compiled against the post-extension common.header sees the
  new fields, and any prior approved spec sees only the fields it
  referenced (offsets unchanged).

**Invariant** (id=`exfat-meta-raw-h-additions`):
  This stage's code_gen_approve extends `fs/exfat/include/exfat_raw.h`
  by adding two epoch boundary constants:
    `#define EXFAT_MIN_TIMESTAMP_SECS    315532800LL  /* 1980-01-01Z */`
    `#define EXFAT_MAX_TIMESTAMP_SECS    4354819199LL /* 2107-12-31Z */`
  These mirror Linux `fs/exfat/exfat_raw.h:163-166`. They are NOT in
  `[RELY]` — this stage is the one that introduces them.

**Invariant** (id=`exfat-meta-le16-byte-order`):
  All multi-byte timestamp fields on disk are little-endian per
  Microsoft exFAT spec. Encode helpers MUST go through `HOST_TO_LE16`
  before writing `file_dentry->dentry.file.{access_time, access_date,
  modify_time, modify_date, create_time, create_date}`; decode MUST
  go through `LE16_TO_HOST` when reading the same fields. The 8-bit
  fields (`*_cs`, `*_tz`) are byte-addressable and need no conversion.
  Mirrors Linux `fs/exfat/misc.c:80-81` (`le16_to_cpu`) and `:113-114`
  (`cpu_to_le16`); aligns with prior approved invariants
  `exfat-dentry-parse-byte-order` and `exfat-fat-chain-le-host-only`.
  On LiteOS-A ARM-LE host the macro is identity; the conversion
  contract is therefore a portability hardening for future BE ports,
  not a runtime correctness requirement on the current platform.

**Invariant** (id=`exfat-meta-no-spinlock-callsite`):
  All exports may sleep transitively only through `time()` →
  `gettimeofday()` (LiteOS-A compat/posix/src/time.c), which is a
  non-blocking memory read protected by a global time spinlock —
  potentially blocking for at most one spinlock window, but does NOT
  schedule. They are therefore safe to call from any non-spin-lock
  context. Callers in spin-lock context MUST NOT use
  `exfat_now_seconds` / `touch_*` — pre-compute the timestamp before
  entering the spin section. This is consistent with
  `exfat-fat-chain-no-spinlock-callsite` and
  `exfat-dentry-iter-no-spinlock-callsite`.

**Invariant** (id=`exfat-meta-civil-time-epoch-self-contained`):
  Encode/decode internally compute the (year, mon, mday, hour, min,
  sec) ↔ epoch_seconds round-trip using a stage-local helper
  (LLM-named, but contractually equivalent to the well-known civil-
  time formula: see Howard Hinnant's "date" algorithms — pure
  arithmetic, no calls to mktime/timegm/mktime64). Rationale (audit
  finding 009): LiteOS-A kernel does not provide a portable timegm,
  and the shell `mktime64` is a private macro not safe to depend on.
  The local helper must be ~30 LoC, infallible, and unit-tested in
  Layer T (round-trip ten sentinel dates: 1980-01-01, 2000-02-29 leap,
  2100-02-28 non-leap, 2107-12-31, plus six random in-range).

**System Algorithm**:

  Phase 1 — encode_atime(sbi, epoch_s):
    s ← clamp(epoch_s, EXFAT_MIN_TIMESTAMP_SECS, EXFAT_MAX_TIMESTAMP_SECS)
    s ← round_down(s, 2)                                   // 2-sec atime
    (year, mon, mday, hour, min, sec) ← civil(s)           // local helper
    *date_out  ← ((year - 1980) << 9) | (mon << 5) | mday
    *time_out  ← (hour << 11) | (min << 5) | (sec >> 1)
    *tz_out    ← EXFAT_TZ_VALID                            // offset 0

  Phase 2 — encode_mtime(sbi, epoch_s) [identical for encode_ctime]:
    s ← clamp(epoch_s, MIN, MAX)
    (year, mon, mday, hour, min, sec) ← civil(s)
    *date_out  ← ((year - 1980) << 9) | (mon << 5) | mday
    *time_out  ← (hour << 11) | (min << 5) | (sec >> 1)
    *cs_out    ← (sec & 1) ? 100 : 0                       // odd-sec preserve
    *tz_out    ← EXFAT_TZ_VALID

  Phase 3 — decode_entry_time(sbi, time, date, cs, tz):
    year ← (date >> 9) + 1980
    mon  ← (date >> 5) & 0x000F
    mday ←  date       & 0x001F
    hour ←  time >> 11
    min  ← (time >> 5) & 0x003F
    sec  ← (time & 0x001F) << 1
    s    ← epoch(year, mon, mday, hour, min, sec)          // local helper
    if (cs > 0 && cs < 200): s += cs / 100                 // odd-sec recover
    if (tz & EXFAT_TZ_VALID):
        off7 ← tz & 0x7F
        adj  ← (off7 < 0x40) ? off7 : (off7 - 0x80)        // sign-extend
        s -= adj * (15 * 60)                               // tz minutes
    else:
        s -= sbi->options.time_offset * 60                 // Linux else
    return clamp(s, MIN, MAX)

  Phase 4 — generic touch(ei, mask):
    /* Each helper sets `mask` to a fixed combination, then runs this
     * single algorithm.  Single-bumper guarantee: `version + 1`
     * happens exactly once regardless of how many fields the mask
     * selects (per invariant exfat-meta-version-monotonic-singlebump). */
    s ← exfat_now_seconds()
    if (mask & ATIME): ei->atime_sec ← exfat_truncate_atime_seconds(s)
    if (mask & MTIME): ei->mtime_sec ← s
    if (mask & CTIME): ei->ctime_sec ← s
    ei->version  ← ei->version + 1                         // wraparound OK

  Phase 4a — touch_atime(ei):        touch(ei, ATIME | CTIME)
  Phase 4b — touch_mtime(ei):        touch(ei, MTIME | CTIME)
  Phase 4c — touch_ctime(ei):        touch(ei, CTIME)
  Phase 4d — touch_atime_mtime(ei):  touch(ei, ATIME | MTIME)
  Phase 4e — touch_mtime_ctime(ei):  touch(ei, MTIME | CTIME)
  Phase 4f — touch_now(ei):          touch(ei, ATIME | MTIME | CTIME)

  Phase 4g — bump_version(ei):
    /* No timestamp write; sole side effect is the version bump. */
    ei->version  ← ei->version + 1                         // wraparound OK

  Phase 5 — load_metadata(sbi, ei, file_dentry):
    fe ← &file_dentry->dentry.file
    ei->atime_sec ← decode(sbi, fe->access_time, fe->access_date,
                           /*cs=*/0, fe->access_tz)
    ei->mtime_sec ← decode(sbi, fe->modify_time, fe->modify_date,
                           fe->modify_time_cs, fe->modify_tz)
    ei->ctime_sec ← decode(sbi, fe->create_time, fe->create_date,
                           fe->create_time_cs, fe->create_tz)
    /* ei->version NOT mutated — this is read-back */
    return 0

  Phase 6 — store_metadata(sbi, ei, file_dentry):
    fe ← &file_dentry->dentry.file
    encode_atime(sbi, ei->atime_sec,
                 &fe->access_time, &fe->access_date, &fe->access_tz)
    encode_mtime(sbi, ei->mtime_sec, &fe->modify_time, &fe->modify_date,
                 &fe->modify_time_cs, &fe->modify_tz)
    encode_ctime(sbi, ei->ctime_sec, &fe->create_time, &fe->create_date,
                 &fe->create_time_cs, &fe->create_tz)
    /* fe->checksum NOT touched — caller's dentry-set-write does it */
    return 0

  Phase 7 — get_nlink(ei):
    if (ei->type == TYPE_FILE): return 1
    if (ei->type == TYPE_DIR):  return MAX(ei->num_subdirs, 2u)
    return 1                                               // defensive
