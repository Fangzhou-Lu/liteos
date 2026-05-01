[PROMPT]
exFAT 目录遍历 (4 个 VFS callback)。本阶段实现在 `g_exfatVops` 中挂的目录
枚举接口，使 `ls /mnt/exfat` 与 readdir(3) 可工作：

  - `VfsExfatOpendir`   —— 初始化 `fs_dirent_s` 内部游标 (fd_int_offset=0)
  - `VfsExfatReaddir`   —— 扫父目录 dentry 流，填充 `idir->fd_dir[]`
  - `VfsExfatClosedir`  —— 释放本 stage 没有分配的 per-dirent 资源 (no-op)
  - `VfsExfatRewinddir` —— 重置游标 (fd_int_offset=0, fd_position=0)

Readdir 是核心；Opendir/Closedir/Rewinddir 都是薄壳。

锁模型：以 Linux `fs/exfat/dir.c::exfat_iterate` 为事实标准——核心扫描全程
持 `sbi->s_lock` (line 229/285，与 lookup stage 同 mutex)。Opendir / Closedir
/ Rewinddir 仅操作传入 `fs_dirent_s` 自身的轻量字段，不读 sbi、不读父目录
in-memory 状态，故不取 sbi->s_lock。

复用前序 helper：
  - `exfat_get_dentry`         —— peek primary type
  - `exfat_get_dentry_set`     —— 拉完整 file dentry-set
  - `exfat_validate_dentry_set`—— SetChecksum 校验
  - `exfat_uni_to_utf8`        —— UTF-16 → UTF-8 转字符串

字符集：v1 仅 UTF-8 输出。"."/".." 不由本 stage 合成，由 VFS 上层 (与 fatfs
一致) 处理或由用户态 readdir 自行补——`ls` 借助 `getdents(2)` + libc 即可
看到。

策略：每次 Readdir 调用从 `idir->fd_int_offset` 续扫，至多填 `idir->read_cnt`
条；填够 / 父目录扫到 EXFAT_UNUSED / 簇链耗尽 / 触上界 任一条件即终止；
返回填充条数 (>= 0)；负数表示硬错。

[RELY]
```c
/* 共享导入与类型 (来自 spec/exfat/common.header)。*/
#include "los_typedef.h"
#include <errno.h>
#include "securec.h"
#include "los_mux.h"
#include "los_memory.h"
#include "los_printf.h"
#include "vnode.h"
#include "mount.h"
#include "exfat_raw.h"

/* fs/include/fs/dirent_fs.h 提供 struct fs_dirent_s + struct dirent。 */
#include "fs/dirent_fs.h"
#include <dirent.h>   /* DT_DIR / DT_REG */

extern uint32_t LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern uint32_t LOS_MuxUnlock(LosMux *mutex);
extern VOID    *LOS_MemAlloc(VOID *pool, UINT32 size);
extern UINT32   LOS_MemFree(VOID *pool, VOID *ptr);
extern UINT8   *m_aucSysMem0;

/* 已批准的 helper 导出 (来自 common.header)。 */
extern int  exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                             int entry_idx, struct exfat_dentry *out,
                             uint64_t *out_sector);
extern int  exfat_get_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
                                 int start_entry, struct exfat_dentry *set,
                                 int max_entries, int *num_entries);
extern int  exfat_validate_dentry_set(const struct exfat_dentry *set,
                                      int num_entries);
extern int  exfat_uni_to_utf8(const uint16_t *uni, int uni_len,
                              char *out, int out_max);

/* dirent.d_name 上限——典型 256，由 fs/include/fs/dirent.h 决定。 */

/* LE → host 包装器 (与 lookup / dentry stage 一致)。 */
#define LE16_TO_HOST(x) ((uint16_t)(x))
```

