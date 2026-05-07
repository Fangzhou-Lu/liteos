# Linux ↔ LiteOS-A VFS 映射速查表

将 Linux 文件系统移植到 LiteOS-A 时，需要把 Linux 内核基础设施翻译为 LiteOS-A 的对应物。本表来自对 `fs/vfs/`、`kernel/base/`、`drivers/block/` 的实测整理。

## 顶层对象映射

| Linux | LiteOS-A | 关键差异 |
|---|---|---|
| `struct super_block` | `struct Mount` | LiteOS-A 把挂载实例与其上的 VFS 操作分离 |
| `struct super_operations` | `struct MountOps` | 字段子集；无 `nr_cached_objects` / `free_cached_objects` |
| `struct inode` + `struct dentry` | `struct Vnode`（合一） | LiteOS-A 没有独立 dcache，名字存在 `path_cache.c` |
| `struct file` | `struct file`（VFS 自家） | 字段更少 |
| `struct inode_operations` | `struct VnodeOps` | 见下 |
| `struct file_operations` | `struct file_operations_vfs` | 见下 |
| `struct address_space_operations` | （无） | 直接调用 `fs/vfs/bcache/` |
| `struct kiocb` | （无） | 用 `loff_t *` 参数即可 |

参考头文件：
- `fs/vfs/include/vnode.h`
- `fs/include/fs/file.h`
- `fs/include/fs/mount.h`
- `fs/include/fs/dirent_fs.h`

## VFS 操作表对照

### `MountOps`（对应 `super_operations`）

| Linux 字段 | LiteOS-A 字段 | 备注 |
|---|---|---|
| `alloc_inode` / `destroy_inode` | （隐式于 vnode 分配） | 通过 `VnodeAlloc` / `VnodeDestory` |
| `write_inode` | `WriteVnodeOps` | |
| `evict_inode` | `Reclaim`（在 `VnodeOps`） | 注意是放在 vnode 一侧 |
| `put_super` | `Unmount` | |
| `sync_fs` | `Statfs` 不含；用 `flush` 自行实现 | LiteOS-A 没有强制的全 FS sync |
| `statfs` | `Statfs` | |
| `remount_fs` | （无） | 不支持运行期 remount |
| `show_options` | （无） | 选项打印通过 procfs |

### `VnodeOps`（对应 `inode_operations` 的常用项）

| Linux 字段 | LiteOS-A 字段 | 备注 |
|---|---|---|
| `lookup` | `Lookup` | |
| `create` | `Create` | |
| `link` | `Link` | |
| `unlink` | `Unlink` | |
| `symlink` | `Symlink` | |
| `mkdir` | `Mkdir` | |
| `rmdir` | `Rmdir` | |
| `mknod` | `Mknod` | |
| `rename` | `Rename` | |
| `getattr` | `Getattr` | |
| `setattr` | `Chattr` | |
| `truncate`（已废弃） | `Truncate` / `Truncate64` | |
| `permission` | （无） | 权限检查由 `security/cap/` 在 VFS 入口完成 |
| `listxattr` | （无） | 暂无 xattr |

### `file_operations_vfs`（对应 `file_operations`）

| Linux 字段 | LiteOS-A 字段 | 备注 |
|---|---|---|
| `open` | `open` | |
| `release` | `close` | 字段名直接是 `close` |
| `read_iter` / `read` | `read` | 单缓冲区接口；矢量 IO 通过 `readv` 帮手 |
| `write_iter` / `write` | `write` | |
| `llseek` | `seek` | |
| `mmap` | `mmap` | |
| `fsync` | `fsync` | |
| `iterate_shared` / `readdir` | `readdir` | |
| `unlocked_ioctl` | `ioctl` | |
| `poll` | `poll` | |

完整字段列表见 `fs/include/fs/file.h` 中 `struct file_operations_vfs`。

## 同步原语映射

