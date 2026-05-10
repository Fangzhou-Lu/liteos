<!--
四类 on-disk 格式兼容性陷阱 — LLM 在代码生成时必须扫描；命中即必须 AskUserQuestion。
Four classes of format-compatibility traps the LLM MUST scan at code-gen
time; any positive hit → MUST AskUserQuestion. Loaded as {FORMAT_TRAPS}.

History/version: see ../CHANGELOG.md.
-->

When generating FS code that touches on-disk data, scan these four trap classes.
Any positive hit → MUST AskUserQuestion before proceeding.

## Trap 1: Checksum / hash algorithm mismatch

Linux FS often uses CRC variants or hashes that LiteOS-A does NOT provide a
compatible implementation for.

Concrete cases:
- **CRC-32 IEEE poly** (`LOS_Crc32` is this; matches Linux `crc32` for ext4 metadata)
- **CRC-32C Castagnoli** (used by EROFS, btrfs, F2FS for some metadata) —
 **`LOS_Crc32` is INCOMPATIBLE.** Need to bring in a Castagnoli implementation
 or skip verification.
- **CRC-16** (used by exFAT for boot region checksum, dentry SetChecksum) —
 LiteOS has no equivalent; need to write inline or skip.
- **MD5 / SHA** (used in some FS for content-addressed storage) — LiteOS has
 via mbedTLS; usable but cost.

Action when hit: AskUserQuestion with options "skip verification" / "bring in
a port of the algorithm" / "fail mount if checksum unverifiable".

## Trap 2: Byte order

Most modern FS are explicitly little-endian on disk (exFAT, EROFS, F2FS, ext4).
But:
- Some legacy FS use native byte order (cpio, tarfs) — runtime sensitive.
- ARM/aarch64 in LiteOS-A is configurable; verify with `LITTLE_ENDIAN` macro.
- Always use explicit conversion functions:
 - `le16_to_cpu(x)` — wrap as inline helper if missing
 - `le32_to_cpu(x)`
 - `le64_to_cpu(x)`
 - For exFAT we wrote `le16_load(p)` / `le32_load(p)` / `le64_load(p)` that
 do byte-by-byte assembly to be safe on unaligned buffers.

Action: If on-disk uses non-LE, AskUserQuestion to confirm conversion strategy.

## Trap 3: Character set / charset

Linux FS character handling for filenames varies:
- **ASCII-only** — trivial
- **UTF-8** — supported by LiteOS musl libc; trivial
- **UTF-16LE** — used by exFAT, FAT-LFN. LiteOS has NO bundled converter;
 need to either write a minimal converter (~80 LOC) OR degrade to ASCII-only
 in (require all filenames ≤127 ASCII; otherwise return -ENOENT).
- **GBK / GB18030** — Chinese encoding; LiteOS has none.
- **UCS-2 / UTF-16BE** — rare.

Action: For non-UTF-8 charsets, AskUserQuestion to choose:
- Degrade to ASCII-only (recommended for ; matches `LOSCFG_FS_FAT_CHINESE` off)
- Implement minimal converter
- Defer to 后续 (mark spec scope explicit)

## Trap 4: Alignment / packed structs

On-disk struct layouts use `__attribute__((packed))` to defeat C natural
alignment. ARM (especially ARMv7-A in QEMU) traps unaligned access by default.

Concrete cases:
- A `__attribute__((packed)) struct exfat_dentry` has fields at byte
 offsets that aren't 4-byte aligned. Reading `le32_to_cpu(de->some_u32)`
 by direct memory access can fault.
- `boot_sector` has 64-bit fields (vol_length, partition_offset) at non-
 64-byte-aligned offsets — must read byte-by-byte (`le64_load(p)`).

Action when hit:
- Confirm the on-disk struct uses `packed` attribute (or matches Linux
 upstream which does)
- Use safe accessor helpers (`le16_load(p)`, `le32_load(p)`, `le64_load(p)`)
 that do explicit byte assembly
- DO NOT assume the C compiler will emit unaligned-safe code — ARM compilers
 often do not; you'll see SIGBUS-equivalent (data abort) at runtime.

Action: If spec involves on-disk structs with multi-byte fields, AskUserQuestion
"Confirm packed-struct accessor strategy: (a) byte-by-byte le_load helpers
[recommended], (b) direct cast (only safe if struct is naturally aligned)".

## Pre-emption: Add `Assumptions made:` for low-severity

If a trap applies but the default is obvious (e.g., on-disk LE + ARM little-endian
config + use le_load helpers — all standard), include in output:
```c
/* Assumptions made:
    * - On-disk byte order: little-endian (matches Linux upstream)
    * - Use le16_load/le32_load/le64_load for unaligned access (defeats packed struct alignment)
    * - UTF-8 only filenames in ; UTF-16LE filenames return -ENOENT
    */
```
This makes assumptions auditable in Step 6 user review without blocking generation.
