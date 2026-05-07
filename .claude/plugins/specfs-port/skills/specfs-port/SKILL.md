---
name: specfs-port
description: 凡用户提到将 Linux 内核文件系统（exFAT、F2FS、EROFS、BTRFS、ext4、NTFS 等）移植到 OpenHarmony LiteOS-A 内核，或在本仓库下新增/重写文件系统时，必须立即调用本技能；同时也是 specfs-port Claude Code 插件的伴生技能（自动随插件加载）。触发短语包括："port exFAT to LiteOS-A"、"添加 F2FS 支持"、"重写 FAT"、"把 Linux fs/<name> 翻译过来"、"SpecFS"、"sysspec"、"spec-first 文件系统"、"specfs-port"、"/specfs-port-spec"、"/specfs-port-code"，以及任何将 Linux FS 实现转写为 LiteOS-A 版本的请求；用户希望用 SpecFS 规范优先方法重构现有 LiteOS-A 文件系统（fs/fat、fs/jffs2）时同样触发。本技能驱动一条五阶段流水线（摄取 → 规范 → 映射 → 代码 → 接线），并绑定 P1.4（2026-05-07）后的四层防御 + 一层 HITL：Layer 1a Compile + Style【P1.4 起 LSP（clangd）→ style 顺序串行；style 由 P1.2 sibling 收回为 1a 内部子步骤】→ Layer 3 SpecEvaluator 自审【P1.4 提前到 Layer 2 之前；spec conformance only】→ Layer 2 Build + cmocka 单测执行 + QEMU smoke【P1.4 吞并 SpecValidator】→ Layer 4 用户审核；Layer T cmocka 测试派生由 P1.4 从 Loop A 移到 Loop B（在生成 C 代码之后立即生成对应测试）。两层回归套件（cmocka host + QEMU LTP smoke）通过 `tools/regress/run_all.sh` 在模块完结时仍可手动 / CI 触发。本技能取代过去的 liteos-fs-port 同名技能，与 specfs-port 插件**统一命名、协同工作**。
---

# specfs-port — 规范优先的 Linux→LiteOS-A 文件系统移植（plugin 伴生技能）

本技能由方法论 + 工程契约组成，与 `.claude/plugins/specfs-port/` 插件配套使用。

走规范这条弯路不是学术姿态：Linux 文件系统挂着十几年沉淀下来的 Linux 假设
（`struct page`、`struct buffer_head`、`kmem_cache_t`、RCU、基于 `bio` 的 IO、
dcache）。把这些逐行搬到 LiteOS-A 几乎一定会得到一个能编译、会泄漏、行为
微妙错乱的产物。规范化把 Linux 味道剥掉，强迫开发者用前置/后置条件描述函数
**做什么**，再围绕 LiteOS-A 原语把"做什么"落成"怎么做"。

## 与 specfs-port 插件的关系

