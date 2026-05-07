# Changelog — specfs-port plugin

All notable changes to this plugin. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely; semver applies.

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
