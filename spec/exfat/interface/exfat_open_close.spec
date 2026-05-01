[PROMPT]
将 Linux `fs/exfat/file.c` 中省略不实现（默认 `generic_file_open` / 无 release）
的 `.open`/`.release` 行为，移植为 LiteOS-A 端 file_operations_vfs 的 `.open` /
`.close` 显式回调 `VfsExfatOpen` / `VfsExfatClose`。

Linux 上 exfat_file_operations 不写 `.open` 与 `.release`，等于走 VFS 默认：
- `.open` = `generic_file_open`（仅校验 O_LARGEFILE 与文件大小关系，无 FS 私有
  状态分配）
- `.release` = 无（结构体留空，VFS 在 `__fput` 时跳过 FS 回调）

LiteOS-A 的 file_operations_vfs 没有"默认空实现"机制——slot 为 NULL 时 sys_open
/ sys_close 行为不一定退化得干净（部分路径直接解引用），且后续 Wave B 想加
O_TRUNC、O_APPEND 处理时找不到挂载点。Wave A 提供两个最小桩：

- `VfsExfatOpen(filep)`：校验 `f_vnode->type == VNODE_TYPE_REG`、`ei->type ==
  TYPE_FILE`；不分配任何 per-open 状态（read 路径直接用 `vp->data`）；不取任何
  exfat 锁；返回 0 或 `-EISDIR` / `-EINVAL`。语义对齐 Linux generic_file_open。
- `VfsExfatClose(filep)`：直接返回 0。无 per-open 状态可释放；不取锁；不刷盘
  （Wave A 只读）。语义对齐 Linux exfat 不实现 `.release`（VFS 自然路径）。

两者都标注 `Wave A 桩`：Wave B 写路径（O_TRUNC / O_APPEND / 写时分配/落盘）将
扩展同一文件，添加 sbi->s_lock、bitmap_lock 等。

输出阶段必填：把 `g_exfatFops` 由 `{ .read = VfsExfatRead }` 扩展为
`{ .open = VfsExfatOpen, .close = VfsExfatClose, .read = VfsExfatRead }`
（其他 slot 仍为 NULL,待 Stage 9 vfs_ops_filled refine 补齐 seek 等）。

[RELY]
```c
/* common.header 已冻结类型 */
typedef struct exfat_sb_info exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;

/* TYPE_* 常量（fs/exfat/include/exfat.h，由 common.header 间接引入）*/
#define TYPE_FILE  0x011Fu
#define TYPE_DIR   0x0104u

/* LiteOS-A VFS 类型 */
struct file;             /* 含 f_vnode、f_pos、f_priv、f_oflags、ops、fd */
struct Vnode;            /* 含 type、data、originMount */
typedef struct Vnode Vnode;

/* VnodeType 枚举（fs/vfs/include/vnode.h:95-98）*/
enum VnodeType {
    VNODE_TYPE_UNKNOWN = 0,
    VNODE_TYPE_REG,         /* regular file */
    VNODE_TYPE_DIR,         /* directory */
    /* ...（v1 仅用前 3 个）*/
};

/* errno（POSIX 负值边界返回）*/
#define EINVAL  22
#define EISDIR  21

/* logging（debug 路径仅；生产路径静默）*/
extern void PRINT_DEBUG(const char *fmt, ...);
```