| 物件 | 路径 | 所有者 |
|---|---|---|
| **本技能（SKILL.md）** | `.claude/skills/specfs-port/SKILL.md` | 方法论 |
| **本技能 references/** | `.claude/skills/specfs-port/references/` | 详细操作手册 |
| **插件（manifest + 服务）** | `.claude/plugins/specfs-port/` | 流程编排 + MCP 工具 |
| **prompts** | `.claude/plugins/specfs-port/prompts/` | LLM 提示拼装 |
| **commands** | `.claude/plugins/specfs-port/commands/` | `/specfs-port-spec`、`/specfs-port-code` |
| **server** | `.claude/plugins/specfs-port/server/` | Python MCP server |

**自动加载**：项目根 `.claude/skills/specfs-port/` 与 `.claude/plugins/specfs-port/`
同时存在，Claude Code 同步加载。插件 `plugin.json::bundles.skills` 声明依赖，
**插件不再依赖** `liteos-fs-port` 旧技能。

## 用户必须给出（或必须确认）的输入

- **目标 FS 名**（如 `exfat`）。所有路径据此推导——LiteOS-A 端的 `fs/exfat/`、
 `LOSCFG_FS_EXFAT` Kconfig、`g_exfatVops` 表等。
- **Linux 源码根目录**——默认 `/Users/kissa/Codebase/linux/fs/<name>/`。开始前需确认存在。
- **范围**——一次性做读写，还是先只读？多数移植应先落地只读路径，第二轮再补写路径。

任何一项缺失就追问一次（"ask-first"），得到后立即推进。
插件 `prompts/ask_first_rules.md` 是这一规则的运行时副本。

## 触发但不适用的情形

- 纯 Linux 问题（"ext4 日志怎么工作？"）。直接回答即可，不涉及移植。
- 修复已经移植好的 FS 中的 bug（修 bug 即可，不需要重建规范树）。
- 用户想要的是用户态 FUSE 文件系统——更接近 `~/workspace/projects/specfs/` 的上游
 SpecFS 流水线，引导其去那边。

---

## 五阶段流水线

```
[阶段 1] 摄取 Linux 源码 ─┐
[阶段 2] 规范 SYSSPEC 树 ─┤── 产物落在 spec/<name>/
[阶段 3] 映射 Linux→LiteOS │ （从规范驱动，不从代码驱动）
[阶段 4] 代码 fs/<name>/ │
[阶段 5] 接线 构建 + 回归 ─┘
```

按顺序执行。**绝不允许** 规范未经评审就进入阶段 4。规范是事实来源。

### 阶段 1 — 摄取 Linux 源码

产出一页 Linux 上游 FS 速览：盘上布局（`*_raw.h`）+ 四张操作表（super/inode/file/
address_space）+ 子系统依赖（page cache / buffer head / bio / jbd2 / kmem_cache /
NLS / crypto / RCU——这些在 LiteOS-A 都不存在）+ 规范分层（FAT 家族默认：interface/
inode/ file/ path/ bitmap/ util/）。**用户拍板分层后才进阶段 2**。

### 阶段 2 — 抽取 SYSSPEC 规范树

产物 `spec/<name>/`：`common.header` + 各模块 `.header` + 每函数 `.spec`（四段格式
[PROMPT][RELY][GUARANTEE][SPECIFICATION] + 可选 `## Refine Prompt`）。完整格式见
`references/specfs-format.md`。

质量门槛：
- 规范 LOC < 生成代码 LOC（SpecFS 生产力命题）；
- `[RELY]` 必须列具体 LiteOS-A 函数签名（不能"使用互斥锁"这种泛指）；
- `[GUARANTEE]` 必须紧跟一段调用约定注释（持锁状态、返回值含义、副作用）；
- 加锁是单独的 `## Refine Prompt` 轮次，不是一轮的关注点。

### 阶段 3 — Linux → LiteOS-A 映射

完整映射见 `references/liteos-vfs-mapping.md`。要点：
- `super_block + super_operations` → `Mount + MountOps`
- `inode + dentry` → 单一 `Vnode`（path_cache 维护名字）
- `address_space_operations` → 直接调 bcache
- `kmalloc` → `LOS_MemAlloc(m_aucSysMem0, sz)` / `zalloc`
- `mutex_lock` / `spin_lock` → `LOS_MuxLock` / `LOS_SpinLock`
- `submit_bio` / `bread` → `los_disk_read` / `los_part_read`（分区相对读用 part 版）
- `strncpy / memcpy` → **`strncpy_s / memcpy_s`** libsec（华为安全编码硬约束）
- `printk` → `PRINT_ERR / PRINT_INFO / PRINTK`

阶段 3 必须主动检查"格式兼容性陷阱"（CRC 算法 / 字节序 / 字符集 / packed 对齐）。

### 阶段 4 — 由规范生成代码

风格细节见 `references/liteos-fs-style.md`。硬约束：
- 版权头 BSD-3-Clause 华为 Device 模板（**不要**沿用 Linux GPL 头）
- `#ifdef LOSCFG_FS_<NAME>` 包每个翻译单元
- 命名：VFS 回调 `Vfs<Name><Op>`、私有辅助 `<name>_<verb>_<noun>`、
 盘上结构 `struct exfat_dentry` 沿用上游
- 字符串/内存：`_s` libsec 变体
- 加锁：可能睡眠用 `LOS_MuxLock`；否则 `LOS_SpinLock`。**绝不**在持自旋锁时睡眠
- 错误码：VFS 边界返回负 POSIX errno；内部允许正值 errno + goto-stack

具体可工作样例见 `references/exfat-walkthrough.md`。

### 阶段 5 — 构建 + 两层回归

**构建接线**：`fs/<name>/{BUILD.gn, Makefile, Kconfig}` + 顶层 `fs/{BUILD.gn, Kconfig}`
+ `tools/build/mk/los_config.mk` + 产品 config。

**注册胶水**：LiteOS-A 不用 `LOS_MODULE_INIT`。用链接器表：
```c
struct MountOps g_<name>Mops = { .Mount, .Unmount, .Statfs, .Sync };
FSMAP_ENTRY(<name>_fsmap, "<name>", g_<name>Mops, FALSE, TRUE);
```

**两层回归测试**：
| 层 | 跑在哪 | 入口 | 详细操作手册 |
|---|---|---|---|
| **A: cmocka host** | dev/CI host (Linux x86_64) | `make` in `testsuites/unittest/<name>_host/` | `references/cmocka-host-harness.md` |
| **B: QEMU LTP smoke** | QEMU + LiteOS-A | `tools/regress/qemu_<name>_run.sh` | `references/ltp-qemu-regression.md` |

**聚合**：`tools/regress/run_all.sh` 顺序跑两层，时间戳化报告写到 `docs/<name>_regression_<ts>.md`，
symlink 到 `_latest.md`。退出码 0/1/2 = 全过 / 有失败 / panic-or-hang。

---

## 防御层次（plugin 与 skill 共享的契约）

P1.4 (2026-05-07) 拓扑：style 由 P1.2 sibling 收回为 Layer 1a 内部第二个串行子步骤；
SpecEval 提前到 Layer 2 之前；Layer 2 吞并 SpecValidator（build + cmocka exec + QEMU smoke）；
Layer T cmocka 测试派生由 Loop A 移到 Loop B（生成 C 代码之后立即生成对应测试）。

```
Loop A: 仅生成 / 审 / 批 spec, 不再触发测试派生
Loop B (per stage):
  Step 3   生成 C 代码
  Step 3a  Layer T cmocka 测试派生（≤ 3 轮 self-eval）
  Step 4   Layer 1a (sequential)：4.1 LSP compile (≤ 4 轮) → 4.2 style audit (≤ 5 轮)
  Step 5   Layer 3 SpecEvaluator (≤ 8 轮，spec conformance only)
  Step 6   Layer 2 (single budget ≤ 3 轮): 6.1 build → 6.2 cmocka exec → 6.3 QEMU smoke
  Step 7   Layer 4 用户审核（一并审 code + test）
模块完结：tools/regress/run_all.sh 仍可手动 / CI 触发（per-stage Layer 2 已覆盖）
```

**Layer 1a.2 style audit**（P1.4 收回原 Layer 1b/Layer S）从 `prompts/style_rules.md`
读取 LiteOS-A 风格 canon（≥ 17 项硬规则：libsec / 内存 / 锁 / 错误码符号 /
FS 注册 / 命名 / ... 完整列表见 style_rules.md）。模板
`.claude/plugins/specfs-port/prompts/style_audit.md`，输出 JSON
`{is_good, score, summary, violations[]}`。`is_good=true && score≥80` 进 Step 5
SpecEval；否则注入 violations 重生（≤ 5 轮）。clang-format dry-run 作为
advisory hint 喂入 `[AUTO CHECKS]` 段，不当硬门。**与 Layer 1a.1 LSP 同 retry
budget 完全分离**——LSP 失败只消耗 LSP 预算，style 失败只消耗 style 预算。

**Layer 3** SpecEvaluator (P1.4 提前到 Layer 2 之前；P1.2 起 spec conformance only)：
6 类 spec 偏差—— 函数签名 ≠ [GUARANTEE] / Pre-Post-Invariant 违反 / Locking
标注漂移于 `## Refine Prompt` / 幻觉 helper（不在 [RELY]/[PRIOR CODE INTERFACE]）/
Case 分支与 [SPECIFICATION] 不符 / 跳过 System Algorithm 的 phase 顺序。
模板 `.claude/plugins/specfs-port/prompts/speceval.md`。**风格不归此层**——
看到风格违例本层会作为 false positive 跳过。`is_good=true` 进 Layer 2，
否则注入 comments 重生（≤ 8 轮，paper 设定）。

**Layer 2** (P1.4 吞并 SpecValidator，单一 retry budget ≤ 3 轮)：
6.1 build (`run_build_kernel`) → 6.2 cmocka exec (host 单测，覆盖刚生成的
test_<stage>.c + 历史已批准测试) → 6.3 QEMU smoke (`run_qemu_smoke`)。任一
子步骤失败统一注入 `layer="build|qemu"` source 区分，回 Step 3 重生代码（或 Step 3a
重生测试，若 cmocka failure 来自新生成的测点）。原 Step 9 holistic SpecValidator
被 Layer 2 完全替代；`tools/regress/run_all.sh` 保留为模块完结时的手动 / CI
聚合工具。

**Layer T**（v0.3.4 加；P1.4 从 Loop A 移到 Loop B 的 Step 3a）紧接 Step 3
代码生成之后，由 spec [SPECIFICATION] Cases + Invariants + 刚生成的代码符号
派生 `testsuites/unittest/<name>/test_<stage>.c.draft`：每个 Case 至少一个测点、
每个可单测的 Invariant 一个测点；用 `mock_disk_*` + `<name>_image_builder_*`
原语，禁止依赖完整 VFS（VnodeAlloc / VfsHashInsert 等归 Layer 2.3 QEMU smoke 覆盖）。
模板 `.claude/plugins/specfs-port/prompts/unittest_gen.md`。
通过 `is_good=true && score≥80` → 等待 Step 7 与代码同审批；否则 `test_gen_refine`
重生（≤ 3 轮，超出意味 spec [SPECIFICATION] 写得不够清楚，应回 Loop A 而不是
在 Layer T 里磨）。MCP 工具：`test_gen_{start,submit,refine,approve}`。
`test_gen_approve` 写 `.c` + 自动接 `Makefile::HARNESS_SRCS` 与 `main.c::run_suite()`。

报告范例：`docs/exfat_speceval.md`（Layer 3 spec conformance，7/7 stage 通过）+
`docs/exfat_style_audit.md`（Layer 1a.2 风格，6/7 stage 通过；v0.3 期间产物，
P1.2/P1.4 拓扑下结论仍适用）。

---

## 移植"开局"

- `/specfs-port`：查看 DAG 状态、推荐下一步。
- `/specfs-port-spec <linux-path> <stage>`：Loop A，从 Linux 源码起草 SYSSPEC spec。
- `/specfs-port-code <spec-path>`：Loop B，从已批准的 spec 生成 LiteOS-A C 代码。

可选 flag：
- `--speceval-off`（v0.2）：关闭 Layer 3 自审（默认 ON）
- `--style-off`（v0.3 引入；P1.2 sibling；P1.4 收回为 Layer 1a.2）：关闭风格子步骤（默认 ON）
- `--test-off`（v0.3.4；P1.4 同时关掉 Layer T 派生与 Layer 2.2 cmocka exec）：默认 ON。
  仅当新代码无 host-testable 表面时使用（例如纯 QEMU LTP 覆盖的写路径）；用了就在
  最终报告里显式记 "测试覆盖：跳过（gap）" 让后审可补
- `--no-build`：跳过 Layer 2 整层（不 build / 不跑 cmocka / 不跑 QEMU）
- `--no-regress`：跳过模块完结时的 `tools/regress/run_all.sh` 提醒
- `--prompt-override <file>`：跳过 spec 派生的提示拼装

每一轮完成后，DAG 节点（`spec/<name>/.specfs.dag.json`）记录 spec/code 的双层
批准时间戳；下一阶段拉取 inherited invariants 做跨阶段一致性检查。

---

## 常见陷阱（速查）

- **页缓存惯性**：Linux 用 `struct page`/`buffer_head`，LiteOS-A 用 bcache。映射前
 先把 FS 用"逻辑偏移处的缓冲区"重新表述。
- **跳过 Refine Prompt 锁轮次**：第一轮就把锁糅进规范，后续无法仅凭规范推导锁序错误。
- **照搬 `struct buffer_head` 回调名**：Linux 操作表与 `VnodeOps` 不是 1:1，按
 `references/liteos-vfs-mapping.md` 走。
- **忘了 libsec**：`strcpy`/`memcpy` 编译过但违反华为安全编码方针。
- **引入 Linux GPL 头**：LiteOS-A 全树 BSD-3-Clause，不许带署名注释黏贴 GPL 文件。
- **单一巨大的 `vfs_<name>.c`**：VFS 回调要分散到 `interface/`，注册胶水保持薄。
- **混用 `los_disk_read` 与 `los_part_read`**：两者索引来自不同命名空间，混用导致
 "mutex lock failed"。
- **`g_<fs>Vops = { 0 }` 全 NULL 会 umount panic**：见 `references/fs-debug-recipe.md`
 "教训 #1"。即使 只做 mount，`vop->Reclaim` 也**必须**是真实函数。
- **FS Unmount 不可触碰 root vnode**：照抄 fatfs_umount，详见 `references/fs-debug-recipe.md`
 "教训 #2"。
- **静态 LTP 加载失败**：用 OHOS clang 动态链才行，详见 `references/ltp-qemu-regression.md`。

## FS 调试硬约束（用户明确指令）

修 FS bug 时**坚守 fs/<name>/ 内部**。`fs/vfs/`、`kernel/`、`syscall/`、
`drivers/block/disk/` 是公共层——**禁止**未经用户许可改这些路径。理由：
其它 FS（fat、jffs2）能跑就证明公共层是好的；问题一定在新加的 FS 自己。

诊断流程 + 用维测日志定位 + fatfs 参照 + 删临时 print：见 `references/fs-debug-recipe.md`。

---

## 完成后向用户输出的报告

- 规范树体量：文件数、规范总 LOC。
- 生成代码：文件数、代码总 LOC，spec/code 比例（用作 SpecFS 生产力命题的合理性自检）。
- 构建状态：`make build` 结果（per-stage Layer 2.1）。
- Layer 2.2 cmocka exec：通过 stage 数 / 总数（per-stage 已运行）。
- Layer 2.3 QEMU smoke：通过 stage 数 / 总数。
- 回归状态：`tools/regress/run_all.sh` 退出码 + cmocka pass/fail + LTP pass/fail（模块完结时手动跑）。
- Layer 3 SpecEval 自审：`is_good=true` 的 stage 数 / 总数。
- Layer 1a.2 风格审计：通过 stage 数 / 总数 + 主要 hard 违例。
- **测试覆盖（v0.3.4 加，P1.4 起在 Loop B 里同步生成）：N/M** — N 个 stage 通过
  Layer T 落了 cmocka 测试 / 总 M 个；跳过的 stage 列出名字 + 跳过原因
  （`--test-off` 还是 `harness_dir` 未就绪）。
- 标注 "v2 删除" 的开放映射项（如日志、fscrypt），便于规划后续。

---

## 参考文档（按需读取）

| 文件 | 内容 |
|---|---|
| `references/specfs-format.md` | SYSSPEC 规范格式与带注解示例 |
| `references/liteos-vfs-mapping.md` | Linux↔LiteOS-A 类型/API/锁速查表 |
| `references/liteos-fs-style.md` | 版权头、命名、构建接线模板 |
| `references/exfat-walkthrough.md` | 端到端样例（exfat_alloc_cluster 一例） |
| `references/cmocka-host-harness.md` | Layer A 单测目录骨架、stub 设计、加新 stage 测试 4 步 |
| `references/ltp-qemu-regression.md` | Layer B LTP 交叉编译（动态链接）、QEMU runner 契约、排查矩阵 |
| `references/fs-debug-recipe.md` | FS bug 调试硬约束、维测日志方法、Reclaim hook 教训、4 轮 mount/umount 验证 |

外部参考：
- `.claude/plugins/specfs-port/DESIGN.md`：插件实现规格（DAG / MCP 工具表 / prompts 拼装）。
- `.claude/plugins/specfs-port/CHANGELOG.md`：插件版本演进（v0.1→v0.2 加 SpecEval 默认 ON→
 v0.3 加 Layer S（P1.2 重定位为 Layer 1b sibling；P1.4 收回为 Layer 1a.2 串行子步骤）→
 v0.3.1 修 umount panic + LTP dynamic linking→v0.3.2 加 cmocka 测试派生
 prompt 模板（**advertised but not implemented** — Wave A 9 个 stage 在该期累积测试欠债）→
 v0.3.3 合并 Layer 0 LSP 与 Layer 1 gcc→v0.3.4 真正接通 Layer T 服务端实现→
 P1.3 去掉 Layer 1a 的 gcc 兜底→P1.4 总体重排：style→1a.2、SpecEval 提前到 build 之前、
 Layer 2 吞并 SpecValidator、Layer T 从 Loop A 移到 Loop B）。
- `docs/dev/exfat_mount.md`：mount 端到端实战记录（含本技能与插件协作的真实流程）。
- `docs/exfat_speceval.md`：Layer 3 SpecEvaluator 自审报告范例。
- `docs/exfat_style_audit.md`：Layer 1a.2 风格审计报告范例（v0.3 期间产物，文件名沿用 "style_audit"）。
- `tools/regress/README.md`：两层回归套件运行手册（模块完结手动 / CI 用；per-stage Layer 2 已包含同样链路）。

## 与旧 liteos-fs-port 技能的关系

旧技能 `.claude/skills/liteos-fs-port/` 是本技能的前身。本技能在以下方面是
上位替代：
1. 命名与插件统一为 `specfs-port`；
2. 显式纳入风格审计（旧 Layer S → P1.2 Layer 1b sibling → P1.4 Layer 1a.2 串行子步骤）+ Layer 3 SpecEvaluator 自审；
3. 显式纳入两层回归套件契约（cmocka + QEMU LTP）；
4. 编译/调试细节移到 references/，SKILL.md 只留方法论；
5. 显式说明与插件的伴生关系。

旧技能保留用于历史溯源与外部引用兼容，新工作应优先使用本技能。
