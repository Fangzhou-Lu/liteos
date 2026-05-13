[PROMPT]
Wave A 收尾——补齐两个让"`ls -l /mnt/exfat/file`"与"`lseek(fd, off, whence)`"
能跑通的元数据/定位回调：

- `VfsExfatGetattr(vp, st)`：填充 `struct stat`，挂在 `g_exfatVops.Getattr`。
  Linux 端是 `exfat_getattr`（fs/exfat/file.c）调用 `generic_fillattr` + 设置
  `stat->blksize = sbi->cluster_size`、`stat->btime` 等。LiteOS-A 端写一份
  对等的"读 ei + sbi 几何字段填 struct stat"，仅 fstat / stat / ls -l 路径
  需要。Wave A 不维护 atime/mtime/ctime（ei 中尚未引入 timestamp 字段），
  全部置 0；Wave B 解析 dentry timestamp 后再补。
- `VfsExfatSeek(filep, offset, whence)`：调整 `filep->f_pos`，挂在
  `g_exfatFops.seek`。Linux 端是 `.llseek = generic_file_llseek`
  （default，exfat 不覆盖）。LiteOS-A 端做最简 lseek 算术：支持 SEEK_SET /
  SEEK_CUR / SEEK_END；返回新 pos 或 `-EINVAL`。允许 seek 越过 EOF（POSIX
  允许；后续 read 自然返回 0）。

锁模型（Linux 字面对齐）：
- exfat_getattr 不取 sbi->s_lock；仅依赖 VFS 上层 inode i_lock 做 timestamp
  原子读。LiteOS-A 取 `ei->inode_lock` 做 ei 字段（特别是 size）的原子快照。
- generic_file_llseek 也不取 s_lock；SEEK_END 在 Linux 通过 `i_size_read()` 配
  对 seq counter 做无锁原子读，LiteOS-A 用 `ei->inode_lock` 短暂保护
  `ei->size` 读取，比 Linux 略保守一点（LosMux 没有 seqcount）。
- 与 Stage 7 read 的 inode_lock 锁序兼容：read 也取 inode_lock，二者不会
  互相嵌套（不同 fd 互不相干，同 fd 的 read+seek 由 VFS 上层串行）。

输出阶段必填：扩展 `g_exfatVops`、`g_exfatFops`：
```c
struct VnodeOps g_exfatVops = {
    .Lookup    = VfsExfatLookup,
    .Reclaim   = VfsExfatReclaim,
    .Opendir   = VfsExfatOpendir,
    .Readdir   = VfsExfatReaddir,
    .Closedir  = VfsExfatClosedir,
    .Rewinddir = VfsExfatRewinddir,
    .Getattr   = VfsExfatGetattr,        /* + */
};
struct file_operations_vfs g_exfatFops = {
    .open  = VfsExfatOpen,
    .close = VfsExfatClose,
    .read  = VfsExfatRead,
    .seek  = VfsExfatSeek,               /* + */
};
```

[RELY]
```c
/* common.header 已冻结类型 */
typedef struct exfat_sb_info exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;

/* TYPE_* 常量 */
#define TYPE_FILE  0x011Fu
#define TYPE_DIR   0x0104u

/* LiteOS-A VFS 类型 */
struct file;        /* 含 f_vnode、f_pos、f_oflags、ops */
struct Vnode;       /* 含 type、data、originMount、mode、uid、gid */
struct stat;        /* POSIX struct stat（fs/include/sys/stat.h）*/

/* lseek whence 常量 */
#define SEEK_SET 0
#define SEEK_CUR 1
#define SEEK_END 2

/* errno */
#define EINVAL  22
#define EOVERFLOW 75

/* ei 锁相关 */
extern UINT32 LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern UINT32 LOS_MuxUnlock(LosMux *mutex);
#define LOS_WAIT_FOREVER 0xFFFFFFFFu

/* libsec */
extern errno_t memset_s(void *dest, size_t destMax, int c, size_t count);
```