| Linux | LiteOS-A | 备注 |
|---|---|---|
| `struct mutex` | `LosMux` | `LOS_MuxInit` / `LOS_MuxLock(m, LOS_WAIT_FOREVER)` / `LOS_MuxUnlock` |
| `struct semaphore` | `UINT32` 信号量 ID | `LOS_SemCreate` / `LOS_SemPend` / `LOS_SemPost` |
| `spinlock_t` | `SPIN_LOCK_S` | `LOS_SpinLock` / `LOS_SpinUnlock` |
| `rwlock_t` | `LosRwlock` | 见 `kernel/include/los_rwlock.h` |
| `seqlock_t` | （无） | 用 mutex 退化 |
| `RCU` | （无） | 改写为持锁 |
| `wait_queue_head_t` | `LosEvent` | `LOS_EventInit` / `LOS_EventRead` / `LOS_EventWrite` |
| `completion` | 二值信号量 | `LOS_SemCreate(0)` 然后 pend/post |
| `atomic_t` / `atomic_inc` | `LOS_AtomicInc` 等 | `kernel/include/los_atomic.h` |

**约束**：在持自旋锁期间不允许调用 `LOS_MuxLock`、`LOS_SemPend`、`LOS_TaskDelay` 这类睡眠原语。Linux 实现里大量 `spin_lock_irqsave` 后调用 `kmalloc(GFP_ATOMIC)` 的模式必须改写：要么提前在外层用 mutex，要么把分配挪到自旋锁外。

## 内存分配映射

| Linux | LiteOS-A | 备注 |
|---|---|---|
| `kmalloc(sz, GFP_KERNEL)` | `LOS_MemAlloc(m_aucSysMem0, sz)` | |
| `kzalloc(sz, GFP_KERNEL)` | `zalloc(sz)` 或 `LOS_MemAllocAlign` 后 memset_s | `zalloc` 在 `los_memory.h` |
| `kfree(p)` | `LOS_MemFree(m_aucSysMem0, p)` | |
| `kmem_cache_create / alloc / free` | `LOS_MemboxInit / LOS_MemboxAlloc / LOS_MemboxFree` | `kernel/include/los_membox.h` |
| `vmalloc / vfree` | `LOS_VMallocAlign` / `LOS_VFree` | 极少需要；优先 `LOS_MemAlloc` |
| `__get_free_pages(GFP_KERNEL, order)` | `LOS_PhysPagesAllocContiguous` | `kernel/base/vm/los_vm_phys.c` |
| `GFP_NOFS` / `GFP_NOIO` | （无对应；不从 FS 路径里递归分配） | |

**约束**：`m_aucSysMem0` 是默认内核堆。所有 `LOS_MemAlloc` 必须与 `LOS_MemFree` 配对，且第一参数一致。

## 块设备 IO 映射

| Linux | LiteOS-A | 备注 |
|---|---|---|
| `bdev_read_page` / `bdev_write_page` | （无） | 无 page，按字节读写 |
| `bread(bdev, block, size)` | `los_disk_read(driveID, buf, sector, count)` | 见 `drivers/block/disk/include/disk.h` |
| `__bread` / `submit_bh` | `los_disk_write(driveID, buf, sector, count)` | |
| `bio_alloc` / `submit_bio` | （无 bio） | 直接 `los_disk_read/write` |
| `set_blocksize` | （由分区表决定） | |
| `sb_bread` | `bcache_get` 后 `los_disk_read` 填充 | 见 `fs/vfs/bcache/` |
| `mark_buffer_dirty` / `sync_dirty_buffer` | `BcacheClusterData` / `BcacheSyncBlk` | `fs/vfs/include/bcache/bcache.h` |
| `blkdev_issue_discard` | （无） | discard/trim 通常在 删除 |

**bcache 用法**：先 `LosBcache *bc = SuperGetBcache(mountInfo)` 拿到设备的 bcache 实例；后续读改写都走 bcache，避免直接 `los_disk_*` 绕过缓存。

## 字符串与拷贝（libsec 强制）

