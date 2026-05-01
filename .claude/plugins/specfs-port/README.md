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
 user review — see `commands/specfs-port-code.md` Step 8 hard contract)
- `--style-off` — disable Layer S coding-style audit
 (default **ON** since plugin v0.3; user directive "加入编码风格评估环节" —
 audits 6 dimensions: naming / complexity / layout / memory & libsec / locking /
 error path; see `commands/specfs-port-code.md` Step 6.5 + `prompts/style_audit.md`)
- `--no-build` — skip Layer 2 (build + QEMU smoke)
- `--no-regress` — skip Step 11 regression-suite reminder
- `--prompt-override <file>` — bypass spec-derived prompt assembly

## Workflow

```
User: /specfs-port-spec /Users/kissa/Codebase/linux/fs/exfat lookup
    ↓ (Loop A — Linux source → SYSSPEC spec)
[ask-first scan, generate, user review/refine, approve]
    ↓ saves to spec/exfat/interface/exfat_lookup.spec, DAG node spec layer committed

User: /specfs-port-code spec/exfat/interface/exfat_lookup.spec
    ↓ (Loop B — spec → C code)
[ask-first → Layer 0 LSP → Layer 1 gcc -fsyntax-only → Layer 2 build + QEMU
    → Layer 3 SpecEval (opt-in) → Layer 4 user review]
    ↓ saves to fs/exfat/exfat_lookup.c, DAG node code layer committed
    ↓ common.header auto-synced with new exports
    ↓ git add (no commit — user runs `git commit` themselves)
```

## Files

```
.claude/plugins/specfs-port/
├── .claude-plugin/plugin.json # manifest
├── .mcp.json # MCP server spawn config
├── DESIGN.md # full implementation spec
├── README.md # this file
├── prompts/ # 8 prompt fragments
│ ├── codegen.md, speceval.md # verbatim from gencode.py:158/200
│ ├── linux_to_spec.md # Loop A system prompt
│ ├── ask_first_rules.md # shared "ask before generate"
│ ├── style_rules.md # LiteOS-A coding rules
│ ├── linux_to_liteos_table.md # Linux → LiteOS primitive map
│ ├── format_traps.md # 4 compatibility-trap classes
│ └── validation_checklist.md # Layer 4 user review aid
├── commands/ # 3 slash commands
│ ├── specfs-port.md
│ ├── specfs-port-spec.md
│ └── specfs-port-code.md
└── server/ # Python MCP server
    ├── pyproject.toml
    ├── specfs_server.py # main MCP entry
    ├── state.py # session state classes
    ├── dag.py # DAG load/save/walk
    ├── extract.py # C declaration extractor
    └── prompts.py # template loading + assembly

spec/<module>/ # per-FS spec tree (LLM-authored, user-approved)
├── common.header # shared contract, GROWS as stages approve
├── interface/ # VFS-level ops
├── inode/ file/ path/ util/ bitmap/ # layered subdirs per specfs-port skill
└── .specfs.dag.json # DAG state (committed to git)

fs/<module>/ # LLM-generated code (frozen after approval)

testsuites/unittest/<module>_host/ # cmocka host harness (Layer A regression)
tools/regress/run_all.sh # aggregate regression runner (cmocka + QEMU LTP)

.claude/skills/specfs-port/ # bundled companion skill (auto-loads with plugin)
├── SKILL.md # full methodology
└── references/ # specfs-format / liteos-vfs-mapping / liteos-fs-style / exfat-walkthrough
```

## Requirements

- Python 3.11+
- `uv` (https://docs.astral.sh/uv/) — for dependency management
- The `mcp` Python package (auto-installed by uv)
- For Layer 1: a working `gcc` on PATH (used with `-fsyntax-only`)
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
