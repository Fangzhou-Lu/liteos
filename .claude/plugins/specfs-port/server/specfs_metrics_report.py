"""CLI helper: pretty-print specfs-port metrics from a JSONL log.

Usage:
    cd .claude/plugins/specfs-port/server
    uv run python specfs_metrics_report.py                  # all sessions, default log path
    uv run python specfs_metrics_report.py --session <ID>   # filter to one session
    uv run python specfs_metrics_report.py --log <path>     # custom log path
    uv run python specfs_metrics_report.py --json           # machine-readable output

Output (default human-readable):

    === specfs-port metrics ===
    log: /repo/.specfs-metrics.jsonl
    sessions: 3
    total events: 142
    --
    [session abc123def456]
      MCP tool calls : 27
      MCP duration   : 1.342 s  (avg 0.050 s, max 0.310 s)
      LLM rounds     : 8
      LLM input tok  : 31420 (~$0.094 @ Claude Opus pricing)
      LLM output tok : 4280  (~$0.064 @ Claude Opus pricing)
      LLM round-trip : 142.4 s  (avg 17.8 s, slowest 38.1 s)
      Errors         : 0
      Top tools:
        code_gen_submit         : 6
        inject_diagnostics      : 5
        code_gen_start          : 1
        ...
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Make `from _metrics import ...` work when run from server/
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _metrics import _default_log_path, aggregate, read_log  # noqa: E402


# Rough Claude Opus 4 pricing (USD per million tokens). Update as needed.
PRICE_INPUT_PER_MTOK = 15.0
PRICE_OUTPUT_PER_MTOK = 75.0


def _list_sessions(events: list[dict[str, Any]]) -> list[str]:
    return sorted({str(e.get("session_id")) for e in events if e.get("session_id")})


def _per_tool_durations(events: list[dict[str, Any]],
                        session_id: str | None) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for e in events:
        if session_id is not None and e.get("session_id") != session_id:
            continue
        tool = e.get("tool", "?")
        out[tool].append(float(e.get("duration_s", 0)))
    return out


def _llm_round_durations(events: list[dict[str, Any]],
                         session_id: str | None) -> list[float]:
    """Walk events to extract gap (in seconds) between consecutive
    is_llm_round_trigger → is_llm_ingest pairs."""
    rounds: list[float] = []
    last_trigger_ts: float | None = None
    filtered = [e for e in events
                if e.get("type") == "mcp_tool"
                and (session_id is None or e.get("session_id") == session_id)]
    filtered.sort(key=lambda e: e.get("ts", 0))
    for e in filtered:
        if e.get("is_llm_round_trigger"):
            last_trigger_ts = float(e.get("ts", 0))
        elif e.get("is_llm_ingest") and last_trigger_ts is not None:
            gap = float(e.get("ts", 0)) - last_trigger_ts
            if gap > 0:
                rounds.append(gap)
            last_trigger_ts = None
    return rounds


def _format_session(events: list[dict[str, Any]], session_id: str | None) -> list[str]:
    agg = aggregate(events, session_id=session_id)
    per_tool = _per_tool_durations(events, session_id)
    durations = [d for ds in per_tool.values() for d in ds]
    rounds = _llm_round_durations(events, session_id)

    lines: list[str] = []
    label = session_id or "(all sessions)"
    lines.append(f"[session {label}]")
    lines.append(f"  MCP tool calls : {agg.mcp_tool_calls}")
    if durations:
        avg = sum(durations) / len(durations)
        peak = max(durations)
        lines.append(
            f"  MCP duration   : {agg.mcp_total_duration_s:.3f} s "
            f"(avg {avg:.3f} s, max {peak:.3f} s)"
        )
    else:
        lines.append("  MCP duration   : 0.000 s")
    lines.append(f"  LLM rounds     : {agg.llm_rounds}")
    in_cost = agg.llm_input_tokens_est / 1e6 * PRICE_INPUT_PER_MTOK
    out_cost = agg.llm_output_tokens_est / 1e6 * PRICE_OUTPUT_PER_MTOK
    lines.append(
        f"  LLM input tok  : {agg.llm_input_tokens_est} (~${in_cost:.4f} @ Opus)"
    )
    lines.append(
        f"  LLM output tok : {agg.llm_output_tokens_est} (~${out_cost:.4f} @ Opus)"
    )
    if rounds:
        avg = sum(rounds) / len(rounds)
        peak = max(rounds)
        lines.append(
            f"  LLM round-trip : {sum(rounds):.1f} s "
            f"(avg {avg:.1f} s, slowest {peak:.1f} s)"
        )
    else:
        lines.append("  LLM round-trip : (no completed rounds)")
    lines.append(f"  Errors         : {agg.errors}")
    if per_tool:
        top = sorted(per_tool.items(), key=lambda kv: -len(kv[1]))[:8]
        lines.append("  Top tools:")
        for tool, ds in top:
            lines.append(f"    {tool:<25}: {len(ds)} ({sum(ds):.3f} s total)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="specfs_metrics_report",
                                     description="Aggregate specfs-port metrics from a JSONL log.")
    parser.add_argument("--log", type=Path, default=None,
                        help="Path to JSONL log (default: $SPECFS_METRICS_LOG or <repo>/.specfs-metrics.jsonl)")
    parser.add_argument("--session", type=str, default=None,
                        help="Filter to one session_id (omit to roll up all sessions)")
    parser.add_argument("--json", dest="emit_json", action="store_true",
                        help="Emit machine-readable JSON instead of human-readable table")
    args = parser.parse_args(argv)

    log_path = args.log or _default_log_path()
    if not log_path.is_file():
        print(f"No metrics log at {log_path}", file=sys.stderr)
        return 1

    events = read_log(log_path)
    if not events:
        print(f"Log {log_path} is empty.", file=sys.stderr)
        return 1

    if args.emit_json:
        if args.session is None:
            sessions = _list_sessions(events) or [None]
            payload = {sid or "all": aggregate(events, session_id=sid).as_dict()
                       for sid in sessions}
        else:
            payload = {args.session: aggregate(events, session_id=args.session).as_dict()}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print("=== specfs-port metrics ===")
    print(f"log: {log_path}")
    sessions = _list_sessions(events)
    print(f"sessions: {len(sessions)}")
    print(f"total events: {len(events)}")
    print("--")

    if args.session is not None:
        for line in _format_session(events, args.session):
            print(line)
        return 0

    # All-sessions rollup first, then per-session breakdown
    for line in _format_session(events, None):
        print(line)
    if sessions:
        print("--")
        for sid in sessions:
            for line in _format_session(events, sid):
                print(line)
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
