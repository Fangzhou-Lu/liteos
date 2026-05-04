[PROMPT]
Wave B Stage 4a 准备工：实现 dentry-set **写**入操作 `exfat_set_dentry`
+ `exfat_set_dentry_set`，作为 dentry_iter (Stage 1.dentry_iter) 的写端
对偶。这是后续 Wave B Stage 4b/4c (dentry remove / slot alloc) 与
Stage 4d-g (Unlink/Create/Mkdir/Rmdir VOPs) 的共同最底层前置。

Linux 对照：fs/exfat/dir.c::exfat_update_dir_chksum / exfat_init_dir_entry
等使用 sb_bread + memcpy + exfat_update_bh + brelse 的 buffer-cache
范式。LiteOS-A 没有 buffer cache，做法是每次写入做一次完整的
read-modify-write (los_part_read → memcpy_s → los_part_write)。

调用语义（与 Linux 不同点）：
- 同步 IO：返回时数据已落盘（FAT 路径相同：见 exfat_ent_set 已批准 spec）
- 一次只改一个 dentry（exfat_set_dentry）；多 dentry 由
  exfat_set_dentry_set 循环组装，与 exfat_get_dentry_set 完全对称
- 跨簇正确：每次 set_dentry 都从 entry_idx 重新算 clu/sector，遵守
  Invariant exfat-dentry-iter-set-cluster-boundary 的同精神
  （写端版本 invariant 见下）
- 函数自身**不取**任何锁；调用方持 parent->inode_lock（运行期）或
  sbi->s_lock（rename 跨目录路径）；与 exfat_ent_set 同范式

Out of scope（明确不做）：
- chksum16 自动重算 + 回填：caller 责任。caller 在调 set_dentry_set 之前
  必须自己用 exfat_calc_chksum16(...,CS_DIR_ENTRY) 把 SetChecksum 字段
  填入 set[0].dentry.file.checksum；本层只负责"把 buffer 写到磁盘"。
  这跟 Linux 的 exfat_update_dir_chksum 分层一致——chksum 是 dentry
  组装阶段的事，不是 IO 阶段的事
- 部分提交回滚：set_dentry_set 中第 i 个 set_dentry 失败时不会撤销前
  i-1 个的写入。caller 在失败路径下应当置 vol_flags VOLUME_DIRTY
  让下次 mount fsck 修复——与 exfat_ent_set Case 7 同策略
- 批量 sector 优化：v1 求正确不求性能。**禁止**一次 los_part_read N
  连续扇区再批量 splice 写——会与跨簇的 Invariant 冲突
- 删除标记翻转 (type byte 0x85 → 0x05)：Stage 4b 独立 helper
  exfat_remove_dentry_set 的责任。本层只无脑写 caller 给的 32B 字节

[RELY]
```c
/* common.header 已声明类型；此处不重列 */
typedef struct exfat_sb_info exfat_sb_info;
typedef struct exfat_chain   exfat_chain;
struct exfat_dentry;             /* 32B 联合 */

/* dentry/dentry.header 已声明的常量 */
#define DENTRY_SIZE                32u
#define EXFAT_FIRST_CLUSTER        2u
#define EXFAT_EOF_CLUSTER          0xFFFFFFFFu
#define ALLOC_FAT_CHAIN            0x01u
#define ALLOC_NO_FAT_CHAIN         0x03u

/* errno */
#define EINVAL  22
#define EIO     5
#define ENOMEM  12

/* 内核原语 */
extern VOID  *LOS_MemAlloc(VOID *pool, UINT32 size);
extern UINT32 LOS_MemFree(VOID *pool, VOID *ptr);
extern VOID  *m_aucSysMem0;
extern errno_t memcpy_s(void *dest, size_t destMax, const void *src, size_t count);

/* 分区 IO（同 dentry_iter 用法） */
extern INT32 los_part_read(INT32 part_id, VOID *buf, UINT64 sector,
                            UINT32 count, BOOL useRead);
extern INT32 los_part_write(INT32 part_id, const VOID *buf, UINT64 sector,
                             UINT32 count);

/* 已批准 stage 公共 API */
extern int exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                                   uint32_t *next_clu);
static inline uint64_t exfat_clu_to_sector(const exfat_sb_info *sbi,
                                            uint32_t clu);
```

