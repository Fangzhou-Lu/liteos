# specfs-port

Claude Code plugin for porting Linux kernel filesystem modules to LiteOS-A
via a SYSSPEC-style spec-first workflow with HITL gates and 5-layer defense.

Built on the FAST'26 SpecFS paper (arXiv:2512.13047) but extended:
- Two HITL loops: spec authoring (Loop A) and code generation (Loop B)
- Five layers of defense before user review (LSP → compile → build → QEMU → SpecEvaluator)
- DAG of approved stage nodes preserves cross-stage dependencies + invariants
- Ask-first clarification: LLM pauses before generation when input is ambiguous

See `DESIGN.md` for full implementation spec, `CHANGELOG.md` for version history.

## Slash commands

| Command | Purpose |
|---|---|
| `/specfs-port [module]` | Show DAG status, suggest next action. |
| `/specfs-port-spec <linux-path> <stage>` | Loop A — generate a SYSSPEC spec. |
| `/specfs-port-code <spec-path>` | Loop B — generate LiteOS-A C code from approved spec. |

Optional flags for `/specfs-port-code`:
- `--speceval-off` — disable Layer 3 SpecEvaluator self-audit
 (default **ON** since plugin v0.2; user explicitly required self-audit before
 user review. P1.4 (2026-05-07) moved this layer BEFORE Layer 2 build/QEMU —
 cheap-first ordering catches spec-conformance defects without paying build cost.)
- `--style-off` — disable the style-audit sub-step inside Layer 1a
 (default **ON** since plugin v0.3; user directive "加入编码风格评估环节".
 P1.1 briefly folded the audit into Layer 3 SpecEval; P1.2 split it back out
 as a Layer 1 sibling (Layer 1b); P1.4 (2026-05-07) folded it back into
 Layer 1a as a SEQUENTIAL second sub-step (LSP first, then style; separate
 retry budgets). Rule canon: `prompts/style_rules.md`; LLM template:
 `prompts/style_audit.md`. Layer 3 SpecEval no longer references style.)
- `--test-off` — disable Layer T cmocka test gen AND Layer 2.2 cmocka exec
 (default **ON** since v0.3.4; P1.4 moved Layer T from Loop A → Loop B Step 3a
 so tests reference real generated symbols instead of spec abstractions.)
- `--no-build` — skip Layer 2 entirely (no build, no cmocka exec, no QEMU smoke)
- `--no-regress` — skip the module-completion `tools/regress/run_all.sh` reminder
- `--prompt-override <file>` — bypass spec-derived prompt assembly

## Workflow

```
User: /specfs-port-spec /Users/kissa/Codebase/linux/fs/exfat lookup
    ↓ (Loop A — Linux source → SYSSPEC spec)
[ask-first scan, generate, user review/refine, approve]
    ↓ saves to spec/exfat/interface/exfat_lookup.spec, DAG node spec layer committed
    ↓ (P1.4: Layer T cmocka test gen NO LONGER fired here — moved to Loop B)

User: /specfs-port-code spec/exfat/interface/exfat_lookup.spec
    ↓ (Loop B — spec → C code + cmocka tests, P1.4 ordering)
[Step 3   gen C code
 Step 3a  Layer T  cmocka test gen (≤ 3)
 Step 4   Layer 1a sequential: 4.1 LSP compile (≤ 4) → 4.2 style audit (≤ 5)
 Step 5   Layer 3  SpecEval — spec conformance only (≤ 8)
 Step 6   Layer 2  unified: 6.1 build → 6.2 cmocka exec → 6.3 QEMU smoke (≤ 3)
 Step 7   Layer 4  user review (code + test together)]
    ↓ saves to fs/exfat/exfat_lookup.c + testsuites/unittest/exfat/test_lookup.c
    ↓ DAG node code + tests layers committed
    ↓ common.header auto-synced with new exports
    ↓ Makefile::HARNESS_SRCS + main.c::run_suite() auto-applied
    ↓ git add (no commit — user runs `git commit` themselves)
```

## Files

```
.claude/plugins/specfs-port/                  # plugin root (v0.5.0 self-contained layout)
├── .claude-plugin/plugin.json                # manifest
├── .mcp.json                                 # MCP server spawn config (uses ${CLAUDE_PLUGIN_ROOT})
├── DESIGN.md                                 # full implementation spec
├── README.md                                 # this file
├── CHANGELOG.md                              # version history
├── prompts/                                  # prompt fragments
│   ├── codegen.md, speceval.md               # verbatim from gencode.py:158/200
│   ├── linux_to_spec.md                      # Loop A system prompt
│   ├── ask_first_rules.md                    # shared "ask before generate"
│   ├── style_rules.md, style_audit.md        # Layer 1a.2 style audit canon + LLM template
│   ├── linux_to_liteos_table.md              # Linux → LiteOS primitive map
│   ├── format_traps.md                       # 4 compatibility-trap classes
│   ├── unittest_gen.md                       # Layer T cmocka test gen template
│   └── validation_checklist.md               # Layer 4 user review aid
├── commands/                                 # 3 slash commands
│   ├── specfs-port.md
│   ├── specfs-port-spec.md
│   └── specfs-port-code.md
├── server/                                   # Python MCP server
│   ├── pyproject.toml
│   ├── specfs_server.py                      # main MCP entry
│   ├── state.py                              # session state classes
│   ├── dag.py                                # DAG load/save/walk
│   ├── extract.py                            # C declaration extractor
│   └── prompts.py                            # template loading + assembly
└── skills/specfs-port/                       # v0.5.0: bundled methodology skill (auto-discovered)
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
testsuites/unittest/<module>/                 # cmocka host harness (Layer 2.2 + Layer A regression)
tools/regress/run_all.sh                      # aggregate regression runner (cmocka + QEMU LTP)
```

## Requirements

- Python 3.11+
- `uv` (https://docs.astral.sh/uv/) — for dependency management
- The `mcp` Python package (auto-installed by uv)
- For Layer 1a: OMC LSP (clangd) — see `oh-my-claudecode:mcp-setup`. P1.3 (2026-05-07) made LSP a hard prerequisite; the gcc -fsyntax-only fallback was removed because clangd reads the repo's real `.clangd` config and never has stub-drift.
- For Layer 2: SSH access to the build host (default `192.168.1.15` per project memory)

## Roles (HITL contract)

| Actor | Writes | Reviews | Approves |
|---|---|---|---|
| User | natural-language description, suggestions, free-form feedback | spec drafts, code drafts, build/QEMU results | spec, code, DAG node commits |
| LLM (Claude) | spec files, code files, AskUserQuestion calls | own outputs (SpecEvaluator opt-in) | nothing |
| Plugin | DAG state, prompt assemblies, queue ordering | nothing | nothing |

The user does NOT write specs or code directly. They describe needs, review
plugin output, suggest edits, and approve. The plugin enforces the iteration
order; the user remains the final arbiter.

## Why this exists

Pure batch automation (specfs's `gen.py`) requires comprehensive regression
tests as the validation gate. LiteOS-A kernel FS porting has no such test
suite — the QEMU smoke test is slow and shallow. HITL fills the gap: the
human is the missing test oracle. Plugin extends specfs with:

1. Linux source → spec phase (specfs assumes specs already written)
2. Ask-first clarification (specfs assumes spec is final)
3. Layer 0 (LSP) and Layer 4 (user review) (specfs has compile + speceval only)
4. Per-stage DAG with explicit invariant tracking (specfs has DAG for evolve mode only)
5. .prompt override (specfs has no prompt-level user control)

See `DESIGN.md §Appendix A` for full mapping vs paper.
