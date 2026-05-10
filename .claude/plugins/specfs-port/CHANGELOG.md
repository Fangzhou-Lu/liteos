# Changelog — specfs-port plugin

All notable changes to this plugin. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely; semver applies.

> 文档版本变动归集 / Plugin docs single-source-of-truth: 自 0.5.7 起，所有
> README / DESIGN / SKILL / commands / prompts 内联的版本/Phase/日期/迁移
> 记录全部抽到本文件，其他文档仅保留事实性内容。

---

## [0.5.8] — 2026-05-11

### Changed — Loop code 流水线重排 + Loop 重命名 + Layer/Step 统一编号

**用户指令 / User directive (2026-05-11)**:
> "lsp compile + build + style audit 应该放在 C 代码生成之后, cmocka test gen 之前；
>  step 5 和 step3b 实际是同一个事情, step 6 build 已提前；
>  请按执行顺序调整编号，去掉 layer T 表述，统一用数字
>  loop A 请改为 loop spec, Loop B 请改为 loop code"
> "Loop C 命名为 Loop eval"

#### 一. Loop 命名空间重命名（语义化）

- `Loop A` → **`Loop spec`**（规范起草，对应 `/specfs-port-spec` 命令）。
- `Loop B` → **`Loop code`**（代码 + 测试生成，对应 `/specfs-port-code` 命令）。
- `Loop C` → **`Loop eval`**（Linux 等价比对 + prompt 反向优化）。

通过 `perl -pi` 在 12 个文档（README / DESIGN / SKILL / 4 commands / 16 prompts）
做全局替换；CHANGELOG.md 不动（保留历史 entry 中的原 Loop A/B/C 用语作为时间锚点）。
共替换 78 处 → 0 残留。

#### 二. Loop code 流水线按执行顺序重排为 6 步

**旧拓扑**（Layer 命名混乱：Layer T / Layer 1a.1 / Layer 1a.2 / Layer 3 / Layer 2.1 /
Layer 2.2 / Layer 2.3 / Layer 4，编号不连续、Step 3a / Step 3b 子步骤难记）：
```
Step 3 codegen → Step 3a Layer T cmocka test gen → Step 3b heterogeneous code audit
→ Step 4 Layer 1a (LSP+style) → Step 5 Layer 3 SpecEval → Step 6 Layer 2 (build+cmocka exec+QEMU)
→ Step 7 Layer 4 user review
```

**新拓扑**（按执行顺序，纯数字、含 fail-fast 路径）：

```
Step 1  生成 C 代码 (codegen)
Step 2  静态检查 + 内核 build （fail fast）
        2.1 LSP compile        (≤ 4)
        2.2 style audit        (≤ 5)
        2.3 kernel build       (≤ 3)         ← build 提前到 cmocka test gen 之前
Step 3  cmocka 测试生成 + 测试编译            (≤ 3 共享)
        3.1 test gen
        3.2 cmocka build only —— 只编译，不执行
Step 4  spec/code audit                      (≤ 3)
        合并旧 SpecEvaluator (spec conformance) + 旧异构审计 (Linux 等价性)
        ↑ 用户明确判断：旧 Step 5 (SpecEval) 与旧 Step 3b (heterogeneous code audit)
          是同一件事，分流到 codegen_drift / test_gap / spec_under_specified / prompt_gap
Step 5  运行时验证                            (≤ 3 共享)
        5.1 cmocka exec —— 跑 Step 3 编译出来的 binary + 历史 stage 测试
        5.2 QEMU smoke
Step 6  用户审核 (HITL，唯一终态闸)
```

#### 三. 关键设计决策

- **build 提前**到 cmocka test gen 之前（用户指令 "step 6 build 已提前"）。理由：
  cmocka 测试看到的是已经 LSP-clean + style-clean + kernel-build 通过的真实 C 符号，
  而不是 spec 抽象——避免一类 test/code drift bug。
- **静态检查 + build 串行 fail-fast**（用户指令 "lsp compile + build + style audit
  应该放在 C 代码生成之后, cmocka test gen 之前"）。三者各自独立 retry 预算，失败
  只消耗自己预算；任一失败回 Step 1。
- **审计合并**（用户指令 "step 5 和 step 3b 实际是同一事情"）。Step 4 同一个独立异构
  agent 一次跑完 spec conformance + Linux 等价性 + test 覆盖。审计预算从 8 降到 3
  (paper 8-round max 是单维 spec conformance；现在审计范围更广，应更早 escalate)。
- **cmocka exec 推迟到 audit 之后**（Oracle 验证建议）。避免给将要被 audit reject
  的代码付昂贵的 QEMU 时间。Step 5 共享 3 轮预算覆盖 cmocka exec + QEMU smoke。
- **失败回退路径明确化**：Step 4 finding 按 `root_cause` 分流——`codegen_drift` →
  Step 1（重生后必须重跑 Step 2→3→4→5）；`test_gap` → Step 3.1 only；
  `spec_under_specified` → SpecFine（cap 3）→ Step 1；`prompt_gap` → 累积到
  `docs/<module>_prompt_feedback.md` 给 Loop eval 用，不消耗本 stage 预算。

#### 四. 术语清理

- 删除全部 `Layer T` / `Layer 1a` / `Layer 1a.1` / `Layer 1a.2` / `Layer 1b` / `Layer 2`
  / `Layer 2.1` / `Layer 2.2` / `Layer 2.3` / `Layer 3` / `Layer 4` / `Layer S` 标号
  （13 个文档，共 100+ 处）。
- 删除全部 `Step 3a` / `Step 3b` 字母后缀子步骤标号；改为纯数字 `Step 2.1` / `Step 3.1`
  小数点编号（一个层级，不跨层）。
- `commands/specfs-port-spec.md` 内的 `Step 3b heterogeneous spec audit` 重编为
  `Step 3.5`（保持 Step 4 = 用户审，序号连续）。

#### 五. CLI flag 同步

| Flag | 旧语义 | 新语义 |
|---|---|---|
| `--speceval-off` | 关 Layer 3 SpecEval | 重命名为 `--audit-off`，关 Step 4 spec/code audit |
| `--style-off` | 关 Layer 1a.2 style | 关 Step 2.2 style audit |
| `--test-off` | 关 Layer T + Layer 2.2 cmocka | 关 Step 3 + Step 5.1 cmocka exec |
| `--no-build` | 跳 Layer 2 整层 | 跳 Step 2.3 内核 build + Step 5 整层 |
| `--no-regress` | 不变 | 不变 |
| `--prompt-override` | 不变 | 不变 |

#### 六. retry 预算调整（Oracle 验证后）

| Step | 旧预算 | 新预算 | 备注 |
|---|---|---|---|
| 2.1 LSP | 4 | 4 | 不变 |
| 2.2 style | 5 | 5 | 不变 |
| 2.3 kernel build | (旧 Layer 2 共享 3) | 3 独立 | 早期 build 失败不消耗下游运行时预算 |
| 3 test gen + compile | 3 (gen) | 3 共享 (gen + compile) | gen 失败 + compile 失败合算 |
| 4 spec/code audit | 8 (paper) | 3 | 范围更广，更早 escalate |
| 5 cmocka exec + QEMU | (旧 Layer 2 共享 3) | 3 共享 | 与 build 预算分离 |
| inner SpecFine | 3 | 3 | 不变 |
| 6 user review | unlimited | unlimited | 不变 |

#### 七. 涉及文件

13 个文档（按行数排序）：

- `DESIGN.md`：§2.2 工作流图重写 / §4.3.5 重命名 / §7 layer detail 表换列名 / §8 命令章节同步 / §10 retry budget 列表 / §12 用户审 checklist 标号 / 附录 A `gencode.py` 对照行更新。
- `commands/specfs-port-code.md`：完全重写（300+ 行）。Active layers 表 / Step 0-6 编排 / iteration accounting 表 / final output 模板全部对齐新流水线。
- `skills/specfs-port/SKILL.md`：frontmatter description / §阶段 4 / §防御层次（整段重写）/ §最终报告契约 / §`--*` flag 列表 / §参考文档全部更新。
- `README.md`：description bullet / Workflow 框 / flag 表 / files tree comments / Requirements / Why this exists 全部更新；新增"回退路径 / Failure routing"段。
- `commands/specfs-port-spec.md`：`Step 3b → Step 3.5`，删 Layer T 跨 loop 注释。
- `prompts/validation_checklist.md`：§7 自动 step status 表（旧 Layer 1a/2/3/4 行 → 新 Step 2.1/2.2/2.3/3/4/5.1/5.2 行）；§8 cmocka draft 段。
- `prompts/speceval.md`：定位由 "Layer 3" 改为 "Step 4 spec conformance 子提示词"。
- `prompts/style_audit.md`：通过后跳转目标由 "Layer 3 SpecEval" 改为 "Step 2.3 kernel build"。
- `prompts/unittest_gen.md`：触发时机表述更新。
- `prompts/linux_compare.md`：与 Step 4 audit 关系澄清。
- `prompts/format_traps.md` / `prompts/style_rules.md`：用户审 step 引用更新。
- `commands/specfs-port-metrics.md`：链接到 SKILL §防御层次 的锚点保持一致。

#### 八. 未变 / 保留

- DAG schema、MCP 工具签名（`code_gen_*` / `test_gen_*` / `spec_gen_*` / `run_build_kernel` /
  `run_qemu_smoke` / `validator_run_holistic` 等）一律未动——server 代码不需重启；
  所有变更纯属文档与命令编排重排。
- 架构性 Wave A (cmocka host) / Wave B (QEMU LTP smoke) 术语保留——它们是回归
  套件层的轴，不在 Loop code 6 步轴上。
- 验证：插件目录所有 .md（除 CHANGELOG）中 `Layer T` / `Step 3a` / `Step 3b` /
  `Layer 1a/1b/2/3/4/S` / `Loop A/B/C` 全部 0 残留。

---

## [0.5.7] — 2026-05-11

### Changed — 文档中英文混合化 + 跨文档去重 + 版本说明集中化

**用户指令 / User directive (2026-05-11)**:
> "请参考 skill 目录下的文档将剩下的文档改为中英文混合模式 …
>  command 下面的文档重复度很高，请只保留一份（优先 skills 目录下的参考文档），
>  其他地方对重复的内容只记录引用 …
>  请同步将文档里面版本变动移到 CHANGELOG.md 中，其他文档不记录版本相关内容"

**混合策略 / Bilingual strategy** —
- 标题、frontmatter `description`、章节说明：中文为主，便于中文读者扫读。
- LLM prompt 模板正文、占位符、JSON key、CLI 标志、库函数、类型签名：保持英文，
  避免影响 LLM 生成质量。
- 架构性 Wave A / Wave B（host cmocka / QEMU LTP）保留为术语，不视为版本记录。

**去重策略 / Dedup strategy** — `commands/specfs-port-{spec,code}.md` 中与
`skills/specfs-port/SKILL.md` 重复的"五阶段流水线"/"防御层次"/"完成报告"
等概念性段落改写为相对路径 markdown 链接，命令文档只保留各 Step 的编排细节
（参数解析、MCP 调用、用户分支处理）。

**版本说明集中 / Version-history aggregation** —
原本散落在以下 17 个文档里的版本/Phase/日期/迁移记录，统一移入本文件并保留
原有版本号 anchor，便于历史回溯：

