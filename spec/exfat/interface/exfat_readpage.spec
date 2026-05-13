[PROMPT]
LiteOS-A 的虚拟内存子系统支持 mmap 文件后通过缺页异常按页装载文件数据。
当用户对一个 exFAT 文件建立私有 / 共享 mmap 之后，访问对应虚拟地址会触发
`OsVmmFileFault → ... → vnode->vop->ReadPage(vnode, kvaddr, pgoff*PAGE_SIZE)`
回调；该回调必须从底层文件系统拉取一整页（PAGE_SIZE = 4096B）数据到内核分
配好的物理页 kvaddr 处。LiteOS-A VM 的契约保证：

- ReadPage 的调用上下文已持 page-cache 相关 spinlock 之外的页帧引用；
  调用方允许底层睡眠 / 阻塞 IO，但禁止持 LiteOS spinlock 进入该回调。
- ReadPage 必须返回 ssize_t：>=0 表示已成功装载的字节数（pgoff 越过文件
  EOF 时返回 0 即可，调用方自行补零）；<0 视为页装载失败，VM 会把 page
  fault 翻译成 SIGBUS 或 OOM-kill。
- ReadPage 不得修改 vnode 任何状态字段；不得改 file table；不得调度 IO
  写路径——只读语义。

本路径 `VfsExfatReadPage` 是 exFAT 端的 ReadPage 实现。设计上把 ReadPage
化简为「以 (pos, PAGE_SIZE) 调用一次 VfsExfatRead」：构造一个零初始化的临时
`struct file page_file`，把 vnode、pos、ops 装进去，让 VfsExfatRead 的整套
锁模型 + 簇链遍历 + RMW 逻辑去完成数据搬运。这样：

1. VfsExfatRead 的所有不变量（持 ei->inode_lock 全程、不取 sbi->s_lock、不
   修改 sbi/ei、按簇链遍历、short-read 语义）天然继承到 ReadPage 路径上，
   spec/code 不重复定义。
2. ReadPage 自身只承担「参数校验 + 临时 file 构造 + 调用 + 直接返回」四步，
   不持锁、不分配、不直接 IO。
3. 临时 `page_file` 是 stack 局部对象，pos 字段由 ReadPage 入参 pos 设定，
   返回后不影响调用方真实 file 的 f_pos；VM 的 pgoff 推进由 VM 自己负责。
4. 4096u (PAGE_SIZE) 在 LiteOS-A 是与 VM 子系统对齐的宏值，本 spec 锁定
   该常量；任何对常量的修改（例如未来支持 16KB page）必须重新走 evolve。

锁模型对齐 VfsExfatRead（参见 exfat_read.spec）：
- ReadPage 自身不取锁；
- 内部调用 VfsExfatRead 取 ei->inode_lock；
- 严格禁止在持任何 LiteOS spinlock 时进入 ReadPage。

历史背景（不进入 [SPECIFICATION] 主体，仅 prompt 解释为何引入）：在
WritePage / ReadPage 接线之前，exFAT vnode 的 vop->ReadPage 是 NULL；运行
LTP 时只要触发 mmap → page fault，OsVmmFileFault 就会跳 NULL 函数指针造成
prefetch_abort（PC=0，远端 fsr=0x5）。补完该回调后 mmap 路径才能正常装页。

[RELY]
```c
/* common.header 已冻结类型 */
typedef struct exfat_sb_info exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;

/* LiteOS-A VFS / VM 类型 */
struct file;        /* 含 f_vnode / f_pos / ops / f_priv */
struct Vnode;       /* 含 type / data / originMount / vop / fop */

/* 复用同 stage 的 read 路径 */
extern ssize_t VfsExfatRead(struct file *filep, char *buf, size_t len);

/* file_operations_vfs 已由本翻译单元定义（exfat_ops.c） */
extern struct file_operations_vfs g_exfatFops;

/* libsec / 内核基础原语 */
extern errno_t memset_s(void *dest, size_t destMax, int c, size_t count);

/* 页大小：LiteOS-A 当前固定 4KB；与 los_vm_zone.h / OsNamedMMap 对齐 */
#define EXFAT_PAGE_SIZE 4096u

/* 边界 errno（VFS 边界负值返回） */
#define EINVAL 22
```

[GUARANTEE]
```c
/*
 * 调用约定：
 *   - VnodeOps.ReadPage 回调，由 LiteOS-A VM (kernel/base/vm/los_vm_filemap.c
 *     OsVmmFileFault → g_commVmOps.fault 路径) 调用。
 *   - 调用方未持任何 exfat 锁；本函数自身亦不取锁，由内部 VfsExfatRead
 *     取 ei->inode_lock（mirror exfat_read.spec 的锁模型）。
 *   - 不取 sbi->s_lock / sbi->bitmap_lock / sbi->inode_hash_lock。
 *   - 锁序：本函数继承 VfsExfatRead 的锁序——ei->inode_lock 在最外层。
 *
 * 返回值：
 *   - >= 0 ：成功装载的字节数 N（0 <= N <= PAGE_SIZE）。N < PAGE_SIZE 是
 *           合法的：包括 pos >= ei->size 时的 0 字节、跨过 EOF 的部分页、
 *           以及 VfsExfatRead 内部中途 IO 失败时的 short-read。VM 调用方
 *           对未填满的尾部自行补零。
 *   -   <0 ：负 POSIX errno；表示参数校验失败或底层 read 立即失败（尚未
 *           拷贝任何字节）。VM 把负返回翻译为 SIGBUS / OOM。
 *
 * 副作用：
 *   - 不修改入参 vnode 任何字段；不修改 sbi / ei 任何字段（继承 read 路径）。
 *   - 不修改任何全局；不分配堆；不向磁盘写。
 *   - 仅写本函数栈帧上的临时 struct file（栈对象，函数返回即销毁）。
 *
 * Pre：
 *   - vnode != NULL && vnode->originMount != NULL && vnode->data 指向有效
 *     exfat_inode_info（ei->type == TYPE_FILE）。
 *   - buffer != NULL（VM 已为页帧建立内核映射）。
 *   - pos >= 0（VM 已按 pgoff * PAGE_SIZE 派发，必为 >= 0 且页对齐；本函数
 *     不强制要求页对齐——只要求非负即可）。
 *   - 调用方未持任何 LiteOS spinlock。
 */
extern ssize_t VfsExfatReadPage(struct Vnode *vnode, char *buffer, off_t pos);
```

