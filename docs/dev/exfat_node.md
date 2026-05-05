# exFAT Wave B Stage 4a/4b：dentry 写路径基础层开发流程实录

本文档记录 Wave B Stage 4a（`exfat_set_dentry` / `exfat_set_dentry_set`）与
Stage 4b（`exfat_alloc_dentry_slot`）的开发过程：ask-first 决策、spec
invariant 选定、双函数写口的双层设计、alloc_slot v1 不扩容的取舍，以及
cmocka 验证结果。

沿用 `docs/dev/exfat_mount.md` / `docs/dev/exfat_module.md` 的框架；
Wave B Stage 4 独有关注点：**读端对偶到写端**（每次写入含一次完整的
read-modify-write）以及**纯查找层的边界语义**（terminator 外推策略）。

---

## 1. 输入与目标

### 1.1 起始基线

| 项 | 值 |
|---|---|
| 上波末态 | Wave B Stage 3 结束（dentry_iter 读路径完整：`exfat_get_dentry` / `exfat_get_dentry_set` / `exfat_validate_dentry_set`）|
| 已就绪 API | `exfat_get_dentry` / `exfat_get_next_cluster` / `exfat_calc_chksum16` / `exfat_ent_set` / `exfat_alloc_cluster` / `exfat_free_cluster` |
| 已就绪 Vnode/Fop 槽 | Lookup / Reclaim / Readdir 四件套 / Getattr / read / open / close / seek / write 占位 |
| 当前分支 | `feature/spec-port` |
| 物理组织 | 7 文件 Linux 风格（2026-05-05 重构后）；Stage 4a/4b 代码进入 `fs/exfat/exfat_file.c` |

### 1.2 dentry 写层出口标准

1. `exfat_set_dentry` + `exfat_set_dentry_set` spec_approved + code_approved，
   DAG 节点登记（depends_on: dentry_iter, fat_chain）。
2. `exfat_alloc_dentry_slot` spec_approved + code_approved，
   DAG 节点登记（depends_on: dentry_iter, dentry_set_write）。
3. Layer 1 cclsp 零诊断；Layer 2 OHOS clang kernel build 通过。
4. cmocka：Stage 4a 22 testpoints / Stage 4b 21 testpoints，全过。
5. 关键 invariant：写端跨簇正确（每次 `set_dentry` 重新算 clu/sector）；
   alloc_slot v1 不扩容（-ENOSPC 即停）；双层无锁（caller 持锁）。

---

## 2. Stage 序列与依赖

| # | Stage | Linux 对照 | 主要输出 |
|---|---|---|---|
| 4a | **dentry_set_write** | `dir.c::exfat_update_dir_chksum` / `exfat_init_dir_entry`（buffer-cache 范式）| `exfat_set_dentry` + `exfat_set_dentry_set` |
| 4b | **alloc_dentry_slot** | `dir.c::exfat_find_free_entry` | `exfat_alloc_dentry_slot` |

依赖关系：

- Stage 4a 是 4b 的直接上游：`alloc_dentry_slot` spec 的 [RELY] 段显式引用
  Stage 4a 的 `exfat_get_dentry`（读路径）完成跨簇 IO；而 caller 拿到
  `slot_idx_out` 后紧接调用 `exfat_set_dentry_set`（写路径）写入内容——
  "找位置"与"写内容"是严格分层的。
- Stage 4a 自身依赖 dentry_iter（`exfat_get_next_cluster` + `exfat_clu_to_sector`）
  与 ent_set 建立的 "无锁 / 同步 IO / caller 持锁" 范式。
- 两个 stage 共同解锁后续 Stage 4d-g（VfsExfatCreate / Mkdir / Unlink /
  Rmdir VOPs）——任何向父目录写 dentry-set 的 VOP 都必须先经由这一层。

---

## 3. Loop A × 2：ask-first 决策

### 3.1 Stage 4a — dentry_set_write

**背景判断**：Linux `exfat_init_dir_entry` / `exfat_update_dir_chksum` 走
`sb_bread → memcpy → exfat_update_bh → brelse` 的 buffer-cache 范式。
LiteOS-A 无 buffer cache，必须每次写入做完整
read-modify-write（`los_part_read → memcpy_s → los_part_write`）。

**ask-first Q1：是否在 `set_dentry_set` 内部自动重算并回填 chksum16？**

