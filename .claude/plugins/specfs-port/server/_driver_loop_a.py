"""Inline Loop-A driver — exercises the plugin's prompt-assembly machinery
without the MCP transport layer. Used because the runtime didn't bind the
specfs MCP tools this session.

Usage:
    cd .claude/plugins/specfs-port/server
    uv run python _driver_loop_a.py <module> <target_stage> <linux_path>

Outputs the assembled Loop-A prompt to stdout (and also writes it to
.assembled_prompt.txt for inspection).
"""
from __future__ import annotations

import sys
from pathlib import Path

import dag as dag_module
import prompts


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    module, stage, linux_path = sys.argv[1:]

    repo_root = Path(__file__).resolve().parents[4]  # .claude/plugins/specfs-port/server -> repo
    common_header_path = repo_root / "spec" / module / "common.header"
    common_header = common_header_path.read_text() if common_header_path.is_file() else ""

    dag = dag_module.load(module)
    inherited_invariants: list[dict[str, str]] = []  # mount has no ancestors

    # Build prior-spec index (just lists existing approved specs)
    spec_root = repo_root / "spec" / module
    prior_lines: list[str] = []
    if spec_root.is_dir():
        for p in sorted(spec_root.rglob("*.spec")):
            prior_lines.append(f"- {p.relative_to(repo_root)}")
    prior_spec_index = "\n".join(prior_lines)

    # Sub_path inferred from stage
    if stage in ("mount", "umount", "statfs", "sync", "lookup", "open", "close",
                 "read", "write", "readdir"):
        sub_path = "interface"
    else:
        sub_path = stage  # fallback

    prompt_text = prompts.assemble_linux_to_spec_prompt(
        module=module,
        linux_path=linux_path,
        target_stage=stage,
        sub_path=sub_path,
        common_header=common_header,
        inherited_invariants=inherited_invariants,
        prior_spec_index=prior_spec_index,
    )

    out = Path(__file__).parent / ".assembled_prompt.txt"
    out.write_text(prompt_text)
    print(f"=== Assembled Loop-A prompt ({len(prompt_text)} chars) ===")
    print(f"=== Saved to: {out} ===\n")
    print(prompt_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
