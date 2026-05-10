---
description: Loop code — 从已批准 spec 生成 LiteOS-A C 代码 + cmocka 测试，按 6 步流水线 fail-fast / Generate LiteOS-A C code + cmocka tests from approved SYSSPEC; six-step fail-fast pipeline with single user-review at end
argument-hint: <spec-path> [--style-off] [--audit-off] [--no-build] [--test-off] [--prompt-override <file>] [--no-regress]
allowed-tools: ["Bash", "Read", "Write", "Edit", "AskUserQuestion", "Grep", "Glob", "Agent"]
---

# specfs-port-code — Loop code（SYSSPEC spec → LiteOS-A C 代码 + cmocka 测试）

User invoked: `/specfs-port-code $ARGUMENTS`.

> 防御层拓扑、retry 预算、各 Step 语义见
> [skills/specfs-port/SKILL.md §防御层次](../skills/specfs-port/SKILL.md#防御层次plugin-与-skill-共享的契约--defense-layer-topology)。
> 风格规则：[prompts/style_rules.md](../prompts/style_rules.md)；测试 harness：
> [skills/specfs-port/references/cmocka-host-harness.md](../skills/specfs-port/references/cmocka-host-harness.md)。
> 本命令只描述每个 Step 的**编排细节**——概念性描述去 SKILL。

## MCP tool naming compatibility

This command is shared by Claude Code and OpenCode through symlinks. Tool names differ only by separator:

- Claude Code form: `specfs.session_start`, `specfs.code_gen_start`, ...
- OpenCode form: `specfs_session_start`, `specfs_code_gen_start`, ...

Use the form exposed in the current runtime. If running in OpenCode, convert every `specfs.foo_bar` mention below to `specfs_foo_bar` before calling; do not attempt dot-form tool names. All examples below use the Claude Code form unless an OpenCode-specific name is required.

> **MCP 工具名 / MCP tool aliases**：Step 4 audit 在 server 上以一对名字暴露，
> 旧名（`toggle_speceval` / `enforce_speceval` / `record_speceval_verdict`）保留
> backward compat，新名（`toggle_audit` / `enforce_audit` / `record_audit_verdict`）
> 是首选。功能一致；本命令两套都接受。Session 字段名 `speceval_enabled` /
> `speceval_pending` 维持旧名（持久化兼容）。

You are running **Loop code** of the specfs-port plugin. Goal: produce approved
LiteOS-A C code AND its cmocka test from an approved spec, defended by
auto-retry steps + a single user review at the end.

## Active steps — 当前生效流水线

| # | Step | 触发位置 | 自动 / HITL |
|---|---|---|---|
| 1 | 生成 C 代码 (codegen) | spec 已批准 | LLM single shot；失败由后续 Step 反馈回此处 |
| 2 | 静态检查 + 内核 build | Step 1 完成后 | auto retry：2.1 LSP 4 / 2.2 style 5 / 2.3 build 3（独立预算） |
| 3 | cmocka 测试生成 + 测试编译 | Step 2 通过后 | auto retry：3 轮共享（gen + compile） |
| 4 | spec/code audit (合并 spec conformance + Linux 异构审计) | Step 3 通过后 | auto retry：3（`--audit-off` 跳过） |
| 5 | 运行时验证 (cmocka exec + QEMU smoke) | Step 4 通过后 | auto retry：3 轮共享（`--no-build` 跳过整层） |
| 6 | 用户审核 (HITL) | Step 5 通过后 | 唯一终态闸 |

每 Step retry 预算与作用见 SKILL §防御层次。

## Step 0 — parse arguments

Parse `$ARGUMENTS`:

- `<spec-path>` — path to approved spec, e.g. `spec/exfat/interface/exfat_lookup.spec`
- `--style-off` — skip Step 2.2 style sub-step. Default ON.
- `--audit-off` — skip Step 4 spec/code audit. Default ON.
- `--test-off` — skip Step 3 (cmocka test gen + compile) AND Step 5.1 cmocka exec. Default ON.
- `--no-build` — skip Step 2.3 kernel build + Step 5 entirely (no build, no cmocka exec, no QEMU smoke).
- `--no-regress` — skip the final regression-suite reminder.
- `--prompt-override <file>` — use a hand-written prompt instead of plugin-assembled.

## Step 1 — 生成 C 代码 (codegen)

Call `specfs.session_start(module=<derived from path>, mode="gen")`. Receive
`{session_id, dag_state}`.

Verify spec is approved (no `.draft` suffix). If not approved:

- Tell user: "Spec at `<path>` is not approved. Run `/specfs-port-spec` first."
- STOP.

Call `specfs.code_gen_start(session_id, spec_path=<path>)`. Receive
`{prompt_for_llm}` — the assembled codegen prompt with:

- `{LITEOS_DIGEST}` — compact LiteOS-A rules digest.
- `{COMMON_HEADER}` (current `spec/<module>/common.header`).
- `{INHERITED_INVARIANTS}` (from DAG ancestors).
- `{PRIOR_CODE_INTERFACE}` (declarations from frozen ancestor code).
- `{ORIG_SPEC_CONTENT}` (the spec content).

If `--prompt-override <file>` was given, the server uses the override file
verbatim (skipping injection assembly).

**On-demand reference expansion**: the assembled prompt contains a compact
LITEOS_DIGEST plus a FRAGMENT INDEX listing detailed reference fragments
(`style_rules`, `linux_to_liteos_table`, `format_traps`, `ask_first_rules`).
Before emitting code, scan the spec — if any concrete uncertainty matches an
INDEX entry's "when to fetch" hint, call
`specfs.fetch_prompt_fragment(name="<id>")` to retrieve the full content.
Do NOT pull all four reflexively; pull only those you need.

Output the C code in your response as a ```c ... ``` fenced block (only the
single C file, no surrounding prose). End with `/* Assumptions made: ... */`
ONLY if the spec / digest left genuine ambiguity that you resolved with a
defensible default (rare).

Save the code to a temp location via Write tool:
`fs/<module>/<derived_path>.draft.c`

Derived path matches the spec's relative path under `spec/<module>/`, but
under `fs/<module>/` and with `.c` extension.

## Step 2 — 静态检查 + 内核 build (fail-fast)

按 2.1 → 2.2 → 2.3 顺序执行；任一子步骤失败回 Step 1（用各自的 retry 预算重生）。
Step 2 在 Step 1 代码生成之后立即跑，目的是**在生成测试 / audit / QEMU 之前**先把
最便宜的 LSP / style / build 错误清掉。

### Step 2.1 — LSP compile (retry budget: 4)

Call OMC LSP `lsp_diagnostics` for `<draft path>`. clangd sees real LiteOS-A
headers via the repo's `.clangd` config, so it catches both syntax and
semantic issues.

- Empty diagnostics (or only `info` severity) → advance to Step 2.2.
- `error` / `warning` present → call
  `specfs.inject_diagnostics(session_id, layer="compile", payload=<diagnostics text>)`
  and loop back to Step 1.

If clangd is unreachable (no OMC LSP server), proceed to Step 2.2 and note
"Step 2.1 skipped (no LSP)" in the final summary.

### Step 2.2 — Style audit (retry budget: 5; skip if `--style-off`)

After `code_gen_submit` (called once in Step 1), the response carries
`next="style_audit"` when `style_audit_enabled` is True. Caller assembles
the style-audit prompt locally via the bundled fragments (`prompts/style_audit.md`
+ `prompts/style_rules.md`); LLM produces JSON
`{is_good: bool, score: int, summary: str, violations: [...]}`.

- If `is_good=true && score >= 80` → advance to Step 2.3.
- If `is_good=false || score < 80` → call
  `specfs.inject_diagnostics(session_id, layer="style", payload=<violations>)`
  and loop back to Step 1.

### Step 2.3 — Kernel build (retry budget: 3; skip if `--no-build`)

Call `specfs.run_build_kernel()`.

- `ok=true` → advance to Step 3.
- Failure → inject `layer="build"` with stderr tail, loop back to Step 1.

Step 2 retry budgets are independent: LSP failure consumes only the LSP budget,
style failure consumes only the style budget, build failure consumes only the
build budget. If any sub-step exhausts its budget, escalate to Step 6 with
status "Step 2.<sub> exhausted retries".

## Step 3 — cmocka 测试生成 + 测试编译 (skip if `--test-off`)

Step 3 在 **Step 2 全部通过之后** 触发——测试看到的是**已经 LSP-clean +
style-clean + 内核 build 通过** 的 C 代码符号，而不是 spec 抽象。详细契约见
SKILL §防御层次 / Step 3 段。

Step 3 共享 retry 预算 3 轮（test gen 失败 + cmocka build 失败合算）。

### Step 3.1 — Test gen

1. Call `specfs.test_gen_start(session_id)` → `{prompt_for_llm, draft_path,
   harness_dir_exists}`.
2. If `harness_dir_exists` is False, warn user that the test file will be
   written but the harness wiring (Makefile / main.c) skip will require
   manual fix-up; proceed anyway.
3. Read the prompt and generate the cmocka test source as a single
   ```c ... ``` fenced block. One testpoint per spec Case + one per testable
   Invariant. Test must reference the actual symbols emitted in Step 1, not
   a spec abstraction.
4. Call `specfs.test_gen_submit(session_id, generated_test_text=<code>)`.
   Server writes `<draft_path>`. Returns `{next: "test_speceval" | "ok"}`.
5. If `next: "test_speceval"`, the server returns a SpecEval-style auto-check
   prompt. Generate JSON `{is_good, comments}`; on `is_good=false` consume one
   round of the Step 3 budget and loop back to Step 3.1 (regenerate the test
   only — do not re-run Step 1 codegen).

### Step 3.2 — cmocka build only

编译刚生成的 `test_<stage>.c.draft`（**不执行**），用 host cmocka harness 的 Makefile
做 dry-build。失败原因通常分两类：

- 测试文件本身有 C 编译错误 → 消耗一轮 Step 3 预算，回 Step 3.1 重生测试。
- 生成的 production code 未导出测试需要的符号 → 罕见（Step 2 应该已抓住），但若
  发生：注入 `layer="cmocka_build"` 回 Step 1 重生代码，Step 3 预算不消耗。

通过后 → 进 Step 4。Test draft 不批准——最终批准在 Step 6 与代码一并审。
Do NOT call `test_gen_approve` here.

如 `--test-off`，整个 Step 3 跳过；进 Step 4 时 audit 输入里 test 部分用空占位。

## Step 4 — spec/code audit (skip if `--audit-off`)

合并的 spec conformance + Linux 等价性审计；**取代旧 SpecEvaluator 与旧
heterogeneous code/test audit**。审计器优先选异构模型族（如 GPT-family，与 Step 1
生成器不同模型族）以保留独立 review 价值。

模板：`prompts/speceval.md` (spec conformance) 与 `prompts/heterogeneous_audit.md`
(`mode="code_audit"`) 联合使用。Audit retry 预算：**3 轮**。

### 调用方式

Call `specfs.enforce_audit(session_id)` (alias `enforce_speceval`) — 返回组装好
的 audit 提示词 + reviewer_contract。**用 `Task()` 起一个独立异构审计 subagent**
（推荐 GPT-family 模型；fallback 用 Sonnet 同款不同上下文）。不要在主 session
上下文里直接审 — 同模型 + 同上下文会丢独立 review value。

Reviewer 返回 JSON 后调用 `specfs.record_audit_verdict(session_id, verdict_json)`
（alias `record_speceval_verdict`）。

### Auditor 契约 (read-only, advisory-only)

- 不写文件、不批准、不改 prompt template。
- 对照 `spec + Linux 源码 + code + test + inherited invariants`。
- 输出 JSON `{is_good, comments, findings[]}`。Each finding must contain
  `finding_id, severity (high/med/low), root_cause, source_anchor (Linux file:line
  or spec/code path:line), generated_anchor, claim, evidence, recommendation,
  confidence, requires_user_arbitration`.
- `root_cause` ∈ `spec_under_specified | codegen_drift | test_gap | prompt_gap | uncertain`。
- 没有具体 Linux/spec/code/test anchor 的 finding 一律降级 `low + requires_user_arbitration=true`。
- 风格违例（已 Step 2.2 处理）视为 false positive 跳过。

### Loop policy

`record_audit_verdict` 收 JSON：

- `is_good=true` → server 返 `next="runtime_validation"`，Step 5 进入 cmocka exec + QEMU smoke。
- `is_good=false` → server 返 `next="code_gen_refine"`，按 finding `root_cause` 分流：

  **codegen_drift** (~70% 案例)：注入
  `[Modification suggestions]` source=audit_code 回 Step 1 重生代码（Step 1 重生后必须
  重跑 Step 2 → Step 3 → Step 4）。

  **test_gap** (~15%)：注入 source=audit_test 回 Step 3.1 重生测试（消耗 Step 3 预算）。

  **spec_under_specified** (~10%)：call `specfs.spec_fine(session_id,
  speceval_comments=<comments>)` 拿 SpecFine 提示词；生成 polished spec text；
  call `specfs.spec_fine_submit(session_id, polished_spec_text=<text>)`。Server
  覆盖已批准 spec 并返回 `next: "code_regen"` → 回 Step 1。**Hard cap on
  SpecFine: 3 rounds.** 超出时返回 `next: "user_review", reason:
  "spec_fine_cap_exceeded"` → 立即升 Step 6 让用户决定 split / hand-revise。

  **prompt_gap** / **uncertain**：累积到 `docs/<module>_prompt_feedback.md`
  做后续 Loop eval prompt 优化用，**不**消耗本 stage 预算（这是给 prompt 模板的反馈）。

Audit 预算 3 轮用完仍不通过 → 升 Step 6 让用户仲裁。

### Grandfather clause for old specs

`enforce_audit` 自动从 DAG 读取 `spec.approved_at` 时戳并注入审计 prompt。
若该 spec 在 SA 强制规则之前已批准（cutoff `2026-05-11`），auditor 会把
"missing System Algorithm" finding 降级为 `info` + `requires_user_arbitration=false`，
不阻塞流水线。新 spec 没有此豁免。

## Step 5 — 运行时验证 (skip if `--no-build`)

只在 Step 4 audit 通过后跑——避免给将要被 audit reject 的代码付昂贵的
QEMU 时间。Step 5 共享 retry 预算 **3 轮**（5.1 + 5.2 共算）。

### Step 5.1 — cmocka exec (skip if `--test-off`)

跑 host cmocka 套件，覆盖 Step 3 编译出来的 `test_<stage>` + 历史 stage 已批准测试。
Call `specfs.validator_run_holistic(module=<module>, mode="cmocka_only")`；
mode 取 `cmocka_only` / `qemu_only` / `holistic` 之一（详见 server docstring）。

- 全部 testpoints pass → 进 Step 5.2。
- Failure：识别失败来源：
  - 失败 testpoint 是本 stage 新生成的 → inject `layer="qemu" source=cmocka` 回
    Step 3.1 重生测试（不消耗 Step 5 预算，消耗 Step 3 预算）。
  - 失败 testpoint 是历史 stage 的（本 stage 代码破坏了上游不变量）→ inject
    `source=cmocka_regression` 回 Step 1 重生代码（消耗 Step 5 预算；重生后必须
    重跑 Step 2 → 3 → 4 → 5）。

如 `--test-off`，跳过 5.1，直接进 5.2。

### Step 5.2 — QEMU smoke

Call `specfs.run_qemu_smoke(commands=[...])` with the mount/umount cycle
(or stage-specific smoke). 等价：`specfs.validator_run_holistic(module, mode="qemu_only")`
跑 QEMU LTP 子集。失败 inject `source=qemu` 回 Step 1。

Step 5 预算 3 轮用完 → 升 Step 6，状态 "Step 5 runtime exhausted retries"。

## Step 6 — 用户审核 (HITL)

Display `<code draft path>` AND `<test draft path>` to user. Optional:
render `prompts/validation_checklist.md` checklist。同时附 audit 结果摘要：
pass / refined N rounds / spec_fine triggered / 用户仲裁 issue。

`AskUserQuestion`:

- (a) Approve both — write code + test to final paths, commit DAG node code+tests layers.
- (b) Suggest code edits — provide feedback; loops back to Step 1（重生后跑 Step 2 → 3 → 4 → 5）。
- (c) Suggest test edits only — provide feedback; loops back to Step 3.1（不重 codegen）。
- (d) Inline-edit — user manually edits draft(s), then "done".
- (e) Reject and regen — discard, restart Loop code from Step 1.
- (f) Reject and revise spec — go back to Loop spec; mark spec dirty.

### 处理用户响应

**(a) Approve both**: call `specfs.code_gen_approve(session_id, final_code=<text>,
files_to_save=[<paths>])`, then `specfs.test_gen_approve(session_id,
final_test_text=<text>)`. Server commits DAG node code+tests layers, syncs
`common.header`, applies Makefile + main.c deltas.

**(b) Suggest code edits**: call `specfs.code_gen_refine(session_id,
user_suggestion=<text>)`. Loop back to Step 1.

**(c) Suggest test edits only**: call `specfs.test_gen_refine(session_id,
user_suggestion=<text>)`. Loop back to Step 3.1 — code stays.

**(d) Inline-edit**: tell user "Make edits to `<draft>` files, then say `done`."
Then call `code_gen_approve` + `test_gen_approve` with post-edit content;
approval mode = "manual_override".

**(e) Reject and regen**: call `code_gen_refine` with "rewrite from scratch",
loop back to Step 1.

**(f) Reject and revise spec**: call `specfs.session_end(session_id)`. Tell
user: "Run `/specfs-port-spec <linux-path> <stage>` to revise the spec."

## Iteration accounting

| # | Step | Retry budget |
|---|---|---|
| 2.1 | LSP compile | 4 |
| 2.2 | Style audit | 5 |
| 2.3 | Kernel build | 3 |
| 3 | Test gen + compile (shared) | 3 |
| 4 | spec/code audit | 3 |
| 5 | cmocka exec + QEMU smoke (shared) | 3 |
| inner | SpecFine (Step 4 spec-side branch) | 3 |
| 6 | User review | unlimited |

任一预算耗尽 → 升 Step 6，状态字符串 `Step <N> exhausted retries`。

## Step 7 — Module-completion regression reminder (skip if `--no-regress`)

Per-stage Step 2 / 3 / 5 已经覆盖与 `tools/regress/run_all.sh` 同链路的 build /
cmocka / QEMU smoke。**模块完结**（最后一个 stage approve 后）仍建议跑一次
聚合脚本——它顺序跑 Wave A (cmocka host) + Wave B (QEMU LTP smoke) 并写时间戳
报告，作为 pre-push gate。

When this is the LAST stage of its module (e.g., the rename stage of the
dirops module ≤ 800 LoC budget — see `tools/specfs_eval/BUDGETS.md`), remind
the user to run:

```
bash tools/regress/run_all.sh
```

Exit codes: `0=pass, 1=test failure, 2=panic-or-hang`。报告写到
`docs/test/exfat_regression_<ts>.md`，symlink `_latest.md`。

If `--no-regress` was passed, skip this reminder.

## Final output

When code + test approved, print summary:

```
Approved: fs/exfat/exfat_lookup.c (<code-git-sha>)
         testsuites/unittest/exfat/test_lookup.c (<test-git-sha>, N testpoints)
DAG node lookup: code + tests layers committed.
common.header diff: <…>

Step 2.1 LSP:           clean (N rounds)
Step 2.2 style:         is_good=true score=<N> (M rounds)
Step 2.3 kernel build:  pass
Step 3 cmocka build:    pass (N testpoints compiled)
Step 4 spec/code audit: is_good=true (K rounds, last: "<one-line digest>")
Step 5.1 cmocka exec:   <N>/<N> pass
Step 5.2 QEMU smoke:    pass

Regression reminder: tools/regress/run_all.sh — please run before pushing
(or rely on per-stage Step 2/3/5 if no cross-stage interactions changed).

Review and run: git commit -m "feat(fs/exfat): lookup (specfs-port)"
```
