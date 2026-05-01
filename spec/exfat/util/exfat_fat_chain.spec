[PROMPT]
exfat_fat_chain —— FAT 链遍历公共工具层。本 stage 把 dentry 阶段私有的 `ReadFatEntry`
提升为公共 API，并补齐 dentry_iter / lookup / read 都将复用的三件套：

1. `exfat_clu_to_sector(sbi, clu)` —— 簇号 → 分区相对扇区起点（纯计算，inline）。
2. `exfat_get_next_cluster(sbi, cur_clu, *next_clu)` —— 读 FAT[cur_clu]，校验合法性，
   按 Linux `__exfat_ent_get` 把 raw > EXFAT_BAD_CLUSTER 的保留值统一映射为 EOF。
3. `exfat_chain_walk(sbi, start_clu, visitor, ctx)` —— 有界（≤ num_clusters）簇链遍历，
   每簇调一次 visitor；返回 0 续走、1 主动停成功、<0 错误透传。

仅读路径——不调用 `los_part_write`。本层 helper 自身无锁；锁权由调用方持有
（详见 `## Refine Prompt`）。LE host 假设（ARM-LE，由 `__LITEOS_A__` 宏保证），
与 upcase 阶段同策略。

[RELY]
```c
/* common.header 已声明的类型与常量（exfat_sb_info、EXFAT_FREE_CLUSTER 等）此处不重列 */

/* 内核内存（kernel/include/los_memory.h）*/
extern VOID  *LOS_MemAlloc(VOID *pool, UINT32 size);
extern UINT32 LOS_MemFree(VOID *pool, VOID *ptr);
extern UINT8 *m_aucSysMem0;

/* 分区相对块 IO（drivers/block/disk/include/disk.h）*/
extern INT32 los_part_read(INT32 part_id, VOID *buf, UINT64 sector,
                           UINT32 count, BOOL useRead);

/* libsec —— 强制使用 _s 变体（Huawei 安全编码硬约束）*/
extern int memcpy_s(void *dest, size_t destMax, const void *src, size_t count);

/* 日志 */
extern void PRINT_ERR(const char *fmt, ...);

/* exfat_raw.h —— 盘上常量 */
#define EXFAT_FIRST_CLUSTER       2u
#define EXFAT_RESERVED_CLUSTERS   2u
#define EXFAT_FREE_CLUSTER        0u
#define EXFAT_BAD_CLUSTER         0xFFFFFFF7u
#define EXFAT_EOF_CLUSTER         0xFFFFFFFFu

/* LE → host 包装（与 exfat_dentry.c 同款，恒等于 LE host）*/
#define LE32_TO_HOST(x) ((uint32_t)(x))

/* visitor 回调约定：
 *   返回 0  → 继续下一簇
 *   返回 1  → 主动停止（成功）
 *   返回 <0 → 错误停止（错误码透传）
 */
typedef int (*exfat_chain_visitor_t)(uint32_t clu, void *ctx);
```

[GUARANTEE]
```c
/*
 * 调用约定:
 * - 纯计算函数，无 IO / 无 alloc / 无锁。
 * - sbi != NULL；调用方保证 sbi->clu_offset 与 sbi->sect_per_clus_bits 已由
 *   exfat_parse_boot_sector 写入。
 * - clu 必须 ≥ EXFAT_FIRST_CLUSTER；调用方负责范围校验。
 * - 返回 LBA（分区相对，单位 = 逻辑扇区，搭配 los_part_read 使用）。
 * - 锁约束：调用方可在持任意锁时调用本函数（无 IO、无 alloc、无睡眠），
 *   含自旋锁路径亦安全。
 */
static inline uint64_t exfat_clu_to_sector(const exfat_sb_info *sbi, uint32_t clu);

/*
 * 调用约定:
 * - 调用方不持任何锁；本函数自行 LOS_MemAlloc 单扇区临时缓冲并在所有返回路径释放。
 * - sbi 字段（sect_size_bits / blocksize / fat_offset / num_clusters / part_id）已就位。
 * - 入参 cur_clu 须满足 EXFAT_FIRST_CLUSTER ≤ cur_clu < sbi->num_clusters；否则 -EIO。
 * - 成功 (=0)：*next_clu 写入；取值范围 = {EXFAT_EOF_CLUSTER} ∪
 *   [EXFAT_FIRST_CLUSTER, sbi->num_clusters)。
 * - 失败：返回负 POSIX errno；*next_clu 不写。
 * - 副作用：仅 los_part_read；不写盘；不修改 sbi。
 * - 锁约束：含 LOS_MemAlloc 与 los_part_read，可能睡眠。**禁止在持自旋锁时调用**。
 *   持 LosMux（含 inode_lock）调用安全。详见 `## Refine Prompt`。
 */
int exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu,
                           uint32_t *next_clu);

/*
 * 调用约定:
 * - start_clu == EXFAT_EOF_CLUSTER 视为空链 → 立即返回 0，visitor 不被调用。
 * - 遍历有上界 sbi->num_clusters；触顶 → -EIO（exfat-fat-chain-bounded）。
 * - visitor 不得在回调内调用 exfat_chain_walk（防递归）；不得 sleep；不得长持锁。
 *   该约束由调用契约保证，运行期不强制。
 * - 返回值: visitor 返回 1 → 0；visitor 返回 <0 → 透传；走到 EOF → 0；
 *   exfat_get_next_cluster 报错 → 透传；越界 → -EIO。
 * - 锁约束：本函数本身不获取/释放任何锁，但内部调用 exfat_get_next_cluster
 *   会触发 alloc + IO。**禁止在持自旋锁时调用**。运行期典型调用方
 *   （lookup / readdir / read）应已持目标 inode->inode_lock；visitor 在
 *   该锁覆盖下访问 sbi 与 inode 字段。详见 `## Refine Prompt`。
 */
int exfat_chain_walk(const exfat_sb_info *sbi, uint32_t start_clu,
                     exfat_chain_visitor_t visitor, void *ctx);
```

[SPECIFICATION]

**Pre-Condition (exfat_clu_to_sector)**:
sbi != NULL；clu ≥ EXFAT_FIRST_CLUSTER；sbi->clu_offset 与 sbi->sect_per_clus_bits
已由 exfat_parse_boot_sector 写入合法值。

**Post-Condition (exfat_clu_to_sector)**:
返回 (uint64_t)sbi->clu_offset + (uint64_t)(clu - EXFAT_FIRST_CLUSTER) *
(uint64_t)(1u << sbi->sect_per_clus_bits)。

**Pre-Condition (exfat_get_next_cluster)**:
sbi != NULL && next_clu != NULL；sbi 完成 boot sector 解析；调用方未持任何
LiteOS spinlock。

**Post-Condition (Case 1: success)**:
返回 0；*next_clu 写入；其值 ∈ {EXFAT_EOF_CLUSTER} ∪
[EXFAT_FIRST_CLUSTER, sbi->num_clusters)。具体而言：
- 若 raw FAT 内容 > EXFAT_BAD_CLUSTER（即落在 0xFFFFFFF8..0xFFFFFFFE 保留段或 EOF），
  *next_clu = EXFAT_EOF_CLUSTER（与 Linux __exfat_ent_get 一致）。
- 否则 *next_clu = LE32_TO_HOST(raw)。

**Post-Condition (Case 2: invalid cur_clu)**:
cur_clu < EXFAT_FIRST_CLUSTER 或 cur_clu ≥ sbi->num_clusters → 返回 -EIO；
*next_clu 不写；无 IO 触发；无 alloc。

**Post-Condition (Case 3: corrupt FAT content)**:
remap 后 *next_clu 仍然等于 EXFAT_FREE_CLUSTER（说明簇被链入但 FAT 表标空闲）→ -EIO；
*next_clu == EXFAT_BAD_CLUSTER → -EIO；
*next_clu != EXFAT_EOF_CLUSTER 且不在 [EXFAT_FIRST_CLUSTER, sbi->num_clusters) → -EIO。
任何 -EIO 路径都不污染调用方传入的 *next_clu。

**Post-Condition (Case 4: IO failure)**:
los_part_read 返回 < 0 → 返回 -EIO；fat_buf 已 LOS_MemFree。

**Post-Condition (Case 5: alloc failure)**:
LOS_MemAlloc(blocksize) 返回 NULL → 返回 -ENOMEM；无堆残留。

**Pre-Condition (exfat_chain_walk)**:
sbi != NULL && visitor != NULL；start_clu 任意值（含 EXFAT_EOF_CLUSTER）；
调用方未持任何 LiteOS spinlock。

**Post-Condition (Case 1: empty chain)**:
start_clu == EXFAT_EOF_CLUSTER → 返回 0；visitor 调用次数 = 0。

**Post-Condition (Case 2: visitor stops with 1)**:
存在某次 visitor(clu_k, ctx) == 1 → 立即返回 0；之后的簇不被访问。

**Post-Condition (Case 3: visitor returns <0)**:
存在某次 visitor(clu_k, ctx) == r < 0 → 立即返回 r；之后的簇不被访问。

**Post-Condition (Case 4: chain ends at EOF naturally)**:
所有 visitor 调用均返回 0，最后一簇的 next_clu == EXFAT_EOF_CLUSTER → 返回 0。

**Post-Condition (Case 5: bounded trip)**:
遍历计数达到 sbi->num_clusters 仍未碰到 EOF → 返回 -EIO（防止 FAT 环导致死循环）。

**Post-Condition (Case 6: mid-chain corruption)**:
exfat_get_next_cluster 在某簇返回 r != 0 → 立即返回 r；遍历终止。

**Invariant** (id=exfat-fat-chain-readonly):
本层任何函数都不调用 los_part_write 或 los_disk_write。

**Invariant** (id=exfat-fat-chain-no-locks):
不获取/释放/检查任何锁。锁责任完全由调用方（mount / lookup / read）承担。
该 invariant 与 exfat-dentry-find-no-locks 同策略——helper 层无锁，
锁集中在 interface/ 层。

**Invariant** (id=exfat-fat-chain-bounded):
exfat_chain_walk 主循环计数器到达 sbi->num_clusters 即返 -EIO。
exfat_get_next_cluster 校验 cur_clu 与 *next_clu 在 [EXFAT_FIRST_CLUSTER,
sbi->num_clusters) 或 EXFAT_EOF_CLUSTER。共同防御损坏卷构造的 FAT 环。
（继承 exfat-dentry-fat-traversal-bounded 同精神）

**Invariant** (id=exfat-fat-chain-leak-free):
exfat_get_next_cluster 在所有 6 个返回路径（成功 + 5 失败 case）下，
本函数 LOS_MemAlloc 的 fat_buf 都已 LOS_MemFree。失败路径上不留任何已分配 heap。

**Invariant** (id=exfat-fat-chain-le-host-only):
v1 假设 LE host（ARM-LE，由 __LITEOS_A__ 宏保证）。FAT 条目为 LE32，
通过 LE32_TO_HOST 恒等访问。未来 BE host 端口须改为逐字节解码。
（与 exfat-upcase-byte-order 同策略）

**Invariant** (id=exfat-fat-chain-reserved-remap):
读 FAT 时 raw 值 > EXFAT_BAD_CLUSTER（即 0xFFFFFFF8..0xFFFFFFFE 保留段或
0xFFFFFFFF EOF 标记）一律映射为 EXFAT_EOF_CLUSTER。**该映射在 io 后立即生效**，
之后的合法性校验拒绝 EXFAT_BAD_CLUSTER 与 EXFAT_FREE_CLUSTER。映射顺序与
Linux __exfat_ent_get 严格一致；偏离即 invariant 违反。

**Invariant** (id=exfat-fat-chain-visitor-no-recursion):
visitor 在回调内调用 exfat_chain_walk 是**未定义行为**，不被 spec 支持。
v1 不强制运行期检测；调用契约由文档与代码 review 共同保证。
该 invariant 给 dentry_iter / readdir 层的回调实现以约束。

**Invariant** (id=exfat-fat-chain-clu-to-sector-overflow-safe):
exfat_clu_to_sector 内部所有运算均在 uint64_t 域完成；clu - EXFAT_FIRST_CLUSTER
与 (1 << sect_per_clus_bits) 相乘前已是 uint64_t，避免 32 位溢出导致回绕到
boot 区或 FAT 区。该 invariant 由签名（uint64_t 返回 + 显式 (uint64_t) 强转）保证。

**Invariant** (id=exfat-fat-chain-no-spinlock-callsite):
exfat_get_next_cluster 与 exfat_chain_walk 内部含 LOS_MemAlloc + los_part_read，
两者都可能睡眠。**调用方不得在持任何 LiteOS spinlock 时调用本层任一函数**
（exfat_clu_to_sector 例外，纯计算）。违反将引发 LiteOS-A 调度错误
（持自旋锁时切上下文）。

## Refine Prompt

本节描述本层 API 与上下游（mount / dentry / lookup / readdir / read）协作时的
锁约定，作为功能行为之外独立维护的并发关注点。

### 调用方持锁场景

| 调用方 | 调用时机 | 已持锁 | 本层 API 使用 |
|---|---|---|---|
| `VfsExfatMount` 内的 `exfat_find_root_dentry`（已批准 dentry stage） | mount 步骤 8 | 无（sbi 尚未 VfsHashInsert，私有可见） | `exfat_get_next_cluster` |
| `dentry_iter`（后续 stage） | mount 阶段 | 同上：无 | `exfat_get_next_cluster` |
| `VfsExfatLookup`（后续 stage） | 运行期 | parent->inode_lock | `exfat_chain_walk` |
| `VfsExfatReaddir`（后续 stage） | 运行期 | dir->inode_lock | `exfat_chain_walk` |
| `VfsExfatRead`（后续 stage） | 运行期 | file->vnode->inode_lock | `exfat_chain_walk` 或 `exfat_get_next_cluster` |

### 锁层次约束（自上而下，不可逆向获取）

```
inode_lock (LosMux, per-inode)        ← 调用方持有此锁后才进入本层
   ↓
