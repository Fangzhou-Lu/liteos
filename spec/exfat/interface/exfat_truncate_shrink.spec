[PROMPT]
Wave B Stage 2e：把 Linux `fs/exfat/file.c::__exfat_truncate` 的 **shrink 路径**
移植到 LiteOS-A，作为文件**截断式收缩** (truncate to a smaller size) 的核心 API。
与 Stage 2d (extend) 配对成完整 truncate VOP；Linux 上游用同一函数处理 shrink/
extend 双向，本端拆成两支以保证规范小且独立。

公开 API：

`exfat_truncate_shrink(sbi, ei, new_size)` —— 把 inode 的逻辑文件长度从
`ei->size` 收缩到 `new_size`：

1. 参数校验：sbi != NULL、ei != NULL、new_size < ei->size、
   ei->flags == ALLOC_FAT_CHAIN（v1 scope，与 alloc/extend 一致）。
2. 计算簇数：num_new_clu = ceil(new_size, cluster_size)（new_size==0 时为 0）；
   num_phys_clu = ceil(ei->i_size_ondisk, cluster_size)。
3. 标 vol_flags dirty。
4. 若 num_new_clu < num_phys_clu：
   a. 若 num_new_clu == 0（截断到空）：
      - discard_chain = {dir=ei->start_clu, size=num_phys_clu, flags=ALLOC_FAT_CHAIN}；
      - exfat_free_cluster(sbi, &discard_chain) 释放整条链；
      - ei->start_clu = EXFAT_EOF_CLUSTER。
   b. 否则：
      - 沿 ei->start_clu 走 FAT 找第 (num_new_clu - 1) 跳得到 new_tail_clu；
      - exfat_get_next_cluster(new_tail_clu, &first_discard_clu) 取被丢弃链头；
      - exfat_ent_set(new_tail_clu, EXFAT_EOF_CLUSTER) 截断 FAT 链；
      - discard_chain = {dir=first_discard_clu, size=num_phys_clu - num_new_clu,
                        flags=ALLOC_FAT_CHAIN}；
      - exfat_free_cluster(sbi, &discard_chain) 释放尾段。
5. 更新 ei->size = new_size；
   ei->i_size_ondisk = num_new_clu * cluster_size；
   ei->valid_size = min(ei->valid_size, new_size)。
6. 清 vol_flags dirty。

LiteOS-A 与 Linux 的差异：
- Linux 通过 page-cache truncate_setsize + invalidate 路径完成"逻辑收缩 + 缓存
  失效"；LiteOS 无 page cache，直接更新 ei + 走 FAT。
- Linux 的 __exfat_truncate 在调 free_cluster 之前先 ent_set(EOF) 切链；本 stage
  保留同样语义——FAT 链完整性在丢失簇位前先得到保证。
- shrink-to-zero 时 start_clu 复位 EOF；Linux 用 EXFAT_FREE_CLUSTER (0)，LiteOS
  端 stage 2d/2c 与 inode_alloc 已统一用 EXFAT_EOF_CLUSTER 表达"无簇"，本 stage
  沿用之以保持 inode-level invariant 一致。
- Linux 同时更新 dentry；本 stage **不**写盘上 dentry（同 2d，sync 路径责任）。

[RELY]
```c
extern int  exfat_set_volume_dirty(exfat_sb_info *sbi);
extern int  exfat_clear_volume_dirty(exfat_sb_info *sbi);
extern int  exfat_free_cluster(exfat_sb_info *sbi, const exfat_chain *p_chain);
extern int  exfat_ent_set(const exfat_sb_info *sbi, uint32_t loc,
                          uint32_t value);
extern int  exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                                   uint32_t *next_clu);
extern void PRINT_ERR(const char *fmt, ...);

#define EXFAT_FIRST_CLUSTER     2u
#define EXFAT_FREE_CLUSTER      0u
#define EXFAT_EOF_CLUSTER       0xFFFFFFFFu
#define ALLOC_FAT_CHAIN         0x01u
#define ALLOC_NO_FAT_CHAIN      0x03u

#define EXFAT_MAX_CHAIN_LEN     0x10000000u
```

