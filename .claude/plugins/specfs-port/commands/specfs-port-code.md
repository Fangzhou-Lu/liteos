---
description: Loop B — generate LiteOS-A C code from an approved SYSSPEC spec via 7-layer defense + HITL (Style + SpecEvaluator + cmocka test gen self-audits ON by default)
argument-hint: <spec-path> [--speceval-off] [--style-off] [--test-off] [--no-build] [--prompt-override <file>] [--no-regress]
allowed-tools: ["Bash", "Read", "Write", "Edit", "AskUserQuestion", "Grep", "Glob", "Agent"]
---

# specfs-port-code — Loop B (SYSSPEC spec → LiteOS-A C code)

User invoked: `/specfs-port-code $ARGUMENTS`

You are running **Loop B** of the specfs-port plugin. Goal: produce approved
LiteOS-A C code from an approved spec, defended by 6 layers (compile [LSP+gcc] /
style / build+QEMU / SpecEvaluator / cmocka test gen / user). User decides final
approval over BOTH the C code AND the spec-derived cmocka test in one HITL pass.

Note (v0.3.3): Layer 0 (LSP) and Layer 1 (gcc -fsyntax-only) merged into a
single **Layer 1: compile gate**. clangd via OMC LSP is the preferred check
(real headers, no stub-drift); gcc -fsyntax-only with the bundled stub is the
automatic fallback when LSP is unavailable. One retry budget, not two.

Note (v0.3.4): Layer T (spec-derived cmocka test gen) **actually wired** into
the MCP server. Was advertised in the v0.3.2 CHANGELOG as `default ON` but
never implemented — Wave A 9 stages accumulated test debt under the v0.3.2
era, see commit 149487a9 for the catch-up batch. From v0.3.4 forward, Layer T
is a hard gate: every Loop B run produces both the C file AND a `test_<stage>.c`
draft that goes through user review together. Disable per-session with
`--test-off` only when the new code has no host-testable surface (e.g. write
paths whose only verification is QEMU LTP).

## Step 0 — parse arguments

Parse `$ARGUMENTS`:
- `<spec-path>` — path to approved spec, e.g., `spec/exfat/interface/exfat_lookup.spec`
- `--speceval-off` — flag, disable Layer 3 SpecEvaluator self-audit
 (default **ON** as of plugin v0.2: user explicitly required self-audit before
 user review — see CHANGELOG)
- `--style-off` — flag, disable Layer S (coding-style audit)
 (default **ON** as of plugin v0.3: user directive "加入编码风格评估环节")
- `--test-off` — flag, disable Layer T (cmocka test gen)
 (default **ON** as of plugin v0.3.4 — actually wired this round; see CHANGELOG).
 Call `specfs.toggle_test_gen(session_id, enabled=False)` after `session_start`
 if this flag is present.
- `--no-build` — flag, skip Layer 2 (build + QEMU); useful for fast logic-only iteration
- `--no-regress` — flag, skip Step 12 regression-suite reminder
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
- If `is_good`, advance to Layer T (Step 8.5) — NOT directly to Layer 4
- If not, inject comments as `[Modification suggestions]` source=speceval and
 loop back to Step 4 (max 8 rounds per paper).

## Step 8.5 — Layer T: Spec-derived cmocka test gen (default ON, skip with --test-off)

**Hard contract (plugin v0.3.4 — actually wired)**: this layer runs after
Layer 3 (SpecEval) passes, BEFORE the user-review HITL pass (Step 9). User
reviews code AND test together in one transaction so test debt cannot
silently accumulate (which is exactly what happened to Wave A under the
v0.3.2 era — see CHANGELOG and commit 149487a9 for the catch-up batch).

Workflow:

1. Confirm Layer T is enabled (`session_status` → `test_gen_enabled=true`).
   If `--test-off` was set in Step 0, skip this step entirely and note in
   final summary as a coverage gap.

2. Call `specfs.code_gen_approve(session_id, final_code=<text>, files_to_save=[<paths>])`
   with the SpecEval-passed code. Server returns `{next: "test_gen", ...}`
   (NOT `next: "done"`) when Layer T is enabled — code is committed to
   `fs/<module>/`, DAG node code-layer approved, common.header synced,
   but the session phase advances to `test_drafting` instead of terminating.

3. Call `specfs.test_gen_start(session_id)`. Receive
   `{prompt_for_llm, draft_path, final_path, harness_dir_exists}`. The prompt
   is `prompts/unittest_gen.md` substituted with:
   - `{GENERATED_CODE}` — the just-approved C file
   - `{ORIGINAL_SPEC}` — the input spec
   - `{HARNESS_LAYOUT}` — `ls testsuites/unittest/<module>/` snapshot

4. Generate the cmocka test source (one testpoint per spec `[SPECIFICATION]`
   Case + one per testable Invariant) per the prompt's quality gate. Output as
   a single ` ```c ... ``` ` fenced block.

5. Call `specfs.test_gen_submit(session_id, generated_test_text=<code>)`.
   Server writes `<draft_path>` (the `.draft` suffix). Returns `next: "review"`.

6. Surface BOTH `<code draft path>` AND `<test draft path>` to the user in
   Step 9 (Layer 4 user review).

7. If user requests test edits before approval, call
   `specfs.test_gen_refine(session_id, user_suggestion=<text>)` to get the
   re-assembled prompt with the prior draft + user feedback baked in. Loop
   back to substep 4. Max **3 rounds** (test gen is usually quick to
   converge — if it's not, the spec [SPECIFICATION] cases are likely under-
   specified, escalate back to Loop A instead of grinding here).

