[PROMPT]
Wave B Stage 4b 准备工：实现 dentry slot 分配 helper
`exfat_alloc_dentry_slot`，作为 Stage 4d-g (Create / Mkdir / Unlink /
Rmdir VOPs) 的共同前置——任何"在父目录里塞 N 个连续 dentry"的操作
都需要先找到 N 个连续的"可写"slot。

Linux 对照：fs/exfat/dir.c::exfat_find_free_entry。Linux 在找不到空
slot 时调 exfat_extend_dir 增长目录簇链；本 v1 **不**走该分支，
返 -ENOSPC 即可。理由：
- 用户态 mkdir/touch 触发的目录 dentry 量非常小（每个 file 3 dentry）
  默认 cluster_size=4KB → 128 entries/cluster → 单簇足够 ~40 个文件
- 自动 grow 目录在 LiteOS-A 还需要：alloc_cluster + ent_set 把新簇
  接入 FAT chain + parent_ei->size 同步更新 + parent dentry 的 stream
  辅助里 valid_size 同步——4 个原子操作的事务。Stage 4b 不引入
  这条路径，等 Stage 4b' (extend_dir) 单独 evolve

slot 的"可写"定义遵循 Microsoft exFAT 规范：
- type == 0x00（UNUSED 终止符）：所有后续 slot 也都是 free 的
- type 高位 bit7 == 0（DELETED，含原 0x05 / 0x40 / 0x41 等）：slot 已
  释放可重用
- type 高位 bit7 == 1（IN_USE，含 0x85 / 0xC0 / 0xC1）：占用中

扫描策略：线性遍历 dir 簇链，统计当前 run 长度（free slot 个数），
遇 in-use 重置 run。遇终止符时 run 直接外推到 dir 末尾——若终止符
后剩余空间足以容纳 n_entries 即返回 run_start，否则 -ENOSPC（v1 不
扩容）。

实现层借用已有 `exfat_get_dentry`（Stage 1.dentry_iter，已批准）
完成跨簇/线性 IO；本层不直接调 los_part_*。无锁；caller 持
parent->inode_lock（运行期）或 sbi->s_lock（rename 跨目录）。
含 alloc + IO，禁止持自旋锁进入。

Out of scope（明确不做）：
- 自动 grow 目录 (extend_dir)：v1 无空间 → -ENOSPC，由 Stage 4b' 后
  续 evolve 引入
- chksum 计算 / dentry-set 内容组装：caller 在调本 helper 之前/之后
  自己处理；本层只负责"找出地方"
- 写入 dentry 内容：本层是纯查找，**不**调 set_dentry / set_dentry_set
- pre-zero / pre-fill found slot：caller 必须紧接调 set_dentry_set
  写入新内容；本层不修改盘上字节

[RELY]
```c
/* common.header 已声明类型；不重列 */
typedef struct exfat_sb_info exfat_sb_info;
typedef struct exfat_chain   exfat_chain;
struct exfat_dentry;             /* 32B 联合 */

/* dentry/dentry.header 常量 */
#define DENTRY_SIZE                32u
#define EXFAT_FIRST_CLUSTER        2u
#define EXFAT_DENTRY_SET_MAX       19
#define ALLOC_FAT_CHAIN            0x01u
#define ALLOC_NO_FAT_CHAIN         0x03u
#define EXFAT_UNUSED               0x00u  /* dentry type byte: end-of-dir */

/* errno */
#define EINVAL  22
#define EIO     5
#define ENOMEM  12
#define ENOSPC  28

/* 已批准 stage 公共 API */
extern int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                             int entry_idx, struct exfat_dentry *out,
                             uint64_t *out_sector);
```