[GUARANTEE]
```c
/* === Public exports (declared in fs/exfat/include/exfat.h) =========== */

/*
 * exfat_set_dentry — write one 32B dentry to directory chain at linear
 *                    index `entry_idx`.
 *
 * 调用约定：
 *   - 调用方持有 parent->inode_lock（运行期）或 sbi->s_lock（rename
 *     跨目录路径）；本函数不取任何锁——与 exfat_ent_set / dentry_iter
 *     同范式。
 *   - 同步 IO：返回时该扇区已落盘。
 *   - 不参与 chksum 计算或回填——caller 已把整个 dentry-set 的
 *     SetChecksum 写入 set[0].dentry.file.checksum 并把 set[0] 作为
 *     `in` 传入（或使用 set 写入器循环）。
 *
 * 返回值：
 *   - 0       ：dentry 已成功写盘。
 *   - -EINVAL ：sbi/dir/in == NULL；entry_idx < 0；dir->flags 非法；
 *               sbi 字段无效；dir->dir < EXFAT_FIRST_CLUSTER。
 *   - -EIO    ：FAT 链 navigation 失败（中途 EOF 或 invalid cluster）；
 *               los_part_read 失败；los_part_write 失败；memcpy_s 失败。
 *   - -ENOMEM ：临时扇区缓冲分配失败。
 *
 * 副作用：
 *   - 成功：盘上目标扇区中 [byte_in_sector, byte_in_sector+DENTRY_SIZE)
 *           = *in 的 32B 副本；扇区内其它字节保持原值。
 *           sbi/dir/in 不变。
 *   - 失败：可能在 los_part_write 失败窗口内盘上扇区已部分修改
 *           （但常见 virtio-blk 实现写失败时盘上保持原样）。caller 在
 *           失败路径下不能假设盘上状态。
 *
 * Pre：sbi != NULL && dir != NULL && in != NULL；entry_idx >= 0；
 *      dir->flags ∈ {ALLOC_FAT_CHAIN, ALLOC_NO_FAT_CHAIN}；
 *      sbi->cluster_size > 0 && sbi->blocksize > 0；
 *      dir->dir >= EXFAT_FIRST_CLUSTER。
 */
extern int exfat_set_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                            int entry_idx, const struct exfat_dentry *in);

/*
 * exfat_set_dentry_set — write `num_entries` consecutive 32B dentries
 *                        starting at linear index `start_entry`.
 *
 * 调用约定：
 *   - 内部循环 num_entries 次调 exfat_set_dentry。
 *   - 与 exfat_get_dentry_set 形态完全对偶；区别在 set 是 const，方向
 *     是写。
 *   - 部分提交：第 i 个 set_dentry 失败时返 -errno；前 i-1 个写入已落盘
 *     且**不**回滚。caller 应在失败路径下置 vol_flags VOLUME_DIRTY
 *     让下次 mount fsck 修复（与 exfat_ent_set Case 7 同策略）。
 *
 * 返回值：
 *   - 0       ：所有 num_entries 个 dentry 写盘成功。
 *   - -EINVAL ：参数校验失败（任一指针 NULL；start_entry < 0；
 *               num_entries < 1；num_entries > EXFAT_DENTRY_SET_MAX）。
 *   - 其他    ：第 i 次 set_dentry 的返回值透传（i ∈ [0, num_entries)）。
 *               已写入的前 i-1 个 dentry 保留在盘上。
 *
 * 副作用：见 exfat_set_dentry。多次 IO 串行；不会并发触发。
 *
 * Pre：sbi != NULL && dir != NULL && set != NULL；start_entry >= 0；
 *      1 <= num_entries <= EXFAT_DENTRY_SET_MAX。
 */
extern int exfat_set_dentry_set(const exfat_sb_info *sbi,
                                const exfat_chain *dir,
                                int start_entry,
                                const struct exfat_dentry *set,
                                int num_entries);
```

[SPECIFICATION]

**Pre-Condition (exfat_set_dentry)**：
1. `sbi != NULL && dir != NULL && in != NULL`；`entry_idx >= 0`。
2. `sbi->cluster_size > 0`（mount 期解析 boot sector 后装填）；
   `sbi->blocksize > 0`（同上）；二者均为 2 的幂。
3. `dir->dir >= EXFAT_FIRST_CLUSTER`；
   `dir->flags ∈ {ALLOC_FAT_CHAIN, ALLOC_NO_FAT_CHAIN}`。
4. 调用方按 Refine Prompt 锁矩阵持有相应锁；不在持自旋锁状态。

**Post-Condition (Case 1: ALLOC_NO_FAT_CHAIN, success)**：
- 计算 `byte_off = (uint64_t)entry_idx * DENTRY_SIZE`；
  `clu_offset = byte_off / cluster_size`；
  `byte_in_clu = byte_off % cluster_size`；
  `sector_in_clu = byte_in_clu / blocksize`；
  `byte_in_sector = byte_in_clu % blocksize`。
