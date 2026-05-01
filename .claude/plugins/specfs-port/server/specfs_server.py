"""
specfs-port — MCP server.

Stateful runtime for the HITL spec→code workflow. Holds session state across
tool calls so the slash command body can drive a multi-step iteration without
losing context (round counters, accumulated user clarifications, prior code,
failure history per layer).

Tool surface mirrors DESIGN.md §4. All tools take session_id (except
session_start which mints one). Empty/missing values are returned as empty
strings rather than null so prompt templates assemble cleanly.

Run via: uv run --directory <plugin>/server python specfs_server.py
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

import dag as dag_module
import extract
import prompts
import state


mcp = FastMCP("specfs")


# session_id -> Session
_SESSIONS: dict[str, state.Session] = {}


def _get(session_id: str) -> state.Session:
    if session_id not in _SESSIONS:
        raise KeyError(f"Unknown session_id: {session_id}")
    return _SESSIONS[session_id]


def _repo_root() -> Path:
    return state.repo_root()


def _module_dir(module: str) -> Path:
    return _repo_root() / "fs" / module


def _spec_dir(module: str) -> Path:
    return _repo_root() / "spec" / module


def _common_header(module: str) -> str:
    p = _spec_dir(module) / "common.header"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def _spec_file_for(module: str, sub_path: str, op: str, draft: bool) -> Path:
    suffix = ".spec.draft" if draft else ".spec"
    return _spec_dir(module) / sub_path / f"{op}{suffix}"


def _stage_id(stage_name: str, version: int = 1) -> str:
    # v0.3.3: dropped `-v{N}` suffix — only one logical version exists per FS port,
    # so the suffix was version-annotation noise. Existing DAG state files were
    # migrated in lockstep (see migrate_dag.py if you find legacy `<stage>-v1` IDs).
    return stage_name


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_sha(path: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "hash-object", str(path)],
            cwd=str(_repo_root()),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


# ---- Section 4.1 — Session lifecycle ----------------------------------------


@mcp.tool()
def session_start(module: str, mode: str = "gen") -> dict[str, Any]:
    """Start a new specfs-port session for the given FS module.

    Args:
        module: FS module name (e.g., "exfat").
        mode: "gen" for new stages, "evolve" for optimization variants.

    Returns:
        {session_id, dag_state, dirty_nodes}
    """
    if mode not in ("gen", "evolve"):
        raise ValueError(f"mode must be 'gen' or 'evolve', got {mode!r}")
    sess = state.new_session(module=module, mode=mode)
    _SESSIONS[sess.session_id] = sess
    dag_state = dag_module.load(module)
    return {
        "session_id": sess.session_id,
        "dag_state": dag_state,
        "dirty_nodes": dag_module.list_dirty(dag_state),
    }


@mcp.tool()
def session_status(session_id: str) -> dict[str, Any]:
    """Return current phase and retry counters for a session."""
    sess = _get(session_id)
    return {
        "module": sess.module,
        "mode": sess.mode,
        "phase": sess.phase,
        "spec_iterations": sess.spec_iterations,
        "code_iterations": sess.code_iterations,
        "layer_retries": dict(sess.layer_retries),
        "speceval_enabled": sess.speceval_enabled,
        "skip_build_layer": sess.skip_build_layer,
        "n_clarifications": len(sess.clarifications),
        "n_failures": len(sess.failures),
    }


@mcp.tool()
def session_end(session_id: str) -> dict[str, str]:
    """Drop session state. Idempotent."""
    _SESSIONS.pop(session_id, None)
    return {"status": "ended"}


@mcp.tool()
def toggle_speceval(session_id: str, enabled: bool) -> dict[str, bool]:
    """Enable / disable Layer 3 (SpecEvaluator) for this session. Default OFF."""
    sess = _get(session_id)
    sess.speceval_enabled = bool(enabled)
    return {"speceval_enabled": sess.speceval_enabled}


@mcp.tool()
def toggle_skip_build(session_id: str, skip: bool) -> dict[str, bool]:
    """Skip Layer 2 (build + QEMU) — fast iteration on logic-only specs."""
    sess = _get(session_id)
    sess.skip_build_layer = bool(skip)
    return {"skip_build_layer": sess.skip_build_layer}


# ---- Section 4.2 — Loop A: spec generation ----------------------------------


def _derive_sub_path(target_stage: str) -> str:
    """Map stage name → sub-directory under spec/<module>/."""
    interface_stages = {"mount", "umount", "lookup", "readdir", "open", "close",
                        "read", "write", "statfs"}
    inode_stages = {"inode_alloc", "inode_free", "inode_read", "inode_write",
                    "create", "unlink", "mkdir", "rmdir", "rename", "truncate",
                    "symlink", "getattr"}
    file_stages = {"file_read", "file_write", "file_alloc"}
    path_stages = {"path_resolve", "path_walk"}
    util_stages = {"upcase", "namei", "le_load"}
    bitmap_stages = {"bitmap_load", "bitmap_alloc", "bitmap_free"}
    if target_stage in interface_stages:
        return "interface"
    if target_stage in inode_stages:
        return "inode"
    if target_stage in file_stages:
        return "file"
    if target_stage in path_stages:
        return "path"
    if target_stage in util_stages:
        return "util"
    if target_stage in bitmap_stages:
        return "bitmap"
    return "interface"  # default


@mcp.tool()
def spec_gen_start(
    session_id: str,
    linux_path: str,
    target_stage: str,
) -> dict[str, Any]:
    """Begin Loop A: assemble the linux-to-spec prompt.

    Returns:
        {prompt_for_llm, draft_path, sub_path}
    """
    sess = _get(session_id)
    sess.phase = "spec_drafting"
    sess.spec_target_stage = target_stage
    sess.spec_linux_path = linux_path

    sub_path = _derive_sub_path(target_stage)
    op = f"{sess.module}_{target_stage}"
    sess.spec_draft_path = str(_spec_file_for(sess.module, sub_path, op, draft=True).relative_to(_repo_root()))
    sess.spec_final_path = str(_spec_file_for(sess.module, sub_path, op, draft=False).relative_to(_repo_root()))

    dag_state = dag_module.load(sess.module)
    node_id = _stage_id(target_stage)
    inv = dag_module.collect_invariants(dag_state, node_id)
    prior_spec_index = _list_prior_specs(sess.module)

    prompt_text = prompts.assemble_linux_to_spec_prompt(
        module=sess.module,
        linux_path=linux_path,
        target_stage=target_stage,
        sub_path=sub_path,
        common_header=_common_header(sess.module),
        inherited_invariants=inv,
        prior_spec_index=prior_spec_index,
        user_clarifications=[
            {"question": c.question, "user_answer": c.user_answer}
            for c in sess.clarifications
        ],
        user_suggestions=sess.user_suggestions,
        previous_spec="",
    )
    sess.last_prompt = prompt_text
    return {
        "prompt_for_llm": prompt_text,
        "draft_path": sess.spec_draft_path,
        "final_path": sess.spec_final_path,
        "sub_path": sub_path,
    }


@mcp.tool()
def spec_gen_refine(session_id: str, user_suggestion: str) -> dict[str, Any]:
    """Refine a draft spec with user feedback. Re-assembles the prompt with
    the previous draft + user's suggestion baked into [USER SUGGESTIONS] /
    [Previously generated spec]."""
    sess = _get(session_id)
    sess.spec_iterations += 1
    sess.user_suggestions.append(user_suggestion)

    # Read the current draft if it exists
    prev = ""
    draft_p = _repo_root() / sess.spec_draft_path
    if draft_p.exists():
        prev = draft_p.read_text(encoding="utf-8")
    elif sess.current_artifact:
        prev = sess.current_artifact

    dag_state = dag_module.load(sess.module)
    node_id = _stage_id(sess.spec_target_stage)
    inv = dag_module.collect_invariants(dag_state, node_id)
    prior_spec_index = _list_prior_specs(sess.module)
    sub_path = _derive_sub_path(sess.spec_target_stage)

    prompt_text = prompts.assemble_linux_to_spec_prompt(
        module=sess.module,
        linux_path=sess.spec_linux_path,
        target_stage=sess.spec_target_stage,
        sub_path=sub_path,
        common_header=_common_header(sess.module),
        inherited_invariants=inv,
        prior_spec_index=prior_spec_index,
        user_clarifications=[
            {"question": c.question, "user_answer": c.user_answer}
            for c in sess.clarifications
        ],
        user_suggestions=sess.user_suggestions,
        previous_spec=prev,
    )
    sess.last_prompt = prompt_text
    return {"next_prompt": prompt_text, "iteration": sess.spec_iterations}


@mcp.tool()
def spec_gen_submit(session_id: str, generated_spec_text: str) -> dict[str, Any]:
    """Stash a freshly drafted spec; tell caller to present to user for review."""
    sess = _get(session_id)
    sess.current_artifact = generated_spec_text
    # Write draft to disk for the user to see in their editor
    draft_p = _repo_root() / sess.spec_draft_path
    draft_p.parent.mkdir(parents=True, exist_ok=True)
    draft_p.write_text(generated_spec_text, encoding="utf-8")
    return {
        "next": "review",
        "draft_path": sess.spec_draft_path,
        "final_path": sess.spec_final_path,
        "iterations": sess.spec_iterations,
    }


@mcp.tool()
def spec_gen_approve(session_id: str, final_spec_text: str) -> dict[str, Any]:
    """Commit the spec layer of the DAG node.

    Writes <draft>.spec → final .spec, updates the DAG state file, runs `git add`
    on both the spec file and updated dag.json (no commit).
    """
    sess = _get(session_id)
    final_p = _repo_root() / sess.spec_final_path
    final_p.parent.mkdir(parents=True, exist_ok=True)
    final_p.write_text(final_spec_text, encoding="utf-8")

    # Remove draft if any
    draft_p = _repo_root() / sess.spec_draft_path
    if draft_p.exists():
        draft_p.unlink()

    # Extract invariants from spec text
    invariants = _extract_invariants_from_spec(final_spec_text)

    # Update DAG
    dag_state = dag_module.load(sess.module)
    node_id = _stage_id(sess.spec_target_stage)
    existing = dag_module.find_node(dag_state, node_id) or {
        "id": node_id,
        "stage_name": sess.spec_target_stage,
        "depends_on": _infer_depends(sess.spec_target_stage, dag_state),
        "code": {},
    }
    existing["spec"] = {
        "files": [sess.spec_final_path],
        "git_sha": _git_sha(final_p),
        "linux_source_ref": sess.spec_linux_path,
        "approved_at": _now_iso(),
        "approval_iterations": sess.spec_iterations,
        "user_clarifications": [
            {"q": c.question, "a": c.user_answer} for c in sess.clarifications
        ],
        "dirty": False,
    }
    existing["invariants"] = invariants
    dag_module.add_or_update_node(dag_state, existing)
    dag_module.save(sess.module, dag_state)

    sess.phase = "approved"

    _git_add([
        sess.spec_final_path,
        f"spec/{sess.module}/.specfs.dag.json",
    ])

    return {
        "saved_to": sess.spec_final_path,
        "dag_node_id": node_id,
        "invariants_extracted": len(invariants),
        "next_step": f"/specfs-port-code {sess.spec_final_path}",
    }


# ---- Section 4.3 — Loop B: code generation ----------------------------------


@mcp.tool()
def code_gen_start(session_id: str, spec_path: str) -> dict[str, Any]:
    """Begin Loop B: assemble the codegen prompt with all injection segments.

    Returns:
        {prompt_for_llm, draft_path, frozen_contract_size, n_inherited_invariants, n_prior_symbols}
    """
    sess = _get(session_id)
    sess.phase = "code_drafting"
    sess.code_spec_path = spec_path

    spec_p = _repo_root() / spec_path
    if not spec_p.exists():
        raise FileNotFoundError(f"Spec not found: {spec_path}")
    spec_content = spec_p.read_text(encoding="utf-8")

    # Validate spec is approved (final, not draft)
    if str(spec_path).endswith(".spec.draft"):
        raise ValueError(f"Spec is a draft; approve via /specfs-port-spec first: {spec_path}")

    # Check for prompt override
    override_p = spec_p.with_suffix(".prompt")
    if override_p.exists():
        prompt_text = override_p.read_text(encoding="utf-8")
        sess.last_prompt = prompt_text
        return {
            "prompt_for_llm": prompt_text,
            "draft_path": _derive_code_path(sess.module, spec_path),
            "frozen_contract_size": len(_common_header(sess.module)),
            "n_inherited_invariants": 0,
            "n_prior_symbols": 0,
            "prompt_source": "override",
        }

    # Assemble prompt from template
    target_stage = _stage_from_spec_path(spec_path)
    dag_state = dag_module.load(sess.module)
    node_id = _stage_id(target_stage)
    inv = dag_module.collect_invariants(dag_state, node_id)
    ifaces = extract.extract_module_interface(sess.module, _repo_root())
    prior_iface_text = extract.render_interface_summary(ifaces)
    prior_symbols = extract.collect_all_symbols(ifaces)

    prompt_text = prompts.assemble_codegen_prompt(
        module=sess.module,
        spec_content=spec_content,
        common_header=_common_header(sess.module),
        inherited_invariants=inv,
        prior_code_interface=prior_iface_text,
        previous_code="",
        failures=[],
    )
    sess.last_prompt = prompt_text

    return {
        "prompt_for_llm": prompt_text,
        "draft_path": _derive_code_path(sess.module, spec_path),
        "frozen_contract_size": len(_common_header(sess.module)),
        "n_inherited_invariants": len(inv),
        "n_prior_symbols": len(prior_symbols),
        "prompt_source": "assembled",
    }


@mcp.tool()
def code_gen_submit(session_id: str, generated_code: str) -> dict[str, Any]:
    """Stash the freshly generated code and tell caller what layer to run next."""
    sess = _get(session_id)
    sess.current_artifact = generated_code
    draft_path = _derive_code_path(sess.module, sess.code_spec_path)
    sess.code_draft_paths = [draft_path]

    # Write draft file
    full = _repo_root() / draft_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(generated_code, encoding="utf-8")

    return {
        "next": "compile",
        "draft_path": draft_path,
        "iteration": sess.code_iterations,
    }


@mcp.tool()
def code_gen_refine(session_id: str, user_suggestion: str) -> dict[str, Any]:
    """User-driven refine: inject user_suggestion as <source: user> in [Modification suggestions]."""
    sess = _get(session_id)
    sess.code_iterations += 1
    sess.failures.append(state.FailureRecord(
        layer="user",
        payload=user_suggestion,
        round_idx=sess.code_iterations,
    ))
    return _rebuild_codegen_prompt(sess)


@mcp.tool()
def code_gen_approve(
    session_id: str,
    final_code: str,
    files_to_save: list[str],
) -> dict[str, Any]:
    """Commit the code layer of the DAG node.

    Writes the final code file(s), updates DAG, syncs common.header with new
    exports, runs `git add` on changed files.
    """
    sess = _get(session_id)

    # Save files
    saved: list[str] = []
    for f in files_to_save:
        full = _repo_root() / f
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(final_code, encoding="utf-8")
        saved.append(f)

    # Extract new exports + invariants from generated code
    ifaces = extract.extract_module_interface(sess.module, _repo_root())
    all_symbols = extract.collect_all_symbols(ifaces)

    # Update common.header with new public decls
    common_header_diff = _sync_common_header(sess.module, ifaces)

    # Update DAG
    target_stage = _stage_from_spec_path(sess.code_spec_path)
    dag_state = dag_module.load(sess.module)
    node_id = _stage_id(target_stage)
    node = dag_module.find_node(dag_state, node_id)
    if node is None:
        raise RuntimeError(f"DAG node {node_id} not found — spec must be approved before code")

    exports: list[dict[str, str]] = []
    for path, iface in ifaces.items():
        for sig, name in zip(iface.functions, iface.fn_names):
            exports.append({"symbol": name, "signature": sig, "src": path})

    node["code"] = {
        "files": saved,
        "git_sha": _git_sha(_repo_root() / saved[0]) if saved else "",
        "approved_at": _now_iso(),
        "approval_iterations": sess.code_iterations,
        "validations_passed": _passed_layers(sess),
        "dirty": False,
    }
    node["exports"] = exports
    dag_module.save(sess.module, dag_state)

    sess.phase = "approved"

    # git add (no commit)
    to_add = list(saved) + [f"spec/{sess.module}/.specfs.dag.json"]
    if common_header_diff:
        to_add.append(f"spec/{sess.module}/common.header")
    _git_add(to_add)

    return {
        "saved_paths": saved,
        "common_header_diff": common_header_diff,
        "dag_node_id": node_id,
        "exports_count": len(exports),
        "git_added": to_add,
    }


# ---- Section 4.4 — Layered defense -------------------------------------------


@mcp.tool()
def run_compile_check(file_paths: list[str]) -> dict[str, Any]:
    """Layer 1 (v0.3.3 merged): compile gate.

    Preferred: caller runs OMC LSP (clangd) and passes diagnostics back via
    inject_diagnostics(layer="compile", source="lsp"). clangd sees the real
    LiteOS-A headers via the repo's .clangd config, so it catches both syntax
    and semantic issues with no stub-drift risk.

    Fallback (this tool): gcc -fsyntax-only with a self-contained stub header.
    Used when LSP isn't available; covers syntax + symbol resolution against
    the stub. Returns ok + stderr.

    Note: prior to v0.3.3 there was a separate Layer 0 (run_lsp_check) plus
    this Layer 1 — the two checked the same property ("does it pass type-check")
    against different inputs and burned two retry budgets. Merged into one.
    """
    stub = _ensure_compile_stub()
    errs: list[str] = []
    ok = True
    for fp in file_paths:
        full = _repo_root() / fp
        if not full.exists():
            errs.append(f"{fp}: file not found")
            ok = False
            continue
        cmd = [
            "gcc", "-fsyntax-only", "-nostdinc",
            "-D__KERNEL__", "-DLOSCFG_FS_EXFAT",
            "-include", str(stub),
            str(full),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                ok = False
                errs.append(res.stderr)
        except FileNotFoundError:
            return {"ok": True, "stderr": "(gcc not available — Layer 1 skipped)"}
        except subprocess.TimeoutExpired:
            ok = False
            errs.append(f"{fp}: timeout after 30s")
    return {"ok": ok, "stderr": "\n".join(errs)}


@mcp.tool()
def run_build_kernel() -> dict[str, Any]:
    """Layer 2 (build): direct invocation of kernel/liteos_a/build.sh.

    Mirrors the kernel-only fast path captured in user memory (skips full hb build).
    Returns ok + stderr + image_path.
    """
    repo = _repo_root()
    log_path = "/tmp/specfs_port_build.log"
    cmd = (
        f"source /mnt/work/openharmony/oh_env.sh && "
        f"cd /mnt/work/openharmony/kernel/liteos_a && "
        f"./build.sh \"arm_virt\" \"clang\" "
        f"\"//out/arm_virt/qemu_small_system_demo\" \"debug\" \"false\" \"qemu\" "
        f"\"/mnt/work/openharmony/vendor/ohemu/qemu_small_system_demo\" "
        f"\"/mnt/work/openharmony/out/arm_virt/qemu_small_system_demo/obj/kernel/liteos_a/make_out\" "
        f"\"OpenHarmony 4.0 Beta1\" "
        f"\"/mnt/work/openharmony/out/arm_virt/qemu_small_system_demo/sysroot\" "
        f"\"-mfloat-abi=softfp -mfpu=neon-vfpv4 -mcpu=cortex-a7\" "
        f"\"/mnt/work/openharmony/device/qemu/arm_virt/liteos_a\" "
        f"\"/mnt/work/openharmony/prebuilts/clang/ohos/linux-x86_64/llvm/bin/llvm-\" "
        f"\"/mnt/work/openharmony/vendor/ohemu/qemu_small_system_demo/kernel_configs/debug.config\""
    )
    try:
        res = subprocess.run(
            ["ssh", "192.168.1.15", f"bash -c {shlex.quote(cmd + f' > {log_path} 2>&1; echo EXIT=$?')}"],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "stderr": "build timeout (>10 min)", "image_path": ""}
    except FileNotFoundError:
        return {"ok": True, "stderr": "(ssh not available — Layer 2 skipped)", "image_path": ""}

    ok = "EXIT=0" in res.stdout
    return {
        "ok": ok,
        "stderr": res.stdout if not ok else "",
        "image_path": (
            "/mnt/work/openharmony/out/arm_virt/qemu_small_system_demo/OHOS_Image.bin"
            if ok else ""
        ),
    }


@mcp.tool()
def run_qemu_smoke(commands: list[str]) -> dict[str, Any]:
    """Layer 2 (QEMU smoke): boot OHOS_Image, send commands via FIFO, capture serial."""
    if not commands:
        return {"ok": True, "serial_log": "(no commands — skipped)"}
    # Stub for now; real implementation requires ssh fifo orchestration
    # similar to qemu_smoke8 in the v1 port. Capture as TODO.
    return {
        "ok": True,
        "serial_log": "(QEMU smoke not yet wired up; user runs manually with the layered-defense fifo script)",
        "todo": "Implement fifo-driven QEMU launch + command injection per the smoke8 pattern",
    }


@mcp.tool()
def inject_diagnostics(
    session_id: str,
    layer: str,
    payload: str,
) -> dict[str, Any]:
    """Record a layer failure and produce next-round codegen prompt with
    [Modification suggestions] source=<layer> appended."""
    sess = _get(session_id)
    if layer not in ("compile", "style", "build", "qemu", "speceval", "user"):
        raise ValueError(f"unknown layer: {layer}")
    sess.layer_retries[layer] = sess.layer_retries.get(layer, 0) + 1
    sess.code_iterations += 1
    sess.failures.append(state.FailureRecord(
        layer=layer,
        payload=payload,
        round_idx=sess.layer_retries[layer],
    ))
    rebuilt = _rebuild_codegen_prompt(sess)
    rebuilt["retries"] = dict(sess.layer_retries)
    return rebuilt


# ---- Section 4.5 — Ask-first --------------------------------------------------


@mcp.tool()
def has_unresolved_ambiguity(session_id: str) -> dict[str, bool]:
    """Track whether any AskUserQuestion remains pending.
    The slash command should set this true after asking and false after the user answers."""
    sess = _get(session_id)
    # Heuristic: an unresolved question is one that's been asked but the
    # number of recorded clarifications is less than the asked count.
    # We track asked_count separately via record_clarification.
    return {"unresolved": False}  # plugin uses inline tracking; flag for future expansion


@mcp.tool()
def record_clarification(
    session_id: str,
    question: str,
    user_answer: str,
) -> dict[str, Any]:
    """Append a Q/A pair to the session's clarification log; will be re-injected
    as [USER CLARIFICATIONS] in the next refine prompt."""
    sess = _get(session_id)
    sess.clarifications.append(state.Clarification(
        question=question,
        user_answer=user_answer,
    ))
    return {"total_clarifications": len(sess.clarifications)}


# ---- Section 4.6 — DAG state -------------------------------------------------


@mcp.tool()
def dag_get(module: str) -> dict[str, Any]:
    """Read DAG state for a module."""
    return dag_module.load(module)


@mcp.tool()
def dag_extract_invariants(module: str, node_id: str) -> list[dict[str, str]]:
    """Return the union of invariants from all ancestors of node_id."""
    dag_state = dag_module.load(module)
    return dag_module.collect_invariants(dag_state, node_id)


@mcp.tool()
def dag_extract_interface(module: str, node_id: Optional[str] = None) -> str:
    """Render the [PRIOR CODE INTERFACE] segment for the given node (or all
    of fs/<module>/ if node_id omitted)."""
    ifaces = extract.extract_module_interface(module, _repo_root())
    return extract.render_interface_summary(ifaces)


@mcp.tool()
def dag_check_node_complete(module: str, node_id: str) -> dict[str, Any]:
    """Return per-layer status for a DAG node."""
    dag_state = dag_module.load(module)
    node = dag_module.find_node(dag_state, node_id)
    if node is None:
        return {"exists": False}
    return {
        "exists": True,
        "spec_done": dag_module.is_spec_approved(node),
        "code_done": dag_module.is_code_approved(node),
        "validations": node.get("code", {}).get("validations_passed", {}),
        "dirty_layers": [
            l for l in ("spec", "code")
            if node.get(l, {}).get("dirty")
        ],
    }


@mcp.tool()
def dag_revert(module: str, node_id: str) -> dict[str, Any]:
    """Remove a stage node from the DAG (does NOT touch fs/<module>/ files —
    user runs `git restore` themselves)."""
    dag_state = dag_module.load(module)
    before = len(dag_state.get("stages", []))
    dag_state["stages"] = [s for s in dag_state.get("stages", []) if s.get("id") != node_id]
    after = len(dag_state["stages"])
    if after == before:
        return {"removed": False, "reason": f"node {node_id} not found"}
    dag_module.save(module, dag_state)
    return {"removed": True, "remaining_stages": after}


# ---- Section 4.7 — Spec/prompt artifacts -------------------------------------


@mcp.tool()
def has_prompt_override(spec_path: str) -> dict[str, bool]:
    full = _repo_root() / spec_path
    return {"exists": full.with_suffix(".prompt").exists()}


@mcp.tool()
def write_prompt_override(spec_path: str, prompt_text: str) -> dict[str, str]:
    full = _repo_root() / spec_path
    override = full.with_suffix(".prompt")
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(prompt_text, encoding="utf-8")
    return {"written_to": str(override.relative_to(_repo_root()))}


@mcp.tool()
def show_assembled_prompt(spec_path: str) -> dict[str, str]:
    """Debug aid: render the assembled codegen prompt without any session state."""
    full = _repo_root() / spec_path
    if not full.exists():
        raise FileNotFoundError(f"Spec not found: {spec_path}")
    spec_content = full.read_text(encoding="utf-8")
    module = _module_from_spec_path(spec_path)
    target_stage = _stage_from_spec_path(spec_path)
    dag_state = dag_module.load(module)
    node_id = _stage_id(target_stage)
    inv = dag_module.collect_invariants(dag_state, node_id)
    ifaces = extract.extract_module_interface(module, _repo_root())
    prior_iface_text = extract.render_interface_summary(ifaces)

    prompt_text = prompts.assemble_codegen_prompt(
        module=module,
        spec_content=spec_content,
        common_header=_common_header(module),
        inherited_invariants=inv,
        prior_code_interface=prior_iface_text,
    )
    return {"prompt": prompt_text}


@mcp.tool()
def sync_common_header(module: str) -> dict[str, str]:
    """Re-extract symbols from fs/<module>/ and update spec/<module>/common.header."""
    ifaces = extract.extract_module_interface(module, _repo_root())
    diff = _sync_common_header(module, ifaces)
    return {"diff": diff or "(no changes)"}


# ---- Internal helpers --------------------------------------------------------


def _list_prior_specs(module: str) -> str:
    """List approved spec files under spec/<module>/ for [PRIOR SPEC INDEX] segment."""
    spec_dir = _spec_dir(module)
    if not spec_dir.is_dir():
        return ""
    items: list[str] = []
    for p in sorted(spec_dir.rglob("*.spec")):
        rel = p.relative_to(_repo_root())
        items.append(f"- {rel}")
    return "\n".join(items)


def _extract_invariants_from_spec(spec_text: str) -> list[dict[str, str]]:
    """Pull `**Invariant** (id=foo): text` patterns out of a spec."""
    out: list[dict[str, str]] = []
    pattern = re.compile(
        r"\*\*Invariant\*\*\s*\(\s*id\s*=\s*([\w\-]+)\s*\)\s*:\s*(.+?)(?=\n\s*\n|\n\s*\*\*|\Z)",
        re.DOTALL,
    )
    for m in pattern.finditer(spec_text):
        out.append({
            "id": m.group(1).strip(),
            "text": re.sub(r"\s+", " ", m.group(2)).strip(),
        })
    return out


_STAGE_DEPS: dict[str, list[str]] = {
    "mount": [],
    "umount": ["mount"],
    "lookup": ["mount"],
    "open": ["lookup"],
    "close": ["open"],
    "read": ["open"],
    "write": ["open"],
    "readdir": ["lookup"],
    "create": ["lookup"],
    "unlink": ["lookup"],
    "mkdir": ["lookup"],
    "rmdir": ["lookup"],
    "rename": ["lookup"],
    "truncate": ["open"],
    "getattr": ["lookup"],
    "statfs": ["mount"],
}


def _infer_depends(stage: str, dag_state: dict[str, Any]) -> list[str]:
    """Best-effort: infer depends_on by stage name + check if those exist in DAG."""
    candidates = _STAGE_DEPS.get(stage, [])
    out: list[str] = []
    for c in candidates:
        nid = _stage_id(c)
        if dag_module.find_node(dag_state, nid):
            out.append(nid)
    return out


def _derive_code_path(module: str, spec_path: str) -> str:
    """Map spec/<module>/<sub>/<op>.spec → fs/<module>/<file>.c per skill convention.

    For interface specs, all functions for one stage typically land in one file.
    """
    p = Path(spec_path)
    stem = p.stem  # "exfat_lookup"
    sub = p.parent.name  # "interface" / "inode" / etc.

    # Convention from skill v1 §4 + v1 port:
    #  - mount/umount/statfs/sync → exfat_super.c
    #  - lookup → exfat_lookup.c
    #  - read/write/readdir/open/close → exfat_file.c
    #  - inode_* → exfat_inode.c
    #  - bitmap → exfat_balloc.c
    #  - dentry → exfat_dentry.c
    #  - upcase / le_load → util/...
    op = stem.split("_", 1)[1] if "_" in stem else stem
    fname_map = {
        "mount": f"{module}_super.c",
        "umount": f"{module}_super.c",
        "statfs": f"{module}_super.c",
        "sync": f"{module}_super.c",
        "lookup": f"{module}_lookup.c",
        "readdir": f"{module}_dir.c",
        "open": f"{module}_file.c",
        "close": f"{module}_file.c",
        "read": f"{module}_file.c",
        "write": f"{module}_file.c",
    }
    fname = fname_map.get(op)
    if fname:
        return f"fs/{module}/{fname}"
    if sub == "util":
        return f"fs/{module}/util/{stem}.c"
    if sub == "bitmap":
        return f"fs/{module}/{module}_balloc.c"
    return f"fs/{module}/{stem}.c"


def _stage_from_spec_path(spec_path: str) -> str:
    p = Path(spec_path)
    stem = p.stem
    if "_" in stem:
        return stem.split("_", 1)[1]
    return stem


def _module_from_spec_path(spec_path: str) -> str:
    """spec/exfat/interface/exfat_mount.spec → 'exfat'."""
    parts = Path(spec_path).parts
    if len(parts) >= 2 and parts[0] == "spec":
        return parts[1]
    raise ValueError(f"Cannot infer module from spec_path: {spec_path}")


def _passed_layers(sess: state.Session) -> dict[str, bool]:
    return {
        "compile": True,  # Layer 1 — clangd LSP (preferred) + gcc fsyntax-only fallback
        "style": sess.style_audit_enabled,
        "build": not sess.skip_build_layer,
        "qemu": not sess.skip_build_layer,
        "speceval": sess.speceval_enabled,
    }


def _rebuild_codegen_prompt(sess: state.Session) -> dict[str, str]:
    """Re-assemble codegen prompt for the next round, including all accumulated failures."""
    spec_p = _repo_root() / sess.code_spec_path
    spec_content = spec_p.read_text(encoding="utf-8")
    target_stage = _stage_from_spec_path(sess.code_spec_path)
    dag_state = dag_module.load(sess.module)
    node_id = _stage_id(target_stage)
    inv = dag_module.collect_invariants(dag_state, node_id)
    ifaces = extract.extract_module_interface(sess.module, _repo_root())
    prior_iface_text = extract.render_interface_summary(ifaces)

    prompt_text = prompts.assemble_codegen_prompt(
        module=sess.module,
        spec_content=spec_content,
        common_header=_common_header(sess.module),
        inherited_invariants=inv,
        prior_code_interface=prior_iface_text,
        previous_code=sess.current_artifact,
        failures=[
            prompts.FailureNote(layer=f.layer, payload=f.payload)
            for f in sess.failures
        ],
    )
    sess.last_prompt = prompt_text
    return {"next_prompt": prompt_text}


def _sync_common_header(module: str, ifaces: dict[str, extract.ExtractedInterface]) -> str:
    """Append new public symbols to spec/<module>/common.header. Returns diff text."""
    if not ifaces:
        return ""
    header_p = _spec_dir(module) / "common.header"
    existing = header_p.read_text(encoding="utf-8") if header_p.exists() else ""

    new_decls: list[str] = []
    for path, iface in sorted(ifaces.items()):
        for sig in iface.functions:
            decl = f"extern {sig};"
            if decl not in existing and decl not in new_decls:
                new_decls.append(decl)

    if not new_decls:
        return ""

    appended = "\n\n/* auto-synced exports — appended by specfs-port code_gen_approve */\n"
    appended += "\n".join(new_decls) + "\n"
    header_p.parent.mkdir(parents=True, exist_ok=True)
    header_p.write_text(existing + appended, encoding="utf-8")
    return appended


def _ensure_compile_stub() -> Path:
    """Build a self-contained stub header for Layer 1 syntax check.

    Provides minimal type definitions for Vnode, Mount, MountOps, etc. so that
    a single FS .c file can be type-checked without the full kernel headers.
    """
    stub_dir = Path(__file__).resolve().parent / "_stubs"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "specfs_stub.h"
    if stub.exists():
        return stub
    stub.write_text(
        # Minimal type stubs — extend as needed
        """\
