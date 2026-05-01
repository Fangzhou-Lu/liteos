[PROMPT]
exFAT 目录查找 (VFS callback)。本阶段实现 `VfsExfatLookup`：在父目录 vnode
所代表的目录中按名字定位一个表项，命中则分配一个新 Vnode (类型由 dentry
ATTR_SUBDIR 决定) 并通过 `*vpp` 返回；未命中返回 `-ENOENT`。

读路径串联 4 个已批准的 helper：
1. `exfat_utf8_to_uni`     —— 把 VFS 传入的 UTF-8 名字 (name, len) 解码为 UTF-16；
2. `exfat_get_dentry_set`  —— 在父目录的簇链上以 dentry 索引枚举完整 dentry 集合；
3. `exfat_validate_dentry_set` —— 校验 set 内 SetChecksum；
4. `exfat_uniname_cmp`     —— 用 sbi->vol_utbl 做大小写不敏感比较。

命中后：`exfat_inode_alloc` 申请 in-memory inode，`VnodeAlloc` 申请 VFS Vnode，
按 ATTR_SUBDIR 位决定 `vp->type`，挂 inode 到 `vp->data`，按 `i_pos =
(start_clu << 32) | entry_idx` 算出哈希值后 `VfsHashInsert`。

锁模型：以 Linux `fs/exfat/namei.c::exfat_lookup` 为事实标准——全程持
`sbi->s_lock` (FS 全局 mutex)，与 Linux 的 `mutex_lock(&EXFAT_SB(sb)->s_lock)`
严格 1:1 对应。Linux exfat namei 代码本身不取 per-inode 锁，本移植同样不取
`parent_ei->inode_lock`。`bitmap_lock` / `inode_hash_lock` 在 lookup 路径上
亦不获取（Linux 一致）。

字符集：v1 仅 UTF-8；非 UTF-8 / 长度越界 → `-EINVAL` 或 `-ENAMETOOLONG`。
分配：失败路径用 goto-stack 反向 LIFO 释放 inode/vnode/buf；任一失败 `*vpp` 不变。
严格 read-only：不写盘，不调 `los_part_write`，不修改 sbi/parent_ei 任意字段。

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

/* exFAT 在内存的 sbi / inode_info / chain 与全局常量从 common.header 取。*/
extern uint32_t LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern uint32_t LOS_MuxUnlock(LosMux *mutex);
extern VOID    *LOS_MemAlloc(VOID *pool, UINT32 size);
extern UINT32   LOS_MemFree(VOID *pool, VOID *ptr);
extern VOID    *zalloc(size_t sz);
extern UINT8   *m_aucSysMem0;

/* VFS 适配 (fs/vfs/include/vnode.h)。*/
extern int VnodeAlloc(struct VnodeOps *vop, struct Vnode **newVnode);
extern int VfsHashInsert(struct Vnode *vnode, uint32_t hash);
extern struct VnodeOps             g_exfatVops;
extern struct file_operations_vfs  g_exfatFops;

/* 已批准的 helper 导出 (来自 common.header)。 */
extern int  exfat_utf8_to_uni(const char *utf8, int utf8_len, uint16_t *uni,
                              int uni_max, int *uni_len);
extern int  exfat_uniname_cmp(const exfat_sb_info *sbi, const uint16_t *a,
                              int a_len, const uint16_t *b, int b_len);
extern int  exfat_get_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
                                 int start_entry, struct exfat_dentry *set,
                                 int max_entries, int *num_entries);
extern int  exfat_validate_dentry_set(const struct exfat_dentry *set,
                                      int num_entries);
extern int  exfat_inode_alloc(exfat_inode_info **out);
extern void exfat_inode_free(exfat_inode_info *ei);
extern void exfat_inode_init_dir_chain(exfat_inode_info *ei, uint32_t start_clu);

/* 直接读 packed dentry 字段 (LE → host)；与 fat_chain stage 一致以 macro 包装，
 * 后续 BE 端口只改这两处即可 (Invariant exfat-lookup-le-host-only)。 */