[GUARANTEE]
```c
/* 调用约定 (Opendir):
 *   - 调用方: VFS 经 vop->Opendir 调用; 进入时不持 exfat 锁。
 *   - 不取 sbi 锁——本函数仅初始化 idir 自身字段。
 *   - 副作用: idir->fd_int_offset = 0; idir->fd_position = 0;
 *             idir->u.fs_dir = NULL (本 stage 不分配每-dirent 私有状态)。
 *   - 返回: 0 成功; -EINVAL 入参 NULL 或 vnode 类型错。 */
int VfsExfatOpendir(struct Vnode *vp, struct fs_dirent_s *idir);

/* 调用约定 (Readdir):
 *   - 调用方: VFS 经 vop->Readdir 调用; 进入时不持 exfat 锁。
 *   - 本函数获取 sbi->s_lock (FS 全局 mutex)，全过程持有，返回前释放。
 *     与 Linux fs/exfat/dir.c::exfat_iterate (line 229/285 取/释放
 *     EXFAT_SB(sb)->s_lock) 严格 1:1 对应。
 *   - 不获取 vp inode_lock / bitmap_lock / inode_hash_lock —— Linux
 *     exfat readdir 路径同样不取。
 *   - 返回: 已填充的 dirent 条数 ∈ [0, read_cnt]；
 *           负数 (-EINVAL/-ENOMEM/-EIO) 表示硬错；返回 0 等价于到达目录尾。
 *   - 副作用 (仅成功路径): idir->fd_dir[0..N-1] 填 d_name/d_off/d_reclen/
 *     d_type；idir->fd_position += N；idir->fd_int_offset 推进到下一个
 *     未消费的 entry_idx。read_cnt==0 → 立即返回 0，不持锁。
 *   - 失败时 idir 不被部分修改 (Invariant exfat-readdir-no-mutate-on-fail)。
 *   - read-only: 不写盘。 */
int VfsExfatReaddir(struct Vnode *vp, struct fs_dirent_s *idir);

/* 调用约定 (Closedir):
 *   - 调用方: VFS 经 vop->Closedir 调用; 进入时不持 exfat 锁。
 *   - 不取任何锁。
 *   - 副作用: 本 stage Opendir 不分配资源，故 Closedir 无释放工作；
 *             仅做 NULL 校验返回。
 *   - 返回: 0 成功; -EINVAL 入参 NULL。 */
int VfsExfatClosedir(struct Vnode *vp, struct fs_dirent_s *idir);

/* 调用约定 (Rewinddir):
 *   - 调用方: VFS 经 vop->Rewinddir 调用; 进入时不持 exfat 锁。
 *   - 不取任何锁。
 *   - 副作用: idir->fd_int_offset = 0; idir->fd_position = 0;
 *             与 Opendir 同步重置。
 *   - 返回: 0 成功; -EINVAL 入参 NULL。 */
int VfsExfatRewinddir(struct Vnode *vp, struct fs_dirent_s *idir);
```

[SPECIFICATION]

**Pre-Condition (公共)**:
- `vp != NULL` 且 `vp->type == VNODE_TYPE_DIR`；
- `idir != NULL` (公共预设)；
- `vp->originMount` 已挂载，`mount->data` 指向 sbi (Readdir 才用到)；
- `vp->data` 指向已初始化的 `exfat_inode_info` (Readdir 才用到)；
- `idir->fd_dir[]` 数组容量 ≥ `idir->read_cnt`。

**Post-Condition for VfsExfatOpendir (Case 1: 成功)**:
- `idir->fd_int_offset = 0`；
- `idir->fd_position = 0`；
- `idir->u.fs_dir = NULL`；
- 返回 0；不持锁。

**Post-Condition for VfsExfatOpendir (Case 2: 入参非法 → -EINVAL)**:
- `vp == NULL` 或 `idir == NULL` 或 `vp->type != VNODE_TYPE_DIR`；
- `idir` 不变；返回 -EINVAL。