| Linux | LiteOS-A | 备注 |
|---|---|---|
| `memcpy(dst, src, n)` | `memcpy_s(dst, dstSize, src, n)` | dstSize 必须正确 |
| `memmove(dst, src, n)` | `memmove_s(dst, dstSize, src, n)` | |
| `memset(p, 0, n)` | `(void)memset_s(p, n, 0, n)` | 注意类型转换 |
| `strcpy(dst, src)` | `strcpy_s(dst, dstSize, src)` | |
| `strncpy(dst, src, n)` | `strncpy_s(dst, dstSize, src, n)` | dstSize 一般 = n+1 |
| `strcat(dst, src)` | `strcat_s(dst, dstSize, src)` | |
| `snprintf(buf, n, fmt, ...)` | `snprintf_s(buf, n, n - 1, fmt, ...)` | 注意"-1" |
| `scanf` 系 | `sscanf_s` | 但 FS 路径几乎不用 |

返回值约定：libsec 函数返回 `EOK` 即成功；失败返回非零码（如 `EINVAL`、`ERANGE_AND_RESET`）。务必检查返回值——`(void)` 强转不是免检通行证。

## 日志与诊断

| Linux | LiteOS-A | 备注 |
|---|---|---|
| `printk(KERN_ERR ...)` | `PRINT_ERR(fmt, ...)` | `kernel/include/los_printf.h` |
| `printk(KERN_INFO ...)` | `PRINT_INFO(fmt, ...)` | |
| `pr_debug` / `dev_dbg` | `PRINT_DEBUG`（默认禁用，用 `LOSCFG_BASE_CORE_PRINTF_DEBUG=y` 开） | |
| `pr_warn` | `PRINT_WARN` | |
| `WARN_ON(cond)` | `LOS_ASSERT(!(cond))` 或自行处理 | LiteOS-A 无 WARN 宏 |
| `BUG_ON(cond)` | `LOS_ASSERT(!(cond))` | |
| `dump_stack` | `OsBackTrace` | `arch/arm/arm/src/los_exc_info.c` |

## 错误码

VFS 边界返回**负 POSIX errno**（与 Linux 一致）：`-EINVAL`、`-ENOMEM`、`-ENOSPC`、`-EROFS`、`-EIO`、`-EEXIST`、`-ENOENT`。这些宏定义在 `<errno.h>`。

内部辅助函数允许返回 LiteOS-A 的 `LOS_ERRNO_FS_*` 系列码（见 `fs/include/fs/errno.h`），但向 VFS 边界冒出前必须翻译。多数 LiteOS-A 现有 FS 直接全程用负 POSIX errno，可参考。

## VNode 注册胶水

注册一个新 FS 的标准模板（取自 `fs/jffs2/src/vfs_jffs2.c` 与 `fs/fat/os_adapt/fatfs.c` 的共有结构）：

```c
static struct VnodeOps g_<name>Vops = { /* Lookup / Create / ... */ };
static struct file_operations_vfs g_<name>Fops = { /* read / write / ... */ };
static struct MountOps g_<name>MountOps = { .Mount = OsExfatMount,
    .Unmount = OsExfatUmount,
    .Statfs = OsExfatStatfs };

LOS_MODULE_INIT(OsExfatInit, LOS_INIT_LEVEL_KMOD_EXTENDED);

int OsExfatInit(void)
{
    return VnodeRegisterFs("exfat", &g_<name>MountOps,
    &g_<name>Vops, &g_<name>Fops);
}
```

`LOS_MODULE_INIT` 注册到 `kernel/common/los_init.c` 的初始化表；启动时按级别依次调用。`VnodeRegisterFs` 来自 `fs/vfs/vnode.c`。

## 不存在的特性（移植时要明确" 删除"）

| Linux 特性 | 处理方式 |
|---|---|
| RCU | 改写为持锁；不要尝试模拟 |
| jbd2 / 任意日志 | 跳过；FS 默认非崩溃安全 |
| fscrypt（透明加密） | 跳过 |
| fsverity | 跳过 |
| io_uring 异步 IO | 不存在；只用同步 IO |
| direct IO（O_DIRECT） | 不支持；忽略该 flag 或返回 `-EINVAL` |
| memory map 写回 | bcache 自动处理；不需要 writepage |
| 透明大页 / readahead | 跳过 |
| UBSAN / KASAN | 用 `kernel/extended/lms/`（LMS）代替 |
