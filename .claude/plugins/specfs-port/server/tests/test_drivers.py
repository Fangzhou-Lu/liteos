"""Smoke tests for _driver_loop_a.py / _driver_loop_b.py — the standalone CLI
entry points used to exercise the prompt-assembly pipeline without MCP."""
from __future__ import annotations

import sys
from pathlib import Path


def test_loop_a_driver_assembles_prompt(tmp_repo: Path, monkeypatch, capsys):
    """Running `_driver_loop_a.py exfat lookup /linux/path` should emit a
    non-empty assembled prompt to stdout + write .assembled_prompt.txt."""
    import _driver_loop_a

    # Seed the module's repo-root walk: the driver hardcodes parents[4]; tests
    # need the working dir set so the spec/<module>/common.header lookup
    # returns empty (no spec yet, that's fine — placeholder fills in).
    monkeypatch.setattr(sys, "argv",
                        ["_driver_loop_a.py", "exfat", "lookup", "/linux/fs/exfat"])

    # The driver's parents[4] math doesn't account for tmp_repo. To still
    # exercise the assembly path, monkey-patch the spec dir lookups.
    # The simplest path: just call assemble_linux_to_spec_prompt directly — we
    # already cover that in test_prompts.py. Here we drive main() by running
    # in the real repo, which has spec/exfat already.
    rc = _driver_loop_a.main()
    assert rc == 0
    captured = capsys.readouterr().out
    assert "Assembled Loop-A prompt" in captured
    assert "exfat" in captured

    # Driver also writes .assembled_prompt.txt next to itself
    artifact = Path(_driver_loop_a.__file__).parent / ".assembled_prompt.txt"
    assert artifact.is_file()
    assert artifact.stat().st_size > 0


def test_loop_a_driver_bad_args(monkeypatch, capsys):
    import _driver_loop_a
    monkeypatch.setattr(sys, "argv", ["_driver_loop_a.py", "only-one-arg"])
    rc = _driver_loop_a.main()
    assert rc == 2  # usage error


def test_loop_b_driver_bad_args(monkeypatch):
    import _driver_loop_b
    monkeypatch.setattr(sys, "argv", ["_driver_loop_b.py", "only-one-arg"])
    rc = _driver_loop_b.main()
    assert rc == 2


def test_loop_b_driver_missing_node(monkeypatch, capsys):
    """Loop-B driver fails cleanly when the requested node isn't in DAG."""
    import _driver_loop_b
    monkeypatch.setattr(sys, "argv", ["_driver_loop_b.py", "exfat", "ghost-v999"])
    rc = _driver_loop_b.main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "ghost-v999" in out
