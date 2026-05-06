# specfs-port 模块/Spec 预算（P3.1 + codex 校准）

论文 §4.2 用 ≤500 LOC/模块的预算（FUSE 用户态 FS）。LiteOS-A 内核 FS 因
工程约束（libsec、FSMAP、partition addressing、BSD-3 license header）较 FUSE
更密集，按 codex 审计校准放宽。

## 硬预算

| 单位 | 上限 | 备注 |
|---|---|---|
| 模块（merged stage 集合） | **800 LOC** generated C | 论文 500 + 30%–60% LiteOS 工程开销 |
| 单函数 spec | **150 LOC** | 论文典型 80–200；超过即提示拆 helper |
| 单模块 spec 总和 | **400 LOC** | spec/code 比 ≤ 0.5 的目标线 |
| 单 stage 内 [GUARANTEE] 函数数 | ≤ 7 | 8+ 即提示拆模块 |

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
