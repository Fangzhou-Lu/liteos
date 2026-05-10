---
name: specfs-port
description: 凡用户提到将 Linux 内核文件系统（exFAT、F2FS、EROFS、BTRFS、ext4、NTFS 等）移植到 OpenHarmony LiteOS-A 内核，或在本仓库下新增/重写文件系统时，必须立即调用本技能；同时也是 specfs-port Claude Code 插件的伴生技能（自动随插件加载）。触发短语包括："port exFAT to LiteOS-A"、"添加 F2FS 支持"、"重写 FAT"、"把 Linux fs/<name> 翻译过来"、"SpecFS"、"sysspec"、"spec-first 文件系统"、"specfs-port"、"/specfs-port-spec"、"/specfs-port-code"，以及任何将 Linux FS 实现转写为 LiteOS-A 版本的请求；用户希望用 SpecFS 规范优先方法重构现有 LiteOS-A 文件系统（fs/fat、fs/jffs2）时同样触发。本技能驱动一条五阶段流水线（摄取 → 规范 → 映射 → 代码 → 接线），并定义 Loop code 的六步流水线（按执行顺序）：Step 1 codegen → Step 2 LSP + style + 内核 build → Step 3 cmocka 测试生成 + 编译 → Step 4 spec/code audit（合并 spec conformance 与 Linux 异构审计）→ Step 5 cmocka exec + QEMU smoke → Step 6 用户审核。两层回归套件 Wave A (cmocka host) + Wave B (QEMU LTP smoke) 通过 `tools/regress/run_all.sh` 在模块完结时手动 / CI 触发。
---

# specfs-port — 规范优先的 Linux→LiteOS-A 文件系统移植（plugin 伴生技能）

本技能是 `.claude/plugins/specfs-port/` 插件的方法论与工程契约伴生件。

走规范这条弯路不是学术姿态：Linux 文件系统挂着多年沉淀下来的 Linux 假设
（`struct page`、`struct buffer_head`、`kmem_cache_t`、RCU、基于 `bio` 的 IO、
dcache）。把这些逐行搬到 LiteOS-A 几乎一定会得到一个能编译、会泄漏、行为
微妙错乱的产物。规范化把 Linux 味道剥掉，强迫开发者用前置/后置条件描述函数
**做什么**，再围绕 LiteOS-A 原语把"做什么"落成"怎么做"。

> **历史/版本演进** — 见 `../../CHANGELOG.md`。本文件只描述当前生效的契约。

## 与 specfs-port 插件的关系

