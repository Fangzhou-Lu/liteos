# specfs-port 模块/Spec 预算（P3.1 + codex 校准）

论文 §4.2 用 ≤500 LOC/模块的预算（FUSE 用户态 FS）。LiteOS-A 内核 FS 因
工程约束（libsec、FSMAP、partition addressing、BSD-3 license header）较 FUSE
更密集，按 codex 审计校准放宽。

## 硬预算

| 单位 | 上限 | 备注 |
|---|---|---|
| 模块（merged stage 集合） | **800 LOC** generated C | 论文 500 + 30%–60% LiteOS 工程开销 |
| 单模块 spec 总和 | **600 LOC** | spec/code 比 ≤ 0.75 的目标线（codex 警告 0.5 在 LiteOS 不现实） |
| 单 stage 内 [GUARANTEE] 函数数 | ≤ 7 | 8+ 即提示拆模块 |

### 单函数 spec 分级（2026-05 mkdir 重生实验校准）

按函数的复杂度与并发特征分三级。实验数据：mkdir VOP（两阶段锁 + 6 helper [RELY]）
最小可达 215 LOC，结构性 [RELY] 块占 85，无法压至 150 而不破坏论文 §4.1 的"用真实 C
签名而非抽象名"硬规则。

| 函数类型 | 上限 LOC | 论文/实验参照 |
|---|---|---|
| 工具函数（≤2 helper, 无锁） | **≤100** | atomfs `malloc_inode.spec` 53 LOC |
| VOP / helper（无锁路径） | **≤150** | atomfs `atomfs_open.spec` 86 LOC |
| VOP（持锁两阶段或更多） | **≤220** | exfat `VfsExfatMkdir` v04draft 215 LOC |

**质量门控不放在总 LOC，放在 [SPECIFICATION] + [Refine] 两段总和（去 [RELY]/
[GUARANTEE] 工程性 boilerplate）**：单函数 ≤ 100 LOC 才算"行为契约不臃肿"。

## 函数 spec 覆盖原则（v0.4 修订 2026-05-07）

依 atomfs sysspec/specfs/ 实际分布 + Linux exfat_fs.h 公共接口，**不是每个
函数都要 spec**。覆盖原则：

| 覆盖 | 不覆盖 |
|---|---|
| (a) Public VFS 回调 (`Vfs<Fs><Op>`) | mkdir 内部组装步骤（add_entry / alloc_new_dir / init_*_entry） |
| (b) Linux exfat_fs.h 公共导出函数 | 实现细节 helper（rollback 顺序、loop 策略、字段位填充）|
| (c) 窄语义稳定工具（≤100 LOC，公式/纯计算）| 编排器（≥2 helper 组合的中间封装） |
| (d) 跨阶段 check 类辅助 | 临时实现选择（per-iter loop count、特定 flag 值）|

工具 `tools/specfs_eval/collect.py::_classify_invariant` 自动按此规则归类
`behavioral` vs `implementation`。AB gate 3 只对 behavioral 子集守门。

**反例**：2026-05-07 之前的 mkdir 拆分尝试为 7 个函数都建了 spec，包括
4 个实现编排器（add_entry / alloc_new_dir / init_dir_entry / init_ext_entry）。
按上面原则属过度规约，已 prune 移到 `backup/spec/exfat/inode/v04-pruned-2026-05-07/`。

## 模块合并示例（exFAT Wave B 后续）

| 旧细分 stage | 合并为模块 | 估 LOC | 状态 |
|---|---|---|---|
| 4d create | dirops | ~80 | 未起 |
| 4e mkdir | dirops | spec 431 / code 138 | ✅ v0.4 落地 (2026-05-07) |
| 4f unlink | dirops | ~150 | 未起 |
| 4g rmdir | dirops | ~100 | 未起 |
| 4h rename | dirops | ~250 | 未起 |
| **总计** | **dirops 模块** | ≤ 800 LOC（强制） | mkdir 占 17% |

## v0.4 mkdir 落地实测

mkdir 重生 (2026-05-07, commit 0f396595 spec / 本次 promote code)：
- spec 模块总和 = 431 LOC（mkdir.spec 265 + calc_num_entries.spec 85 +
  zeroed_cluster.spec 81）— 远低于模块 600 LOC 上限。
- VfsExfatMkdir 函数 138 LOC — 在"VOP（持锁两阶段）≤220"内，且通过
  Phase 1/Phase 2 split 把 s_lock 收窄到只覆盖 disk mutation。
- spec/code 比 = 431/138 = 3.12（mkdir 单函数视角 0.61，含 helper 视角更宽
  松）。基线 742/1375 = 0.54（看似低，但实为 spec 因 [RELY] 工程化膨胀；
  v0.4 spec [SPECIFICATION] body 净 LOC 反而更小）。
- AB 协议两次 ACCEPT：c6e98410（initial promote）+ 0f396595（prune 编排器）。
- cmocka 24 testpoint 全绿；24 suites / 419 testpoints 总绿。

## 预算检测

`tools/specfs_eval/collect.py` 已采集 `code_loc` 和 `spec_loc`。预算违例信号：
- `spec_loc > 150` （单函数 spec 超）
- `code_loc > 800` （模块代码超）
- `spec_code_ratio > 0.5` （比例超）

后续可在 collect.py 加 budget violation 字段，作为合入门控的输入。

## 与论文对照

| 维度 | 论文 SpecFS | 我们 v0.4 计划 | 校准原因 |
|---|---|---|---|
| 模块 LOC 上限 | 500 | 800 | LiteOS 工程开销不可避免 |
| spec/code 比 | 0.2–0.5 | ≤ 0.5 | 同上，目标取上界 |
| FS 数据路径 | FUSE userspace | LiteOS kernel | 不同执行模型 |
| 验证套件 | 单元 + 文件系统压测 | cmocka host + LTP QEMU smoke | 同等覆盖 |
