<!--
LiteOS-A kernel FS digest. Loaded as {LITEOS_DIGEST} placeholder by codegen.md.
Replaces style_rules.md + linux_to_liteos_table.md + format_traps.md +
ask_first_rules.md (those four were 22K combined; this digest is ~1.7K and
covers the rules the LLM cannot infer from the spec). Used in CODE stage only;
spec stage does not need this — see linux_to_spec.md.

If a code-gen retry round detects a specific class of error (libsec violation,
disk_id/part_id mix, FSMAP miswiring, packed-struct unaligned access), the
plugin lazy-injects the corresponding detailed fragment. Default: digest only.
-->

# Memory & strings
- Alloc: `LOS_MemAlloc(m_aucSysMem0, sz)` (uninit) | `zalloc(sz)` (zeroed) | free: `LOS_MemFree(m_aucSysMem0, p)`
- Strings/memory: ALWAYS use libsec `_s` variants (`strncpy_s`, `memcpy_s`, `snprintf_s`, `memset_s`). Compiler will NOT flag the bare versions.
- NEVER `kmalloc` / `kzalloc` / `malloc` / `calloc` / `free` in kernel context.

# Locking
- Sleep-allowed: `LosMux` + `LOS_MuxInit` / `LOS_MuxLock(.., LOS_WAIT_FOREVER)` / `LOS_MuxUnlock` / `LOS_MuxDestroy`
- Atomic only: `LOS_SpinLock` / `LOS_SpinUnlock`. NEVER sleep while holding a spinlock.

# Disk I/O — partition vs disk namespaces
- `los_part_read(part_id, buf, sect, n, TRUE)` — partition-relative (THE COMMON CASE for FS code)
- `los_disk_read(disk_id, ...)` — DIFFERENT NAMESPACE; mixing yields "mutex lock failed"
- Store `part->part_id` in your sbi and call `los_part_read/write` only.

# Error returns
- VFS callbacks (slots in `g_<fs>Vops` / `g_<fs>Fops`): return NEGATIVE POSIX errno (`-EINVAL`, `-ENOMEM`, `-EROFS`, `-EIO`, `-ENOENT`).
- Internal helpers: positive errno + goto-stack reverse-LIFO labels; final `return -ret`.

# FS registration — linker table, NO init function
```c
struct MountOps g_<fs>Mops = { .Mount=..., .Unmount=..., .Statfs=..., .Sync=NULL };
FSMAP_ENTRY(<fs>_fsmap, "<fs>", g_<fs>Mops, FALSE, TRUE);
```
- `MountOps.Mount(struct Mount *, struct Vnode *blk, const void *data)` — `blk` is the BLOCK-DEVICE vnode, not a parent dir.
- `MountOps.Unmount(struct Mount *, struct Vnode **blkdriver)` — write `*blkdriver = mount->vnodeDev`.

# Logging
- `PRINT_ERR` / `PRINT_INFO` / `PRINT_WARN` / `PRINTK`. NEVER `printk` / `pr_err` / `printf`.

# License header & translation-unit gating
- BSD-3-Clause Huawei Device template (mirror `fs/fat/os_adapt/fatfs.c`). NEVER paste GPL Linux headers.
- Wrap entire TU body in `#ifdef LOSCFG_FS_<MODULE> ... #endif`.

# Naming
- VFS callbacks (slots in `g_<fs>Vops`/`Fops`): `Vfs<Fs><Op>` PascalCase.
- Internal helpers: `<fs>_<verb>_<noun>` lower_snake.
- On-disk struct types: preserve UPSTREAM Linux name (e.g. `struct exfat_dentry`).
- Per-FS public types: `<fs>_<role>`. Globals: `g_<fs><Role>`.

# Format-compat traps (scan before codegen)
- CRC: `LOS_Crc32` is IEEE poly. CRC-32C / CRC-16 require own implementation.
- Byte order: assume on-disk LE. For packed structs with multi-byte fields use `le16_load`/`le32_load`/`le64_load` helpers — NEVER cast & deref directly on ARM.
- Charsets: UTF-16LE / GBK have no LiteOS converter. Default to UTF-8-only and reject other names.

# Subsystems absent in LiteOS-A — must DROP from spec scope
RCU, jbd2/journal, fscrypt, dm-*/md/overlayfs, netfs, iomap.

# /proc/mounts visibility
If statfs sets a non-zero `f_type`, also add the magic to `fs/proc/os_adapt/mounts_proc.c::ShowType` switch.
