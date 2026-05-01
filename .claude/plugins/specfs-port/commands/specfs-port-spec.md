---
description: Loop A — generate a SYSSPEC spec from a Linux FS source module via HITL workflow
argument-hint: <linux-path> <target-stage> [--module=<name>]
allowed-tools: ["Bash", "Read", "Write", "Edit", "AskUserQuestion", "Grep", "Glob"]
---

# specfs-port-spec — Loop A (Linux source → SYSSPEC spec)

User invoked: `/specfs-port-spec $ARGUMENTS`

You are running **Loop A** of the specfs-port plugin. Your goal: produce an
approved SYSSPEC specification from a Linux kernel FS module, with the user
in the review loop. The user does NOT write the spec; you draft it, they review,
you refine. Approval gate is theirs.

## Inputs

Parse `$ARGUMENTS`:
- `<linux-path>` — relative or absolute path to Linux FS module dir (e.g., `/Users/kissa/Codebase/linux/fs/exfat` or `linux/fs/exfat`)
- `<target-stage>` — one of: `mount`, `lookup`, `read`, `readdir`, `open`, `close`, `write`, `unlink`, `mkdir`, `rmdir`, `rename`, `truncate`, `symlink`, `getattr`, `statfs`, `umount`, ...
- Optional `--module=<name>` — defaults to `exfat`

If any required arg missing, AskUserQuestion to fill it. Do NOT assume defaults
beyond `module=exfat`.

## Step 1 — start session via MCP

Call `specfs.session_start(module=<module>, mode="gen")`. Receive `{session_id, dag_state}`.

Validate ancestor chain via `specfs.dag_check_node_complete` for each ancestor
of `<target-stage>` per the conventional stage DAG (mount → lookup → readdir/open
→ read → write → ...). If an ancestor's CODE layer is not approved:

- Tell user: "Cannot author <stage> spec; ancestor `<X>-` code is not approved.
 Run `/specfs-port-code spec/<module>/.../<X>.spec` first."
- STOP.

## Step 2 — assemble Loop A prompt via MCP

Call `specfs.spec_gen_start(session_id, linux_path=<linux-path>, target_stage=<target-stage>)`.
Receive `{prompt_for_llm}` — this is the assembled prompt with all injected
segments per `prompts/linux_to_spec.md`.

**Treat the returned prompt as instructions to YOU** (the assistant). Read it
carefully. The prompt expects you to:

1. Use `Read` tool on `<linux-path>` files (start with `*_fs.h`, `*_raw.h`, then
 `super.c` / `inode.c` / per-stage `.c` file) to understand the Linux module.
2. Run the ASK-FIRST scan from `prompts/ask_first_rules.md`.
3. If unresolved ambiguities, use `AskUserQuestion` (multi-choice when possible).
4. After all clarifications: produce a SYSSPEC four-segment spec.

## Step 3 — generate the spec draft

Output the spec text in your response (no fences around the WHOLE spec; the spec
itself uses ```c fences inside [RELY] and [GUARANTEE] segments).

After the spec text, save it to a temp location via Write tool:
`spec/<module>/<sub_path>/<op>.spec.draft`

(Sub-path derives from stage: `interface/` for VFS-level ops like mount/lookup,
`inode/` for inode-level ops like inode_alloc, `file/` for file-level ops like
file_read, etc. Refer to `prompts/style_rules.md §file generation order`.)

## Step 4 — present to user for review

Print:
- Diff vs prior version if this is a refine round, OR full spec if first time
- Auto-collected sanity check:
 - All [RELY] entries are real C decls (not abstract names)?
 - [GUARANTEE] has calling-convention comment block above each fn?
 - [SPECIFICATION] has at least one Invariant with unique id?
 - Any clarifications recorded this session?

Use `AskUserQuestion` with these options:
- (a) Approve — save to `spec/<module>/.../<op>.spec` (drop `.draft`), commit DAG node spec layer
- (b) Suggest edits — user gives free-form feedback
- (c) Reject and rewrite — discard and regenerate from scratch
- (d) Cancel — abandon session

## Step 5 — handle user response

**If approve**: call `specfs.spec_gen_approve(session_id, final_spec_text=<draft content>)`.
The MCP server moves draft → final, updates DAG, returns confirmation. Print
"Saved to <path>; DAG node `<stage>-` spec layer committed."
Suggest: "Next: `/specfs-port-code spec/<module>/.../<op>.spec`".

**If suggest edits**: capture user's free-form feedback. Call
`specfs.spec_gen_refine(session_id, user_suggestion=<text>)` to get a refined
prompt that includes the previous spec + the user's suggestions. Loop back to
Step 3 with this new prompt.

**If reject**: capture user's reason (optional). Call `spec_gen_refine` with
the reason as `user_suggestion="Rewrite from scratch. Reason: <text>"`. Loop
back to Step 3.

**If cancel**: call `specfs.session_end(session_id)`. Print "Session aborted; no
files written." STOP.

## Iteration limits

- Default 5 refine rounds before warning user "Spec keeps drifting; consider
 splitting target stage or starting fresh with clarified requirements."
- No hard cap; user controls.

## Important reminders

- DO NOT write the spec yourself — you are the LLM that drafts; the user is the
 judge. Always present and ask before committing.
- DO NOT skip the ask-first scan. Wasted generation is the highest-cost failure.
- DO NOT use `Edit` to modify already-approved specs (under `spec/<module>/`
 without `.draft` suffix). Those are the frozen contract for descendants.
- DO use `Write` to save drafts and final approved versions.

When all done, print a summary line:
> Approved: spec/exfat/interface/exfat_lookup.spec (<git-sha>) — DAG node lookup spec layer committed