[GUARANTEE]
```c
/* 调用约定（exfat_truncate_shrink）:
 *   - 调用方持 ei->inode_lock；本 helper 不再加 inode_lock。
 *   - 调用方**不**持 sbi->s_lock / bitmap_lock；本 helper 内部委托的
 *     vol_flags / free_cluster 自取自释这两把锁。
 *   - 入参约束：
 *       sbi != NULL；ei != NULL；
 *       new_size < ei->size；
 *       ei->flags == ALLOC_FAT_CHAIN（v1 仅支持 FAT chain 模式）。
 *   - 成功（返回 0）：
 *       ei->size == new_size；
 *       ei->i_size_ondisk == ceil(new_size / cluster_size) * cluster_size；
 *       ei->valid_size <= new_size（若原值更大则被夹到 new_size）；
 *       new_size == 0 时 ei->start_clu == EXFAT_EOF_CLUSTER；否则
 *       ei->start_clu 与 FAT 链一致（链尾簇号读取出 EOF_CLUSTER）；
 *       sbi->vol_flags 调用前后均不残留 VOLUME_DIRTY；
 *       sbi->used_clusters 反映已释放簇数。
 *   - 失败：返回负 errno；ei 与 sbi 状态尽量回滚到调用前：
 *       -EINVAL：参数校验失败；
 *       -EIO   ：vol_flags / get_next / ent_set 失败；ei 不变；
 *                free_cluster 中途失败属于元数据泄漏（已截链的部分簇位无法
 *                回滚），由 caller fsck 修复；ei->size 已先于 free 提交时
 *                按已成功的语义更新——见 Case 9。
 *   - 不修改 dentry（caller 在本 helper 后另行 sync dentry）。
 */
int exfat_truncate_shrink(exfat_sb_info *sbi, exfat_inode_info *ei,
                          uint64_t new_size);
```

[SPECIFICATION]
**Pre-Condition**:
- sbi != NULL 且各字段已被 mount 阶段填充
- ei != NULL 且 ei 字段满足 inode 阶段约束
- 调用方持 ei->inode_lock
- 调用方**不**持 sbi->s_lock 或 sbi->bitmap_lock
- new_size < ei->size（仅收缩；extend 由 Stage 2d 处理；相等由调用方过滤）
- ei->flags == ALLOC_FAT_CHAIN
- sbi->cluster_size > 0 且为 2 的幂
- sbi->cluster_size_bits 与 cluster_size 一致

**Post-Condition (Case 1: input -EINVAL)**:
- new_size >= ei->size 或 ei->flags != ALLOC_FAT_CHAIN 或 sbi/ei 为 NULL：
  - 返回 -EINVAL，ei 与 sbi 不变；不发起任何 IO

**Post-Condition (Case 2: success — no clusters to free)**:
- num_new_clu == num_phys_clu（new_size 落在已分配最后一簇内）：
  - vol_flags dirty 短促置位再清回
  - ei->size = new_size
  - ei->i_size_ondisk 不变
  - ei->valid_size = min(old_valid_size, new_size)
  - ei->start_clu / ei->flags 不变
  - 返回 0

**Post-Condition (Case 3: success — shrink to zero, free entire chain)**:
- new_size == 0 且 ei->start_clu 是合法簇：
  - vol_flags dirty 置位
  - exfat_free_cluster(sbi, &whole_chain) 返回 0
  - ei->start_clu = EXFAT_EOF_CLUSTER
  - ei->size = 0
  - ei->i_size_ondisk = 0
  - ei->valid_size = 0
  - vol_flags dirty 清回
  - 返回 0

**Post-Condition (Case 4: success — shrink across cluster boundary)**:
- 0 < num_new_clu < num_phys_clu：
  - vol_flags dirty 置位
  - 沿 ei->start_clu 走 FAT (num_new_clu - 1) 跳得到 new_tail_clu
  - exfat_get_next_cluster(new_tail_clu, &first_discard) 返回 0
  - exfat_ent_set(new_tail_clu, EXFAT_EOF_CLUSTER) 返回 0（截链）
  - exfat_free_cluster(sbi, &discard_chain {first_discard, num_phys_clu-num_new_clu})
    返回 0
  - ei->start_clu 不变
  - ei->size = new_size
  - ei->i_size_ondisk = num_new_clu * cluster_size
  - ei->valid_size = min(old_valid_size, new_size)
  - vol_flags dirty 清回
  - 返回 0

**Post-Condition (Case 5: -EIO during vol_flags set)**:
- exfat_set_volume_dirty 返回 -EIO：
  - 不发起后续操作；ei 不变
  - 返回 -EIO

**Post-Condition (Case 6: -EIO during walk to new tail)**:
- 沿 ei->start_clu 走链至 new_tail 时 exfat_get_next_cluster 失败：
  - 不发起 ent_set / free_cluster
  - vol_flags dirty 清回（best-effort）
  - ei 不变
  - 返回 -EIO

