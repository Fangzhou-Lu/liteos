[PROMPT]
exfat_dentry_iter —— 目录簇链 dentry 读取与 dentry-set 解析层。本 stage
把 dentry 阶段私有的目录扇区 IO（已写在 `exfat_find_root_dentry` 内联里）
提升为公共 API，并补齐 dentry-set 拆解 + chksum16 校验，让 lookup /
readdir 直接复用：

1. `exfat_get_dentry(sbi, dir, entry_idx, *out, *out_sector)` —— 取目录簇链中
   线性索引 `entry_idx` 处的 1 个 32B dentry。内部按 byte_offset =
   entry_idx * DENTRY_SIZE 计算所在簇/扇区，沿 FAT 链 skip 到目标簇
   （`exfat_get_next_cluster` 逐簇推进；ALLOC_NO_FAT_CHAIN 直接加偏移），
   `los_part_read` 一个扇区，`memcpy_s` 复制 32B 到 out。

2. `exfat_get_dentry_set(sbi, dir, start_entry, *set, max_entries, *num_entries)`
   —— 取从 start_entry 起的最多 max_entries 个连续 dentry。要求第一项
   是 EXFAT_FILE primary (type==0x85)；之后 stream + nameN... 由 SecondaryCount
   指定。处理跨簇边界（dentry-set 可跨簇）。返回成功时 *num_entries =
   1 + ep->dentry.file.num_ext (primary + secondaries)。

3. `exfat_validate_dentry_set(set, num_entries)` —— 纯计算 chksum16 校验。
   调用 `exfat_calc_chksum16(set, num_entries * DENTRY_SIZE, 0, CS_DIR_ENTRY)`
   （CS_DIR_ENTRY 跳过 set[0] 的 byte 2-3 即 SetChecksum 字段），与
   set[0].dentry.file.checksum 比较；不匹配返 -EIO。

仅读路径——不调用 los_part_write。本层无锁；锁由调用方持 inode_lock。
LE 解码用 `LE16_TO_HOST`（恒等于 LE host）。

[RELY]
```c
/* common.header 已声明 (exfat_sb_info / exfat_chain / exfat_inode_info) 此处不重列 */

/* 内核内存 (kernel/include/los_memory.h) */
extern VOID  *LOS_MemAlloc(VOID *pool, UINT32 size);
extern UINT32 LOS_MemFree(VOID *pool, VOID *ptr);
extern UINT8 *m_aucSysMem0;

/* 分区 IO (drivers/block/disk/include/disk.h) */
extern INT32 los_part_read(INT32 part_id, VOID *buf, UINT64 sector,
                           UINT32 count, BOOL useRead);

/* libsec */
extern int memcpy_s(void *dest, size_t destMax, const void *src, size_t count);

/* 日志 */
extern void PRINT_ERR(const char *fmt, ...);

/* 已批准 stage 公共 API（来自 fs/exfat/include/exfat.h，已在 common.header） */
extern int exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                                  uint32_t *next_clu);
extern uint16_t exfat_calc_chksum16(const void *data, int len,
                                     uint16_t chksum, int type);
static inline uint64_t exfat_clu_to_sector(const exfat_sb_info *sbi,
                                            uint32_t clu);

/* exfat_raw.h */
#define DENTRY_SIZE                32u
#define DENTRY_SIZE_BITS           5u
#define EXFAT_FIRST_CLUSTER        2u
#define EXFAT_FREE_CLUSTER         0u
#define EXFAT_EOF_CLUSTER          0xFFFFFFFFu
#define ALLOC_FAT_CHAIN            0x01u
#define ALLOC_NO_FAT_CHAIN         0x03u
#define EXFAT_FILE                 0x85u   /* primary; bit7 set = in-use */
#define EXFAT_STREAM               0xC0u
#define EXFAT_NAME                 0xC1u
#define EXFAT_UNUSED               0x00u
#define CS_DIR_ENTRY               2       /* exfat_calc_chksum16 type 参数 */
#define EXFAT_DENTRY_SET_MAX       19      /* 1 primary + 1 stream + 17 name (Microsoft 上限) */

/* LE → host (与 exfat_dentry.c 同款；恒等于 LE host) */
#define LE16_TO_HOST(x) ((uint16_t)(x))

/* dentry primary 中我们用到的两个字段 (来自 exfat_raw.h) */
struct exfat_dentry;  /* 32B 联合 */
/* ep->type;                            uint8_t */
/* ep->dentry.file.num_ext;             uint8_t  (secondary count) */
/* ep->dentry.file.checksum;            __le16 (SetChecksum) */
```

