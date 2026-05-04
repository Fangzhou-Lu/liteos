[PROMPT]
Wave B Stage 2a 准备工：实现 FAT 表项**写**操作 `exfat_ent_set(sbi, loc, value)`。
读路径 (`exfat_get_next_cluster` / `exfat_chain_walk`) 已在 fat_chain stage
落地；本 stage 补对偶的写路径，是 truncate-shrink (Stage 2c) / truncate-extend
(Stage 2d) / 未来 alloc_cluster / 未来 fsync 路径的共同前置。

Linux 对照：`fs/exfat/fatent.c::exfat_ent_set`（fatent.c L61-82）：
1. 计算 FAT 表项扇区与扇区内偏移
2. `sb_bread` 读 FAT 扇区入 buffer cache
3. `*fat_entry = cpu_to_le32(content)` 改 LE32
4. `exfat_update_bh` 标记 buffer dirty（writeback 异步落盘）
5. **`exfat_mirror_bh(sb, sec, bh)` — 若 num_fats==2，把同样字节写到 FAT2 镜像**
6. `brelse` 释放

LiteOS-A 端差异（已在 fat_chain stage 建立的范式）：
- 无 buffer cache → 直接 `los_part_read` / `los_part_write` 同步 IO
- 无 buffer-head sync 状态 → 写完即落盘（同步语义；调用方决定是否取
  `sbi->s_lock` 串行多次写）
- num_fats 镜像写：检查 `sbi->num_fats == 2`，则额外写 FAT2 一份
  (`fat2_offset` 已在 mount 阶段装填)

输入校验（与 read 路径对称）：
- `loc` 必须在 `[EXFAT_FIRST_CLUSTER, sbi->num_clusters)` 之内（同
  `exfat_get_next_cluster` 的 range guard）
- `value` 必须满足以下之一（写入合法性，借鉴 Linux `exfat_ent_get` 的
  对称校验，但移到 SET 入口）：
  * `EXFAT_EOF_CLUSTER`（chain 终止）
  * `EXFAT_FREE_CLUSTER`（释放回 free pool — truncate-shrink 与 free_cluster
    需要写 0）
  * `[EXFAT_FIRST_CLUSTER, sbi->num_clusters)` 之内的合法簇号（chain link）
- `EXFAT_BAD_CLUSTER` 故意**不允许**作为 value：当前 Wave B 不引入坏块标记
  路径，避免误用；后续阶段需要时再 evolve。

锁约定：函数本身不取任何锁。调用方按以下规则持锁：
- truncate-shrink / fsync 路径：调用方持 `ei->inode_lock`（与 read/write 同序）
- alloc_cluster 路径：调用方持 `sbi->bitmap_lock`（与 vol_amap 修改同步）
- 写 FAT2 与写 FAT1 之间不释放锁（保证 FAT1/FAT2 一致性）

输出阶段必填：扩展 `fs/exfat/util/exfat_fat_chain.c`：
```c
int exfat_ent_set(const exfat_sb_info *sbi, uint32_t loc, uint32_t value);
```
common.header 追加 extern 行（v0.3.4.1 修过的 _sync_common_header 自动处理）。

Out of scope（明确不做）：
- bad block 标记写入（EXFAT_BAD_CLUSTER 作为 value 被拒绝）
- 跨扇区表项写（不会发生：FAT 表项 4 字节，扇区 ≥ 512，自然对齐到扇区内）
- batch / vector 写（一次只改一个表项；批量 chain link 由调用方循环组装，
  Linux `exfat_chain_cont_cluster` 也是这种循环模式）
- volume_dirty 翻转（独立 helper，在 Stage 2a 同伴 stage 落地）

