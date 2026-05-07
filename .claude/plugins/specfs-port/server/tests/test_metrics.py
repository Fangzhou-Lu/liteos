"""Unit tests for _metrics.py — MCP/LLM telemetry collector + aggregator."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest


# ---------- token estimation ----------

def test_estimate_tokens_basic():
    from _metrics import estimate_tokens
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1            # max(1, 0) = 1 floor
    assert estimate_tokens("a" * 4) == 1        # 4//4 = 1
    assert estimate_tokens("a" * 1000) == 250   # 1000//4 = 250


def test_estimate_tokens_handles_unicode():
    """Chinese chars also count as 4-chars-per-token (rough heuristic)."""
    from _metrics import estimate_tokens
    text = "中文字符" * 100  # 400 chars
    assert estimate_tokens(text) == 100


# ---------- emit() / log file behavior ----------

def test_emit_writes_jsonl(tmp_path: Path, monkeypatch):
    from _metrics import emit
    log = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("SPECFS_METRICS_LOG", str(log))
    monkeypatch.delenv("SPECFS_METRICS_OFF", raising=False)

    emit({"type": "mcp_tool", "tool": "session_start", "duration_s": 0.001})
    emit({"type": "mcp_tool", "tool": "session_end", "duration_s": 0.002})

    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    e1 = json.loads(lines[0])
    e2 = json.loads(lines[1])
    assert e1["tool"] == "session_start"
    assert e2["tool"] == "session_end"
    assert "ts" in e1 and "ts_iso" in e1


def test_emit_disabled_via_env(tmp_path: Path, monkeypatch):
    from _metrics import emit
    log = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("SPECFS_METRICS_LOG", str(log))
    monkeypatch.setenv("SPECFS_METRICS_OFF", "1")

    emit({"type": "mcp_tool", "tool": "x", "duration_s": 0.001})
    assert not log.exists(), "metrics emission must respect SPECFS_METRICS_OFF=1"


def test_emit_resilient_to_path_failure(tmp_path: Path, monkeypatch, capsys):
    """emit() must NEVER raise — telemetry can't crash production."""
    from _metrics import emit
    # Point the log at a path whose parent CANNOT be created
    bad = tmp_path / "no" / "such" / "../../../../proc/1/forbidden.jsonl"
    monkeypatch.setenv("SPECFS_METRICS_LOG", str(bad))
    monkeypatch.delenv("SPECFS_METRICS_OFF", raising=False)

    # Should not raise even though the path is hostile
    emit({"type": "mcp_tool", "tool": "x", "duration_s": 0})


# ---------- input/output token extraction ----------

def test_extract_input_tokens_finds_first_match():
    from _metrics import _extract_input_tokens
    # Multiple keys present — first-match-wins per TEXT_PAYLOAD_KEYS order
    out = _extract_input_tokens({
        "generated_code": "a" * 400,
        "user_suggestion": "b" * 100,
    })
    assert out == 100  # 400//4


def test_extract_input_tokens_returns_zero_when_none():
    from _metrics import _extract_input_tokens
    assert _extract_input_tokens({"unrelated": "x"}) == 0
    assert _extract_input_tokens({}) == 0


def test_extract_output_tokens_sums_prompt_keys():
    from _metrics import _extract_output_tokens
    result = {
        "prompt_for_llm": "a" * 200,
        "next_prompt": "b" * 400,
        "ignored": "c" * 800,
    }
    # prompt_for_llm + next_prompt = 50 + 100 = 150
    assert _extract_output_tokens(result) == 150


def test_extract_output_tokens_non_dict_returns_zero():
    from _metrics import _extract_output_tokens
    assert _extract_output_tokens("not a dict") == 0
    assert _extract_output_tokens(None) == 0


# ---------- install_metrics monkey-patch ----------

def test_install_metrics_records_tool_call(tmp_path: Path, monkeypatch):
    from _metrics import install_metrics
    log = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("SPECFS_METRICS_LOG", str(log))
    monkeypatch.delenv("SPECFS_METRICS_OFF", raising=False)

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    fake = FakeMCP()
    install_metrics(fake)

    @fake.tool()
    def example_tool(session_id: str, generated_code: str = "") -> dict[str, str]:
        return {"saved_to": "fs/x.c"}

    out = example_tool(session_id="abc123", generated_code="a" * 400)
    assert out == {"saved_to": "fs/x.c"}

    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    e = json.loads(lines[0])
    assert e["type"] == "mcp_tool"
    assert e["tool"] == "example_tool"
    assert e["session_id"] == "abc123"
    assert e["input_tokens_est"] == 100  # 400 chars / 4
    assert e["output_tokens_est"] == 0   # no prompt-shaped key in return
    assert e["error"] is None


