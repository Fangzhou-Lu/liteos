<!--
P1.6 (2026-05-08) — Two-phase methodology fragment, on-demand fetched by
linux_to_spec.md. Holds the verbose forbidden-list / examples / rationale
that previously inflated the Loop A prompt. Loaded only when LLM is
unsure on a borderline case.
-->

# Two-Phase Spec Methodology — Full Rules

Paper §4.1 mandates two logical phases inside ONE spec file:
- **Phase 1**: functional contract (what the function does, ignoring locks).
- **Phase 2**: concurrency contract (locks held / released, ordering, deadlock note).

Markdown markers separate them: `## First Prompt` opens Phase 1, `## Refine Prompt` opens Phase 2.

## Trigger conditions

Two-phase is REQUIRED when EITHER of the following positive checks fires:

1. **Linux source scan** — `{LINUX_PATH}` source files for this stage call any of:
   - `mutex_lock` / `mutex_unlock` / `mutex_lock_interruptible`
   - `spin_lock*` / `spin_unlock*` (incl. `_irq` / `_irqsave` variants)
   - `down_*` / `up_*` (semaphore)
   - `read_lock*` / `write_lock*` (rwlock)
   - `*_lock_irqsave`

2. **Phase 1 [RELY] scan** — your draft would forward-declare any of:
   - `LOS_MuxLock` / `LOS_MuxUnlock` (function-call primitives)
   - `LOS_MuxInit` / `LOS_MuxDestroy`
   - `LOS_SpinLock` / `LOS_SpinUnlock`
   
   **Caveat:** the presence of `LosMux <field>` inside a struct definition
   does NOT count — those are passive lock-bearing data, not call sites.

If neither check fires, Phase 2 MAY be omitted entirely. The
`## First Prompt` separator is then also omitted, and the spec body
starts directly at `[RELY]`.

## Phase 1 forbidden list

The following constructs MUST NOT appear in Phase 1 (they belong to Phase 2):

| Forbidden | Why |
|---|---|
| Pre/Post-Condition saying "holds X lock", "acquires X", "releases X" | Lock state is Phase 2 only. |
| [GUARANTEE] calling-convention block describing held locks on entry/exit | Phase 2 owns lock state. |
| [SPECIFICATION] System Algorithm step like "LOS_MuxLock(...)" | Acquisition belongs to Phase 2. |
| Lock acquisition ordering diagrams | Phase 2 deadlock note covers this. |

What IS allowed in Phase 1:

- `[RELY]` MAY declare lock-bearing struct fields (e.g. `LosMux bitmap_lock` inside `struct exfat_sb_info`).
- `[RELY]` MAY forward-declare lock primitives if the function NEVER calls them at the Phase-1 abstract level.
- `[SPECIFICATION]` System Algorithm MAY name `LOS_MuxInit` because init does not constitute holding a lock — but acquisition / release in any non-init form belongs to Phase 2.

## Phase 2 forbidden list

The following constructs MUST NOT appear in Phase 2 (they belong only to Phase 1, frozen):

| Forbidden | Why |
|---|---|
| Restating Phase 1 Cases verbatim | Phase 2 refines the same function — additive only. |
| New functional Pre/Post-Condition facts | If you discover one, it belongs in a Phase 1 revision. |
| Changing [GUARANTEE] signatures | Signature is frozen after Phase 1. |
| Re-declaring Phase 1 [RELY] helpers | Phase 2 [RELY] is lock-primitives only. |

## Phase 2 contents (the only place these belong)

- `[RELY]` block: lock primitives only — `LOS_MuxLock` / `LOS_MuxUnlock` / `LOS_SpinLock*` etc.
- `[SPECIFICATION of <fn>]`:
  - **Pre-Condition (lock state)**: which locks the caller MUST / MUST-NOT hold on entry.
  - **Post-Condition (lock state)**: which locks held on exit, per Case (success / each error path).
- **Initialization-order constraint** (when applicable): numbered steps for lock init order vs. external visibility. See `exfat_mount.spec` "初始化顺序约束" 5-step example.
- **Deadlock note** (when applicable): why this function cannot deadlock against siblings — typical content: "this function acquires no lock that any visible vnode is currently waiting on".
- **System Algorithm (locking phases)** (optional): named phases with Goal / Algorithm steps / Pre-Post / Error Handling. See `atomfs_rename.spec` Phase 1/2/3 for the canonical pattern.

## Canonical examples

| Spec | Pattern | Notes |
|---|---|---|
| `feature/exfat-port-spec-first:fs/exfat/spec/interface/exfat_mount.spec` | Full two-phase | 5 Cases + goto-stack rollback Invariant; 5-step init-order constraint; deadlock note. |
| `~/Workspace/projects/specfs/sysspec/specfs/eval/loc/spec/delay-alloc/delalloc.spec` | Paper §4.1 reference | Two-phase with AA-deadlock avoidance pattern. |
| `spec/exfat/inode/exfat_unlink.spec` (this repo) | Two-phase, two locks | s_lock + bitmap_lock acquired in disjoint critical sections. |

## Common borderline cases

**Q: My function only calls `LOS_MuxInit` (no lock/unlock). Phase 2 needed?**

A: No. Init is not "holding". Trigger does not fire. Stay single-phase.

**Q: My function passes a `LosMux *` to another helper that internally locks.**

A: Yes — you transitively cause acquisition. Phase 2 is required, and the [RELY] should comment "// callee acquires X" so the reader knows.

**Q: My function reads a struct field that happens to be a `LosMux`.**

A: Just reading the address (e.g., `&sbi->bitmap_lock` to pass it elsewhere)
   is borderline. If the call site is a lock primitive, trigger fires.
   If you're just storing the address, no trigger.

**Q: Two-phase trigger negative, but I want to document the absence of locks.**

A: Don't emit a `## Refine Prompt` placeholder for this. Add one sentence
   to `[PROMPT]` like "This function holds no locks." Done.

## Server-side enforcement

The plugin's `commands/specfs-port-spec.md` Step 4 sanity check runs the
trigger scan automatically; failed gates auto-`spec_gen_refine` and never
reach the user. The LLM's job here is just to author correctly the first
time so the gate doesn't bounce.
