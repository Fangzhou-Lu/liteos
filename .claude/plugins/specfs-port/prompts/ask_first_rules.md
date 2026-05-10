<!--
"先问后做"指令块 — Loop spec / Loop code 共用，由 linux_to_spec.md 与 codegen.md
分别按需 fetch。
Shared "ask before generate" instruction block — fetched on-demand by both
linux_to_spec.md (Loop spec) and codegen.md (Loop code).

Rationale: ROI of one clarifying question >> generating a wrong artifact
and reviewing 200 lines.

History/version: see ../CHANGELOG.md.
-->

Before producing the artifact, scan ALL inputs for the following four classes
of issues. If ANY occur, STOP and resolve via AskUserQuestion or in-line
question — do NOT proceed.

**Class 1 — Ambiguities (multiple plausible interpretations)**
- Linux module has both fastpath and slowpath — port both, or only one?
- POSIX permits multiple legal behaviors (e.g., readdir on empty dir returning
 -ENOENT vs returning empty list) — which?
- A [RELY] entry has multiple LiteOS-A candidates (`LOS_MuxLock` vs `LOS_SpinLock`,
 `los_disk_read` vs `los_part_read`) — which is correct for THIS code path?
- Spec invariant could mean two things — which?

**Class 2 — Missing information (referenced symbol not in any input)**
- Linux module uses RCU / jbd2 / fscrypt / dcache — LiteOS-A has no equivalent.
 REPLACE / DROP / DEFER to 后续?
- [RELY] references a function that's neither in common.header nor in
 PRIOR CODE INTERFACE — does the user expect us to add it to a prior stage,
 or assume the LLM should infer it from the kernel API?
- Spec specifies an invariant whose preserve mechanism isn't obvious.

**Class 3 — Conflicts (current input contradicts an ancestor invariant)**
- Generating code that would violate an INHERITED INVARIANT — the spec is
 internally consistent but breaks a prior approved stage. Which gives way?
- New common.header symbol clashes with existing entry.

**Class 4 — Out-of-scope features (beyond requested target stage)**
- Spec for `mount` mentions write-path errors — should we omit them or
 is the user signaling we should also generate a write-path stub?
- The Linux source has features (e.g., trim/discard, atomic writes, hugetlb)
 beyond the basic POSIX semantics — include or omit?

## How to ask

Use Claude Code's `AskUserQuestion` tool when the question is multiple-choice.
Otherwise pose questions in your response output as a numbered list, with
your recommended default for each.

## Severity tiers

- **High** (no defensible default; affects correctness):
 MUST AskUserQuestion. Do NOT generate until answered.
- **Medium** (defensible default exists, but worth confirming):
 AskUserQuestion with default option highlighted. Wait for answer.
- **Low** (obvious default; included for transparency):
 Generate with the default and include an `Assumptions made:` line at the
 end of the output. User reviews after, may revise.

## Counts and quotas

- Try to keep AskUserQuestion calls ≤ 5 per generation step. If you have
 more than 5, the spec or input is too vague — ask the user to clarify
 the input AT a higher level rather than fielding many micro-questions.
- Group related questions into one AskUserQuestion when possible (one
 question with 4 sub-options is better than 4 separate questions).

## Example phrasings

> exFAT bitmap dentry can be located in cluster 1 (typical) or chained from root.
> For read-only mount, scan strategy?
> (a) scan first cluster only [recommended — minimal, sufficient for typical layouts]
> (b) follow FAT chain to end [more robust, +30 LOC]

> exFAT volume serial uses CRC-16 / CRC-32. LiteOS-A's `LOS_Crc32` is IEEE poly.
> Verify on mount, or skip?
> (a) skip verification (matches Linux fs/exfat default mount option)
> (b) verify (need to bring in CRC routine)

> The lookup spec [RELY]'s `exfat_get_root_vnode` but mount exposes
> `VfsExfatGetRoot`. Which is correct?
> (a) update spec to use VfsExfatGetRoot (recommended — match frozen code)
> (b) add `exfat_get_root_vnode` as alias in common.header