#define LE16_TO_HOST(x) ((uint16_t)(x))
#define LE32_TO_HOST(x) ((uint32_t)(x))
#define LE64_TO_HOST(x) ((uint64_t)(x))
```

[GUARANTEE]
```c
/* 调用约定:
 *   - 调用方: VFS 通过 parent->vop->Lookup 调用; 进入时不持任何 exfat 锁。
 *   - 本函数获取 sbi->s_lock (FS 全局 mutex)，全过程持有，返回前释放。
 *     与 Linux fs/exfat/namei.c::exfat_lookup 取 EXFAT_SB(sb)->s_lock 严格 1:1。
 *   - 不获取 parent_ei->inode_lock —— Linux exfat namei 代码不取，本移植对齐。
 *   - 不获取 sbi->bitmap_lock / sbi->inode_hash_lock —— lookup 路径上 Linux 同样不取。
 *   - 返回值: 命中=0; 未找到=-ENOENT; 长度越界=-ENAMETOOLONG;
 *             非法 UTF-8 / len<=0 / parent 类型错=-EINVAL;
 *             分配失败=-ENOMEM; IO/校验失败=-EIO。
 *   - 副作用 (仅成功路径): 分配 1 个 exfat_inode_info + 1 个 Vnode，
 *     vp->vop=&g_exfatVops, vp->fop=&g_exfatFops, vp->data=ei,
 *     vp->originMount=parent->originMount, vp->parent=parent,
 *     vp->type 按 ATTR_SUBDIR 决定，并写入 *vpp。
 *   - 失败路径: *vpp 保持调用方原值不变 (Invariant exfat-lookup-no-mutate-on-failure);
 *     无 inode/vnode/buf 泄漏 (Invariant exfat-lookup-leak-free)。
 *   - read-only: 不写盘 (Invariant exfat-lookup-readonly)。 */
int VfsExfatLookup(struct Vnode *parent, const char *name, int len,
                   struct Vnode **vpp);
