---
description: Loop B — generate LiteOS-A C code + cmocka tests from an approved SYSSPEC spec. Auto-feedback (no HITL) across compile/style/spec/build gates; single user-review at end.
argument-hint: <spec-path> [--style-off] [--speceval-off] [--no-build] [--test-off] [--prompt-override <file>] [--no-regress]
allowed-tools: ["Bash", "Read", "Write", "Edit", "AskUserQuestion", "Grep", "Glob", "Agent"]
---

# specfs-port-code — Loop B (SYSSPEC spec → LiteOS-A C code + cmocka tests)

User invoked: `/specfs-port-code $ARGUMENTS`

You are running **Loop B** of the specfs-port plugin. Goal: produce approved
LiteOS-A C code AND its cmocka test from an approved spec, defended by
**3 auto layers + 1 user review**. P1.4 (2026-05-07) reorganized the
pipeline:

- **Layer 1a now bundles compile + style** (sequential within the layer:
  LSP first, then style). Was Layer 1a/1b sibling pair in P1.2.
- **Layer 3 SpecEvaluator runs BEFORE Layer 2**, not after. Catch
  spec-conformance defects before paying the build/QEMU cost.
- **Layer 2 is now a unified build-tier gate** that wraps build + cmocka
  unit-test execution + QEMU smoke + holistic SpecValidator into a single
  retry budget. cmocka runs between build and QEMU so cheap regressions
  fail fast before the expensive QEMU pass.
- **Layer T (cmocka test gen) moves from Loop A to Loop B** — generated
  immediately after the C code, so the test sees the actual generated
  symbols rather than a spec abstraction.

## Active layers

| # | Layer | When | Auto / HITL |
|---|---|---|---|
| 1a | Compile + Style (sequential: LSP → style) | After draft written | auto-feedback retry: LSP 4 / style 5 (skip style with `--style-off`) |
| 3 | SpecEvaluator (spec conformance only) | After Layer 1a passes | auto-feedback retry, max 8 (skip with `--speceval-off`) |
| 2 | Build + cmocka unit-test exec + QEMU smoke (= SpecValidator) | After Layer 3 pass | auto-feedback retry, max 3 (skip with `--no-build`) |
| 4 | User review | After Layer 2 pass | HITL — only gate |

P1.4 (2026-05-07) topology change rationale:
- **Style folds back into Layer 1a (sequential, not parallel).** P1.2 had
  promoted style to a sibling of compile (Layer 1b). Running them serially
  inside one Layer 1a keeps each sub-step's diagnostic source crisp (LSP
  error vs. style violation never mixes in the same retry round) while
  collapsing the layer count back to pre-P1.2 simplicity. Retry budgets
  remain separated (LSP 4 / style 5).
- **SpecEval moved before build.** Spec-conformance defects are cheap to
  catch (one LLM round) and expensive to bury under build/QEMU iteration.
  Reordering so SpecEval gates first cuts retry cycles when the codegen
  drifts from the spec.
- **Layer 2 absorbs SpecValidator.** The former `validator_run_holistic`
  pass (build + cmocka + QEMU) at module-completion time is now folded
  into the per-stage Layer 2 contract: build → cmocka unit tests → QEMU
  smoke. The `tools/regress/run_all.sh` aggregator still exists for
  manual / CI runs but per-stage Layer 2 is the in-loop gate.
- **Layer T moves to Loop B.** Tests now generate against real C code
  (function signatures, file paths) instead of being derived from spec
  abstractions. Removes a class of test/code drift bugs where the spec
  promised a helper that the code chose not to expose.

Removed in v0.4 (per arxiv 2512.13047 §4.5 fidelity):
- Ask-first at code-gen — disambiguation belongs in spec gen, not code gen
- gcc `-fsyntax-only` fallback (P1.3 fully removed — clangd via OMC LSP is the single path; this was already implicit in v0.4 but P1.3 deletes the dead `run_compile_check` MCP tool + `_ensure_compile_stub` helper)

## Step 0 — parse arguments

Parse `$ARGUMENTS`:
- `<spec-path>` — path to approved spec, e.g. `spec/exfat/interface/exfat_lookup.spec`
- `--style-off` — skip the style sub-step inside Layer 1a. Default ON.
- `--speceval-off` — skip Layer 3 SpecEvaluator. Default ON.
- `--test-off` — skip Layer T (cmocka test gen) AND skip cmocka exec inside Layer 2. Default ON.
- `--no-build` — skip Layer 2 entirely (no build, no cmocka exec, no QEMU smoke).
- `--no-regress` — skip the final regression-suite reminder.
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

## Step 3a — Layer T: generate cmocka test (skip if `--test-off`)

P1.4 moved Layer T from Loop A → Loop B. The test now sees the actual
generated C symbols (function names, signatures, file path) — no more
test/code drift from speccing helpers the code chose not to expose.

1. Call `specfs.test_gen_start(session_id)` → `{prompt_for_llm, draft_path,
   harness_dir_exists}`.