[GUARANTEE]
```c
/*
 * exfat_alloc_dentry_slot — locate `n_entries` contiguous "writable" slots
 *                           in dir's chain.
 *
 * 调用约定：
 *   - 调用方持有 parent->inode_lock（运行期）或 sbi->s_lock
 *     （rename 跨目录路径）；本函数不取任何锁。
 *   - 同步 IO：返回时 IO 已完成；本函数不修改盘上任何字节
 *     （纯查找）。
 *   - dir->dir/size/flags 必须反映 caller 已知的 dir chain 实际状态；
 *     本函数依赖 dir->size 作为扫描上界。
 *
 * 返回值：
 *   - 0       ：成功；*slot_idx_out 是 run 起点的线性 dentry 索引
 *               （∈ [0, dir->size * dentries_per_clu)）。
 *   - -EINVAL ：sbi/dir/slot_idx_out == NULL；n_entries < 1；
 *               n_entries > EXFAT_DENTRY_SET_MAX；dir->flags 非法；
 *               dir->dir < EXFAT_FIRST_CLUSTER；sbi 字段无效。
 *   - -ENOSPC ：当前 dir 链内不存在长度 >= n_entries 的 free run。
 *               v1 不扩容；caller 决定是否后续 evolve 调 extend_dir。
 *   - -EIO    ：扫描过程中 exfat_get_dentry 返回 -EIO（损坏 chain
 *               或 IO 失败）。
 *   - -ENOMEM ：扫描过程中 exfat_get_dentry 返回 -ENOMEM。
 *
 * 副作用：
 *   - 成功：slot_idx_out 写入；其它字段不变。
 *   - 失败：slot_idx_out **不**写入。
 *
 * Pre：sbi != NULL && dir != NULL && slot_idx_out != NULL；
 *      1 <= n_entries <= EXFAT_DENTRY_SET_MAX；
 *      dir->dir >= EXFAT_FIRST_CLUSTER；
 *      dir->flags ∈ {ALLOC_FAT_CHAIN, ALLOC_NO_FAT_CHAIN}；
 *      sbi->dentries_per_clu > 0；dir->size > 0。
 */
extern int exfat_alloc_dentry_slot(const exfat_sb_info *sbi,
                                    const exfat_chain *dir,
                                    int n_entries,
                                    int *slot_idx_out);
```

[SPECIFICATION]

**Pre-Condition**：
1. `sbi != NULL && dir != NULL && slot_idx_out != NULL`。
2. `1 <= n_entries <= EXFAT_DENTRY_SET_MAX`。
3. `dir->dir >= EXFAT_FIRST_CLUSTER`；
   `dir->flags ∈ {ALLOC_FAT_CHAIN, ALLOC_NO_FAT_CHAIN}`；
   `dir->size >= 1`；`sbi->dentries_per_clu > 0`。
4. 调用方持 parent->inode_lock 或 sbi->s_lock；不在持自旋锁状态。

**Post-Condition (Case 1: empty terminator at start)**：
- 第一次 get_dentry(0).type == EXFAT_UNUSED；
- run_start = 0；free_run_len = dir->size * dentries_per_clu。
- 若 free_run_len >= n_entries → 写 *slot_idx_out = 0；返回 0。
- 否则（dir 太小）→ 返回 -ENOSPC。

**Post-Condition (Case 2: in-use prefix + free run)**：
- 扫描中第 K 个 dentry 起 (type & 0x80) == 0 即可重用 slot；
  累积到第 K + n_entries - 1 仍可重用 → 写 *slot_idx_out = K；
  返回 0。

**Post-Condition (Case 3: terminator after partial run)**：
- 扫描中第 K 个 dentry 起开始累积可重用 slot（type & 0x80 == 0），
  累积长度 < n_entries 时遇到 type == EXFAT_UNUSED：
  - 该 slot 同时也是可重用的，且其后所有 slot 都是 free。
  - free_run_len = dir->size * dentries_per_clu - K。
  - 若 free_run_len >= n_entries → 写 *slot_idx_out = K；返回 0。
  - 否则 → 返回 -ENOSPC。

**Post-Condition (Case 4: in-use breaks run)**：
- 扫描中第 K 起累积可重用 slot；累积长度 < n_entries 时遇到
  (type & 0x80) == 1 且 type != EXFAT_UNUSED 的 slot：
  - 重置 run_start = -1；继续扫描。
  - 若后续直到 dir 尾仍未找到长度 >= n_entries 的 run → -ENOSPC。

**Post-Condition (Case 5: full dir, no free run)**：
- 全部 dir->size * dentries_per_clu 个 dentry 都 in-use（无任何
  EXFAT_UNUSED 也无任何 deleted slot）→ 返回 -ENOSPC。

**Post-Condition (Case 6: invalid args)**：
- 任一指针 NULL，或 n_entries 越界，或 dir 字段非法 → 返回 -EINVAL；
  *slot_idx_out 不写；不调 IO。