[GUARANTEE]
```c
/* === Public exports (declared in fs/exfat/include/exfat.h) =========== */

/*
 * VfsExfatOpen — file_operations_vfs.open 回调。
 *
 * 调用约定：
 *   - 由 sys_open(2) 在 vnode 解析完毕、struct file 分配并 f_vnode/ops 已就位
 *     后调用。本函数不修改 filep->f_pos（VFS 已置 0）、不修改 filep->f_priv。
 *   - 不取任何 exfat 锁（Wave A 桩；Linux generic_file_open 同样不取）。
 *   - 不分配内存。Wave A 不持有 per-open 状态——所有元数据自 vp->data 取。
 *   - 不刷盘、不修改 sbi/ei 任何字段。
 *
 * 返回值：
 *   - 0          ：常规文件打开成功，可继续 read。
 *   - -EISDIR    ：目标为目录（应走 opendir/readdir 路径）。
 *   - -EINVAL    ：filep == NULL / f_vnode == NULL / vp->data == NULL /
 *                  ei->type 非 TYPE_FILE 也非 TYPE_DIR。
 *
 * 副作用：无（保留 Linux 上 generic_file_open 的"软"语义）。
 *
 * Pre：filep != NULL && filep->f_vnode != NULL && f_vnode->originMount != NULL；
 *      VFS 已通过 lookup 链建立 vp。
 */
extern int VfsExfatOpen(struct file *filep);

/*
 * VfsExfatClose — file_operations_vfs.close 回调。
 *
 * 调用约定：
 *   - 由 sys_close(2) 经 file refcount 减至零路径调用。Wave A 不分配 per-open
 *     状态，故无释放责任；返回 0 即可。
 *   - 不取任何 exfat 锁（Wave A 桩；Linux exfat 不实现 .release，对应 VFS 默认
 *     是无 FS 回调）。
 *   - 不刷盘（Wave A 只读）。Wave B 写路径将扩展为 sbi->s_lock 持有期间调用
 *     fsync/truncate-on-close 等。
 *
 * 返回值：
 *   - 0          ：始终返回 0。VFS 不需要 close 失败的语义（POSIX close(2)
 *                  失败仅产生 errno 不阻止 fd 释放）。
 *
 * 副作用：无。
 *
 * Pre：filep != NULL（VFS 保证；本函数额外断言 NULL 输入也安全返回 0）。
 */
extern int VfsExfatClose(struct file *filep);
```

[SPECIFICATION]
**Pre-Condition (Open)**：
1. `filep != NULL` 且 `filep->f_vnode != NULL` 且 `filep->f_vnode->originMount != NULL`。
2. `vp = filep->f_vnode` 由 VFS path-walk 阶段设置（lookup 已为常规文件返回
   `VNODE_TYPE_REG` vnode；为目录返回 `VNODE_TYPE_DIR` vnode）。
3. `ei = vp->data` 由 lookup 阶段安装的 exfat_inode_info；`ei->type` 在
   `{TYPE_FILE, TYPE_DIR}` 之一。
4. `filep->f_pos` 已由 VFS 在 file 分配时初始化为 0；`filep->f_priv` 初始为 NULL。
5. `filep->f_oflags` 已由 sys_open 解析自用户 flag。

**Post-Condition (Open Case 1: 常规文件成功)**：
- `vp->type == VNODE_TYPE_REG && ei->type == TYPE_FILE`：返回 0；
  `filep->f_priv` 不变（保持 NULL）；`filep->f_pos` 不变（保持 0）；
  不取锁、不分配、不读盘。

**Post-Condition (Open Case 2: 目录被当文件打开)**：
- `vp->type == VNODE_TYPE_DIR || ei->type == TYPE_DIR`：返回 `-EISDIR`；
  状态不变。VFS 上层一般通过 sys_opendir 走 g_exfatVops.Opendir 路径，
  此分支作为最后一道断言（少数构造路径可能透传过来）。

**Post-Condition (Open Case 3: 入参非法)**：
- `filep == NULL || vp == NULL || vp->originMount == NULL || vp->data == NULL`：
  返回 `-EINVAL`；状态不变。
- `ei->type` 既非 TYPE_FILE 又非 TYPE_DIR：返回 `-EINVAL`。

**Pre-Condition (Close)**：
1. `filep != NULL`（本函数额外允许 NULL 输入直接返回 0）。
2. 若 Open 曾被调用并返回 0，则 `filep->f_priv` 为 NULL（Wave A 不分配）；
   若 Open 失败或未调用，`filep->f_priv` 任意值——本函数都不解引用。

**Post-Condition (Close)**：
- 始终返回 0。`filep->f_priv` / `filep->f_pos` / sbi / ei 全部不变；不取锁，
  不分配，不刷盘。