2. If `harness_dir_exists` is False, warn user that the test file will be
   written but the harness wiring (Makefile / main.c) skip will require
   manual fix-up; proceed anyway.
3. Read the prompt and generate the cmocka test source as a single
   ```c ... ``` fenced block. One testpoint per spec Case + one per testable
   Invariant. Test must reference the actual symbols emitted in Step 3, not
   a spec abstraction.
4. Call `specfs.test_gen_submit(session_id, generated_test_text=<code>)`.
   Server writes `<draft_path>`. Returns `{next: "test_speceval" | "ok"}`.
5. If `next: "test_speceval"`, the server returns a SpecEval-style auto-check
   prompt. Generate JSON `{is_good, comments}`; on `is_good=false` loop back
   to step 3 of THIS sub-step (regenerate the test only — do not re-run
   Step 3 codegen). Max 3 rounds.

Test draft is held — final approval happens together with the code in
Step 7 (Layer 4). Do NOT call `test_gen_approve` here.

## Step 4 — Layer 1a: Compile + Style (sequential)

P1.4 folds the former Layer 1b (style audit) back into Layer 1a as a
sequential second sub-step. LSP runs first; if clean, style runs second.
Each sub-step has its own retry budget. A failure of either sub-step loops
back to Step 3 (regenerate code) with the appropriate diagnostic source.

### Step 4.1 — LSP compile (retry budget: 4)

Call OMC LSP `lsp_diagnostics` for `<draft path>`. clangd sees real LiteOS-A
headers via the repo's `.clangd` config, so it catches both syntax and
semantic issues.

- Empty diagnostics (or only `info` severity) → advance to Step 4.2.
- `error` / `warning` present → call
  `specfs.inject_diagnostics(session_id, layer="compile", payload=<diagnostics text>)`
  and loop back to Step 3.

If clangd is unreachable (no OMC LSP server), proceed to Step 4.2 and note
"Layer 1a.1 skipped (no LSP)" in the final summary.

### Step 4.2 — Style audit (retry budget: 5; skip if `--style-off`)

Call `specfs.code_gen_submit(session_id, generated_code=<code>)`. The server
returns `next: "style_audit"` with the assembled style-audit prompt
(`prompts/style_audit.md` against `prompts/style_rules.md`).

- Read the style-audit prompt
- Generate JSON `{is_good: bool, score: int, summary: str, violations: [...]}` in your response
- Call `specfs.code_gen_submit` again with the JSON
- If `is_good=true && score >= 80` → advance to Step 5 (Layer 3 SpecEval)
- If `is_good=false || score < 80` → inject as
  `[Modification suggestions]` source=style and loop back to Step 3.

Retry budgets are independent: LSP failure consumes only the LSP budget,
style failure consumes only the style budget. If either is exhausted,
escalate to Layer 4 with status "Layer 1a.<sub> exhausted retries".

## Step 5 — Layer 3: SpecEvaluator (skip if `--speceval-off`)

**P1.4 moved this layer BEFORE Layer 2.** Spec-conformance defects are
cheap to catch (one LLM round) and expensive to bury under build/QEMU
iteration. SpecEval (`prompts/speceval.md`) covers spec conformance ONLY —
style was removed in P1.2, gcc fallback in P1.3.

Call `specfs.code_gen_submit(session_id, generated_code=<code>)`. The server
returns `next: "speceval"` with the assembled eval prompt.

- Read the eval prompt
- Generate JSON `{is_good: bool, comments: str}` in your response
- Call `specfs.code_gen_submit` again with the JSON
- If `is_good=true` → advance to Step 6 (Layer 2 build/test/qemu)
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

## Step 6 — Layer 2: Build + cmocka exec + QEMU smoke (skip if `--no-build`)