[GUARANTEE]
```c
/* === Public exports (declared in fs/exfat/include/exfat.h) =========== */

/*
 * VfsExfatGetattr — VnodeOps.Getattr 回调。
 *
 * 调用约定：
 *   - 由 sys_stat / sys_lstat / sys_fstat（filep->ops->stat == NULL 路径
 *     fallback）/ shell `ls -l` 触发。
 *   - 调用方未持任何 exfat 锁；本函数自取 ei->inode_lock 做 size 快照，
 *     释放后填字段。
 *   - 不取 sbi->s_lock（Linux exfat_getattr 同样不取）。
 *
 * 返回值：
 *   - 0          ：填字段成功。所有 *st 字段已写入。
 *   - -EINVAL    ：vp == NULL / vp->data == NULL / vp->originMount == NULL /
 *                 st == NULL。
 *
 * 副作用：
 *   - 写入 *st 全部字段（先 memset_s 清零，再逐字段填）。
 *   - 不修改 sbi、ei、vp 任何字段。
 */
extern int VfsExfatGetattr(struct Vnode *vp, struct stat *st);

/*
 * VfsExfatSeek — file_operations_vfs.seek 回调。
 *
 * 调用约定：
 *   - 由 sys_lseek / sys_lseek64 / fseek 触发。
 *   - 调用方未持任何 exfat 锁；SEEK_END 路径自取 ei->inode_lock 读 size，
 *     释放后再写 filep->f_pos。SEEK_SET / SEEK_CUR 路径不取锁。
 *   - 不取 sbi->s_lock。
 *
 * 返回值：
 *   - >= 0       ：新的 filep->f_pos 值（off_t 范围内）。
 *   - -EINVAL    ：whence 不在 {SEEK_SET, SEEK_CUR, SEEK_END} 之中；
 *                 计算出的新 pos 为负数；filep / f_vnode / vp->data 为 NULL；
 *                 ei->type 不是 TYPE_FILE。
 *   - -EOVERFLOW ：SEEK_END + offset 溢出 off_t 上限（罕见，>= 2^63 - size）。
 *
 * 副作用：
 *   - 成功时写 filep->f_pos = 新值。
 *   - 失败时不修改 filep->f_pos / sbi / ei / vp。
 */
extern off_t VfsExfatSeek(struct file *filep, off_t offset, int whence);
```

[SPECIFICATION]
**Pre-Condition (Getattr)**：
1. `vp != NULL` 且 `vp->originMount != NULL` 且 `vp->data != NULL` 且 `st != NULL`。
2. `ei = vp->data` 是 lookup 阶段安装的有效 exfat_inode_info；`ei->inode_lock`
   已 init。
3. `sbi = vp->originMount->data` 是 mount 完成时初始化的 exfat_sb_info。

**Post-Condition (Getattr Case 1: 成功)**：
- 取 `ei->inode_lock` 后读 `ei->size`、`ei->valid_size`、`ei->type`、`ei->attr`、
  `vp->mode`、`sbi->cluster_size`、`sbi->options.fs_uid`、`sbi->options.fs_gid`，
  释放锁。
- `memset_s(st, sizeof(*st), 0, sizeof(*st))` 先清零。
- 填入：
  - `st->st_dev`     = sbi->part_id（partition id；与 fatfs.fs->pdrv 同源）。
  - `st->st_ino`     = `(ino_t)(ei->i_pos >> 32)`（取 start_clu 作为伪 ino；
                       root 用 EXFAT_ROOT_INO（1））。
  - `st->st_mode`    = `vp->mode`（lookup 安装；DIR 是 0755 mask + S_IFDIR；
                       FILE 是 0644 mask + S_IFREG，按 sbi->options.fs_dmask /
                       fs_fmask 调整）。
  - `st->st_nlink`   = 1（FAT/exFAT 无硬链接）。
  - `st->st_uid`     = sbi->options.fs_uid。
  - `st->st_gid`     = sbi->options.fs_gid。
  - `st->st_size`    = `(off_t)ei->size`。
  - `st->st_blksize` = sbi->cluster_size。
  - `st->st_blocks`  = `ei->size ? ((ei->size + 511) / 512) : 0`
                       （POSIX 512 字节单位，与 Linux 标准一致）。
  - `st->st_atime` = `(time_t)ei->atime_sec`（已由 chattr / inode_load_metadata
                       维护；初始为 mount 时间戳）。
  - `st->st_mtime` = `(time_t)ei->mtime_sec`。
  - `st->st_ctime` = `(time_t)ei->ctime_sec`。
  - 若结构体含 `__st_atim32.tv_sec` / `__st_mtim32.tv_sec` / `__st_ctim32.tv_sec`
                       兼容字段，同步填同值；`tv_nsec` 字段填 0（exFAT dentry 仅
                       秒精度 + 10ms increment，Linux 也 truncate atime 到 2s）。