| 移出位置 / Removed from | 涉及版本/Phase 标记 |
|---|---|
| `README.md` | `since plugin v0.2 / v0.3 / v0.3.4 / v0.5.0`、`P1.1 / P1.2 / P1.3 / P1.4` |
| `DESIGN.md` | `Layer T (v0.3.4)`、`v0.3.2-era gap`、`v0.4 alignment`、`P1.2 / P1.3 / P1.4 / P4.1`、`v04-pruned-2026-05-07` 等多处 |
| `commands/specfs-port-spec.md` | `P1.4 / P1.5 / P4.1` 工作流重排说明 |
| `commands/specfs-port-code.md` | `P1.4 (2026-05-07)` pipeline reorder、`Removed in v0.4`、`P1.3 fully removed`、`On-demand reference expansion (v0.4)` 等 |
| `prompts/codegen.md` | `P1.6 / P1.7 / P1.8 / P1.9` prompt 演化注释 |
| `prompts/linux_to_spec.md` | `P1.5 two-phase`、`P1.9 (2026-05-10) realignment`、`pre-realignment` |
| `prompts/heterogeneous_audit.md` | `P1.8 draft, 2026-05-10`、`P1.9 (2026-05-10)` 删除 Behavior Obligations |
| `prompts/speceval.md` | `P1.2 (2026-05-07)` scope split、`P1.5` phase-layering |
| `prompts/style_audit.md` | `v0.3 / P1.2 / P1.4` topology 演化 |
| `prompts/prompt_optimize.md` | `P1.6 Wave 2`、`P1.7 (2026-05-08)` |
| `prompts/linux_compare.md` | `P1.6 Wave 2`、`v0.5.5 deferred-work` |
| `prompts/two_phase_rules.md` | `P1.6 (2026-05-08)` 头注 |
| `prompts/validation_checklist.md` | `P1.2 / P1.3 / P1.4` 层级标签 |
| `prompts/unittest_gen.md` | `v0.3.2 / v0.3.4 / P1.4 / P1.9 (2026-05-10)` |
| `prompts/format_traps.md` | "added after EROFS evaluation…" 历史注释 |
| `skills/specfs-port/SKILL.md` | `P1.2 / P1.4 / P1.5 / P1.8 / P1.9`、`v0.2 / v0.3 / v0.3.4 / v0.5.0`、`v0.1→v0.2→v0.3→v0.3.4→P1.3→P1.4` 累计史 |
| `skills/specfs-port/references/fs-debug-recipe.md` | `v0.4 改进项` |

**架构性引用保留 / Kept (architecture, not release-history)** —
- `DESIGN.md` 里 `Wave A cmocka host + Wave B QEMU LTP smoke` 表述。
- `SKILL.md §回归套件` 中 `Wave A 9 个 stage` 等 stage-counting 描述（描述当前状态，不是版本演进）。

### Migration notes — 当前层拓扑（来自历次 P1.x 累积）

为了让读者不必翻阅历次条目即可理解今日防御层顺序，下表汇总了截至 0.5.6 已稳定的层定义。后续若再调整请直接编辑此表并升版本号：

| 层 / Layer | 触发位置 / Trigger | 当前实现 / Current behavior |
|---|---|---|
| Layer 1a Compile + Style | Loop B Step 4 | LSP (clangd) 编译先行；style audit 串行其后；各自独立重试预算。|
| Layer 3 SpecEvaluator | Loop B Step 5（先于 Layer 2） | 仅做 spec ↔ code conformance；不再做 style/convention。|
| Layer 2 Build + cmocka exec + QEMU smoke | Loop B Step 6 | 三段统一为 Layer 2；fold 进了之前的 SpecValidator。|
| Layer T cmocka test gen | Loop B Step 3a（ex-Loop A） | 已从 Loop A 移到 Loop B，便于 reference 真实生成符号。|
| Layer 4 用户审核 | Loop B Step 7 | 始终终态闸；Layer 1-3 全 pass 才进入。|

---



### Add — Loop C: linux_compare 评估 + prompt-template 反向优化 (P1.6 Wave 2)

User directive 2026-05-08（v0.5.5 已记入 Known caveats，本轮交付）:
> "实现评估prompt 和生成C代码的评估机制（通过对比linux 代码功能）来通过反馈
>  优化spec 抽取prompt ，以及代码生成prompt"

#### 关键修正

用户在实现过程中两次澄清了优化目标：
1. "这里是优化prompt 不是优化spec 和代码，不能直接复用之前的流程" — Loop C
   产出反馈给 **prompt 模板自身**，不是当前 stage 的 spec/code 产物。
   `linux_compare_submit` 不能往 `sess.failures` 里塞 FailureRecord（那会
   触发 `code_gen_refine`）。本轮严格遵守此边界。
2. "进行优化prompt 测试的时候，只对比第一次spec 或C代码生成结果" + "优化
   prompt 提供快速路径，spec 和代码都只生成一遍，无反馈机制" — 评估必须
   测的是 prompt 的引导能力，而非 LLM 的迭代修复能力，否则 prompt 质量信号
   被反馈循环污染。本轮通过 `.first` 快照 + `fast_eval` 模式双管落实。

#### Added

1. **`prompts/linux_compare.md` (NEW, ~120 行)**:
   Loop C 元提示。LLM 比对生成的 SYSSPEC spec + LiteOS-A C 与 Linux 原始 TU
   的功能等价性，输出 JSON 包括 `spec_gaps[] / code_gaps[]` （带 severity /
   linux_evidence / root_cause）以及 ADDITIVE 的 `spec_prompt_recommendations
   / codegen_prompt_recommendations`。SCOPE 显式排除 Layer 3 SpecEval 已覆
   盖的 spec↔code 一致性、Layer 1b 已覆盖的命名/libsec/锁原语，以及
   "linux_intentional_drop" 类（RCU / page cache / jbd2 等）。

2. **`prompts/prompt_optimize.md` (NEW, ~70 行)**:
   元元提示 — 把累计的 Loop C 推荐滚动注入到一个具体的 prompt 模板（如
   `linux_to_spec.md` / `codegen.md` / 任一 fragment）。强制约束:加性编辑、
   开头 `<!-- ... -->` 开发注释块必须保留并追加日期分项、长度增长 ≤30%、
   遇结构性变更（如拆分一个 `[SCOPE]` 段）输出 `# RESTRUCTURE NEEDED:` 标记
   留给人审决定。

3. **MCP 工具 5 个**:
   - `linux_compare_start(session_id, linux_source_path, code_path?)`:
     组装比对提示。**只读 `.first` 快照**——若 `<draft>.spec.first` 或
     `<code>.first` 存在则用之，确保比对的是首次生成产物；否则回退到当前
     文件并在 `spec_source / code_source` 字段里报"no .first snapshot"。
   - `linux_compare_submit(session_id, comparison_json)`:
     解析 JSON，把 spec_gaps / code_gaps / 推荐项追加到
     `docs/<module>_prompt_feedback.md`。**不**注入 `sess.failures`。
   - `prompt_optimize_propose(target_prompt_name, module)`:
     读 `_OPTIMIZABLE_PROMPTS` 白名单中的目标 prompt 文件 + 累计推荐，按
     `_TARGET_REC_TYPE` 选 spec / codegen 子集，组装元元提示。空推荐时返回
     `skipped_reason` 而非空字符串提示。
   - `prompt_optimize_apply(target, new_text, dry_run)`:
     dry_run=True 仅返回 unified diff；dry_run=False 把当前模板 backup 到
     `<name>.md.bak.<unix_ts>`，写入新文本，并清掉 `prompts._TEMPLATE_CACHE`
     的对应条目使下一次 `assemble_*` 立即生效。
     非致命 sanity warnings:缩水 >20% / 膨胀 >50% / 缺失开头注释块。
   - `prompt_feedback_summary(module)`:
     读 `docs/<module>_prompt_feedback.md`，返回 stage 数 + 截断到 30K 的
     原文。