[sbi 字段读路径]                      ← 本层只读 sbi；boot sector 解析后 sbi 几何
                                          字段不再被改写（参见 exfat-mount-mount-data-
                                          self-consistent），无需 sbi 锁
   ↓
[本层内部 alloc + IO]                 ← LOS_MemAlloc + los_part_read，可能睡眠；
                                          因此**不得**在持自旋锁时进入本层
```

### Visitor 回调的锁约束

`exfat_chain_walk` 的 visitor 在调用方（lookup/readdir/read）持有的 `inode_lock`
覆盖下执行。visitor 实现须遵守：

1. **不得**重新获取已被调用方持有的 `inode->inode_lock`（自死锁）。
2. **不得**调用 `exfat_chain_walk`（递归引发栈爆 + 锁混乱）。
3. **不得**在回调内 sleep 长时间（持锁时间 = 调用方期望，长睡阻塞同 inode 其它操作）。
4. visitor 可读 sbi 字段（boot sector 解析后只读，无写者）。
5. visitor 可读/写 `ctx` 指向的局部状态——`ctx` 的生命周期由调用方管理。

### 与 sbi 锁的关系（v1 不引入）

Linux 在 `bitmap_lock` / `inode_hash_lock` / `s_lock` 上有更细粒度的并发控制。
本 stage（fat_chain）：

- **不需要 bitmap_lock**：FAT 表与 bitmap 是两个独立物理结构；fat_chain 只读 FAT。
- **不需要 inode_hash_lock**：本层不操作 hash 链。
- **不需要 s_lock**：本层不修改 vol_flags 或 boot_buf。

未来若引入 FAT 写路径（v2），相关 spec 须新增 `bitmap_lock + s_lock` 锁序约束。

### 死锁避免清单（与 Linux 上游不同的关注点）

- LiteOS-A 无 RCU——Linux 用 `rcu_read_lock` 兜底的 sbi 字段读取在本层退化为
  "读快照"（见 boot sector 后只读约束）。
- LiteOS-A 自旋锁不可重入也不允许睡眠——本层任何 helper 通过 `extern` 调用栈
  最终都会触达 `los_part_read` 内部的块设备 mux 锁，因此**spinlock-safe 等级**：
  - `exfat_clu_to_sector`：spinlock-safe（纯计算）
  - `exfat_get_next_cluster`：**non-spinlock**（alloc + IO）
  - `exfat_chain_walk`：**non-spinlock**（内部调用 get_next_cluster）
- 该等级由 `exfat-fat-chain-no-spinlock-callsite` invariant 强制。