- 返回 0。

**Post-Condition (Getattr Case 2: 入参非法)**：
- vp / vp->data / vp->originMount / st 任一为 NULL → 返回 -EINVAL；不取锁,
  不修改 *st。

**Pre-Condition (Seek)**：
1. `filep != NULL` 且 `filep->f_vnode != NULL` 且 `filep->f_vnode->data != NULL`。
2. `ei = vp->data` 有效；`ei->type == TYPE_FILE`（目录走 readdir/rewinddir 路径，
   不应到 seek）。
3. `whence ∈ {SEEK_SET, SEEK_CUR, SEEK_END}`。

**Post-Condition (Seek Case 1: SEEK_SET)**：
- 计算 `new_pos = offset`。若 `offset < 0` → 返回 -EINVAL；`filep->f_pos` 不变。
- 不取锁；写 `filep->f_pos = new_pos`；返回 `new_pos`。

**Post-Condition (Seek Case 2: SEEK_CUR)**：
- 计算 `new_pos = filep->f_pos + offset`。若 `new_pos < 0` 或溢出 → -EINVAL。
- 不取锁；写 `filep->f_pos = new_pos`；返回 `new_pos`。

**Post-Condition (Seek Case 3: SEEK_END)**：
- 取 `ei->inode_lock`；读 `cur_size = ei->size`；释放锁。
- 计算 `new_pos = (off_t)cur_size + offset`。若溢出 (off_t 加法上溢) → 返回
  -EOVERFLOW；若 `new_pos < 0` → 返回 -EINVAL；二者均不修改 f_pos。
- 写 `filep->f_pos = new_pos`；返回 `new_pos`。

**Post-Condition (Seek Case 4: 入参非法)**：
- filep / f_vnode / vp->data 任一为 NULL，或 ei->type != TYPE_FILE，或
  whence 越界 → 返回 -EINVAL；filep->f_pos 不变；不取锁。

**Invariant** (id=exfat-vfsops-getattr-no-s-lock)：
VfsExfatGetattr 不取 sbi->s_lock / sbi->bitmap_lock / sbi->inode_hash_lock。
仅读 sbi 的几何字段（cluster_size, part_id, options.fs_uid/fs_gid）；这些字段
mount 后冻结。Mirror Linux exfat_getattr 不取 sbi 锁的语义。

**Invariant** (id=exfat-vfsops-getattr-uses-inode-lock)：
VfsExfatGetattr 取 ei->inode_lock 做 ei 字段（含 size / valid_size / type /
attr）的原子快照——避免与并发的 Wave B 写路径（truncate/append）撕裂读。
释放锁后才填 *st，遵循"短临界区"原则。

**Invariant** (id=exfat-vfsops-getattr-clears-st)：
入口先 `memset_s(st, sizeof(*st), 0, sizeof(*st))` 清零，避免内核栈/堆 leak
进 user space（POSIX struct stat 含若干 padding 与未填字段）。

**Invariant** (id=exfat-vfsops-getattr-from-inode)：
`st->st_atime / st_mtime / st_ctime` 直接取自 `ei->atime_sec / mtime_sec /
ctime_sec`（在快照锁下读取）。Linux `exfat_getattr` 通过 `generic_fillattr`
读 `inode->i_atime / i_mtime / i_ctime` 同语义。这是 Wave 4 三向 baseline
对比指出的 LiteOS-A 修复点之一 —— LTP `safe_touch` 内部
`stat() → cotimes[0] = sb.st_atime → utimes(path, cotimes)` 需要 stat 返回
非 0 时间戳才能形成正确的回写序列；之前 invariant `-no-timestamps` 把
时间戳全清零是导致 utimes 后续诊断混乱的根因之一。
Wave A 的 ei->{a,m,c}time_sec 初值由 mount 时 inode_load_metadata 从 dentry
解码或由 mount-time `exfat_now_seconds()` 填入；本路径只读不写。

