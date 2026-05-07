"""Unit tests for commit_node.py — CLI entrypoint exercising spec/code DAG layer commits."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _spec_with_invariants() -> str:
    return """[PROMPT]
test spec.

[GUARANTEE]
```c
int VfsExfatLookup(struct Vnode *p, const char *n, struct Vnode **vpp);
```

[SPECIFICATION]
**Invariant** (id=lookup-name-resolved):
After VfsExfatLookup returns 0, the named entry exists in parent's directory.

**Invariant** (id=lookup-no-mutation-on-failure):
Failure paths must not mutate parent's vnode state.
"""


def test_parse_invariants_extracts_all_ids(tmp_path: Path):
    import commit_node
    spec_path = tmp_path / "test.spec"
    spec_path.write_text(_spec_with_invariants())
    invs = commit_node.parse_invariants(spec_path)
    ids = {i["id"] for i in invs}
    assert ids == {"lookup-name-resolved", "lookup-no-mutation-on-failure"}


def test_parse_invariants_text_body(tmp_path: Path):
    """Body should be the immediately-following non-blank line(s)."""
    import commit_node
    spec_path = tmp_path / "test.spec"
    spec_path.write_text(_spec_with_invariants())
    invs = commit_node.parse_invariants(spec_path)
    by_id = {i["id"]: i["text"] for i in invs}
    assert "named entry exists" in by_id["lookup-name-resolved"]
    assert "Failure paths" in by_id["lookup-no-mutation-on-failure"]


def test_parse_invariants_missing_file_returns_empty(tmp_path: Path):
    import commit_node
    assert commit_node.parse_invariants(tmp_path / "nope.spec") == []


def test_parse_exports_arg():
    import commit_node
    out = commit_node.parse_exports_arg("g_xVops:var,VfsXMount:func,exfat_fsmap:fsmap")
    assert out == [
        {"symbol": "g_xVops", "kind": "var"},
        {"symbol": "VfsXMount", "kind": "func"},
        {"symbol": "exfat_fsmap", "kind": "fsmap"},
    ]


def test_parse_exports_arg_default_kind_is_func():
    import commit_node
    out = commit_node.parse_exports_arg("Foo,Bar:var")
    assert out == [
        {"symbol": "Foo", "kind": "func"},
        {"symbol": "Bar", "kind": "var"},
    ]


def test_parse_exports_arg_empty():
    import commit_node
    assert commit_node.parse_exports_arg("") == []


def test_cmd_spec_creates_dag_node(tmp_repo: Path):
    """commit_node.cmd_spec writes the spec layer with approval timestamp."""
    import commit_node
    import dag

    spec_dir = tmp_repo / "spec" / "exfat" / "interface"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "exfat_lookup.spec"
    spec_path.write_text(_spec_with_invariants())

    args = SimpleNamespace(
        module="exfat",
        node_id="lookup-v1",
        stage_name="lookup",
        spec_path=str(spec_path),
        depends_on="mount-v1",
        exports="VfsExfatLookup:func",
    )
    rc = commit_node.cmd_spec(args)
    assert rc == 0

    d = dag.load("exfat")
    n = dag.find_node(d, "lookup-v1")
    assert n is not None
    assert n["stage_name"] == "lookup"
    assert n["depends_on"] == ["mount-v1"]
    assert n["spec"]["approved_at"]  # ISO timestamp present
    assert n["spec"]["dirty"] is False
    assert {i["id"] for i in n["invariants"]} == {
        "lookup-name-resolved",
        "lookup-no-mutation-on-failure",
    }
    assert n["exports"] == [{"symbol": "VfsExfatLookup", "kind": "func"}]


def test_cmd_code_requires_spec_layer_first(tmp_repo: Path, capsys):
    """code commit fails if the node has no approved spec."""
    import commit_node
    args = SimpleNamespace(
        module="exfat",
        node_id="ghost-v1",
        code_path="fs/exfat/ghost.c",
    )
    rc = commit_node.cmd_code(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "not in DAG" in err


def test_cmd_code_after_spec_approves_code_layer(tmp_repo: Path):
    """Full happy path: spec → code; both layers approved."""
    import commit_node
    import dag

    # Step 1: commit spec
    spec_dir = tmp_repo / "spec" / "exfat" / "interface"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "exfat_mount.spec"
    spec_path.write_text("[PROMPT]\nmount.\n")
    rc = commit_node.cmd_spec(SimpleNamespace(
        module="exfat", node_id="mount-v1", stage_name="mount",
        spec_path=str(spec_path), depends_on="", exports="",
    ))
    assert rc == 0

    # Step 2: commit code
    code_path = tmp_repo / "fs" / "exfat" / "exfat_super.c"
    code_path.write_text("int VfsExfatMount(...) { return 0; }\n")
    rc = commit_node.cmd_code(SimpleNamespace(
        module="exfat", node_id="mount-v1", code_path=str(code_path),
    ))
    assert rc == 0

    d = dag.load("exfat")
    n = dag.find_node(d, "mount-v1")
    assert n is not None
    assert n["spec"]["approved_at"]
    assert n["code"]["approved_at"]
    assert dag.is_node_complete(n)


def test_cmd_show_dumps_node(tmp_repo: Path, capsys, sample_dag: dict):
    """`commit_node show <module> <node_id>` prints JSON of one node."""
    import commit_node
    rc = commit_node.cmd_show(SimpleNamespace(module="exfat", node_id="mount-v1"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "mount-v1" in out
    assert "VfsExfatMount" in out


def test_cmd_show_unknown_node_returns_1(tmp_repo: Path, capsys, sample_dag: dict):
    import commit_node
    rc = commit_node.cmd_show(SimpleNamespace(module="exfat", node_id="ghost"))
    assert rc == 1
