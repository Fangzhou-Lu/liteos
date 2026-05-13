[PROMPT]
LiteOS-A 的虚拟内存子系统在两种场景下需要回写一整页文件数据到底层 FS：

1. `munmap` / 进程退出时遍历 region，每个被标记 dirty 的 file-backed page
   通过 `OsDoFlushDirtyPage → OsFlushDirtyPage → vnode->vop->WritePage` 落
   盘；这是 exFAT 端 `VfsExfatWritePage` 的主要触发器。在 prefetch_abort
   症状被 ReadPage 修复后，下一个空白回调就是 WritePage——若不接线，VFS
   会跳 NULL 指针造成 unmap 阶段二次崩溃。
2. （未来）显式 `msync(MS_SYNC)` 主动刷脏页。LiteOS-A 当前 `msync` syscall
   未实现（Unsupported syscall ID: 144），但 vop->WritePage 一旦接线即兼
   容该未来路径，不需要在本 stage 额外处理。

调用契约（来自 kernel/base/vm/los_vm_filemap.c::OsFlushDirtyPage）：

```c
ret = vnode->vop->WritePage(vnode, (VOID *)buff, fpage->pgoff, len);
```

- vnode：被映射的文件 vnode；
- buff：内核态可访问的页帧虚拟地址；
- pos：fpage->pgoff（按页索引；调用方传入的实际是 PAGE_SIZE * pgoff 之前
  统一为字节偏移）。**注意**：当前 LiteOS-A 实现把 pgoff 直接当字节偏移
  传给底层 WritePage，是 VM 子系统的既有调用约定，本 spec 沿用而不引入
  额外换算；
- len：本次需要回写的字节数（>0；最大 PAGE_SIZE；若文件末页 < PAGE_SIZE
  则 len 是 GetDirtySize 返回的剩余字节）。

本路径 `VfsExfatWritePage` 与 `VfsExfatReadPage` 结构对称：构造一个零初始
化的临时 `struct file page_file`，把 vnode / pos / ops 装进去，然后调用
`VfsExfatWrite(&page_file, buffer, buflen)` 完成 RMW + 簇定位 + 扇区写。
所有不变量（持 ei->inode_lock 全程、不取 sbi->s_lock / bitmap_lock、按簇
链遍历、cluster RMW、short-write 语义、必要时调 truncate_extend）天然继
承自 VfsExfatWrite，不重复定义。

Out of scope（本 stage 显式不做）：
- pos 与 PAGE_SIZE 的对齐推断：完全信任 VM 调用约定；不做额外对齐校验。
- 写穿 vs 写回策略：LiteOS-A 无 page cache writeback queue，VfsExfatWrite
  已是同步写穿，本路径直接返回完成字节数。
- 文件 mtime 推进：留 Wave B6 fsync 一并处理（VfsExfatWrite 已在 size
  扩展时更新 valid_size，足够 LTP）。

锁模型：本函数自身不取锁；VfsExfatWrite 取 ei->inode_lock。严禁在持任何
LiteOS spinlock 时进入 WritePage。

[RELY]
```c
/* common.header 已冻结类型 */
typedef struct exfat_sb_info exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;

/* LiteOS-A VFS / VM 类型 */
struct file;
struct Vnode;

/* 复用同 stage 的 write 路径 */
extern ssize_t VfsExfatWrite(struct file *filep, const char *buf, size_t len);

/* file_operations_vfs 已由 exfat_ops.c 定义 */
extern struct file_operations_vfs g_exfatFops;

/* libsec / 内核基础原语 */
extern errno_t memset_s(void *dest, size_t destMax, int c, size_t count);

#define EINVAL 22
```

[GUARANTEE]
```c
/*
 * 调用约定：
 *   - VnodeOps.WritePage 回调，由 LiteOS-A VM 子系统 (kernel/base/vm/
 *     los_vm_filemap.c::OsFlushDirtyPage) 调用，常见触发点：munmap / 进程退
 *     出（OsDoFlushDirtyPage）、page-cache 回收（OsTryShrinkMemory）。
 *   - 调用方未持任何 exfat 锁；本函数自身亦不取锁，由内部 VfsExfatWrite
 *     取 ei->inode_lock。
 *   - 不取 sbi->s_lock（沿用 exfat_write.spec：write 路径不取）。
 *   - 不取 sbi->bitmap_lock（沿用：v1 简化—简单 in-place / truncate_extend
 *     已在 VfsExfatWrite 内部隔离簇分配的锁责任）。
 *   - 锁序：继承 VfsExfatWrite 的 ei->inode_lock 最外层规则。
 *
 * 返回值：
 *   - > 0 ：成功回写的字节数 N（0 < N <= buflen）。N < buflen 视为 short
 *           write，VM 调用方据此判定页是否依然 dirty（OsFlushDirtyPage 当
 *           前实现：ret <= 0 视为失败、ret > 0 视为成功并清 dirty 位）。
 *   - == 0：理论不发生；VfsExfatWrite 的 zero-len-fast-path 已在入口拦截
 *           （buflen 在 OsFlushDirtyPage 里恒为 GetDirtySize() > 0）。
 *   -  <0 ：负 POSIX errno；VM 视为本页回写失败（仅 PRINT_ERR；脏位保留
 *           直至下次 flush 重试或 force_umount）。
 *
 * 副作用：
 *   - 成功路径：在底层分区内一个或多个扇区被原子写入新数据（cluster RMW）。
 *   - 不修改入参 vnode 的 vop / fop / data / originMount 等字段；
 *     不修改 sbi 任何字段；
 *     仅由 VfsExfatWrite 在扩展场景下修改 ei->valid_size / ei->size /
 *     ei->i_size_ondisk / ei->start_clu / ei->flags（按已批准 exfat_write.spec
 *     / exfat_truncate_extend.spec 的不变量执行）。
 *   - 不修改任何全局；本函数仅写自身栈帧上的临时 struct file。
 *
 * Pre：
 *   - vnode != NULL && vnode->originMount != NULL && vnode->data 指向有效
 *     exfat_inode_info（ei->type == TYPE_FILE）。
 *   - buffer != NULL（VM 已为脏页建立内核映射）。
 *   - pos >= 0 && buflen > 0。
 *   - 调用方未持任何 LiteOS spinlock。
 */
extern ssize_t VfsExfatWritePage(struct Vnode *vnode, char *buffer, off_t pos,
                                  size_t buflen);
```

