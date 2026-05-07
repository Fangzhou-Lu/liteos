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
from _timeout import install_default_timeout
from _metrics import install_metrics, read_log, aggregate as metrics_aggregate


mcp = FastMCP("specfs")
# v0.5.2 (2026-05-07): wrap every @mcp.tool() with a 30 s default timeout
# (configurable via SPECFS_DEFAULT_TIMEOUT_S env var). On overrun the tool
# returns a structured `{_specfs_error: "timeout", ...}` dict instead of
# blocking the LLM caller indefinitely. Per-tool override: pass
# `@mcp.tool(timeout=N)` — used below for run_build_kernel (620 s) and
# validator_run_holistic (920 s) to accommodate their subprocess budgets.
install_default_timeout(mcp)

# v0.5.3 (2026-05-08): emit per-tool telemetry to JSONL log. Captures
# duration / input+output token estimates / LLM-round flags / errors per
# tool call. Default log path: <repo>/.specfs-metrics.jsonl (gitignored);
# override via SPECFS_METRICS_LOG env var. Disable entirely with
# SPECFS_METRICS_OFF=1. Decorator wraps OUTSIDE the timeout wrapper so
# metrics see the full wall-clock including timeout returns.
install_metrics(mcp)


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
    """Get the git SHA of a file. Returns "" on any failure (lock contention,
    non-git repo, timeout, etc.) so callers can no-op instead of crashing.

    v0.5.2 (2026-05-07): added timeout=10 s to defend against `.git/index.lock`
    left by a crashed prior process — without it git could block forever.
    """
    try:
        out = subprocess.check_output(
            ["git", "hash-object", str(path)],
            cwd=str(_repo_root()),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
        return out
    except (subprocess.SubprocessError, OSError):
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


@mcp.tool()
def toggle_test_gen(session_id: str, enabled: bool) -> dict[str, bool]:
    """Enable / disable Layer T (v0.3.4 cmocka test gen) for this session.

    Default ON. Disable with --test-off when iterating on a stage whose
    behavior is fully covered by Wave B (QEMU LTP) and a host-side cmocka
    test would only duplicate coverage.
    """
    sess = _get(session_id)
    sess.test_gen_enabled = bool(enabled)
    return {"test_gen_enabled": sess.test_gen_enabled}


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

    # v0.3.4: capture for Layer T re-use without re-reading from disk
    sess.code_final_text = final_code
    sess.code_final_paths = saved

    # v0.3.4: when Layer T is enabled, code approval transitions into
    # test_drafting (Layer T) instead of terminal "approved". The user
    # reviews code+test together in one HITL pass via Step 9.
    next_phase: str
    if sess.test_gen_enabled:
        sess.phase = "test_drafting"
        next_phase = "test_gen"
    else:
        sess.phase = "approved"
        next_phase = "done"

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
        "next": next_phase,
    }


# ---- Section 4.3.5 — Loop C: Layer T cmocka test gen (v0.3.4) ---------------
#
# Sits between Layer 3 (SpecEval) and Layer 4 (user review). Was advertised in
# the v0.3.2 CHANGELOG ("Spec-derived cmocka test generation, default ON") but
# never wired in the server until v0.3.4 — Wave A 9 stages accumulated test
# debt under the v0.3.2 era (see commit 149487a9 for the catch-up batch).
#
# Pipeline: code_gen_approve → test_gen_start → test_gen_submit → user review
# of code+test together → test_gen_approve (writes test_<stage>.c + applies
# Makefile/main.c deltas).


def _harness_dir(module: str) -> Path:
    """testsuites/unittest/<module>/ — the cmocka host harness root."""
    return _repo_root() / "testsuites" / "unittest" / module


def _test_paths_for(module: str, stage: str) -> tuple[str, str]:
    """Return (draft_relpath, final_relpath) under the harness dir."""
    base = f"testsuites/unittest/{module}/test_{stage}.c"
    return base + ".draft", base