- 选项：(a) 自动算 chksum + 回填 set[0] / (b) **不触碰 chksum，caller 责任** ← 用户选
- 理由：Linux 分层已经把 chksum 放在 dentry 组装阶段（`exfat_update_dir_chksum`），
  IO 阶段只负责"把给好的 buffer 写到磁盘"。本层若自动 recalculate，
  会让 caller 无法在写盘前自行修改 set 里其它字段——caller 必须先把
  SetChecksum 字段装填好再调 set_dentry_set。
- 确立 invariant：`exfat-set-dentry-no-chksum-touch`。

**ask-first Q2：`set_dentry_set` 第 i 次失败时是否回滚前 i-1 次写入？**

- 选项：(a) 回滚（需 pre-image 缓冲）/ (b) **不回滚，caller 置 VOLUME_DIRTY** ← 用户选
- 理由：回滚要为每个 dentry 保留 pre-image 缓冲，含额外 IO；Linux 也无回滚，
  依赖 fsck.exfat。v1 与 `exfat_ent_set` Case 7 同策略。
- 确立 invariant：`exfat-set-dentry-set-no-rollback`。

**ask-first Q3：是否做批量 sector 优化（一次 `los_part_read` N 个扇区）？**

- 选项：(a) **禁止，v1 每次 set_dentry 读一次扇区** ← 用户选 / (b) 批量读写
- 理由：跨簇时 set[K] 与 set[K+1] 可能落在不连续的簇，"连续扇区"假设会
  踩进相邻目录的 dentry 写脏它。v1 求正确不求性能；每次从 `entry_idx`
  重新计算 clu/sector 保证跨簇正确。
- 确立 invariant：`exfat-set-dentry-cluster-boundary`（写端继承读端
  `exfat-dentry-iter-set-cluster-boundary` 同精神）。

**spec 落地**：302 行，9 个 invariant，2 个 export（`exfat_set_dentry` /
`exfat_set_dentry_set`）。

### 3.2 Stage 4b — alloc_dentry_slot

**背景判断**：Linux `exfat_find_free_entry` 在找不到空 slot 时调
`exfat_extend_dir` 增长目录簇链。

**ask-first Q1：v1 找不到空 slot 时是否自动 grow 目录？**

- 选项：(a) 自动 grow（alloc_cluster + ent_set 接入 FAT chain + size 同步）/
  (b) **-ENOSPC 即停，Stage 4b' 后续 evolve** ← 用户选
- 理由：自动 grow 涉及 4 个原子操作：alloc_cluster + ent_set 把新簇接入
  FAT chain + parent_ei->size 同步更新 + parent dentry 的 stream
  辅助 valid_size 同步。这是独立事务语义，v1 不引入。典型 4KB 簇容量
  128 entries/cluster 足够 ~40 个文件，日常场景不触发。
- 确立 invariant：`exfat-alloc-slot-no-grow`，并在 Refine Prompt Phase 2
  钉定未来 wrapper 的扩展合同（新增 `exfat_alloc_dentry_slot_grow`
  不修改本 helper 语义）。

**ask-first Q2：terminator（type==0x00）遇到时是否继续扫描后续 slot？**

- 选项：(a) 继续扫（每 slot 都 IO）/ (b) **terminator 外推到 dir 末尾，早退** ← 用户选
- 理由：Microsoft exFAT 规范明确：type==0x00 之后所有 slot 均为 unused，
  无需再发 IO。命中 terminator 时直接以 `max_dentries - run_start`
  计算剩余空间，决定 ENOSPC 还是返回 run_start。
- 确立 invariant：`exfat-alloc-slot-terminator-extends-run`。

**spec 落地**：227 行，9 个 invariant，1 个 export（`exfat_alloc_dentry_slot`）。

---

## 4. 跨 Stage invariant：dentry 写口双层与 alloc v1 边界

### 4.1 为什么写口是双层：`exfat_set_dentry` vs `exfat_set_dentry_set`

读端的 `exfat_get_dentry` / `exfat_get_dentry_set` 是双层结构：
前者处理单 dentry IO，后者循环组装多个。写端完全对偶，理由相同：

- **`exfat_set_dentry`（单 dentry 写）**：每次做一次完整的
  read-modify-write。跨簇时 clu/sector 从 `entry_idx` 重新计算，
  不依赖上一次的 cur_clu。O(clu_offset) FAT chain walk + 1 read + 1 write。
- **`exfat_set_dentry_set`（整 set 写）**：仅是循环 N 次调单 dentry 写，
  无任何额外状态。部分提交失败时不回滚——这是分层选择，不是 bug。
  N 的上界 `EXFAT_DENTRY_SET_MAX = 19`（Microsoft 规范上限）。