4. **`.first` 首次产物快照** (per 用户约束 #2):
   - `spec_gen_submit`: 首次提交时写 `<draft>.spec.first`（再次提交不覆盖）。
   - `code_gen_submit`: 首次提交时写 `<code>.first`（再次提交不覆盖）。
   - `linux_compare_start`: 优先读 `.first`，回退当前文件。

5. **`fast_eval` 单次生成模式** (per 用户约束 #3):
   - `state.Session.fast_eval_mode: bool = False`。
   - `session_start(mode="fast_eval")` 自动关 speceval / style_audit /
     test_gen，置 skip_build_layer=True。
   - 新工具 `toggle_fast_eval_mode` 用于罕见的中途切换。
   - `_refuse_if_fast_eval` 闸接到 `spec_gen_refine` / `code_gen_refine` /
     `spec_fine` / `inject_diagnostics` 上——fast_eval 模式下统统 raise
     ValueError，确保单次产物不被任何反馈循环污染。

6. **`prompts/prompts.py::assemble_linux_compare_prompt`** +
   **`assemble_prompt_optimize_prompt`** 两个新 assembler。

#### Modified

- `state.py::Session.mode` 类型扩到 `Literal["gen", "evolve", "fast_eval"]`。
- `state.py::Session` 新增 `fast_eval_mode: bool` 字段（默认 False）。

#### Tests

- `tests/test_prompts.py`: +4 测试覆盖 linux_compare / prompt_optimize 两个
  新 assembler（含空 invariants 段消除、空推荐占位符、加性约束）。
- `tests/test_mcp_tools.py`: +25 测试覆盖
  - linux_compare_start 组装 / 缺源文件错误 / 缺 spec session 错误
  - linux_compare_submit JSON 解析（含 ```json fence 容错）/ 文档写入 /
    sess.failures 不被污染
  - `.first` 快照写入 / 不被二次提交覆盖 / linux_compare 优先读 / 缺失时回退
  - prompt_optimize_propose 空推荐返回 skipped / 多 stage 滚动 / 白名单错误
  - prompt_optimize_apply dry_run / 实际备份 / 缩水告警 / 缺注释块告警
  - prompt_feedback_summary 文档存在性 + stage 计数
  - fast_eval 模式 e2e + 4 个 refuse 闸 / 中途 toggle / 错误 mode 拒绝

测试总数 165 → **194** (+29)，全部通过。

#### Known caveats

(无遗留——本轮交付了 v0.5.5 CHANGELOG 列出的全部"待办 (a) (b) 第3项"。)
后续 P1.7 候选:
- `prompt_optimize_propose` 目前的累计推荐去重仅按 lowercased-trim 字面
  匹配，语义近似项（措辞不同但等价）需 LLM 在元元提示里二次去重——可考
  虑加 `embedding_dedup` 选项。
- `fast_eval` 模式目前不支持自动 batch 跑多 stage 的 harness 命令，HITL
  仍需逐 stage 调 spec_gen → code_gen → linux_compare。后续可加
  `fast_eval_run_stage(stage_name)` 一键串联。

## [0.5.5] — 2026-05-08

### Slim — Loop A / Loop B prompt 减重 (P1.6 Wave 1)

User directive 2026-05-08:
> "LLM 调用过多，请根据日志数据对比原始论文和代码给出优化方案 ...
>  当前规范存在可读性差以及规范冗余对代码生成无帮助的问题，请参照
>  论文公开代码优化"

#### 数据对比

Telemetry 累计:212 MCP 调用 / 48 LLM rounds / 138K 输入 token / 10.7K
输出。Loop A spec_gen 单轮 ~25K input(linux_to_spec 256 行 + ASK_FIRST
全文 ~70 行 + TWO-PHASE 显式 forbidden list ~50 行)。Loop B code_gen
~25K(codegen 84 行 + LITEOS_DIGEST + PRIOR INTERFACE)。

上游公开仓 specfs/tools/gencode.py:158-176 codegen prompt 仅 ~25 行,
spec 写作完全是人工(无 Loop A)。本插件 Loop A 是相对论文新增的能力,
其提示理应紧凑。本轮优化目标:把"显式行为说明"压缩为"on-demand
fragment"模式,默认提示精简、按需 fetch。

#### Modified

1. `prompts/linux_to_spec.md`:**256 → 116 行**(-55%)。
   - 去掉 OUTPUT FORMAT 全段示例,改为每段一行的 terse skeleton。
   - 去掉 TWO-PHASE METHODOLOGY 全段(50 行 forbidden list),改为
     5 行触发规则 + 一句"borderline 时 fetch two_phase_rules.md"。
   - REJECTION CRITERIA 从 9 项压缩为 6 项,合并相似条目。
   - 估算 spec_gen_start 单轮输入 -3K(25K → 22K)。

2. `prompts/codegen.md`:**84 → 50 行**(-40%)。
   - "Required (always present)" / "Optional" 段头解释删除——LLM
     直接读 spec 标题字面即可。
   - "Output rules" 4 条压缩为 3 条,"Be precise and conservative"
     语气词去掉。
   - 估算 code_gen_start 单轮输入 -1K(25K → 24K)。

3. `prompts/two_phase_rules.md` (NEW, ~140 行):
   存原 linux_to_spec.md 的 TWO-PHASE 全量 forbidden list / 触发条件
   / 经典示例 / borderline Q&A,fetch 时按需返回。

4. `server/specfs_server.py::_FRAGMENT_REGISTRY`:
   注册新 `two_phase_rules` 条目;`fetch_prompt_fragment` docstring 更新
   有效名字列表。

#### Why now

每轮 spec_gen / code_gen 实测都在 22~25K 输入区间,占总开销主体。Loop A 单
stage 一次性下发"全量两阶段 forbidden list"是大量浪费——LLM 多数时候
不需要详细规则。fragment 模式对齐了上游的"轻量默认 + 按需展开"设计哲学,
预计批量移植 12+ stage 时累计省 ~36K 输入 token。

#### Known caveats

- 本轮只动了 Loop A / Loop B 默认提示;尚未实现:
  (a) 按 Linux 源功能对比生成代码的评估机制(用户原始诉求第 3 项);
  (b) 反馈回路把评估结果写回 prompt 调优建议。
  这两项作为 P1.7 候选,下一轮交付。
- spec_fine.md / speceval.md / unittest_gen.md 暂未瘦身——它们触发频率
  远低于 spec_gen / code_gen,优先级低。
- 现有已批准 spec 不需要重写:新提示影响新 spec 写作流程;旧 spec 仍
  按旧契约。

## [0.5.4] — 2026-05-08

### Restored — paper §4.1 two-phase spec methodology (P1.5)

User directive 2026-05-08:
> "请回复论文中的两阶段 spec 方法，第一阶段不处理锁序，第二阶段处理"
> "之前已经实现该功能，后面去掉了，可以参考之前的提交"

`feature/exfat-port-spec-first:fs/exfat/spec/interface/exfat_mount.spec`
shows the original pattern verbatim — `## First Prompt` opens Phase 1
(functional contract: 5 Cases + goto-stack rollback Invariant), then
`## Refine Prompt` opens Phase 2 (lock-state Pre/Post + 5-step
initialization-order constraint + deadlock note). At v0.4 (commit
`7c5244bd`) the two-phase methodology was demoted from "required when
locks involved" to OPTIONAL with a note `"声明已跳过 paper two-phase
SpecCompiler（用户：当时 LLM 局限）"`. P1.5 restores it as a hard gate.

#### Modified

1. `prompts/linux_to_spec.md`:
   - New `[TWO-PHASE METHODOLOGY]` segment ahead of `[OUTPUT FORMAT]`.
   - Trigger rule: two-phase REQUIRED if either (a) Linux source uses
     `mutex_lock` / `spin_lock*` / `down_*` / `*_lock_irqsave` etc., or
     (b) draft [RELY] declares lock-call primitives like `LOS_MuxLock` /
     `LOS_MuxUnlock` (struct-field lock types alone don't trigger).
   - Phase 1 forbidden list: Pre/Post-Condition cannot mention "holds X
     lock" / acquire / release; [GUARANTEE] calling-convention block
     describes return value + side effects only, NOT lock state.
   - Phase 2 forbidden list: cannot restate Phase 1 Cases, cannot change
     [GUARANTEE] signatures, cannot introduce new functional facts.
   - REJECTION CRITERIA expanded with 5 two-phase violation cases.
   - Canonical example pointer to `feature/exfat-port-spec-first` mount
     spec + paper's `delalloc.spec` Phase 1/2 with AA-deadlock note.

2. `prompts/speceval.md`:
   - New "Phase-layering mismatch" check in spec conformance bucket.
     Code uses lock-acquire primitives but spec lacks `## Refine Prompt`
     → flag as SPEC GAP (routes to F3 SpecFine, not codegen retry).
   - Phase 1 lock-leakage variant: spec mentions held locks in Phase 1
     with no Phase 2 → also flag with SpecFine recommendation.

3. `commands/specfs-port-spec.md` Step 4:
   - New "Two-phase lock check (P1.5)" sanity check ahead of HITL
     review. Two-step trigger scan (Linux source + draft [RELY]). On
     trigger-fired-but-spec-missing, auto-call `spec_gen_refine` with a
     P1.5-specific user_suggestion and loop back to Step 3 — user
     never sees a draft that fails the two-phase gate.
   - Mirror check on the negative path: trigger NOT fired but spec
     emits empty `## Refine Prompt` placeholder → also rejected.

4. `skills/specfs-port/SKILL.md`:
   - Stage 2 quality gate list: replaced "加锁是单独的 Refine Prompt
     轮次" one-liner with a full two-phase methodology block (5
     bullets) covering trigger conditions, Phase 1 / Phase 2 boundary
     rules, and pointer to `exfat_mount.spec` as canonical example.
   - [GUARANTEE] gate updated: "持锁状态留到 Phase 2".

#### Why now

The "lenient single-pass spec with optional Refine Prompt" stance from
v0.4 produced spec drift: codegen would emit lock acquisitions that the
spec never sanctioned, then SpecEvaluator and Layer 2 cmocka exec would
flag concurrency bugs that traced back to spec gaps, not code defects.
Forcing two-phase output puts lock contracts under the same review gate
as functional contracts, eliminating that class of feedback drift.

LLM capability has improved enough since v0.4 that the original "skip
two-phase" rationale no longer applies. The format is single-output
(LLM emits both phases in one round; the markdown markers separate
them), so there is no extra LLM round cost — only stricter format
enforcement.

#### Known caveats

- The auto-detection happens in two places (LLM via [TWO-PHASE
  METHODOLOGY] section + Step 4 sanity check). They use the same trigger
  rule but are independently coded; if the rule changes, update BOTH.
- Phase 2's Initialization-order constraint sub-block is currently a
  free-form numbered list; future work could formalize it as a phase
  System Algorithm with its own retry budget.
- Server-side `spec_gen_submit` does not yet run the trigger scan — the
  enforcement is in the LLM prompt + the client-side Step 4 check. A
  future revision could add server-side validation as a defense in
  depth, returning a structured `{validation_error: "phase_layering_*"}`
  before the spec text reaches the user.

## [0.5.3] — 2026-05-08

### Added — performance / token telemetry for MCP + LLM rounds

User directive 2026-05-08:
> "请设计 specfs-port 插件的性能检测机制，能够检测 LLM 调用次数时间和 token
> 消耗以及 MCP 调用次数和时间消耗记录到日志中"

Per-tool telemetry, JSONL log, CLI aggregator. Captures both MCP-tool
metrics directly and infers LLM round-trip metrics from the gap between
prompt-issuing tools and tools that ingest LLM-generated text.

#### Captured per tool call (one JSONL line)

```json
{"type":"mcp_tool","tool":"code_gen_submit","session_id":"abc123",
 "ts":1715170800.123,"ts_iso":"2026-05-08T...Z",
 "duration_s":0.0421,"input_tokens_est":1380,"output_tokens_est":0,
 "is_llm_round_trigger":false,"is_llm_ingest":true,"error":null}
```

- `duration_s`: MCP-server-side wall-clock (includes timeout-wrapper time)
- `input_tokens_est`: char-count/4 heuristic on the largest text-shaped
  kwarg (generated_code / generated_spec_text / payload / ...)
- `output_tokens_est`: char-count/4 of any prompt the tool returned
  (`prompt_for_llm` / `next_prompt`)
- `is_llm_round_trigger`: True when the tool returned a prompt
  (server just kicked an LLM round)
- `is_llm_ingest`: True for `*_submit`, `*_approve`, `inject_diagnostics`
  — tools that consume LLM-generated text
- `error`: stringified exception if the tool raised

LLM round-trip duration is computed offline as the gap between
consecutive `is_llm_round_trigger` and `is_llm_ingest` events on the
same `session_id`.

#### How metrics are wired

1. `server/_metrics.py` (~210 LOC):
   - `estimate_tokens(text)` — char/4 heuristic.
   - `emit(event)` — append JSONL, lockfile-protected, never raises.
   - `install_metrics(mcp)` — monkey-patches `mcp.tool` a SECOND time
     after `install_default_timeout`. Decoration order ends up:
     `metrics → timeout → original_fn`. Uses `inspect.signature.bind_partial`
     so positional and keyword calls both surface session_id correctly.
     Idempotent (`__specfs_metrics_patched__` tag).
   - `aggregate(events, session_id?)` → `SessionAggregate` dataclass.
   - `read_log(path?)` — JSONL reader, skips malformed lines.
2. `server/specfs_server.py`:
   - imports + calls `install_metrics(mcp)` right after the timeout
     installer. Zero per-tool edits.
   - new `metrics_summary(session_id?)` MCP tool returns the aggregated
     dict for in-session checks.
3. `server/specfs_metrics_report.py` (~140 LOC):
   - CLI: pretty-print per-session counters or `--json` machine output.
   - Includes Claude Opus pricing estimation
     ($15/Mtok in, $75/Mtok out — adjust constants if needed).
4. `.gitignore` (new at plugin root) excludes `.specfs-metrics.jsonl`
   and the standard Python build artifacts.

#### Configuration (env vars)

- `SPECFS_METRICS_LOG=<path>` — override default log path. Default:
  `<repo_root>/.specfs-metrics.jsonl`.
- `SPECFS_METRICS_OFF=1` — disable telemetry entirely (CI / privacy).

#### Tests

`tests/test_metrics.py` (22 testpoints, all green):
- `estimate_tokens` baseline + Unicode behavior.
- `emit` writes JSONL, respects `SPECFS_METRICS_OFF`, never raises on
  hostile path.
- `_extract_input_tokens` first-match-wins on TEXT_PAYLOAD_KEYS;
  returns 0 for unrelated kwargs.
- `_extract_output_tokens` sums LLM_ROUND_TRIGGER_KEYS, handles
  non-dict.
- `install_metrics` records: ordinary tool / LLM-round trigger /
  LLM-ingest tool / exceptions / **positional-arg session_id**
  (via `inspect.signature.bind_partial`).
- `install_metrics` idempotent.
- `read_log` returns events in order, skips malformed lines, handles
  missing file.
- `aggregate` rolls up per-session, counts errors, supports no-filter
  cross-session view, computes `llm_total_gap_s` correctly.
- End-to-end smoke: real `specfs_server.session_start` writes a real
  metrics event.

Suite total: **165 passed in 0.72 s** (was 143 in 0.5.2).

#### CLI usage

```
$ uv run python specfs_metrics_report.py
=== specfs-port metrics ===
log: /repo/.specfs-metrics.jsonl
sessions: 3
total events: 142
--
[session abc123def456]
  MCP tool calls : 27
  MCP duration   : 1.342 s  (avg 0.050 s, max 0.310 s)
  LLM rounds     : 8
  LLM input tok  : 31420 (~$0.4713 @ Opus)
  LLM output tok : 4280  (~$0.3210 @ Opus)
  LLM round-trip : 142.4 s  (avg 17.8 s, slowest 38.1 s)
  Errors         : 0
  Top tools:
    code_gen_submit         : 6 (0.260 s total)
    inject_diagnostics      : 5 (0.041 s total)
    code_gen_start          : 1 (0.150 s total)
```

Or machine-readable: `--json` flag emits per-session aggregate dict.

#### Modified

- `server/specfs_server.py` — imports `install_metrics`, `read_log`,
  `aggregate as metrics_aggregate`. Calls `install_metrics(mcp)` right
  after `install_default_timeout(mcp)`. Adds new `metrics_summary` MCP
  tool returning the aggregated counters.
- `.claude-plugin/plugin.json` — version 0.5.2 → 0.5.3.

#### Added

- `server/_metrics.py`
- `server/specfs_metrics_report.py`
- `server/tests/test_metrics.py`
- `.claude/plugins/specfs-port/.gitignore`

#### Why now

Repeated user complaints about long codegen rounds (the 卡死
incidents that motivated 0.5.2 timeout). With timing data in hand,
operators can:
- Bisect which tool / LLM round is the bottleneck (vs guessing).
- Set `SPECFS_DEFAULT_TIMEOUT_S` based on actual p95/p99.
- Spot tool-error rates across sessions.
- Cost-track Opus-vs-Sonnet usage decisions.

#### Known caveats

- Token estimates are char/4 heuristic — off ~±15% on Chinese-heavy
  text. Acceptable for telemetry; do NOT use for billing decisions.
- Background-thread timeout returns (from 0.5.2) DO write a metrics
  event because the wrapper's `finally` clause runs synchronously
  when the Future returns control. The event reflects wrapper
  wall-clock (close to budget), not the actual fn's runtime.
- The `metrics_summary` MCP tool itself emits an event on every call,
  so subsequent reads see one extra `metrics_summary` row. Tagged
  `is_llm_round_trigger=False / is_llm_ingest=False` so it doesn't
  distort LLM-side counters — just `mcp_per_tool["metrics_summary"]`.

## [0.5.2] — 2026-05-07

### Added — MCP-tool timeout / hang-protection (user-reported workflow stalls)

User directive 2026-05-07:
> "之前使用 mcp 生成代码经常出现卡死的情况，请为 mcp 调用增加超时返回机制"

Two-layer defense against subprocess / I/O stalls that previously froze
the LLM end of the conversation indefinitely.

#### Layer 1: subprocess timeouts on git operations

- `_git_sha`: added `timeout=10`. Without it, a stale `.git/index.lock`
  from a crashed prior process or a slow NFS-mounted `.git/` would block
  forever (witnessed during one Wave B session: 30+ min hang on a
  partial-write `index.lock`).
- `_git_add`: added `timeout=10`. Same failure mode — `git add --` blocks
  on the index lock with no way for the caller to recover.
- Both also tightened the exception clause from bare `except Exception` to
  `(subprocess.SubprocessError, OSError)` so timeout-related errors are
  caught in the same swallow-and-no-op path as the original error
  handling, but unexpected exceptions still bubble up.

#### Layer 2: process-wide MCP-tool timeout decorator

- New `server/_timeout.py` (~115 LOC) with:
  - `with_timeout(seconds)` decorator: runs wrapped fn in a daemon
    `ThreadPoolExecutor` and returns a structured error dict
    (`{_specfs_error: "timeout", tool, budget_s, hint}`) on overrun.
    Background thread keeps running but caller is unblocked.
    `functools.wraps` preserves metadata; exposes `__wrapped__` and
    `__specfs_timeout_s__` for introspection / testing.
  - `install_default_timeout(mcp, default_seconds=30)`: monkey-patches
    `mcp.tool` so every existing `@mcp.tool()` registration is wrapped
    transparently — zero per-tool edits in `specfs_server.py`. Idempotent
    (tagged with `__specfs_patched__`).
  - Per-tool override via `@mcp.tool(timeout=N)`. The patched function
    pops `timeout=` from kwargs before delegating to FastMCP so the
    framework doesn't see an unknown kwarg.
- Default budget: **30 s** (chosen for headroom on `*_gen_approve` flows
  that touch git + Makefile + main.c on a large repo). Configurable via
  `SPECFS_DEFAULT_TIMEOUT_S` env var.
- Per-tool overrides:
  - `run_build_kernel`: `timeout=620` (≈ internal 600 s subprocess timeout
    + 20 s wrapper margin).
  - `validator_run_holistic`: `timeout=920` (≈ 900 s + 20 s margin).
  - All 35 other tools: 30 s default.
- Worker pool size 4 (configurable via `SPECFS_TIMEOUT_WORKERS`); daemon
  threads vanish on process exit.

#### Behavior on timeout

The wrapped tool returns to the LLM as:
```json
{
  "_specfs_error": "timeout",
  "tool": "<fn_name>",
  "budget_s": 30,
  "hint": "Tool exceeded its budget. Background work may still be in
           progress; consider session_end() and retry with smaller
           scope, or override the budget via SPECFS_DEFAULT_TIMEOUT_S
           env var."
}
```

The LLM can pattern-match `_specfs_error` to decide whether to retry, end
the session, or surface the failure to the user. Background thread keeps
running (Python cannot safely kill threads); for idempotent work this
is fine, for I/O-heavy work the worst case is a partial-write the next
round overwrites.

Exceptions raised inside the wrapped function still propagate normally —
**only wall-clock budget overrun** is converted to a dict.

### Tests

- New `tests/test_timeout.py` (10 testpoints):
  - Pass-through on success (no schema change).
  - Structured error dict on overrun + actually returns within budget.
  - Exceptions propagate unchanged.
  - `functools.wraps` preserves `__name__` / `__doc__` / introspection attrs.
  - kwargs preserved.
  - `install_default_timeout` is idempotent.
  - `install_default_timeout` default budget applied to registered tools.
  - Per-tool override works.
  - `timeout=` kwarg stripped before FastMCP delegation.
  - Smoke test: real `specfs_server` tools have the timeout attribute,
    `run_build_kernel`/`validator_run_holistic` carry their overrides.
- Suite total: **143 passed in 0.72 s** (was 133/0.49 s in 0.5.1).

### Modified

- `server/specfs_server.py` — imports `install_default_timeout`, calls it
  right after `mcp = FastMCP("specfs")`. `_git_sha` / `_git_add` get
  explicit `timeout=10`. `run_build_kernel` decorator becomes
  `@mcp.tool(timeout=620)`, `validator_run_holistic` becomes
  `@mcp.tool(timeout=920)`. No other tool sites touched (default 30 s
  applies via the patch).
- `.claude-plugin/plugin.json` — version 0.5.1 → 0.5.2.

### Why now (vs leave as-is and warn users)

Repeated user-reported stalls during Wave B code generation. Previous
mitigation was "ctrl-c the MCP server, restart" — destructive, loses
session state, frustrating. The structured timeout dict lets the LLM
recover gracefully without operator intervention.

The 30 s default was calibrated against measured `code_gen_approve`
durations on this repo: typical 2-5 s, p99 ≈ 8 s, so 30 s is ~6× p99
headroom. If a real workflow regresses to >30 s due to repo growth,
operators can bump `SPECFS_DEFAULT_TIMEOUT_S` per-session without code
changes.

### Known caveats

- Threads keep running after timeout. For tools that mutate state
  (Makefile delta, common.header sync), a timed-out write may leave a
  partial file that the next retry overwrites. Acceptable risk for the
  hang-prevention benefit; tracked as a paper cut.
- `pyright` flags the new `_timeout` module import as unresolved
  because it doesn't introspect the venv path. Runtime works. Adding
  `pyrightconfig.json` is deferred until a separate cleanup pass.

## [0.5.1] — 2026-05-07

### Added — pytest test suite covering the full MCP tool surface (L3)

User directive 2026-05-07:
> "请使用 plugin-dev 为 specfs-port 插件完成测试用例"

Added `server/tests/` with 133 testpoints across 8 files, exercising the
plugin from manifest down to MCP tool returns. Test plan was scoped via
plugin-dev:plugin-structure guidance to L3 (full coverage including MCP
end-to-end smoke). Runtime: ~0.5 s on local Apple Silicon.

#### Test inventory

| File | Testpoints | Coverage |
|---|---|---|
| `test_state.py` | 8 | Session defaults, layer_retries 7-key invariant, FailureRecord dataclass, repo_root resolution |
| `test_prompts.py` | 23 | Template loading + cache, comment stripping, drop_empty_sections, substitute, filter_common_header_by_symbols, extract_rely_symbols, every assemble_*_prompt golden path + failure rendering edge cases |
| `test_extract.py` | 12 | C function/extern/FSMAP_ENTRY/pub_global/LOSCFG guard extraction, **v0.3.4.1 block-comment regex fix regression test**, render_interface_summary, collect_all_symbols |
| `test_dag.py` | 19 | empty/load/save/find/add/walk_ancestors/walk_descendants/is_*_approved/collect_invariants/mark_dirty_cascade/list_dirty + atomic-write + schema-mismatch reject |
| `test_commit_node.py` | 11 | parse_invariants from spec, parse_exports_arg, cmd_spec/cmd_code/cmd_show CLI commands, full spec→code commit happy path |
| `test_drivers.py` | 4 | _driver_loop_a + _driver_loop_b smoke (assembled prompt size, missing-node failure path) |
| `test_mcp_tools.py` | 29 | Session lifecycle, all 4 toggle_* flags, Loop A end-to-end (start→submit→approve writes file + commits DAG node), Loop B end-to-end, code_gen_refine, inject_diagnostics retry-counter increment + failure recording, dag_get/dag_extract_invariants/dag_check_node_complete/dag_revert, fragment fetch (known + unknown), prompt_override write+read, record_clarification, has_unresolved_ambiguity |
| `test_manifest.py` | 27 | plugin.json schema (name/version/keywords/no-bundles), .mcp.json portability (${CLAUDE_PLUGIN_ROOT} present, no /Users/ leak), bundled SKILL.md frontmatter, all 3 commands have frontmatter, all 12 prompt fragments present |

#### Added files

- `server/tests/__init__.py`
- `server/tests/conftest.py` — `tmp_repo` fixture (creates spec/fs/testsuites
  shape under tmp_path + monkey-patches `state.repo_root` / `dag.repo_root`
  / `commit_node.repo_root` so DAG operations land in tmp), `fresh_session`,
  `sample_spec_text`, `sample_dag` (single-node DAG with mount-v1 approved),
  `reset_template_cache` (autouse, clears `prompts._TEMPLATE_CACHE` between
  tests), `reset_session_registry` (clears `specfs_server._SESSIONS`).
- `server/tests/test_*.py` — 8 test modules above

#### Modified

- `server/pyproject.toml` — added `[project.optional-dependencies].dev`
  with `pytest>=8.0`, `[tool.pytest.ini_options]` with testpaths/file-pattern
  config so `pytest` works zero-config from server/.
- `.claude-plugin/plugin.json` — version 0.5.0 → 0.5.1.

#### How to run

```
cd .claude/plugins/specfs-port/server
uv pip install -e '.[dev]'
uv run python -m pytest
```

Or equivalent:
```
cd .claude/plugins/specfs-port/server
pip install -e '.[dev]'
pytest
```

Result: `133 passed in 0.49s`.

#### Why now (vs ship without tests)

Two motivations:
1. **The MCP server is 2,836 LOC of Python and was completely uncovered.**
   Manifest-level changes (P1.2 → P1.3 → P1.4 → 0.5.0) cumulatively
   touched return-shape contracts and retry-budget keys. A test suite
   pins those contracts so the next refactor surfaces breakage at
   commit time, not at next-stage codegen time.
2. **Plugin-dev:plugin-validator agent already passed structural
   validation in 0.5.0** — but structural validity alone doesn't catch
   functional regressions (e.g., Wave A → Wave B "v0.3.2 advertised
   but not implemented" Layer T silent skip — a unit test for
   `code_gen_approve.next` would have caught it). 0.5.1 closes that
   gap with regression tests at every layer the validator can't reach.

#### Known caveats

- `test_drivers.py::test_loop_a_driver_assembles_prompt` runs against
  the REAL repo's spec/exfat/ tree (the driver scripts hardcode
  `parents[4]` for repo-root resolution; can't be tmp-pathed without
  module-level refactoring). All other 132 tests are tmp-pathed and
  hermetic.
- pyright reports "unused parameter" warnings on every fixture-receiving
  test function (tmp_repo, sample_dag, reset_session_registry). These
  are pytest fixtures whose effect is side-effects (monkey-patch +
  clear registries) — false positives.

## [0.5.0] — 2026-05-07

### Changed — bundle the methodology skill INSIDE the plugin

User directive 2026-05-07:
> "specfs-port skill 是 specfs-port 的内置插件，请将 skill 放入 plugin 目录中，
> 对插件整体使用 plugin-dev 插件进行重构"

The plugin now follows the canonical Claude Code plugin layout (per
`code.claude.com/docs/en/plugins-reference §Skills`): the `specfs-port`
methodology skill lives inside the plugin's `skills/specfs-port/` directory
and is auto-discovered via the standard `skills/<name>/SKILL.md` convention.

Pre-v0.5.0 layout:
```
.claude/plugins/specfs-port/  (plugin)
.claude/skills/specfs-port/   (separate top-level skill, declared via
                               plugin.json::bundles.skills)
```

v0.5.0 layout:
```
.claude/plugins/specfs-port/
├── .claude-plugin/plugin.json
├── .mcp.json
├── prompts/
├── commands/
├── server/
└── skills/
    └── specfs-port/                 (auto-discovered, no manifest entry needed)
        ├── SKILL.md
        └── references/
```

### Modified

- **Moved (via `git mv`, history preserved):**
  - `.claude/skills/specfs-port/SKILL.md` → `.claude/plugins/specfs-port/skills/specfs-port/SKILL.md`
  - `.claude/skills/specfs-port/references/*` → `.claude/plugins/specfs-port/skills/specfs-port/references/*`
- **Removed:** the now-empty `.claude/skills/specfs-port/` and `.claude/skills/`
  directories.
- **`.claude-plugin/plugin.json`:**
  - Dropped `bundles.skills` field — auto-discovery handles it; the field is
    not in the official plugin manifest schema (see `plugin-dev:plugin-structure`
    skill output).
  - Bumped `version` from `0.3.4.2` → `0.5.0` to mark the structural refactor.
  - Description rewritten to (a) mention the bundled skill at `skills/specfs-port/`,
    (b) reflect P1.4 layered defense ordering (LSP / style / SpecEval / build+cmocka+QEMU / user review).
- **`README.md`:** file-tree diagram rewritten to show the v0.5.0 layout (skill
  inside plugin), with all paths now relative to the plugin root.
- **`skills/specfs-port/SKILL.md`:** "与 specfs-port 插件的关系" table re-anchored
  on relative-to-plugin-root paths; "自动加载" paragraph updated to point at the
  plugin reference §Skills convention.

### Validated

- `git mv` preserves history; `git status` shows 8 renames (1 SKILL.md + 7 references).
- Plugin manifest is valid JSON (matches plugin reference §"Plugin Manifest").
- `.mcp.json` already used `${CLAUDE_PLUGIN_ROOT}` since v0.3.0 — no portability
  fixup needed.
- No path-reference cleanup needed in `commands/`, `prompts/`, or `server/` — the
  intra-plugin references in those files were already plugin-root-relative or
  used `${CLAUDE_PLUGIN_ROOT}`.

### Migration notes

- DAG state unchanged — `spec/<module>/.specfs.dag.json` files load identically.
- Operators who had `.claude/skills/specfs-port/` checked into git will see the
  rename in their next pull; no manual action required.
- Anyone with hardcoded references to `.claude/skills/specfs-port/...` in custom
  scripts or notes should update to `.claude/plugins/specfs-port/skills/specfs-port/...`
  (the auto-discovery means most uses don't need a path reference at all).

### Why now (vs leave as separate top-level skill)

Two reasons surfaced together:
1. The plugin and skill always co-evolved — every release moved both in
   lock-step (P1.2 / P1.3 / P1.4 each touched both directories). Coupling
   them physically removes the "did I forget to update one of them?"
   failure mode that bit the v0.3 → P1.2 transition.
2. Distributing the plugin as a single tarball (for users who don't have
   project-local `.claude/skills/` discovery) now ships the methodology
   alongside the tooling without requiring two install paths.

## [P1.4] — 2026-05-07

### Changed — full pipeline reorder (5 simultaneous topology moves)

User directive 2026-05-07:
> "请将 specfs-port-spec loop A 中生成 unittest 测试放到 loop B spec 生成 C
> 代码之后；执行 unittest 单元测试放在 build 之后 qemu 冒烟测试之前；1b
> liteos-A style audit 合并到 1a lsp compile 中；specEvaluator 放在 lsp 检查
> 之后 build 之前；SpecValidator 和 2 build + qemu smoke 合并"

Five concurrent topology changes consolidate the pipeline that had grown
five distinct layers (1a / 1b / 2 / 3 / T + holistic Step 9 SpecValidator)
into three auto layers + 1 HITL, with cleaner cheap-first ordering:

1. **Layer T cmocka test gen moves Loop A → Loop B (Step 3a).**
   Tests now generate IMMEDIATELY after the C code, against real generated
   symbols. Removes a class of test/code drift bugs where the spec promised
   a helper that the code chose not to expose. Trigger: between Step 3
   codegen and Step 4 Layer 1a (instead of post-spec_gen_approve).

2. **cmocka exec moves into Layer 2 between build and QEMU.**
   Per-stage Layer 2 now runs build → cmocka unit tests → QEMU smoke as a
   single shared retry budget (3 rounds). Cheap regressions fail fast
   before the expensive QEMU pass. Failing testpoint generated this round
   → loops to Step 3a (test regen); failing pre-existing testpoint →
   loops to Step 3 (code regen).

3. **Layer 1b style audit folds back into Layer 1a as a SEQUENTIAL second
   sub-step.** P1.2 had promoted style to a sibling of compile; P1.4
   collapses the sibling back inside Layer 1a (4.1 LSP → 4.2 style)
   while keeping retry budgets separate (LSP 4 / style 5). Diagnostic
   sources stay crisp (LSP error vs. style violation never mix in the
   same retry round) but the layer count drops back to pre-P1.2.

4. **Layer 3 SpecEvaluator moves BEFORE Layer 2.** Spec-conformance
   defects are cheap to catch (one LLM round) and expensive to bury under
   build/QEMU iteration. Reordering so SpecEval gates first cuts retry
   cycles when the codegen drifts from the spec.

5. **Holistic SpecValidator merges into Layer 2.** The former `Step 9
   validator_run_holistic` (build + cmocka + QEMU) at module-completion
   time is now folded into the per-stage Layer 2 contract. The
   `tools/regress/run_all.sh` aggregator still exists for manual / CI
   runs (and is reminded at module-completion if `--no-regress` is not
   set), but per-stage Layer 2 is the in-loop gate.

Final pipeline (Loop B, per stage):

```
Step 3   gen C code
Step 3a  Layer T  cmocka test gen (≤ 3)
Step 4   Layer 1a sequential: 4.1 LSP compile (≤ 4) → 4.2 style audit (≤ 5)
Step 5   Layer 3  SpecEval — spec conformance only (≤ 8)
Step 6   Layer 2  unified: 6.1 build → 6.2 cmocka exec → 6.3 QEMU smoke (≤ 3)
Step 7   Layer 4  user review (code + test together)
```

### Modified

- `commands/specfs-port-code.md` — full rewrite of Steps 3-9 to reflect new
  ordering: Step 3a Layer T inserted; Step 4 Layer 1a now sequential
  (4.1+4.2); Step 5 Layer 3 promoted; Step 6 Layer 2 unified with cmocka
  in the middle; Step 9 demoted from holistic SpecValidator to a
  module-completion regression-suite reminder.
- `commands/specfs-port-spec.md` — Layer T section removed entirely;
  spec_gen_approve no longer fires test gen. Replaced with note pointing
  users to `/specfs-port-code` Step 3a.
- `server/state.py` — `layer_retries` retains all keys (compile / style /
  build / qemu / speceval / test_gen / spec_fine); comments updated to
  reflect the new ordering. `style_audit_enabled` and `speceval_enabled`
  docstrings updated; no functional change to defaults.
- `prompts/validation_checklist.md` — §7 reordered (Layer 1a.1 → 1a.2 →
  Layer 3 → Layer 2 build → Layer 2 cmocka → Layer 2 QEMU); §8 added
  for Layer T status.
- `DESIGN.md` — §2.2 Loop B pipeline diagram fully rewritten; §7 defense
  table re-ordered + Layer 2 cell updated to "build + cmocka exec + QEMU
  smoke = SpecValidator"; §8 flag list updated for `--style-off` /
  `--test-off` / `--no-build` / `--no-regress` semantics.
- `README.md` — `--style-off` / `--test-off` / `--no-build` / `--no-regress`
  flag descriptions rewritten; workflow ASCII art shows Step 3 → 3a → 4
  → 5 → 6 → 7 sequence with explicit Layer references.
- `.claude/skills/specfs-port/SKILL.md` — frontmatter description rewritten
  to enumerate P1.4 ordering; "防御层次" section body rewritten to show the
  new Step ladder; "完成后向用户输出的报告" section split per-stage Layer
  2 cmocka exec from module-completion regression aggregator.

### Migration notes

- DAG nodes carry no Layer 1a/1b discriminator — both wrote
  `code.validations_passed.style: true|false`. Existing nodes load fine.
- `specfs.validator_run_holistic(module=...)` MCP tool unchanged in
  signature; it is now invoked per-stage from Layer 2 (sub-step 6.2 +
  6.3) AND remains available for manual / CI run via
  `tools/regress/run_all.sh`.
- Operators with `--style-off` / `--test-off` / `--speceval-off` /
  `--no-build` / `--no-regress` in muscle memory: all flags work as
  before; only `--style-off` and `--test-off` had their referenced
  layer position move (they now skip a sub-step rather than a top-level
  layer).
- Layer 2 retry budget is now SHARED across build / cmocka / QEMU
  sub-steps (was 3 retries each in pre-P1.4 wording, but the holistic
  pass already shared one budget; this just formalizes the per-stage
  contract).

### Why now (vs leave as P1.2 + P1.3)

Cumulative experience with Wave A + Wave B:
- The Layer 1b sibling cost an extra layer-pivot per codegen round
  without payoff — diagnostic source already had `source=lsp|style`
  attribution, so a sequential pass through Layer 1a captures the same
  signal with one fewer top-level retry counter to reason about.
- Multiple stages caught spec-conformance bugs only after a successful
  build + clean QEMU smoke, then had to re-run all of Layer 2 after a
  spec_fine round. Promoting Layer 3 to before Layer 2 ends that
  rework.
- The holistic Step 9 SpecValidator was effectively duplicating Layer 2
  for the LAST stage of every module, while non-last stages had cheaper
  Layer 2 coverage. Folding holistic into per-stage Layer 2 spreads the
  cost evenly and removes the "module-last special case" branch.
- Layer T fired post-spec_gen_approve was correct in theory but in
  practice the test referenced spec-abstraction symbols that the
  codegen later renamed or omitted. Moving Layer T to immediately after
  Step 3 codegen lets the test reference real generated names —
  catching test/code drift at the cheapest possible moment.

## [P1.3] — 2026-05-07

### Removed — gcc -fsyntax-only fallback dropped from Layer 1a

User directive 2026-05-07: "Tier 1 中去掉 gcc fsyntax-only 检查, 只保留 clangd
LSP". Layer 1a (compile) is now LSP-exclusive. Rationale:

- **Stub-drift was chronic.** `_ensure_compile_stub` hand-maintained a
  ~60-line stub header (`Vnode`, `Mount`, `LosMux`, `LOS_MemAlloc`, etc.)
  that had to be extended every time a new LiteOS-A type touched an FS
  file. clangd via OMC LSP reads the repo's real `.clangd` config and
  sees the actual headers — zero drift.
- **The fallback was always second-class.** The "preferred path" was
  already LSP via `inject_diagnostics(layer="compile", source="lsp")`;
  the gcc tool only ran when LSP was missing. Modern dev setups + CI
  images all ship OMC LSP. Hard prerequisite simplifies the contract.
- **Two retry budgets confused the gate semantics.** Some failures
  produced `source=lsp`, others `source=gcc` — both used `layer=compile`
  but had different stub-vs-real signal quality. Operators couldn't
  tell whether a clean Layer 1 meant "real headers said OK" or "stub
  said OK".

### Removed code

- `server/specfs_server.py::run_compile_check` (43 LOC, was an `@mcp.tool()`)
  — deleted. Layer 1a is now driven entirely by caller-side LSP +
  `inject_diagnostics(layer="compile", source="lsp", payload=...)`.
- `server/specfs_server.py::_ensure_compile_stub` (62 LOC) — deleted.
- `server/_stubs/specfs_stub.h` — moved to `backup/.claude/plugins/specfs-port/server/_stubs-removed-P1.3/`.

### Modified

- `server/specfs_server.py` — `_passed_layers` "compile" comment now
  "clangd LSP only (P1.3 dropped gcc fallback)".
- `server/state.py` — `STAGE_NAMES` and `Session` docstrings updated.
- `server/prompts.py` — `assemble_style_audit_prompt` docstring no
  longer references `gcc -fsyntax-only` (only LSP).
- `commands/specfs-port-code.md` — Active layers footer note + the
  "Removed in v0.4" bullet now read "P1.3 fully removed".
- `DESIGN.md` — §4.4 MCP tool table drops `run_compile_check`; §6
  layer-pipeline diagram drops gcc fallback; §7 defense table updates
  Layer 1a column to "clangd via OMC LSP, no fallback"; §6
  `[Modification suggestions]` source list drops `<source: compile (gcc)>`.
- `README.md` — workflow ASCII art and prerequisite list updated.
- `prompts/validation_checklist.md` — §7 Build status checklist line
  "Layer 1 gcc -fsyntax-only" replaced with "Layer 1a clangd LSP".
- `.claude/skills/specfs-port/SKILL.md` — frontmatter description and
  body "防御层次" section now read "Layer 1a [P1.3 起 clangd LSP-only]".

### Migration notes

- DAG nodes carrying `code.validations_passed.compile: true` keep the
  same shape — just the underlying signal source narrows from
  "lsp-or-gcc" to "lsp-only".
- Existing sessions: `state.layer_retries["compile"]` keeps its semantics.
- Operators relying on the gcc fallback in offline / no-LSP environments:
  install OMC LSP via `oh-my-claudecode:mcp-setup`. There is no longer
  a graceful degradation path.

### Why now (vs let it fade)

Two consecutive sessions hit the stub-drift wall: Wave B Stage 4d added
`exfat_inode_info.fs_fmask` + `VnodeOps.Create` slot, both unknown to
the stub. Each forced a stub edit before the gcc path would even run,
yet LSP reported the same error in seconds without intervention.
Keeping a fallback whose maintenance cost > its no-LSP coverage value
was no longer defensible.

## [P1.2] — 2026-05-07

### Changed — Layer S split back out of Layer 3 SpecEval, repositioned as Layer 1 sibling

P1.1 (v0.4) folded the v0.3 Layer S coding-style audit into Layer 3
SpecEvaluator as a single LLM round (one `is_good/comments` JSON covering
both spec conformance and LiteOS-A style). Real-world use exposed two
problems with the merged form:

1. **Mixed feedback was hard to action.** A single `comments` blob
   interleaved style nits ("VFS callback returned positive errno") with
   spec violations ("Phase 2 ran under s_lock — violates `## Refine
   Prompt`"). Operators had to triage two unrelated severities at once
   and the codegen LLM frequently fixed style first, leaving the harder
   spec violation in the next round's `comments`.
2. **Style failures should fail-fast at compile tier.** Style is a
   mechanical/syntactic check (libsec usage, naming, error-code sign).
   Spec conformance is semantic and depends on the full spec context.
   Forcing them to share a retry budget (8 in P1.1) made cheap fixes
   wait behind expensive ones.

P1.2 splits them apart:
- **Layer 1a (compile)** stays as before — clangd LSP preferred, gcc
  -fsyntax-only fallback. Max 4 retries.
- **Layer 1b (style)** is reinstated as a STANDALONE layer at the SAME
  tier as Layer 1a (sibling, runs in parallel). Max 5 retries. Rule canon
  in `prompts/style_rules.md`; LLM template in `prompts/style_audit.md`.
  Both 1a and 1b must pass before Layer 2.
- **Layer 3 (SpecEval)** is now spec-conformance ONLY. Style rules are
  NOT inlined and NOT injected via `{STYLE_RULES}`. Max 8 retries
  (paper). Reduces false-positive style flags from spec-focused review.

### Modified

- `prompts/speceval.md` — removed the inline `## LiteOS-A style` bucket
  (11 items). Now only enumerates 6 spec-conformance check items. Adds
  explicit "what NOT to flag" guard: "if you flag a style issue here it
  will be a false positive".
- `prompts/style_audit.md` — unchanged; reused as-is by the reinstated
  Layer 1b assembler.
- `server/prompts.py::assemble_speceval_prompt` — reverted to 2-arg
  signature `(generated_code, original_spec)`; no `{STYLE_RULES}`
  placeholder.
- `server/prompts.py::assemble_style_audit_prompt` — restored. Docstring
  updated to reflect P1.2 positioning ("sibling of compile, NOT a serial
  step after compile").
- `server/state.py::style_audit_enabled` — restored. Comment updated to
  "sibling of compile" framing.
- `server/state.py::layer_retries` — restored "style": 0 default; now
  documented as Layer 1b not Layer S.
- `server/specfs_server.py::_passed_layers` — restored "style" key,
  reflects Layer 1a/1b sibling topology in the docblock.
- `server/specfs_server.py::inject_diagnostics` — restored "style" as a
  valid `layer` value alongside "compile".
- `commands/specfs-port-code.md` — Active layers table now shows Layer
  1a + 1b as siblings; explicitly notes "P1.2 reinstated style as
  standalone, was briefly folded into Layer 3 in P1.1; reverted because
  combined comments mixed nits with spec violations".
- `prompts/unittest_gen.md` — comment updated to say "test_gen 与 Layer
  1/2/3 解耦, 由 Loop A spec_gen_approve 触发".
- `DESIGN.md` — §1, §6 (layer pipeline diagram), §7 defense table, §8
  flag list, §10 retry budgets all renumbered: Layer S → Layer 1b sibling.
- `.claude/skills/specfs-port/SKILL.md` — frontmatter description and
  body "防御层次" section updated to 6-layer naming with Layer 1a/1b
  siblings.
- `README.md` — `--style-off` description rewritten to reference the
  P1.2 split (Layer 1b standalone, no longer Layer S serial-step).

### Migration notes

- No state migration needed. Existing sessions: state.style_audit_enabled
  and layer_retries["style"] keep their meaning; only the surrounding
  docs/comments changed framing.
- DAG nodes carry no Layer S/Layer 1b discriminator — both wrote
  `code.validations_passed.style: true|false`. Existing nodes load fine.
- Operators with `--style-off` in muscle memory: flag still works, still
  default ON. Behavior unchanged (skips Layer 1b audit). What changed is
  what `--style-off` actually skips: a standalone audit pass, not part of
  Layer 3.

### Why now (vs leave as P1.1 fold)

User directive 2026-05-07: "从 specEval 去除风格审计，风格设计移到 LSP
同一层级"  — explicit request to physically separate the two passes and
position style at compile-tier. The P1.1 merge was originally motivated
by token-cost economy (one LLM call instead of two); P1.2 accepts the
extra call cost as the price of clean separation between mechanical
(style) and semantic (spec) gates. With Wave B mkdir + create both
landing in the same week, the "what's blocking the merge?" question
became hard to answer when style nits and spec drift came back in one
JSON blob — splitting reduces that ambiguity at the cost of one extra
LLM round per codegen retry.

## [0.3.4.2] — 2026-05-05

### Fixed — two recurrent bugs in `_apply_makefile_delta` and `_sync_common_header`

Both surfaced once per stage during Wave B (Stages 2a–2e). Manual workarounds
(re-route `test_<stage>.c` from OBJS into HARNESS_SRCS, `python3` truncate at
duplicate `auto-synced exports` markers, then append clean stage exports)
were applied 5×. v0.3.4.2 fixes the root causes so future stages don't need
the workaround.

- **`specfs_server.py::_apply_makefile_delta` — continuation-line regex
  slurped past blank lines.** The pattern
  `(HARNESS_SRCS\s*:=[^\n]*(?:\n\s+[^\n]+)*)` used `\s+` for the leading
  indent of each continuation line. Because `\s` matches newlines (`\n`,
  `\t`, ` `, etc.), the inner alternation `\n\s+[^\n]+` would consume a
  blank line plus the *next* assignment line whole — typically the
  `OBJS := $(PROD_SRCS:.c=.o) $(HARNESS_SRCS:.c=.o)` line that immediately
  follows the HARNESS_SRCS block. The captured "block" thus extended into
  OBJS, and the `test_<stage>.c` continuation entry was inserted at end-of-OBJS
  instead of end-of-HARNESS_SRCS. Result: `make` would see `test_<stage>.c`
  as a phantom OBJS member with no compilation rule.

  Fix: replace `\s+` with `[ \t]+` so continuation indent is detected by
  spaces/tabs only, never newlines. Verified via Python repro: OLD regex
  captures `HARNESS_SRCS := ... test_b.c\n\nOBJS := ...`; NEW correctly
  stops at `test_b.c`.

- **`specfs_server.py::_sync_common_header` — `auto-synced exports` marker
  duplicated per stage.** Each successful `code_gen_approve` unconditionally
  emitted a fresh `/* auto-synced exports — appended by specfs-port
  code_gen_approve */` comment block plus the new decls, leading to N
  markers and N decl blobs after N stages. Visual noise plus duplicate-decl
  risk if two stages exported overlapping symbol names (the existing
  `decl not in existing` check is whitespace-sensitive but the per-stage
  marker amplification was the dominant pollution).

  Fix: emit the marker singleton — if it already exists in the file, splice
  new decls in immediately after the marker line (preserving previous-stage
  decls below). If absent, append `\n\n<marker>\n<decls>` at end-of-file as
  before. Verified via standalone Python harness: 3 successive sync calls
  → 1 marker / 3 collected decls.

### Why this matters

Stages 2a–2e each cost ~30 seconds of manual cleanup. Stage 3 (truncate VOP)
*also* triggered both bugs. With v0.3.4.2 the pipeline stops needing the
human-in-the-loop fix — `code_gen_approve` should land a clean Makefile
delta and a clean common.header sync on the first try.

### Validated

- Bug A: Python repro confirmed OLD regex eats OBJS line; NEW does not.
- Bug B: Python repro confirmed marker count=1 after 3 sync calls.
- Both fixes are isolated to `specfs_server.py`; no behavioural change for
  any path that doesn't trigger these specific code paths.

## [0.3.4.1] — 2026-05-04 (afternoon)

### Fixed — two bugs surfaced by v0.3.4 first native Layer T run

- **`extract.py::extract_from_text` — strip C comments before regex match.**
  `_FUNC_DEF_RE`'s `[\w*\s]+?` lazy quantifier could traverse multi-line
  block comments preceding a function and capture comment fragments as
  the return type; `\([^;{}]*\)` then greedily spanned multiple `(...)`
  pairs inside the comment as if they were the parameter list. Result was
  garbled extern decls in `common.header` like
  `extern * exfat_find_root_dentry * * FAT chain walk ... */ int exfat_find_root_dentry(...)`.

  Fix: new `_strip_c_comments(text)` helper that replaces `/* ... */`
  blocks with whitespace (preserving newlines so `^` anchors still match
  the right lines) and `// ...` line comments. Called at the top of
  `extract_from_text`. Verified via full sweep of `fs/exfat/`: 33 public
  functions across 20 files, **0 corrupted signatures** (was 7+ before
  the fix on the same input).

- **`specfs_server.py::_derive_code_path` — drop wrong upstream-Linux
  conventions, keep only the truly-shared mappings.**
  Old map sent `write/open/close → <module>_file.c` and `readdir →
  <module>_dir.c`, which is the upstream Linux `fs/exfat/` layout. Our
  LiteOS-A port splits per-stage: `exfat_write.c`, `exfat_open_close.c`,
  `exfat_readdir.c`, `exfat_lookup.c`, etc. The hint path therefore
  pointed to non-existent files for these stages.

  Fix: `SHARED_FILE_MAP` now contains ONLY:
  `mount/umount/statfs/sync → <module>_super.c` and
  `read → <module>_file.c`. Everything else falls back to
  `<module>_<stage>.c`. Verified against 10 exfat stages
  (mount / umount / lookup / read / write / readdir / open_close /
  vfs_ops_filled / chksum util / balloc bitmap), all map to the
  expected file. Note: `vfs_ops_filled` still maps to a non-existent
  `exfat_vfs_ops_filled.c` because the actual landing file is
  `exfat_attr.c` — the docstring now states explicitly that the path
  is a HINT only; the authoritative final location is the
  `code_gen_approve(files_to_save=[...])` argument.

### Why this matters

Bug (1) silently appended pseudo-extern lines on every `code_gen_approve`
call, polluting `spec/<module>/common.header` with junk that
collected over Wave A's 15 stages. The header had to be hand-cleaned
twice (commit `b8b27abc`, then again during the v0.3.4 first native
run) — explicit `git restore` after the run; the comment "hand-cleaned
of comment-fragment regex misfires" still sits in the file as evidence.
After 0.3.4.1, future `code_gen_approve` invocations should not need
manual cleanup of `common.header`.

Bug (2) was harmless because `code_gen_approve(files_to_save=[...])`
overrides the draft path. But the misleading hint cost time during
the v0.3.4 first run (saw `exfat_file.c` in the prompt, had to
mentally remap to `exfat_write.c`). After 0.3.4.1, the hint matches
reality for the common cases.

### Validated

- `python3 -m py_compile` clean on both modified files.
- Smoke: `extract_module_interface('exfat', ...)` returns 33 functions,
  0 with comment-fragment markers `(/* */ — *)`. Spot-checked all 20
  source files; signatures look clean.
- Smoke: `_derive_code_path` manually exercised on 10 spec paths
  (mount / umount / lookup / read / write / readdir / open_close /
  vfs_ops_filled / util / bitmap), all return the expected hint path.

## [0.3.4] — 2026-05-04

### Added — Layer T actually wired (closes v0.3.2 gap)

- **5 new MCP tools in `server/specfs_server.py`** (~290 LOC):
  `toggle_test_gen`, `test_gen_start`, `test_gen_submit`, `test_gen_refine`,
  `test_gen_approve`. Total tool surface: 33 (was 28).
- `code_gen_approve` now returns `{next: "test_gen"}` (instead of terminal
  `phase=approved`) when `test_gen_enabled=True` (default ON). Final code +
  files-saved cached on the session for Layer T re-use without re-reading
  from disk.
- `test_gen_approve` writes the cmocka file and **best-effort applies the
  Makefile + main.c deltas automatically**: regex-based append to
  `HARNESS_SRCS` and insertion of `extern` decl + `total += run_suite(...)`
  in main.c. Idempotent — second invocation with same `<stage>` is a no-op.
  Verified against the real `testsuites/unittest/exfat/` harness (14 existing
  externs detected, no double-add on existing stages).
- `prompts.assemble_unittest_gen_prompt(generated_code, original_spec,
  harness_layout)` — the `unittest_gen.md` template that has existed since
  v0.3.2 finally has a server-side caller.
- `dag.is_tests_approved(node)` helper. The DAG node schema gains an additive
  `tests` block (`{files, git_sha, approved_at, approval_iterations,
  testpoints, test_array_name, dirty}`). No schema_version bump — old nodes
  without `tests` continue to report `is_node_complete=True`, surfacing the
  gap separately as "tests-debt" via the new helper.
- `state.SessionPhase` adds `"test_drafting"`; `Session` adds
  `test_stage` / `test_draft_path` / `test_final_path` / `test_iterations` /
  `test_gen_enabled` / `code_final_text` / `code_final_paths`;
  `layer_retries` adds the `test_gen` slot (3 max per `DESIGN.md §10`).
- `--test-off` CLI flag in `commands/specfs-port-code.md` to opt out per
  session (e.g., write paths whose only verification is QEMU LTP).

### Changed — pipeline reorder (Step 11 was in the wrong place)

- `commands/specfs-port-code.md` rewritten: Layer T moved from old Step 11
  (post-approval) to new **Step 8.5**, between Layer 3 (SpecEval) and Layer 4
  (user review). The v0.3.2 prompt comment said this was the intent
  ("plugin runs Layer T after Layer S + Layer 3 pass, BEFORE user review")
  but the actual command body had it sequenced after Step 10 (handle approve
  response). Fixed in v0.3.4.
- Step 9 (user review) now displays code + cmocka test together. Step 10
  options expanded from 5 to 6: separate `Suggest test edits` (option c)
  branch that calls only `test_gen_refine` without re-running code generation.
- `argument-hint` updated; `description` says "7-layer defense" (was "6-layer").

### Why this matters — the v0.3.2 self-deception

v0.3.2 announced "Spec-derived cmocka test generation (default ON)" and
shipped the `prompts/unittest_gen.md` template, but **0 server-side tools
consumed it**. The slash command's Step 11 was a soft reminder, not a hard
gate. Result: Wave A's 9 stages (chksum, options, dentry, balloc, upcase,
fat_chain, nls_utf16, dentry_iter, inode_alloc, open_close, getattr_seek,
read, readdir, lookup) all skipped Layer T silently and accumulated test
debt — caught only in commit 149487a9 where 152 testpoints were back-filled
in one batch. v0.3.4 closes the loop so Wave B and forward cannot repeat
this failure mode.

### Validated

- All 4 modified `.py` files pass `python3 -m py_compile`.
- Static AST scan: 5 new MCP tools registered (`toggle_test_gen` +
  `test_gen_{start,submit,refine,approve}`); 33 total tools.
- End-to-end smoke: `assemble_unittest_gen_prompt` renders 3489-char
  prompt; `Session` exposes new fields; `is_tests_approved` correctly
  distinguishes legacy nodes (False) from v0.3.4-era nodes (True);
  Makefile delta is idempotent; main.c regex matches all 14 existing
  externs (no false negatives).

### DAG completeness — 4 finalization commits

- `ccd8060d` — Wave A 12 stages tests-layer reverse_filled (175 testpoints
  catalogued: chksum 8 / options 18 / dentry 10 / balloc 8 / upcase 5 /
  fat_chain 20 / inode_alloc 9 / dentry_iter 15 / nls_utf16 42 /
  lookup 12 / readdir 15 / read 13).
- `178d9258` — `open_close` code+tests double-layer reverse_fill (10
  testpoints; both layers were missing because the original commit
  predated v0.3.4 plumbing).
- `ee1feeaf` — `write` code-layer reverse_fill + Makefile `PROD_SRCS`
  appended `exfat_write.c`. Tests intentionally NOT reverse-filled —
  reserved for native Layer T flow.
- `f1961efd` — `vfs_ops_filled` (getattr+seek) code+tests double-layer
  reverse_fill (16 testpoints). Corrects task-#47 misjudgement that
  "spec missing"; the spec was at `exfat_vfs_ops_filled.spec` all
  along, only DAG layers were empty.

After this batch, every Wave A stage's DAG node has both code and tests
layers populated; `dag.is_node_complete` returns True for all 14 stages.

### First native Layer T run — `write` stage (cafcfccf)

- First end-to-end use of the v0.3.4 native flow (vs the 9-stage
  reverse_fill batch that established the historical baseline):
  `session_start → code_gen_start → code_gen_approve → test_gen_start →
   test_gen_submit → test_gen_approve`.
- `assemble_unittest_gen_prompt` produced a **53-KB prompt**: full
  `exfat_write.c` (~7 KB) + `exfat_write.spec` (~17 KB) + 9.3 KB frozen
  contract + 40 prior symbols + harness layout snapshot.
- LLM generated `testsuites/unittest/exfat/test_write.c` (16 testpoints
  covering all 7 spec Cases + 11 of 16 testable invariants); `test_gen_approve`
  auto-applied Makefile + main.c deltas without manual editing.
- Cmocka regression after merge: **15 suites / 217 testpoints, 0 failures**
  (was 14 / 201 before write).
- Build-fix found via Layer T: `host_stubs/disk.h` and `mock_disk.c` had
  a 5-arg `los_part_write` declaration whereas real
  `drivers/block/disk/include/disk.h:485` is 4-arg. The mismatch was
  benign until `exfat_write.c` joined `PROD_SRCS`; bundled into the same
  commit (`cafcfccf`) as the test artifact.

### Known v0.3.4 server bugs — RESOLVED in v0.3.4.1 (see entry above)

Surfaced during the first native run; both fail open (do not block the
flow) and were worked around manually. **Both fixed in v0.3.4.1**:

1. **`_sync_common_header` regex misfires** on multi-line block comments,
   appending pseudo-extern lines that quote comment fragments as part of
   the signature (e.g. `extern * are handled transparently ... */ int
   exfat_get_dentry_set(...)`). Manually `git restore`d after
   `code_gen_approve`. Root cause: the signature extractor in
   `prompts.py` / `extract.py` strips block-comment start `/*` but
   re-greedily consumes leading whitespace + inner `*` continuation
   lines into the next signature token. Fix candidate: tokenize on
   semicolons after stripping comments via a real C preprocessor pass,
   not a line-oriented regex.
2. **`_derive_code_path` mis-derives draft path**: spec
   `spec/exfat/interface/exfat_write.spec` resolves to
   `fs/exfat/exfat_file.c` rather than `fs/exfat/exfat_write.c`. Caused
   by sharing the read+write convention from upstream Linux (read+write
   in same `file.c`). Does not affect the final saved path because
   `code_gen_approve(files_to_save=[...])` overrides — but the draft
   path is misleading. Fix: derive from `stage_name` not from a hardcoded
   filename heuristic.

## [0.3.3] — 2026-05-01 (late evening)

### Changed

- **Layer 0 (LSP) merged into Layer 1 (compile gate).** Driven by user audit
 "lsp检查和gcc syntax 编译检查是否重复". Both layers were checking the same
 property — "does this translation unit pass type-check" — against different
 inputs. Layer 0 ran clangd via OMC LSP (real LiteOS-A headers via `.clangd`),
 Layer 1 ran `gcc -fsyntax-only` against a hand-maintained stub header.
 clangd is a strict semantic superset of gcc-against-stub, so the stub-based
 check could only catch a subset and carried drift risk as real headers evolve.
 - **New unified Layer 1 (compile gate)**: clangd via OMC LSP **preferred**
 (zero stub-drift, sees real types/macros), gcc -fsyntax-only with the
 bundled stub is the **automatic fallback** when LSP isn't reachable.
 - Single retry budget: **4 rounds** (was 3+3 = 6 in v0.3.2; merged & relaxed
 by 1 since LSP catches more findings per round).
 - Diagnostic source attribution: `inject_diagnostics(layer="compile",
 payload=<rendered>)` with the payload prefixed `source=lsp` or `source=gcc`
 so the LLM can tell which check fired.
- Dropped MCP tool `specfs.run_lsp_check` (was a stub). Removed `"lsp"` from
 `inject_diagnostics` allowlist and from `state.SessionPhase`/`layer_retries`.
- Step renumber in `commands/specfs-port-code.md`: old Step 5 (Layer 0) + Step 6
 (Layer 1) → new Step 5 (Layer 1 compile gate). Old Step 6.5 (Layer S) → Step 6.
- DESIGN.md §7 layer table, §10 retry budget, §4.4 layered defense API list
 all updated to reflect the merge.

### Validated

- gcc fallback path remains identical — no behavior change on hosts without
 OMC LSP. exFAT mount-stage code still passes Layer 1 with `ok=true`.

## [0.3.2] — 2026-05-01 (evening)

### Added

- **Spec-derived cmocka test generation (default ON).** New `prompts/unittest_gen.md`
 template + `commands/specfs-port-code.md` Step 11 — after Layer S and Layer 3
 pass, the plugin auto-synthesizes a `test_<stage>.c.draft` from the spec's
 `[SPECIFICATION]` Cases and Invariants. User reviews code + test together in
 Layer 4. On approval, the draft is renamed and Makefile/main.c are auto-updated.
 Driven by user directive about syncing test authorship into the HITL loop.
- **Step 12 regression-suite run reminder** (was Step 11 in v0.3.1) — explicitly
 separated from Step 11 (UT generation) so the workflow shows test → review →
 run as 3 distinct gates.

### Updated

- Skill `.claude/skills/specfs-port/` reorganized: detailed compile/debug
 procedure moved out of `SKILL.md` into 3 new `references/` docs:
 - `references/cmocka-host-harness.md`
 - `references/ltp-qemu-regression.md`
 - `references/fs-debug-recipe.md`
 Skill is now ~190 lines vs 375 — methodology only, with pointer references
 for operational detail. Driven by user directive "编译调试相关的具体步骤记录在
 参考文档而不是直接在 skill.md 中".

## [0.3.1] — 2026-05-01 (afternoon)

### Added

- **cmocka host unit-test recipe** in CLAUDE.md and bundled skill — concrete
 steps for adding `test_<stage>.c` testpoints when porting new FS stages
 (e.g. lookup, read). Driven by user feedback "遗漏了单元测试步骤".
- **LTP dynamic-linking lesson** — static glibc ARM ELFs (built with
 `arm-linux-gnueabi-gcc`) won't load on LiteOS-A (`OsLoadELFSegment` fails on
 PT_GNU_STACK / glibc-specific segments). Use OHOS clang
 (`armv7-unknown-linux-ohos-clang`) with `--sysroot=$OH_OUT/sysroot` and NO
 `-static` — links dynamically against `/lib/libc.so` and `/lib/ld-musl-arm.so.1`
 already in rootfs.

### Fixed

- **exfat umount panic** (data_abort far=0x4 in `VnodePathCacheFree`).
 Root cause: `g_exfatVops = { 0 }` left `Reclaim` NULL; VFS `VnodeFreeAll` →
 `VnodeFree` skipped Reclaim and the path_cache walk panicked. Fix: lazy-init
 `g_exfatVops.Reclaim = VfsExfatReclaim` in `VfsExfatMount`; the handler frees
 inode_info + destroys the inode lock + clears `vnode->data`. Mirrors fatfs
 ownership of FS-private data via Reclaim.

### Constraint added

- **No common-layer edits without permission** — debug FS bugs strictly inside
 `fs/<name>/`. `fs/vfs/`, `kernel/`, `syscall/`, `drivers/block/disk/` need
 explicit user OK. Use diagnostic `PRINT_ERR` logs to localize the bug;
 remove them after diagnosis.

## [0.3.0] — 2026-05-01

### Added

- **Layer S — coding-style audit (default ON).** New defense layer between Layer 1
 syntax check and Layer 2 build. 6 dimensions: naming / function complexity /
 code layout / memory & libsec / locking / error path. Output JSON
 `{is_good, score, summary, violations: [...]}`. Max 5 retries on style fail.
 See `prompts/style_audit.md` and `commands/specfs-port-code.md` Step 6.5.
 Driven by user directive "加入编码风格评估环节".
- `--style-off` CLI flag to opt out per session.
- `assemble_style_audit_prompt()` in `server/prompts.py`.
- `style_audit_enabled: bool = True` in `server/state.py`.
- `fs/exfat/.clang-format` — advisory style anchor for fs/exfat/* (informational
 hint feed for Layer S; not a hard gate due to LiteOS-A multi-space alignment
 idioms in fs/fat/*, fs/jffs2/* that clang-format can't preserve).
- **Bundled companion skill at `.claude/skills/specfs-port/`** — auto-loads with
 the plugin (project-local skill discovery). Carries over the methodology that
 was previously in `liteos-fs-port` plus the new Layer S + regression
 contracts. Plugin manifest declares `bundles.skills: ["specfs-port"]`.
- 4 reference docs copied into `.claude/skills/specfs-port/references/`:
 `specfs-format.md`, `liteos-vfs-mapping.md`, `liteos-fs-style.md`,
 `exfat-walkthrough.md`.
- DESIGN.md §7 layer table updated: Layer S inserted, Layer 3 marked default ON.
- Step 11 regression-suite reminder in `commands/specfs-port-code.md` (with
 `--no-regress` flag to skip).

### Changed

- **Plugin decoupled from `liteos-fs-port` skill.** All 8 internal references
 (README/DESIGN/3 prompt files) now say "specfs-port skill". Old
 `liteos-fs-port` skill remains for historical reference but new work goes
 through this plugin's bundled skill.

### Validated

- 49/49 cmocka host tests continue to pass after plugin edits.
- Layer S audit run on 7 stages: 6/7 `is_good=true`. Hard violation:
 `exfat_super.c::VfsExfatMount` is 271 lines (cap 150) — recorded in
 `docs/exfat_style_audit.md` for .1.

---

## [0.2.0] — 2026-04-30

### Added

- **Layer 3 SpecEvaluator self-audit defaults to ON.** Was opt-in via
 `--speceval-on`; user explicitly required self-audit before user review.
 Now opt out via `--speceval-off`.
- `prompts/speceval.md` template (verbatim port of paper's
 `gencode.py:200`).
- `assemble_speceval_prompt()` in `server/prompts.py`.
- `speceval_enabled: bool = True` in `server/state.py`.
- audit report at `docs/exfat_speceval.md` (7/7 stage `is_good=true`).

### Validated

- exFAT mount 7-stage DAG fully approved with SpecEval gate.

---

## [0.1.0] — 2026-04-29

### Added

- Initial plugin: HITL spec-first FS porting.
- Two loops: Loop A (Linux source → SYSSPEC spec), Loop B (spec → C code).
- Five-layer defense: Layer 0 LSP / Layer 1 compile / Layer 2 build+QEMU /
 Layer 3 SpecEvaluator (opt-in) / Layer 4 user review.
- DAG state model in `spec/<module>/.specfs.dag.json` with cross-stage
 invariant inheritance.
- Ask-first clarification before code generation.
- 25 MCP tools surfaced via `server/specfs_server.py`.
- 8 prompt fragments under `prompts/`.
- 3 slash commands: `/specfs-port`, `/specfs-port-spec`, `/specfs-port-code`.

### Validated

- exFAT mount path fully ported via this plugin (`fs/exfat/*.c`,
 spec/exfat/*.spec, 7 DAG stages, QEMU mount RC=0). See
 `docs/dev/exfat_mount.md` for the end-to-end log.
