<!--
Loop A spec-gen 提示词 — 把 Linux FS 源码抽象成 SYSSPEC spec。
Loop A spec-gen prompt — abstract Linux FS code into a SYSSPEC spec.

History/version: see ../CHANGELOG.md.

Reference upstream specs (paper arxiv 2512.13047 + reference impl):
- $SPECFS_REPO/sysspec/specfs/interface/atomfs_del.spec
- $SPECFS_REPO/sysspec/specfs/interface/atomfs_open.spec
- $SPECFS_REPO/sysspec/specfs/interface/atomfs_rename.spec  (locking)
- $SPECFS_REPO/sysspec/specfs/util/malloc_inode.spec        (helper)
- arxiv 2512.13047 Appendix A.1 dentry_lookup case study

Placeholder syntax: {NAME} substituted by server/prompts.py.
-->

[ROLE]
Abstract a Linux kernel FS module into a SYSSPEC spec for LiteOS-A. Produce
the spec text only; user reviews and approves it.

[INPUTS]
- Linux source root: {LINUX_PATH}  (Read `*_fs.h` / `*_raw.h` first, then per-stage `.c`)
- Module: {MODULE}     Stage: {TARGET_STAGE}     Output: spec/{MODULE}/{SUB_PATH}/{OP}.spec

[FROZEN CONTRACT — already approved spec/{MODULE}/common.header]
{COMMON_HEADER}

[INHERITED INVARIANTS — lazy]
{INHERITED_INVARIANTS}
On demand: `specfs.dag_extract_invariants(module="{MODULE}", node_id="<ancestor-id>")`.

[PRIOR APPROVED SPECS — reference, do not re-emit]
{PRIOR_SPEC_INDEX}

[USER SUGGESTIONS]
{USER_SUGGESTIONS}

[Previously generated spec — only on refine round]
{PREVIOUS_SPEC}

[OUTPUT FORMAT — paper §4.1, complexity-scaled]

A spec is plain text with these segments in order. Per paper §4.1, write
ONLY what the stage's complexity warrants — do NOT pad. Aim:

  Level 1 (straightforward helper, ~30 LoC spec):
    Pre/Post-Condition + (sometimes) Invariant. No System Algorithm.

  Level 2 (intricate logic, ~50-90 LoC spec):
    Add a brief intent / [PROMPT] sentence describing approach.

  Level 3 (performance-critical or non-trivial concurrency, ~80-200 LoC spec):
    Add explicit System Algorithm (phase-granularity steps, NOT C-with-braces-removed).

Reference sizes from paper impl: util helper ~28 LoC, atomfs_del 90 LoC,
atomfs_rename (most complex, lock) 196 LoC. If you exceed 250 LoC for a
single stage, you are over-specifying — re-evaluate.

```
[PROMPT]      one paragraph: destination file (spec [PROMPT] mirrors
              server-side _derive_code_path; see SHARED_FILE_MAP), header
              to include, "single C code block" output rule, optional
              high-level intent (Level 2+).

[RELY]        ```c block: real C decls only — types + helper signatures
              with one-line `// what it does` comment per helper. NO
              "use a mutex" abstractions. NO Phase-1 lock-state language.

[GUARANTEE]   ```c block: function signature(s). May add a short
              calling-convention comment when return semantics need
              clarifying (e.g. "returns -POSIX errno"); not mandatory
              for trivial signatures. NO lock state — that's Phase 2.

[SPECIFICATION]
  **Pre-Condition**: free-form bullets describing inputs / required
                     state. No held locks (those go to Phase 2).
  **Post-Condition**: split by Case when behavior branches.
    **Case 1 (label)**: facts + return value.
    **Case 2 (label)**: ...
  **Invariant** (optional, paper says "sometimes"): free-form prose.
                Identifiers welcome but not mandated; re-cite via prose
                when convenient. Skip when the only invariants come
                from inherited DAG ancestors.
  **System Algorithm** (optional, Level 3 only): phase-granularity
                       steps. Skip when pre/post + a one-sentence intent
                       in [PROMPT] suffices.

## Refine Prompt (only when two-phase trigger fires)
  [RELY]        ```c block: ONLY lock primitives (LOS_MuxLock etc.)
  [SPECIFICATION of <fn>]
    Pre-Condition (lock state) / Post-Condition (lock state) per case
    Initialization-order constraint (when applicable)
    Deadlock note (when applicable)
    System Algorithm (locking phases) (optional)
```

[TWO-PHASE TRIGGER — paper §4.3]

Two-phase is REQUIRED when the function path acquires locks. Detect via:
1. Linux source uses `mutex_lock` / `spin_lock*` / `down_*` / `*_lock_irqsave`, OR
2. Your draft [RELY] forward-declares `LOS_Mux*` / `LOS_Spin*` call primitives
   (LosMux fields inside structs alone do NOT count).

When triggered:
- Phase 1 ([PROMPT] → [SPECIFICATION]): functional contract only. NO held
  locks in Pre/Post-Condition; NO lock state in [GUARANTEE]; lock-bearing
  struct fields OK in [RELY].
- Phase 2 (`## Refine Prompt` block): only adds lock-state delta + ordering.
  NO restating Phase 1 Cases; NO new functional facts; NO signature change.

When NOT triggered: emit `[PROMPT]` then jump straight to `[RELY]` (no
`## First Prompt` separator, no `## Refine Prompt` block at all).

For the full forbidden-list and rationale, fetch
`prompts/two_phase_rules.md` on demand — only if you find yourself
unsure about a borderline case (e.g., is `LOS_MuxInit` Phase 1 or 2?).

[ASK-FIRST RULES — disambiguation only]
{ASK_FIRST_RULES}

[SCOPE GUARDRAILS]
- DO NOT include Linux-only features (RCU, jbd2, fscrypt) unless user requested.
- DO NOT exceed the requested target stage. Mount spec MUST NOT specify R/W ops.
- Linux fast/slow paths: default to slow path; ASK before fast.
- Implementation rules (libsec, FSMAP, partition addressing, primitive choice)
  belong in code-gen, NOT in spec.

[REJECTION CRITERIA]
- [PROMPT] omits file name / header / output rule.
- [RELY] uses abstract names instead of real C decls.
- [SPECIFICATION] has no Pre-Condition or no Post-Condition.
- Spec exceeds the stage scope.
- Two-phase violations: trigger fired but no `## Refine Prompt`; lock
  state leaked into Phase 1; Phase 2 restates Phase 1; signature changed
  in Phase 2; empty `## Refine Prompt` placeholder when not triggered.

Return ONLY the SYSSPEC spec text. No surrounding prose. ```c fences inside
[RELY] / [GUARANTEE] segments per the format above.
