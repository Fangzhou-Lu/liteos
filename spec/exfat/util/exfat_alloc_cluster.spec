[PROMPT]
Wave B Stage 2c：把 Linux `fs/exfat/balloc.c::exfat_find_free_bitmap` +
`exfat_set_bitmap` + `fs/exfat/fatent.c::exfat_alloc_cluster` 移植到 LiteOS-A，
作为 Wave B 写路径中**簇分配**的入口（truncate-extend / write 越过当前
i_size_ondisk 时唤起）。

三个公开 API：

1. `exfat_set_bitmap(sbi, clu)` —— 内部辅助，与 Stage 2b 的 `exfat_clear_bitmap`
   对偶：把分配位图中簇号 `clu` 对应位置 1，并把所在 bitmap 扇区同步写回盘面。
   调用方持 sbi->bitmap_lock。

2. `exfat_find_free_bitmap(sbi, hint_clu, out_clu)` —— 在 sbi->vol_amap 中扫描
   首个空闲位（值 0），从 `hint_clu` 开始环绕扫到 num_clusters，再绕回
   FIRST_CLUSTER；找到返回 0 + `*out_clu = 找到的簇号`，全空满返回
   `-ENOSPC` 且 `*out_clu = EXFAT_EOF_CLUSTER`。纯内存计算，不发起 IO。
   调用方持 sbi->bitmap_lock。

3. `exfat_alloc_cluster(sbi, num_alloc, p_chain)` —— 公开 API：
   - 检查 num_alloc 与可用簇数；总数不足返回 `-ENOSPC`。
   - 入口 LOS_MuxLock(&sbi->bitmap_lock)。
   - 选首簇：`p_chain->dir == EXFAT_EOF_CLUSTER` 时用 sbi->clu_srch_ptr 作 hint
     调 find_free_bitmap；否则把 p_chain->dir 当 hint。
   - 进入循环：每次 find_free_bitmap → set_bitmap → ent_set(EOF) →
     如果不是首簇则 ent_set(prev_clu, new_clu) 把链续上；num_alloc--；
     hint = new_clu + 1 进入下一轮。
   - 任一步失败：对**已经分配**的部分用内部 inline rollback（直接清 bitmap，
     不重入 bitmap_lock）回滚；释放 bitmap_lock 后返回 -EIO 或 -ENOSPC。
   - 成功：sbi->clu_srch_ptr 更新为最后命中 hint，sbi->used_clusters += 实际分配数，
     p_chain->dir = 第一个新簇，p_chain->size += 实际分配数。
   - **v1 强制要求 p_chain->flags == ALLOC_FAT_CHAIN**——避免移植
     chain_cont_cluster；NO_FAT 翻转推迟到 Stage 2c-v2。

[RELY]
```c
extern int     LOS_MuxLock(LosMux *mutex, uint32_t timeout);
extern int     LOS_MuxUnlock(LosMux *mutex);
extern int     los_part_write(int32_t part_id, void *buf, uint64_t sector,
                              uint32_t count);
extern void    PRINT_ERR(const char *fmt, ...);
extern int     exfat_ent_set(const exfat_sb_info *sbi, uint32_t loc,
                             uint32_t value);

#define EXFAT_RESERVED_CLUSTERS 2
#define EXFAT_FIRST_CLUSTER     2u
#define EXFAT_FREE_CLUSTER      0u
#define EXFAT_EOF_CLUSTER       0xFFFFFFFFu
#define ALLOC_FAT_CHAIN         0x01u
#define ALLOC_NO_FAT_CHAIN      0x03u

#define EXFAT_MAX_CHAIN_LEN     0x10000000u
```