这样分层的好处：caller（未来的 VfsExfatCreate / Mkdir）可以直接调
`exfat_set_dentry_set` 一次写完整个 file/stream/name 三元组，而
单元测试可以精确控制每个 dentry 的写入顺序和失败注入点，不需要构造
完整的 VFS 环境。

### 4.2 alloc_slot v1 为什么不做 auto-grow（-ENOSPC 即停）

`exfat-alloc-slot-no-grow` 把 v1 边界锁住，理由三层：

1. **容量充裕**：默认 `cluster_size = 4 KiB` → 128 entries/cluster → 单簇容
   纳 ~42 个 3-dentry 文件。实测 QEMU LTP smoke 中 mkdir/creat/unlink
   操作量远低于该上限。
2. **事务复杂度**：grow 目录需要 alloc_cluster + ent_set 接入 FAT chain +
   `parent_ei->size` 同步 + parent stream dentry 的 `valid_size` 字段回写——
   4 个原子操作，任一中途失败都需要独立的回滚策略。这是独立 stage
   （Stage 4b'）的复杂度预算，不应混入"纯查找"这一层。
3. **接口稳定性**：`exfat-alloc-slot-no-grow` 钉死了本 helper 是
   "纯查找，不修改盘上任何字节"的语义（invariant `exfat-alloc-slot-readonly`）。
   未来 Stage 4b' 引入 wrapper `exfat_alloc_dentry_slot_grow` 时，组合：
   先调本 helper → -ENOSPC 则 extend_dir → 重调本 helper。本层行为不变，
   测试不需要重写。

### 4.3 统一的无锁范式

两个 stage 的所有函数均不取/释/检查任何锁，继承
`exfat_ent_set` / dentry_iter 建立的三层无锁约定：

| caller 路径 | 持锁 |
|---|---|
| Stage 4d Create / 4e Mkdir（向父目录写 dentry-set）| parent->inode_lock |
| Stage 4f rename 跨目录 | sbi->s_lock |
| cmocka host（LosMux nop）| 无 |

两层均含 `LOS_MemAlloc` + `los_part_*`，**禁止持自旋锁进入**
（invariants `exfat-set-dentry-no-spinlock-callsite` /
`exfat-alloc-slot-no-spinlock-callsite`）。

---

## 5. 多层防御反馈

### 5.1 Layer 1 cclsp（编辑期）

Stage 4a 代码全程 `clangd --check fs/exfat/exfat_file.c → 0 diagnostics`。
Wave B Stage 4 新注意点：

- `exfat_set_dentry` 的 `goto out` 单标签回收 buf，确保所有路径
  （read fail / memcpy_s fail / write fail）都经过 `LOS_MemFree`。
  Invariant `exfat-set-dentry-leak-free`。
- `byte_off` 用 `(uint64_t)(uint32_t)entry_idx * (uint64_t)DENTRY_SIZE`
  显式双重转型，防 ARM32 32-bit 乘溢出（`entry_idx * 32` 最大值
  19 * 32 = 608 字节，单 set 范围内安全；但 `dir->size * dentries_per_clu`
  可达 65535 * 16 → 用 `(uint64_t)dir->size * (uint64_t)sbi->dentries_per_clu`
  在 alloc_slot 里同款防溢）。

### 5.2 Layer 2 OHOS clang kernel build

Stage 4a/4b 代码（含入前为 `exfat_dentry_set_write.c` /
`exfat_alloc_dentry_slot.c`，重构后合并进 `exfat_file.c`）进入
`build.sh arm_virt clang` 编译链，远端 192.168.1.15 通过，
`liteos.map` 可见新增符号：

```
exfat_set_dentry
exfat_set_dentry_set
exfat_alloc_dentry_slot
```

### 5.3 Layer 5 cmocka

| Stage | testpoints | 分组 | 关键覆盖 |
|---|---|---|---|
| 4a dentry_set_write | **22** | arg validation (7) / happy path (3) / failure paths (3) / set_-arg-validation (4) / set_-happy+partial (2) / invariants (3) | round-trip byte-exact / chksum 不被覆盖 / read-fail 无 write / partial-failure 无 rollback / bounded IO (N reads + N writes) |
| 4b alloc_dentry_slot | **21** | arg validation (8) / default-image happy (4) / layout-sensitive (5) / failure passthrough (1) / invariants (3) | terminator 短路 / deleted 可重用 / in-use 重置 run / dir 全满 -ENOSPC / 只读（write_count==0）|

- **layout-sensitive 测试** 在 cmocka host 直接用 Stage 4a 的 `exfat_set_dentry`
  向合成镜像注入 in-use / deleted 布局后再调 `exfat_alloc_dentry_slot`——
  这是两个 stage 在测试层的直接耦合，也验证了 Stage 4b 对 Stage 4a 的依赖。
