[PROMPT]
Wave B Stage 2d：把 Linux `fs/exfat/file.c::exfat_cont_expand`（联合
`fs/exfat/inode.c::exfat_map_new_buffer` 的 lazy-alloc 路径）移植到 LiteOS-A，
作为文件**截断式扩展** (truncate to a larger size) 的核心 API。Linux 上游
通过 generic_cont_expand_simple + page-cache lazy-zeroing 完成；LiteOS 没有
page cache，所以本 stage 只做"分配 + 链接 + 元数据更新"，**不**物理零填充
新簇——读路径已经通过 `valid_size` 约束实现了 [valid_size, size) 区段返回零
（与 exFAT on-disk 语义一致：valid_size 标记真实数据边界，扩展后 size 增长但
valid_size 不变）。

公开 API：

`exfat_truncate_extend(sbi, ei, new_size)` —— 把 inode 的逻辑文件长度从
`ei->size` 扩展到 `new_size`：

1. 参数校验：sbi != NULL、ei != NULL、new_size > ei->size、
   new_size <= sbi->s_maxbytes、ei->flags == ALLOC_FAT_CHAIN（v1 scope，
   与 alloc_cluster 一致）。
2. 计算所需簇数：num_new_clu = ceil(new_size, cluster_size)，
   num_phys_clu = ceil(ei->i_size_ondisk, cluster_size)。
3. 标 vol_flags dirty。
4. 若 num_new_clu > num_phys_clu：
   a. 构造空 new_chain = {dir=EOF, size=0, flags=ALLOC_FAT_CHAIN}；
   b. 调 exfat_alloc_cluster(num_new_clu - num_phys_clu, &new_chain)；
   c. 若 ei->start_clu == EOF：ei->start_clu = new_chain.dir；
   d. 否则：沿 ei->start_clu 走 FAT 找到 last_existing_clu，
      exfat_ent_set(last_existing_clu, new_chain.dir) 链接旧尾到新头。
5. 更新 ei->size = new_size；ei->i_size_ondisk = num_new_clu * cluster_size；
   ei->valid_size 不变。
6. 清 vol_flags dirty。

LiteOS-A 与 Linux 的差异：
- Linux 用 generic_cont_expand_simple + page cache 完成"分配 + lazy 零填充"；
  LiteOS 无 page cache，把"分配"做成显式步骤，"零填充"由读路径基于 valid_size
  虚拟实现。
- Linux 在 __exfat_truncate 一并处理 shrink/extend；本 stage 只覆盖 extend，
  shrink 留给 Stage 2e。
- Linux 同时更新 directory entry；本 stage **不**写盘上 dentry——这是 sync 路径
  的责任。
- Linux 处理 IS_SYNC(inode)；本 stage 默认所有 IO 同步。

[RELY]
```c
extern int  exfat_set_volume_dirty(exfat_sb_info *sbi);
extern int  exfat_clear_volume_dirty(exfat_sb_info *sbi);
extern int  exfat_alloc_cluster(exfat_sb_info *sbi, uint32_t num_alloc,
                                exfat_chain *p_chain);
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
/* 调用约定（exfat_truncate_extend）:
 *   - 调用方持 ei->inode_lock；本 helper 不再加 inode_lock。
 *   - 调用方**不**持 sbi->s_lock / bitmap_lock；本 helper 内部委托的
 *     vol_flags / alloc_cluster / free_cluster 自取自释这两把锁。
 *   - 入参约束：
 *       sbi != NULL；ei != NULL；
 *       new_size > ei->size 且 new_size <= sbi->s_maxbytes；
 *       ei->flags == ALLOC_FAT_CHAIN（v1 仅支持 FAT chain 模式）。
 *   - 成功（返回 0）：
 *       ei->size == new_size；
 *       ei->i_size_ondisk >= new_size 且按 cluster_size 上对齐；
 *       ei->valid_size 不变；
 *       ei->start_clu 与 FAT 链一致（链尾簇号读取出 EOF_CLUSTER）；
 *       sbi->vol_flags 调用前后均不残留 VOLUME_DIRTY；
 *       sbi->used_clusters 反映已分配数。
 *   - 失败：返回负 errno；ei 与 sbi 状态尽量回滚到调用前：
 *       -EINVAL：参数校验失败；
 *       -EFBIG ：new_size > sbi->s_maxbytes；
 *       -ENOSPC：alloc_cluster 报告容量/簇不足；
 *       -EIO   ：vol_flags / alloc_cluster / ent_set / get_next 失败；
 *                已分配簇通过 exfat_free_cluster 回滚；vol_flags 尽力清回。
 *   - 不修改 dentry（caller 在本 helper 后另行 sync dentry）。
 */
int exfat_truncate_extend(exfat_sb_info *sbi, exfat_inode_info *ei,
                          uint64_t new_size);
```

[SPECIFICATION]
**Pre-Condition**:
- sbi != NULL 且各字段已被 mount 阶段填充
- ei != NULL 且 ei 字段满足 inode 阶段约束
- 调用方持 ei->inode_lock
- 调用方**不**持 sbi->s_lock 或 sbi->bitmap_lock
- new_size > ei->size（仅扩展；shrink 由 Stage 2e 处理）
- ei->flags == ALLOC_FAT_CHAIN
- sbi->cluster_size > 0 且为 2 的幂
- sbi->cluster_size_bits 与 cluster_size 一致

**Post-Condition (Case 1: input -EINVAL)**:
- new_size <= ei->size 或 ei->flags != ALLOC_FAT_CHAIN：
  - 返回 -EINVAL，ei 与 sbi 不变；不发起任何 IO

