<!--
LiteOS-A 内核 FS 代码硬性风格规则，由 codegen / style_audit 加载为 {STYLE_RULES}。
Hard style rules for LiteOS-A kernel FS code; loaded as {STYLE_RULES}
placeholder by codegen / style_audit. Compiler does NOT enforce most of
these — LLM must self-enforce, Step 6 user reviews.

History/version: see ../CHANGELOG.md.
-->

## Hard rules (non-negotiable)

### Copyright header
Use the BSD-3-Clause Huawei Device template from `fs/fat/os_adapt/fatfs.c`:

```c
/*
    * Copyright (c) <year> Huawei Device Co., Ltd. All rights reserved.
    *
    * Redistribution and use in source and binary forms, with or without modification,
    * are permitted provided that the following conditions are met:
    *
    * 1. Redistributions of source code must retain the above copyright notice, this list of
    * conditions and the following disclaimer.
    *
    * 2. Redistributions in binary form must reproduce the above copyright notice, this list
    * of conditions and the following disclaimer in the documentation and/or other materials
    * provided with the distribution.
    *
    * 3. Neither the name of the copyright holder nor the names of its contributors may be used
    * to endorse or promote products derived from this software without specific prior written
    * permission.
    *
    * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS
    * OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
    * MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
    * COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
    * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
    * GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
    * AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
    * NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
    * OF THE POSSIBILITY OF SUCH DAMAGE.
    */
```

NEVER paste GPL Linux headers. Whole repo is BSD-3-Clause.

### Conditional compilation
Wrap entire translation unit body in:
```c
#include "<module>.h"
#ifdef LOSCFG_FS_<MODULE>
... your code ...
#endif /* LOSCFG_FS_<MODULE> */
```

### libsec mandatory
Every `strcpy` / `strncpy` / `strcat` / `memcpy` / `sprintf` MUST use libsec `_s` variant:
- `strcpy_s(dest, dest_size, src)`
- `strncpy_s(dest, dest_size, src, n)`
- `memcpy_s(dest, dest_size, src, src_size)`
- `sprintf_s(dest, dest_size, fmt, ...)`

Compiler will NOT flag the unsafe versions. Self-audit every call site.

### Allocation
- Kernel heap: `LOS_MemAlloc(m_aucSysMem0, sz)` — uninitialized
- Kernel heap zeroed: `zalloc(sz)` — wrapper that returns zeroed buffer
- Free: `LOS_MemFree(m_aucSysMem0, ptr)`
- NEVER `kmalloc`, `kzalloc`, `malloc`, `calloc`, `free` in kernel context

### Locking
- May sleep / wait / IO: `LOS_MuxLock(LosMux *, UINT32 timeout)` + `LOS_MuxUnlock`
- Cannot sleep (interrupt context, holding spinlock): `LOS_SpinLock` / `LOS_SpinUnlock`
- NEVER call sleeping primitives while holding a spinlock
- Mux init: `LOS_MuxInit(&mutex, NULL)`; matching destroy: `LOS_MuxDestroy(&mutex)`

### Error returns
- VFS callbacks (in g_<fs>Vops / g_<fs>Fops): return NEGATIVE POSIX errno on
 failure (`-EINVAL`, `-ENOMEM`, `-EROFS`, `-EIO`, `-ENOENT`)
- Internal helpers: may use POSITIVE errno + goto-stack reverse-LIFO labels;
 final return is `-ret`
- Pattern (from fatfs.c::fatfs_mount):
 ```c
 ret = step1();
 if (ret) goto ERROR_EXIT;
 ret = step2();
 if (ret) goto ERROR_STEP1;
 ...
 return 0;
 ERROR_STEPN: cleanup_n();
 ERROR_STEP2: cleanup_2();
 ERROR_STEP1: cleanup_1();
 ERROR_EXIT: return -ret;
 ```

### Logging
- Errors: `PRINT_ERR("[%s] message: %d\n", __func__, ret)`
- Info: `PRINT_INFO(...)`
- Always-on: `PRINTK(...)`
- NEVER `printk`, `pr_err`, `pr_info` (Linux); never `printf` (userspace)