[GUARANTEE]
```c
/*
 * 调用约定:
 * - 调用方负责 sbi/dir 字段稳定（mount 期 sbi 私有；运行期持 inode_lock）。
 * - dir->dir ≥ EXFAT_FIRST_CLUSTER；dir->flags ∈ {ALLOC_FAT_CHAIN,
 *   ALLOC_NO_FAT_CHAIN}；其它值返 -EINVAL。
 * - entry_idx ≥ 0；entry_idx 太大致跨链超 EOF → -EIO。
 * - 自行 LOS_MemAlloc 一个扇区缓冲，所有路径释放 (Invariant
 *   exfat-dentry-iter-leak-free)。
 * - 成功 (=0): out 写入 32B dentry；out_sector (可为 NULL) 写入该扇区 LBA。
 * - 失败：out / out_sector 不写。
 * - 锁约束：内含 alloc + IO，**禁止在持自旋锁时调用**。
 */
int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                     int entry_idx, struct exfat_dentry *out,
                     uint64_t *out_sector);

/*
 * 调用约定:
 * - dir/start_entry 同 exfat_get_dentry。set != NULL，max_entries ≥ 1，
 *   max_entries ≤ EXFAT_DENTRY_SET_MAX；num_entries != NULL。
 * - 内部循环调 exfat_get_dentry，必要时跨簇重新定位。
 * - 第一项必须 type == EXFAT_FILE；否则 -EIO（不是 file primary）。
 * - secondary 数量 = set[0].dentry.file.num_ext；总数 = 1 + num_ext。
 *   总数 > max_entries → -EIO（buffer 不够）；总数 > EXFAT_DENTRY_SET_MAX
 *   → -EIO（损坏 primary）。
 * - 中间项 type 不在 {EXFAT_STREAM, EXFAT_NAME, EXFAT_FILE} 高 7 位指示
 *   "in-use" 范围（按 Microsoft 规范，secondary type bit7 set 表示 in-use）→
 *   -EIO。
 * - 成功 (=0): set[0..*num_entries-1] 写入；其余位置不动。
 * - 失败：set 内容未定义；*num_entries 未定义；调用方不应读。
 * - 锁约束：同 exfat_get_dentry（alloc + IO）。
 */
int exfat_get_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
                         int start_entry, struct exfat_dentry *set,
                         int max_entries, int *num_entries);

/*
 * 调用约定:
 * - 纯计算；无 IO/无 alloc/无锁。spinlock-safe。
 * - set != NULL；num_entries ∈ [1, EXFAT_DENTRY_SET_MAX]；set[0].type ==
 *   EXFAT_FILE；否则 -EINVAL。
 * - 计算: chksum = exfat_calc_chksum16(set, num_entries * DENTRY_SIZE, 0,
 *   CS_DIR_ENTRY); CS_DIR_ENTRY 让 calc 跳过 set[0] 的字节 2-3
 *   (即 file.checksum 自身字段)。
 * - 与 LE16_TO_HOST(set[0].dentry.file.checksum) 比较：
 *     相等 → 返回 0；不等 → 返回 -EIO。
 * - 不修改 set 任何字节；不写任何全局。
 */
int exfat_validate_dentry_set(const struct exfat_dentry *set, int num_entries);
```

[SPECIFICATION]

**Pre-Condition (exfat_get_dentry)**:
sbi != NULL && dir != NULL && out != NULL；entry_idx ≥ 0；
sbi 完成 boot sector 解析 (cluster_size / sect_per_clus_bits / blocksize / part_id
就位)；dir->dir ≥ EXFAT_FIRST_CLUSTER；dir->flags ∈ {ALLOC_FAT_CHAIN,
ALLOC_NO_FAT_CHAIN}；调用方未持 LiteOS spinlock。

**Post-Condition (Case 1: success)**:
返回 0；out 含目标位置的 32B dentry 副本；out_sector (若 != NULL) 含该扇区
分区相对 LBA。其它字节不写。

**Post-Condition (Case 2: invalid args)**:
sbi/dir/out 任一为 NULL，或 dir->flags 不在合法值 → 返回 -EINVAL；out 不写。

**Post-Condition (Case 3: chain navigation failure)**:
ALLOC_FAT_CHAIN 路径中 exfat_get_next_cluster 返回错误，或途中遇 EOF
但 entry_idx 仍未到达目标簇 → 返回 -EIO；out 不写；alloc 资源已释放。