**Post-Condition for VfsExfatReaddir (Case 1: 填充 N>0 条，返回 N)**:
- 从 `idir->fd_int_offset` 起扫描父目录 dentry 流，对每个 in-use file
  primary (type==EXFAT_FILE 0x85) 取完整 dentry-set 校验 SetChecksum，
  解码 UTF-16 名字 → UTF-8，写入 `idir->fd_dir[i]`：
    - `d_name` = UTF-8 名字 (NUL 截尾，最多 d_name 槽容量-1 字节)
    - `d_type` = (attr & ATTR_SUBDIR) ? DT_DIR : DT_REG
    - `d_reclen` = sizeof(struct dirent)
    - `d_off`    = ++idir->fd_position
- 处理至 `i == read_cnt` 或扫到 EXFAT_UNUSED 或扫到 max_dentries 上界
  即停止；
- 返回 i (∈ [1, read_cnt])；
- `idir->fd_int_offset` 更新到下一个未消费的 entry_idx；
- `sbi->s_lock` 已释放。

**Post-Condition for VfsExfatReaddir (Case 2: 已到目录尾，返回 0)**:
- 从入口处或扫描中扫到 EXFAT_UNUSED；本次未填充任何 dirent；
- `idir->fd_dir[]` / `idir->fd_position` 未修改；
- `idir->fd_int_offset` 不变 (或修正为指向 UNUSED 位置)；
- 返回 0；`sbi->s_lock` 已释放。

**Post-Condition for VfsExfatReaddir (Case 3: 入参非法 / read_cnt=0)**:
- 触发：`vp==NULL`、`idir==NULL`、`vp->type != VNODE_TYPE_DIR`、
  `vp->data == NULL`、`mount->data == NULL` → -EINVAL；
- `idir->read_cnt <= 0` → 返回 0 (语义等同"无请求")，不持锁不修改 idir；
- 失败路径不持锁。

**Post-Condition for VfsExfatReaddir (Case 4: IO/校验失败 → -EIO)**:
- 触发：`exfat_get_dentry` / `exfat_get_dentry_set` 返回 -EIO 且本次
  调用尚未填充任何 dirent；
- v1 行为：与 lookup 一致——单 entry IO 错跳过推进；本调用全程零填充
  且至少一次 IO 错才返 -EIO；若已填充 ≥1 条，把已填充条数返回 (Case 1)；
- 失败时 `idir->fd_dir[]` / `idir->fd_position` 未部分修改超出已成功填充的
  范围；`sbi->s_lock` 已释放。

**Post-Condition for VfsExfatReaddir (Case 5: 内存分配失败 → -ENOMEM)**:
- 触发：dentry-set 缓冲 zalloc/LOS_MemAlloc 失败；
- `idir` 不变；`sbi->s_lock` 在加锁前/已释放后；返回 -ENOMEM。

**Post-Condition for VfsExfatClosedir (Case 1: 成功)**:
- 不释放任何 exfat 资源 (本 stage Opendir 未分配)；
- `idir` 不变；返回 0；不持锁。

**Post-Condition for VfsExfatClosedir (Case 2: 入参非法 → -EINVAL)**:
- `vp == NULL` 或 `idir == NULL`；返回 -EINVAL；不持锁。

**Post-Condition for VfsExfatRewinddir (Case 1: 成功)**:
- `idir->fd_int_offset = 0`；
- `idir->fd_position = 0`；
- 返回 0；不持锁。

**Post-Condition for VfsExfatRewinddir (Case 2: 入参非法 → -EINVAL)**:
- `vp == NULL` 或 `idir == NULL`；返回 -EINVAL；不持锁。

**Invariant** (id=exfat-readdir-readonly):
    本 stage 仅读盘——4 个 callback 任一都禁止调用 `los_part_write` /
    `los_disk_write`，禁止修改 sbi 任意字段，禁止修改 vp / vp->data 任意字段。

