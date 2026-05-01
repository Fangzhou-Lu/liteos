# docs/

LiteOS-A 文件系统移植与 specfs-port 插件相关文档。

## 谁读什么

| 角色 | 推荐顺序 |
|---|---|
| **新手 / 想理解整体架构** | 1. `specfs_plugin_design.md` §1-3 → 2. `dev/exfat_mount.md` §1-2 |
| **想用插件移植新 FS** | 1. `specfs_plugin_design.md` §6 → 2. `dev/exfat_mount.md`（参照实例）→ 3. `../CLAUDE.md` §QEMU recipe |
| **维护 / 扩展插件** | 1. `specfs_plugin_design.md` §4-5 → 2. `.claude/plugins/specfs-port/DESIGN.md` → 3. server/ 源码 |
| **接 exFAT 后续 工作** | 1. `dev/exfat_mount.md`（背景）→ 2. `exfat_mount_invariants.md`（契约）→ 3. `exfat_roadmap.md`（计划）|
| **审 PR / debug 已合并代码** | 1. `exfat_mount_invariants.md`（必看）→ 2. `dev/exfat_mount.md` §4-8（已知边界 + reconcile 历史） |

## 文档清单

| 文件 | 行数 | 主题 |
|---|---|---|
| [specfs_plugin_design.md](specfs_plugin_design.md) | 529 | specfs-port 插件设计、与论文 8 项差异、25 个 MCP 工具、5 层防御、文件布局 |
| [dev/exfat_mount.md](dev/exfat_mount.md) | 538 | exFAT mount 端到端开发实录：ask-first 决策、跨阶段 reconcile、构建接线、QEMU 验收（开发记录类，不计入设计文档主线） |
| [exfat_mount_invariants.md](exfat_mount_invariants.md) | — | mount 全部 36 个 invariants 的契约说明、动机、违反检测方法 |
| [exfat_roadmap.md](exfat_roadmap.md) | 341 | 后续读路径计划：9 个新 stage 依赖图 + 详细 [GUARANTEE] + 估算 + 风险 |
| [exfat_speceval.md](exfat_speceval.md) | — | Layer 3 SpecEvaluator 自审报告（7/7 stage 通过，36 invariants 逐条比对） |
| [exfat_style_audit.md](exfat_style_audit.md) | — | Layer S 编码风格审计报告（6 维度，6/7 stage 通过，super.c 长函数 hard 违例） |
| `test/exfat_regression_latest.md` | — | 最新一次回归测试报告（cmocka host + QEMU LTP smoke）；symlink，每次 run_all.sh 刷新。`docs/test/` 目录已加入 `.gitignore`，仅本地留档 |

## 仓库内 spec 与代码定位

| 文件夹 | 内容 |
|---|---|
| `spec/exfat/` | SYSSPEC 规范树（人审核 + plugin 起草，frozen） |
| `spec/exfat/.specfs.dag.json` | DAG 状态文件，记录 7 个 stage 的双层批准时间戳 |
| `fs/exfat/` | LLM 生成的 LiteOS-A 实现 |
| `.claude/plugins/specfs-port/` | 插件本体（manifest + prompts + commands + Python MCP server） |

## 与上游 CLAUDE.md 的关系

- [`../CLAUDE.md`](../CLAUDE.md)：LiteOS-A 工程通用上下文（构建系统、Kconfig、
 QEMU 端到端 testing recipe、coding rules）。**新人首先读它**。
- `docs/`：specfs-port 插件特定知识——只在你接触 spec-driven FS 移植工作时才读。

## 文档更新规约

1. **每个 v 完成后追加章节** — 不删旧章节，只标 deprecated。
2. **invariant ID 不变** — `exfat_mount_invariants.md` 的 ID 一旦发布就不能改名，
 后续 spec_gen_refine 可加新 invariant，不能改旧 ID。
3. **真实数据替代估算** — 估算值（roadmap §4）在 后续完成后用实测数替换，
 保留估算列做对比。

## 引用约定

文档内引用其他文档用**相对路径**（如 `[link](specfs_plugin_design.md)`），
引用代码用**仓库根的相对路径**（如 `fs/exfat/exfat_super.c:152`）。
