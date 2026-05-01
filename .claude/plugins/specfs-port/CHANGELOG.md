# Changelog — specfs-port plugin

All notable changes to this plugin. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely; semver applies.

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
