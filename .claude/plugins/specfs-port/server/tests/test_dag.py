"""Unit tests for dag.py — DAG load/save/walk + node-completeness predicates."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_empty_dag_shape():
    import dag
    d = dag.empty_dag("exfat")
    assert d["module"] == "exfat"
    assert d["schema_version"] == dag.SCHEMA_VERSION
    assert d["stages"] == []


def test_load_returns_empty_dag_when_file_missing(tmp_repo: Path):
    import dag
    d = dag.load("exfat")
    assert d == dag.empty_dag("exfat")


def test_save_and_load_round_trip(tmp_repo: Path):
    import dag
    d = dag.empty_dag("exfat")
    d["stages"].append({
        "id": "mount-v1",
        "stage_name": "mount",
        "depends_on": [],
        "spec": {"approved_at": "2026-04-30T10:00:00+00:00"},
    })
    dag.save("exfat", d)
    out = dag.load("exfat")
    assert len(out["stages"]) == 1
    assert out["stages"][0]["id"] == "mount-v1"


def test_load_rejects_schema_mismatch(tmp_repo: Path):
    """A DAG with the wrong schema_version raises ValueError so the user catches
    a stale checkout instead of corrupting state."""
    import dag
    p = dag.dag_path("exfat")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"module": "exfat", "schema_version": 999, "stages": []}\n')
    with pytest.raises(ValueError, match="schema version"):
        dag.load("exfat")


def test_save_is_atomic_via_tmp_file(tmp_repo: Path):
    """save() writes via a .tmp sidecar then renames — verify final file lands
    and no .tmp residue remains."""
    import dag
    d = dag.empty_dag("exfat")
    dag.save("exfat", d)
    p = dag.dag_path("exfat")
    assert p.is_file()
    assert not p.with_suffix(p.suffix + ".tmp").exists()


def test_find_node(sample_dag: dict):
    import dag
    n = dag.find_node(sample_dag, "mount-v1")
    assert n is not None
    assert n["stage_name"] == "mount"

    assert dag.find_node(sample_dag, "nonexistent") is None


def test_find_node_by_stage_name(sample_dag: dict):
    import dag
    n = dag.find_node_by_stage_name(sample_dag, "mount")
    assert n is not None
    assert n["id"] == "mount-v1"

    assert dag.find_node_by_stage_name(sample_dag, "lookup") is None


def test_add_or_update_node_inserts_new(sample_dag: dict):
    import dag
    new_node = {"id": "lookup-v1", "stage_name": "lookup", "depends_on": ["mount-v1"]}
    dag.add_or_update_node(sample_dag, new_node)
    assert dag.find_node(sample_dag, "lookup-v1") is not None


def test_add_or_update_node_replaces_existing(sample_dag: dict):
    import dag
    n = dag.find_node(sample_dag, "mount-v1")
    assert n is not None and n["spec"]["dirty"] is False

    updated = dict(sample_dag["stages"][0])
    updated["spec"] = {"approved_at": "2026-05-01T10:00:00+00:00", "dirty": True}
    dag.add_or_update_node(sample_dag, updated)
    n = dag.find_node(sample_dag, "mount-v1")
    assert n is not None and n["spec"]["dirty"] is True


def test_ancestors_walks_transitive_closure(tmp_repo: Path):
    import dag
    d = dag.empty_dag("exfat")
    d["stages"] = [
        {"id": "a", "stage_name": "a", "depends_on": []},
        {"id": "b", "stage_name": "b", "depends_on": ["a"]},
        {"id": "c", "stage_name": "c", "depends_on": ["b"]},
    ]
    ancs = dag.ancestors(d, "c")
    ids = {a["id"] for a in ancs}
    assert ids == {"a", "b"}


def test_descendants_walks_transitive_closure(tmp_repo: Path):
    import dag
    d = dag.empty_dag("exfat")
    d["stages"] = [
        {"id": "a", "stage_name": "a", "depends_on": []},
        {"id": "b", "stage_name": "b", "depends_on": ["a"]},
        {"id": "c", "stage_name": "c", "depends_on": ["b"]},
    ]
    descs = dag.descendants(d, "a")
    ids = {x["id"] for x in descs}
    assert ids == {"b", "c"}


def test_is_node_complete(sample_dag: dict):
    """sample_dag has mount-v1 with both spec + code approved → complete."""
    import dag
    n = dag.find_node(sample_dag, "mount-v1")
    assert n is not None
    assert dag.is_node_complete(n) is True
    assert dag.is_spec_approved(n) is True
    assert dag.is_code_approved(n) is True


def test_is_node_complete_spec_only_returns_false():
    import dag
    n = {"spec": {"approved_at": "2026-04-30T10:00:00+00:00"}, "code": {}}
    assert dag.is_spec_approved(n) is True
    assert dag.is_code_approved(n) is False
    assert dag.is_node_complete(n) is False


def test_is_tests_approved_legacy_node_returns_false():
    """Pre-v0.3.4 nodes have no `tests` block — must return False."""
    import dag
    n = {"spec": {"approved_at": "x"}, "code": {"approved_at": "y"}}
    assert dag.is_tests_approved(n) is False


def test_is_tests_approved_v034_node():
    import dag
    n = {"tests": {"approved_at": "2026-05-04T..."}}
    assert dag.is_tests_approved(n) is True


def test_collect_invariants_walks_ancestors(tmp_repo: Path):
    import dag
    d = dag.empty_dag("exfat")
    d["stages"] = [
        {"id": "a", "stage_name": "a", "depends_on": [],
         "invariants": [{"id": "inv-a", "text": "A invariant"}]},
        {"id": "b", "stage_name": "b", "depends_on": ["a"],
         "invariants": [{"id": "inv-b", "text": "B invariant"}]},
    ]
    invs = dag.collect_invariants(d, "b")
    ids = [inv["id"] for inv in invs]
    assert "inv-a" in ids
    # b's own invariants are NOT in the collected set — only ancestors


def test_collect_invariants_dedups_by_id(tmp_repo: Path):
    """Same invariant id present in two ancestors should appear once."""
    import dag
    d = dag.empty_dag("exfat")
    d["stages"] = [
        {"id": "a", "stage_name": "a", "depends_on": [],
         "invariants": [{"id": "shared", "text": "first"}]},
        {"id": "b", "stage_name": "b", "depends_on": [],
         "invariants": [{"id": "shared", "text": "second-but-deduped"}]},
        {"id": "c", "stage_name": "c", "depends_on": ["a", "b"]},
    ]
    invs = dag.collect_invariants(d, "c")
    ids = [inv["id"] for inv in invs]
    assert ids.count("shared") == 1


def test_mark_dirty_cascade(tmp_repo: Path):
    import dag
    d = dag.empty_dag("exfat")
    d["stages"] = [
        {"id": "a", "stage_name": "a", "depends_on": [], "spec": {}, "code": {}},
        {"id": "b", "stage_name": "b", "depends_on": ["a"], "spec": {}, "code": {}},
        {"id": "c", "stage_name": "c", "depends_on": ["b"], "spec": {}, "code": {}},
    ]
    dirty = dag.mark_dirty_cascade(d, "a", "spec")
    assert set(dirty) == {"a", "b", "c"}
    assert all(s["spec"]["dirty"] for s in d["stages"])
    # Code layer untouched
    assert not any(s["code"].get("dirty") for s in d["stages"])


def test_list_dirty(tmp_repo: Path):
    import dag
    d = dag.empty_dag("exfat")
    d["stages"] = [
        {"id": "a", "stage_name": "a", "spec": {"dirty": True}, "code": {}},
        {"id": "b", "stage_name": "b", "spec": {}, "code": {"dirty": True}},
        {"id": "c", "stage_name": "c", "spec": {}, "code": {}},  # clean
    ]
    out = dag.list_dirty(d)
    out_dict = dict(out)
    assert out_dict["a"] == ["spec"]
    assert out_dict["b"] == ["code"]
    assert "c" not in out_dict
