[PROMPT]
Wave B Stage 2b：把 Linux `fs/exfat/fatent.c::__exfat_free_cluster` +
`fs/exfat/balloc.c::exfat_clear_bitmap` 移植到 LiteOS-A，作为 truncate-shrink
（Stage 2c）/ unlink / rename 删除路径的核心释放助手。

两个公开 API：

1. `exfat_clear_bitmap(sbi, clu)` —— 内部辅助：把分配位图中簇号 `clu` 对应位
   清零并把所在 bitmap 扇区同步写回盘面。Linux 端用 buffer_head + sync
   异步合并；LiteOS-A 同步阻塞 IO，每次清位即写回 1 扇区。

2. `exfat_free_cluster(sbi, p_chain)` —— 释放一条簇链：
   - p_chain->dir == FREE/EOF/<FIRST_CLUSTER 时返回 0（无簇可释放）；
   - p_chain->size == 0 直接返回 0；
   - dir 落在 [FIRST_CLUSTER, num_clusters) 之外报 -EIO；
   - flags == ALLOC_NO_FAT_CHAIN：连续 size 个簇 [dir, dir+size) 逐簇 clear_bitmap；
   - flags == ALLOC_FAT_CHAIN：以 dir 为起点用 exfat_get_next_cluster 沿 FAT 走，
     直到 EOF；过程中每簇 clear_bitmap，遇到链中错误就提前结束并 -EIO，但
     已清的位 / 已扣的 used_clusters 不回滚（与 Linux 的 dec_used_clus
     标签语义对齐）。
   - 完成后 sbi->used_clusters -= 实际释放数；
   - 与 Linux 不同：本路径**不**写 FAT 项（不调 exfat_ent_set 把链清成
     FREE_ENT）；那是 truncate-shrink 阶段才需要的。这里只做 bitmap 释放。
   - 全程持 sbi->bitmap_lock；helper 自身负责 lock/unlock，调用者无需提前持锁。

LiteOS-A bitmap 内存镜像存储在 sbi->vol_amap（uint8_t * 平铺缓冲区，大小
map_sectors * blocksize 字节）。一次清位修改一个字节里某个 bit；写回时只写
该字节所在 1 扇区（不写整张 bitmap）。

[RELY]
```c
extern int     LOS_MuxLock(LosMux *mutex, uint32_t timeout);
extern int     LOS_MuxUnlock(LosMux *mutex);
extern int     los_part_write(int32_t part_id, void *buf, uint64_t sector,
                              uint32_t count);
extern void    PRINT_ERR(const char *fmt, ...);
extern int     exfat_get_next_cluster(const exfat_sb_info *sbi,
                                      uint32_t cur_clu, uint32_t *next_clu);

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
/* 调用约定（exfat_clear_bitmap）:
 *   - 调用方持有 sbi->bitmap_lock（LosMux）。本辅助不再加锁。
 *   - clu 必须满足 EXFAT_FIRST_CLUSTER ≤ clu < sbi->num_clusters；越界返回 -EINVAL。
 *   - 修改 sbi->vol_amap 中目标 bit；同步写回 bitmap 所在 1 扇区。
 *   - 返回 0 = 成功；-EINVAL = 簇号越界；-EIO = los_part_write 失败
 *     （vol_amap in-memory 已清，盘面未持久；不回滚）。
 */
int exfat_clear_bitmap(exfat_sb_info *sbi, uint32_t clu);

/* 调用约定（exfat_free_cluster）:
 *   - 公共 API；自负责 LOS_MuxLock(&sbi->bitmap_lock, LOS_WAIT_FOREVER)
 *     与对应 LOS_MuxUnlock；调用方进入时**不**应持有 bitmap_lock。
 *   - p_chain 非 NULL；fields 含义与 common.header::exfat_chain 一致。
 *   - 释放成功（包括无簇可释放的早返回）：返回 0；
 *   - dir 越界（既非 FREE/EOF/<FIRST，也不在 [FIRST, num_clusters) 内）：
 *     返回 -EIO 且不进入释放循环。
 *   - 中途 IO 错误：已扣的 used_clusters 与已清的 bit 保持当前状态，
 *     函数返回 -EIO。
 *   - 不修改 p_chain；副作用仅 sbi->vol_amap、sbi->used_clusters、盘面 bitmap 扇区。
 */
int exfat_free_cluster(exfat_sb_info *sbi, const exfat_chain *p_chain);
```

[SPECIFICATION]
**Pre-Condition**:
- `sbi != NULL`
- `sbi->vol_amap != NULL` 且大小 ≥ `sbi->map_sectors * sbi->blocksize`
- `sbi->blocksize > 0` 且为 2 的幂
- `sbi->map_clu >= EXFAT_FIRST_CLUSTER`
- `sbi->num_clusters > EXFAT_RESERVED_CLUSTERS`
- exfat_clear_bitmap 调用前调用方持 `sbi->bitmap_lock`；
  exfat_free_cluster 调用前调用方**不**持 `sbi->bitmap_lock`
- p_chain（仅 free_cluster）：`p_chain != NULL`，flags ∈ {ALLOC_FAT_CHAIN, ALLOC_NO_FAT_CHAIN}

**Post-Condition (Case 1: clear_bitmap success)**:
- 令 `ent_idx = clu - EXFAT_RESERVED_CLUSTERS`
- 令 `byte_in_amap = ent_idx / 8`，`bit_in_byte = ent_idx % 8`
- 令 `sector_in_amap = byte_in_amap / sbi->blocksize`
- 操作完成后：
  - `sbi->vol_amap[byte_in_amap]` 的第 `bit_in_byte` 位 == 0
  - 分区扇区 `clu_to_bitmap_sector(sbi, sector_in_amap)` 已被同步写回 1 扇区
  - 函数返回 0