def test_install_metrics_marks_llm_round_trigger(tmp_path: Path, monkeypatch):
    from _metrics import install_metrics
    log = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("SPECFS_METRICS_LOG", str(log))
    monkeypatch.delenv("SPECFS_METRICS_OFF", raising=False)

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    fake = FakeMCP()
    install_metrics(fake)

    @fake.tool()
    def code_gen_start(session_id: str, spec_path: str) -> dict[str, str]:
        return {"prompt_for_llm": "x" * 4000}  # 1000 tokens

    code_gen_start(session_id="s1", spec_path="x.spec")

    e = json.loads(log.read_text(encoding="utf-8").strip())
    assert e["is_llm_round_trigger"] is True
    assert e["is_llm_ingest"] is False
    assert e["output_tokens_est"] == 1000


def test_install_metrics_marks_llm_ingest(tmp_path: Path, monkeypatch):
    from _metrics import install_metrics
    log = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("SPECFS_METRICS_LOG", str(log))
    monkeypatch.delenv("SPECFS_METRICS_OFF", raising=False)

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    fake = FakeMCP()
    install_metrics(fake)

    @fake.tool()
    def code_gen_submit(session_id: str, generated_code: str) -> dict[str, str]:
        return {"next": "user_review"}

    code_gen_submit(session_id="s1", generated_code="x" * 8000)  # 2000 tokens

    e = json.loads(log.read_text(encoding="utf-8").strip())
    assert e["is_llm_ingest"] is True
    assert e["is_llm_round_trigger"] is False
    assert e["input_tokens_est"] == 2000


def test_install_metrics_records_exception(tmp_path: Path, monkeypatch):
    from _metrics import install_metrics
    log = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("SPECFS_METRICS_LOG", str(log))
    monkeypatch.delenv("SPECFS_METRICS_OFF", raising=False)

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    fake = FakeMCP()
    install_metrics(fake)

    @fake.tool()
    def bad_tool(session_id: str = "x") -> Any:
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        bad_tool(session_id="s1")

    e = json.loads(log.read_text(encoding="utf-8").strip())
    assert e["error"] is not None
    assert "kaboom" in e["error"]
    assert e["session_id"] == "s1"


def test_install_metrics_captures_positional_session_id(tmp_path: Path, monkeypatch):
    """Positional-arg calls (CLI / tests) must still surface session_id in the
    log. Production MCP transport always uses kwargs, but the helper uses
    inspect.signature.bind_partial so both styles work."""
    from _metrics import install_metrics
    log = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("SPECFS_METRICS_LOG", str(log))
    monkeypatch.delenv("SPECFS_METRICS_OFF", raising=False)

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    fake = FakeMCP()
    install_metrics(fake)

    @fake.tool()
    def some_tool(session_id: str, payload: str = "") -> dict[str, str]:
        return {"ok": "x"}

    # Positional call — session_id NOT in kwargs
    some_tool("abc-pos", payload="x" * 200)

    e = json.loads(log.read_text(encoding="utf-8").strip())
    assert e["session_id"] == "abc-pos"
    assert e["input_tokens_est"] == 50  # 200//4


def test_install_metrics_idempotent():
    from _metrics import install_metrics

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    fake = FakeMCP()
    install_metrics(fake)
    once = fake.tool
    install_metrics(fake)
    twice = fake.tool
    assert once is twice  # second call is a no-op


# ---------- aggregate / read_log ----------

def test_read_log_returns_events_in_order(tmp_path: Path):
    from _metrics import read_log
    log = tmp_path / "m.jsonl"
    log.write_text(
        json.dumps({"type": "mcp_tool", "tool": "a", "ts": 1.0}) + "\n"
        + json.dumps({"type": "mcp_tool", "tool": "b", "ts": 2.0}) + "\n",
        encoding="utf-8",
    )
    events = read_log(log)
    assert [e["tool"] for e in events] == ["a", "b"]


