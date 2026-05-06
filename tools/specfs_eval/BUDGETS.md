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

## 模块合并示例（exFAT Wave B 后续）

| 旧细分 stage | 合并为模块 | 估 LOC |
|---|---|---|
| 4d create | dirops | ~80 |
| 4e mkdir | dirops | ~250（已 generated 1375 — **超预算**，待 P3.1 重写） |
| 4f unlink | dirops | ~150 |
| 4g rmdir | dirops | ~100 |
| 4h rename | dirops | ~250 |
| **总计** | **dirops 模块** | ≤ 800 LOC（强制） |

## 既有违例

mkdir 当前 spec 742 LOC + code 1375 LOC（生成于 2026-05-05）— spec/code 1.16，
显著超预算。F1 + P3.1 一并应用后重生时压到 spec ≤150 + code ≤250。

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