- `cur_clu = dir->dir + clu_offset`；
  若 `cur_clu < EXFAT_FIRST_CLUSTER || cur_clu >= sbi->num_clusters`
  → 返回 -EIO；不分配 buf；不调 IO。
- `sector_lba = exfat_clu_to_sector(sbi, cur_clu) + sector_in_clu`。
- 分配 `buf = LOS_MemAlloc(m_aucSysMem0, blocksize)`。
- `los_part_read(part_id, buf, sector_lba, 1, TRUE)` 成功。
- `memcpy_s(buf + byte_in_sector, blocksize - byte_in_sector,
           in, DENTRY_SIZE) == EOK`。
- `los_part_write(part_id, buf, sector_lba, 1)` 成功。
- `LOS_MemFree(m_aucSysMem0, buf)`；返回 0。

**Post-Condition (Case 2: ALLOC_FAT_CHAIN, success)**：
- 同 Case 1 的 sector 计算逻辑，但 `cur_clu` 通过 `clu_offset` 次
  `exfat_get_next_cluster` 步进得到（与 exfat_get_dentry 同款 chain
  walk）。
- 步进过程中若遇 EOF（`next_clu == EXFAT_EOF_CLUSTER`）或
  `exfat_get_next_cluster` 返回错误 → 透传错误；不分配 buf；不调 IO。
- 后续 sector_lba 计算 / IO / memcpy / 释放同 Case 1。

**Post-Condition (Case 3: invalid args)**：
- 任一参数 NULL，或 entry_idx < 0，或 dir->flags 非法，或 sbi
  字段无效 → 返回 -EINVAL；不分配 buf；不调 IO；盘上不变。

**Post-Condition (Case 4: alloc failure)**：
- `LOS_MemAlloc` 返回 NULL → 返回 -ENOMEM；不调 IO；盘上不变。

**Post-Condition (Case 5: read failure)**：
- `los_part_read` 返回 < 0 → 释放 buf；返回 -EIO；盘上不变（write
  尚未触发）。

**Post-Condition (Case 6: memcpy_s failure)**：
- `memcpy_s` 返回非 EOK（理论上仅当参数越界，本层已自校验所以不应
  发生；防御性兜底）→ 释放 buf；返回 -EIO；盘上不变。

**Post-Condition (Case 7: write failure)**：
- `los_part_write` 返回 < 0 → 释放 buf；返回 -EIO；盘上扇区状态：
  虚拟块设备常见实现是"写未发生即盘上保持原值"，但 caller 不应
  依赖该假设——失败路径下必须置 vol_flags VOLUME_DIRTY。

**Pre-Condition (exfat_set_dentry_set)**：
1. `sbi != NULL && dir != NULL && set != NULL`；`start_entry >= 0`。
2. `1 <= num_entries <= EXFAT_DENTRY_SET_MAX`（=19，Microsoft 上限）。
3. 调用方按 Refine Prompt 锁矩阵持有相应锁。

**Post-Condition (Case 1: success)**：
- 循环 `i = 0..num_entries-1`，每次调
  `exfat_set_dentry(sbi, dir, start_entry + i, &set[i])`。
- 全部返回 0 → 返回 0。

**Post-Condition (Case 2: invalid args)**：
- 任一指针 NULL；start_entry < 0；num_entries 越界 → 返回 -EINVAL；
  不调任何 set_dentry。

**Post-Condition (Case 3: partial failure)**：
- 第 i 次 (i < num_entries) `exfat_set_dentry` 返回 err != 0
  → 立即返回 err；前 i 个 dentry 已落盘且**不**回滚。caller 责任
  在该错误路径下置 vol_flags VOLUME_DIRTY 以触发 mount fsck。

**Invariant** (id=exfat-set-dentry-no-locks)：
本层 `exfat_set_dentry` / `exfat_set_dentry_set` 均不取/释/检查任何
锁。锁责由 caller 持 parent->inode_lock 或 sbi->s_lock。继承
exfat_ent_set / dentry_iter 同精神。

**Invariant** (id=exfat-set-dentry-leak-free)：
所有返回路径下，`LOS_MemAlloc(m_aucSysMem0, blocksize)` 已成功的
buf 都经过 `LOS_MemFree(m_aucSysMem0, buf)`。set_dentry_set 内部
循环每次的 buf 即用即释，**不**跨调用持有。