def test_read_log_skips_malformed_lines(tmp_path: Path):
    from _metrics import read_log
    log = tmp_path / "m.jsonl"
    log.write_text(
        json.dumps({"type": "mcp_tool", "tool": "a"}) + "\n"
        "not-json\n"
        + json.dumps({"type": "mcp_tool", "tool": "b"}) + "\n",
        encoding="utf-8",
    )
    events = read_log(log)
    assert [e["tool"] for e in events] == ["a", "b"]


def test_read_log_missing_file_returns_empty(tmp_path: Path):
    from _metrics import read_log
    assert read_log(tmp_path / "nope.jsonl") == []


def test_aggregate_rolls_up_per_session():
    from _metrics import aggregate
    events = [
        # session A — one llm round
        {"type": "mcp_tool", "tool": "code_gen_start", "session_id": "A",
         "ts": 100.0, "duration_s": 0.05, "input_tokens_est": 0,
         "output_tokens_est": 1000, "is_llm_round_trigger": True,
         "is_llm_ingest": False, "error": None},
        {"type": "mcp_tool", "tool": "code_gen_submit", "session_id": "A",
         "ts": 110.0, "duration_s": 0.02, "input_tokens_est": 500,
         "output_tokens_est": 0, "is_llm_round_trigger": False,
         "is_llm_ingest": True, "error": None},
        # session B — different session, ignored when filtering
        {"type": "mcp_tool", "tool": "session_start", "session_id": "B",
         "ts": 120.0, "duration_s": 0.01, "input_tokens_est": 0,
         "output_tokens_est": 0, "is_llm_round_trigger": False,
         "is_llm_ingest": False, "error": None},
    ]
    agg_a = aggregate(events, session_id="A")
    assert agg_a.mcp_tool_calls == 2
    assert agg_a.mcp_per_tool == {"code_gen_start": 1, "code_gen_submit": 1}
    assert agg_a.llm_rounds == 1
    assert agg_a.llm_input_tokens_est == 1000
    assert agg_a.llm_output_tokens_est == 500
    assert agg_a.llm_total_gap_s == 10.0  # 110 - 100
    assert agg_a.mcp_total_duration_s == pytest.approx(0.07, rel=1e-3)
    assert agg_a.errors == 0


def test_aggregate_counts_errors():
    from _metrics import aggregate
    events = [
        {"type": "mcp_tool", "tool": "x", "session_id": "A",
         "ts": 100.0, "duration_s": 0.01, "input_tokens_est": 0,
         "output_tokens_est": 0, "is_llm_round_trigger": False,
         "is_llm_ingest": False, "error": "ValueError('boom')"},
    ]
    assert aggregate(events, session_id="A").errors == 1


def test_aggregate_no_filter_includes_all():
    """session_id=None (default) aggregates across ALL sessions."""
    from _metrics import aggregate
    events = [
        {"type": "mcp_tool", "tool": "x", "session_id": "A",
         "ts": 100.0, "duration_s": 0.01, "input_tokens_est": 0,
         "output_tokens_est": 0, "is_llm_round_trigger": False,
         "is_llm_ingest": False, "error": None},
        {"type": "mcp_tool", "tool": "y", "session_id": "B",
         "ts": 100.0, "duration_s": 0.02, "input_tokens_est": 0,
         "output_tokens_est": 0, "is_llm_round_trigger": False,
         "is_llm_ingest": False, "error": None},
    ]
    agg = aggregate(events)
    assert agg.mcp_tool_calls == 2
    assert agg.mcp_per_tool == {"x": 1, "y": 1}
    assert agg.session_id is None


# ---------- end-to-end smoke through real specfs_server tool ----------

def test_specfs_server_emits_metrics_on_real_tool(tmp_path: Path, monkeypatch):
    """Smoke: a real specfs_server tool call writes a metrics event."""
    log = tmp_path / "specfs-real-test.jsonl"
    monkeypatch.setenv("SPECFS_METRICS_LOG", str(log))
    monkeypatch.delenv("SPECFS_METRICS_OFF", raising=False)

    # Set up a tmp_repo so session_start doesn't crash
    (tmp_path / "spec" / "exfat").mkdir(parents=True)
    import state
    monkeypatch.setattr(state, "repo_root", lambda: tmp_path)

    import specfs_server
    specfs_server._SESSIONS.clear()
    specfs_server.session_start(module="exfat")

    # The metrics file should now have at least one event
    assert log.is_file()
    text = log.read_text(encoding="utf-8")
    assert "session_start" in text
    e = json.loads(text.strip().split("\n")[0])
    assert e["tool"] == "session_start"
