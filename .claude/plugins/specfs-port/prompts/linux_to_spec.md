<!--
Loop A spec-gen prompt — abstracts Linux FS code into a SYSSPEC spec.

P1.6 (2026-05-08) slim:
- 256 → ~110 lines. Removed verbose two-phase methodology block (now via
  reference to prompts/two_phase_rules.md, fetched on demand).
- Removed [TWO-PHASE METHODOLOGY] forbidden-list expansion (kept as 5-line
  rule). Removed verbose [OUTPUT FORMAT] segment skeleton — replaced with
  a terse 3-block reference.
- Goal: per-call input drops by ~3K (Loop A averaged 25K → 22K).

Reference upstream specs:
- /Users/kissa/Workspace/projects/specfs/sysspec/specfs/interface/atomfs_open.spec
- /Users/kissa/Workspace/projects/specfs/sysspec/specfs/interface/atomfs_rename.spec  (locking)
- /Users/kissa/Workspace/projects/specfs/sysspec/specfs/util/malloc_inode.spec       (helper)

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

[USER CLARIFICATIONS]
{USER_CLARIFICATIONS}

[USER SUGGESTIONS]
{USER_SUGGESTIONS}

[Previously generated spec — only on refine round]
{PREVIOUS_SPEC}

[OUTPUT FORMAT — paper §4.1, terse]

A spec is plain text with these segments in order:

```
[PROMPT]      one paragraph: destination file (spec [PROMPT] mirrors
              server-side _derive_code_path; see SHARED_FILE_MAP), header
              to include, "single C code block" output rule, optional
              high-level intent. If two-phase fires, end with one sentence
              stating WHY a Refine Prompt section is needed, then `## First Prompt`.

[RELY]        ```c block: real C decls only — types + helper signatures
              with one-line `// what it does` comment per helper. NO
              "use a mutex" abstractions. NO Phase-1 lock-state language.

[GUARANTEE]   ```c block: function signature(s) + calling-convention
              comment block (return value, side effects). NO lock state
              here — that's Phase 2.

[SPECIFICATION]
  **Pre-Condition**: inputs / state. NO held locks.
  **Post-Condition**:
    **Case 1 (label)**: facts + return value
    **Case 2 (label)**: ...
  **Invariant** (id=<module>-<stage>-<noun>): self-contained property
  **System Algorithm** (optional): phase-granularity steps. If it looks
                                   like C with braces removed, drop it.

## Refine Prompt (CONDITIONAL — only when two-phase trigger fires)
  [RELY]        ```c block: ONLY lock primitives (LOS_MuxLock etc.)
  [SPECIFICATION of <fn>]
    Pre-Condition (lock state) / Post-Condition (lock state) per case
    Initialization-order constraint (when applicable)
    Deadlock note (when applicable)
    System Algorithm (locking phases) (optional)
```

[TWO-PHASE TRIGGER — paper §4.1, P1.5 hard gate]

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

[INVARIANT IDS]
- Lowercase-hyphenated, prefixed with module name: `<m>-<stage>-<noun>`.
- Each invariant text self-contained; no IDs reused across stages.

[SCOPE GUARDRAILS]
- DO NOT include Linux-only features (RCU, jbd2, fscrypt) unless user requested.
- DO NOT exceed the requested target stage. Mount spec MUST NOT specify R/W ops.
- Linux fast/slow paths: default to slow path; ASK before fast.
- Implementation rules (libsec, FSMAP, partition addressing, primitive choice)
  belong in code-gen, NOT in spec.

[REJECTION CRITERIA]
- [PROMPT] omits file name / header / output rule.
- [RELY] uses abstract names instead of real C decls.
- [GUARANTEE] omits the calling-convention comment.
- [SPECIFICATION] has no Invariant or has duplicate IDs.
- Spec exceeds the stage scope.
- Two-phase violations: trigger fired but no `## Refine Prompt`; lock
  state leaked into Phase 1; Phase 2 restates Phase 1; signature changed
  in Phase 2; empty `## Refine Prompt` placeholder when not triggered.

Return ONLY the SYSSPEC spec text. No surrounding prose. ```c fences inside
[RELY] / [GUARANTEE] segments per the format above.