def _harness_layout(module: str) -> str:
    """ls-style snapshot for the unittest_gen prompt's [Existing harness layout]
    segment. Empty string if the harness dir does not exist (first-time port)."""
    d = _harness_dir(module)
    if not d.is_dir():
        return ""
    items: list[str] = []
    for p in sorted(d.iterdir()):
        if p.name.startswith("."):
            continue
        items.append(p.name)
    return "\n".join(items)


@mcp.tool()
def test_gen_start(session_id: str) -> dict[str, Any]:
    """Begin Layer T: assemble the unittest_gen prompt from the just-approved
    code + spec + harness layout snapshot.

    Pre-condition: code_gen_approve must have completed AND test_gen_enabled
    is True; otherwise raises RuntimeError so the slash command can short-circuit.

    Returns:
        {prompt_for_llm, draft_path, final_path, harness_dir_exists}
    """
    sess = _get(session_id)
    if not sess.test_gen_enabled:
        raise RuntimeError(
            "test_gen disabled for this session — enable via toggle_test_gen "
            "or remove --test-off from the slash-command args"
        )
    if not sess.code_final_text:
        raise RuntimeError(
            "Layer T requires an approved code artifact. Run code_gen_approve first."
        )

    sess.phase = "test_drafting"
    stage = _stage_from_spec_path(sess.code_spec_path)
    sess.test_stage = stage
    draft_rel, final_rel = _test_paths_for(sess.module, stage)
    sess.test_draft_path = draft_rel
    sess.test_final_path = final_rel

    spec_p = _repo_root() / sess.code_spec_path
    spec_content = spec_p.read_text(encoding="utf-8") if spec_p.exists() else ""

    prompt_text = prompts.assemble_unittest_gen_prompt(
        generated_code=sess.code_final_text,
        original_spec=spec_content,
        harness_layout=_harness_layout(sess.module),
    )
    sess.last_prompt = prompt_text

    return {
        "prompt_for_llm": prompt_text,
        "draft_path": draft_rel,
        "final_path": final_rel,
        "harness_dir_exists": _harness_dir(sess.module).is_dir(),
    }


@mcp.tool()
def test_gen_submit(session_id: str, generated_test_text: str) -> dict[str, Any]:
    """Stash the freshly generated test draft. Writes to <draft_path>.

    Returns next='review' so the slash command knows to surface code+test
    together in the Layer 4 user-review pass.
    """
    sess = _get(session_id)
    if not sess.test_draft_path:
        raise RuntimeError("test_gen_start must be called before test_gen_submit")

    full = _repo_root() / sess.test_draft_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(generated_test_text, encoding="utf-8")
    sess.current_artifact = generated_test_text

    return {
        "next": "review",
        "draft_path": sess.test_draft_path,
        "iteration": sess.test_iterations,
    }


@mcp.tool()
def test_gen_refine(session_id: str, user_suggestion: str) -> dict[str, Any]:
    """User-driven test refine: re-assemble unittest_gen prompt with the prior
    draft baked in as context + user_suggestion appended.

    The unittest_gen.md template doesn't have a {PREVIOUS_TEST} slot today, so
    we append the prior draft + suggestion as a [Modification suggestions] tail
    segment that the LLM is expected to honor on round 2+.
    """
    sess = _get(session_id)
    sess.test_iterations += 1
    sess.layer_retries["test_gen"] = sess.layer_retries.get("test_gen", 0) + 1

    spec_p = _repo_root() / sess.code_spec_path
    spec_content = spec_p.read_text(encoding="utf-8") if spec_p.exists() else ""

    prior = ""
    draft_p = _repo_root() / sess.test_draft_path
    if draft_p.exists():
        prior = draft_p.read_text(encoding="utf-8")

    base_prompt = prompts.assemble_unittest_gen_prompt(
        generated_code=sess.code_final_text,
        original_spec=spec_content,
        harness_layout=_harness_layout(sess.module),
    )
    refine_tail = (
        "\n\n[Previously generated test]\n```c\n" + prior.strip() + "\n```\n"
        "\n[Modification suggestions]\n<source: user>\n"
        + user_suggestion.strip() + "\n</source>\n"
    )
    prompt_text = base_prompt + refine_tail
    sess.last_prompt = prompt_text

    return {
        "next_prompt": prompt_text,
        "iteration": sess.test_iterations,
        "retries": dict(sess.layer_retries),
    }