**Invariant** (id=exfat-set-dentry-cluster-boundary)：
exfat_set_dentry_set 必须能正确处理 dentry-set 跨越目录簇边界。
每次循环内调 exfat_set_dentry 都从 `start_entry + i` 重新算
clu/sector，**不**复用上次的 cur_clu / sector_lba。该 invariant
由"逐个 set_dentry"的实现策略保证；alternative 实现（一次性
mass write）**禁止**——会写到不属于本目录的扇区。继承
exfat-dentry-iter-set-cluster-boundary 的精神到写端。

**Invariant** (id=exfat-set-dentry-no-spinlock-callsite)：
本层含 LOS_MemAlloc + los_part_read + los_part_write，皆可能睡眠。
**禁止在持自旋锁时调用**。继承 exfat-ent-set-no-spinlock-callsite
同策略。

**Invariant** (id=exfat-set-dentry-bounded-per-call)：
每次 exfat_set_dentry 恰好一次 LOS_MemAlloc + 一次 los_part_read +
一次 los_part_write。无递归。O(N) IO 仅在 set_dentry_set 循环层
体现，N == num_entries <= EXFAT_DENTRY_SET_MAX。

**Invariant** (id=exfat-set-dentry-no-mutate-on-arg-failure)：
任一 -EINVAL / -ENOMEM 路径下不修改 sbi / dir / in / set 的字段；
caller 传入指针不变。-EIO 路径下盘上扇区可能在 write 失败窗口处于
部分修改状态——非本函数 invariant 违反，由 caller VOLUME_DIRTY
策略协调（与 exfat-ent-set-no-mutate-on-failure 同策略）。

**Invariant** (id=exfat-set-dentry-no-chksum-touch)：
本层**不**计算 chksum16，**不**修改 set[0].dentry.file.checksum
字段。caller 在调用前必须自己装填好 chksum。这与 dentry_iter 读
端 `exfat_validate_dentry_set` 验 chksum 但不重算的分层对偶。

**Invariant** (id=exfat-set-dentry-no-vol-flags-touch)：
本层**不**读写 sbi->vol_flags。VOLUME_DIRTY 翻转是
exfat_set_vol_flags helper 与 caller 编排（Wave B Stage 2a 已批准）
的责任。本层若 IO 失败，caller 决定是否置 dirty。

**Invariant** (id=exfat-set-dentry-set-no-rollback)：
exfat_set_dentry_set 第 i 次失败时**不**撤销前 i-1 次写入。set 写
是非原子操作；rollback 需要保留 pre-image，pre-image 缓冲增加 IO
与内存代价，且 v1 不引入。caller 必须在 -errno 路径置 VOLUME_DIRTY。

## Refine Prompt

锁矩阵（callee 不取，caller 持有，与 exfat_ent_set 矩阵呼应）：

| caller 路径                              | 持锁                                         | 释锁时机                |
|---|---|---|
| Stage 4d unlink (mark dentry deleted)    | parent->inode_lock                           | set_dentry 返回后        |
| Stage 4e create / 4f mkdir (write set)   | parent->inode_lock                           | set_dentry_set 返回后    |
| Stage 4f rename 跨目录                   | sbi->s_lock                                  | set_dentry_set 返回后    |
| 直接 set_dentry 测试 (cmocka)            | 无（host LosMux nop）                        | N/A                      |

锁序约束：caller 已持的锁层级必须 ≥ inode_lock；本层不引入新锁
需求。spinlock 禁区：本路径含 alloc + IO，禁止持自旋锁进入。

## Refine Prompt — Phase 2 (cluster boundary correctness)

dentry-set 在小簇 (cluster_size=512) 文件系统上可能跨簇：
3-dentry 普通文件 set = 96 字节落入单扇区 / 单簇是常见场景，但
17-dentry 长文件名 set = 544 字节，若簇起点偏移使 set 横跨两簇，
正确实现必须每次都从 entry_idx 重新计算 clu，而不是顺序复用。

本 spec invariant `exfat-set-dentry-cluster-boundary` 把这一要求
显式锁定到写端。代价是 O(N) sector IO 而非 O(1)；v1 求正确不求
性能，与读端 exfat_get_dentry_set 同策略。

不允许的"优化"：
- 一次 los_part_read N 个连续扇区，splice 全部 dentry 后一次
  los_part_write
- 假设 set[i] 与 set[i-1] 在同一扇区
- 跳过中间项的 cluster-walk

理由：跨簇时 set[K] 与 set[K+1] 落在不连续的簇，"连续扇区"假设
读到的字节不属于本目录——可能踩进相邻目录的 dentry，把别人的
dentry 当作自己的 in-use 项写脏。