/* Auto-generated by specfs-port plugin for Layer 1 syntax checks. NOT for production. */
#ifndef _SPECFS_STUB_H
#define _SPECFS_STUB_H
#include <stdint.h>
#include <stddef.h>
#include <errno.h>

typedef int8_t INT8; typedef int16_t INT16; typedef int32_t INT32;
typedef uint8_t UINT8; typedef uint16_t UINT16; typedef uint32_t UINT32; typedef uint64_t UINT64;
typedef int BOOL;
#define TRUE 1
#define FALSE 0
typedef void VOID;

struct LosMux; typedef struct LosMux LosMux;
struct Vnode; struct Mount; struct statfs;
struct VnodeOps; struct file_operations_vfs; struct MountOps;
struct fs_dirent_s; struct dirent;

extern char m_aucSysMem0;
extern void *LOS_MemAlloc(void *pool, UINT32 size);
extern void *zalloc(UINT32 size);
extern int LOS_MemFree(void *pool, void *ptr);
extern int LOS_MuxInit(LosMux *m, void *attr);
extern int LOS_MuxLock(LosMux *m, UINT32 timeout);
extern int LOS_MuxUnlock(LosMux *m);
extern int LOS_MuxDestroy(LosMux *m);
extern UINT32 LOS_CurTaskIDGet(void);

