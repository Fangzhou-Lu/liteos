<!--
Linux 内核原语 → LiteOS-A 等价物映射表，由 codegen 按需 fetch。
Linux kernel primitive → LiteOS-A equivalent map; fetched on-demand by
codegen as {LINUX_TO_LITEOS_TABLE}. When the spec references Linux
primitives, the LLM MUST translate via this table — never hallucinate
that Linux primitives exist in LiteOS-A.

History/version: see ../CHANGELOG.md.
-->

## Type / structure mapping

| Linux | LiteOS-A | Header | Notes |
|---|---|---|---|
| `struct super_block` + `struct super_operations` | `struct Mount` + `struct MountOps` | `fs/vfs/include/vnode.h`, `fs/vfs/mount.c` | Mount holds FS-private data via `mount->data` |
| `struct inode` + `struct dentry` | `struct Vnode` (combined) | `fs/vfs/include/vnode.h` | Names are stored separately in `path_cache` |
| `struct inode_operations` | `struct VnodeOps` | `fs/vfs/include/vnode.h` | Attaches to `vp->vop` |
| `struct file_operations` | `struct file_operations_vfs` | `fs/vfs/include/file.h` | Attaches to `vp->fop` |
| `struct address_space_operations` | (no equivalent) | — | Use `bcache` or `los_part_read`/`los_disk_read` directly |
| `struct dentry_operations` | (no equivalent) | — | LiteOS uses `path_cache` for name caching |
| `struct page` | (no equivalent) | — | Use raw byte buffers with explicit alloc |
| `struct buffer_head` | (no equivalent) | — | Use bcache buffers via `BlockCacheRead` |
| `struct super_block::s_fs_info` | `mount->data` | — | Plain void* for FS-private metadata |
| `struct inode::i_private` | `vnode->data` | — | Plain void* for inode-private metadata |
| `struct file::private_data` | `filep->f_priv` | `fs/vfs/include/file.h` | Per-open private state |

## Memory allocation

| Linux | LiteOS-A | Header |
|---|---|---|
| `kmalloc(sz, GFP_KERNEL)` | `LOS_MemAlloc(m_aucSysMem0, sz)` | `kernel/include/los_memory.h` |
| `kzalloc(sz, GFP_KERNEL)` | `zalloc(sz)` | (returns zeroed; same backing) |
| `kfree(ptr)` | `LOS_MemFree(m_aucSysMem0, ptr)` | |
| `kmem_cache_alloc(cache, flags)` | `LOS_MemAlloc(m_aucSysMem0, sizeof(...))` | LiteOS has no slab cache |
| `kmem_cache_free(cache, ptr)` | `LOS_MemFree(m_aucSysMem0, ptr)` | |

## Locking

| Linux | LiteOS-A | Header | Notes |
|---|---|---|---|
| `mutex_init(&mutex)` | `LOS_MuxInit(&mutex, NULL)` | `kernel/include/los_mux.h` | LosMux must live in stable memory |
| `mutex_lock(&mutex)` | `LOS_MuxLock(&mutex, LOS_WAIT_FOREVER)` | | |
| `mutex_unlock(&mutex)` | `LOS_MuxUnlock(&mutex)` | | |
| `mutex_destroy(&mutex)` | `LOS_MuxDestroy(&mutex)` | | |
| `spin_lock(&lock)` | `LOS_SpinLock(&lock)` | `kernel/include/los_spinlock.h` | non-sleeping path only |
| `spin_unlock(&lock)` | `LOS_SpinUnlock(&lock)` | | |
| `rcu_read_lock()` | (none) | — | LiteOS-A has no RCU; replace with mutex or remove path |
| `down_read` / `down_write` | (no rwlock) | — | Use plain mux; performance loss but correct |

## Disk / block IO

| Linux | LiteOS-A | Header | Notes |
|---|---|---|---|
| `submit_bio()` | `los_disk_write` / `los_part_write` | `drivers/block/disk/include/disk.h` | Synchronous |
| `bread()` / `submit_bh()` | `los_disk_read` / `los_part_read` | | |
| `__bread(disk, bn, sz)` (disk-relative read) | `los_disk_read(disk_id, buf, sect, n, TRUE)` | | |
| `bh = sb_bread(sb, bn)` (FS-relative read) | `los_part_read(part_id, buf, sect, n, TRUE)` | | partition-relative |
| `mark_buffer_dirty(bh)` | (no implicit caching layer) | — | Write happens via `los_*_write` directly |
| `sync_dirty_buffer(bh)` | (no-op) | — | All writes are sync in LiteOS |

