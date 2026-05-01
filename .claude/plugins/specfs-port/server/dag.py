"""
DAG state load/save/walk for specfs-port.

Each FS module has its own DAG file at:
    spec/<module>/.specfs.dag.json

DAG nodes have two layers: spec and code. Both must be approved to consider
the node "complete". Code generation can begin only after the spec layer is
approved. Subsequent stages can begin only after their ancestor's CODE layer
is approved.

Schema documented in DESIGN.md §6.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from state import repo_root


SCHEMA_VERSION = 1


def dag_path(module: str) -> Path:
    return repo_root() / "spec" / module / ".specfs.dag.json"


def empty_dag(module: str) -> dict[str, Any]:
    return {
        "module": module,
        "schema_version": SCHEMA_VERSION,
        "stages": [],
    }


def load(module: str) -> dict[str, Any]:
    """Load DAG state, or return an empty DAG if file does not exist."""
    p = dag_path(module)
    if not p.exists():
        return empty_dag(module)
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"DAG schema version mismatch at {p}: "
            f"expected {SCHEMA_VERSION}, got {data.get('schema_version')}"
        )
    return data


def save(module: str, dag: dict[str, Any]) -> None:
    """Write DAG state atomically. Create parent dir if missing."""
    p = dag_path(module)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(dag, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(p)


def find_node(dag: dict[str, Any], node_id: str) -> Optional[dict[str, Any]]:
    """Return the stage dict matching node_id, or None."""
    for stage in dag.get("stages", []):
        if stage.get("id") == node_id:
            return stage
    return None


def find_node_by_stage_name(dag: dict[str, Any], stage_name: str) -> Optional[dict[str, Any]]:
    """Return the most recent (last) stage with matching stage_name (e.g., 'lookup')."""
    matches = [s for s in dag.get("stages", []) if s.get("stage_name") == stage_name]
    return matches[-1] if matches else None


def add_or_update_node(dag: dict[str, Any], node: dict[str, Any]) -> None:
    """Insert a new stage or replace an existing one with the same id."""
    existing = find_node(dag, node["id"])
    if existing:
        existing.clear()
        existing.update(node)
    else:
        dag.setdefault("stages", []).append(node)


def ancestors(dag: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    """Return list of ancestor stage dicts (transitive closure of depends_on)."""
    target = find_node(dag, node_id)
    if not target:
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    queue: deque[str] = deque(target.get("depends_on", []))
    while queue:
        anc_id = queue.popleft()
        if anc_id in seen:
            continue
        seen.add(anc_id)
        anc = find_node(dag, anc_id)
        if anc is None:
            # missing ancestor — caller may want to flag this
            continue
        out.append(anc)
        queue.extend(anc.get("depends_on", []))
    return out


def descendants(dag: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    """Return list of descendant stage dicts (transitive closure of reverse-edges)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    queue: deque[str] = deque([node_id])
    while queue:
        ancestor_id = queue.popleft()
        for stage in dag.get("stages", []):
            if ancestor_id in stage.get("depends_on", []):
                if stage["id"] in seen:
                    continue
                seen.add(stage["id"])
                out.append(stage)
                queue.append(stage["id"])
    return out


def is_node_complete(node: dict[str, Any]) -> bool:
    """Both spec and code layers approved."""
    return bool(
        node.get("spec", {}).get("approved_at")
        and node.get("code", {}).get("approved_at")
    )


def is_spec_approved(node: dict[str, Any]) -> bool:
    return bool(node.get("spec", {}).get("approved_at"))


def is_code_approved(node: dict[str, Any]) -> bool:
    return bool(node.get("code", {}).get("approved_at"))


def collect_invariants(dag: dict[str, Any], node_id: str) -> list[dict[str, str]]:
    """Walk ancestors of node_id and merge their invariants. Returns flat list of {id, text}."""
    out: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for anc in ancestors(dag, node_id):
        for inv in anc.get("invariants", []):
            inv_id = inv.get("id", "")
            if inv_id and inv_id not in seen_ids:
                seen_ids.add(inv_id)
                out.append({"id": inv_id, "text": inv.get("text", "")})
    return out


def collect_exports(dag: dict[str, Any], node_id: str) -> list[dict[str, str]]:
    """Walk ancestors of node_id and merge their public exports."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for anc in ancestors(dag, node_id):
        for exp in anc.get("exports", []):
            sym = exp.get("symbol", "")
            if sym and sym not in seen:
                seen.add(sym)
                out.append(exp)
    return out


def mark_dirty_cascade(dag: dict[str, Any], node_id: str, layer: str) -> list[str]:
    """Mark a node and all its descendants as dirty in the given layer.

    Returns list of dirty node IDs.
    """
    assert layer in ("spec", "code"), f"unknown layer {layer}"
    dirty: list[str] = []
    target = find_node(dag, node_id)
    if target:
        target.setdefault(layer, {})["dirty"] = True
        dirty.append(target["id"])
    for d in descendants(dag, node_id):
        d.setdefault(layer, {})["dirty"] = True
        dirty.append(d["id"])
    return dirty


def list_dirty(dag: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Return [(node_id, [dirty_layers]), ...] for nodes with any dirty layer."""
    out: list[tuple[str, list[str]]] = []
    for stage in dag.get("stages", []):
        layers: list[str] = []
        if stage.get("spec", {}).get("dirty"):
            layers.append("spec")
        if stage.get("code", {}).get("dirty"):
            layers.append("code")
        if layers:
            out.append((stage["id"], layers))
    return out
