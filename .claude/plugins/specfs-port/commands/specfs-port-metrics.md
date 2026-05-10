---
description: Render specfs-port telemetry report (per-session or all-sessions rollup)
argument-hint: [session_id]
allowed-tools: ["Bash"]
---

# specfs-port — metrics report

You are showing the user a token / latency / round-trip rollup of the specfs-port telemetry log. The user invoked `/specfs-port-metrics` (with optional session id `$ARGUMENTS`).

## Step 1 — resolve session filter

- If `$ARGUMENTS` is empty, aggregate across ALL sessions.
- Otherwise, treat the first space-separated token as the session id.

## Step 2 — call the MCP tool

- OpenCode: `specfs_metrics_report(session_id=<id-or-omit>)`
- Claude Code: `specfs.metrics_report(session_id=<id-or-omit>)`

The tool returns `{markdown, raw}`. The `markdown` field is already the human-friendly table — surface it verbatim. Only fall back to `raw` if `markdown` is empty.

## Step 3 — augment with hints

After the table, add a one-paragraph summary:

- If `prompt_size_max_tokens > 30000`, recommend looking at which tool generated that round (use `prompt_size_per_tool_tokens` to identify).
- If `errors > 0`, recommend `specfs_session_status` for active sessions.
- If `llm_rounds > 5` per stage, suggest looking at why retries fired (build/QEMU/SpecEval failures).

## Step 4 — output

Render the table, then the hints. Do NOT recommend any code changes — this command is read-only diagnostics.