**Post-Condition (Case 2: clear_bitmap invalid clu)**:
- `clu < EXFAT_FIRST_CLUSTER` 或 `clu >= sbi->num_clusters`：
  返回 `-EINVAL`，sbi 与盘面不变

**Post-Condition (Case 3: clear_bitmap write-back fails)**:
- `vol_amap` 内存中相应位**已**清；los_part_write 返回非 0：
  - PRINT_ERR 打印失败信息
  - 函数返回 `-EIO`
  - 不回滚 in-memory 状态

**Post-Condition (Case 4: free_cluster invalid chain — early return)**:
- p_chain->dir 属于 {FREE, EOF, <FIRST_CLUSTER} 之一，或 p_chain->size == 0：
  - 返回 0
  - sbi->used_clusters / sbi->vol_amap 不变
  - bitmap_lock 被进入并退出（即使无操作，仍按一致协议加解锁）

**Post-Condition (Case 5: free_cluster dir out of valid range)**:
- p_chain->dir 在 [FIRST_CLUSTER, num_clusters) 之外（且非 Case 4 中的 FREE / EOF）：
  - PRINT_ERR("invalid start cluster %u")
  - 返回 `-EIO`
  - sbi 与盘面不变；bitmap_lock 进入并退出

**Post-Condition (Case 6: free_cluster ALLOC_NO_FAT_CHAIN success)**:
- 令 `last = p_chain->dir + p_chain->size - 1`
- 对 clu ∈ [p_chain->dir, last]：
  - exfat_clear_bitmap(sbi, clu) 返回 0
- 完成后：
  - `sbi->used_clusters -= p_chain->size`（饱和到 0）
  - 函数返回 0

**Post-Condition (Case 7: free_cluster ALLOC_FAT_CHAIN success)**:
- 从 p_chain->dir 开始用 exfat_get_next_cluster 走链：
  - 每访问一个 clu：调 exfat_clear_bitmap → 累加 num_freed → 取下一个
  - 直到 next == EXFAT_EOF_CLUSTER 退出循环
- 完成后：
  - `sbi->used_clusters -= num_freed`（饱和到 0）
  - 函数返回 0
- 防御上限：循环次数 ≤ EXFAT_MAX_CHAIN_LEN；超过则强制结束并返回 -EIO

**Post-Condition (Case 8: free_cluster mid-chain IO error)**:
- ALLOC_FAT_CHAIN 模式下 exfat_get_next_cluster 或 exfat_clear_bitmap 中途失败：
  - sbi->used_clusters 已扣减 `num_freed`（不回滚）
  - 已清的 bitmap 位保持清状态
  - 函数返回 `-EIO`
- bitmap_lock 在错误路径上正确释放

**Invariant** (id=exfat-free-cluster-bitmap-lock-acquired):
  exfat_free_cluster 入口处必须 LOS_MuxLock(&sbi->bitmap_lock)，出口处
  统一 LOS_MuxUnlock；任何 return 都经由 unlock_out 标签，禁止裸 return。

**Invariant** (id=exfat-free-cluster-no-fat-write):
  本 stage **不**调用 exfat_ent_set。FAT chain 上的 EOF / 链断点写入是
  truncate-shrink (Stage 2c) 的责任。

**Invariant** (id=exfat-free-cluster-no-realloc):
  不分配 / 释放任何堆内存。所有缓冲都借用 sbi->vol_amap。

**Invariant** (id=exfat-free-cluster-bounded-loop):
  ALLOC_FAT_CHAIN 路径循环次数硬上限 EXFAT_MAX_CHAIN_LEN；防御 FAT 自环
  与互指环（与 exfat_chain_walk 的 bounded 不变量同源）。

**Invariant** (id=exfat-free-cluster-used-saturate):
  sbi->used_clusters 减法做饱和：当 used_clusters < num_freed 时
  used_clusters = 0，避免回绕到 0xFFFFFFFE 引发后续 alloc 路径误判。

**Invariant** (id=exfat-clear-bitmap-bit-then-disk):
  clear_bitmap 必须先修改 in-memory `sbi->vol_amap` 字节、再调
  los_part_write；不可顺序颠倒。

**Invariant** (id=exfat-clear-bitmap-single-sector):
  clear_bitmap 写回操作严格 1 扇区；不批量合并多 bit 的多次清位。

**Invariant** (id=exfat-clear-bitmap-no-discard):
  v1 不发起 trim / discard 操作（Linux opts->discard 分支整段删除）；
  LiteOS-A 块层不支持 trim。

## Refine Prompt
Locking discipline:

- exfat_free_cluster 是公共 API，独占 sbi->bitmap_lock：
  入口 LOS_MuxLock(&sbi->bitmap_lock, LOS_WAIT_FOREVER) 成功后再做参数校验；
  所有错误 / 早返回路径统一 goto unlock_out 然后 LOS_MuxUnlock 再 return。
- exfat_clear_bitmap 是内部辅助，**默认假设调用方已持锁**——free_cluster
  循环中不重复加锁。
- 锁序硬约束：bitmap_lock 之外不允许在持 bitmap_lock 时再请求 s_lock 或
  inode_hash_lock。任何写路径在持 s_lock 时进入 free_cluster 才符合
  锁序——这是合法正向链。
- 锁深度：bitmap_lock 单次锁深 1，不重入。