[SPECIFICATION]

**Pre-Condition**：
1. `vnode != NULL` 且 `buffer != NULL` 且 `pos >= 0` 且 `buflen > 0`。
2. `vnode->originMount != NULL` 且 `vnode->originMount->data` 是 `exfat_sb_info`。
3. `vnode->data` 是 `exfat_inode_info`，`type == TYPE_FILE`。
4. 调用方可睡眠，未持 LiteOS spinlock。

**Post-Condition (Case 1: Success)**：
- 栈上声明 `struct file page_file`，`memset_s` 清零。
- 设置 `page_file.f_vnode = vnode`；`page_file.f_pos = (loff_t)pos`；
  `page_file.ops = &g_exfatFops`。
- 调用 `VfsExfatWrite(&page_file, buffer, buflen)`，把返回值原样作为本函数
  返回值。
- VfsExfatWrite 按 exfat_write.spec 的语义把数据 RMW 到底层分区，必要时
  会调 exfat_truncate_extend（pos+buflen > ei->size 的边界尾页）。

**Post-Condition (Case 2: Args 校验失败)**：
- `vnode == NULL` 或 `buffer == NULL` 或 `pos < 0`：返回 `-EINVAL`；不构造
  page_file，不调用 VfsExfatWrite，不解引用 vnode/buffer。
- 注：`buflen == 0` 不在此处显式拒绝，由 VfsExfatWrite 的 zero-len-fast-path
  返回 0（与 read 路径对称；该路径在 OsFlushDirtyPage 中实际不会触发）。

**Post-Condition (Case 3: 写中途 IO 失败 / 簇链断裂)**：
- VfsExfatWrite 触发 short-write 时返回 0 < N < buflen，本函数原样返回 N。
- VfsExfatWrite 首次 IO 即失败时返回负 errno，本函数原样返回。

**Post-Condition (Case 4: 扩展写场景)**：
- 当 `pos + buflen > ei->size` 时，VfsExfatWrite 会先调 exfat_truncate_extend
  分配新簇；该子路径不在本 spec 重复声明，全权委托。

**Invariant** (id=exfat-writepage-stack-file-only)：
临时 `struct file page_file` 在函数栈上构造，零堆分配；返回即销毁。禁止
升级为缓存对象（同 readpage-stack-file-only）。

**Invariant** (id=exfat-writepage-no-locks)：
WritePage 自身不取/释锁、不检测锁状态；锁责任完全下沉至 VfsExfatWrite。

**Invariant** (id=exfat-writepage-no-spinlock-callsite)：
本函数体内含 VfsExfatWrite 调用，后者必睡眠（LOS_MuxLock + 可能的 alloc +
los_part_read + los_part_write）。调用方必须保证未持任何 LiteOS spinlock。
LiteOS-A VM 的 OsFlushDirtyPage 在调用 vop->WritePage 之前已释放 page-list
spinlock，约束满足。

**Invariant** (id=exfat-writepage-fops-self-ref)：
临时 page_file 的 `ops` 指向 `&g_exfatFops`，与真实文件一致；保持类型对
称，便于未来在该路径加 ops-based 子分派。

**Invariant** (id=exfat-writepage-rely-only-on-write)：
不重新实现簇链遍历 / cluster RMW / 扇区 IO / size 扩展；数据写入全部委托
给 VfsExfatWrite。任何写路径语义的修改（exfat_write.spec / exfat_truncate
_extend.spec）自动传染本路径，不在本 spec 复刻。

**Invariant** (id=exfat-writepage-no-extra-mutate)：
除 VfsExfatWrite 内部已声明的字段修改外，本路径不额外修改 sbi / ei / vnode
任何字段；不写额外日志（仅 VfsExfatWrite 自身 PRINT_ERR）。

**Invariant** (id=exfat-writepage-args-validation-early)：
参数校验在构造 page_file 之前；任何 -EINVAL 路径不触及任何外部状态。

**Invariant** (id=exfat-writepage-len-passthrough)：
传给 VfsExfatWrite 的 len 严格等于本函数入参 buflen；不在中间做 PAGE_SIZE
clamp（VM 框架已保证 buflen <= PAGE_SIZE，且尾页可能 < PAGE_SIZE）。
该不变量保证文件末页脏数据按实际有效字节回写，而非整页强制 4KB 覆盖。

**Invariant** (id=exfat-writepage-pos-untouched)：
VM 框架按 fpage->pgoff 推进，不依赖本函数返回时的 page_file.f_pos 值；
本函数也不向 vnode 写回 pos / 任何文件偏移；调用方真实 file 的 f_pos
不受影响。