**Invariant** (id=exfat-open-no-alloc)：
VfsExfatOpen 不调用 `LOS_MemAlloc`、`zalloc` 或任何分配原语；不修改
`filep->f_priv`（保持 sys_open 初始化的 NULL）。Wave A 所有 per-open 元数据从
`vp->data` 取，无需独立 per-open 结构。Wave B 写路径若需引入（如脏页跟踪），
应在 spec evolve 时显式扩展并通过 inherited invariant 解除本约束。

**Invariant** (id=exfat-open-no-lock)：
VfsExfatOpen 不取任何 exfat 锁（sbi->s_lock / sbi->bitmap_lock /
sbi->inode_hash_lock / ei->inode_lock 一概不取）。Mirror Linux
`generic_file_open` 的无锁语义。

**Invariant** (id=exfat-open-rejects-dir)：
检测到 vp 是目录类型（VNODE_TYPE_DIR 或 ei->type == TYPE_DIR）时立即返回
`-EISDIR`，绝不进入 read fastpath。VFS 上层用 opendir/readdir 处理目录；
此分支兜底防御非常规调用路径。

**Invariant** (id=exfat-open-no-side-effects)：
VfsExfatOpen 不修改 sbi 任何字段、不修改 ei 任何字段、不修改 vp 任何字段。
仅做参数校验后返回 0/-EISDIR/-EINVAL。filep 自身的 f_pos / f_priv 也保持
sys_open 初始化的 0/NULL 值。

**Invariant** (id=exfat-open-pos-untouched)：
不重置 filep->f_pos：sys_open 保证调用本函数前 f_pos == 0；本函数无须再写入。
（与 Linux generic_file_open 一致——它也不主动 seek。）

**Invariant** (id=exfat-open-flags-not-enforced)：
Wave A 不在 Open 阶段拒绝 O_WRONLY/O_RDWR/O_TRUNC/O_APPEND——失败语义由后续
write/seek 路径以"slot == NULL → -ENOSYS"自然兜底。Linux 同样不在
generic_file_open 拒绝写 flag；exfat 写失败发生在 .write_iter 调用未实现
（Wave B 之前）的更下游。

**Invariant** (id=exfat-close-no-alloc-no-free)：
VfsExfatClose 不分配、不释放（因为 Open 不分配）。无 LOS_MemFree 调用。
若 Wave B 引入 per-open 状态，必须配套扩展本函数的释放路径并新增 invariant
解除本约束。

**Invariant** (id=exfat-close-always-zero)：
VfsExfatClose 始终返回 0；不存在 -errno 路径。POSIX close(2) 的失败仅由更上层
（fd 表回收等）产生；FS 层不引入新的失败语义。

**Invariant** (id=exfat-close-no-lock)：
VfsExfatClose 不取任何 exfat 锁。Wave A 只读、无 dirty 状态、无 fsync 责任。
Wave B 引入写时，本约束应在 spec evolve 时显式解除。

**Invariant** (id=exfat-open-close-no-spinlock-callsite)：
本两个回调既无 LOS_MemAlloc 也无 los_part_read，理论上 spinlock-safe。但
仍约束调用方不要在持自旋锁时进入——VFS 上层默认不持自旋锁，约束自然成立；
此 invariant 的目的是保留与其他 exfat callbacks 同构的"无 spinlock 调用方"
契约，避免 Wave B 扩展时遗忘重新评估。

**Invariant** (id=exfat-open-close-linux-faithful)：
VfsExfatOpen 对应 Linux 默认 .open = generic_file_open（exfat 不覆盖）的语义；
VfsExfatClose 对应 Linux exfat 不实现 .release（VFS 自然路径）的语义。两者
共同确保：在 read-only Wave A，open/close 是 read 系统调用 lifecycle 的
"轻量壳"——所有真实工作发生在 read 路径，open/close 只做合法性断言。

**Invariant** (id=exfat-open-close-trace-silent-by-default)：
不在生产路径输出日志。如需调试，调用方应用 `PRINT_DEBUG`（默认编译时关闭）
而非 `PRINT_INFO`。Wave A 期望 cat 大文件时无 noise burst。

**Invariant** (id=exfat-open-no-mutate-on-failure)：
Open 任意 -errno 返回路径不修改 filep / vp / ei / sbi 任何字段。仅传播
NULL/类型错误。
