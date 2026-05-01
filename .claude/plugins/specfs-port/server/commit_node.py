"""commit_node.py — generic DAG node commit driver for specfs-port.

Stand-in for the plugin's MCP tools `spec_gen_approve` / `code_gen_approve`
until the runtime registers the specfs MCP server. Idempotent: re-running
overwrites the named layer's `approved_at` to "now" and leaves untouched
fields intact.

Usage:
    # Approve the spec layer of a node
    uv run python commit_node.py spec <module> <node_id> <stage_name> <spec_path> \\
        [--depends-on=a,b,c] [--exports=sym:kind,...] [--invariant-prefix=PREFIX]

    # Approve the code layer of a node (spec layer must already exist)
    uv run python commit_node.py code <module> <node_id> <code_path>

    # Show the DAG
    uv run python commit_node.py show <module> [<node_id>]

Invariants are auto-extracted from the spec file by matching markdown lines
of the form:
    **Invariant** (id=<lower-hyphenated-id>):
        <single-line text — first non-blank line after the header>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import dag as dag_module
from state import repo_root


_INV_RE = re.compile(
    r"^\s*\*\*Invariant\*\*\s*\(id=([a-z0-9][a-z0-9\-]*)\)\s*:?\s*$",
    re.MULTILINE,
)


def parse_invariants(spec_path: Path) -> list[dict[str, str]]:
    """Extract invariants from a SYSSPEC .spec file.

    Looks for lines like `**Invariant** (id=foo-bar):` and grabs the
    immediately-following non-blank text as the invariant body (stops at the
    next blank line or the next **Invariant** / [SECTION] marker).
    """
    if not spec_path.is_file():
        return []
    text = spec_path.read_text(encoding="utf-8")
    out: list[dict[str, str]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = _INV_RE.match(lines[i])
        if not m:
            i += 1
            continue
        inv_id = m.group(1)
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            ln = lines[j]
            if not ln.strip():
                if body:
                    break
                j += 1
                continue
            if _INV_RE.match(ln):
                break
            if re.match(r"^\s*\[[A-Z]", ln) or re.match(r"^\s*##\s", ln):
                break
            body.append(ln.strip())
            j += 1
            if body and (j >= len(lines) or not lines[j].strip()):
                break
        if body:
            out.append({"id": inv_id, "text": " ".join(body).strip()})
        i = j
    return out


def parse_exports_arg(arg: str) -> list[dict[str, str]]:
    """Parse '--exports=g_xVops:var,VfsXMount:func,exfat_fsmap:fsmap'."""
    out: list[dict[str, str]] = []
    if not arg:
        return out
    for entry in arg.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            sym, kind = entry.split(":", 1)
        else:
            sym, kind = entry, "func"
        out.append({"symbol": sym.strip(), "kind": kind.strip()})
    return out


def cmd_spec(args: argparse.Namespace) -> int:
    module = args.module
    node_id = args.node_id
    spec_path = Path(args.spec_path)

    if not spec_path.is_absolute():
        spec_path = repo_root() / spec_path
    if not spec_path.is_file():
        print(f"ERROR: spec file not found: {spec_path}", file=sys.stderr)
        return 1
    rel_spec = spec_path.relative_to(repo_root())

    invariants = parse_invariants(spec_path)
    exports = parse_exports_arg(args.exports or "")
    depends_on = [s.strip() for s in (args.depends_on or "").split(",") if s.strip()]

    dag = dag_module.load(module)
    existing = dag_module.find_node(dag, node_id) or {}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    node = {
        "id": node_id,
        "stage_name": args.stage_name,
        "depends_on": depends_on or existing.get("depends_on", []),
        "spec": {
            "path": str(rel_spec),
            "approved_at": now,
            "dirty": False,
        },
        "code": existing.get("code", {}),
        "invariants": invariants or existing.get("invariants", []),
        "exports": exports or existing.get("exports", []),
    }
    dag_module.add_or_update_node(dag, node)
    dag_module.save(module, dag)

    print(f"✓ {module}::{node_id} spec layer approved ({rel_spec}) at {now}")
    print(f"  invariants: {len(invariants)}; exports: {len(exports)}; depends_on: {depends_on or '∅'}")
    return 0


def cmd_code(args: argparse.Namespace) -> int:
    module = args.module
    node_id = args.node_id
    code_path = Path(args.code_path)

    if not code_path.is_absolute():
        code_path = repo_root() / code_path
    rel_code = code_path.relative_to(repo_root())

    dag = dag_module.load(module)
    node = dag_module.find_node(dag, node_id)
    if node is None:
        print(f"ERROR: node {node_id} not in DAG (commit spec layer first)", file=sys.stderr)
        return 1
    if not node.get("spec", {}).get("approved_at"):
        print(f"ERROR: node {node_id} spec layer not approved yet", file=sys.stderr)
        return 1

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    node["code"] = {
        "path": str(rel_code),
        "approved_at": now,
        "dirty": False,
    }
    dag_module.save(module, dag)
    print(f"✓ {module}::{node_id} code layer approved ({rel_code}) at {now}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    module = args.module
    dag = dag_module.load(module)
    if args.node_id:
        node = dag_module.find_node(dag, args.node_id)
        if node is None:
            print(f"ERROR: node {args.node_id} not in DAG", file=sys.stderr)
            return 1
        print(json.dumps(node, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(dag, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="commit_node.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("spec", help="Approve spec layer of a DAG node")
    p.add_argument("module")
    p.add_argument("node_id", help="e.g., chksum-v1, mount-v1")
    p.add_argument("stage_name", help="e.g., chksum, mount")
    p.add_argument("spec_path", help="Path to .spec file (relative to repo root or absolute)")
    p.add_argument("--depends-on", default="", help="CSV of node_ids this node depends on")
    p.add_argument("--exports", default="", help="CSV of sym:kind pairs (kinds: func/var/fsmap)")
    p.set_defaults(func=cmd_spec)

    p = sub.add_parser("code", help="Approve code layer of a DAG node")
    p.add_argument("module")
    p.add_argument("node_id")
    p.add_argument("code_path")
    p.set_defaults(func=cmd_code)

    p = sub.add_parser("show", help="Print DAG state (full or single node)")
    p.add_argument("module")
    p.add_argument("node_id", nargs="?", default=None)
    p.set_defaults(func=cmd_show)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