If `harness_dir_exists` is False (first-time port, no
`testsuites/unittest/<module>/` skeleton yet), warn the user that the test
draft will be written but the Makefile/main.c rewiring will skip — they need
to bootstrap the harness directory first via the cmocka-host-harness reference
guide before re-running.

## Step 9 — Layer 4: User review (code + cmocka test together)

Display BOTH artifacts to the user (Layer T draft path is the `.draft` sibling
of the test final path):

- `<code draft path>` — the just-approved (by Layer 3) C source
- `<test draft path>` — the just-generated cmocka test (only if Layer T ran;
  i.e., `--test-off` was NOT set)

Optional: render `prompts/validation_checklist.md` for the C code if a
`run_user_review_render` tool is available (auto-collected status: symbol
existence, pattern compliance, format-trap echo, invariant preservation, QEMU
baseline diff, build status). The cmocka test is reviewed against the prompt's
own quality-gate checklist (every Case has a testpoint, negative-path errnos
asserted specifically, no Linux-only APIs, etc.).

Then `AskUserQuestion` with five options:
- (a) Approve both — write both files to final paths, commit DAG node code+tests
- (b) Suggest code edits — user provides feedback on the C file
- (c) Suggest test edits — user provides feedback on the cmocka test
- (d) Inline-edit either — user manually edits one or both drafts
- (e) Reject and regen code — discard, restart Loop B from Step 4
- (f) Reject and revise spec — go back to Loop A; mark spec as needing edit

## Step 10 — handle user response

**If approve both** (option a):
1. Code layer was already committed by Layer T (Step 8.5 substep 2 called
   `code_gen_approve` ahead of test gen so Layer T could re-use the final
   text). Verify via `session_status(session_id)` that `phase == "test_drafting"`.
2. Call `specfs.test_gen_approve(session_id, final_test_text=<text>)`.
   - Server: renames `.draft` → `.c`, best-effort applies Makefile
     (`HARNESS_SRCS += test_<stage>.c`) and main.c (extern decl + run_suite
     call) deltas, updates DAG node `tests` block, runs `git add` (no commit).
   - Print: "Saved <test path> ({testpoints} testpoints). Wired into
     {makefile_diff} / {mainc_diff}. DAG node tests layer approved.
     common.header diff:\n<diff>\nReview & `git commit` when ready."
   - If `makefile_diff` or `mainc_diff` is the "(no … change — verify
     manually)" sentinel, surface explicitly: "Wiring failed for
     {Makefile|main.c} — apply manually: {`HARNESS_SRCS += test_<stage>.c`
     | `extern …; run_suite("<stage>", …)`}"

**If suggest code edits** (option b): call
`specfs.code_gen_refine(session_id, user_suggestion=<text>)` to get refined
prompt with `[Modification suggestions]` source=user segment. Loop back to
Step 4. **Both code AND test will be re-generated** because the test depends
on the code text — Layer T fires again after the new code passes Layer 3.

**If suggest test edits** (option c): call
`specfs.test_gen_refine(session_id, user_suggestion=<text>)`. Loop back to
Step 8.5 substep 4 only — no code re-gen.

**If inline-edit** (option d): tell user "Make your edits now to <draft path>,
then say `done`." After "done", call the appropriate approve tool with the
post-edit content. Server records approval mode as "manual_override".

**If reject code-and-regen** (option e): call
`specfs.code_gen_refine(session_id, user_suggestion="rewrite from scratch")`,
loop back to Step 4.

**If reject and revise spec** (option f): call
`specfs.session_end(session_id)`. Tell user: "Run `/specfs-port-spec
<linux-path> <stage>` to revise the spec. Mark `<stage>` dirty."

## Iteration accounting

- Layer 1 (compile, merged) retries: 4 max
- Layer S (style) retries: 5 max
- Layer 2 (build / qemu) retries: 3 max each (per `DESIGN.md §10`)
- Layer 3 (SpecEval) retries: 8 max (paper)
- Layer T (test gen) retries: 3 max (v0.3.4 — escalate to Loop A if exceeded;
  spec is under-specified)
- Layer 4 user iterations: unlimited
- AskUserQuestion calls: warn at 5+ in a single Step 4 round

## Step 11 — (placeholder — Layer T moved to Step 8.5 in v0.3.4)

Empty in v0.3.4. The "spec-derived cmocka test generation" content that
formerly lived here was relocated to Step 8.5 (where it actually belongs in
the pipeline order: after SpecEval, before user review). See CHANGELOG
v0.3.4.

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

When code+test approved (and regression reminder issued or skipped), print summary:
```
Approved: fs/exfat/exfat_lookup.c (<code-git-sha>)
Approved: testsuites/unittest/exfat/test_lookup.c (<test-git-sha>) — N testpoints
DAG node lookup: code layer + tests layer committed.
common.header diff:
+ extern int VfsExfatLookup(struct Vnode *, const char *, int, struct Vnode **);
+ /* Invariant: lookup-found-vnode-cached: ... */
Harness wiring:
+ HARNESS_SRCS += test_lookup.c (testsuites/unittest/exfat/Makefile)
+ run_suite("lookup", test_lookup_tests, ...) (testsuites/unittest/exfat/main.c)

Regression: tools/regress/run_all.sh — please run before pushing.
SpecEvaluator: is_good=true (1 round) | comments: "<one-line digest>"
Layer T: 1 round, N testpoints (if --test-off was set, this line reads "skipped (gap)")

Review and run: git commit -m "feat(fs/exfat): lookup + cmocka tests (HITL specfs-port)"
```