# Compiled once: matches `static const struct CMUnitTest test_<stage>_tests[]`
_TEST_ARRAY_RE = re.compile(
    r"const\s+struct\s+CMUnitTest\s+(test_\w+_tests)\s*\[",
)


def _derive_test_array_name(test_text: str, stage: str) -> str:
    """Extract `test_<stage>_tests` array symbol from the generated file. Falls
    back to the stage-derived default if regex misses (LLM used non-standard naming).
    """
    m = _TEST_ARRAY_RE.search(test_text)
    return m.group(1) if m else f"test_{stage}_tests"


def _apply_makefile_delta(module: str, stage: str) -> str:
    """Best-effort: append `test_<stage>.c` to HARNESS_SRCS in the harness Makefile.

    Idempotent — silently skips if the entry already exists. Returns the diff
    string for surfacing back to the user (empty if no-op).
    """
    mk = _harness_dir(module) / "Makefile"
    if not mk.exists():
        return ""
    text = mk.read_text(encoding="utf-8")
    test_entry = f"    test_{stage}.c"
    if test_entry in text or f"test_{stage}.c " in text:
        return ""
    # Find HARNESS_SRCS := ... \\ block; append before the closing line.
    # Continuation lines are detected by leading [ \t] (NOT \s, which matches
    # newlines and lets the regex slurp blank lines plus subsequent assignments
    # like OBJS := ...). v0.3.4.2 fix: bare `\s+` was eating the blank line
    # plus the OBJS line, causing test_<stage>.c to be appended after OBJS.
    m = re.search(r"(HARNESS_SRCS\s*:=[^\n]*(?:\n[ \t]+[^\n]+)*)", text)
    if not m:
        return ""
    block = m.group(1)
    # Append "    test_<stage>.c" continuation. Last line of block ends with
    # either a continuation `\\` or a real terminator; we insert a new line
    # before the terminator if any.
    lines = block.split("\n")
    # Last real entry — find where to insert
    new_lines = list(lines)
    # If last line ends with backslash, we extend the chain
    if new_lines and new_lines[-1].rstrip().endswith("\\"):
        new_lines.append(test_entry)
    else:
        # Last line is bare entry — turn it into continuation
        if new_lines:
            new_lines[-1] = new_lines[-1].rstrip() + "          \\"
        new_lines.append(test_entry)
    new_block = "\n".join(new_lines)
    new_text = text[:m.start(1)] + new_block + text[m.end(1):]
    mk.write_text(new_text, encoding="utf-8")
    return f"+ HARNESS_SRCS += test_{stage}.c (in {mk.relative_to(_repo_root())})"


def _apply_mainc_delta(module: str, stage: str, array_name: str) -> str:
    """Best-effort: append extern decl + run_suite() call to harness main.c.

    Idempotent — skips if the suite is already wired. Returns diff string."""
    main_c = _harness_dir(module) / "main.c"
    if not main_c.exists():
        return ""
    text = main_c.read_text(encoding="utf-8")
    extern_decl = f"extern const struct CMUnitTest {array_name}[];"
    suite_call = f'run_suite("{stage}",'
    if extern_decl in text and suite_call in text:
        return ""
    # Append extern decl after the last existing extern declaration
    extern_pat = re.compile(
        r"(extern\s+const\s+struct\s+CMUnitTest\s+test_\w+_tests\[\];\s*"
        r"extern\s+const\s+size_t\s+test_\w+_tests_count;\s*\n)",
    )
    matches = list(extern_pat.finditer(text))
    if matches and extern_decl not in text:
        last = matches[-1]
        new_decl = (
            f"extern const struct CMUnitTest {array_name}[];        "
            f"extern const size_t {array_name}_count;\n"
        )
        text = text[:last.end()] + new_decl + text[last.end():]
    # Append run_suite call inside main()
    if suite_call not in text:
        # Find last existing run_suite call inside main
        last_run = None
        for m in re.finditer(r"^\s*total\s*\+=\s*run_suite\([^;]+;\s*\n", text, re.MULTILINE):
            last_run = m
        if last_run is not None:
            new_call = (
                f'    total += run_suite("{stage}",'
                f' {array_name}, {array_name}_count);\n'
            )
            text = text[:last_run.end()] + new_call + text[last_run.end():]
    main_c.write_text(text, encoding="utf-8")
    return f"+ run_suite(\"{stage}\", {array_name}, ...) (in {main_c.relative_to(_repo_root())})"


