<!--
Loop A spec-gen prompt — abstracts Linux FS code into a SYSSPEC spec.

P1.6 (2026-05-08) slim:
- 256 → ~110 lines. Removed verbose two-phase methodology block (now via
  reference to prompts/two_phase_rules.md, fetched on demand).
- Removed [TWO-PHASE METHODOLOGY] forbidden-list expansion (kept as 5-line
  rule). Removed verbose [OUTPUT FORMAT] segment skeleton — replaced with
  a terse 3-block reference.
- Goal: per-call input drops by ~3K (Loop A averaged 25K → 22K).

P1.7 (2026-05-08) integrate additive recommendations from Loop C:
  - Survey-step rule: enumerate every Linux in-memory mutation on
    parent / target / sibling / super-block / dentry-cache; each must
    map to an Invariant or an explicit `# OUT-OF-SCOPE: <reason>` line.
  - Group Pre/Post-Condition bullets under explicit `on parent` /
    `on target` / `on sibling state` sub-headings; no conflation.
  - Tombstone sub-section: stages that set a deletion sentinel
    (DIR_DELETED, FREE_CLUSTER, NULL, …) must enumerate caches to
    evict before the sentinel is visible AND inode-state fields to
    clear so a later evolve-stage cannot re-process the dead entity.

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
  **Pre-Condition**: inputs / state. NO held locks. Bullets MUST be
                     grouped under `on parent`, `on target`, and
                     `on sibling state` sub-headings (omit a heading
                     only if the stage provably has no actor of that
                     role; never conflate the three).
  **Post-Condition**:
    **Case 1 (label)**: facts + return value (same parent/target/sibling
                        sub-headings as Pre-Condition).
    **Case 2 (label)**: ...
  **Tombstone Semantics** (CONDITIONAL — required when this stage sets a
                          deletion sentinel: DIR_DELETED, FREE_CLUSTER,
                          NULL ptr, etc.):
    - Caches to evict BEFORE the sentinel becomes visible (name-resolution
      cache, dentry hash, parent's child-index, …).
    - Inode-state fields that MUST be cleared so a later evolve-stage
      cannot re-process the dead entity (i_size, link count, dirty bits, …).
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
- Mutation-survey rule (VFS callbacks): BEFORE drafting Post-Conditions,
  walk the Linux source and enumerate EVERY in-memory mutation performed
  on (a) the parent inode, (b) the target inode, (c) the parent's
  containing super block, (d) the dentry hash / name cache. Each
  enumerated mutation MUST map to either an Invariant in [SPECIFICATION]
  or an explicit `# OUT-OF-SCOPE: <reason>` comment line. Generic
  hand-waves like "VFS handles it" or "Reclaim handles it" are forbidden.
- Actor-grouping rule: while surveying, classify each dereferenced
  inode/dentry into exactly one of {parent, target, sibling}. Carry that
  classification into the Pre/Post-Condition sub-headings (see
  [OUTPUT FORMAT]). Do not conflate parent and target state.

[REJECTION CRITERIA]
- [PROMPT] omits file name / header / output rule.
- [RELY] uses abstract names instead of real C decls.
- [GUARANTEE] omits the calling-convention comment.
- [SPECIFICATION] has no Invariant or has duplicate IDs.
- [SPECIFICATION] Pre/Post-Condition bullets are not grouped under
  `on parent` / `on target` / `on sibling state` sub-headings (and the
  stage has more than one actor role).
- Mutation-survey gap: a Linux in-memory mutation on parent / target /
  super-block / dentry-cache is neither covered by an Invariant nor
  marked `# OUT-OF-SCOPE: <reason>`.
- Tombstone gap: stage sets a deletion sentinel but the spec lacks a
  **Tombstone Semantics** sub-section (or that sub-section omits either
  the cache-eviction list or the inode-field-clear list).
- Spec exceeds the stage scope.
- Two-phase violations: trigger fired but no `## Refine Prompt`; lock
  state leaked into Phase 1; Phase 2 restates Phase 1; signature changed
  in Phase 2; empty `## Refine Prompt` placeholder when not triggered.

Return ONLY the SYSSPEC spec text. No surrounding prose. ```c fences inside
[RELY] / [GUARANTEE] segments per the format above.
