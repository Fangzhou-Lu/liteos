"""Shared pytest fixtures for specfs-port server tests.

The plugin's modules import each other by bare name (`from state import ...`),
which only works when cwd is the server/ directory. We add server/ to sys.path
at conftest import time so tests can run from any directory and import the
modules cleanly.

Most tests work against a tmp_repo fixture: a temp directory shaped like the
real liteos_a repo (with `spec/<module>/`, `fs/<module>/`, etc.) so DAG load/save
and commit_node operations land in tmp instead of the real repo.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Add server/ to sys.path so `from state import ...` works from tests/
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


@pytest.fixture
def tmp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp directory shaped like the liteos_a repo root.

    Creates the standard subdirectories the plugin expects:
      spec/<module>/  fs/<module>/  testsuites/unittest/<module>/

    Patches state.repo_root() so dag/commit_node/etc. resolve paths inside
    this tmp directory. Tests can write into tmp_repo / "spec" / "exfat" / ...
    and the plugin code will find them.
    """
    # Create the directory shape
    (tmp_path / "spec" / "exfat").mkdir(parents=True)
    (tmp_path / "fs" / "exfat").mkdir(parents=True)
    (tmp_path / "testsuites" / "unittest" / "exfat").mkdir(parents=True)

    # `from state import repo_root` in other modules binds the function into
    # their own namespaces — so we must patch every consumer, not just state.
    import state
    import dag
    import commit_node
    fn = lambda: tmp_path
    monkeypatch.setattr(state, "repo_root", fn)
    monkeypatch.setattr(dag, "repo_root", fn)
    monkeypatch.setattr(commit_node, "repo_root", fn)
    return tmp_path


@pytest.fixture
def fresh_session():
    """A fresh in-memory Session (no DAG side effects)."""
    import state
    return state.new_session("exfat")


@pytest.fixture
def sample_spec_text() -> str:
    """A minimal-but-valid SYSSPEC spec used by multiple tests."""
    return """[PROMPT]
This is the lookup VOP for exfat.

[RELY]
Vnode lookup interfaces:
```c
int VnodeAlloc(struct VnodeOps *ops, struct Vnode **vnode);
int VfsHashGet(struct Mount *mount, uint32_t hash, struct Vnode **vp);
```

[GUARANTEE]
```c
/* Calling convention: caller holds parent vnode lock; returns negative POSIX errno. */
int VfsExfatLookup(struct Vnode *parent, const char *name, struct Vnode **vpp);
```

[SPECIFICATION]
**Pre-Condition**: parent is a directory vnode.
**Post-Condition**: on success, *vpp points at a refcounted child vnode.

**Invariant** (id=lookup-name-resolved):
After VfsExfatLookup returns 0, the named entry exists in parent's directory.

**Invariant** (id=lookup-no-mutation-on-failure):
Failure paths must not mutate parent's vnode state.
"""


@pytest.fixture
def sample_dag(tmp_repo: Path) -> dict:
    """A populated DAG with one approved root node (mount) for ancestor queries."""
    import dag
    d = dag.empty_dag("exfat")
    d["stages"].append({
        "id": "mount-v1",
        "stage_name": "mount",
        "depends_on": [],
        "spec": {"path": "spec/exfat/interface/exfat_mount.spec",
                 "approved_at": "2026-04-30T10:00:00+00:00", "dirty": False},
        "code": {"path": "fs/exfat/exfat_super.c",
                 "approved_at": "2026-04-30T11:00:00+00:00", "dirty": False},
        "invariants": [
            {"id": "mount-locked", "text": "mount->data non-NULL on success."},
            {"id": "mount-part-claimed", "text": "SetDiskPartName paired with free."},
        ],
        "exports": [{"symbol": "VfsExfatMount", "kind": "func"}],
    })
    dag.save("exfat", d)
    return d


@pytest.fixture(autouse=True)
def reset_template_cache():
    """Drop the prompt-template cache between tests so each test sees a fresh load.

    The template cache makes prod faster but is shared global state that can
    leak across tests if a test monkey-patches the prompts dir.
    """
    import prompts
    prompts._TEMPLATE_CACHE.clear()
    yield
    prompts._TEMPLATE_CACHE.clear()


@pytest.fixture
def reset_session_registry():
    """Drop the in-memory session dict in specfs_server between tests.

    The MCP server holds sessions in a module-global dict; cross-test pollution
    would surface as session_id collisions. Tests that exercise MCP tools must
    request this fixture.
    """
    import specfs_server
    specfs_server._SESSIONS.clear()
    yield
    specfs_server._SESSIONS.clear()
