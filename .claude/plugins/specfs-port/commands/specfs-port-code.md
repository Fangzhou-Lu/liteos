---
description: Loop B — generate LiteOS-A C code from an approved SYSSPEC spec. Auto-feedback (no HITL) on style/spec deviations; single user-review at end.
argument-hint: <spec-path> [--speceval-off] [--no-build] [--prompt-override <file>] [--no-regress]
allowed-tools: ["Bash", "Read", "Write", "Edit", "AskUserQuestion", "Grep", "Glob", "Agent"]
---

# specfs-port-code — Loop B (SYSSPEC spec → LiteOS-A C code)

User invoked: `/specfs-port-code $ARGUMENTS`

You are running **Loop B** of the specfs-port plugin. Goal: produce approved
LiteOS-A C code from an approved spec, defended by **3 auto layers + 1 user
review**. Layer S (style) is folded into SpecEvaluator — single LLM check, no
HITL gate. Layer T (cmocka test gen) is owned by Loop A (`/specfs-port-spec`)
and is fired in parallel after spec approval; this command does NOT generate
tests.

## Active layers

| # | Layer | When | Auto / HITL |
|---|---|---|---|
| 1 | LSP compile (clangd via OMC LSP) | After draft written | auto-feedback retry, max 4 |
| 2 | Build + QEMU smoke | After SpecEval pass | auto-feedback retry, max 3 (skip with `--no-build`) |
| 3 | SpecEvaluator (now includes style dimensions) | After build pass | auto-feedback retry, max 8 (skip with `--speceval-off`) |
| 4 | User review | After Layer 3 pass | HITL — only gate |

Removed in v0.4 (per arxiv 2512.13047 §4.5 fidelity):
- Layer S as standalone — folded into Layer 3 (single LLM round covers
  spec-conformance + LiteOS style)
- Ask-first at code-gen — disambiguation belongs in spec gen, not code gen
- gcc `-fsyntax-only` fallback — clangd via OMC LSP is the single path
- Layer T (cmocka) — moved to Loop A, fires after `spec_gen_approve`

## Step 0 — parse arguments

Parse `$ARGUMENTS`:
- `<spec-path>` — path to approved spec, e.g. `spec/exfat/interface/exfat_lookup.spec`
- `--speceval-off` — skip Layer 3 (SpecEval+style). Default ON.
- `--no-build` — skip Layer 2. Useful for fast logic-only iteration.
- `--no-regress` — skip Step 8 regression-suite reminder.
- `--prompt-override <file>` — use a hand-written prompt instead of plugin-assembled.

## Step 1 — start session, validate spec

Call `specfs.session_start(module=<derived from path>, mode="gen")`. Receive
`{session_id, dag_state}`.

Verify spec is approved (no `.draft` suffix). If not approved:
- Tell user: "Spec at `<path>` is not approved. Run `/specfs-port-spec` first."
- STOP.

## Step 2 — assemble Loop B prompt

Call `specfs.code_gen_start(session_id, spec_path=<path>)`. Receive
`{prompt_for_llm}` — the assembled codegen prompt with:
- `{LITEOS_DIGEST}` — compact ~1.7K LiteOS-A rules digest (replaces former 22K
  of style_rules + linux_to_liteos_table + format_traps + ask_first_rules)
- `{COMMON_HEADER}` (current `spec/<module>/common.header`)
- `{INHERITED_INVARIANTS}` (from DAG ancestors)
- `{PRIOR_CODE_INTERFACE}` (declarations from frozen ancestor code)
- `{ORIG_SPEC_CONTENT}` (the spec content)

If `--prompt-override <file>` was given, the server uses the override file
verbatim (skipping injection assembly).

## Step 3 — generate code

**On-demand reference expansion** (v0.4): the assembled prompt contains a
compact LITEOS_DIGEST (~1.7K) plus a FRAGMENT INDEX listing four detailed
reference fragments (style_rules, linux_to_liteos_table, format_traps,
ask_first_rules). Before emitting code, scan the spec — if any concrete
uncertainty matches an INDEX entry's "when to fetch" hint, call
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

## Step 4 — Layer 1: LSP compile gate

Call OMC LSP `lsp_diagnostics` for `<draft path>`. clangd sees real LiteOS-A
headers via the repo's `.clangd` config, so it catches both syntax and
semantic issues.

- Empty diagnostics (or only `info` severity) → advance to Layer 2.
- `error` / `warning` present → call
  `specfs.inject_diagnostics(session_id, layer="compile", payload=<diagnostics text>)`
  and loop back to Step 3.

Retry budget: **4 rounds**. If exhausted, escalate to Layer 4 with status
"compile gate exhausted retries".

If clangd is unreachable (no OMC LSP server), proceed to Layer 2 and note
"Layer 1 skipped (no LSP)" in the final summary.

## Step 5 — Layer 2: Build + QEMU smoke (skip if `--no-build`)

Call `specfs.run_build_kernel()`. If `ok=true`, proceed; else inject
`layer="build"` and retry up to 3.

After build passes, call `specfs.run_qemu_smoke(commands=[...])` with the
mount/umount cycle (or stage-specific smoke). On failure inject `layer="qemu"`
and retry up to 3.

## Step 6 — Layer 3: SpecEvaluator + style audit (skip if `--speceval-off`)

Single LLM round. The `prompts/speceval.md` template now flags BOTH
spec-conformance deviations AND LiteOS-A style violations (libsec, allocation,
locking, errno, naming, FSMAP wiring, etc. — see prompt for full list).