[SPECIFICATION]

**Pre-Condition**：
1. `vnode != NULL` 且 `buffer != NULL` 且 `pos >= 0`。
2. `vnode->originMount != NULL` 且 `vnode->originMount->data` 是有效的
   `exfat_sb_info`。
3. `vnode->data` 是有效的 `exfat_inode_info`，`type == TYPE_FILE`。
4. 调用方位于可睡眠上下文，未持任何 LiteOS spinlock。

**Post-Condition (Case 1: Success)**：
- 在栈上声明 `struct file page_file`，用 `memset_s` 清零。
- 设置 `page_file.f_vnode = vnode`、`page_file.f_pos = (loff_t)pos`、
  `page_file.ops = &g_exfatFops`。
- 调用 `VfsExfatRead(&page_file, buffer, EXFAT_PAGE_SIZE)`，把其返回值
  原样作为本函数返回值。
- VfsExfatRead 内部按其既定语义（持 ei->inode_lock、簇链遍历、cluster
  RMW）装载数据；本函数不二次包装错误码。

**Post-Condition (Case 2: Args 校验失败)**：
- `vnode == NULL` 或 `buffer == NULL` 或 `pos < 0`：直接返回 `-EINVAL`；
  不构造 page_file，不调用 VfsExfatRead，不解引用 vnode/buffer。

**Post-Condition (Case 3: EOF / 越界页)**：
- 若 `pos >= ei->size`：经由 VfsExfatRead 的 -eof-returns-zero 不变量直接
  得到 0；本函数原样返回 0。VM 调用方将整页填零。

**Post-Condition (Case 4: 部分页 IO 失败)**：
- VfsExfatRead 内部 mid-loop IO 失败时按其 short-read 语义返回已拷字节数 N
  （0 < N < EXFAT_PAGE_SIZE）；本函数返回该 N，VM 调用方对尾部补零。

**Post-Condition (Case 5: 首次 IO 即失败 / 簇链断裂)**：
- VfsExfatRead 返回负 errno；本函数原样返回负值。VM 翻译为 SIGBUS。

**Invariant** (id=exfat-readpage-stack-file-only)：
临时 `struct file page_file` 在函数栈上构造，无堆分配；返回后栈帧销毁。
本不变量禁止把 page_file 升级为 static / heap-allocated 缓存（一旦缓存
就需要 inode_hash_lock 之类的同步原语，违反「ReadPage 不持任何锁」契约）。

**Invariant** (id=exfat-readpage-no-locks)：
ReadPage 自身不取锁、不释锁、不检测锁状态。锁责任完全下沉到 VfsExfatRead。
（继承 exfat-read-* 锁系列；本路径只是 thin shim。）

**Invariant** (id=exfat-readpage-no-mutate)：
本函数任意返回路径下不修改 vnode、sbi、ei 任何字段；不向磁盘写；不修改
任何全局变量。栈上 page_file 是唯一可写对象，函数返回即销毁。

**Invariant** (id=exfat-readpage-fixed-page-size)：
本路径硬编码 `EXFAT_PAGE_SIZE = 4096u`，与 LiteOS-A VM `PAGE_SIZE` 一致。
未来支持非 4KB 页（如 ARM 16KB / 64KB） **必须** 走 evolve 而非默默改宏。

**Invariant** (id=exfat-readpage-fops-self-ref)：
临时 page_file 的 `ops` 字段指向 `&g_exfatFops`，与真实 file 通过 lookup
路径获得的 ops 完全一致。这样 VfsExfatRead 在做参数校验时（虽然实际不读
ops）保持类型对称，便于未来在该路径上加 ops-based 子分派。

**Invariant** (id=exfat-readpage-rely-only-on-read)：
本路径**不**重新实现簇链遍历、cluster RMW、扇区 IO；所有数据搬运委托给
VfsExfatRead。任何对该函数语义的修改（例如：将来按 valid_size 截断）
必须在 exfat_read.spec 修改一次，本路径自动受益；禁止在 ReadPage 内做
read 路径已经处理过的二次保护。

**Invariant** (id=exfat-readpage-zero-trailing-by-caller)：
当 pos + PAGE_SIZE 超过 ei->size 时，本函数返回的字节数 N < PAGE_SIZE 是
合法行为；buffer[N .. PAGE_SIZE-1] 由 LiteOS-A VM 框架按已建立的 page
cache 协议自行补零，不在本路径职责内（继承 OsVmmFileFault 既有行为）。

**Invariant** (id=exfat-readpage-no-spinlock-callsite)：
本函数体内含 VfsExfatRead 调用，后者可能睡眠（LOS_MuxLock + LOS_MemAlloc +
los_part_read）。调用方必须保证未持任何 LiteOS spinlock。VM 缺页路径
默认满足该约束（page fault handler 释放 spinlock 后才回调 vop->ReadPage）。

**Invariant** (id=exfat-readpage-args-validation-early)：
参数校验在构造 page_file 之前；任何 -EINVAL 路径不触及任何外部状态。
该不变量保证错误注入测试可以稳定复现，不依赖 memset_s / 栈对齐细节。