**P1.4 absorbed SpecValidator into this layer.** Per-stage Layer 2 now runs
the full build-tier verification chain that v0.4's separate `Step 9 holistic
SpecValidator` used to run only at module-completion: build → cmocka → QEMU.
Cheap regressions fail fast before the expensive QEMU pass. Single retry
budget: **3 rounds** for ANY of the three sub-steps.

### Step 6.1 — Kernel build

Call `specfs.run_build_kernel()`.
- `ok=true` → advance to Step 6.2.
- Failure → inject `layer="build"` with stderr tail, loop back to Step 3.

### Step 6.2 — cmocka unit-test exec (skip if `--test-off`)

Call `specfs.validator_run_holistic(module=<module>, qemu=False)` to run
ONLY the cmocka host-side wave (no QEMU yet). Or use the underlying
`tools/regress/run_all.sh --cmocka-only` if a discrete tool is preferred.

The cmocka run targets the test draft generated in Step 3a plus all prior
approved tests for this module.

- All testpoints pass → advance to Step 6.3.
- Failure → identify whether the regression is in:
  - The new test (test was wrong) → inject `layer="qemu"` source=cmocka
    payload=<failure log>, loop back to Step 3a (regenerate test only,
    consumes Layer T retry budget).
  - The new code (code broke a prior assertion) → inject `layer="qemu"`
    source=cmocka payload=<failure log>, loop back to Step 3 (regenerate
    code, consumes Layer 2 retry budget).
  - Use the `cmocka-only` exit code + failure name: prefer code regen if
    the failing testpoint pre-dates this stage; prefer test regen if the
    failing testpoint was just generated.

If `--test-off` was passed, skip this sub-step entirely — no cmocka exec.

### Step 6.3 — QEMU smoke

Call `specfs.run_qemu_smoke(commands=[...])` with the mount/umount cycle
(or stage-specific smoke). On failure inject `layer="qemu"` source=qemu and
loop back to Step 3.

Layer 2 retry budget: 3 rounds total across 6.1 / 6.2 / 6.3. If exhausted,
escalate to Layer 4 with status "Layer 2 build-tier exhausted retries".

## Step 7 — Layer 4: User review

Display `<code draft path>` AND `<test draft path>` to user. Optional:
render `prompts/validation_checklist.md` checklist.

`AskUserQuestion`:
- (a) Approve both — write code + test to final paths, commit DAG node code+tests layers
- (b) Suggest code edits — provide feedback; loops back to Step 3 (re-runs Layer T after)
- (c) Suggest test edits only — provide feedback; loops back to Step 3a only
- (d) Inline-edit — user manually edits draft(s), then "done"
- (e) Reject and regen — discard, restart Loop B from Step 3
- (f) Reject and revise spec — go back to Loop A; mark spec dirty

## Step 8 — handle user response

**(a) Approve both**: call `specfs.code_gen_approve(session_id, final_code=<text>,
files_to_save=[<paths>])`, then `specfs.test_gen_approve(session_id,
final_test_text=<text>)`. Server commits DAG node code+tests layers, syncs
common.header, applies Makefile + main.c deltas.

**(b) Suggest code edits**: call `specfs.code_gen_refine(session_id, user_suggestion=<text>)`.
Loop back to Step 3. Test will be regenerated in Step 3a after the new code lands.

**(c) Suggest test edits only**: call `specfs.test_gen_refine(session_id,
user_suggestion=<text>)`. Loop back to Step 3a only — code stays.

**(d) Inline-edit**: tell user "Make edits to `<draft>` files, then say `done`."
Then call `code_gen_approve` + `test_gen_approve` with post-edit content;
approval mode = "manual_override".

**(e) Reject and regen**: call `code_gen_refine` with "rewrite from scratch",
loop back to Step 3.

**(f) Reject and revise spec**: call `specfs.session_end(session_id)`. Tell
user: "Run `/specfs-port-spec <linux-path> <stage>` to revise the spec."

## Iteration accounting

- Layer 1a.1 (LSP compile): 4 retries
- Layer 1a.2 (style audit): 5 retries
- Layer 3 (SpecEval): 8 retries (paper)
- Layer 2 (build/cmocka/qemu, shared): 3 retries
- Layer T (cmocka test gen, in Step 3a): 3 retries
- SpecFine (Step 5 inner branch): 3 retries
- Layer 4 (user): unlimited

## Step 9 — Module-completion regression reminder (skip if `--no-regress`)

P1.4 made per-stage Layer 2 absorb the build/cmocka/QEMU chain that used
to live only in the holistic validator. The standalone `tools/regress/run_all.sh`
script still exists for manual / CI runs and remains the recommended pre-push
gate, but Layer 2 already covers it per stage.

When this is the LAST stage of its module (e.g., the rename stage of the
dirops module ≤800 LOC budget — see `tools/specfs_eval/BUDGETS.md`), remind
the user to run:

```
bash tools/regress/run_all.sh
```

This re-runs cmocka + QEMU LTP smoke as a single batch and writes a
timestamped report to `docs/test/exfat_regression_<ts>.md` plus updates the
`_latest.md` symlink. Exit codes: 0=pass, 1=test failure, 2=panic-or-hang.

If `--no-regress` was passed, skip this reminder.

## Final output

When code + test approved, print summary:
```
Approved: fs/exfat/exfat_lookup.c (<code-git-sha>)
         testsuites/unittest/exfat/test_lookup.c (<test-git-sha>, N testpoints)
DAG node lookup: code + tests layers committed.
common.header diff: <…>
Layer 1a.1 LSP: clean (N rounds)
Layer 1a.2 style: is_good=true score=<N> (M rounds)
Layer 3 SpecEval: is_good=true (K rounds, last: "<one-line digest>")
Layer 2 build: pass | cmocka: <N>/<N> | qemu_smoke: pass
Regression reminder: tools/regress/run_all.sh — please run before pushing (or rely on per-stage Layer 2 if no cross-stage interactions changed).

Review and run: git commit -m "feat(fs/exfat): lookup (specfs-port)"
```
