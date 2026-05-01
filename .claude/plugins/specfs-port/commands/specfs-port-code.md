---
description: Loop B — generate LiteOS-A C code from an approved SYSSPEC spec via 6-layer defense + HITL (Style + SpecEvaluator self-audits ON by default)
argument-hint: <spec-path> [--speceval-off] [--style-off] [--no-build] [--prompt-override <file>] [--no-regress]
allowed-tools: ["Bash", "Read", "Write", "Edit", "AskUserQuestion", "Grep", "Glob", "Agent"]
---

# specfs-port-code — Loop B (SYSSPEC spec → LiteOS-A C code)

User invoked: `/specfs-port-code $ARGUMENTS`

You are running **Loop B** of the specfs-port plugin. Goal: produce approved
LiteOS-A C code from an approved spec, defended by 5 layers (compile [LSP+gcc] /
style / build+QEMU / SpecEvaluator / user). User decides final approval.

Note (v0.3.3): Layer 0 (LSP) and Layer 1 (gcc -fsyntax-only) merged into a
single **Layer 1: compile gate**. clangd via OMC LSP is the preferred check
(real headers, no stub-drift); gcc -fsyntax-only with the bundled stub is the
automatic fallback when LSP is unavailable. One retry budget, not two.

## Step 0 — parse arguments

Parse `$ARGUMENTS`:
- `<spec-path>` — path to approved spec, e.g., `spec/exfat/interface/exfat_lookup.spec`
- `--speceval-off` — flag, disable Layer 3 SpecEvaluator self-audit
 (default **ON** as of plugin v0.2: user explicitly required self-audit before
 user review — see CHANGELOG)
- `--style-off` — flag, disable Layer S (coding-style audit)
 (default **ON** as of plugin v0.3: user directive "加入编码风格评估环节")
- `--no-build` — flag, skip Layer 2 (build + QEMU); useful for fast logic-only iteration
- `--no-regress` — flag, skip Step 11 regression-suite reminder
- `--prompt-override <file>` — flag with arg, use a hand-written `.prompt` file
 instead of plugin-assembled prompt

## Step 1 — start session, validate spec

Call `specfs.session_start(module=<derived from path>, mode="gen")`. Receive
`{session_id, dag_state}`.

Verify the spec is approved (not `.draft` suffix). If not approved:
- Tell user: "Spec at `<path>` is not approved. Run `/specfs-port-spec` first."
- STOP.

## Step 2 — assemble Loop B prompt via MCP

Call `specfs.code_gen_start(session_id, spec_path=<path>)`. Receive
`{prompt_for_llm}` — the assembled codegen prompt with all injection segments
from `prompts/codegen.md`:
- [STYLE RULES] [LINUX→LITEOS PRIMITIVE MAP] [FORMAT-COMPATIBILITY TRAPS]
- [ASK-FIRST RULES]
- [FROZEN CONTRACT] (current spec/<module>/common.header)
- [INHERITED INVARIANTS] (from DAG ancestors)
- [PRIOR CODE INTERFACE] (declarations from frozen ancestor code)
- [PROMPT][RELY][GUARANTEE][SPECIFICATION] (the spec content)

If `--prompt-override <file>` was given, the server uses the override file
verbatim (skipping injection assembly).

## Step 3 — Layer -1: Ask-first

Read the assembled prompt. Run the ASK-FIRST scan per `prompts/ask_first_rules.md`.
If unresolved ambiguities found, use `AskUserQuestion` (preferred) or pose
questions in your response. Do NOT generate code until all blockers resolved.

After clarifications, capture them via `specfs.record_clarification(session_id, ...)`
so they accumulate in the prompt's `[USER CLARIFICATIONS]` segment for any future refine round.

## Step 4 — generate code

Output the C code in your response as a ```c ... ``` fenced block (only the
single C file, no prose). End with `/* Assumptions made: ... */` if Layer -1
made any low-severity defaults explicit.

Save the code to a temp location via Write tool:
`fs/<module>/<derived_path>.draft.c`

The derived path matches the spec's relative path under `spec/<module>/`, but
under `fs/<module>/` and with `.c` extension. Examples:
- `spec/exfat/interface/exfat_mount.spec` → `fs/exfat/exfat_super.c`
 (or `interface/exfat_mount.c` per spec layout convention; refer to
 `prompts/style_rules.md §file generation order`)
- `spec/exfat/file/exfat_read.spec` → `fs/exfat/exfat_file.c`

## Step 5 — Layer 1: Compile gate (LSP + gcc fallback)

**Merged in v0.3.3** — a single check covering both clangd semantic diagnostics
(preferred, sees real LiteOS-A headers via `.clangd`) and gcc `-fsyntax-only`
against the plugin stub header (fallback when LSP is unavailable).

Order:

