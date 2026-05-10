# specfs-port

把 Linux 内核文件系统按 **SYSSPEC 规范优先** 流程移植到 OpenHarmony LiteOS-A 的
Claude Code 插件。HITL 双闸 + 多层防御。

A Claude Code plugin for porting Linux kernel filesystem modules to LiteOS-A
via a SYSSPEC-style spec-first workflow with HITL gates and a layered defense
of compile / style / spec-conformance / build+QEMU before user review.

基于 FAST'26 SpecFS 论文（arXiv:2512.13047）扩展：

- **双 HITL 闸**：spec authoring (Loop spec) + code generation (Loop code)。
- **Loop code 流水线**（按执行顺序）：codegen → 静态检查 + 内核 build → cmocka 测试生成 + 测试编译 → spec/code 审计 → cmocka 执行 + QEMU smoke → 用户审核。各阶段失败 fail-fast 回到对应上游环节。
- **DAG 节点**保留跨 stage 依赖与 invariant 继承。
- **Ask-first**：输入有歧义时 LLM 先发 AskUserQuestion，再生成。

> 完整版本/Phase 演进见 [CHANGELOG.md](CHANGELOG.md)。
> 实现规格见 [DESIGN.md](DESIGN.md)。
> 方法论与五阶段流水线契约见 [skills/specfs-port/SKILL.md](skills/specfs-port/SKILL.md)。

## Slash commands

| 命令 / Command | 用途 / Purpose |
|---|---|
| `/specfs-port [module]` | 显示 DAG 状态 + 推荐下一步 / Show DAG status + suggest next action |
| `/specfs-port-spec <linux-path> <stage>` | Loop spec — 起草 SYSSPEC spec / Generate a SYSSPEC spec |
| `/specfs-port-code <spec-path>` | Loop code — 从已批准 spec 生成 LiteOS-A C 代码 + cmocka 测试 |
| `/specfs-port-metrics [session-id]` | 遥测聚合（tool 调用、LLM 轮次、prompt 大小） |

可选 flag for `/specfs-port-code` — 默认值见 SKILL.md：

| Flag | 行为 / Behavior |
|---|---|
| `--audit-off` | 关闭 Step 4 spec/code audit 自审（合并的 spec conformance + 异构审计）。默认 ON。 |
| `--style-off` | 关闭 Step 2.2 style audit 子步骤。默认 ON。Canon: `prompts/style_rules.md`；模板：`prompts/style_audit.md`。 |
| `--test-off` | 同时关闭 Step 3 cmocka 测试生成 + Step 5.1 cmocka exec。默认 ON。 |
| `--no-build` | 跳过 Step 2.3 内核 build + Step 5（不 build、不跑 cmocka、不跑 QEMU smoke）。 |
| `--no-regress` | 跳过模块完结的 `tools/regress/run_all.sh` 提醒。 |
| `--prompt-override <file>` | 用手写 prompt 文件覆盖 spec 派生的拼装。 |

## Workflow / 工作流