**Invariant** (id=exfat-readdir-linux-s-lock-faithful):
    `VfsExfatReaddir` 全程持 `sbi->s_lock`，与 Linux
    `fs/exfat/dir.c::exfat_iterate` 的锁模型 1:1 对应：扫描循环开始前
    `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)`，对应 Linux line 229
    的 `mutex_lock(&EXFAT_SB(sb)->s_lock)`；返回前在所有 `goto ERROR_*`
    标签后单点 `LOS_MuxUnlock(&sbi->s_lock)`。锁序：`s_lock` 在最外层。
    Opendir / Closedir / Rewinddir 不持任何锁——它们只动 `idir` 自身字段。

**Invariant** (id=exfat-readdir-no-per-inode-lock):
    Readdir **不**获取 `vp` 关联的 `parent_ei->inode_lock`，与 Linux exfat
    dir 代码一致——Linux 端 `exfat_iterate` 不直接取 per-inode 锁；inode
    `i_rwsem` 由 Linux generic VFS 在上层持有，与 exfat 代码无关。
    LiteOS-A v1 单线程 readdir 路径下，`s_lock` 已串行化所有 namei + 目录
    枚举操作。

**Invariant** (id=exfat-readdir-no-bitmap-lock):
    Readdir 不获取 `sbi->bitmap_lock`，与 Linux 一致——只读 dentry 流和
    FAT 链，不读位图字节。

**Invariant** (id=exfat-readdir-cursor-int-offset):
    游标存于 `idir->fd_int_offset` (off_t 视为 int 储存 entry_idx)。
    每次 Readdir 入口从该字段读起始 entry_idx，扫描结束写回下一个未消费
    的 entry_idx。`idir->u.fs_dir` 在本 stage 始终为 NULL (Opendir 不分配)。

**Invariant** (id=exfat-readdir-fd-position-monotone):
    每填充一条 dirent，`idir->fd_position++`，并将该值写入 `d_off`。
    `fd_position` 从未减小 (Rewinddir 强制重置除外)。

**Invariant** (id=exfat-readdir-utf8-only):
    v1 输出 UTF-8 文件名 (`exfat_uni_to_utf8`)。若解码失败 (转 UTF-8 越
    `d_name` 容量) 则跳过该 entry 推进，不计入返回条数。

**Invariant** (id=exfat-readdir-stop-on-unused):
    扫描父目录 dentry 流时，`exfat_get_dentry` 读到 `EXFAT_UNUSED 0x00`
    primary 即视为目录尾，停止扫描；`idir->fd_int_offset` 不再向后推进。

**Invariant** (id=exfat-readdir-skip-non-file-primary):
    只有 in-use EXFAT_FILE (0x85) primary 才计入返回；
    EXFAT_BITMAP (0x81) / EXFAT_UPCASE (0x82) / EXFAT_VOLUME (0x83) /
    deleted (高位清零的 0x05/...) 一律跳过 (entry_idx++ 推进)。

**Invariant** (id=exfat-readdir-d-type-from-attr):
    `d_type` = (attr & ATTR_SUBDIR) ? `DT_DIR` : `DT_REG`。其余 dentry
    属性位在 v1 不映射。

**Invariant** (id=exfat-readdir-le-host-only):
    所有 packed dentry 字段读取经 LE16/32_TO_HOST 包装 (与
    `exfat-lookup-le-host-only` 相同形态)；ARMv7-A LE host 上宏退化为
    identity。

**Invariant** (id=exfat-readdir-leak-free):
    Readdir 中 `LOS_MemAlloc` 的临时 dentry-set 缓冲在所有返回路径
    (无论成功/失败/早返) 释放。通过 `goto ERROR_<step>:` 标签栈实现。

**Invariant** (id=exfat-readdir-no-mutate-on-fail):
    返回 < 0 (硬错) 时，`idir->fd_dir[]` / `fd_position` / `fd_int_offset`
    保持调用前原值——不部分写入。
    (注：返回 N≥1 表示已成功填充 N 条，是合法的"部分进展"，不属此条。)