1. **Try clangd via OMC LSP** — call the OMC LSP MCP tool's `lsp_diagnostics`
 for `<draft path>`. If clangd is reachable and returns:
 - empty diagnostics (or only `info` severity) → compile gate **passes**, advance to Layer S.
 - one or more `error`/`warning` → call
 `specfs.inject_diagnostics(session_id, layer="compile", payload=<diagnostics rendered as text, prefixed "source=lsp">)`
 and loop back to Step 4.

2. **Fallback to gcc** — if clangd is unreachable (no OMC LSP server, file not
 in compile DB, or the LSP call errors out), call
 `specfs.run_compile_check(file_paths=[<draft path>])`. Same pass/fail handling;
 payload is the gcc stderr prefixed `source=gcc`.

Retry budget: **4 rounds** (one more than the old separate Layer 0/1 had each,
since LSP catches a strict superset and may need an extra iteration to fix
semantic-level findings).

If retry count == 4 → escalate to Layer 4 with status
"compile gate exhausted retries (last source: lsp|gcc)".

## Step 6 — Layer S: Coding-style audit (default ON, skip with --style-off)

**Hard contract (plugin v0.3)**: this layer runs by default after Layer 1 syntax
check, BEFORE the expensive Layer 2 build. Style nits are cheaper to fix than
build failures.

Audited dimensions (per `prompts/style_audit.md`):
1. **Naming** — VFS callbacks `Vfs<Fs><Op>`, helpers `<fs>_<verb>_<noun>`,
 on-disk types `struct <fs>_*` from upstream, no Hungarian, no single-letter
 beyond `i j k n m`.
2. **Function complexity** — cyclomatic ≤ 15 (hard ≤ 25), body ≤ 100 lines (hard ≤ 150).
3. **Code layout** — 4-space indent (no tabs), K&R braces, mandatory braces on
 single-statement bodies, BSD-3-Clause Huawei header, `_<NAME>_H` guards,
 `#ifdef LOSCFG_FS_<NAME>` wrap.
4. **Memory & sec-coding** — `LOS_MemAlloc(m_aucSysMem0, ...)` / `zalloc`, every
 string/memory op uses libsec `_s` variant.
5. **Locking** — sleep paths `LOS_MuxLock`, no-sleep paths `LOS_SpinLock`,
 never sleep with spinlock held.
6. **Error path style** — VFS boundary returns negative POSIX errno, internal
 helpers may use positive errno + final `return -ret`, goto-stack labels
 reverse-LIFO.

Workflow:
- Server first runs **auto checks** (`clang-format --dry-run`, libsec regex scan,
 cyclomatic-complexity heuristic). The dry-run output, if any, is captured into
 the prompt's `[AUTO CHECKS]` segment.
- Server then assembles `prompts/style_audit.md` with the generated code and the
 auto-check output → returns prompt for the LLM to self-judge.
- LLM outputs JSON `{is_good, score, summary, violations: [...]}` per the schema.
- If `is_good=true && score >= 80` → advance to Layer 2.
- Else → inject `violations` as `[Modification suggestions]` source=style and
 loop back to Step 4. Max 5 rounds (style is usually quick to fix).

Call:
```python
specfs.code_gen_submit(session_id, generated_code=<code>) → next: "style"
# read prompt, generate JSON, then:
specfs.code_gen_submit(session_id, style_verdict=<json>) → next: "build" | "style" (retry)
```

If `--style-off` is set, the server skips this step and proceeds straight to
build. Note in final summary so a future reviewer can spot the gap.

## Step 7 — Layer 2: build + QEMU smoke (skip if --no-build)

This is the heaviest layer. Only run after Layer 1 passes.

Call `specfs.run_build_kernel()`. If `ok = true`, proceed; else inject `layer="build"`
and retry up to 3.

After build passes, call `specfs.run_qemu_smoke(commands=[...])` with the standard
mount/umount/remount cycle (or stage-specific smoke). If `ok = true`, proceed;
else inject `layer="qemu"` and retry up to 3.

## Step 8 — Layer 3: SpecEvaluator self-audit (default ON, skip with --speceval-off)

**Hard contract (plugin v0.2)**: this layer runs by default. The user explicitly
required SpecEvaluator self-audit *before* surfacing code to user review — see
`docs/exfat_speceval.md` for the mount-stage report (7/7 stage `is_good=true`).

Call `specfs.code_gen_submit(session_id, generated_code=<code>)`. The server
will return either `next: "speceval"` (with prompt) or `next: "review"` (skipping
to Layer 4).

If "speceval":
- Read the eval prompt (verbatim from `prompts/speceval.md`)
- Generate JSON `{is_good: bool, comments: str}` in your response
- Call `specfs.code_gen_submit` again with the JSON
- If `is_good`, advance to Layer 4
- If not, inject comments as `[Modification suggestions]` source=speceval and
 loop back to Step 4 (max 8 rounds per paper).

## Step 9 — Layer 4: User review