- `test_invariant_terminator_short_circuit`：terminator 在 slot 2，扫描
  slot 0/1/2 共 3 次 read 即早退，不扫 slot 3-15。这个 IO 计数直接证明
  `exfat-alloc-slot-terminator-extends-run` 的实现正确。

全套 cmocka（含 Wave A 原 49 testpoints + Wave B Module 层 + Stage 4a/4b）
远端跑通，0 failure。

---

## 6. 落地清单

| Commit | 内容 |
|---|---|
| `024ad533` | Stage 4a-dentry_set_write 完整交付（spec / code / tests 三层）|
| `808bc906` | Stage 4b-alloc_dentry_slot 完整交付（spec / code / tests 三层）|

**DAG 节点**（`spec/exfat/.specfs.dag.json`）：

- `dentry_set_write`：depends_on=["dentry_iter", "fat_chain"]，
  9 invariants，2 exports，spec_approved + code_approved。
- `alloc_dentry_slot`：depends_on=["dentry_iter", "dentry_set_write"]，
  9 invariants，1 export，spec_approved + code_approved。

**spec/code 行数**：

| Stage | spec 行数 | 原始 .c 行数（含注释） |
|---|---|---|
| 4a dentry_set_write | 302 | 177（`exfat_dentry_set_write.c`，pre-consolidation）|
| 4b alloc_dentry_slot | 227 | 128（`exfat_alloc_dentry_slot.c`，pre-consolidation）|
| 合计 | 529 | 305 |
| 4a spec/code 比 | 302 / 177 ≈ **1.71** | （spec 大量篇幅在双 Case 集 + Refine Prompt Phase 2 跨簇分析）|
| 4b spec/code 比 | 227 / 128 ≈ **1.77** | （spec 详述终止符外推语义 + 4b' 扩展合同）|

> 两个 stage 的 spec/code 比均高于 Wave B Module 层均值（~1.5），原因是
> spec 需要显式分析跨簇 corner case（Phase 2 Refine Prompt）和 terminator
> 语义，而代码实现相对紧凑。

**测试文件行数**：

| 文件 | 行数 |
|---|---|
| `testsuites/unittest/exfat/test_dentry_set_write.c` | 507 |
| `testsuites/unittest/exfat/test_alloc_dentry_slot.c` | 440 |

---

## 7. 下一波预告

Stage 4a + 4b 构成"dentry 写口"的最底层。整个 inode 创建/删除的完整
VOP 链（Create / Mkdir / Unlink / Rmdir / Rename）需要在此基础上
再搭建多层：

| 待做 Stage | 简述 | 前置 |
|---|---|---|
| 4c（可选）| chksum compose helper | 现有 `exfat_calc_chksum16` 够用，可跳过 |
| 4d VfsExfatCreate | 文件创建 VOP | 4a + 4b + alloc_cluster |
| 4e VfsExfatMkdir | 目录创建 VOP（Wave 2 LTP mkdir 解锁的关键）| 4a + 4b + alloc_cluster |
| 4f VfsExfatUnlink | 文件删除 VOP（标记 dentry deleted + free_cluster）| 4a + free_cluster |
| 4g VfsExfatRmdir | 目录删除 VOP | 4f + dir-empty check |
| 4h VfsExfatRename | 跨目录重命名 VOP（sbi->s_lock 路径）| 4a + 4b + 4d + 4e |

这些完整 VOP 链及其跨目录事务语义将在后续 `docs/dev/exfat_namei.md`
（暂定命名）中记录。本文档明确标注的 **跳过范围**：rename / unlink /
create / mkdir 的完整 VOP 链实现不在 Stage 4a/4b 之内。

---

## 8. 引用

- `spec/exfat/dentry/exfat_dentry_set_write.spec` — Stage 4a 规范（302 行）
- `spec/exfat/dentry/exfat_alloc_dentry_slot.spec` — Stage 4b 规范（227 行）
- `fs/exfat/exfat_file.c:268-463` — Stage 4a/4b 代码（合并后当前位置）
- `testsuites/unittest/exfat/test_dentry_set_write.c` — 22 testpoints
- `testsuites/unittest/exfat/test_alloc_dentry_slot.c` — 21 testpoints
- `docs/dev/exfat_module.md §8` — 7 文件 Linux 风格物理组织（2026-05-05 重构）
- `docs/dev/exfat_mount.md` — Loop A / Loop B / 多层防御框架参考
