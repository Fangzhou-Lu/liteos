"""Unit tests for state.py — Session dataclass + new_session helper."""
from __future__ import annotations


def test_new_session_default_phase_is_idle():
    import state
    s = state.new_session("exfat")
    assert s.phase == "idle"
    assert s.module == "exfat"
    assert s.mode == "gen"
    assert len(s.session_id) == 12  # uuid hex prefix


def test_new_session_unique_ids():
    """Each call returns a distinct session_id."""
    import state
    ids = {state.new_session("exfat").session_id for _ in range(50)}
    assert len(ids) == 50


def test_layer_retries_keys():
    """P1.4 retry budgets cover compile/style/build/qemu/speceval/test_gen/spec_fine.

    Adding/removing keys here is a breaking change — _passed_layers,
    inject_diagnostics, retry-cap logic all depend on this set.
    """
    import state
    s = state.new_session("exfat")
    expected = {"compile", "style", "build", "qemu", "speceval", "test_gen", "spec_fine"}
    assert set(s.layer_retries.keys()) == expected
    assert all(v == 0 for v in s.layer_retries.values())


def test_default_toggle_flags_on():
    """speceval / style_audit / test_gen all default ON (--*-off opts out)."""
    import state
    s = state.new_session("exfat")
    assert s.speceval_enabled is True
    assert s.style_audit_enabled is True
    assert s.test_gen_enabled is True
    assert s.skip_build_layer is False


def test_failure_record_dataclass():
    """FailureRecord captures source layer + payload + round index."""
    import state
    fr = state.FailureRecord(layer="compile", payload="error: X", round_idx=2)
    assert fr.layer == "compile"
    assert fr.payload == "error: X"
    assert fr.round_idx == 2
    assert fr.occurred_at > 0  # default factory used time.time()


def test_repo_root_walks_up_four_levels():
    """state.repo_root() resolves the project root from the server/ dir."""
    import state
    root = state.repo_root()
    # Repo root has fs/ and spec/ at top — both expected in this project.
    assert (root / "fs").is_dir() or root.name == "liteos_a"


def test_clarifications_default_empty():
    import state
    s = state.new_session("exfat")
    assert s.clarifications == []
    assert s.failures == []
    assert s.user_suggestions == []


def test_session_id_is_string_not_uuid_obj():
    """session_id should be JSON-serializable directly (str)."""
    import json
    import state
    s = state.new_session("exfat")
    assert isinstance(s.session_id, str)
    json.dumps({"session_id": s.session_id})  # must not raise
