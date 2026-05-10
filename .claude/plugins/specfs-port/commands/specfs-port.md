---
description: Show specfs-port DAG status for the current FS module and suggest next action
argument-hint: [module-name (default: exfat)]
allowed-tools: ["Bash", "Read"]
---

# specfs-port — status & next action

You are helping the user navigate the specfs-port plugin. The user invoked
`/specfs-port` (with optional module arg `$ARGUMENTS`).

## Step 1 — resolve module name
- Default to `exfat` if `$ARGUMENTS` is empty
- Module name = first space-separated token of `$ARGUMENTS` (lowercase)
- Compute DAG path: `spec/<module>/.specfs.dag.json`

## Step 2 — query plugin state via MCP
Call the DAG status MCP tool:
- OpenCode: `specfs_dag_get(module=<module>)`
- Claude Code: `specfs.dag_get(module=<module>)`

If the DAG file does not exist:
- Show: "No DAG found for module `<module>`. To begin, run:
 `/specfs-port-spec <linux-path> mount`"
- STOP.

## Step 3 — render status table
Print a markdown table summarizing each stage's progress:

```
| Stage | Spec | Code | Validations | Dirty? |
|------------|--------|--------|----------------------|--------|
| mount | ✅ | ✅ | lsp/compile/build/qemu | - |
| lookup | ✅ | ⏳ | - | - |
| readdir | - | - | - | - |
```

Legend:
- ✅ approved at <git-sha>
- ⏳ in progress (current session, not committed)
- ❌ failed (with retry count)
- - not started

## Step 4 — suggest next action
Based on DAG state, suggest exactly ONE next command:

- If a stage's spec is approved but code is not → `/specfs-port-code <spec-path>`
- If a stage's spec is in progress → continue current session
- If all current stages done, suggest the next conventional stage:
 mount → lookup → readdir → open → read → write → ...
- If any node is marked dirty, list those first with the action options
 (re-validate / cascade-regen / dismiss).

## Step 5 — display
Print the status table + the single recommended next command. Do NOT execute
that command — let the user explicitly invoke it.

If the user has un-handled DAG dirty flags, surface them prominently with the
suggested resolution per `DESIGN.md §9.1`.