| 物件 | 相对插件根的路径 | 所有者 |
|---|---|---|
| **本技能（SKILL.md）** | `skills/specfs-port/SKILL.md` | 方法论 |
| **本技能 references/** | `skills/specfs-port/references/` | 详细操作手册 |
| **manifest** | `.claude-plugin/plugin.json` | 插件元数据 |
| **prompts** | `prompts/` | LLM 提示拼装 |
| **commands** | `commands/` | `/specfs-port`、`/specfs-port-spec`、`/specfs-port-code` |
| **server** | `server/` | Python MCP server |
| **MCP 启动配置** | `.mcp.json` | uv + `${CLAUDE_PLUGIN_ROOT}` |

**自动加载**：技能位于插件目录 `skills/specfs-port/SKILL.md`，遵循 Claude Code
plugin reference §"Skills" 的标准目录约定（`skills/<name>/SKILL.md` 自动发现）。
插件不依赖 `liteos-fs-port` 旧技能。

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

## 五阶段流水线 / Five-stage pipeline

```
[阶段 1] 摄取 Linux 源码 ─┐
[阶段 2] 规范 SYSSPEC 树 ─┤── 产物落在 spec/<name>/
[阶段 3] 映射 Linux→LiteOS │ （从规范驱动，不从代码驱动）
[阶段 4] 代码 fs/<name>/   │
[阶段 5] 接线 构建 + 回归 ─┘
```

按顺序执行。**绝不允许** 规范未经评审就进入阶段 4。规范是事实来源。
评审分两层：先由异构 LLM 审计器做只读证据审计，再由用户做最终 HITL 批准。

### 阶段 1 — 摄取 Linux 源码

产出一页 Linux 上游 FS 速览：盘上布局（`*_raw.h`）+ 四张操作表（super/inode/file/
address_space）+ 子系统依赖（page cache / buffer head / bio / jbd2 / kmem_cache /
NLS / crypto / RCU——这些在 LiteOS-A 都不存在）+ 规范分层（FAT 家族默认：interface/
inode/ file/ path/ bitmap/ util/）。**用户拍板分层后才进阶段 2**。

### 阶段 2 — 抽取 SYSSPEC 规范树

产物 `spec/<name>/`：`common.header` + 各模块 `.header` + 每函数 `.spec`（四段格式
`[PROMPT][RELY][GUARANTEE][SPECIFICATION]` + 可选 `## Refine Prompt`）。完整格式见
[references/specfs-format.md](references/specfs-format.md)。

**质量门槛 / Quality gates**：
- 规范 LoC < 生成代码 LoC（SpecFS 生产力命题）；
- `[RELY]` 必须列具体 LiteOS-A 函数签名（不能"使用互斥锁"这种泛指）；
- `[GUARANTEE]` 必须紧跟一段调用约定注释（返回值含义、副作用；**持锁
  状态留到 Phase 2**）；
- **`[SPECIFICATION]` 必须包含 `**System Algorithm**` 块**——项目策略，
  比论文 §4.1 严格（论文 Level 1 helper 不要求 SA，本项目要求每条新生成
  spec 都有；长度按复杂度伸缩，trivial helper 2–4 个 phase bullet 即可，
  Level 3 完整 Goal / Algorithm / Pre-Post / Error Handling 分 phase）。
  **Grandfather 条款**：本规则生效前已批准的 spec 豁免，无需 SpecFine 回填。
- **异构审计门**：主生成模型（优先 Claude Opus / 最强可用模型）抽取规范后，必须交给
  异构审计器（优先 GPT-family）对照 Linux 源码审计。审计器只输出 JSON finding，
  不写文件、不批准、不改 prompt；每条 finding 必须含 Linux/spec anchor、claim、
  evidence、recommendation、confidence。生成器最多按审计结果 refine 2 轮；
  仍无法一致时交给用户确认 scope / split / cancel。
- **两阶段 spec 写法**（paper §4.1 + delalloc.spec + exfat_mount.spec）——
  spec 必须把"功能"和"加锁"切成两段同输出：
  - Phase 1（`## First Prompt` 之后到 `## Refine Prompt` 之前）：`[RELY] +
    [GUARANTEE] + [SPECIFICATION]`，**只描述功能契约**。Pre/Post-Condition
    禁止出现"持有 X 锁 / 获取 X / 释放 X"。
  - Phase 2（`## Refine Prompt` 之后）：仅追加 lock state pre/post +
    初始化顺序约束 + deadlock note。**不重复 Phase 1 的 Cases**，
    **不改 [GUARANTEE] 签名**。
  - 触发条件（任一命中 → 必须两阶段）：① Linux 源文件出现 `mutex_lock` /
    `spin_lock*` / `down_*` / `*_lock_irqsave`；② 自己的 `[RELY]` 会
    forward-declare `LOS_MuxLock` / `LOS_MuxUnlock` / `LOS_MuxInit` /
    `LOS_MuxDestroy` / `LOS_SpinLock` / `LOS_SpinUnlock` 等**调用形式**
    （仅 `LosMux` 字段嵌在 struct 里不算）。
  - 反例：`feature/exfat-port-spec-first:fs/exfat/spec/interface/exfat_mount.spec`
    是按本约束写的范本——`## First Prompt` 段含 5 个功能 Case + goto-stack
    回滚 Invariant，`## Refine Prompt` 段只放 lock state Pre/Post + 5 步
    初始化顺序 + deadlock note。
  - 触发未命中时（纯无锁 helper），`## First Prompt` / `## Refine Prompt`
    **不能**作为空占位写——直接从 `[RELY]` 起即可。

### 阶段 3 — Linux → LiteOS-A 映射

完整映射见 [references/liteos-vfs-mapping.md](references/liteos-vfs-mapping.md)。要点：
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

风格细节见 [references/liteos-fs-style.md](references/liteos-fs-style.md)。硬约束：

- 版权头 BSD-3-Clause 华为 Device 模板（**不要**沿用 Linux GPL 头）
- `#ifdef LOSCFG_FS_<NAME>` 包每个翻译单元
- 命名：VFS 回调 `Vfs<Name><Op>`、私有辅助 `<name>_<verb>_<noun>`、
  盘上结构 `struct exfat_dentry` 沿用上游
- 字符串/内存：`_s` libsec 变体
- 加锁：可能睡眠用 `LOS_MuxLock`；否则 `LOS_SpinLock`。**绝不**在持自旋锁时睡眠
- 错误码：VFS 边界返回负 POSIX errno；内部允许正值 errno + goto-stack

具体可工作样例见 [references/exfat-walkthrough.md](references/exfat-walkthrough.md)。

代码生成走 Step 1（codegen）→ Step 3（cmocka 测试生成）→ Step 4（spec/code audit）
合并审计：审计器只读对照 `spec + Linux + code + test + inherited invariants`，将问题
分为 `spec_under_specified` / `codegen_drift` / `test_gap` / `prompt_gap` / `uncertain`。
codegen / test gap 分别进入 `code_gen_refine` / `test_gen_refine`；
`spec_under_specified` 不允许静默改已批准 spec，必须回 Loop spec 或交用户确认范围裁剪。
最多 3 轮，最终批准仍只属于用户。

**破坏性目录操作流程**（unlink / rmdir / rename source-delete / create-overwrite）：
直接遵循 SysSpec 论文 §4.1 复杂度分级 — 用 Pre/Post-Condition Cases + 必要 Invariant +
**强制 System Algorithm**（项目策略，比论文严，全 stage 必含）明确每个 Linux 副作用
（path-cache eviction、parent metadata refresh、cluster release 等）应当落在哪个
Case / Invariant / Phase，或显式 prose 说明为何 OUT-OF-SCOPE。Loop code 的 Step 1
把每条 Post-Condition 落到代码语句 / helper / error branch；Step 3 给每个 testable
Case + Invariant 出测点；Step 4 跨对照 spec → code → test → Linux 闭环。

### 阶段 5 — 构建 + 两层回归

**构建接线**：`fs/<name>/{BUILD.gn, Makefile, Kconfig}` + 顶层 `fs/{BUILD.gn, Kconfig}`
+ `tools/build/mk/los_config.mk` + 产品 config。

**注册胶水**：LiteOS-A 不用 `LOS_MODULE_INIT`。用链接器表：

```c
struct MountOps g_<name>Mops = { .Mount, .Unmount, .Statfs, .Sync };
FSMAP_ENTRY(<name>_fsmap, "<name>", g_<name>Mops, FALSE, TRUE);
```

**两层回归测试 / Two-wave regression**：

| 层 / Wave | 跑在哪 | 入口 | 详细操作手册 |
|---|---|---|---|
| **Wave A: cmocka host** | dev/CI host (Linux x86_64) | `make` in `testsuites/unittest/<name>_host/` | [references/cmocka-host-harness.md](references/cmocka-host-harness.md) |
| **Wave B: QEMU LTP smoke** | QEMU + LiteOS-A | `tools/regress/qemu_<name>_run.sh` | [references/ltp-qemu-regression.md](references/ltp-qemu-regression.md) |

**聚合**：`tools/regress/run_all.sh` 顺序跑 Wave A → Wave B，时间戳化报告写到
`docs/<name>_regression_<ts>.md`，symlink 到 `_latest.md`。退出码 `0/1/2 = 全过 / 有失败 / panic-or-hang`。

---

## 防御层次（plugin 与 skill 共享的契约）/ Defense layer topology

```
Loop spec: 生成 spec 草稿 → 异构 spec_audit（≤ 2 轮 refine）→ 用户审 / 批 spec
Loop code (per stage)：按执行顺序
  Step 1  生成 C 代码 (codegen)
  Step 2  静态检查 + 内核 build （fail fast；任一子步骤失败 → 回 Step 1）
          2.1 LSP compile (clangd via OMC LSP)            (≤ 4 轮)
          2.2 style audit (prompts/style_audit.md)        (≤ 5 轮)
          2.3 kernel build (run_build_kernel)             (≤ 3 轮)
  Step 3  cmocka 测试生成 + 测试编译                       (≤ 3 轮 共享)
          3.1 test gen     (prompts/unittest_gen.md)
          3.2 cmocka build only —— 只编译，不执行
  Step 4  spec/code audit                                 (≤ 3 轮)
          合并旧 SpecEvaluator + 旧异构审计：spec↔code conformance + Linux 等价性 +
          inherited invariant + test 覆盖。Finding 分流：
            codegen_drift          → 回 Step 1
            test_gap               → 回 Step 3
            spec_under_specified   → SpecFine（cap 3） → Step 1
  Step 5  运行时验证                                      (≤ 3 轮 共享)
          5.1 cmocka exec (host wave，覆盖刚编译的测试 + 历史 stage 测试)
          5.2 QEMU smoke (run_qemu_smoke)
          失败若来自新生成测试 → 回 Step 3；否则 → 回 Step 1，重跑 Step 2 → 4 → 5
  Step 6  用户审核 (HITL，唯一终态闸；一并审 code + test)
模块完结：tools/regress/run_all.sh 仍可手动 / CI 触发（per-stage Step 2/3/5 已覆盖）
```

**Step 2.2 style audit** 从 `prompts/style_rules.md` 读取 LiteOS-A 风格 canon
（≥ 17 项硬规则：libsec / 内存 / 锁 / 错误码符号 / FS 注册 / 命名 / ... 完整列表见
`style_rules.md`）。模板 `prompts/style_audit.md`，输出 JSON
`{is_good, score, summary, violations[]}`。`is_good=true && score≥80` 进 Step 2.3
内核 build；否则注入 violations 重生（≤ 5 轮）。clang-format dry-run 作为
advisory hint 喂入 `[AUTO CHECKS]` 段，不当硬门。**与 Step 2.1 LSP / Step 2.3 build
预算完全分离**——LSP 失败只消耗 LSP 预算、style 失败只消耗 style 预算、build
失败只消耗 build 预算。

**Step 3 测试生成 + 编译**：由 spec `[SPECIFICATION]` Cases + Invariants + 刚通过
Step 2 的真实代码符号派生 `testsuites/unittest/<name>/test_<stage>.c.draft`：每个
Case 至少一个测点、每个可单测的 Invariant 一个测点；用 `mock_disk_*` +
`<name>_image_builder_*` 原语，禁止依赖完整 VFS（VnodeAlloc / VfsHashInsert 等
归 Step 5.2 QEMU smoke 覆盖）。模板 `prompts/unittest_gen.md`。Step 3.2 只把测试
binary 编译出来——执行留给 Step 5。≤ 3 轮预算共享：编译失败也算这预算的消耗。
超出意味着 spec `[SPECIFICATION]` 写得不够清楚——应回 Loop spec SpecFine。
MCP 工具：`test_gen_{start,submit,refine,approve}`。`test_gen_approve` 写 `.c` +
自动接 `Makefile::HARNESS_SRCS` 与 `main.c::run_suite()`。

**Step 4 spec/code audit**（合并 spec conformance 与 Linux 异构审计）：覆盖
7 类 spec 偏差（函数签名 ≠ `[GUARANTEE]` / Pre-Post-Invariant 违反 / Locking
标注漂移于 `## Refine Prompt` / 幻觉 helper / Case 分支与 `[SPECIFICATION]` 不符 /
跳过 System Algorithm 的 phase 顺序 / **新生成 spec 缺失 System Algorithm 块**
[grandfather: 旧 spec 豁免]）+ Linux 等价性（Linux 源 ↔ spec/code）+ test
覆盖缺口。**审计器优先选异构模型族**（如 GPT-family，与生成器不同）以保留独立
review 价值。模板 `prompts/speceval.md` 与 `prompts/heterogeneous_audit.md`
（mode=`code_audit`）联合使用。审计器**只读 / advisory**：不写文件、不批准、
不改 prompt；输出 JSON 含 `finding_id, severity, root_cause, source_anchor,
generated_anchor, claim, evidence, recommendation, confidence`。
风格违例不归此 step（已在 Step 2.2 处理）；看到风格 finding 视为 false positive。

**Step 5 运行时验证**：5.1 cmocka exec (host 单测，跑 Step 3 编译出来的 test +
历史已批准测试) → 5.2 QEMU smoke (`run_qemu_smoke`)。任一失败统一注入
`layer="cmocka|qemu"` 区分。**只在审计通过后跑**——避免给审计将要 reject 的代码
付昂贵的 QEMU 时间。

报告范例：`docs/exfat_speceval.md`（Step 4 spec/code audit）+
`docs/exfat_style_audit.md`（Step 2.2 风格）。

---

## 移植"开局" / Quick start

- `/specfs-port` — 查看 DAG 状态、推荐下一步。
- `/specfs-port-spec <linux-path> <stage>` — Loop spec，从 Linux 源码起草 SYSSPEC spec。
- `/specfs-port-code <spec-path>` — Loop code，从已批准的 spec 生成 LiteOS-A C 代码。

可选 flag：

- `--audit-off` — 关闭 Step 4 spec/code audit 自审（默认 ON）。
- `--style-off` — 关闭 Step 2.2 风格子步骤（默认 ON）。
- `--test-off` — 同时关掉 Step 3 cmocka 测试生成与 Step 5.1 cmocka exec（默认 ON）。
  仅当新代码无 host-testable 表面时使用（例如纯 QEMU LTP 覆盖的写路径）；用了就在
  最终报告里显式记 "测试覆盖：跳过（gap）"，让后审可补。
- `--no-build` — 跳过 Step 2.3 内核 build + Step 5（不 build / 不跑 cmocka / 不跑 QEMU）。
- `--no-regress` — 跳过模块完结时的 `tools/regress/run_all.sh` 提醒。
- `--prompt-override <file>` — 跳过 spec 派生的提示拼装。

每一轮完成后，DAG 节点（`spec/<name>/.specfs.dag.json`）记录 spec/code 的双层
批准时间戳；下一阶段拉取 inherited invariants 做跨阶段一致性检查。异构审计记录是
append-only review evidence，不是批准状态；不能把 auditor pass 当成用户批准，
也不能把 auditor recommendation 自动写进全局 prompt。

---

## 常见陷阱（速查）/ Common pitfalls

- **页缓存惯性**：Linux 用 `struct page`/`buffer_head`，LiteOS-A 用 bcache。映射前
  先把 FS 用"逻辑偏移处的缓冲区"重新表述。
- **跳过 Refine Prompt 锁轮次**：第一轮就把锁糅进规范，后续无法仅凭规范推导锁序错误。
- **照搬 `struct buffer_head` 回调名**：Linux 操作表与 `VnodeOps` 不是 1:1，按
  [references/liteos-vfs-mapping.md](references/liteos-vfs-mapping.md) 走。
- **忘了 libsec**：`strcpy`/`memcpy` 编译过但违反华为安全编码方针。
- **引入 Linux GPL 头**：LiteOS-A 全树 BSD-3-Clause，不许带署名注释黏贴 GPL 文件。
- **单一巨大的 `vfs_<name>.c`**：VFS 回调要分散到 `interface/`，注册胶水保持薄。
- **混用 `los_disk_read` 与 `los_part_read`**：两者索引来自不同命名空间，混用导致
  "mutex lock failed"。
- **`g_<fs>Vops = { 0 }` 全 NULL 会 umount panic**：见 [references/fs-debug-recipe.md](references/fs-debug-recipe.md)
  "教训 #1"。即使只做 mount，`vop->Reclaim` 也**必须**是真实函数。
- **FS Unmount 不可触碰 root vnode**：照抄 fatfs_umount，详见 [references/fs-debug-recipe.md](references/fs-debug-recipe.md)
  "教训 #2"。
- **静态 LTP 加载失败**：用 OHOS clang 动态链才行，详见 [references/ltp-qemu-regression.md](references/ltp-qemu-regression.md)。

## FS 调试硬约束（用户明确指令）/ FS debugging hard constraint

修 FS bug 时**坚守 fs/<name>/ 内部**。`fs/vfs/`、`kernel/`、`syscall/`、
`drivers/block/disk/` 是公共层——**禁止**未经用户许可改这些路径。理由：
其它 FS（fat、jffs2）能跑就证明公共层是好的；问题一定在新加的 FS 自己。

诊断流程 + 用维测日志定位 + fatfs 参照 + 删临时 print：见
[references/fs-debug-recipe.md](references/fs-debug-recipe.md)。

---

## 完成后向用户输出的报告 / Final report contract

- 规范树体量：文件数、规范总 LoC。
- 生成代码：文件数、代码总 LoC，spec/code 比例（用作 SpecFS 生产力命题的合理性自检）。
- Step 2.1 LSP：通过 stage 数 / 总数。
- Step 2.2 风格审计：通过 stage 数 / 总数 + 主要 hard 违例。
- Step 2.3 内核 build：`make build` 结果（per-stage 已运行）。
- Step 4 spec/code audit：`is_good=true` 的 stage 数 / 总数。
- Step 5.1 cmocka exec：通过 stage 数 / 总数。
- Step 5.2 QEMU smoke：通过 stage 数 / 总数。
- 回归状态：`tools/regress/run_all.sh` 退出码 + cmocka pass/fail + LTP pass/fail（模块完结时手动跑）。
- **测试覆盖**：N/M — N 个 stage 在 Step 3 落了 cmocka 测试 / 总 M 个；
  跳过的 stage 列出名字 + 跳过原因（`--test-off` 还是 `harness_dir` 未就绪）。
- 标注 "v2 删除" 的开放映射项（如日志、fscrypt），便于规划后续。

---

## 参考文档（按需读取）/ Reference docs (load on demand)

| 文件 | 内容 |
|---|---|
| [references/specfs-format.md](references/specfs-format.md) | SYSSPEC 规范格式与带注解示例 |
| [references/liteos-vfs-mapping.md](references/liteos-vfs-mapping.md) | Linux↔LiteOS-A 类型/API/锁速查表 |
| [references/liteos-fs-style.md](references/liteos-fs-style.md) | 版权头、命名、构建接线模板 |
| [references/exfat-walkthrough.md](references/exfat-walkthrough.md) | 端到端样例（exfat_alloc_cluster 一例） |
| [references/cmocka-host-harness.md](references/cmocka-host-harness.md) | Wave A 单测目录骨架、stub 设计、加新 stage 测试 4 步 |
| [references/ltp-qemu-regression.md](references/ltp-qemu-regression.md) | Wave B LTP 交叉编译（动态链接）、QEMU runner 契约、排查矩阵 |
| [references/fs-debug-recipe.md](references/fs-debug-recipe.md) | FS bug 调试硬约束、维测日志方法、Reclaim hook 教训、4 轮 mount/umount 验证 |

外部参考 / External cross-refs：

- `../../DESIGN.md` — 插件实现规格（DAG / MCP 工具表 / prompts 拼装）。
- `../../CHANGELOG.md` — 插件版本演进与历史 Phase 标记（本文件不再内联）。
- `docs/dev/exfat_mount.md` — mount 端到端实战记录。
- `docs/exfat_speceval.md` — Step 4 spec/code audit 报告范例。
- `docs/exfat_style_audit.md` — Step 2.2 风格审计报告范例。
- `tools/regress/README.md` — 两层回归套件运行手册。