[RELY]
```c
/* common.header 已冻结类型 */
typedef struct exfat_sb_info exfat_sb_info;

/* 簇 sentinel */
#define EXFAT_FIRST_CLUSTER 2u
#define EXFAT_FREE_CLUSTER  0u
#define EXFAT_EOF_CLUSTER   0xFFFFFFFFu
#define EXFAT_BAD_CLUSTER   0xFFFFFFF7u

/* sbi 字段（mount 后冻结）：
 *   uint32_t fat_offset;       FAT1 起始扇区
 *   uint32_t fat2_offset;      FAT2 起始扇区（num_fats==2 时；==fat_offset
 *                              when num_fats==1）
 *   uint8_t  num_fats;         1 或 2
 *   uint32_t blocksize;        扇区字节数
 *   uint32_t num_clusters;     总簇数（含保留簇）
 *   INT32    part_id;
 */

/* 内核原语 */
extern VOID  *LOS_MemAlloc(VOID *pool, UINT32 size);
extern UINT32 LOS_MemFree(VOID *pool, VOID *ptr);
extern VOID  *m_aucSysMem0;
extern errno_t memcpy_s(void *dest, size_t destMax, const void *src, size_t count);
extern int  los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);
extern int  los_part_write(INT32 pt, const VOID *buf, UINT64 sector, UINT32 count);

/* errno */
#define EINVAL  22
#define EIO     5
#define ENOMEM  12

/* 字节序：identity macro from existing exfat_fat_chain.c (LE host) */
#define LE32_TO_HOST(x) ((uint32_t)(x))
#define HOST_TO_LE32(x) ((uint32_t)(x))
```

[GUARANTEE]
```c
/* === Public exports (declared in fs/exfat/include/exfat.h) =========== */

/*
 * exfat_ent_set — write FAT[loc] = value, mirroring to FAT2 if configured.
 *
 * 调用约定：
 *   - 调用方持有合适锁（详见 Refine Prompt 的锁矩阵）：
 *       truncate-shrink / fsync 路径 → ei->inode_lock
 *       alloc_cluster 路径           → sbi->bitmap_lock
 *     函数自身**不取**任何锁——与现有 read 路径
 *     `exfat_get_next_cluster` 同范式。
 *   - 同步 IO：返回时数据已落盘（FAT1 + 可选 FAT2）。
 *
 * 返回值：
 *   - 0          ：写入成功（FAT1 + 可选 FAT2 均已落盘）。
 *   - -EINVAL    ：sbi == NULL；loc 越界（`< EXFAT_FIRST_CLUSTER` 或
 *                  `>= sbi->num_clusters`）；value 非法（`EXFAT_BAD_CLUSTER`，
 *                  或非 EOF/FREE 且不在合法簇范围）。
 *   - -EIO       ：FAT1 部分读失败；FAT1 写失败；FAT2 写失败（写 FAT1 已成功
 *                  时返回 -EIO 表示部分提交——上层调用应在持锁状态下重试，
 *                  或标 vol_flags VOLUME_DIRTY 等下次 mount fsck）。
 *   - -ENOMEM    ：fat_buf 分配失败。
 *
 * 副作用：
 *   - 成功：盘上 FAT1 的 `loc` 表项 = value（LE32）；若 num_fats==2 则 FAT2
 *           同步更新。sbi 字段不变。
 *   - 失败：FAT 状态可能部分修改（FAT1 写成功而 FAT2 写失败的窗口）；调用
 *           方应通过 vol_flags VOLUME_DIRTY 与 fsync 路径协调持久化一致性。
 *
 * Pre：sbi != NULL；sbi->blocksize > 0 且为 2^k；
 *      loc ∈ [EXFAT_FIRST_CLUSTER, sbi->num_clusters)；
 *      value ∈ {EOF, FREE} ∪ [EXFAT_FIRST_CLUSTER, sbi->num_clusters)。
 */
extern int exfat_ent_set(const exfat_sb_info *sbi, uint32_t loc, uint32_t value);
```