**Post-Condition (Case 4: IO failure)**:
los_part_read 返回 < 0 → 返回 -EIO；out 不写；alloc 资源已释放。

**Post-Condition (Case 5: alloc failure)**:
LOS_MemAlloc(blocksize) 返回 NULL → 返回 -ENOMEM；out 不写；无 IO 触发。

**Pre-Condition (exfat_get_dentry_set)**:
sbi/dir/set/num_entries != NULL；start_entry ≥ 0；
1 ≤ max_entries ≤ EXFAT_DENTRY_SET_MAX；其它同 exfat_get_dentry。

**Post-Condition (Case 1: success)**:
返回 0；*num_entries = 1 + set[0].dentry.file.num_ext；set[0..*num_entries-1]
是连续 dentry 副本；set[0].type == EXFAT_FILE；后续项 type 指示 "in-use"
secondary（bit7 = 1）。

**Post-Condition (Case 2: not a primary)**:
读到的 set[0].type != EXFAT_FILE → 返回 -EIO；set / *num_entries 内容
未定义。

**Post-Condition (Case 3: too many secondaries)**:
1 + set[0].dentry.file.num_ext > max_entries → 返回 -EIO（调用方 buffer 不够）。
1 + set[0].dentry.file.num_ext > EXFAT_DENTRY_SET_MAX → 返回 -EIO（primary
声明的 num_ext 超出 Microsoft 规范上限，视为损坏）。

**Post-Condition (Case 4: corrupt secondary)**:
某 secondary type 不在 in-use secondary 范围（type 高位 bit7 == 0 即未在用，
或 type 不属 {EXFAT_STREAM, EXFAT_NAME}）→ 返回 -EIO。

**Post-Condition (Case 5: chain / IO / alloc failure)**:
underlying exfat_get_dentry 返回错误 → 错误码透传；set / *num_entries 未定义。

**Pre-Condition (exfat_validate_dentry_set)**:
set != NULL；1 ≤ num_entries ≤ EXFAT_DENTRY_SET_MAX。

**Post-Condition (Case 1: not a primary)**:
set[0].type != EXFAT_FILE → 返回 -EINVAL。

**Post-Condition (Case 2: chksum match)**:
exfat_calc_chksum16(set, num_entries * DENTRY_SIZE, 0, CS_DIR_ENTRY) ==
LE16_TO_HOST(set[0].dentry.file.checksum) → 返回 0。

**Post-Condition (Case 3: chksum mismatch)**:
不等 → 返回 -EIO；set 不修改。

**Invariant** (id=exfat-dentry-iter-readonly):
本层任何函数都不调用 los_part_write 或 los_disk_write。

**Invariant** (id=exfat-dentry-iter-no-locks):
不获取/释放/检查任何锁。锁权由调用方 (lookup / readdir) 承担。
继承 fat_chain stage 的 helper-no-locks 同精神。

**Invariant** (id=exfat-dentry-iter-leak-free):
exfat_get_dentry / exfat_get_dentry_set 在所有返回路径下，本函数 LOS_MemAlloc
的临时缓冲已 LOS_MemFree。get_dentry_set 内部多次调 get_dentry 时每次的扇区
缓冲都即用即释，**不**跨调用持有。

**Invariant** (id=exfat-dentry-iter-bounded):
exfat_get_dentry 内部簇链 skip 步数有上界 sbi->num_clusters；exfat_get_dentry_set
内部 dentry 计数有上界 EXFAT_DENTRY_SET_MAX。两者均防御损坏卷。

**Invariant** (id=exfat-dentry-iter-le-host-only):
LE16_TO_HOST 是恒等宏，依赖 LE host (ARM-LE)。SetChecksum 字段 16-bit LE 解码
通过该宏。BE host 端口须改逐字节。继承 fat_chain le-host-only 同策略。

**Invariant** (id=exfat-dentry-iter-set-primary-required):
exfat_get_dentry_set 第一项必须是 EXFAT_FILE primary（type == 0x85）。任何
其它 type 视为 not-a-set 错误 → -EIO。**注意**: type 高位 bit7 区分 in-use
(1) 与 deleted (0)；本 v1 版本只接受 in-use primary，遇 deleted (type == 0x05)
返 -EIO。（删除项处理留给后续 reclaim/undelete 路径，不在 v1 scope。）