**Invariant** (id=exfat-readdir-bounded):
    扫描 entry_idx 上界与 lookup 同：`parent_ei->size / DENTRY_SIZE`，
    且不超过 `MAX_EXFAT_DENTRIES = 8388608`。超界视作目录损坏 → -EIO。

**Invariant** (id=exfat-readdir-no-spinlock-callsite):
    Readdir 内部调用 `LOS_MemAlloc` / `LOS_MuxLock(s_lock, ...)` /
    `los_part_read` (经 helper) 都可能睡眠。调用方禁止在持
    `LOS_SpinLock` 时调用本 callback。Opendir/Closedir/Rewinddir 不睡
    眠 (纯字段操作)，但保留同样的禁忌。

## Refine Prompt

加锁与并发约定 (与功能规范分离的第二轮关注点；以 Linux
`fs/exfat/dir.c::exfat_iterate` 为事实标准)：

1. **Readdir 持锁范围 = `sbi->s_lock` 全程**：参数检查通过 (NULL / type /
   read_cnt) 后即 `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)`；返回前
   在所有 `goto ERROR_*` 标签后单点 `LOS_MuxUnlock`。Linux line 229
   `mutex_lock(&EXFAT_SB(sb)->s_lock)` + line 285/298 `mutex_unlock` 的
   1:1 对齐。

2. **Linux 在 dir_emit 前后 unlock/relock s_lock 是 page-fault 适配**：
   Linux line 285/289 在 `dir_emit()` 调用前后释放再重取 s_lock，因为
   `dir_emit` 可能触发用户态拷贝 page fault。LiteOS-A 的 `idir->fd_dir[]`
   是内核缓冲 (由 VFS 在 readdir 系统调用栈上分配)，**不**触 user copy
   on fault，故无需 lock-shimming。本 stage 在持锁全程内填 fd_dir，
   返回前再统一释放。

3. **Opendir / Closedir / Rewinddir 不取任何 exfat 锁**：它们仅写入
   `idir` 自身的轻量字段 (fd_int_offset / fd_position / u.fs_dir)。
   `idir` 是 caller 私有 (per-fd)，无并发竞争；不读 sbi/parent_ei 任何
   字段。

4. **不取 `vp` 关联的 `inode_lock`**：Linux exfat dir 代码不取，本移植
   对齐。`s_lock` FS 全局串行化已涵盖。

5. **不取 `bitmap_lock` / `inode_hash_lock`**：read 路径与 lookup 一致
   不分配/不查 inode hash。

6. **锁序约定**：`s_lock` 在最外层。Wave A 仅读路径，无次级锁需求。

7. **自旋锁 callsite 约束** (重申)：Readdir 路径含 `LOS_MemAlloc` 与
   `los_part_read`，可能睡眠；caller 不得在持 `LOS_SpinLock` 时调用。
   Opendir / Closedir / Rewinddir 不分配/不 IO，但保留同等禁忌以与
   Readdir 一致。

8. **重入安全**：`exfat_uni_to_utf8` 是 pure (Invariant
   `exfat-nls-utf16-pure`)；`exfat_get_dentry` / `exfat_get_dentry_set`
   本身无锁，由 caller (本 Readdir) 经 `s_lock` 串行化。

## Assumptions made
- `idir->fd_dir[i].d_name` 的容量来自 `struct dirent` 的 `d_name` 数组
  长度 (典型 256 字节，含 NUL)。`exfat_uni_to_utf8` 输出超过容量时
  跳过该 entry (Invariant `exfat-readdir-utf8-only`)。
- "." / ".." 不由本 stage 合成；VFS 上层或 libc 自行补 (与 fatfs 一致)。
- `idir->u.fs_dir` 在本 stage 不使用，保持 NULL；后续 stage (例如
  hint-cache 优化) 可在此处接入小型私有状态而不破坏本 stage 契约。