Call `specfs.run_user_review_render(session_id, file_paths=[<draft path>])`
which produces the rendered `prompts/validation_checklist.md` filled with
auto-collected status (symbol existence, pattern compliance, format-trap echo,
invariant preservation, QEMU baseline diff, build status).

Display the rendered checklist to the user. Then `AskUserQuestion`:
- (a) Approve — save to final path, commit DAG node code layer, sync common.header
- (b) Suggest edits — user provides free-form feedback
- (c) Inline-edit — user manually edits, plugin records as approved with manual override
- (d) Reject and regen — discard, restart Loop B from Step 4
- (e) Reject and revise spec — go back to Loop A; mark spec as needing edit

## Step 10 — handle user response

**If approve**: call `specfs.code_gen_approve(session_id, final_code=<text>, files_to_save=[<paths>])`.
- Server: writes files to `fs/<module>/`, runs `git add` (no commit), updates
 DAG node, calls `sync_common_header` to extract new exports and append diff.
- Print: "Saved <files>. DAG node `<stage>-` code layer committed.
 common.header diff:\n<diff>\nReview & `git commit` when ready."

**If suggest**: capture feedback. Call `specfs.code_gen_refine(session_id, user_suggestion=<text>)`
to get refined prompt with `[Modification suggestions]` source=user segment.
Loop back to Step 4.

**If inline-edit**: tell user "Make your edits now to <draft path>, then say `done`."
After "done", call `specfs.code_gen_approve` with the post-edit content. Server
records approval mode as "manual_override".

**If reject and regen**: call `specfs.code_gen_refine(session_id, user_suggestion="rewrite from scratch")`,
loop back to Step 4.

**If reject and revise spec**: call `specfs.session_end(session_id)`. Tell user:
"Run `/specfs-port-spec <linux-path> <stage>` to revise the spec. Mark `<stage>` dirty."

## Iteration accounting

- Layer 1 (compile, merged) retries: 4 max
- Layer S (style) retries: 5 max
- Layer 2 (build / qemu) retries: 3 max each (per `DESIGN.md §10`)
- Layer 3 retries: 8 max (paper)
- Layer 4 user iterations: unlimited
- AskUserQuestion calls: warn at 5+ in a single Step 4 round

## Step 11 — Spec-derived cmocka test generation (default ON, skip with --no-regress)

**Hard contract (plugin v0.3.2)**: after Layer S and Layer 3 pass, BEFORE
surfacing to user review (Step 9 / Layer 4), automatically synthesize a draft
`test_<stage>.c` for the cmocka host harness. This pulls regression-test
authorship into the same HITL loop as code authorship — user approves spec,
code, AND tests in one review pass.

Workflow:

1. Assemble the unit-test prompt via `prompts/unittest_gen.md`:
 - {GENERATED_CODE} = the just-approved C file
 - {ORIGINAL_SPEC} = current spec
 - {HARNESS_LAYOUT} = `ls testsuites/unittest/<module>_host/` output
2. Generate `testsuites/unittest/<module>_host/test_<stage>.c.draft`.
 - One testpoint per spec `[SPECIFICATION]` Case
 - One testpoint per testable Invariant (skip those needing QEMU)
 - Uses existing `mock_disk_*` + `<module>_image_builder_*` primitives
 - Includes `// TODO:` block listing Makefile `PROD_SRCS` + main.c registration deltas
3. Surface the test draft together with the code draft in Layer 4 user review.
4. On approval: rename `.draft.c` → `.c` and apply the TODO updates to
 `Makefile` and `main.c` automatically.

If `--no-regress` is set: skip Step 11 entirely, note in final summary as a
gap for future reviewer to backfill.

## Step 12 — Regression suite run reminder

After approval (Step 9 → Step 11 → Layer 4 acks the test draft too):

1. **Layer A** (host cmocka): expected to be auto-built and passing. Run
 `cd testsuites/unittest/<module>_host && make` to confirm.

2. **Layer B** (QEMU LTP smoke): if the new stage adds user-visible behavior
 (mount/lookup/read/write paths), append the smoke case to
 `tools/regress/qemu_<module>_run.sh::run_exfat.sh`'s test list.

3. **Aggregate**: `bash tools/regress/run_all.sh`. Exit 0 must hold.
 Diff `docs/<module>_regression_<ts>.md` against previous report — any
 new fail/pass count must be intentional.

## Final output

When code approved (and regression reminder issued or skipped), print summary:
```
Approved: fs/exfat/exfat_lookup.c (<git-sha>)
DAG node lookup code layer committed.
common.header diff:
+ extern int VfsExfatLookup(struct Vnode *, const char *, int, struct Vnode **);
+ /* Invariant: lookup-found-vnode-cached: ... */

Regression: tools/regress/run_all.sh — please run before pushing.
SpecEvaluator: is_good=true (1 round) | comments: "<one-line digest>"

Review and run: git commit -m "feat(fs/exfat): lookup (HITL specfs-port)"
```