[SPECIFICATION]
**Pre-Condition**：
1. `sbi != NULL`；`sbi->blocksize > 0` 且为 2 的幂；
   `sbi->fat_offset` 与 `sbi->fat2_offset` 均已在 mount 阶段装填；
   `sbi->num_fats ∈ {1, 2}`；`sbi->num_clusters >= EXFAT_FIRST_CLUSTER`。
2. `loc ∈ [EXFAT_FIRST_CLUSTER, sbi->num_clusters)`。
3. `value ∈ {EXFAT_EOF_CLUSTER, EXFAT_FREE_CLUSTER}` ∪
   `[EXFAT_FIRST_CLUSTER, sbi->num_clusters)`。
4. 调用方按 Refine Prompt 锁矩阵持有相应锁。

**Post-Condition (Case 1: Success, num_fats == 1)**：
- 计算 `fat_byte_off = loc * 4u`，`fat_sector = sbi->fat_offset +
  fat_byte_off / sbi->blocksize`，`in_sector_off = fat_byte_off %
  sbi->blocksize`。
- 分配 `fat_buf` (sbi->blocksize 字节)。
- `los_part_read(part_id, fat_buf, fat_sector, 1, TRUE)` 成功。
- `*((uint32_t *)(fat_buf + in_sector_off)) = HOST_TO_LE32(value)`（用
  memcpy_s 写入以避免 unaligned 风险）。
- `los_part_write(part_id, fat_buf, fat_sector, 1)` 成功。
- 释放 fat_buf；返回 0。

**Post-Condition (Case 2: Success, num_fats == 2)**：
- 同 Case 1 的 FAT1 写入流程（读—改—写 fat_offset）。
- 计算 `fat2_sector = sbi->fat2_offset + fat_byte_off / sbi->blocksize`。
- `los_part_write(part_id, fat_buf, fat2_sector, 1)` 成功（**复用同一个
  fat_buf**——FAT1 与 FAT2 必须 byte-for-byte 一致，这正是 mirror 的语义）。
- 释放 fat_buf；返回 0。

**Post-Condition (Case 3: Invalid loc / value / sbi)**：
- 返回 `-EINVAL`；不分配 fat_buf；不调 los_part_read / los_part_write；
  盘上 FAT 不变。

**Post-Condition (Case 4: Allocation failure)**：
- `LOS_MemAlloc` 返回 NULL → 返回 `-ENOMEM`；不调 IO；盘上 FAT 不变。

**Post-Condition (Case 5: FAT1 read failure)**：
- `los_part_read` 返回非零 → 释放 fat_buf；返回 `-EIO`；盘上 FAT 不变。

**Post-Condition (Case 6: FAT1 write failure)**：
- FAT1 `los_part_write` 返回非零 → 释放 fat_buf；返回 `-EIO`；
  盘上 FAT 状态：read 把当前 FAT1 内容读到 buf，write 失败 → 盘上 FAT1
  保持原样（los_part_write 失败语义假定为"写未发生"——LiteOS-A virtio-blk
  的常见实现）。

**Post-Condition (Case 7: FAT2 write failure, num_fats == 2)**：
- FAT1 写入已成功；FAT2 `los_part_write` 返回非零 → 释放 fat_buf；
  返回 `-EIO`；盘上 FAT1 已是新值，FAT2 是旧值（**部分提交窗口**）。
  调用方有责任在调用 exfat_ent_set 的失败路径下，要么后续重试，要么置位
  VOLUME_DIRTY 让下次 mount-time fsck 修复（Wave B6 fsync 接管）。

**Invariant** (id=exfat-ent-set-no-locks)：
本函数不取 sbi->s_lock / bitmap_lock / inode_hash_lock / 任意 ei 锁。与
`exfat_get_next_cluster` 同范式。锁责任在调用方；锁矩阵在 Refine Prompt。

**Invariant** (id=exfat-ent-set-le-host-only)：
盘上 FAT 表项始终 LE32 存储；本路径仅做 host→LE 转换写入。ARMv7-A LE
host：HOST_TO_LE32 是 identity macro。BE host 移植需替换该 macro，调用点
不变。