[GUARANTEE]
```c
/* 调用约定（exfat_set_bitmap）:
 *   - 调用方持 sbi->bitmap_lock。
 *   - clu ∈ [FIRST_CLUSTER, num_clusters)；越界返回 -EINVAL。
 *   - 修改 sbi->vol_amap 目标 bit；同步写回 1 扇区。
 *   - 返回 0 / -EINVAL / -EIO（与 clear_bitmap 对偶；in-memory bit 已置位
 *     但盘面失败时不回滚）。
 */
int exfat_set_bitmap(exfat_sb_info *sbi, uint32_t clu);

/* 调用约定（exfat_find_free_bitmap）:
 *   - 调用方持 sbi->bitmap_lock。
 *   - hint_clu ∈ [FIRST_CLUSTER, num_clusters)；越界视为 hint=FIRST_CLUSTER。
 *   - 纯内存扫描；不发起 IO。
 *   - 找到：返回 0，*out_clu = 命中簇号。
 *   - 全满：返回 -ENOSPC，*out_clu = EXFAT_EOF_CLUSTER。
 */
int exfat_find_free_bitmap(const exfat_sb_info *sbi, uint32_t hint_clu, uint32_t *out_clu);

/* 调用约定（exfat_alloc_cluster）:
 *   - 公开 API；自负责 LOS_MuxLock / LOS_MuxUnlock(&sbi->bitmap_lock)。
 *   - 调用方进入时**不**持 bitmap_lock。
 *   - p_chain != NULL；num_alloc > 0；
 *   - **v1 强制要求 p_chain->flags == ALLOC_FAT_CHAIN**；其他值返回 -EINVAL。
 *   - 成功：返回 0，p_chain->dir = 首簇号，p_chain->size += 实际分配数；
 *     sbi->used_clusters += 实际分配数；sbi->clu_srch_ptr 更新。
 *   - 失败：返回负 errno；已部分分配的簇通过 inline rollback 清 bitmap 回滚；
 *     p_chain->dir 在 -EIO/-ENOSPC 路径上设为 EXFAT_EOF_CLUSTER，size 回到调用前。
 */
int exfat_alloc_cluster(exfat_sb_info *sbi, uint32_t num_alloc, exfat_chain *p_chain);
```

[SPECIFICATION]
**Pre-Condition**:
- `sbi != NULL` 且各字段已被 mount 阶段填充
- `sbi->vol_amap != NULL`，`sbi->blocksize > 0`，`sbi->num_clusters > EXFAT_RESERVED_CLUSTERS`
- exfat_set_bitmap / exfat_find_free_bitmap 调用方持 `sbi->bitmap_lock`；
  exfat_alloc_cluster 调用方**不**持锁
- p_chain（仅 alloc_cluster）：`p_chain != NULL`，`p_chain->flags == ALLOC_FAT_CHAIN`
- num_alloc（仅 alloc_cluster）：`num_alloc >= 1`

**Post-Condition (Case 1: set_bitmap success)**:
- `clu` 对应 vol_amap 位被置 1；分区扇区同步写回 1 扇区；返回 0

**Post-Condition (Case 2: set_bitmap invalid clu)**:
- `clu < FIRST_CLUSTER` 或 `clu >= num_clusters`：返回 `-EINVAL`，sbi 不变

**Post-Condition (Case 3: set_bitmap write-back fails)**:
- in-memory bit **已**置位；los_part_write 失败：返回 `-EIO`；不回滚

**Post-Condition (Case 4: find_free_bitmap hits)**:
- 从 hint_clu 起环绕扫描，遇到首个未置位的簇 c ∈ [FIRST_CLUSTER, num_clusters)：
  - `*out_clu = c`，函数返回 0

**Post-Condition (Case 5: find_free_bitmap full)**:
- 整个 vol_amap 中无未置位簇：`*out_clu = EXFAT_EOF_CLUSTER`，返回 `-ENOSPC`

**Post-Condition (Case 6: alloc_cluster -EINVAL on bad flags)**:
- p_chain->flags != ALLOC_FAT_CHAIN：返回 `-EINVAL`；不进入 bitmap_lock；sbi 不变

**Post-Condition (Case 7: alloc_cluster -ENOSPC on insufficient capacity)**:
- 当 sbi->used_clusters != EXFAT_CLUSTERS_UNTRACKED 且
  `num_alloc > (sbi->num_clusters - EXFAT_RESERVED_CLUSTERS) - sbi->used_clusters`：
  返回 `-ENOSPC`；不进入 bitmap_lock；p_chain 与 sbi 不变

**Post-Condition (Case 8: alloc_cluster success — single cluster)**:
- num_alloc == 1，find_free_bitmap 命中 c：
  - vol_amap 中 c 位已置 1；FAT[c] = EXFAT_EOF_CLUSTER（同步写回）
  - p_chain->dir = c；p_chain->size += 1；
  - sbi->used_clusters += 1；sbi->clu_srch_ptr = c；
  - 函数返回 0

**Post-Condition (Case 9: alloc_cluster success — multi cluster N)**:
- num_alloc == N（≥ 2），find_free_bitmap 命中 c1, c2, ..., cN：
  - vol_amap 中每个 ci 位已置 1
  - FAT[c1] = c2，FAT[c2] = c3，...，FAT[cN] = EXFAT_EOF_CLUSTER（同步写回）
  - p_chain->dir = c1（如调用前为 EXFAT_EOF_CLUSTER）；p_chain->size += N；
  - sbi->used_clusters += N；sbi->clu_srch_ptr = cN
  - 返回 0