**Post-Condition (Case 7: -EIO during ent_set break)**:
- exfat_ent_set(new_tail, EOF) 失败：
  - 不发起 free_cluster
  - vol_flags dirty 清回（best-effort）
  - ei 不变
  - 返回 -EIO

**Post-Condition (Case 8: -EIO during free entire chain to-zero)**:
- new_size == 0 且 exfat_free_cluster 返回非零（部分簇未能回收）：
  - ei->size、ei->i_size_ondisk、ei->valid_size 已更新为 0
  - ei->start_clu = EXFAT_EOF_CLUSTER
  - vol_flags dirty 清回（best-effort）
  - sbi->used_clusters 反映了 free_cluster 已成功释放的部分
  - 返回 -EIO（提示有元数据泄漏）

**Post-Condition (Case 9: -EIO during free of discard tail)**:
- 0 < num_new_clu < num_phys_clu 且 ent_set 已成功但 free_cluster 中途失败：
  - FAT 链已截至 new_tail（链完整性保留）
  - ei->size = new_size、i_size_ondisk = num_new_clu*cluster_size、valid_size 夹紧
  - ei->start_clu 不变
  - sbi->used_clusters 反映 free_cluster 已成功释放的部分
  - vol_flags dirty 清回（best-effort）
  - 返回 -EIO（提示有元数据泄漏，需 fsck 修复孤儿位图位）

**Invariant** (id=exfat-truncate-shrink-monotone-size):
  成功路径上 ei->size == new_size < old_size；失败路径（Case 1/5/6/7）上
  ei->size 保持 old_size。size 单调不增（在 shrink 范围内）。

**Invariant** (id=exfat-truncate-shrink-valid-size-clamped):
  成功路径上 ei->valid_size <= new_size：原 valid_size <= new_size 时保持，
  否则被夹紧到 new_size（避免"已写过但收缩后逻辑越界"的脏读）。

**Invariant** (id=exfat-truncate-shrink-i-size-ondisk-aligned):
  成功路径上 ei->i_size_ondisk 是 cluster_size 的整数倍，且
  i_size_ondisk == num_new_clu * cluster_size >= new_size。

**Invariant** (id=exfat-truncate-shrink-fat-chain-only):
  v1 仅支持 ei->flags == ALLOC_FAT_CHAIN；NO_FAT_CHAIN 直接 -EINVAL。

**Invariant** (id=exfat-truncate-shrink-vol-flags-bracketed):
  本 helper 退出前尝试清 VOLUME_DIRTY（best-effort——set 失败不会发起 clear；
  其它路径 clear 失败仅打 PRINT_ERR 不改变最终错误码）。

**Invariant** (id=exfat-truncate-shrink-fat-then-bitmap):
  收缩路径必须先 ent_set(EOF) 切断 FAT 链，再调 free_cluster 释放位图；
  顺序反了会出现"链尾仍指向 free 簇"的悬挂引用——比"位图泄漏"更严重。

**Invariant** (id=exfat-truncate-shrink-no-dentry-write):
  本 stage 不写盘上 dentry；调用方负责后续 dentry 同步。

**Invariant** (id=exfat-truncate-shrink-bounded-walk):
  走 FAT 链找 new_tail 的循环硬上限 EXFAT_MAX_CHAIN_LEN；防御自环。

**Invariant** (id=exfat-truncate-shrink-start-clu-on-empty):
  new_size == 0 路径上 ei->start_clu 必须复位 EXFAT_EOF_CLUSTER；
  保留旧 start_clu 会与"chain 已 free"形成 use-after-free 视图。

## Refine Prompt
Locking discipline:

- 调用方进入时持 ei->inode_lock，**不**持 sbi->s_lock 与 sbi->bitmap_lock。
- 本 helper 自身**不**调用 LOS_MuxLock / LOS_MuxUnlock。
- 锁序硬约束：inode_lock < s_lock（vol_flags） < bitmap_lock（free_cluster）。
  本 helper 调用顺序：set_volume_dirty (取 s_lock 后释) →
  walk via get_next_cluster (无锁) → ent_set (无锁) →
  free_cluster (取 bitmap_lock 后释) → clear_volume_dirty (取 s_lock 后释)
  inode_lock 持有期间内层锁交替进出，**不嵌套**（s_lock 与 bitmap_lock 不
  同时持有）。
- 与 Stage 2d (extend) 的差异：2d 顺序是 set→alloc→ent_set→clear；2e 顺序是
  set→walk→ent_set→free→clear。两者在 helper 边界都已释放内层锁，故同进程
  反复 truncate 不会自死锁。