Call `specfs.code_gen_submit(session_id, generated_code=<code>)`. The server
returns `next: "speceval"` with the assembled eval prompt.

- Read the eval prompt
- Generate JSON `{is_good: bool, comments: str}` in your response
- Call `specfs.code_gen_submit` again with the JSON
- If `is_good=true` → advance to Layer 4
- If `is_good=false` → **decide root cause from `comments`:**

  **Code-side defect** (signature wrong, missing libsec, wrong locking,
  hallucinated helper, etc. — ~80% of cases): inject as
  `[Modification suggestions]` source=speceval and loop back to Step 3.
  Max 8 rounds (paper §4.5).

  **Spec-side defect** (Pre/Post under-specified, missing case, ambiguous
  invariant — ~20% of cases): call `specfs.spec_fine(session_id,
  speceval_comments=<comments>)` to fetch the SpecFine prompt. Generate the
  polished spec text, then call `specfs.spec_fine_submit(session_id,
  polished_spec_text=<text>)`. Server overwrites the approved spec in place
  and returns `next: "code_regen"`. Loop back to Step 3 — codegen now sees
  the tightened spec.

  **Hard cap on SpecFine: 3 rounds.** When `spec_fine` returns
  `next: "user_review", reason: "spec_fine_cap_exceeded"`, escalate
  immediately to Layer 4 with status "spec_fine cap exhausted; user must
  revise spec by hand or split the stage". Do NOT continue auto-iterating.

## Step 7 — Layer 4: User review

Display `<code draft path>` to user. Optional: render
`prompts/validation_checklist.md` checklist.

`AskUserQuestion`:
- (a) Approve — write file to final path, commit DAG node code layer
- (b) Suggest edits — provide feedback; loops back to Step 3
- (c) Inline-edit — user manually edits draft, then "done"
- (d) Reject and regen — discard, restart Loop B from Step 3
- (e) Reject and revise spec — go back to Loop A; mark spec dirty

## Step 8 — handle user response

**(a) Approve**: call `specfs.code_gen_approve(session_id, final_code=<text>,
files_to_save=[<paths>])`. Server commits DAG node code layer, syncs
common.header. Note: Layer T (cmocka tests) was already approved in Loop A
parallel with code generation; no test approval needed here.

**(b) Suggest edits**: call `specfs.code_gen_refine(session_id, user_suggestion=<text>)`.
Loop back to Step 3.

**(c) Inline-edit**: tell user "Make edits to `<draft>`, then say `done`."
Then call `code_gen_approve` with post-edit content; approval mode = "manual_override".

**(d) Reject and regen**: call `code_gen_refine` with "rewrite from scratch",
loop back to Step 3.

**(e) Reject and revise spec**: call `specfs.session_end(session_id)`. Tell
user: "Run `/specfs-port-spec <linux-path> <stage>` to revise the spec."

## Iteration accounting

- Layer 1 (LSP compile): 4 retries
- Layer 2 (build / qemu): 3 retries each
- Layer 3 (SpecEval+style): 8 retries (paper)
- Layer 4 (user): unlimited

## Step 9 — Holistic SpecValidator (F4, module completion only)

**Per-stage validation in v0.4 ends at Layer 1 (LSP) + Layer 3 (SpecEval +
style).** Build + QEMU smoke + cmocka are gathered into a single holistic
validator pass that runs ONCE per module (paper §4.5; codex audit
recommendation).

**When to fire:**
- This stage is the LAST stage in its module (e.g., the rename stage of
  the dirops module ≤800 LOC budget — see `tools/specfs_eval/BUDGETS.md`)
- OR `--no-regress` is NOT set AND the user explicitly requests it.

**Skip when:**
- The module still has un-approved siblings.
- `--no-regress` was passed.
- The user is in the middle of an iterative spec refine across multiple stages.

**Call:**
```
specfs.validator_run_holistic(module=<module>)
  → {ok, cmocka_pass, qemu_smoke_pass, exit_code, report_path, stderr_tail}
```

The tool wraps `tools/regress/run_all.sh` and returns a single verdict.
Exit codes from the underlying script: 0=pass, 1=test failure, 2=panic-or-hang.

**On failure:**
- If `cmocka_pass=false` → at least one host unit test broke. Diff the report
  vs the prior `_latest.md` to find the new failures; fix root cause (could
  be in any stage of the module, not just this one).
- If `qemu_smoke_pass=false` → kernel/userspace integration broke. Inspect
  `stderr_tail` for panic/oops; if found, recall that FS bug debugging stays
  inside `fs/<module>/` per project memory `feedback_no_common_layer_edits.md`.
- If `exit_code=2` → kernel hung. Investigate; likely a locking deadlock or
  an infinite loop in a spec-conformance fix.

**Per-stage `--no-build` flag:** still honored — skips Layer 2 entirely. The
holistic pass at module completion catches what per-stage build would have
caught, plus cross-stage interactions that per-stage build cannot detect.

## Final output

When code approved + regression reminder issued, print summary:
```
Approved: fs/exfat/exfat_lookup.c (<code-git-sha>)
DAG node lookup: code layer committed.
common.header diff: <…>
SpecEval+style: is_good=true (N rounds, last: "<one-line digest>")
Regression: tools/regress/run_all.sh — please run before pushing.

Review and run: git commit -m "feat(fs/exfat): lookup (specfs-port)"
```