**Post-Condition (Case 10: alloc_cluster mid-loop -EIO with rollback)**:
- 若分配第 k 个簇时 set_bitmap 或 ent_set 失败（k < N）：
  - 已分配的 k-1 个簇通过 inline rollback（直接清 bitmap 位）回滚；
    used_clusters 抵消（饱和减法）；
  - p_chain->dir = EXFAT_EOF_CLUSTER；p_chain->size 回到调用前
  - 返回 `-EIO`

**Post-Condition (Case 11: alloc_cluster mid-loop -ENOSPC with rollback)**:
- 若 find_free_bitmap 在第 k 次（k < N）找不到下一个空闲簇：
  - 同 Case 10 回滚；返回 `-ENOSPC`

**Invariant** (id=exfat-alloc-cluster-bitmap-lock-acquired):
  exfat_alloc_cluster 入口处必须 LOS_MuxLock(&sbi->bitmap_lock)，所有出口路径
  统一 LOS_MuxUnlock；任何 return 都经由 unlock 标签。

**Invariant** (id=exfat-alloc-cluster-fat-chain-only):
  v1 仅支持 p_chain->flags == ALLOC_FAT_CHAIN；ALLOC_NO_FAT_CHAIN 路径 EINVAL
  reject。Stage 2c-v2 再补 chain_cont_cluster 转换。

**Invariant** (id=exfat-alloc-cluster-rollback-on-error):
  任何中途失败必走 inline rollback 清 bitmap；不留半成品分配。回滚不调用
  exfat_free_cluster 公共 API（避免在持锁状态下 self-recurse 取 bitmap_lock）。

**Invariant** (id=exfat-alloc-cluster-no-realloc):
  本路径不调用 LOS_MemAlloc / zalloc / LOS_MemFree。所有缓冲（位图 / FAT）
  借用 sbi 已有结构。

**Invariant** (id=exfat-alloc-cluster-fat-eof-on-tail):
  分配链的最后一簇 cN 的 FAT entry == EXFAT_EOF_CLUSTER。中间簇 ci → ci+1。

**Invariant** (id=exfat-alloc-cluster-bitmap-then-fat):
  每个 ci 的写回顺序固定：先 set_bitmap（位图）再 ent_set（FAT）。这样如果中途
  crash，已 set 的位会被未来 free_cluster 回收为 free 状态；FAT 残留为孤儿不
  影响其他链。反之顺序不行：crash 后位图未标但 FAT 写了，链头会被后续 alloc
  当 free 重用，造成数据交叉。

**Invariant** (id=exfat-find-free-bitmap-no-io):
  find_free_bitmap 是纯内存扫描；不调 los_part_read / los_part_write。

**Invariant** (id=exfat-find-free-bitmap-bounded):
  扫描循环硬上限 EXFAT_MAX_CHAIN_LEN（防御 num_clusters 异常大）。

**Invariant** (id=exfat-set-bitmap-bit-then-disk):
  set_bitmap 先改 in-memory `sbi->vol_amap` 字节，再调 los_part_write；不可
  顺序颠倒（与 clear_bitmap 同源）。

**Invariant** (id=exfat-set-bitmap-single-sector):
  set_bitmap 写回严格 1 扇区；不批量合并多次 set 调用。

**Invariant** (id=exfat-alloc-cluster-srch-ptr-monotone):
  成功路径上 sbi->clu_srch_ptr 总是更新到最后命中簇号；失败回滚路径上保持
  调用前的旧值。

## Refine Prompt
Locking discipline:

- exfat_alloc_cluster 是公开 API，独占 sbi->bitmap_lock：入口 LOS_MuxLock 成功
  后再做参数校验；所有错误路径 goto 一个统一 unlock_out 标签。
- exfat_set_bitmap / exfat_find_free_bitmap 是内部辅助，假设调用方持锁；
  本身不调用 LOS_MuxLock / LOS_MuxUnlock。
- exfat_alloc_cluster 在回滚时**不**调用 exfat_free_cluster 公共 API（后者会
  自己取 bitmap_lock，导致 self-recurse 死锁）。改为内部 inline rollback：
  对已分配的簇直接 in-memory 清位 + los_part_write。
- 锁序硬约束：alloc_cluster → bitmap_lock → 不向上请求 s_lock / inode_hash_lock。
- 锁深度：bitmap_lock 单次锁深 1，不重入。