**Post-Condition (Case 7: get_dentry IO/ENOMEM/EIO failure)**：
- 扫描中某次 exfat_get_dentry 返回 != 0 → 错误码透传；
  *slot_idx_out 不写。

**Invariant** (id=exfat-alloc-slot-no-locks)：
本函数不取/释/检查任何锁。锁责由 caller 承担。继承
exfat-set-dentry-no-locks / exfat-dentry-iter-no-locks 同精神。

**Invariant** (id=exfat-alloc-slot-readonly)：
本函数**不**修改盘上任何字节。仅调 exfat_get_dentry（read-only），
不调 set_dentry / set_dentry_set / ent_set / 任何 los_part_write。
caller 在拿到 slot_idx 后自行 set_dentry_set 写入新内容——这是
"找位置"与"写内容"的分层。

**Invariant** (id=exfat-alloc-slot-no-grow)：
v1 不调 alloc_cluster / extend_dir。dir 链满时即 -ENOSPC，**绝不**
擅自增长目录。该 invariant 锁住 v1 行为；后续 evolve 引入 grow 时
应该独立 helper（exfat_extend_dir + exfat_alloc_dentry_slot_grow）
而不是修改本 helper 含义。

**Invariant** (id=exfat-alloc-slot-bounded)：
扫描上界 = dir->size * sbi->dentries_per_clu。即使 chain 损坏出现
环路，本函数最多调 N 次 get_dentry，N = scan 上界。无递归。

**Invariant** (id=exfat-alloc-slot-no-spinlock-callsite)：
本函数内部 exfat_get_dentry 含 LOS_MemAlloc + los_part_read，可能
睡眠。**禁止持自旋锁进入**。继承 exfat-dentry-iter-no-spinlock-callsite
策略。

**Invariant** (id=exfat-alloc-slot-no-mutate-on-failure)：
任一 -errno 路径下，*slot_idx_out 不写；sbi/dir 字段不变；caller
传入指针不变。

**Invariant** (id=exfat-alloc-slot-no-chksum-touch)：
本函数不读不写 set[0].dentry.file.checksum；与 set_dentry_set 同
分层，"chksum 是 dentry compose 阶段的事"。

**Invariant** (id=exfat-alloc-slot-terminator-extends-run)：
type == EXFAT_UNUSED 既是 deleted slot（可立即用）也是终止符（其
后所有 slot 都可用）。所以遇 terminator 时不再继续扫描——直接以
run_start 起点 + (上界 - run_start) 作为 run 长度判断 ENOSPC。
这与 Linux exfat_find_free_entry 同语义。

**Invariant** (id=exfat-alloc-slot-no-side-effect-on-enospc)：
返回 -ENOSPC 时不修改 sbi/dir/盘上任何状态——这与 v1 不扩容的
策略一致。caller 拿到 -ENOSPC 后可以选择"报错向上"或"调
extend_dir 增长后重试"（后者是 Stage 4b' 的接口）。

## Refine Prompt

锁矩阵（callee 不取，caller 持有）：

| caller 路径               | 持锁                  | 备注                |
|---|---|---|
| Stage 4d Create / 4f Mkdir | parent->inode_lock    | 创建路径，已持父锁  |
| Stage 4f Rmdir 跨目录      | sbi->s_lock           | rename 同上层路径   |
| 直接 alloc_slot 测试       | 无（host LosMux nop）  | cmocka              |

锁序约束：caller 已持的锁层级必须 ≥ inode_lock；本层不引入新锁
需求。spinlock 禁区：本路径含 alloc + IO，禁止持自旋锁进入。

## Refine Prompt — Phase 2 (extension contract for Stage 4b')

未来如需引入自动 grow 目录（Stage 4b'），应当**新增**独立的高层
wrapper `exfat_alloc_dentry_slot_grow`，组合：
1. `exfat_alloc_dentry_slot(...)` 先尝试 in-place
2. 若 -ENOSPC → `exfat_extend_dir(...)` 增长一簇并把新簇接入 FAT
   chain；同步更新 parent_ei->size + parent dentry stream 的
   valid_size 字段
3. 重新 `exfat_alloc_dentry_slot(...)` 即可成功

不应当**修改**本 helper 的语义——保持"v1 不扩容"的纯查找契约，
让 caller / 后续 wrapper 自行编排，避免本 helper 同时承担"找"与
"扩"两种职责（违反单一职责原则；测试也更清晰）。
