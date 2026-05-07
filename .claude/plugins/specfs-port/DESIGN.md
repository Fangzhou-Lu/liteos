# specfs-port plugin — implementation spec

> Built on SYSSPEC paper (FAST'26 arXiv:2512.13047) but extended with HITL workflow,
> layered defense, ask-first clarification, and DAG-managed cross-stage dependencies.

## 1. Goals & non-goals

### Goals
- Two HITL loops for porting Linux FS modules to LiteOS-A:
 - **Loop A** (Linux → spec): user describes need, LLM drafts SYSSPEC spec, user reviews
 - **Loop B** (spec → code): LLM generates C code with seven-layer defense, user reviews
- Plugin enforces **ask-first** clarification before generation when LLM is uncertain
- Plugin controls iteration order via stateful MCP server; user can't be skipped
- DAG of approved stage nodes preserves cross-stage dependencies + invariants
- **Layer T (v0.3.4)**: spec-derived cmocka test generation in the same HITL pass
  as code review — eliminates the test-debt failure mode where regression coverage
  silently lags behind merged code (see CHANGELOG v0.3.4 for the v0.3.2-era gap).
- Reproducibility = "same user decisions → same outputs"; session JSON is the trace

### Non-goals
- Not a SpecFS paper reproducer (no batch automation, no statistical evaluation)
- Not a general-purpose FS porting toolkit (focused on LiteOS-A target only)
- No spec authoring by user (specs are LLM-generated artifacts only)

## 2. Mental model

### 2.1 Roles

| Actor | Writes | Reviews | Approves |
|---|---|---|---|
| **User** | natural-language description, suggestions, free-form feedback | spec drafts, code drafts, build/QEMU results | spec, code, DAG node commits |
| **LLM (Claude)** | spec files, code files, response messages, AskUserQuestion calls | own outputs (SpecEvaluator opt-in) | nothing |
| **Plugin (MCP server)** | DAG state, prompt assemblies, queue ordering | nothing | nothing |

### 2.1.1 Mapping to paper §4.5 (v0.4 alignment)

The SpecFS paper §4.5 defines three components. Our porting variant maps as
follows — names of our existing tools / commands are NOT renamed (avoid churn);
this table is the canonical Rosetta stone.

| Paper component (§4.5) | Our component | Tools / commands |
|---|---|---|
| **SpecAssistant** — develop/refine spec, run SpecFine on SpecEval feedback | Loop A — `spec_gen_*` (porting variant: input is NL + Linux source, not user draft) + **F3 SpecFine** | `/specfs-port-spec`, `spec_gen_start` / `_submit` / `_refine` / `_approve`, **`spec_fine` / `spec_fine_submit`** (cap 3) |
| **SpecCompiler** — spec → C; iterative retry-with-feedback | Loop B — `code_gen_*` | `/specfs-port-code`, `code_gen_start` / `_submit` / `_refine` / `_approve` |
| **SpecValidator** — final holistic verification (spec review + tests) | **F4 holistic validator** at module completion (≤800 LOC budget) | **`validator_run_holistic(module)`** wrapping `tools/regress/run_all.sh` (Wave A cmocka host + Wave B QEMU LTP smoke) |

**Concept-only equivalents (no separate tool):**

- **Intent / domain knowledge injection** (paper §4.1): folded into spec
  `[PROMPT]` block, plus `// helper-purpose` comments above [RELY] entries.
- **System Algorithm** (paper §4.1): optional sub-block within spec
  [SPECIFICATION], same as paper's atomfs_rename example.
- **SpecEvaluator** (paper §4.5 sub-component of SpecCompiler): Layer 3 in our
  Loop B, single LLM round per code-gen retry, `prompts/speceval.md`. **Spec
  conformance only** — P1.2 (2026-05-07) split style back out as a sibling of
  Layer 1 compile (was briefly folded in during v0.4 P1.1; reverted because
  combined comments mixed style nits with spec violations and made fix
  prioritisation harder). Style canon lives in `prompts/style_rules.md`,
  consumed by the standalone style-audit layer (`prompts/style_audit.md`).

**Things we have, paper does not:**

- DAG with multi-stage spec inheritance (`dag_extract_invariants`) — needed
  because LiteOS porting builds the FS bottom-up across many merges, paper's
  AtomFS is one shot per FS.
- Layer T (cmocka test generation, v0.3.4) — runs after `spec_gen_approve`
  in v0.4 (was after `code_gen_approve` in v0.3.4); auto-approved on internal
  self-check pass.
- LITEOS_DIGEST + FRAGMENT INDEX (v0.4) — LiteOS-A-specific rules the paper
  did not need (BSD-3 license, libsec, FSMAP_ENTRY linker tables, partition
  vs disk addressing). Compact preamble; LLM pulls full detail on demand via
  `fetch_prompt_fragment`.

**Things paper has, we explicitly skipped:**

- Two-phase SpecCompiler (sequential logic / concurrency instrumentation
  separately, paper §4.5). Skipped per user decision: "拆分两阶段是当时 LLM
  的局限"; current Claude handles single-shot generation. Locking still gets
  separate treatment via spec's optional `## Refine Prompt` segment.
- ThreadPoolExecutor parallelism (`spec2code.py:188`). Replaced by P4.1
  Claude `Task()` agent dispatch — same parallel intent, native to our
  runtime, no additional process management.

### 2.2 The two loops

```
Loop A: Linux source → spec
─────────────────────────────
User: "/specfs-port-spec linux/fs/exfat lookup"
    ▼
Plugin: read Linux source + ancestors → assemble linux-to-spec prompt → emit
    ▼
LLM: scan for ambiguities
    ├─ HAS ambiguity → AskUserQuestion(s) → User answers → integrate → continue
    └─ NO ambiguity → generate SYSSPEC four-segment spec (PROMPT/RELY/GUARANTEE/SPECIFICATION)
    ▼
User: review + free-form suggest
    ├─ approve → save to spec/<module>/<sub>/<op>.spec, mark spec.approved_at
    ├─ suggest → feed back to LLM with [Modification suggestions] → regen
    └─ reject → "rewrite from scratch" → regen
    ▼
DAG node spec layer committed; advance to Loop B

Loop B: spec → code
────────────────────
User: "/specfs-port-code spec/exfat/interface/exfat_lookup.spec"
    ▼
Plugin: assemble codegen prompt
    - [PROMPT] [RELY] [GUARANTEE] [SPECIFICATION] from spec
    - [INHERITED INVARIANTS] from DAG ancestors
    - [FROZEN CONTRACT] from common.header
    - [PRIOR CODE INTERFACE] extracted from fs/<module>/*.c (DAG ancestor code)
    - [Previously generated code] (only on retry)
    - [Modification suggestions] (only on retry)
    ▼
LLM: scan for ambiguities (Layer -1) → AskUserQuestion if needed → generate code
    ▼
Plugin: Layer 1 tier (parallel siblings, both must pass before Layer 2)
    ├── Layer 1a compile  (clangd LSP preferred + gcc fsyntax-only fallback) — max 4 retries
    └── Layer 1b style    (LLM self-judge against prompts/style_rules.md)   — max 5 retries
    ▼
    Layer 2 (build+QEMU) — max 3 retries
    Diagnostics injected as [Modification suggestions] with source=lsp|gcc|style|build|qemu.
    (v0.3.3 merged former Layer 0 LSP into Layer 1a — same property, no stub-drift.
     P1.2 split Layer S out of Layer 3 into Layer 1b — see commands/specfs-port-code.md
     for the full topology rationale.)
    ▼
Layer 3: SpecEvaluator — spec conformance ONLY (default ON since v0.2,
    --speceval-off to skip). Style is NOT inlined here; that's Layer 1b's job.
    ▼
Layer T: cmocka test gen (default ON since v0.3.4, --test-off to skip)
    Plugin assembles unittest_gen.md prompt from {generated_code, spec, harness_layout}
    LLM produces test_<stage>.c.draft (one testpoint per [SPECIFICATION] Case +
    one per testable Invariant; no full-VFS dependencies — those go to Layer B QEMU LTP).
    Max 3 retries per session (escalate to Loop A if exceeded — spec is under-specified).
    ▼
Layer 4: User review with diff + build status + QEMU log + style score + speceval verdict
    + cmocka test draft (code AND test reviewed in one HITL pass — v0.3.4)
    ├─ approve both → save code+test to final paths, mark code.approved_at + tests.approved_at
    ├─ suggest code edits → feed back as [Modification suggestions] → regen code (+ Layer T re-fires)
    ├─ suggest test edits → feed back to Layer T only via test_gen_refine
    ├─ inline-edit → user manually edits, plugin acknowledges
    └─ reject → regen
    ▼
DAG node code layer + tests layer committed; trigger common.header sync (auto-extract
new exports); apply best-effort Makefile (HARNESS_SRCS) + main.c (extern + run_suite)
deltas; advance to next stage
```

### 2.3 DAG of stages

Each stage = DAG node with **two layers** (spec + code) and explicit dependencies.

```
mount (root)
    │
    ├─ lookup ──┐
    │ │
    ├─ readdir ┤── read
    │ │
    └─ open ───┘
    │
    └─ write (后续, future)
```

Topological constraint: a stage's spec generation can begin only after **all ancestor code layers** are approved. Within a stage, multiple specs (e.g., `lookup.spec` + `readdir.spec` + `open.spec`) can be generated in parallel via sub-agents if they share no [RELY] dependencies on each other.

### 2.4 Spec coverage rule — interface + narrow utilities only (hard rule, v0.4)

**Refined 2026-05-07 after the mkdir over-spec experience.** Initial v0.4 read
of "1 spec / 1 function" was too literal — promoted 6 helper specs for mkdir
including 4 implementation orchestrators (`exfat_add_entry`,
`exfat_alloc_new_dir`, `exfat_init_dir_entry`, `exfat_init_ext_entry`) that
the paper's AtomFS reference does NOT spec. Implementation orchestrators are
high-volatility code internals; speccing them creates fragile fixed contracts
where flexibility is needed.

**The actual rule:** spec a function only if it falls into one of these
classes:

| Class | Examples in AtomFS | Examples in our tree |
|---|---|---|
| (a) **Public VFS callbacks** | `atomfs_open`, `atomfs_rename`, `atomfs_ins` | `VfsExfatMkdir`, `VfsExfatLookup`, `VfsExfatRead` |
| (b) **Linux exFAT public-header functions** | (n/a, AtomFS is FUSE) | `exfat_zeroed_cluster`, `exfat_alloc_cluster`, `exfat_set_volume_dirty`, ... (see `exfat_fs.h`) |
| (c) **Narrow stable utilities** (single concern, ≤100 LOC, formula or pure compute) | `getlen`, `hash_name`, `calculate`, `malloc_inode` | `exfat_calc_num_entries`, `exfat_inode_alloc` |
| (d) **Cross-stage check helpers** with stable contract | `check_open`, `check_src_exist_dst_delete` | `exfat_validate_dentry_set` (when promoted) |

**Do NOT spec:**
- Implementation orchestrators (compose ≥2 helpers into a sequence): `add_entry`, `alloc_new_dir`
- Field-layout step helpers (set bit pattern N at offset M): `init_dir_entry`, `init_ext_entry`
- Internal rollback / loop strategies (these are commit-message + cmocka concerns)

**Mkdir module worked example** (post-v0.4, post-prune):
```
spec/exfat/inode/
├── exfat_mkdir.spec            (a) — VOP, 7 invariants
├── exfat_calc_num_entries.spec (c) — formula, 2 invariants
├── exfat_zeroed_cluster.spec   (b) — Linux public, 3 invariants
└── exfat_inode_alloc.spec      (c) — narrow util, predates v0.4
```

The 4 deleted helper specs (`add_entry`, `alloc_new_dir`, `init_dir_entry`,
`init_ext_entry`) were moved to `backup/spec/exfat/inode/v04-pruned-2026-05-07/`
for archival.

**Auto-classifier** in `tools/specfs_eval/collect.py::_classify_invariant`
implements this rule deterministically:
1. Host function is a `Vfs<Op>` or `exfat_<vop>` → behavioral
2. Host function is in Linux `exfat_fs.h` public surface → behavioral
3. Host spec is ≤100 LOC (narrow utility tier) → behavioral
4. Invariant text mentions cross-module / observable keyword → behavioral
5. Otherwise → implementation (excluded from AB protocol gate 3)

`tools/specfs_eval/ab_run.py` gate 3 (invariant_preserve) uses
`behavioral_invariant_ids` only; impl-class invariants do not block merge.

## 3. File layout

```
.claude/plugins/specfs-port/
├── .claude-plugin/plugin.json ← manifest
├── .mcp.json ← MCP server spawn config
├── server/
│ ├── pyproject.toml
│ ├── specfs_server.py ← stateful MCP server, tool implementations
│ ├── dag.py ← DAG state load/save/validate
│ ├── prompts.py ← prompt assembly logic
│ └── extract.py ← C interface extractor (frozen code → declarations)
├── prompts/ ← human-readable templates (loaded by server)
│ ├── linux_to_spec.md ← Loop A system prompt
│ ├── codegen.md ← Loop B CodeGen prompt (verbatim from gencode.py:158)
│ ├── speceval.md ← Loop B SpecEvaluator (verbatim from gencode.py:200)
│ ├── ask_first_rules.md ← shared "ask before generate" instruction block
│ ├── style_rules.md ← LiteOS-A coding rules (BSD header / libsec / etc., §11.1-11.2)
│ ├── linux_to_liteos_table.md ← Linux primitive → LiteOS equivalent map (§11.3)
│ ├── format_traps.md ← 4 compatibility-trap checks (§11.4)
│ └── validation_checklist.md ← Layer-4 user review aid (§12)
├── commands/
│ ├── specfs-port.md ← entry point: show DAG status, suggest next action
│ ├── specfs-port-spec.md ← Loop A entry
│ └── specfs-port-code.md ← Loop B entry
├── DESIGN.md ← this document
└── README.md ← user-facing usage

spec/<module>/ ← per-FS spec tree (LLM-authored, user-approved)
├── common.header ← shared contract, GROWS as stages approve
├── interface/
│ ├── exfat_mount.spec
│ ├── exfat_lookup.spec
│ └── ...
├── inode/ file/ path/ util/ ← layered subdirs per specfs-port skill
└── .specfs.dag.json ← DAG state (committed to git)

fs/<module>/ ← LLM-generated code (frozen after approval)
├── exfat_super.c
├── exfat_lookup.c
└── ...
```

## 4. MCP tool surface

All tools take `session_id`; server holds session state across tool calls.

### 4.1 Session lifecycle
```
specfs.session_start(module, mode="gen"|"evolve") → {session_id, dag_state}
specfs.session_status(session_id) → {current_stage, current_phase, retry_count}
specfs.session_end(session_id)
```

### 4.2 Loop A (spec generation)
```
specfs.spec_gen_start(session_id, linux_path, target_stage)
    → {prompt_for_llm} # assembled linux-to-spec prompt
specfs.spec_gen_submit(session_id, generated_spec_text)
    → {next: "review" | "ambiguity" | "regen", payload}
specfs.spec_gen_approve(session_id, final_spec_text)
    → {saved_to: spec/<module>/.../op.spec, dag_node_id}
specfs.spec_gen_refine(session_id, user_suggestion)
    → {next_prompt} # spec content + [Modification suggestions]
```

### 4.3 Loop B (code generation)
```
specfs.code_gen_start(session_id, spec_path)
    → {prompt_for_llm} # assembled codegen prompt with all injection segments
specfs.code_gen_submit(session_id, generated_code)
    → {next: "compile" | "style" | "build" | "speceval" | "review" | "regen",
    payload: {prompt | diagnostics | stderr | code_diff}}
specfs.code_gen_approve(session_id, final_code, files_to_save: list)
    → {saved_paths, common_header_diff, dag_node_id}
specfs.code_gen_refine(session_id, user_suggestion)
    → {next_prompt}
```

### 4.3.5 Layer T (v0.3.4 — cmocka test gen)
```
specfs.toggle_test_gen(session_id, enabled: bool)
specfs.test_gen_start(session_id)
    → {prompt_for_llm, draft_path, final_path, harness_dir_exists}
    # Pre-condition: code_gen_approve must have run AND test_gen_enabled=True.
    # Server caches the just-approved code text + spec for re-use without disk re-read.
specfs.test_gen_submit(session_id, generated_test_text)
    → {next: "review", draft_path, iteration}
    # Writes <draft_path> (.c.draft suffix). Layer 4 will display alongside the code draft.
specfs.test_gen_refine(session_id, user_suggestion)
    → {next_prompt, iteration, retries}
    # Re-assembles unittest_gen.md prompt with prior draft + user feedback as a
    # [Modification suggestions] tail segment.
specfs.test_gen_approve(session_id, final_test_text)
    → {saved_to, dag_node_id, testpoints, test_array_name, makefile_diff,
       mainc_diff, git_added}
    # Renames .draft → .c, best-effort applies Makefile (HARNESS_SRCS) + main.c
    # (extern decl + run_suite call) deltas, updates DAG node `tests` block,
    # runs git add. If regex-based wiring fails, returns "(verify manually)"
    # sentinel so user can patch — does NOT abort approval.
```

### 4.4 Layered defense
```
specfs.run_compile_check(file_paths) → {ok, stderr} # v0.3.3: caller runs OMC LSP first;
    # this tool is gcc -fsyntax-only fallback
specfs.run_build_kernel() → {ok, stderr, image_path} # build.sh fast path
specfs.run_qemu_smoke(commands) → {ok, serial_log}
specfs.inject_diagnostics(session_id, layer, payload)
    → {next_codegen_prompt} # builds [Modification suggestions] from layer output
specfs.toggle_speceval(session_id, enabled: bool)
```

### 4.5 Ask-first
```
specfs.has_unresolved_ambiguity(session_id) → bool
specfs.record_clarification(session_id, question, user_answer)
    → {context_addition} # to be appended to prompt's [USER CLARIFICATIONS] section
```

(LLM uses Claude Code's built-in `AskUserQuestion`; plugin records the Q/A.)

### 4.6 DAG state
```
specfs.dag_get(module) → DAG (full)
specfs.dag_extract_invariants(node_id) → [{id, text}, ...]
specfs.dag_extract_interface(node_id) → [{symbol, signature, src_file}, ...]
specfs.dag_check_node_complete(node_id) → {spec_done, code_done, validations}
specfs.dag_commit_spec(node_id, spec_files) → updated_dag
specfs.dag_commit_code(node_id, code_files, validations_passed) → updated_dag
```

### 4.7 Spec/prompt artifacts
```
specfs.has_prompt_override(spec_path) → bool
specfs.write_prompt_override(spec_path, prompt_text)
specfs.show_assembled_prompt(spec_path) → string # debug/transparency
specfs.sync_common_header(module, new_decls) → diff # auto-append after code approval
```

## 5. Prompt templates

### 5.1 Loop A: linux_to_spec.md

```
[ROLE]
You are abstracting a Linux kernel filesystem module into a SYSSPEC specification
suitable for porting to LiteOS-A. The user does not write specs; they describe
needs and review your output.

[INPUTS]
- Linux source: {linux_path} (read via Read tool)
- Target stage: {target_stage}
- Existing common.header: {common_header_content}
- Ancestor invariants: {inherited_invariants}

[OUTPUT FORMAT]
Four-segment SYSSPEC spec: [PROMPT] [RELY] [GUARANTEE] [SPECIFICATION].
Each Invariant in [SPECIFICATION] must have a unique id (e.g., id=mount-locked-on-success).

[ASK-FIRST RULES]
{from prompts/ask_first_rules.md}

[USER CLARIFICATIONS]
{accumulated Q/A from prior AskUserQuestion calls; empty on first try}

[USER SUGGESTIONS]
{user's free-form feedback if this is a refine round; empty on first try}
```

### 5.2 Loop B: codegen.md (verbatim from gencode.py:158, augmented)

```
[ROLE] ← gencode.py:158 verbatim
You need to generate code according to provided specification and comments.
... (rest verbatim)

[STYLE RULES] ← from prompts/style_rules.md (see §11)
{embedded list of hard rules: BSD-3-Clause header, libsec _s variants,
    LOSCFG_FS_<NAME> ifdef, LOS_MemAlloc, mux/spinlock guidance, goto-stack errno,
    FSMAP_ENTRY registration, naming conventions}

[LINUX→LITEOS PRIMITIVE MAP] ← from prompts/linux_to_liteos_table.md (see §11.3)
{cheat sheet: kmalloc → LOS_MemAlloc, struct super_block → struct Mount, etc.}

[FORMAT-COMPATIBILITY TRAPS] ← from prompts/format_traps.md (see §11.4)
{4 categories: checksum algo, byte order, charset, alignment/packed —
    LLM must AskUserQuestion if any apply to this spec}

[FROZEN CONTRACT] ← extends paper
{spec/<module>/common.header full content}

[INHERITED INVARIANTS] ← extends paper
{flat list of {id, text} from all DAG ancestors}

[PRIOR CODE INTERFACE] ← extends paper
{declarations extracted from fs/<module>/*.c (DAG ancestor code, FROZEN)}

[CURRENT SPEC] ← gencode.py:178 (placeholder for {original_spec})
{spec content from spec/<module>/.../op.spec}

[Previously generated code] ← gencode.py:179, only on retry
...

[Modification suggestions] ← gencode.py:180, only on retry — multi-source
{tagged segments by origin:
    <source: compile (lsp)>...</source> ← v0.3.3 merged: clangd preferred
    <source: compile (gcc)>...</source> ← v0.3.3 merged: gcc -fsyntax-only fallback
    <source: style>...</source>
    <source: build>...</source>
    <source: qemu>...</source>
    <source: speceval>...</source>
    <source: user>...</source>
}

[ASK-FIRST RULES]
{from prompts/ask_first_rules.md}

[OUTPUT]
Single C file in a markdown ```c ... ``` fenced block. No prose.
End with a line: "Assumptions made: <list>" if any low-severity items per §11.6.
```

### 5.3 ask_first_rules.md (shared)

```
Before producing the artifact, scan all inputs for:
1. Ambiguities (multiple plausible interpretations)
2. Missing information (referenced symbol not in any input)
3. Conflicts (current input contradicts an ancestor invariant)
4. Out-of-scope features (beyond requested stage)

If ANY of the above exist:
- Use AskUserQuestion tool with 2-4 concrete options when possible
- Otherwise pose questions in your response (numbered list, with your recommended default)
- Do NOT proceed to generation until the user resolves all blockers

Severity tiers:
- High (no defensible default, affects correctness) → MUST AskUserQuestion before generating
- Medium (defensible default, but worth confirming) → AskUserQuestion with default option marked
- Low (obvious default, included for transparency) → list under "Assumptions made:" at top of output

Example phrasings:
- "exFAT bitmap dentry can be in cluster 1 or chained from root. POSIX-equivalent
    semantics for read-only? Options: (a) scan first cluster only [recommended],
    (b) follow chain to end."
```

## 6. DAG state schema

`spec/<module>/.specfs.dag.json`:

```json
{
    "module": "exfat",
    "schema_version": 1,
    "stages": [
    {
    "id": "mount",
    "stage_name": "mount",
    "spec": {
    "files": ["spec/exfat/interface/exfat_mount.spec"],
    "git_sha": "<oid>",
    "linux_source_ref": "linux/fs/exfat/super.c",
    "approved_at": "2026-04-30T01:14:00Z",
    "approval_iterations": 2,
    "user_clarifications": [
    {"q": "...", "a": "..."}
    ]
    },
    "code": {
    "files": ["fs/exfat/exfat_super.c", "fs/exfat/exfat_balloc.c"],
    "git_sha": "<oid>",
    "approved_at": "2026-04-30T...",
    "approval_iterations": 1,
    "validations_passed": {
    "lsp": true, "compile": true, "build": true, "qemu_smoke": true
    }
    },
    "invariants": [
    {"id": "mount-locked-on-success",
    "text": "After successful Mount, mount->data is non-NULL and ..."}
    ],
    "exports": [
    {"symbol": "VfsExfatMount", "signature": "...", "src": "fs/exfat/exfat_super.c"}
    ],
    "depends_on": []
    }
    ]
}
```

## 7. Defense layers detail

| Layer | Trigger | Tool | On failure | Max retries |
|---|---|---|---|---|
| -1 Ask-first | Before any LLM gen | LLM self-scan + AskUserQuestion | Pause until user clarifies | (not iterative) |
| **1a Compile** (v0.3.3 merged: LSP + gcc) | After every codegen | clangd via OMC LSP (preferred, real headers) → gcc -fsyntax-only with stub (fallback when LSP unavailable) | Inject diagnostics with source=lsp\|gcc → re-codegen | 4 |
| **1b Style** (P1.2: sibling of 1a, was Layer S in v0.3) | After every codegen, parallel to 1a | auto-check (clang-format dry-run + libsec scan + length heuristic) → LLM self-judge against `prompts/style_rules.md` → JSON {is_good, score, violations} | Inject violations with source=style → re-codegen | 5 |
| 2 Build+QEMU | After 1a + 1b both pass; per-stage | build.sh + qemu-system-arm with smoke | Inject build/qemu log → re-codegen | 3 |
| 3 SpecEvaluator (v0.2, ON by default) | After Layer 2 pass | LLM self-judge → JSON {is_good, comments} — **spec conformance only**, style stays in Layer 1b | Inject comments → re-codegen | 8 (paper) |
| **T cmocka test gen (v0.3.4, ON by default)** | After Loop A spec_gen_approve (decoupled from Layer 1/2/3) | LLM assembles `test_<stage>.c.draft` from spec [SPECIFICATION] Cases + Invariants via `prompts/unittest_gen.md` | `test_gen_refine` with prior draft + user feedback → regen | 3 (then escalate to Loop A — spec under-specified) |
| 4 User review | After all auto layers pass | diff + style score + speceval verdict + cmocka test draft | Inject user suggestion → re-codegen (or test_gen_refine for test-only edits) | unlimited |

Each layer has its own [Modification suggestions] segment header so the LLM can
distinguish "compiler said X" from "QEMU panicked Y" from "user said Z".

## 8. Slash command surfaces

### `/specfs-port`
No-arg entry. Reads DAG state, prints status table, suggests next action:
- "next stage to spec: lookup (run `/specfs-port-spec linux/fs/exfat lookup`)"
- "stage with un-approved code: lookup (run `/specfs-port-code spec/exfat/interface/exfat_lookup.spec`)"

### `/specfs-port-spec <linux-path> <target-stage>`
Triggers Loop A. Plugin:
1. Validates DAG ancestors are complete
2. Calls `spec_gen_start` to assemble prompt
3. Skill body instructs Claude: "follow ask-first rules, generate spec, use Write to save draft to spec/<module>/<auto-derived>.spec"
4. Claude generates with possible AskUserQuestion calls
5. Skill body instructs: "show diff, ask user approve/suggest/reject"
6. On approve: call `spec_gen_approve` to commit DAG node spec layer

### `/specfs-port-code <spec-path>`
Triggers Loop B. Plugin:
1. Validates spec is approved
2. Calls `code_gen_start` → assembled codegen prompt with all injection segments
3. Skill body runs the layered defense loop ((compile || style) → build → qemu → speceval → user); test_gen runs decoupled from Loop B, fired by Loop A spec_gen_approve
4. On final approval: call `code_gen_approve` then `test_gen_approve` → commit DAG node code+tests layers + sync common.header + apply Makefile/main.c deltas

### Optional flags
- `--speceval-off` (v0.2 ON by default) — disable Layer 3 SpecEvaluator self-audit
- `--style-off` (v0.3 ON by default; reinstated P1.2 after a brief P1.1 fold-in) — disable Layer 1b coding-style audit
- `--test-off` (v0.3.4 ON by default) — disable Layer T cmocka test gen
- `--no-build` — skip Layer 2 (for fast iteration on logic-only specs)
- `--no-regress` — skip Step 12 regression-suite reminder
- `--prompt-override <file>` — bypass spec-derived prompt assembly

## 9. Edge cases & open questions

### 9.1 Spec drift detection (DECIDED: mark dirty, user decides)
If user edits an already-approved spec (e.g., realized mount spec was wrong AFTER
lookup is in progress), DAG node mount becomes "dirty". Plugin behavior:
- Mark `mount.spec.dirty = true` and propagate dirty flag to all descendant nodes
- On next `/specfs-port` invocation, list dirty nodes with the option:
 - re-run validation only (Layer 1/S/2/3 against current code)
 - re-generate descendant code (cascade regenerate)
 - dismiss (accept dirty state)
- **Plugin does NOT auto-trigger re-runs** — user picks per dirty node.

### 9.2 Common.header divergence
If `code_gen_approve` would add a symbol to common.header that conflicts with an
existing entry, plugin must surface the conflict and ask user to resolve before
committing.

### 9.3 Sub-agent parallelism within stage (DECIDED: spec AND code both parallel)
Stage 后续 has lookup + readdir + open all generated. Plugin parallelizes both phases:
- **Spec generation parallel**: dispatch one sub-agent per target spec (no shared
 state during draft phase; each sub-agent reads same Linux source + ancestor invariants)
- **Code generation parallel**: dispatch one sub-agent per spec when [RELY]
 references show no cross-target dependencies
- Pre-flight check: plugin parses each spec's [RELY] segment; if spec A's [RELY]
 references a symbol from spec B, force serial order (B first, then A)
- Post-flight: invariants from each parallel result are merged into the DAG node
 AFTER each is approved. User can approve in any order; merge is idempotent.
- Max 4 parallel sub-agents (cost guard, see §10).

### 9.4 What "approved" means at git layer (DECIDED: stage only, user commits)
Plugin stages files via `git add`, prints a draft commit message to terminal,
**never invokes `git commit`** itself. User runs `git commit` when satisfied.
Rationale: avoid surprise commits, preserve user's commit-style preferences,
allow user to bundle multiple stage approvals into one commit if desired.

### 9.5 Reversion / rollback
"Reject this stage and reset DAG to before lookup" — needs a tool. Tentatively:
`specfs.dag_revert(node_id)` removes the node, restores files via `git restore`.
User confirms first.

### 9.6 Multi-FS
The plugin design assumes single FS at a time per project. Multiple FSes
(exfat + erofs in parallel) would each have their own DAG file under
`spec/<fs>/.specfs.dag.json`. Sessions are FS-scoped.

## 10. Iteration limits & cost guards

- Layer 1a (compile, merged) retries: 4. Layer 1b (style, sibling of 1a) retries: 5.
- Layer 2 (build / qemu): 3 each. After exhaustion on any layer, escalate to user.
- Layer 3 retries: 8 (paper). User can override.
- **Layer T retries: 3.** v0.3.4. Hard cap is intentionally tight: if Layer T can't
  converge in 3 rounds, the spec [SPECIFICATION] Cases are likely under-specified
  (no testable post-condition, ambiguous error path). Escalate back to Loop A and
  refine the spec rather than grinding more rounds in test gen.
- Sub-agent dispatch: max 4 parallel (avoid Claude Code rate limits).
- AskUserQuestion: no hard limit, but plugin tracks count per session and warns
 if > 5 in a single generation step (suggests spec is too vague).

## 11. Style consistency contract (from `specfs-port` skill)

The plugin is the **runtime** that enforces the **methodology** of the
`specfs-port` skill. All hard rules below MUST be embedded in the codegen
prompt as `[STYLE RULES]` segment so the LLM produces consistent output without
the user having to re-state them every round.

### 12.1 Hard style rules (compiler won't catch — LLM must self-enforce)

| Rule | Source | What to embed in prompt |
|---|---|---|
| BSD-3-Clause Huawei Device header | skill , 后续-8 | "Use the copyright template from `fs/fat/os_adapt/fatfs.c`. Never paste Linux GPL headers — repo is BSD-3-Clause." |
| `LOSCFG_FS_<NAME>` ifdef wrapper | skill | "Wrap the entire translation unit body in `#ifdef LOSCFG_FS_<NAME>` … `#endif`." |
| libsec `_s` variants | skill , common pitfall #4 | "Every `strcpy` / `strncpy` / `memcpy` MUST be `strcpy_s` / `strncpy_s` / `memcpy_s`. Compiler won't flag the unsafe variants — review every call site." |
| `LOS_MemAlloc(m_aucSysMem0, sz)` for kernel heap | skill | "Use `LOS_MemAlloc(m_aucSysMem0, sz)` (or `zalloc(sz)` for zeroed). Pair with `LOS_MemFree(m_aucSysMem0, ptr)`. NEVER `kmalloc`/`malloc` in kernel context." |
| Mux for sleep paths, Spin for non-sleep | skill | "Use `LOS_MuxLock` if path may sleep; `LOS_SpinLock` otherwise. NEVER call sleeping primitives while holding a spinlock." |
| Negative POSIX errno at VFS boundary | skill | "VFS callbacks return negative POSIX errno (`-EINVAL`, `-ENOMEM`, `-EROFS`). Internal helpers may use `LOS_ERRNO_*` but must translate before returning to VFS." |
| Goto-stack error handling | PR-DRAFT, fatfs pattern | "On error, internal use POSITIVE errno + reverse-LIFO `goto` labels (ERROR_VNODE → ERROR_INLOCK → ... → ERROR_EXIT). Final `return -ret`." |
| FS registration via `FSMAP_ENTRY` only | skill 后续-8 | "Filesystem registration uses `FSMAP_ENTRY(<name>_fsmap, \"<name>\", g_<name>Mops, FALSE, TRUE)` — linker-table macro from `los_tables.h`. NEVER use `LOS_MODULE_INIT` for FS registration." |

### 12.2 Naming rules

| Identifier kind | Convention | Example |
|---|---|---|
| VFS callbacks (in `g_<fs>Vops` / `g_<fs>Fops`) | `Vfs<Fs><Op>` PascalCase | `VfsExfatLookup`, `VfsExfatRead` |
| Internal helpers (FS-private) | `<fs>_<verb>_<noun>` lower_snake | `exfat_read_cluster_chain` |
| On-disk struct types | preserve upstream Linux name for grep-ability | `struct exfat_dentry`, `struct exfat_chksum_entry` |
| Per-FS public types | `<fs>_<role>` lower_snake | `exfat_sb_info`, `exfat_inode_info` |
| Magic constants | `<FS>_<PURPOSE>` UPPER_SNAKE | `EXFAT_SUPER_MAGIC`, `EXFAT_FIRST_CLUSTER` |

### 12.3 Linux → LiteOS-A primitive mapping (from skill stage-3)

The codegen prompt embeds this table so the LLM never hallucinates Linux-only
primitives. Plugin auto-generates the table content from a single source of
truth (`prompts/linux_to_liteos_table.md`).

| Linux | LiteOS-A | Notes |
|---|---|---|
| `struct super_block` + `super_operations` | `struct Mount` + `struct MountOps` | `fs/vfs/include/vnode.h`, `fs/vfs/mount.c` |
| `struct inode` + `struct dentry` | `struct Vnode` (combined; name held by `path_cache`) | `fs/vfs/vnode.c`, `path_cache.c` |
| `inode_operations` + `file_operations` | `struct VnodeOps` + `struct file_operations_vfs` | both attach to Vnode |
| `address_space_operations` | direct bcache calls | `fs/vfs/bcache/` |
| `kmalloc` / `kmem_cache_alloc` | `LOS_MemAlloc(m_aucSysMem0, sz)` / `zalloc` | `kernel/include/los_memory.h` |
| `mutex_lock` / `spin_lock` | `LOS_MuxLock` / `LOS_SpinLock` | `kernel/include/los_mux.h`, `los_spinlock.h` |
| `submit_bio` / `bread` | `los_disk_read` / `los_disk_write` (or `los_part_read` for partition-relative) | `drivers/block/disk/` |
| `strncpy` / `memcpy` | `strncpy_s` / `memcpy_s` (libsec) | mandatory |
| `printk` | `PRINT_ERR` / `PRINT_INFO` / `PRINTK` | `kernel/include/los_printf.h` |
| RCU | (none — use mutex or remove the path) | LiteOS-A has no RCU |
| jbd2 / journal | (: drop journaling) | journal not in scope |
| fscrypt | (: drop encryption) | not supported |

### 12.4 Format compatibility traps (from skill 后续-9)

The codegen prompt MUST instruct LLM to scan for these four traps at code-time
and call `AskUserQuestion` if any apply:

1. **Checksum algorithm**: CRC32 vs CRC32c, MD5 vs SHA-256 — does LiteOS provide a
 compatible implementation? (`LOS_Crc32` is IEEE polynomial; EROFS uses Castagnoli — incompatible.)
2. **Byte order**: on-disk LE/BE/native? Need `LE32_TO_HOST` etc.?
3. **Charset**: UTF-8 / UTF-16LE / GBK — does LiteOS provide conversion? (should
 degrade to ASCII-only.)
4. **Alignment / packed**: on-disk struct uses `__attribute__((packed))` — does ARM
 need explicit unaligned-access helpers?

### 12.5 File generation order (from skill )

Plugin enforces this order when dispatching parallel sub-agents within a stage:

```
1. <name>.h (public types + API decls)
2. <name>_pri.h (private types if any)
3. util/*.c (no external deps)
4. bitmap/*.c, path/*.c (depend on util)
5. inode/*.c, file/*.c (depend on bitmap / path)
6. interface/*.c (depend on everything; called by g_<name>Vops)
7. (no separate vfs_<name>.c — registration glue lives in interface/super.c)
```

Layers 3 and 4 can have parallel sub-agents within them (no inter-target deps);
layer transitions are serial.

### 12.6 Common pitfalls (from skill section "常见陷阱")

The codegen prompt's `[ASK-FIRST RULES]` section explicitly lists these as
ambiguity triggers — LLM must check and ask if uncertain:

- Page cache inertia (using `struct page` from Linux source as if LiteOS had it)
- Skipping `## Refine Prompt` lock-annotation round when the path holds 2+ locks
- Copy/pasting `struct buffer_head` callback names from Linux (1:1 mapping doesn't exist)
- Forgetting libsec (most common — every string/memory call must be reviewed)
- Pasting GPL Linux headers (license violation)
- Single huge `vfs_<name>.c` file (split per `interface/`)


## 12. Validation checklist (Layer 4 user review aid, from PR-DRAFT)

When user reaches Layer 4 (post automatic layers), plugin renders a checklist
with auto-filled status. User can override any line.

### 13.1 Symbol existence
For each LiteOS-A API referenced in generated code, plugin auto-greps the repo:
- ✅ `VnodeAlloc` → `fs/vfs/include/vnode.h`
- ✅ `los_part_find` → `drivers/block/disk/include/disk.h`
- ❌ `exfat_get_root` → not found in any header — **flag for user**

### 13.2 Pattern consistency (auto-checked, surfaced as %)
- Goto-stack reverse-LIFO error handling present? (regex `goto ERROR_`)
- Internal positive errno + final `return -ret`? (regex)
- `bind_check` → `SetDiskPartName` → resource init sequence preserved?
- `mount->data = sbi` AND `mount->vnodeCovered = vp` both set?
- `VfsHashInsert(vp, root_dir)` placed AFTER all internal state initialized?

### 13.3 Style rule compliance (auto-checked)
- Copyright header present and Huawei BSD-3-Clause? (regex on first 30 lines)
- All `strcpy`/`memcpy` are `_s` variants? (grep + count)
- `LOSCFG_FS_<NAME>` ifdef wraps body? (regex)
- No `kmalloc`/`malloc`/`free` outside `LOS_MemAlloc`/`LOS_MemFree` family?
- No `printk`? (replaced with `PRINT_ERR`/`PRINTK`)

### 13.4 Format-compatibility self-check (Layer -1 echo)
- Was each of the 4 traps in §11.4 either irrelevant or explicitly addressed?
- If LLM said "I assumed UTF-8 only", did the spec or user clarification match?

### 13.5 Cross-stage invariant preservation
- For each ancestor invariant ID, plugin generates a static check or asks user to confirm:
 - `mount-locked-on-success` → does the new code path preserve `mount->data != NULL` post-success?
 - `part-name-claimed` → does the new code release `part->part_name` on failure?

### 13.6 QEMU smoke baseline
After Layer 2 passes, plugin runs the smoke commands (mount/umount/remount cycle)
and (optionally) compares against a cached prior-approved log if available:
- Cached baseline output (from prior approved run, if present)
- New version output (this round)
- Diff highlighted

User confirms the new version is functionally equivalent or better. The legacy
hand-written `fs/exfat_backup` reference implementation has been archived outside
the repo and is no longer part of the automated comparison.


## 13. What this is NOT

- NOT specfs's `gen.py` reimplemented — we extend with HITL, ask-first, layered defense, DAG.
- NOT a porting agent that runs unsupervised — every stage's spec AND code requires user approve.
- NOT a replacement for `specfs-port` skill — that skill is the methodology document;
 this plugin is the runtime that enforces the methodology.

## Appendix A: Mapping to SpecFS paper

| Paper concept | Our equivalent | Notes |
|---|---|---|
| `gen.py:49` while True | per-layer retry loop | finer-grained, per-layer |
| `gencode.py:158` codegen_prompt | `prompts/codegen.md` | verbatim + extra segments |
| `gencode.py:200` speceval | `prompts/speceval.md` | retained, opt-in |
| `gencode.py:152` 8-round max | Layer 3 max retries | same |
| `[Previously generated code]` | same | retained |
| `[Modification suggestions]` | same, multi-source | sources tagged: compile/qemu/eval/user |
| `sysspec/specfs/common.header` | `spec/<module>/common.header` | grows automatically post-stage |
| `sysspec/evolvefs/` | `--mode=evolve` | for optimization variants |
| ThreadPoolExecutor | sub-agents within stage | stages serial, files parallel |
| compile + test feedback | Layer 1 + Layer 2 | + Layer 4 user replaces test suite |
| (none) Linux→spec | Loop A | our extension |
| (none) ask-first | Layer -1 | our extension |
| (none) DAG state file | `spec/<module>/.specfs.dag.json` | makes paper's DAG explicit |
| (none) HITL gate | Layer 4 | makes paper's "review patches" explicit |