**Invariant** (id=exfat-ent-set-buf-leak-free)：
fat_buf 一旦 alloc 成功，所有返回路径（含 -EIO）均经过 LOS_MemFree。

**Invariant** (id=exfat-ent-set-no-spinlock-callsite)：
函数内部含 LOS_MemAlloc + los_part_read + los_part_write，皆可能睡眠。
调用方不允许在持自旋锁时进入。

**Invariant** (id=exfat-ent-set-mirror-byte-exact)：
num_fats == 2 时，FAT1 写入的 buffer 内容**必须**逐字节复用作为 FAT2 写入
内容——禁止两次独立编码（防止 LE32 转换在 FAT1 与 FAT2 之间引入差异）。
这是 Linux exfat_mirror_bh 的本质约束。

**Invariant** (id=exfat-ent-set-rejects-bad-cluster)：
value == EXFAT_BAD_CLUSTER → 返回 -EINVAL，不进 IO。Wave B 暂不引入坏块
标记路径；future stage 若需要再 evolve 解除该约束。

**Invariant** (id=exfat-ent-set-validates-value-range)：
value 必须在 {EOF, FREE} ∪ [FIRST, num_clusters) 之内；其它值（含
EXFAT_BAD_CLUSTER、随机超大数字）一律拒绝。这把 Linux `exfat_ent_get` 的
读端校验对称到写端入口，避免用脏值污染 FAT。

**Invariant** (id=exfat-ent-set-sector-aligned-io)：
FAT 表项 4 字节，扇区 ≥ 512 字节，且 sbi->blocksize 是 2^k → 表项不会
跨扇区。每次调用恰好读 1 扇区、写 1 扇区（或 num_fats==2 时 2 扇区）。

**Invariant** (id=exfat-ent-set-no-vol-amap-touch)：
本路径仅触碰盘上 FAT 区域（fat_offset / fat2_offset 起始的扇区段），
不读写 sbi->vol_amap（bitmap），不读写 boot_buf。bitmap 修改是
exfat_set_bitmap / exfat_clear_bitmap 的责任。

**Invariant** (id=exfat-ent-set-bounded-per-call)：
每次调用恰好一次 LOS_MemAlloc + 一次 los_part_read + 1～2 次
los_part_write。无循环、无递归。O(1) IO。

**Invariant** (id=exfat-ent-set-no-mutate-on-failure)：
任意 -errno 路径不修改 sbi 任何字段；fat_buf 释放正常；用户传入指针
不变。FAT1 写已成功而 FAT2 写失败的部分提交是已知窗口（Case 7），
通过 invariant exfat-ent-set-mirror-byte-exact + 调用方 VOLUME_DIRTY
策略协调，**不属于本函数 invariant 违反**。

## Refine Prompt

锁矩阵（callee 不取，caller 持有）：

| 调用方路径                | 持锁                  | 释锁时机                    |
|---|---|---|
| truncate-shrink / fsync   | ei->inode_lock        | exfat_ent_set 返回后        |
| alloc_cluster             | sbi->bitmap_lock      | 同上                        |
| Wave B 后续 rename        | sbi->s_lock           | 同上                        |
| 直接 ent_set 测试 (cmocka)| 无（host LosMux nop） | N/A                         |

锁序约束：caller 已持的锁层级必须 ≥ inode_lock（或 bitmap_lock 平行层），
exfat_ent_set 不引入新锁需求。

并发注意：FAT1 + FAT2 之间不允许释放锁——多线程 truncate 同 inode 时，
若任一线程写完 FAT1 而尚未写 FAT2 时被另一线程抢占 ent_set 同 loc，
FAT 镜像一致性破坏。inode_lock 保证同 inode 的 truncate 串行；
bitmap_lock 保证 alloc 串行；二者覆盖全部场景。

spinlock 禁区：本路径含 LOS_MemAlloc + los_part_read/write，禁止在持
自旋锁时进入。