**Invariant** (id=exfat-vfsops-getattr-blksize-cluster)：
`st->st_blksize = sbi->cluster_size`，与 Linux exfat_getattr 一致——这是 IO
首选大小，read 系统调用 buffer 应该是其倍数。

**Invariant** (id=exfat-vfsops-getattr-blocks-512)：
`st->st_blocks` 单位是 POSIX 512 字节块，不是 cluster。`(size + 511) / 512`
取上整。Linux generic_fillattr 同语义。

**Invariant** (id=exfat-vfsops-getattr-nlink-one)：
`st->st_nlink = 1`：exFAT 无硬链接概念，所有常规文件 / 目录 nlink 恒为 1，
与 fatfs / Linux exfat 一致。

**Invariant** (id=exfat-vfsops-seek-no-lock-set-cur)：
SEEK_SET 与 SEEK_CUR 路径不取任何锁——`filep->f_pos` 是 per-file 状态，VFS
上层保证同一 fd 的 lseek/read 不并发；offset/whence 计算无需 ei 字段。

**Invariant** (id=exfat-vfsops-seek-takes-inode-lock-end)：
SEEK_END 路径取 `ei->inode_lock` 短暂保护 `ei->size` 读；释放锁后才写
`filep->f_pos`。临界区不含 IO，spinlock-friendly 但仍按 mux 处理（与现有
锁框架一致）。

**Invariant** (id=exfat-vfsops-seek-allows-past-eof)：
允许 SEEK_SET/CUR/END 计算结果 > ei->size——POSIX 允许 seek 越过 EOF（产生
"sparse-like"行为）；后续 read 在该位置自然返回 0（exfat-read-eof-returns-zero
invariant）。本路径不做"超 size 截断"。

**Invariant** (id=exfat-vfsops-seek-rejects-negative)：
计算结果 < 0（SEEK_CUR 或 SEEK_END 的 offset 太负）→ 返回 -EINVAL；
filep->f_pos 不变。POSIX lseek 标准行为。

**Invariant** (id=exfat-vfsops-seek-overflow-detection)：
SEEK_END + offset 检测 off_t 加法溢出（`offset > 0 && cur_size > OFF_MAX -
offset`）→ 返回 -EOVERFLOW。POSIX SUSv4 规定 lseek 越过 off_t 上限返回
EOVERFLOW（非 EINVAL）。

**Invariant** (id=exfat-vfsops-seek-rejects-non-file)：
`ei->type != TYPE_FILE` 立即返回 -EINVAL，不取锁。VFS 上层一般不会把目录
fd 传到 .seek，但作为最后一道断言。

**Invariant** (id=exfat-vfsops-no-mutate-on-failure)：
任意 -errno 路径（Getattr 与 Seek）不修改 sbi、ei、vp、filep 任何字段。
唯一允许的写入是成功路径下的 *st（Getattr）或 filep->f_pos（Seek）。

**Invariant** (id=exfat-vfsops-no-disk-io)：
本两个回调不调 los_part_read / los_disk_read / los_part_write —— 全部数据
来自内存中的 ei + sbi。响应时间 O(1)（除 mux 锁等待）。

**Invariant** (id=exfat-vfsops-no-spinlock-callsite)：
本两个回调内部含 LOS_MuxLock（可能睡眠）。调用方不允许在持自旋锁时进入。
VFS 上层默认不持自旋锁，约束自然成立。

**Invariant** (id=exfat-vfsops-getattr-st-dev-part-id)：
`st->st_dev = (dev_t)sbi->part_id`——LiteOS-A 没有 Linux 那种统一 dev_t
namespace，这里用 partition id 当作"设备 id"，与 fatfs 的 `fs->pdrv` 选法
同构。后续若 LiteOS-A 引入正式的 dev_t 编码（major:minor），可在 evolve
阶段调整。
