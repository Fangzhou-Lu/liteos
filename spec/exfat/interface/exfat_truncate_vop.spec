[PROMPT]
Wave B Stage 3：把 Stage 2d (extend) + Stage 2e (shrink) 编排为 LiteOS-A
VFS 的 truncate Vnode 操作。Linux 上游用 `__exfat_truncate` 一函数处理双向；
本端拆出 extend/shrink 两个底层 helper 后，VOP 入口退化为薄薄的 dispatcher：
锁 inode_lock → 算长度差 → 调对应 helper → 解锁 → 转 errno。

公开 API：

`VfsExfatTruncate(vp, len)` —— off_t 入口；转调 64-bit 实现。
`VfsExfatTruncate64(vp, len)` —— 真实实现：

1. 参数校验：vp != NULL、vp->originMount != NULL、vp->data != NULL、
   vp->originMount->data != NULL、len >= 0。
2. 取 sbi = vp->originMount->data；ei = vp->data。
3. LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER)。
4. 比较 len 与 ei->size：
   - len == ei->size → 直接 unlock 返回 0（no-op）。
   - len > ei->size  → exfat_truncate_extend(sbi, ei, (uint64_t)len)。
   - len < ei->size  → exfat_truncate_shrink(sbi, ei, (uint64_t)len)。
5. 转换返回值（helper 已返回负 POSIX errno；直接传递）。
6. 解锁、返回。

LiteOS-A 与 Linux 的差异：
- Linux 在 truncate 内部更新盘上 dentry（exfat_update_dir_chksum_with_entry_set）；
  本 stage **不**写盘上 dentry——与 Wave A 读路径"不持久化任何写入"一致。
  v1 路径下 truncate 之后未 sync 即 umount，重新 mount 看到的还是旧 size。
  这是已知的 v1 限制；后续单独 Stage 用 sync helper 统一持久化。
- Linux 通过 `inode_dio_wait` 等 page-cache 串行化机制在 truncate 前等 IO 排空；
  LiteOS 无 page cache，依赖 inode_lock 与 helpers 内部的 vol_flags / bitmap_lock
  分层确保 truncate 与 read/write/lookup 互斥。
- Linux truncate 会清算 i_blocks / mtime / atime；本 stage 仅更新 size /
  i_size_ondisk / valid_size（valid_size 由 helper clamp）；timestamp 留给 caller
  在 sync 路径补齐。

[RELY]
```c
extern int  exfat_truncate_extend(exfat_sb_info *sbi, exfat_inode_info *ei,
                                  uint64_t new_size);
extern int  exfat_truncate_shrink(exfat_sb_info *sbi, exfat_inode_info *ei,
                                  uint64_t new_size);
extern UINT32 LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern UINT32 LOS_MuxUnlock(LosMux *mutex);
extern void PRINT_ERR(const char *fmt, ...);

#define LOS_WAIT_FOREVER  0xFFFFFFFFu

struct Vnode {
    struct Mount *originMount;
    void         *data;
    /* ... 省略 */
};
struct Mount {
    void *data;
    /* ... */
};
```

[GUARANTEE]
```c
/* 调用约定（VfsExfatTruncate / _Truncate64）:
 *   - 调用方：LiteOS-A VFS 在 sys_truncate / sys_ftruncate 路径上调本 VOP。
 *   - 调用方**不**持 ei->inode_lock；本 helper 自取自释 inode_lock。
 *   - 调用方**不**持 sbi->s_lock / bitmap_lock；委托的 helper 自管。
 *   - 入参约束：vp != NULL；vp->originMount != NULL；vp->data != NULL；
 *               vp->originMount->data != NULL；len >= 0。
 *   - 成功（返回 0）：
 *       ei->size == (uint64_t)len；
 *       valid_size 被 shrink helper clamp 或被 extend helper 保留；
 *       i_size_ondisk 与新 size 按 cluster_size 对齐（extend 路径）或
 *       与新 size 完全等价（shrink-to-zero）或 不变（shrink-within-last-clu）；
 *       FAT 链与 ei->start_clu 一致。
 *   - 失败（返回负 POSIX errno）：
 *       -EINVAL：vp/Mount/data NULL 或 len < 0；
 *       -EFBIG：extend 路径 len > sbi->s_maxbytes；
 *       -ENOSPC：extend 路径 alloc_cluster 报告容量不足；
 *       -EIO：extend / shrink helper 报告底层 IO 失败；
 *             ei 状态由对应 helper 的 Case 8/9 语义决定（leak 路径已记入 spec）。
 *   - 不修改 dentry on-disk；caller 在后续 sync 中持久化（v1 留作已知限制）。
 *   - VfsExfatTruncate 与 _Truncate64 在内部完全等价；前者是 off_t 桥接。
 */
int VfsExfatTruncate(struct Vnode *vp, off_t len);
int VfsExfatTruncate64(struct Vnode *vp, off64_t len);
```

