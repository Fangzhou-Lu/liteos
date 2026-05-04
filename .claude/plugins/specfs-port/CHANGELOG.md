# Changelog — specfs-port plugin

All notable changes to this plugin. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely; semver applies.

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

### Known v0.3.4 server bugs (deferred to v0.3.4.x)

Surfaced during the first native run; both fail open (do not block the
flow) and were worked around manually:

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