@mcp.tool()
def test_gen_approve(session_id: str, final_test_text: str) -> dict[str, Any]:
    """Commit the test layer of the DAG node.

    1. Renames <draft_path>.c.draft → <final_path>.c with the user-approved text.
    2. Best-effort applies Makefile (HARNESS_SRCS) and main.c (extern + run_suite)
       deltas. If parsing fails, returns the diffs as empty strings so the user
       knows to wire manually — does NOT abort.
    3. Updates the DAG node's `tests` block (additive — no schema bump).
    4. git-adds the test file + Makefile + main.c + dag.json.
    5. Sets phase = "approved" (terminal).
    """
    sess = _get(session_id)
    if not sess.test_final_path:
        raise RuntimeError("test_gen_start must be called before test_gen_approve")

    final_p = _repo_root() / sess.test_final_path
    final_p.parent.mkdir(parents=True, exist_ok=True)
    final_p.write_text(final_test_text, encoding="utf-8")

    # Drop draft if present
    draft_p = _repo_root() / sess.test_draft_path
    if draft_p.exists():
        draft_p.unlink()

    # Best-effort Makefile/main.c rewiring
    array_name = _derive_test_array_name(final_test_text, sess.test_stage)
    mk_diff = _apply_makefile_delta(sess.module, sess.test_stage)
    mc_diff = _apply_mainc_delta(sess.module, sess.test_stage, array_name)

    # Count testpoints — match `cmocka_unit_test_setup_teardown(...)` /
    # `cmocka_unit_test(...)` entries in the array
    tp_count = len(re.findall(
        r"cmocka_unit_test(?:_setup_teardown)?\s*\(", final_test_text,
    ))

    # DAG update — additive `tests` block on the existing node
    target_stage = _stage_from_spec_path(sess.code_spec_path)
    dag_state = dag_module.load(sess.module)
    node_id = _stage_id(target_stage)
    node = dag_module.find_node(dag_state, node_id)
    if node is None:
        raise RuntimeError(
            f"DAG node {node_id} not found — code_gen_approve must run before test_gen_approve"
        )
    node["tests"] = {
        "files": [sess.test_final_path],
        "git_sha": _git_sha(final_p),
        "approved_at": _now_iso(),
        "approval_iterations": sess.test_iterations,
        "testpoints": tp_count,
        "test_array_name": array_name,
        "dirty": False,
    }
    dag_module.save(sess.module, dag_state)

    sess.phase = "approved"

    to_add: list[str] = [sess.test_final_path, f"spec/{sess.module}/.specfs.dag.json"]
    if mk_diff:
        to_add.append(f"testsuites/unittest/{sess.module}/Makefile")
    if mc_diff:
        to_add.append(f"testsuites/unittest/{sess.module}/main.c")
    _git_add(to_add)

    return {
        "saved_to": sess.test_final_path,
        "dag_node_id": node_id,
        "testpoints": tp_count,
        "test_array_name": array_name,
        "makefile_diff": mk_diff or "(no Makefile change — verify manually)",
        "mainc_diff": mc_diff or "(no main.c change — verify manually)",
        "git_added": to_add,
    }


# ---- Section 4.4 — Layered defense -------------------------------------------