> 防御层完整契约与 retry 预算见
> [skills/specfs-port/SKILL.md §防御层次](skills/specfs-port/SKILL.md#防御层次plugin-与-skill-共享的契约--defense-layer-topology)。

```
User: /specfs-port-spec /Users/kissa/Codebase/linux/fs/exfat lookup
    ↓ (Loop spec — Linux source → SYSSPEC spec)
[ask-first scan, generate, heterogeneous spec audit, user review/refine, approve]
    ↓ saves to spec/exfat/interface/exfat_lookup.spec, DAG node spec layer committed

User: /specfs-port-code spec/exfat/interface/exfat_lookup.spec
    ↓ (Loop code — spec → C code + cmocka tests)
[Step 1  生成 C 代码 (codegen)
 Step 2  静态检查 + 内核 build （fail fast）
         2.1 LSP compile        (≤ 4)
         2.2 style audit        (≤ 5)
         2.3 kernel build       (≤ 3)
 Step 3  cmocka 测试生成 + 测试编译
         3.1 test gen           (≤ 3)
         3.2 cmocka build only  (与 3.1 共享预算)
 Step 4  spec/code audit          (≤ 3)
         合并 spec↔code conformance + Linux 异构审计；按 finding 类别分流
 Step 5  运行时验证                (≤ 3 共享)
         5.1 cmocka exec
         5.2 QEMU smoke
 Step 6  用户审核 (HITL，唯一终态闸；code + test 一并审)]
    ↓ saves to fs/exfat/exfat_lookup.c + testsuites/unittest/exfat/test_lookup.c
    ↓ DAG node code + tests layers committed
    ↓ common.header auto-synced with new exports
    ↓ Makefile::HARNESS_SRCS + main.c::run_suite() auto-applied
    ↓ git add (no commit — user runs `git commit` themselves)
```

回退路径 / Failure routing：

- Step 2 任意子步骤失败 → 回 Step 1 重生 C 代码。
- Step 3 失败 → 回 Step 3 重生测试（不重 codegen）。
- Step 4 finding：`codegen_drift` → 回 Step 1；`test_gap` → 回 Step 3；
  `spec_under_specified` → 回 Loop spec 用 SpecFine 收紧 spec（cap 3）。
- Step 5 cmocka/QEMU 失败：失败的是新生成测试 → 回 Step 3；其它 → 回 Step 1（重生代码后必须重跑 Step 2 → Step 4 → Step 5）。

## 文件结构 / Files

```
.claude/plugins/specfs-port/                  # plugin root (self-contained, auto-discoverable)
├── .claude-plugin/plugin.json                # manifest
├── .mcp.json                                 # MCP server spawn config (uses ${CLAUDE_PLUGIN_ROOT})
├── DESIGN.md                                 # full implementation spec
├── README.md                                 # this file
├── CHANGELOG.md                              # 版本演进 / version history (single source of truth)
jjjjjj├── prompts/                                  # prompt fragments
│   ├── codegen.md, speceval.md               # ports of paper gencode.py:158/200
│   ├── linux_to_spec.md                      # Loop spec system prompt
│   ├── ask_first_rules.md                    # shared "ask before generate"
│   ├── style_rules.md, style_audit.md        # Step 2.2 style audit canon + LLM template
│   ├── linux_to_liteos_table.md              # Linux → LiteOS primitive map
│   ├── format_traps.md                       # 4 compatibility-trap classes
│   ├── unittest_gen.md                       # Step 3 cmocka test gen template
│   ├── heterogeneous_audit.md                # spec/code 异构审计契约
│   ├── linux_compare.md                      # Loop eval Linux ↔ port 等价比对
│   ├── prompt_optimize.md                    # Loop eval 元提示词
│   └── validation_checklist.md               # Step 6 user review aid
├── commands/                                 # slash commands
│   ├── specfs-port.md
│   ├── specfs-port-spec.md
│   ├── specfs-port-code.md
│   └── specfs-port-metrics.md
├── server/                                   # Python MCP server
│   ├── pyproject.toml
│   ├── specfs_server.py                      # main MCP entry
│   ├── state.py                              # session state classes
│   ├── dag.py                                # DAG load/save/walk
│   ├── extract.py                            # C declaration extractor
│   └── prompts.py                            # template loading + assembly
└── skills/specfs-port/                       # bundled methodology skill (auto-discovered)
    ├── SKILL.md                              # full methodology
    └── references/                           # specfs-format / liteos-vfs-mapping / liteos-fs-style /
                                              # exfat-walkthrough / cmocka-host-harness /
                                              # ltp-qemu-regression / fs-debug-recipe

spec/<module>/                                # per-FS spec tree (LLM-authored, user-approved)
├── common.header                             # shared contract, GROWS as stages approve
├── interface/                                # VFS-level ops
├── inode/ file/ path/ util/ bitmap/          # layered subdirs per specfs-port skill
└── .specfs.dag.json                          # DAG state (committed to git)

fs/<module>/                                  # LLM-generated code (frozen after approval)
testsuites/unittest/<module>/                 # cmocka host harness (Wave A regression)
tools/regress/run_all.sh                      # aggregate regression runner (Wave A cmocka + Wave B QEMU LTP)
```

## Requirements

- Python 3.11+。
- `uv` (https://docs.astral.sh/uv/) — 依赖管理。
- `mcp` Python 包（uv 自动装）。
- Step 2.1 LSP 需 **OMC LSP (clangd)**；可通过 `oh-my-claudecode:mcp-setup` 安装。clangd 读仓库 `.clangd` 配置，不依赖 stub。
- Step 2.3 / Step 5 构建与 QEMU 需 SSH 到构建主机（默认 `192.168.1.15`，per project memory）。

## HITL 角色契约 / Roles

| Actor | Writes | Reviews | Approves |
|---|---|---|---|
| User | 自然语言描述、建议、free-form feedback | spec drafts、code drafts、build / QEMU results | spec、code、DAG node commits |
| LLM (Claude) | spec 文件、code 文件、AskUserQuestion 调用 | 自审（SpecEvaluator opt-in） | 无 |
| Plugin | DAG 状态、prompt 拼装、队列顺序 | 无 | 无 |

用户**不**直接写 spec 或 code。用户描述需求、审阅插件产物、提建议、最终批准。
插件强制流水线顺序；用户始终是最终仲裁者。

## 为何如此设计 / Why this exists

纯批处理自动化（specfs 的 `gen.py`）依赖完整回归测试集做 validation gate。
LiteOS-A 内核 FS 移植没有这种测试集——QEMU smoke 既慢又浅。HITL 填上这个缺口：
**人就是缺失的 test oracle**。本插件在 specfs 之上额外提供：

1. Linux 源码 → spec 阶段（specfs 假设 spec 已写好）。
2. Ask-first 澄清（specfs 假设 spec 终稿）。
3. Step 2.1 (LSP) 与 Step 6 (用户审核)（specfs 仅有 compile + speceval）。
4. 含 invariant 继承的 per-stage DAG（specfs 仅在 evolve 模式有 DAG）。
5. `--prompt-override` 用户级 prompt 控制（specfs 无）。

完整 vs-paper 对照见 [DESIGN.md §Appendix A](DESIGN.md)。