`los_disk_read(disk_id, ...)` and `los_part_read(part_id, ...)` take indices from
DIFFERENT namespaces. For FS code that addresses relative to its partition,
store `part->part_id` in your sbi and use `los_part_read`.

## String / memory

| Linux | LiteOS-A | Notes |
|---|---|---|
| `strcpy(d, s)` | `strcpy_s(d, sizeof(d), s)` | libsec mandatory |
| `strncpy(d, s, n)` | `strncpy_s(d, sizeof(d), s, n)` | |
| `memcpy(d, s, n)` | `memcpy_s(d, sizeof(d), s, n)` | |
| `memset(d, c, n)` | `memset_s(d, sizeof(d), c, n)` | |
| `sprintf(buf, fmt, ...)` | `sprintf_s(buf, sizeof(buf), fmt, ...)` | |
| `snprintf(buf, n, fmt, ...)` | `snprintf_s(buf, n, n - 1, fmt, ...)` | note arg order |

## Logging

| Linux | LiteOS-A | Header |
|---|---|---|
| `printk(KERN_ERR fmt, ...)` | `PRINT_ERR(fmt, ...)` | `kernel/include/los_printf.h` |
| `pr_info(fmt, ...)` | `PRINT_INFO(fmt, ...)` | |
| `pr_warn(fmt, ...)` | `PRINT_WARN(fmt, ...)` | |
| `printk(KERN_DEBUG fmt, ...)` | `PRINT_DEBUG(fmt, ...)` | |
| (always-on print) | `PRINTK(fmt, ...)` | |

## Errno

| Linux | LiteOS-A (VFS boundary) | Notes |
|---|---|---|
| `return -EINVAL` | `return -EINVAL` | same names; `<errno.h>` |
| `return -ENOMEM` | `return -ENOMEM` | |
| `return -EIO` | `return -EIO` | |
| `return -EROFS` | `return -EROFS` | |
| `LOS_ERRNO_*` constants | (deeper internal use only) | translate to negative POSIX errno before returning to VFS |

## Subsystems with NO equivalent (must replace or drop)

| Linux | Action |
|---|---|
| RCU | Use mutex; if read-mostly, performance loss; if hot path, redesign |
| jbd2 / journal | DROP — no journaling in ; mark spec as "non-journaled" |
| fscrypt | DROP — encryption not in scope |
| dm-* / md / overlayfs | DROP |
| keyring / cred | (security/capability has separate LiteOS adaptation; coordinate per port) |
| netfs | DROP — networked FS is its own can of worms; this plugin targets local FS |
| iomap | DROP — use bcache or direct `los_part_*` |

## VFS callback signature differences

| Linux | LiteOS-A | Note |
|---|---|---|
| `int .lookup(struct inode *dir, struct dentry *dentry, unsigned flags)` | `int .Lookup(struct Vnode *parent, const char *name, int len, struct Vnode **out)` | name + len explicit; out param for new vnode |
| `int .read(struct file *, char __user *buf, size_t, loff_t *)` | `ssize_t .read(struct file *, char *, size_t)` | offset is in `filep->f_pos`; no __user qualifier |
| `int .mount(struct super_block *sb, void *data)` | `int .Mount(struct Mount *mount, struct Vnode *blk, const void *data)` | block device vnode is explicit arg |
| `void .destroy_inode(struct inode *)` | `int .Reclaim(struct Vnode *)` | reclaim returns int; called when vnode refcnt hits 0 |
| `int .readdir(struct file *, struct dir_context *)` | `int .Readdir(struct Vnode *, struct fs_dirent_s *)` | output is `dirent_s`, not Linux's emit callback |

## Linker tables (replace LOS_MODULE_INIT for FS)

```c
/* WRONG — does not register FS in LiteOS-A: */
LOS_MODULE_INIT(my_fs_init, LOS_INIT_LEVEL_KMOD_EXTENDED);

/* RIGHT: */
struct MountOps g_<name>Mops = { .Mount = ..., .Unmount = ..., .Statfs = ..., .Sync = NULL };
FSMAP_ENTRY(<name>_fsmap, "<name>", g_<name>Mops, FALSE, TRUE);
```

`FSMAP_ENTRY` is `LOS_HAL_TABLE_ENTRY` from `los_tables.h`, places the entry
in `.liteos.table.fsmap.data` section; the linker aggregates into `g_fsmap[]`.
No init function needed — entry exists merely by being compiled.