**Invariant** (id=exfat-dentry-iter-set-checksum-fixed-skip):
exfat_validate_dentry_set 调用 exfat_calc_chksum16 必须传 type == CS_DIR_ENTRY
(=2)，使其按 Microsoft 规范跳过 offset 2-3 (SetChecksum 字段自身)。**绝不**
传 CS_DEFAULT 或自实现 skip 逻辑——这是 chksum 阶段已批准的 type 编码。

**Invariant** (id=exfat-dentry-iter-set-cluster-boundary):
exfat_get_dentry_set 必须能正确处理 dentry-set 跨越目录簇边界的情形：
若 set 起点在簇 A 末尾 K 个 dentry 处而 num_entries > K，剩余 (num_entries -
K) 个 dentry 必须从簇 A 在 FAT 链上的下一簇 B 读取，而不是越界继续读簇 A
相邻扇区。该 invariant 由内部循环对每个 dentry 单独走 exfat_get_dentry
（每次都从 entry_idx 重新算 clu/sector）保证；alternative 实现（一次性大读）
**禁止**——会读到不属于本目录的扇区。

**Invariant** (id=exfat-dentry-iter-no-spinlock-callsite):
exfat_get_dentry 与 exfat_get_dentry_set 含 LOS_MemAlloc + los_part_read，
两者**禁止在持自旋锁时调用**。exfat_validate_dentry_set 例外，纯计算。

**Invariant** (id=exfat-dentry-iter-set-not-mutated-on-failure):
任一函数失败路径下，set 缓冲与 *num_entries 内容**未定义**——调用方**不得**
依赖部分填充的 set。这是 Linux exfat 行为外的额外约束（Linux 允许部分填充
+ caller cleanup），在 v1 简化为全失败语义减少 bug 面。

## Refine Prompt

本节描述本层 API 与上下游 (mount / lookup / readdir / read) 协作时的锁约定，
继承 fat_chain stage 的 ## Refine Prompt 同样精神。

### 调用方持锁场景

| 调用方 | 调用时机 | 已持锁 | 本层 API |
|---|---|---|---|
| 后续 lookup（按名查 dentry） | 运行期 | parent->inode_lock | get_dentry_set + validate_dentry_set |
| 后续 readdir（枚举 dentry） | 运行期 | dir->inode_lock | get_dentry_set + validate_dentry_set 循环 |
| 后续 mount-time 树扫描（如 exists） | mount | 无（sbi 私有） | get_dentry_set 单次 |

### 锁层次

```
inode_lock (LosMux, per-inode)        ← 调用方持有
   ↓
[本层 helper alloc + IO]              ← LOS_MemAlloc + los_part_read，禁止持自旋锁进入
   ↓
[underlying fat_chain helper]         ← exfat_get_next_cluster (alloc + IO)
```

### 与 chain_walk visitor 的关系

readdir 的预期实现是：调用方在自己的 visitor 内调 exfat_get_dentry_set。
visitor 必须遵守 fat_chain 的 visitor-no-recursion invariant —— 不在回调内
反向调 chain_walk。本层 helper 不主动调 chain_walk，只调 get_next_cluster
（线性步进），因此 visitor 调用本层是安全的，不会触发递归。

### dentry-set buffer 生命周期

调用方分配 `struct exfat_dentry set[EXFAT_DENTRY_SET_MAX]` 的栈/堆缓冲传入
get_dentry_set；本函数填充后立即返回。validate_dentry_set 直接在缓冲上做
chksum，无额外拷贝。

## Refine Prompt — Phase 2 (cluster boundary correctness)

dentry-set 跨簇是 Microsoft exFAT 规范允许的（cluster_size 通常 4KB-512KB，
dentry-set 总长 32B*20 = 640B，所以小簇情形会跨）。Linux exfat 通过逐个
exfat_get_dentry 隐式处理（每次 get_dentry 都重算 clu/sector），不依赖
"set 一定在同一扇区"的假设。

本 spec 显式写入 invariant exfat-dentry-iter-set-cluster-boundary 锁定该
要求；实现层禁止做以下"优化"：
- 一次性 los_part_read N 个连续扇区
- 假设 set[i] 与 set[i-1] 在同一扇区
- 跳过中间项的 cluster-walk

任何上述捷径在 dentry-set 跨簇时会读到不属于本目录的扇区，可能：
- 把别的目录的 dentry 误认为本目录的 secondary
- 通过 chksum 校验（碰撞概率 1/65536）
- 在 lookup/readdir 返回错误的文件名

代价是 O(N) 次扇区 IO 而非 O(1)，但 v1 求正确不求性能；后续可加 set-spanning
buffer 优化。
