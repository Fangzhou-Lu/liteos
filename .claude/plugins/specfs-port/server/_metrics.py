"""Telemetry for specfs-port: per-tool MCP timing + LLM-round token accounting.

Why this exists
---------------
v0.5.3 (2026-05-08): user wants to know how much time / how many tokens the
plugin spends on each `/specfs-port-{spec,code}` invocation, both at the
MCP tool layer and at the LLM round-trip layer.

What it captures (per tool call, one JSONL line)
-------------------------------------------------
- type: "mcp_tool"
- tool: function name (e.g. "code_gen_submit")
- session_id: from kwargs (None for session_start)
- ts / ts_iso: emission time
- duration_s: wall-clock from server's POV
- input_tokens_est: char-count/4 heuristic on the largest text-shaped kwarg
  (generated_code / generated_spec_text / payload / user_suggestion / ...)
- output_tokens_est: char-count/4 of any prompt the tool returned
- is_llm_round_trigger: bool — return dict carries prompt_for_llm /
  next_prompt / next_step → server just handed off to the LLM
- is_llm_ingest: bool — tool consumes LLM-generated text (via *_submit /
  *_approve)
- error: stringified exception if raised, else None

LLM round-trip timing is computed offline from the gap between successive
events on the same session_id (see specfs_metrics_report.py).

Token estimation: 4-chars-per-token heuristic. Close enough for telemetry;
NOT for billing decisions. Off ~±15% on Chinese-heavy text.

Toggles
-------
- SPECFS_METRICS_OFF=1 — disable all emission (no-op).
- SPECFS_METRICS_LOG=<path> — override log path. Default: <repo>/.specfs-metrics.jsonl.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


# Tools whose return dict carries a prompt for the LLM. Counts as "server
# just kicked an LLM round". The keys span Loop A (`prompt_for_llm`,
# `next_prompt`) and Layer T's variant (`prompt_for_llm`).
LLM_ROUND_TRIGGER_KEYS = ("prompt_for_llm", "next_prompt")

# Tools that ingest LLM-generated text. We count their input tokens as the
# LLM "output token" total for the session.
LLM_INGEST_TOOLS = frozenset({
    "spec_gen_submit", "code_gen_submit", "test_gen_submit",
    "spec_gen_approve", "code_gen_approve", "test_gen_approve",
    "spec_fine_submit",
    "inject_diagnostics",  # diagnostic payload from a build/style/qemu step
})

# Kwargs keys that, if present, hold the largest text payload we'd want
# to count as input tokens. First match wins.
TEXT_PAYLOAD_KEYS = (
    "generated_code", "generated_spec_text", "generated_test_text",
    "polished_spec_text", "final_code", "final_spec_text", "final_test_text",
    "payload", "user_suggestion", "speceval_comments", "prompt_text",
)


_LOCK = threading.Lock()


def _default_log_path() -> Path:
    """Default: `<plugin_root>/.specfs-metrics.jsonl` (covered by plugin
    .gitignore so it never ships with commits). Override via
    SPECFS_METRICS_LOG env var.

    Plugin-local rather than repo-root so the gitignore lives next to the
    plugin and travels with it; the file location is consistent regardless
    of which host project the plugin is dropped into.
    """
    override = os.environ.get("SPECFS_METRICS_LOG")
    if override:
        return Path(override).expanduser().resolve()
    # _metrics.py lives at <plugin_root>/server/_metrics.py
    return Path(__file__).resolve().parent.parent / ".specfs-metrics.jsonl"


def _is_disabled() -> bool:
    return os.environ.get("SPECFS_METRICS_OFF", "").lower() in ("1", "true", "yes", "on")


def estimate_tokens(text: str) -> int:
    """4-chars-per-token heuristic. Close to Claude's actual rate for English
    + code; off ~±15% on Chinese-heavy text. Acceptable for telemetry, not
    for billing."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _extract_input_tokens(kwargs: dict[str, Any]) -> int:
    """Find the first text-shaped kwarg and estimate its token count. Returns 0
    if no recognized payload key is present."""
    for key in TEXT_PAYLOAD_KEYS:
        if key in kwargs and isinstance(kwargs[key], str):
            return estimate_tokens(kwargs[key])
    return 0


def _extract_output_tokens(result: Any) -> int:
    """If the tool returned a dict containing a prompt-shaped string, estimate
    its token count. Useful for measuring the input prompt sent to the LLM."""
    if not isinstance(result, dict):
        return 0
    total = 0
    for key in LLM_ROUND_TRIGGER_KEYS:
        if key in result and isinstance(result[key], str):
            total += estimate_tokens(result[key])
    return total


def _extract_session_id(kwargs: dict[str, Any], result: Any) -> Optional[str]:
    """session_id is in kwargs for almost every tool; session_start is the
    only outlier — extract it from the return value there."""
    sid = kwargs.get("session_id")
    if sid:
        return str(sid)
    if isinstance(result, dict) and "session_id" in result:
        return str(result["session_id"])
    return None


def emit(event: dict[str, Any]) -> None:
    """Append one JSON event to the log file. Never raises — telemetry must
    not crash production."""
    if _is_disabled():
        return
    event.setdefault("ts", time.time())
    event.setdefault("ts_iso", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(event["ts"])))
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    path = _default_log_path()
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # never block production for telemetry