# run_compile_check + _ensure_compile_stub removed in P1.3 (2026-05-07).
# Layer 1a is now LSP-only: callers run OMC clangd via the standard MCP LSP
# tools (mcp__cclsp__get_diagnostics or mcp__plugin_oh-my-claudecode_t__lsp_diagnostics)
# against the freshly written file, and feed any errors back via
# `inject_diagnostics(layer="compile", source="lsp", payload=<diagnostics>)`.
# Rationale: gcc -fsyntax-only with a hand-rolled stub header has chronic
# stub-drift (every new LiteOS-A type that touches an FS file forces a stub
# update); the clangd path uses the repo's real .clangd config and never
# drifts. The fallback existed for environments without LSP, but P1.3 makes
# LSP a hard prerequisite — any modern dev setup or CI image already has
# OMC LSP installed.


# v0.5.2: override default 30 s timeout — build can legitimately run up
# to its internal subprocess.run(timeout=600), so the MCP wrapper must
# allow ~620 s before declaring the tool itself stuck.
@mcp.tool(timeout=620)
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


_FRAGMENT_REGISTRY = {
    "style_rules": "Full LiteOS-A style rules: naming, layout, license, error path, file generation order. Fetch when picking a function-name convention or inventing a helper file.",
    "linux_to_liteos_table": "Full Linux→LiteOS-A primitive map: types, memory, locking, disk IO, strings, errno, VFS callbacks, linker tables. Fetch when the spec mentions a Linux primitive the digest does not cover.",
    "format_traps": "Format-compatibility traps: CRC variants, byte order, charsets, packed-struct alignment. Fetch when the spec touches on-disk data with checksums / multi-byte fields / non-UTF-8 charsets.",
    "ask_first_rules": "Ask-first disambiguation framework. Fetch only if mid-codegen you encounter spec ambiguity that the spec author did not resolve.",
}


@mcp.tool()
def fetch_prompt_fragment(name: str) -> dict[str, str]:
    """LLM-driven on-demand expansion of a code-gen reference fragment.

    The default codegen prompt carries only a compact LITEOS_DIGEST plus an
    INDEX of these fragments. The LLM decides which (if any) to pull while
    generating; this tool returns the fragment's full markdown content.

    Valid `name` values: style_rules / linux_to_liteos_table / format_traps /
    ask_first_rules. Other names raise ValueError.
    """
    if name not in _FRAGMENT_REGISTRY:
        raise ValueError(
            f"Unknown fragment {name!r}. Valid names: "
            + ", ".join(sorted(_FRAGMENT_REGISTRY.keys()))
        )
    content = prompts.load(name)
    return {
        "name": name,
        "content": content,
        "size": len(content),
        "hint": _FRAGMENT_REGISTRY[name],
    }


_SPEC_FINE_CAP = 3  # per project memory feedback_specfine_cap_3.md