```

[SPECIFICATION]

**Pre-Condition**:
- `parent != NULL` 且 `parent->type == VNODE_TYPE_DIR`；
- `parent->data` 指向已初始化的 `exfat_inode_info`，其 `start_clu` ∈
  [EXFAT_FIRST_CLUSTER, sbi->num_clusters)，`flags` ∈ {ALLOC_FAT_CHAIN, ALLOC_NO_FAT_CHAIN}；
- `parent->originMount` 已挂载，`mount->data` 指向已在 mount 阶段完成初始化的 sbi
  (含 `s_lock` 已 `LOS_MuxInit`、几何字段已写入、`vol_utbl` 已加载)；
- `name` 非 NULL，`len` 是 `name` 中除尾部 NUL 外的字节数；
- `vpp != NULL`；
- 调用方进入时未持有 `sbi->s_lock` (避免重入)、未持有任何 `LOS_SpinLock`。

**Post-Condition (Case 1: 命中并返回新分配的 vnode，返回 0)**:
- 在父目录 dentry 流中找到首个 in-use file primary (type==EXFAT_FILE，0x85)，
  其完整 set (primary + stream + name secondaries) 通过 SetChecksum 校验；
- 该 set 中 EXFAT_NAME (0xC1) 子项拼出的 UTF-16 名字与 `(name, len)` 解出的
  目标名经 `exfat_uniname_cmp` 返回 0 (大小写不敏感等价)；
- 新分配的 `ei`：`ei->dir.dir = parent_ei->start_clu`,
  `ei->dir.size = parent_ei->size_in_clusters`, `ei->dir.flags = parent_ei->flags`,
  `ei->entry = matched_entry_idx`, `ei->type = TYPE_DIR | TYPE_FILE` (按 ATTR_SUBDIR),
  `ei->attr = LE16_TO_HOST(set[0].dentry.file.attr)`,
  `ei->start_clu = LE32_TO_HOST(set[1].dentry.stream.start_clu)`,
  `ei->flags = set[1].dentry.stream.flags`,
  `ei->size = LE64_TO_HOST(set[1].dentry.stream.size)`,
  `ei->valid_size = LE64_TO_HOST(set[1].dentry.stream.valid_size)`,
  `ei->i_pos = ((uint64_t)ei->start_clu << 32) | (uint32_t)ei->entry`,
  `ei->num_subdirs = 0` (v1 不预扫子目录数)；
- 新分配的 vnode：`vp->type` = (attr & ATTR_SUBDIR) ? `VNODE_TYPE_DIR` : `VNODE_TYPE_REG`,
  `vp->vop = &g_exfatVops`, `vp->fop = &g_exfatFops`, `vp->data = ei`,
  `vp->parent = parent`, `vp->originMount = parent->originMount`,
  `vp->uid = sbi->options.fs_uid`, `vp->gid = sbi->options.fs_gid`，
  且 `VfsHashInsert(vp, (uint32_t)ei->i_pos)` 已成功调用；
- `*vpp = vp`；
- 返回 0；
- `sbi->s_lock` 已释放。

**Post-Condition (Case 2: 未找到，返回 -ENOENT)**:
- 已遍历父目录全部 dentry (扫至 EXFAT_UNUSED 终止符 或 父目录簇链耗尽)，
  无任何在用 file primary 与目标名匹配；
- `*vpp` 保持调用前原值不变；
- 无新分配遗留 (任何中途分配的 ei/vp 已释放)；
- 返回 -ENOENT；
- `sbi->s_lock` 已释放。

**Post-Condition (Case 3: 输入非法 / 名字越界，返回 -EINVAL 或 -ENAMETOOLONG)**:
- 触发条件：`parent==NULL`、`name==NULL`、`vpp==NULL`、`parent->type != VNODE_TYPE_DIR`、
  `parent->data == NULL`、`parent->originMount == NULL`、`mount->data == NULL`、
  `len <= 0`、`exfat_utf8_to_uni` 返回 < 0 (非法 UTF-8) → -EINVAL；
- `len > EXFAT_MAX_NAME_LEN * 4` (UTF-8 最长字节估计) 或 `exfat_utf8_to_uni`
  解码后 `uni_len > EXFAT_MAX_NAME_LEN` → -ENAMETOOLONG；
- `*vpp` 不变；无分配遗留；
- 参数级检查 (NULL / type 错) 在取 `s_lock` 之前完成，不持锁返回；
  `exfat_utf8_to_uni` 失败发生在已持 `s_lock` 后，按 goto-stack 释放后返回。

**Post-Condition (Case 4: IO 或校验失败，返回 -EIO)**:
- 触发条件：`exfat_get_dentry_set` 返回 -EIO (扇区读失败 / 簇链断裂)；
  或 set 通过签名扫描但 `exfat_validate_dentry_set` 返回 -EIO (chksum 不符)；
- v1 行为：跳过该 entry_idx (向后推进)，不直接终止 lookup；仅当全程无任何
  candidate 通过校验且至少一次出现 IO 错误时，最终返回 -EIO；
- `*vpp` 不变；无分配遗留；`sbi->s_lock` 已释放。

**Post-Condition (Case 5: 内存分配失败，返回 -ENOMEM)**:
- 触发条件：`zalloc/LOS_MemAlloc` 用于临时 dentry-set 缓冲、UTF-16 缓冲、
  `exfat_inode_alloc`、或 `VnodeAlloc` 任一失败；
- 已成功分配的中间产物按 goto-stack 反向 LIFO 释放；
- `*vpp` 不变；`sbi->s_lock` 已释放；返回 -ENOMEM。

**Invariant** (id=exfat-lookup-readonly):
    本 stage 仅读盘——禁止调用 `los_part_write`/`los_disk_write`，
    禁止修改 sbi 任意字段，禁止修改 parent / parent_ei 任意字段。

**Invariant** (id=exfat-lookup-linux-s-lock-faithful):
    `VfsExfatLookup` 全程持 `sbi->s_lock`，与 Linux
    `fs/exfat/namei.c::exfat_lookup` 的锁模型 1:1 对应：入口
    `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)`，等价于 Linux
    `mutex_lock(&EXFAT_SB(sb)->s_lock)`；返回前在所有 `goto ERROR_*` 标签
    后单点 `LOS_MuxUnlock(&sbi->s_lock)`；中途任何分支不重复取或释放该锁。
    锁序：`sbi->s_lock` 永远在最外层；禁止在持 `s_lock` 之内反向获取
    `parent_ei->inode_lock`、`sbi->bitmap_lock` 或 `sbi->inode_hash_lock`。

**Invariant** (id=exfat-lookup-no-per-inode-lock):
    `VfsExfatLookup` **不**获取 `parent_ei->inode_lock`，与 Linux exfat
    namei 代码一致——Linux 端 `exfat_lookup` 函数体内部不直接取
    per-inode 锁；parent inode 的 `i_rwsem` 由 Linux generic namei 在 VFS
    上层持有，与 exfat 代码无关。LiteOS-A v1 单线程 namei 路径下，
    `s_lock` 已对所有并发 namei 操作 (lookup/create/unlink/...) 提供 FS
    全局串行化，无需再叠加 inode_lock。

**Invariant** (id=exfat-lookup-no-bitmap-lock):
    `VfsExfatLookup` 不获取 `sbi->bitmap_lock`，与 Linux 一致——lookup
    路径只读 dentry 流和 FAT 链，不操作 allocation bitmap 字节。

**Invariant** (id=exfat-lookup-utf8-only):
    v1 仅接受 UTF-8 输入名字；非法 UTF-8 (overlong / 续字节缺失 / 代理半码点 /
    超过 U+10FFFF) 返回 -EINVAL；不做 GBK / UCS-2 兼容降级。

**Invariant** (id=exfat-lookup-name-len-bound):
    `(name, len)` 解出的 UTF-16 长度必须 ∈ [1, EXFAT_MAX_NAME_LEN=255]。
    `len <= 0` → -EINVAL；`uni_len > 255` → -ENAMETOOLONG。

**Invariant** (id=exfat-lookup-stop-on-unused):
    扫描父目录 dentry 流时，若 `exfat_get_dentry_set` 返回 num_entries==0
    (起始字节为 EXFAT_UNUSED 0x00) 则视为目录尾，停止扫描，按 Case 2 返回。

**Invariant** (id=exfat-lookup-skip-non-file-primary):
    只有 in-use EXFAT_FILE (0x85) primary 才参与匹配；
    EXFAT_BITMAP (0x81) / EXFAT_UPCASE (0x82) / EXFAT_VOLUME (0x83) /
    deleted (0x05) 一律跳过 (推进到下一个 entry_idx)。

**Invariant** (id=exfat-lookup-vnode-type-from-attr):
    新 vnode 的 `vp->type` 由 `set[0].dentry.file.attr & ATTR_SUBDIR` 决定：
    位 set → VNODE_TYPE_DIR；位 clear → VNODE_TYPE_REG。其余 dentry 类型字节
    在 v1 不映射 (例如 VNODE_TYPE_LNK 不出现，因 exFAT 无符号链接)。

**Invariant** (id=exfat-lookup-i-pos-hash-key):
    `ei->i_pos = ((uint64_t)ei->start_clu << 32) | (uint32_t)ei->entry`；
    传给 `VfsHashInsert` 的 `hash` = `(uint32_t)ei->i_pos`，与 fatfs_hash 同一
    取低 32 位的语义。零簇文件 (start_clu==EXFAT_FREE_CLUSTER==0 且 size==0)
    用 `entry_idx` 单独成键避免哈希碰撞。

**Invariant** (id=exfat-lookup-leak-free):
    任何中途失败：已 zalloc 的 UTF-16 buf、已 LOS_MemAlloc 的 dentry-set buf、
    已通过 `exfat_inode_alloc` 申请的 ei、已通过 `VnodeAlloc` 申请的 vp，必须
    按反向 LIFO 释放 (`exfat_inode_free` / `VnodeFree`-via-Reclaim / `LOS_MemFree`)。
    通过 `goto ERROR_<step>:` 标签栈实现 (与 fatfs_lookup / VfsExfatMount 风格一致)。
    `s_lock` 释放紧贴 `return -ret` 之前的 `ERROR_EXIT:` 标签。

**Invariant** (id=exfat-lookup-no-mutate-on-failure):
    任何 `return < 0` 的路径，`*vpp` 不被写入 (保持调用方原值)；
    parent / sbi / parent_ei 任意字段不被修改。

**Invariant** (id=exfat-lookup-le-host-only):
    所有 packed dentry 字段 (file.attr, stream.start_clu, stream.size,
    stream.valid_size, stream.flags 等) 必须经 LE16/32/64_TO_HOST 取值,
    不得直接 cast。LiteOS-A ARMv7-A 当前是 LE host，宏退化为 identity；
    BE port 时只改这一处。

**Invariant** (id=exfat-lookup-no-spinlock-callsite):
    本函数内部调用 `LOS_MemAlloc`、`zalloc`、`LOS_MuxLock(s_lock, ...)`、
    `los_part_read` (经 helper) ——任何路径都可能睡眠。
    调用方禁止在持有 `LOS_SpinLock` 时调用 `VfsExfatLookup`。

**Invariant** (id=exfat-lookup-bounded):
    扫描 entry_idx 的循环上界：`parent_ei->size / DENTRY_SIZE` (即父目录已分配
    字节数对应的 dentry 数)，且不超过 `MAX_EXFAT_DENTRIES = 8388608`。
    超出上界视作目录损坏 → -EIO。

## Refine Prompt

加锁与并发约定 (与功能规范分离的第二轮关注点；以 Linux
`fs/exfat/namei.c::exfat_lookup` 为事实标准)：

1. **持锁范围 = `sbi->s_lock` 全程**：参数级检查 (NULL / type / len <= 0)
   完成后第一时间 `LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)`；返回前
   在所有 `goto ERROR_*` 标签后单点 `LOS_MuxUnlock(&sbi->s_lock)`。
   这与 Linux line 708 的 `mutex_lock(&EXFAT_SB(sb)->s_lock)` 与 line 758/763/769
   的对应 unlock 严格 1:1。

2. **不取 `parent_ei->inode_lock`**：Linux `exfat_lookup` 函数体不取，本移植
   对齐。理由：`s_lock` 是 FS 全局 mutex，已串行化所有 namei 操作
   (lookup / create / unlink / mkdir / rmdir / rename，全部 grep 自 Linux
   源——line 554/583, 708/769, 785/833, 847/878, 938/999, 1347/1403)，
   再叠加 inode_lock 是冗余且引入潜在 AB/BA 锁序风险。
   LiteOS-A VFS 层不像 Linux generic namei 那样自动持 parent inode 的
   `i_rwsem`，但本移植 v1 单线程读路径 + Wave A 不含 mutator，无并发
   写入竞争 parent_ei 的可能。

3. **不取 `sbi->bitmap_lock`**：Linux `exfat_lookup` 不取——lookup 不分配/
   释放簇，不读写位图字节内容。

4. **不取 `sbi->inode_hash_lock`**：Linux 用 `inode->i_hash` 经 generic
   `iget5_locked` 处理；本移植用 `VfsHashInsert`，其内部锁机制由
   `fs/vfs/vnode_hash.c` 自管理，调用方不需自取。

5. **新分配 ei 的 `inode_lock`**：成功路径分配的 `ei` 经 `exfat_inode_alloc`
   已 `LOS_MuxInit + SetProtocol(LOS_MUX_PRIO_INHERIT)` 初始化，但本函数
   **不持有** 它 (新 vnode 此时尚未对外暴露，并发上下文不可能拿到引用)；
   首次 Open / Read 时由 fop 路径自行取该锁——这是后续 stage 的关注点。

6. **锁序约定**：`sbi->s_lock` 永远在最外层。若未来 v2 写路径需要在
   `s_lock` 内取 `bitmap_lock` (clu alloc) / `inode_lock` (写文件时同步
   ei 状态)，必须严格遵循 Linux 锁序：s_lock → bitmap_lock，s_lock →
   inode_lock；禁止反向。本 stage 不取后两者，无锁序风险。

7. **自旋锁 callsite 约束** (重申)：本函数所有路径可能睡眠 (LOS_MemAlloc /
   LOS_MuxLock LOS_WAIT_FOREVER / los_part_read 阻塞等待块层)。
   VFS 自身从不在持自旋锁时调用 VnodeOps.Lookup，但若未来引入异步 lookup
   shim，必须先释放任何已持的 `LOS_SpinLock` 才能进入。

8. **重入安全**：`exfat_uniname_cmp` 是 pure 函数 (Invariant
   `exfat-nls-utf16-pure`)，可在持 `s_lock` 时安全调用；
   `exfat_get_dentry_set` 经 fat_chain helper，本身无锁 (Invariant
   `exfat-fat-chain-no-locks`)，由调用方串行化——`s_lock` 已提供。