def install_metrics(mcp: Any) -> None:
    """Monkey-patch mcp.tool a SECOND time (after install_default_timeout) so
    every @mcp.tool() registration is wrapped with metrics emission.

    Order of decoration (outer-first → inner-last):
      mcp.tool wrapper            ← install_metrics (this)
        └─ timeout wrapper        ← install_default_timeout
             └─ original function

    Result: metrics see total wall-clock including any timeout return.

    Idempotent — tagged with __specfs_metrics_patched__.
    """
    orig_tool = mcp.tool
    if getattr(orig_tool, "__specfs_metrics_patched__", False):
        return

    def patched_tool(*args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        decorator = orig_tool(*args, **kwargs)

        def outer(fn: Callable[..., Any]) -> Any:
            # Cache the inspect signature once at registration time — used to
            # normalize positional calls to a name→value dict so we can find
            # session_id / generated_code / ... regardless of call style.
            try:
                fn_sig = inspect.signature(fn)
            except (TypeError, ValueError):
                fn_sig = None

            @functools.wraps(fn)
            def wrapper(*a: Any, **kw: Any) -> Any:
                start = time.monotonic()
                err: Optional[str] = None
                result: Any = None
                # Normalize args+kwargs into a single name→value mapping so
                # later extractors don't care about call style. MCP transport
                # always passes kwargs in production, but local tests / CLI
                # smoke calls may pass positional.
                if fn_sig is not None:
                    try:
                        bound = fn_sig.bind_partial(*a, **kw)
                        normalized = dict(bound.arguments)
                    except TypeError:
                        normalized = dict(kw)
                else:
                    normalized = dict(kw)
                try:
                    result = fn(*a, **kw)
                    return result
                except BaseException as e:
                    err = repr(e)
                    raise
                finally:
                    elapsed = time.monotonic() - start
                    in_tok = _extract_input_tokens(normalized)
                    out_tok = _extract_output_tokens(result)
                    sid = _extract_session_id(normalized, result)
                    is_trigger = isinstance(result, dict) and any(
                        k in result for k in LLM_ROUND_TRIGGER_KEYS
                    )
                    is_ingest = fn.__name__ in LLM_INGEST_TOOLS
                    emit({
                        "type": "mcp_tool",
                        "tool": fn.__name__,
                        "session_id": sid,
                        "duration_s": round(elapsed, 4),
                        "input_tokens_est": in_tok,
                        "output_tokens_est": out_tok,
                        "is_llm_round_trigger": is_trigger,
                        "is_llm_ingest": is_ingest,
                        "error": err,
                    })
            return decorator(wrapper)

        return outer

    patched_tool.__specfs_metrics_patched__ = True  # type: ignore[attr-defined]
    patched_tool.__wrapped_orig_tool__ = orig_tool  # type: ignore[attr-defined]
    mcp.tool = patched_tool  # type: ignore[assignment]


# ---- Aggregation (used by CLI report + metrics_summary MCP tool) ----------


@dataclass
class SessionAggregate:
    session_id: Optional[str]
    mcp_tool_calls: int = 0
    mcp_total_duration_s: float = 0.0
    mcp_per_tool: dict[str, int] = field(default_factory=dict)
    llm_rounds: int = 0
    llm_input_tokens_est: int = 0    # tokens we sent INTO the LLM (prompts)
    llm_output_tokens_est: int = 0   # tokens we got BACK from the LLM
    llm_total_gap_s: float = 0.0     # sum of inter-event gaps for this session
    errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "mcp_tool_calls": self.mcp_tool_calls,
            "mcp_total_duration_s": round(self.mcp_total_duration_s, 4),
            "mcp_per_tool": dict(self.mcp_per_tool),
            "llm_rounds": self.llm_rounds,
            "llm_input_tokens_est": self.llm_input_tokens_est,
            "llm_output_tokens_est": self.llm_output_tokens_est,
            "llm_total_gap_s": round(self.llm_total_gap_s, 4),
            "errors": self.errors,
        }


def read_log(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Read a JSONL log file. Returns events in file order. Skips malformed
    lines silently (returns the rest)."""
    p = path or _default_log_path()
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def aggregate(events: list[dict[str, Any]],
              session_id: Optional[str] = None) -> SessionAggregate:
    """Roll up events into a SessionAggregate. If session_id is given, filter
    to events on that session; otherwise aggregate ALL events under
    session_id=None (cross-session view)."""
    agg = SessionAggregate(session_id=session_id)

    # Filter to this session (or pass-through if no filter)
    if session_id is not None:
        events = [e for e in events if e.get("session_id") == session_id]

    # Sort by ts so gap computation is correct even if writes were
    # interleaved across threads.
    events = sorted(events, key=lambda e: e.get("ts", 0))

    last_trigger_ts: Optional[float] = None
    for e in events:
        if e.get("type") != "mcp_tool":
            continue
        tool = e.get("tool", "?")
        agg.mcp_tool_calls += 1
        agg.mcp_total_duration_s += float(e.get("duration_s", 0))
        agg.mcp_per_tool[tool] = agg.mcp_per_tool.get(tool, 0) + 1

        if e.get("error"):
            agg.errors += 1

        out_tok = int(e.get("output_tokens_est", 0))
        in_tok = int(e.get("input_tokens_est", 0))

        # Server-side prompt sent → LLM input tokens
        if e.get("is_llm_round_trigger"):
            agg.llm_input_tokens_est += out_tok
            agg.llm_rounds += 1
            last_trigger_ts = float(e.get("ts", 0))

        # Tool ingested LLM-generated text → LLM output tokens
        if e.get("is_llm_ingest"):
            agg.llm_output_tokens_est += in_tok
            # Compute LLM round-trip gap if we have a prior trigger
            if last_trigger_ts is not None:
                gap = float(e.get("ts", 0)) - last_trigger_ts
                if gap > 0:
                    agg.llm_total_gap_s += gap
                last_trigger_ts = None  # consumed

    return agg