# v0.5.2: override default 30 s timeout — holistic validator wraps
# tools/regress/run_all.sh which itself has subprocess.run(timeout=900).
# Allow ~920 s at the MCP layer before declaring the wrapper stuck.
@mcp.tool(timeout=920)
def validator_run_holistic(module: str) -> dict[str, Any]:
    """F4 — holistic SpecValidator (paper §4.5).

    Runs at MODULE COMPLETION (not per-stage). Single comprehensive pass:
    1. Wave A — host cmocka via testsuites/unittest/<module>/Makefile
    2. Wave B — QEMU LTP smoke via tools/regress/qemu_<module>_run.sh
    3. Both aggregated by tools/regress/run_all.sh

    Per-stage validation in v0.4 is reduced to LSP only (Layer 1). This
    holistic validator replaces the per-stage Layer 2 (build + QEMU smoke)
    that v0.3 ran on every code_gen_approve — wasteful for module sizes
    that approach the paper's 500 LoC budget.

    Returns:
      {ok: bool, cmocka_pass: bool, qemu_smoke_pass: bool, exit_code: int,
       report_path: str, stderr_tail: str}

    Exit codes from run_all.sh: 0=pass, 1=test failure, 2=panic-or-hang.
    """
    repo = _repo_root()
    runner = repo / "tools" / "regress" / "run_all.sh"
    if not runner.exists():
        return {
            "ok": False,
            "cmocka_pass": False,
            "qemu_smoke_pass": False,
            "exit_code": -1,
            "report_path": "",
            "stderr_tail": f"run_all.sh not found at {runner}",
        }
    log_path = f"/tmp/specfs_holistic_{module}.log"
    try:
        res = subprocess.run(
            ["bash", str(runner)],
            cwd=str(repo),
            capture_output=True, text=True, timeout=900,
            env={**os.environ, "MODULE": module, "REGRESS_LOG": log_path},
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "cmocka_pass": False,
            "qemu_smoke_pass": False,
            "exit_code": -2,
            "report_path": "",
            "stderr_tail": "holistic validator timeout (>15 min)",
        }
    except FileNotFoundError as ex:
        return {
            "ok": False,
            "cmocka_pass": False,
            "qemu_smoke_pass": False,
            "exit_code": -3,
            "report_path": "",
            "stderr_tail": f"failed to run: {ex}",
        }

    out = (res.stdout or "") + (res.stderr or "")
    report_link = repo / "docs" / "test" / f"{module}_regression_latest.md"
    report_path = str(report_link.resolve()) if report_link.exists() else ""
    return {
        "ok": res.returncode == 0,
        "cmocka_pass": "TOTAL FAILURES: 0" in out,
        "qemu_smoke_pass": "LTP_DONE" in out and "panic" not in out.lower(),
        "exit_code": res.returncode,
        "report_path": report_path,
        "stderr_tail": "\n".join(out.strip().splitlines()[-30:]),
    }


@mcp.tool()
def spec_fine(session_id: str, speceval_comments: str) -> dict[str, Any]:
    """F3 SpecFine — polish the approved spec based on SpecEval comments.

    Triggered when Layer 3 (SpecEval) flags a defect that is spec-side, not
    code-side. Hard cap of 3 rounds; on cap exceeded, returns
    `{"next": "user_review", "reason": "spec_fine_cap_exceeded"}` and does
    NOT increment further.

    Returns `{"next": "spec_polish", "prompt": <text>, "attempt": <n>,
    "cap": 3}` on a fresh round. Caller (LLM) uses the prompt to produce a
    polished spec, then calls `spec_fine_submit` to record the result.

    Note: this rewires the SpecEval failure path. Prior behaviour: SpecEval
    fail → code regen only. Now: SpecEval fail → caller decides spec-side or
    code-side fix; spec-side calls spec_fine; code-side keeps the existing
    code_gen_refine path.
    """
    sess = _get(session_id)
    cur = sess.layer_retries.get("spec_fine", 0)
    if cur >= _SPEC_FINE_CAP:
        return {
            "next": "user_review",
            "reason": "spec_fine_cap_exceeded",
            "attempt": cur,
            "cap": _SPEC_FINE_CAP,
            "advice": (
                "SpecFine reached its 3-round cap without the SpecEval defect "
                "being resolved. Escalate: ask the user to revise the spec by "
                "hand or to break the stage into smaller pieces."
            ),
        }

    # Resolve the spec to polish
    spec_path = sess.spec_final_path or sess.code_spec_path
    if not spec_path:
        raise ValueError(
            "Session has no approved spec attached — call spec_gen_approve "
            "or code_gen_start first."
        )
    full = _repo_root() / spec_path
    if not full.exists():
        raise FileNotFoundError(f"Spec file missing: {spec_path}")
    original = full.read_text(encoding="utf-8")

    prompt_text = prompts.assemble_spec_fine_prompt(
        original_spec=original,
        speceval_comments=speceval_comments,
    )
    sess.last_prompt = prompt_text
    sess.layer_retries["spec_fine"] = cur + 1

    return {
        "next": "spec_polish",
        "prompt": prompt_text,
        "spec_path": spec_path,
        "attempt": cur + 1,
        "cap": _SPEC_FINE_CAP,
    }