[SPECIFICATION]
**Pre-Condition**:
- vp 由 VFS 传入，已通过 VfsHashGet / Lookup 取得（lifetime 保证）
- ei = vp->data 已被 mount 阶段 / Lookup 阶段填充并 LOS_MuxInit
- sbi = vp->originMount->data 在整个挂载期内有效
- 调用方处于可阻塞上下文（允许内部取互斥锁）

**Post-Condition (Case 1: input -EINVAL)**:
- vp == NULL 或 vp->originMount == NULL 或 vp->data == NULL 或
  vp->originMount->data == NULL 或 len < 0：
  - 返回 -EINVAL；ei / sbi 不变；不取锁不发 IO

**Post-Condition (Case 2: success — no-op)**:
- (uint64_t)len == ei->size：
  - 取锁 → 立刻判定 no-op → 解锁 → 返回 0
  - ei / sbi 完全不变

**Post-Condition (Case 3: success — extend)**:
- (uint64_t)len > ei->size：
  - 取锁 → exfat_truncate_extend 返回 0 → 解锁 → 返回 0
  - ei->size == len；i_size_ondisk 按 cluster_size 上对齐；
    valid_size 不变；start_clu / FAT 链已分配并链接

**Post-Condition (Case 4: success — shrink)**:
- (uint64_t)len < ei->size：
  - 取锁 → exfat_truncate_shrink 返回 0 → 解锁 → 返回 0
  - ei->size == len；i_size_ondisk 与 num_new_clu*cluster_size 等价；
    valid_size <= len（被 helper clamp）；新 size==0 时 start_clu = EOF

**Post-Condition (Case 5: extend helper 报错)**:
- 取锁 → exfat_truncate_extend 返回负 errno（-EFBIG/-ENOSPC/-EIO） → 解锁
  → 透传该 errno
- ei 状态由 extend helper Case 6-9 的语义决定

**Post-Condition (Case 6: shrink helper 报错)**:
- 取锁 → exfat_truncate_shrink 返回负 errno（-EIO） → 解锁 → 透传
- ei 状态由 shrink helper Case 5-9 的语义决定

**Invariant** (id=exfat-truncate-vop-inode-lock-bracketed):
  本 VOP 在所有非 -EINVAL 早退路径上都 LOS_MuxLock + LOS_MuxUnlock 严格成对；
  -EINVAL 早退（参数校验失败）路径**不**触锁。

**Invariant** (id=exfat-truncate-vop-no-self-recurse):
  本 VOP 是 inode_lock 持有者；底层 helper 不再加 inode_lock，但会自取
  s_lock / bitmap_lock。锁序：inode_lock < s_lock < bitmap_lock。

**Invariant** (id=exfat-truncate-vop-no-dentry-write):
  本 stage 不修改 ei->dir / ei->entry 指向的盘上 dentry；
  size 仅在内存层（ei->size 字段）变更。

**Invariant** (id=exfat-truncate-vop-len-non-negative):
  off_t 是有符号；负值视为参数错误返回 -EINVAL，**不**类型转换为大正值
  调用 extend——这会跨过 s_maxbytes 校验导致 -EFBIG 而不是 -EINVAL，
  把语义错误归类错。

**Invariant** (id=exfat-truncate-vop-errno-passthrough):
  helper 返回的负 errno 直接透传；本 VOP **不**做 errno 重映射
  （-EIO 不变 -EIO，-ENOSPC 不变 -ENOSPC）。

**Invariant** (id=exfat-truncate-vop-truncate-equiv-truncate64):
  VfsExfatTruncate(vp, len) == VfsExfatTruncate64(vp, (off64_t)len)，
  对所有合法 off_t 值成立；不存在仅 32-bit 入口可达的边界条件。

## Refine Prompt
Locking discipline:

- 入口：caller 不持任何 sbi / ei 锁。
- 本 VOP 取 ei->inode_lock（LOS_WAIT_FOREVER；与 VfsExfatRead/Write 一致）。
- 底层 helper（extend/shrink）已声明 "caller holds inode_lock"——本 VOP 满足。
- helper 内部自取 sbi->s_lock 与 sbi->bitmap_lock；锁序 inode_lock < s_lock <
  bitmap_lock 在 helper 边界都已释放，反复 truncate 不会自死锁。
- 解锁顺序：单一锁 inode_lock 在 VOP 出口处一次性释放；helper 失败也走相同
  goto-style 出口确保解锁。
- caller 不能持 vp->originMount 上的其它锁——LiteOS-A VFS 在调 VOP 前已释
  pathcache_lock 等。