**Post-Condition (Case 2: input -EFBIG)**:
- new_size > sbi->s_maxbytes：
  - 返回 -EFBIG，ei 与 sbi 不变

**Post-Condition (Case 3: success — no new clusters needed)**:
- num_new_clu == num_phys_clu（new_size 落在已分配最后一簇内）：
  - vol_flags dirty 短促置位再清回
  - ei->size = new_size；i_size_ondisk 不变；valid_size 不变
  - ei->start_clu / ei->flags 不变
  - 返回 0

**Post-Condition (Case 4: success — clusters allocated, file was empty)**:
- 旧 ei->start_clu == EOF_CLUSTER 且 num_new_clu > 0：
  - vol_flags dirty 置位
  - alloc_cluster(sbi, num_new_clu, &new_chain) 返回 0
  - ei->start_clu = new_chain.dir
  - ei->size = new_size
  - ei->i_size_ondisk = num_new_clu * cluster_size
  - ei->valid_size 不变
  - vol_flags dirty 清回
  - 返回 0

**Post-Condition (Case 5: success — clusters allocated, link to existing tail)**:
- 旧 ei->start_clu 是合法簇且 num_new_clu > num_phys_clu：
  - vol_flags dirty 置位
  - 沿 ei->start_clu 走 FAT 找到链尾 last_existing_clu
  - alloc_cluster(num_new_clu - num_phys_clu, &new_chain) 返回 0
  - ent_set(last_existing_clu, new_chain.dir) 返回 0
  - ei->size = new_size
  - ei->i_size_ondisk = num_new_clu * cluster_size
  - ei->valid_size 不变
  - vol_flags dirty 清回
  - 返回 0

**Post-Condition (Case 6: -ENOSPC from alloc_cluster)**:
- alloc_cluster 返回 -ENOSPC：
  - vol_flags dirty 已清回（best-effort）
  - ei 不变
  - 返回 -ENOSPC

**Post-Condition (Case 7: -EIO during vol_flags set)**:
- exfat_set_volume_dirty 返回 -EIO：
  - 不发起 alloc_cluster；ei 不变
  - 返回 -EIO

**Post-Condition (Case 8: -EIO during alloc_cluster mid-loop)**:
- alloc_cluster 返回 -EIO（已内部 rollback）：
  - vol_flags dirty 清回（best-effort）
  - ei 不变
  - 返回 -EIO

**Post-Condition (Case 9: -EIO during chain link)**:
- alloc 成功但 ent_set(last_existing_clu, new_chain.dir) 失败：
  - exfat_free_cluster(sbi, &new_chain) 释放刚分配的链
  - vol_flags dirty 清回（best-effort）
  - ei 不变
  - 返回 -EIO

**Post-Condition (Case 10: -EIO during walk to tail)**:
- 沿 ei->start_clu 走链 exfat_get_next_cluster 失败：
  - 不发起 alloc_cluster
  - vol_flags dirty 清回（best-effort）
  - ei 不变
  - 返回 -EIO

**Invariant** (id=exfat-truncate-extend-monotone-size):
  成功路径上 ei->size == new_size > old_size；失败路径上 ei->size 保持
  old_size。size 单调不减。

**Invariant** (id=exfat-truncate-extend-valid-size-preserved):
  ei->valid_size 在本 stage 任何路径上都不变。

**Invariant** (id=exfat-truncate-extend-i-size-ondisk-aligned):
  成功路径上 ei->i_size_ondisk 是 cluster_size 的整数倍且 >= new_size。

**Invariant** (id=exfat-truncate-extend-fat-chain-only):
  v1 仅支持 ei->flags == ALLOC_FAT_CHAIN；NO_FAT_CHAIN 直接 -EINVAL。

**Invariant** (id=exfat-truncate-extend-vol-flags-bracketed):
  本 helper 退出前尝试清 VOLUME_DIRTY（best-effort——set 失败不会发起 clear；
  其它路径 clear 失败仅打 PRINT_ERR 不改变最终错误码）。

**Invariant** (id=exfat-truncate-extend-rollback-on-link-failure):
  Case 9 alloc 成功但 link 失败时必须 exfat_free_cluster 释放新链；
  不允许"链分配出来又没人引用"的孤儿状态。

**Invariant** (id=exfat-truncate-extend-no-dentry-write):
  本 stage 不写盘上 dentry；调用方负责后续 dentry 同步。

**Invariant** (id=exfat-truncate-extend-bounded-walk):
  走 FAT 链找尾巴的循环硬上限 EXFAT_MAX_CHAIN_LEN；防御自环。

**Invariant** (id=exfat-truncate-extend-no-physical-zerofill):
  本 stage **不**对新分配的簇做 los_part_write 零填充；通过 valid_size
  在读路径上等价实现。

## Refine Prompt
Locking discipline:

- 调用方进入时持 ei->inode_lock，**不**持 sbi->s_lock 与 sbi->bitmap_lock。
- 本 helper 自身**不**调用 LOS_MuxLock / LOS_MuxUnlock。
- 锁序硬约束：inode_lock < s_lock（vol_flags） < bitmap_lock（alloc/free）。
  本 helper 调用顺序：set_volume_dirty (取 s_lock 后释) →
  alloc_cluster (取 bitmap_lock 后释) → ent_set (无锁) →
  clear_volume_dirty (取 s_lock 后释)
  inode_lock 持有期间内层锁交替进出，**不嵌套**（s_lock 与 bitmap_lock 不
  同时持有）。
- Self-recurse 防御：本 helper 在 alloc_cluster 已释锁后才调 free_cluster；
  free_cluster 重新取 bitmap_lock 不会与已释放的 alloc 锁冲突。