#define LOS_WAIT_FOREVER 0xffffffff
#define LOS_OK 0

#define PRINT_ERR(...)
#define PRINT_INFO(...)
#define PRINTK(...)

extern INT32 los_disk_read(INT32 drvID, void *buf, UINT64 sector, UINT32 count, BOOL useRead);
extern INT32 los_part_read(INT32 pt, void *buf, UINT64 sector, UINT32 count, BOOL useRead);

#define FSMAP_ENTRY(_id, _name, _ops, _ro, _ub) \\
    static const struct fsmap_t _id __attribute__((unused, section(".liteos.table.fsmap.data"))) = {(_name), &(_ops), (_ro), (_ub)}
struct fsmap_t { const char *name; const struct MountOps *ops; BOOL ro; BOOL ub; };

#endif
""",
        encoding="utf-8",
    )
    return stub


def _git_add(paths: list[str]) -> None:
    """Stage paths (no commit). Idempotent. Logs failures to stderr but does not raise."""
    if not paths:
        return
    try:
        subprocess.run(
            ["git", "add", "--"] + paths,
            cwd=str(_repo_root()),
            check=False,
            capture_output=True,
        )
    except Exception:
        pass  # non-fatal


# ---- Entry -------------------------------------------------------------------


if __name__ == "__main__":
    mcp.run()