@mcp.tool()
def spec_fine_submit(session_id: str, polished_spec_text: str) -> dict[str, Any]:
    """Persist a SpecFine round's polished spec back to the approved path.

    Overwrites the approved spec in place. The DAG node spec layer remains
    approved (the polish is treated as a tightening, not a regression). After
    this call, downstream code_gen_refine should be invoked so codegen retries
    against the tighter spec.

    Returns `{"next": "code_regen", "spec_path": <path>, "bytes_written": N}`.
    """
    sess = _get(session_id)
    spec_path = sess.spec_final_path or sess.code_spec_path
    if not spec_path:
        raise ValueError("No approved spec attached to session.")
    full = _repo_root() / spec_path
    if not full.exists():
        raise FileNotFoundError(f"Spec file missing: {spec_path}")
    text = polished_spec_text.strip() + "\n"
    full.write_text(text, encoding="utf-8")
    return {
        "next": "code_regen",
        "spec_path": spec_path,
        "bytes_written": len(text),
        "attempt": sess.layer_retries.get("spec_fine", 0),
    }


@mcp.tool()
def list_prompt_fragments() -> dict[str, dict[str, str]]:
    """Return the fragment registry — names, sizes, and when-to-fetch hints —
    so the LLM can decide whether to call fetch_prompt_fragment(name)."""
    out: dict[str, dict[str, str]] = {}
    for name, hint in _FRAGMENT_REGISTRY.items():
        try:
            size = len(prompts.load(name))
        except FileNotFoundError:
            size = 0
        out[name] = {"hint": hint, "size_chars": str(size)}
    return out


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
    """Map spec/<module>/<sub>/<op>.spec → fs/<module>/<file>.c (best-effort hint).

    The returned path is a HINT — the authoritative final location comes from
    `code_gen_approve(files_to_save=[...])`. Use this only to pre-create the
    `<draft>.c.draft` scratchpad and seed the prompt's "expected output file"
    line. If the hint is wrong, the caller corrects it at approve time.

    v0.3.4.x: previous version baked Linux upstream conventions
    (read+write+open+close all in one `<module>_file.c`, readdir in
    `<module>_dir.c`). LiteOS-A actually splits these per-stage. We now keep
    overrides ONLY for stages that DO share a file in our port:
      - mount/umount/statfs/sync → `<module>_super.c` (FSMAP entry + super ops)
      - read                    → `<module>_file.c` (currently solo there;
                                   if seek/getattr ever join it the override
                                   stays correct, no spec naming clash)
    Everything else falls back to `<module>_<stage>.c` which matches the
    actual per-stage filenames (exfat_write.c, exfat_open_close.c,
    exfat_readdir.c, exfat_lookup.c, exfat_dentry.c, ...).
    """
    p = Path(spec_path)
    stem = p.stem            # "exfat_write" / "exfat_open_close" / ...
    sub = p.parent.name      # "interface" / "inode" / "bitmap" / "util"
    op = stem.split("_", 1)[1] if "_" in stem else stem

    SHARED_FILE_MAP = {
        # super-ops cluster
        "mount":  f"{module}_super.c",
        "umount": f"{module}_super.c",
        "statfs": f"{module}_super.c",
        "sync":   f"{module}_super.c",
        # file-ops cluster
        "read":   f"{module}_file.c",
        # inode-management cluster (P1.5.1, 2026-05-08)
        # All inode-side helpers + VFS callbacks land in <m>_inode.c. The
        # spec [PROMPT] for these stages uses the "addition to <m>_inode.c"
        # pattern — see prompts/linux_to_spec.md TWO-PHASE METHODOLOGY note
        # on standalone vs shared-TU patterns.
        "mkdir":             f"{module}_inode.c",
        "create":            f"{module}_inode.c",
        "lookup":            f"{module}_inode.c",
        "open_close":        f"{module}_inode.c",
        "getattr":           f"{module}_inode.c",
        "seek":              f"{module}_inode.c",
        "inode_alloc":       f"{module}_inode.c",
        "calc_num_entries":  f"{module}_inode.c",
        "zeroed_cluster":    f"{module}_inode.c",
        "alloc_new_dir":     f"{module}_inode.c",
        "init_dir_entry":    f"{module}_inode.c",
        "init_ext_entry":    f"{module}_inode.c",
        "add_entry":         f"{module}_inode.c",
        # mutation paths added in P1.5.2 (2026-05-08)
        "unlink":            f"{module}_inode.c",
        "rmdir":             f"{module}_inode.c",
        "rename":            f"{module}_inode.c",
    }
    fname = SHARED_FILE_MAP.get(op)
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
    # P1.2 (2026-05-07) layer topology:
    #   compile  ─┐  (sibling)        Layer 1 tier — both must pass before
    #   style    ─┘                   Layer 2; SpecEval owns spec conformance,
    #                                 style here owns LiteOS-A coding rules.
    #   build / qemu                  Layer 2.
    #   speceval                      Layer 3 — spec conformance only; NO
    #                                 style rules inlined.
    return {
        "compile": True,  # Layer 1a — clangd LSP only (P1.3 dropped gcc fallback)
        "style":   sess.style_audit_enabled,  # Layer 1 sibling — style canon (was Layer S)
        "build":   not sess.skip_build_layer,
        "qemu":    not sess.skip_build_layer,
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

    # v0.3.4.2 fix: emit the marker comment AT MOST ONCE per common.header.
    # Pre-0.3.4.2 each code_gen_approve appended a fresh marker, so after N
    # stages the file accumulated N "auto-synced exports" comment fragments
    # plus per-stage decl blobs — making manual dedup necessary.
    marker = "/* auto-synced exports — appended by specfs-port code_gen_approve */"
    decls_text = "\n".join(new_decls) + "\n"
    if marker in existing:
        # Append the new decls right after the existing marker block,
        # keeping the marker singleton.
        appended = decls_text
        # Insert after the line containing the marker.
        idx = existing.find(marker)
        line_end = existing.find("\n", idx)
        if line_end == -1:
            line_end = len(existing)
        new_text = existing[: line_end + 1] + appended + existing[line_end + 1 :]
    else:
        appended = "\n\n" + marker + "\n" + decls_text
        new_text = existing + appended
    header_p.parent.mkdir(parents=True, exist_ok=True)
    header_p.write_text(new_text, encoding="utf-8")
    return appended


# _ensure_compile_stub removed in P1.3 (2026-05-07) along with the gcc
# -fsyntax-only fallback. Layer 1a is now LSP-only (clangd via OMC LSP),
# so no hand-rolled stub header is needed — clangd reads the repo's real
# .clangd config and sees actual LiteOS-A headers.


def _git_add(paths: list[str]) -> None:
    """Stage paths (no commit). Idempotent. Logs failures to stderr but does not raise.

    v0.5.2 (2026-05-07): added timeout=10 s. `git add` can stall on
    .git/index.lock from a crashed editor / prior process; without a
    timeout the parent MCP tool would inherit the hang.
    """
    if not paths:
        return
    try:
        subprocess.run(
            ["git", "add", "--"] + paths,
            cwd=str(_repo_root()),
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        pass  # non-fatal


# ---- Section 4.10 — Telemetry --------------------------------------------------


@mcp.tool()
def metrics_summary(session_id: Optional[str] = None) -> dict[str, Any]:
    """Read the per-tool telemetry log and return aggregate counters.

    Args:
        session_id: filter to one session (omit for all-sessions rollup).

    Returns:
        dict with mcp_tool_calls, mcp_total_duration_s, mcp_per_tool,
        llm_rounds, llm_input_tokens_est, llm_output_tokens_est,
        llm_total_gap_s (sum of LLM round-trip wall-clock), errors.

    Note: this tool itself emits an event before returning, so its own
    call shows up in subsequent reads. The event is tagged
    is_llm_round_trigger=False / is_llm_ingest=False, so it won't
    distort the LLM-side counters — only the mcp_per_tool count.
    """
    events = read_log()
    return metrics_aggregate(events, session_id=session_id).as_dict()


# ---- Entry -------------------------------------------------------------------


if __name__ == "__main__":
    mcp.run()