### FS registration — link-time table, NO init function
```c
struct MountOps g_<name>Mops = {
    .Mount = Vfs<Name>Mount,
    .Unmount = Vfs<Name>Umount,
    .Statfs = Vfs<Name>Statfs,
    .Sync = NULL,
};
FSMAP_ENTRY(<name>_fsmap, "<name>", g_<name>Mops, FALSE, TRUE);
```

`FSMAP_ENTRY` (from `los_tables.h`) places the entry in `.liteos.table.fsmap.data`
section; the linker aggregates into `g_fsmap[]`. Compile-time-only registration.
**Do NOT use `LOS_MODULE_INIT` for FS registration.**

`MountOps.Mount` signature: `int (*)(struct Mount *, struct Vnode *blk, const void *data)`
The `blk` arg is the BLOCK-DEVICE vnode, not a parent directory.

`MountOps.Unmount` signature: `int (*)(struct Mount *, struct Vnode **blkdriver)`
Write `*blkdriver = mount->vnodeDev` so caller can re-acquire the block device.

## Naming rules

| Identifier kind | Convention | Example |
|---|---|---|
| VFS callbacks (entries in g_<fs>Vops / g_<fs>Fops) | `Vfs<Fs><Op>` PascalCase | `VfsExfatLookup`, `VfsExfatRead` |
| Internal helpers (FS-private, static linkage) | `<fs>_<verb>_<noun>` lower_snake | `exfat_read_cluster_chain` |
| On-disk struct types | preserve UPSTREAM Linux name for grep | `struct exfat_dentry`, `struct exfat_chksum_entry` |
| Per-FS public types | `<fs>_<role>` lower_snake | `exfat_sb_info`, `exfat_inode_info` |
| Magic constants | `<FS>_<PURPOSE>` UPPER_SNAKE | `EXFAT_SUPER_MAGIC`, `EXFAT_FIRST_CLUSTER` |
| Module-wide globals | `g_<fs><Role>` PascalCase suffix | `g_exfatVops`, `g_exfatMops` |

## File generation order (within a single stage's sub-agent dispatch)

```
1. <fs>.h public types + API decls (read by all .c)
2. <fs>_pri.h private types if needed
3. util/*.c no external deps
4. bitmap/*.c, path/*.c depend on util
5. inode/*.c, file/*.c depend on bitmap / path
6. interface/*.c depend on everything; called by g_<fs>Vops
    (registration glue lives here, NOT in vfs_<fs>.c)
```

Layers 3 and 4 can spawn parallel sub-agents if no inter-target [RELY] deps.
Layer transitions are serial.

## Common pitfalls — must NOT do

1. **Page-cache inertia**: writing `struct page *page = ...; page->mapping = ...` —
 LiteOS has no page cache. Use bcache or direct `los_disk_read` / `los_part_read`.
2. **Skipping ## Refine Prompt** when path holds 2+ locks: spec MUST have a
 second `## Refine Prompt` round describing locking discipline as separate concern.
3. **Copying `struct buffer_head` callbacks**: Linux op tables are not 1:1 with
 VnodeOps. Don't blindly map; consult `references/liteos-vfs-mapping.md`.
4. **Forgetting libsec**: most common compile-clean style violation.
5. **GPL header pasted from Linux source**: license violation.
6. **Single huge `vfs_<fs>.c`**: spread VFS callback bodies across `interface/` files;
 keep registration glue thin.

## Disk IO — partition vs disk addressing

- `los_disk_read(disk_id, buf, sector, count, useRead)` — DISK-relative sector
- `los_part_read(part_id, buf, sector, count, useRead)` — PARTITION-relative sector
- `disk_id` and `part_id` are DIFFERENT namespaces. Mixing causes "mutex lock failed".
- For FS code that operates relative to its partition (which is most FS code):
 store `part->part_id` in `sbi->dev_id` and call `los_part_read(sbi->dev_id, ...)`.

## /proc/mounts visibility

If you set a non-zero `f_type` in your statfs implementation, also add a case in
`fs/proc/os_adapt/mounts_proc.c::ShowType` switch — otherwise `/proc/mounts`
silently drops your FS from listing (kernel-side mount is fine; only the procfs
adapter's switch dropped it).
