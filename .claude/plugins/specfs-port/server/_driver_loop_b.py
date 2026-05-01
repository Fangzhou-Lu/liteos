"""Loop-B driver: assemble codegen prompt for an approved spec.

Stand-in for plugin's MCP tool `code_gen_start` until runtime binds the
specfs MCP server. Loads the approved spec + frozen common.header +
inherited invariants from DAG, then calls prompts.assemble_codegen_prompt.

Usage:
    uv run python _driver_loop_b.py <module> <node_id>
    e.g.  uv run python _driver_loop_b.py exfat mount-v1
"""
from __future__ import annotations

import sys
from pathlib import Path

import dag as dag_module
import extract
import prompts


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    module, node_id = sys.argv[1:]

    repo_root = Path(__file__).resolve().parents[4]
    dag = dag_module.load(module)
    node = dag_module.find_node(dag, node_id)
    if node is None:
        print(f"ERROR: node {node_id} not in DAG"); return 1
    if not node.get("spec", {}).get("approved_at"):
        print(f"ERROR: spec layer of {node_id} not approved"); return 1

    spec_path = repo_root / node["spec"]["path"]
    spec_content = spec_path.read_text()

    common_header = (repo_root / "spec" / module / "common.header").read_text()

    # Walk ancestors for inherited invariants
    inherited = dag_module.collect_invariants(dag, node_id)

    # Extract prior code interface from fs/<module>/
    ifaces = extract.extract_module_interface(module, repo_root)
    prior_iface = extract.render_interface_summary(ifaces)

    prompt_text = prompts.assemble_codegen_prompt(
        module=module,
        spec_content=spec_content,
        common_header=common_header,
        inherited_invariants=inherited,
        prior_code_interface=prior_iface,
    )

    out = Path(__file__).parent / ".loop_b_prompt.txt"
    out.write_text(prompt_text)
    print(f"=== Loop-B prompt assembled ({len(prompt_text)} chars) ===")
    print(f"=== Saved to: {out} ===")
    print(f"=== inherited_invariants: {len(inherited)} ===")
    print(f"=== prior_code_interface: {len(prior_iface)} chars ===")
    print()
    print(prompt_text[:1200])
    print("...")
    print(prompt_text[-1500:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
