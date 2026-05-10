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
from _persist_session import install_persist


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

_SESSIONS: dict[str, state.Session] = {}


def _peek_session(session_id: str) -> state.Session | None:
    return _SESSIONS.get(session_id)


install_persist(mcp, _peek_session)


def _get(session_id: str) -> state.Session:
    if session_id in _SESSIONS:
        return _SESSIONS[session_id]
    rehydrated = state.load_session(session_id)
    if rehydrated is not None:
        _SESSIONS[session_id] = rehydrated
        return rehydrated
    raise KeyError(f"Unknown session_id: {session_id}")


def _persist(sess: state.Session) -> None:
    """Mirror in-memory session to ``.specfs/sessions/<id>.json`` so that an
    OpenCode / MCP-server restart does not orphan an in-flight workflow.
    Best-effort; silent on filesystem errors. Call after every mutation
    that the workflow could meaningfully resume from.
    """
    state.save_session(sess)


def _repo_root() -> Path:
    return state.repo_root()


_PATH_PREFIX = "@path:"


def _resolve_text_or_path(arg: str, kind: str) -> str:
    """Decode submit/approve text args supporting an `@path:` redirection.

    When `arg` starts with `@path:`, the remainder is interpreted as a path
    (absolute or repo-relative) and that file's content is returned. This lets
    callers avoid round-tripping large generated artifacts through tool-call
    arguments. Otherwise the arg is returned as-is (legacy text mode).

    `kind` is a label used only in error messages.
    """
    if not arg.startswith(_PATH_PREFIX):
        return arg
    rel = arg[len(_PATH_PREFIX):].strip()
    if not rel:
        raise RuntimeError(f"{kind}: empty path after '@path:' prefix")
    p = Path(rel)
    if not p.is_absolute():
        p = _repo_root() / rel
    if not p.exists():
        raise RuntimeError(f"{kind}: path-mode file does not exist: {p}")
    return p.read_text(encoding="utf-8")


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


def _git_diff_for_files(files: list[str]) -> str:
    """Return a unified diff of ``files`` vs HEAD, including untracked
    additions. Used by Layer T to show the LLM exactly what this stage
    added/changed without inlining the full per-file source.

    Returns "" on any failure or when ``files`` is empty.
    """
    if not files:
        return ""
    try:
        tracked = subprocess.check_output(
            ["git", "diff", "--no-color", "HEAD", "--", *files],
            cwd=str(_repo_root()),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "--", *files],
            cwd=str(_repo_root()),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).splitlines()
        if untracked:
            untracked_diff = subprocess.check_output(
                ["git", "diff", "--no-color", "--no-index", "/dev/null", *untracked],
                cwd=str(_repo_root()),
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
            )
            tracked = (tracked + "\n" + untracked_diff).strip()
        return tracked
    except subprocess.CalledProcessError as e:
        return e.output if isinstance(e.output, str) else ""
    except (subprocess.SubprocessError, OSError):
        return ""


# ---- Section 4.1 — Session lifecycle ----------------------------------------


@mcp.tool()
def session_start(module: str, mode: str = "gen") -> dict[str, Any]:
    """Start a new specfs-port session for the given FS module.

    Args:
        module: FS module name (e.g., "exfat").
        mode: One of "gen", "evolve", "fast_eval".
            - "gen": new stage with full layered defense + retry budgets.
            - "evolve": optimisation variant of an existing stage.
            - "fast_eval" (P1.6 Wave 2): single-shot prompt-evaluation
              mode. Spec and code each generated ONCE; no SpecEval / no
              spec_fine / no refine / no inject_diagnostics / no build /
              no QEMU / no Layer T. Use when measuring prompt quality
              for Loop C; the only iteration is across stages, not within
              a stage.

    Returns:
        {session_id, dag_state, dirty_nodes, fast_eval_mode}
    """
    if mode not in ("gen", "evolve", "fast_eval"):
        raise ValueError(
            f"mode must be one of 'gen' / 'evolve' / 'fast_eval', got {mode!r}"
        )
    sess = state.new_session(module=module, mode=mode)
    if mode == "fast_eval":
        sess.fast_eval_mode = True
        # Disable every iterative-repair / validation layer so the
        # downstream orchestrator cannot accidentally feed defect-driven
        # repair signal into a session that exists to measure first-shot
        # prompt quality.
        sess.speceval_enabled = False
        sess.style_audit_enabled = False
        sess.test_gen_enabled = False
        sess.skip_build_layer = True
    _SESSIONS[sess.session_id] = sess
    _persist(sess)
    dag_state = dag_module.load(module)
    return {
        "session_id": sess.session_id,
        "dag_state": dag_state,
        "dirty_nodes": dag_module.list_dirty(dag_state),
        "fast_eval_mode": sess.fast_eval_mode,
    }


@mcp.tool()
def session_status(session_id: str) -> dict[str, Any]:
    """Return current phase and retry counters for a session."""
    sess = _get(session_id)
    latest = _latest_round_failures(sess.failures)
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
        "n_failures_total": len(sess.failures),
        "n_failures_latest_round": len(latest),
        "n_user_suggestions_total": len(sess.user_suggestions),
    }


@mcp.tool()
def session_end(session_id: str) -> dict[str, str]:
    """Drop session state. Idempotent."""
    _SESSIONS.pop(session_id, None)
    state.drop_session(session_id)
    return {"status": "ended"}


@mcp.tool()
def toggle_speceval(session_id: str, enabled: bool) -> dict[str, bool]:
    """Enable / disable Step 4 spec/code audit for this session. Default ON.

    Legacy alias retained for backward compat — prefer toggle_audit.
    """
    sess = _get(session_id)
    sess.speceval_enabled = bool(enabled)
    return {"speceval_enabled": sess.speceval_enabled}


@mcp.tool()
def toggle_audit(session_id: str, enabled: bool) -> dict[str, bool]:
    """Enable / disable Step 4 spec/code audit for this session. Default ON.

    Backs --audit-off CLI flag in /specfs-port-code.
    """
    sess = _get(session_id)
    sess.speceval_enabled = bool(enabled)
    return {"audit_enabled": sess.speceval_enabled}


@mcp.tool()
def toggle_skip_build(session_id: str, skip: bool) -> dict[str, bool]:
    """Skip Layer 2 (build + QEMU) — fast iteration on logic-only specs."""
    sess = _get(session_id)
    sess.skip_build_layer = bool(skip)
    return {"skip_build_layer": sess.skip_build_layer}


@mcp.tool()
def toggle_fast_eval_mode(session_id: str, enabled: bool) -> dict[str, bool]:
    """Toggle Loop C fast-eval mode mid-session (P1.6 Wave 2).

    Enabling at any point disables every iterative-repair layer
    (speceval, style_audit, test_gen, build/qemu) and refuses subsequent
    spec_gen_refine / code_gen_refine / spec_fine / inject_diagnostics
    calls. Disabling restores those flags' defaults so a session can
    transition from prompt-evaluation back to normal generation if
    needed.

    Most callers should pass mode="fast_eval" to session_start instead;
    this toggle exists for the rare case of converting an in-flight
    session into an evaluation harness.
    """
    sess = _get(session_id)
    sess.fast_eval_mode = bool(enabled)
    if sess.fast_eval_mode:
        sess.speceval_enabled = False
        sess.style_audit_enabled = False
        sess.test_gen_enabled = False
        sess.skip_build_layer = True
    return {"fast_eval_mode": sess.fast_eval_mode}


def _refuse_if_fast_eval(sess: state.Session, tool_name: str) -> None:
    """Hard gate: tools listed below are disabled in fast_eval mode so
    Loop C measures first-shot prompt quality, not iterative repair."""
    if sess.fast_eval_mode:
        raise ValueError(
            f"{tool_name} is disabled in fast_eval mode (Loop C "
            "single-shot prompt evaluation). Re-create the session with "
            "mode='gen' or call toggle_fast_eval_mode(enabled=False) if "
            "you need iterative repair."
        )


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
        user_suggestions=[],
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
    the previous draft + user's CURRENT-ROUND suggestion baked into
    [USER SUGGESTIONS] / [Previously generated spec].

    Paper-aligned (gencode.py:180 single-round semantics): only the
    just-submitted suggestion is injected. Earlier rounds' suggestions
    are already MATERIALISED in `previous_spec` (the LLM rewrote the
    draft after each round), so re-injecting them inflates the prompt
    and confuses the LLM about which hints are still actionable.
    """
    sess = _get(session_id)
    _refuse_if_fast_eval(sess, "spec_gen_refine")
    sess.spec_iterations += 1
    sess.user_suggestions.append(user_suggestion)

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
        user_suggestions=[user_suggestion],
        previous_spec=prev,
    )
    sess.last_prompt = prompt_text
    return {"next_prompt": prompt_text, "iteration": sess.spec_iterations}


@mcp.tool()
def spec_gen_submit(session_id: str, generated_spec_text: str) -> dict[str, Any]:
    """Stash a freshly drafted spec; tell caller to present to user for review.

    `generated_spec_text` accepts an `@path:<repo-rel-or-abs>` redirection —
    the server reads that file's content instead of treating the arg as
    literal text. Use this for large drafts to keep tool-call args small.
    """
    sess = _get(session_id)
    generated_spec_text = _resolve_text_or_path(generated_spec_text, "spec_gen_submit")
    sess.current_artifact = generated_spec_text
    draft_p = _repo_root() / sess.spec_draft_path
    draft_p.parent.mkdir(parents=True, exist_ok=True)
    draft_p.write_text(generated_spec_text, encoding="utf-8")
    # P1.6 Wave 2: stash first submission as <draft>.first.spec for Loop C
    # linux_compare. ONLY written once — refine / spec_fine submissions don't
    # touch this snapshot. Compare against this baseline so the comparison
    # measures the prompt's first-shot quality, not the LLM's iterative
    # repair ability.
    first_p = draft_p.with_suffix(draft_p.suffix + ".first")
    if not first_p.exists():
        first_p.write_text(generated_spec_text, encoding="utf-8")
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

    Path-mode: `final_spec_text` may be `@path:<repo-rel-or-abs>` (most useful:
    `@path:<draft_path>` to roll the existing draft to final without
    re-piping the file content through the tool argument).
    """
    sess = _get(session_id)
    final_spec_text = _resolve_text_or_path(final_spec_text, "spec_gen_approve")
    if len(final_spec_text) < 50:
        raise RuntimeError(
            f"spec_gen_approve: refusing to write {len(final_spec_text)}-byte "
            f"spec to {sess.spec_final_path} — empty/near-empty payload, "
            f"likely a caller bug."
        )
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

    target_stage = _stage_from_spec_path(spec_path)
    dag_state = dag_module.load(sess.module)
    node_id = _stage_id(target_stage)
    inv = dag_module.collect_invariants(dag_state, node_id)
    ifaces = extract.extract_module_interface(sess.module, _repo_root())
    prior_symbols = extract.collect_all_symbols(ifaces)

    full_header = _common_header(sess.module)
    keep = prompts.collect_codegen_keep_symbols(spec_content)
    filtered_header = (
        prompts.filter_common_header_by_symbols(full_header, keep) if keep else full_header
    )
    prior_iface_text = extract.render_interface_summary(ifaces, keep_symbols=keep or None)

    prompt_text = prompts.assemble_codegen_prompt(
        module=sess.module,
        spec_content=spec_content,
        common_header=filtered_header,
        inherited_invariants=inv,
        prior_code_interface=prior_iface_text,
        previous_code="",
        failures=[],
    )
    sess.last_prompt = prompt_text

    return {
        "prompt_for_llm": prompt_text,
        "draft_path": _derive_code_path(sess.module, spec_path),
        "frozen_contract_size": len(filtered_header),
        "frozen_contract_size_full": len(full_header),
        "frozen_contract_filter_savings": len(full_header) - len(filtered_header),
        "n_inherited_invariants": len(inv),
        "n_prior_symbols": len(prior_symbols),
        "prompt_source": "assembled",
    }


@mcp.tool()
def code_gen_submit(session_id: str, generated_code: str) -> dict[str, Any]:
    """Stash the freshly generated code and tell caller what layer to run next.

    `generated_code` accepts an `@path:<repo-rel-or-abs>` redirection — the
    server reads the file at that path instead of treating the arg as literal
    text. Use this for large artifacts to avoid bloating tool-call context.
    """
    sess = _get(session_id)
    generated_code = _resolve_text_or_path(generated_code, "code_gen_submit")
    if len(generated_code) < 30:
        raise RuntimeError(
            f"code_gen_submit: refusing to stash {len(generated_code)}-byte "
            f"draft — empty/near-empty payload. Use a non-empty marker "
            f"comment if the artifact is already written via Edit/Write."
        )
    sess.current_artifact = generated_code
    draft_path = _derive_code_path(sess.module, sess.code_spec_path)
    sess.code_draft_paths = [draft_path]

    full = _repo_root() / draft_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(generated_code, encoding="utf-8")

    first_p = full.with_suffix(full.suffix + ".first")
    if not first_p.exists():
        first_p.write_text(generated_code, encoding="utf-8")

    if sess.style_audit_enabled:
        next_phase = "style_audit"
    elif sess.speceval_enabled:
        sess.speceval_pending = True
        next_phase = "speceval"
    else:
        next_phase = "compile"

    return {
        "next": next_phase,
        "draft_path": draft_path,
        "iteration": sess.code_iterations,
    }


@mcp.tool()
def code_gen_refine(session_id: str, user_suggestion: str) -> dict[str, Any]:
    """User-driven refine: inject user_suggestion as <source: user> in [Modification suggestions]."""
    sess = _get(session_id)
    _refuse_if_fast_eval(sess, "code_gen_refine")
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

    Multi-file mode: when ``final_code`` is empty (``""``), the server treats
    every path in ``files_to_save`` as already-written on disk (the caller
    used Edit/Write to apply per-file diffs because the spec required
    multi-file output that the single-string ``final_code`` API cannot
    represent). Files are read back from disk; ``code_final_text`` becomes
    the concatenation with FILE markers so Layer T sees real symbols. The
    on-disk content is NOT overwritten in this mode — protects working tree
    against an accidental empty ``final_code``.

    Path-mode: ``final_code`` may be ``@path:<repo-rel-or-abs>`` to redirect
    to a file on disk (single-file mode); the server reads the path's content
    and writes it to every entry in ``files_to_save`` (existing single-file
    semantics preserved).
    """
    sess = _get(session_id)
    final_code = _resolve_text_or_path(final_code, "code_gen_approve")

    saved: list[str] = []
    captured_chunks: list[str] = []
    multi_file = (final_code == "")
    if not multi_file and len(final_code) < 50:
        raise RuntimeError(
            f"code_gen_approve: refusing to write {len(final_code)}-byte "
            f"final_code to {files_to_save!r} — empty/near-empty payload. "
            f"If you meant multi-file mode, pass final_code=\"\" exactly."
        )
    for f in files_to_save:
        full = _repo_root() / f
        full.parent.mkdir(parents=True, exist_ok=True)
        if multi_file:
            if not full.exists():
                raise RuntimeError(
                    f"code_gen_approve: multi-file mode requires {f} to "
                    f"already exist on disk; got missing path"
                )
            existing = full.read_text(encoding="utf-8")
            captured_chunks.append(f"/* === FILE: {f} === */\n{existing}")
        else:
            full.write_text(final_code, encoding="utf-8")
        saved.append(f)
    captured_final = ("\n".join(captured_chunks) if multi_file else final_code)

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
    sess.code_final_text = captured_final
    sess.code_final_paths = saved

    if sess.speceval_pending:
        raise RuntimeError(
            "speceval_pending=True; the merged spec/code audit must complete "
            "BEFORE code_gen_approve. Call enforce_speceval(session_id), "
            "spawn an INDEPENDENT reviewer agent, then post the verdict via "
            "record_speceval_verdict (alias: enforce_audit / record_audit_verdict)."
        )

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
def test_gen_start(session_id: str, spec_path: str = "") -> dict[str, Any]:
    """Begin Layer T: assemble the unittest_gen prompt from the just-approved
    code + spec + harness layout snapshot.

    Pre-condition: the stage's code layer must be approved (either in this
    session via code_gen_approve, OR in any prior session — the DAG state
    carries the code.files manifest forward). Layer T is also gated by
    test_gen_enabled.

    Session-rebuild path: when ``code_final_text`` is empty but the DAG node
    for this stage already records ``code.files``, the server reconstructs
    ``code_final_text`` and ``code_spec_path`` from disk. Pass ``spec_path``
    when the session was newly minted via ``session_start`` (no prior
    ``code_gen_start`` call); otherwise the field is recovered from session
    state. This eliminates the need to re-run code_gen_start/submit/approve
    just to advance phase after an OpenCode restart.

    Returns:
        {prompt_for_llm, draft_path, final_path, harness_dir_exists,
         rehydrated: bool}
    """
    sess = _get(session_id)
    if not sess.test_gen_enabled:
        raise RuntimeError(
            "test_gen disabled for this session — enable via toggle_test_gen "
            "or remove --test-off from the slash-command args"
        )

    rehydrated = False
    if not sess.code_final_text:
        if not sess.code_spec_path and spec_path:
            sess.code_spec_path = spec_path
        if not sess.code_spec_path:
            raise RuntimeError(
                "Layer T requires either an approved code artifact in this "
                "session, or a spec_path argument so the DAG node can be "
                "located. Got neither."
            )
        target_stage = _stage_from_spec_path(sess.code_spec_path)
        dag_state = dag_module.load(sess.module)
        node = dag_module.find_node(dag_state, _stage_id(target_stage))
        code_layer = (node or {}).get("code") or {}
        files = code_layer.get("files") or ([code_layer["path"]] if code_layer.get("path") else [])
        if not files:
            raise RuntimeError(
                "Layer T requires an approved code artifact. Neither this "
                "session nor the DAG node carries one — run code_gen_approve "
                "first."
            )
        chunks: list[str] = []
        for f in files:
            full = _repo_root() / f
            if not full.exists():
                raise RuntimeError(
                    f"DAG node references {f} but the file is missing from "
                    f"the working tree; aborting Layer T rehydrate."
                )
            chunks.append(f"/* === FILE: {f} === */\n{full.read_text(encoding='utf-8')}")
        sess.code_final_text = "\n".join(chunks)
        sess.code_final_paths = list(files)
        rehydrated = True

    sess.phase = "test_drafting"
    stage = _stage_from_spec_path(sess.code_spec_path)
    sess.test_stage = stage
    draft_rel, final_rel = _test_paths_for(sess.module, stage)
    sess.test_draft_path = draft_rel
    sess.test_final_path = final_rel

    spec_p = _repo_root() / sess.code_spec_path
    spec_content = spec_p.read_text(encoding="utf-8") if spec_p.exists() else ""

    code_files = list(sess.code_final_paths) if sess.code_final_paths else []
    code_diff = _git_diff_for_files(code_files)

    prompt_text = prompts.assemble_unittest_gen_prompt(
        code_files=code_files,
        code_diff=code_diff,
        spec_path=sess.code_spec_path,
        original_spec=spec_content,
        harness_layout=_harness_layout(sess.module),
    )
    sess.last_prompt = prompt_text

    return {
        "prompt_for_llm": prompt_text,
        "draft_path": draft_rel,
        "final_path": final_rel,
        "harness_dir_exists": _harness_dir(sess.module).is_dir(),
        "rehydrated": rehydrated,
        "code_files": code_files,
        "code_diff_size": len(code_diff),
    }


@mcp.tool()
def test_gen_submit(session_id: str, generated_test_text: str) -> dict[str, Any]:
    """Stash the freshly generated test draft. Writes to <draft_path>.

    Returns next='review' so the slash command knows to surface code+test
    together in the Layer 4 user-review pass.

    `generated_test_text` accepts an `@path:<repo-rel-or-abs>` redirection —
    the server reads that file's content instead of treating the arg as
    literal text. Use this for large drafts to keep tool-call args small.
    """
    sess = _get(session_id)
    if not sess.test_draft_path:
        raise RuntimeError("test_gen_start must be called before test_gen_submit")

    generated_test_text = _resolve_text_or_path(generated_test_text, "test_gen_submit")
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

    Path-mode: `final_test_text` may be `@path:<repo-rel-or-abs>` (most useful:
    `@path:<draft_path>` to roll the existing draft to final without
    re-piping the file content through the tool argument).
    """
    sess = _get(session_id)
    if not sess.test_final_path:
        raise RuntimeError("test_gen_start must be called before test_gen_approve")

    final_test_text = _resolve_text_or_path(final_test_text, "test_gen_approve")
    if len(final_test_text) < 50:
        raise RuntimeError(
            f"test_gen_approve: refusing to write {len(final_test_text)}-byte "
            f"test file to {sess.test_final_path} — empty/near-empty "
            f"payload, likely a caller bug."
        )
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
def enforce_speceval(session_id: str) -> dict[str, Any]:
    """Step 4 spec/code audit gate — assemble the prompt and require an
    INDEPENDENT reviewer agent.

    Returns the prompts/speceval.md prompt populated with the current code
    artifact + original spec, plus a ``reviewer_contract`` block telling the
    caller to spawn a fresh-context reviewer subagent. The author of the
    code MUST NOT be the evaluator — self-evaluation introduces confirmation
    bias.

    Runs PRE-APPROVE: caller invokes after code_gen_submit + Step 2 static
    checks pass, but BEFORE code_gen_approve. Refuses with backward-compat
    behaviour if speceval_pending is unset (older sessions that approved
    code first will see this on a code_gen_submit re-run).

    Returns:
        {prompt_for_llm, reviewer_contract, code_path, spec_path,
         attempt, retry_cap}
    """
    sess = _get(session_id)
    _refuse_if_fast_eval(sess, "enforce_speceval")

    spec_path = sess.code_spec_path or sess.spec_final_path
    if not spec_path:
        raise RuntimeError("no spec_path on session; run code_gen_start first")
    spec_text = (_repo_root() / spec_path).read_text(encoding="utf-8")

    code_text = sess.current_artifact or sess.code_final_text
    if not code_text and sess.code_final_paths:
        chunks = []
        for p in sess.code_final_paths:
            full = _repo_root() / p
            if full.exists():
                chunks.append(f"// FILE: {p}\n" + full.read_text(encoding="utf-8"))
        code_text = "\n\n".join(chunks)
    if not code_text and sess.code_draft_paths:
        chunks = []
        for p in sess.code_draft_paths:
            full = _repo_root() / p
            if full.exists():
                chunks.append(f"// FILE: {p}\n" + full.read_text(encoding="utf-8"))
        code_text = "\n\n".join(chunks)
    if not code_text:
        raise RuntimeError(
            "no code artifact on session; call code_gen_submit first"
        )

    if not sess.speceval_pending and sess.speceval_enabled:
        sess.speceval_pending = True

    spec_approved_at = ""
    try:
        dag_state = dag_module.load(sess.module)
        node_id = sess.code_spec_path.rsplit("/", 1)[-1].removesuffix(".spec")
        for node in dag_state.get("stages", []):
            if node.get("id") == node_id:
                spec_approved_at = (node.get("spec") or {}).get(
                    "approved_at", ""
                ) or ""
                break
    except Exception:
        spec_approved_at = ""

    prompt = prompts.assemble_speceval_prompt(
        generated_code=code_text,
        original_spec=spec_text,
        spec_approved_at=spec_approved_at,
    )
    sess.last_prompt = prompt

    reviewer_contract = (
        "REVIEWER CONTRACT — READ BEFORE PROCEEDING:\n"
        "1. The agent calling this tool MUST NOT evaluate the prompt itself.\n"
        "2. Spawn an INDEPENDENT reviewer subagent with FRESH context using:\n"
        "     task(subagent_type=\"Momus - Plan Critic\", run_in_background=false,\n"
        "          load_skills=[], description=\"SpecEval for <stage>\",\n"
        "          prompt=<prompt_for_llm>)\n"
        "3. Momus returns a JSON verdict {is_good: bool, comments: str}.\n"
        "4. Post the verdict back via record_speceval_verdict(session_id, verdict_json).\n"
        "5. The server uses the verdict to choose: pass-through, code_gen_refine,\n"
        "   or spec_fine."
    )

    cap = 8
    return {
        "prompt_for_llm": prompt,
        "reviewer_contract": reviewer_contract,
        "code_path": sess.code_final_paths or [sess.code_spec_path],
        "spec_path": spec_path,
        "attempt": sess.layer_retries.get("speceval", 0),
        "retry_cap": cap,
    }


@mcp.tool()
def record_speceval_verdict(
    session_id: str, verdict_json: str,
) -> dict[str, Any]:
    """Consume the JSON verdict from an independent reviewer subagent.

    Verdict schema (from prompts/speceval.md):
        {"is_good": bool, "comments": str}

    On is_good=True: clear speceval_pending. Caller advances to Step 5
        runtime validation (cmocka exec + QEMU smoke), then code_gen_approve
        on user approval.

    On is_good=False: record FailureRecord(layer="speceval"), increment
        retry counter (cap 8), KEEP speceval_pending=True so the gate stays
        armed. Returns next="code_gen_refine" with the comments wired as the
        suggestion. Caller decides spec_fine vs code_gen_refine based on
        finding root_cause.

    Returns:
        {ok, is_good, next, retries, retry_cap, comments}
    """
    sess = _get(session_id)
    _refuse_if_fast_eval(sess, "record_speceval_verdict")

    try:
        verdict = json.loads(verdict_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"verdict_json is not valid JSON: {e}") from e
    if "is_good" not in verdict:
        raise RuntimeError("verdict missing required key 'is_good'")
    is_good = bool(verdict["is_good"])
    comments = str(verdict.get("comments", "")).strip()

    if is_good:
        sess.speceval_pending = False
        return {
            "ok": True, "is_good": True, "next": "runtime_validation",
            "retries": sess.layer_retries.get("speceval", 0),
            "retry_cap": 8, "comments": "",
        }

    sess.layer_retries["speceval"] = sess.layer_retries.get("speceval", 0) + 1
    sess.failures.append(state.FailureRecord(
        layer="speceval", payload=comments, round_idx=sess.code_iterations,
    ))
    return {
        "ok": True, "is_good": False, "next": "code_gen_refine",
        "retries": sess.layer_retries["speceval"], "retry_cap": 8,
        "comments": comments,
    }


@mcp.tool()
def enforce_audit(session_id: str) -> dict[str, Any]:
    """Step 4 spec/code audit gate — alias of enforce_speceval.

    Use this name from new code; the legacy enforce_speceval is retained
    for session-JSON backward compat. Same returns and semantics.
    """
    return enforce_speceval(session_id)


@mcp.tool()
def record_audit_verdict(
    session_id: str, verdict_json: str,
) -> dict[str, Any]:
    """Consume Step 4 audit verdict — alias of record_speceval_verdict.

    Use this name from new code. Same returns and semantics.
    """
    return record_speceval_verdict(session_id, verdict_json)


@mcp.tool()
def inject_diagnostics(
    session_id: str,
    layer: str,
    payload: str,
) -> dict[str, Any]:
    """Record a layer failure and produce next-round codegen prompt with
    [Modification suggestions] source=<layer> appended."""
    sess = _get(session_id)
    _refuse_if_fast_eval(sess, "inject_diagnostics")
    if layer not in ("compile", "style", "build", "qemu", "speceval", "user"):
        raise ValueError(f"unknown layer: {layer}")
    sess.layer_retries[layer] = sess.layer_retries.get(layer, 0) + 1
    sess.code_iterations += 1
    sess.failures.append(state.FailureRecord(
        layer=layer,
        payload=payload,
        round_idx=sess.code_iterations,
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


@mcp.tool()
def dedup_common_header(module: str, dry_run: bool = False) -> dict[str, Any]:
    """One-shot cleanup of duplicate `extern fn(...)` lines in common.header.

    Pre-fix sync used raw-string compare so whitespace-different signatures
    of the same function were both appended. This tool keeps the FIRST
    occurrence of each function name and drops later duplicates. Non-extern
    lines (typedefs, struct decls, comments, blank lines) are preserved
    verbatim. Set dry_run=True to preview without writing.
    """
    header_p = _spec_dir(module) / "common.header"
    if not header_p.exists():
        return {"ok": False, "reason": "common.header not found"}

    text = header_p.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    seen: set[str] = set()
    kept: list[str] = []
    dropped: list[str] = []

    for ln in lines:
        m = _EXTERN_FN_NAME_RE.match(ln)
        if m:
            fn = m.group(1)
            if fn in seen:
                dropped.append(ln.rstrip("\n"))
                continue
            seen.add(fn)
        kept.append(ln)

    new_text = "".join(kept)
    if not dry_run and new_text != text:
        header_p.write_text(new_text, encoding="utf-8")
        _git_add([f"spec/{module}/common.header"])

    return {
        "ok": True,
        "dry_run": dry_run,
        "before_lines": len(lines),
        "after_lines": len(kept),
        "removed_count": len(dropped),
        "removed_sample": dropped[:5],
        "bytes_before": len(text),
        "bytes_after": len(new_text),
    }


_FRAGMENT_REGISTRY = {
    "style_rules": "Full LiteOS-A style rules: naming, layout, license, error path, file generation order. Fetch when picking a function-name convention or inventing a helper file.",
    "linux_to_liteos_table": "Full Linux→LiteOS-A primitive map: types, memory, locking, disk IO, strings, errno, VFS callbacks, linker tables. Fetch when the spec mentions a Linux primitive the digest does not cover.",
    "format_traps": "Format-compatibility traps: CRC variants, byte order, charsets, packed-struct alignment. Fetch when the spec touches on-disk data with checksums / multi-byte fields / non-UTF-8 charsets.",
    "ask_first_rules": "Ask-first disambiguation framework. Fetch only if mid-codegen you encounter spec ambiguity that the spec author did not resolve.",
    "two_phase_rules": "P1.5/P1.6 two-phase methodology — full forbidden lists for Phase 1 / Phase 2, trigger conditions, canonical examples, borderline-case Q&A. Fetch when unsure whether a construct (e.g. LOS_MuxInit) belongs in Phase 1 or Phase 2.",
}


@mcp.tool()
def fetch_prompt_fragment(name: str) -> dict[str, str]:
    """LLM-driven on-demand expansion of a code-gen reference fragment.

    The default codegen prompt carries only a compact LITEOS_DIGEST plus an
    INDEX of these fragments. The LLM decides which (if any) to pull while
    generating; this tool returns the fragment's full markdown content.

    Valid `name` values: style_rules / linux_to_liteos_table / format_traps /
    ask_first_rules / two_phase_rules. Other names raise ValueError.
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
_VALIDATOR_MODES = {"holistic", "cmocka_only", "qemu_only"}


@mcp.tool(timeout=920)
def validator_run_holistic(
    module: str, mode: str = "holistic",
) -> dict[str, Any]:
    """Module-completion regression runner (paper §4.5 SpecValidator) with
    per-stage shortcuts.

    Modes:
      - "holistic" (default): Wave A cmocka host + Wave B QEMU LTP smoke,
        aggregated by tools/regress/run_all.sh. Use at module completion.
      - "cmocka_only": Wave A only — used by Loop code Step 5.1 per-stage
        cmocka exec. Skips QEMU.
      - "qemu_only": Wave B only — used by Loop code Step 5.2 per-stage
        QEMU smoke. Skips cmocka.

    Returns:
      {ok, cmocka_pass, qemu_smoke_pass, exit_code, report_path,
       stderr_tail, mode}

    Exit codes: 0=pass, 1=test failure, 2=panic-or-hang.
    """
    if mode not in _VALIDATOR_MODES:
        raise RuntimeError(
            f"validator_run_holistic: unknown mode={mode!r}; "
            f"expected one of {sorted(_VALIDATOR_MODES)}"
        )
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
            "mode": mode,
        }
    log_path = f"/tmp/specfs_{mode}_{module}.log"
    extra_env = {"MODULE": module, "REGRESS_LOG": log_path}
    if mode == "cmocka_only":
        extra_env["REGRESS_SKIP_QEMU"] = "1"
    elif mode == "qemu_only":
        extra_env["REGRESS_SKIP_CMOCKA"] = "1"
    try:
        res = subprocess.run(
            ["bash", str(runner)],
            cwd=str(repo),
            capture_output=True, text=True, timeout=900,
            env={**os.environ, **extra_env},
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "cmocka_pass": False,
            "qemu_smoke_pass": False,
            "exit_code": -2,
            "report_path": "",
            "stderr_tail": "validator timeout (>15 min)",
            "mode": mode,
        }
    except FileNotFoundError as ex:
        return {
            "ok": False,
            "cmocka_pass": False,
            "qemu_smoke_pass": False,
            "exit_code": -3,
            "report_path": "",
            "stderr_tail": f"failed to run: {ex}",
            "mode": mode,
        }

    out = (res.stdout or "") + (res.stderr or "")
    report_link = repo / "docs" / "test" / f"{module}_regression_latest.md"
    report_path = str(report_link.resolve()) if report_link.exists() else ""
    cmocka_pass = (
        "TOTAL FAILURES: 0" in out if mode != "qemu_only" else True
    )
    qemu_smoke_pass = (
        ("LTP_DONE" in out and "panic" not in out.lower())
        if mode != "cmocka_only"
        else True
    )
    return {
        "ok": res.returncode == 0,
        "cmocka_pass": cmocka_pass,
        "qemu_smoke_pass": qemu_smoke_pass,
        "exit_code": res.returncode,
        "report_path": report_path,
        "stderr_tail": "\n".join(out.strip().splitlines()[-30:]),
        "mode": mode,
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
    _refuse_if_fast_eval(sess, "spec_fine")
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


# ---- Loop C — Linux-functional comparison (P1.6 Wave 2, 2026-05-08) ----------
# User directive (deferred from v0.5.5): "实现评估prompt 和生成C代码的评估机制
# (通过对比linux 代码功能) 来通过反馈优化spec 抽取prompt, 以及代码生成prompt"
#
# Pipeline position: AFTER code_gen_approve. Compares the generated SYSSPEC spec
# + LiteOS-A C against the original Linux TU and produces:
#   (1) per-gap fix suggestions (severity-tagged),
#   (2) ADDITIVE prompt-tuning recommendations for linux_to_spec.md and
#       codegen.md — written to docs/<module>_prompt_feedback.md for HITL
#       review. The plugin DOES NOT auto-rewrite prompt templates.
# High-severity gaps are also stashed as `linux_compare` FailureRecords on the
# session so the next code_gen_refine / spec_fine call surfaces them.

_FEEDBACK_DOC_DIR = "docs"  # docs/<module>_prompt_feedback.md


def _feedback_doc_path(module: str) -> Path:
    return _repo_root() / _FEEDBACK_DOC_DIR / f"{module}_prompt_feedback.md"


def _stage_from_spec_path_safe(spec_path: str) -> str:
    """Best-effort stage extractor that doesn't blow up on unusual paths."""
    try:
        return _stage_from_spec_path(spec_path)
    except Exception:
        return Path(spec_path).stem


@mcp.tool()
def linux_compare_start(
    session_id: str,
    linux_source_path: str,
    code_path: Optional[str] = None,
) -> dict[str, Any]:
    """Loop C — assemble the Linux↔generated comparison prompt.

    Args:
        session_id: existing session (must have an approved spec and at least
            one generated code path on disk).
        linux_source_path: path to the original Linux TU. Absolute, or
            relative to repo root, or relative to /Users/kissa/Codebase/linux.
        code_path: override the code path to compare. Defaults to the
            session's first `code_final_paths` entry, falling back to the
            file derived from `code_spec_path`.

    Returns:
        {prompt_for_llm, linux_path, spec_path, code_path,
         linux_source_size, code_size}

    The LLM is expected to consume `prompt_for_llm` and reply with a JSON
    body matching the schema documented in prompts/linux_compare.md. That
    JSON is then handed back via `linux_compare_submit`.
    """
    sess = _get(session_id)

    spec_path = sess.spec_final_path or sess.code_spec_path
    if not spec_path:
        raise ValueError(
            "Session has no approved spec attached. Run spec_gen_approve "
            "or code_gen_start first."
        )
    spec_full = _repo_root() / spec_path
    if not spec_full.is_file():
        raise FileNotFoundError(f"Spec missing on disk: {spec_path}")

    # P1.6 Wave 2 (per user directive 2026-05-08): linux_compare must measure
    # the prompt's first-shot quality, not the LLM's iterative repair ability.
    # Prefer the `.first` snapshot captured at the first spec_gen_submit /
    # code_gen_submit. Falls back to the current file only when no snapshot
    # exists (legacy stages / DAG-imported nodes).
    spec_first_candidates = [
        # Final-path snapshot — when first submission landed via approve.
        spec_full.with_suffix(spec_full.suffix + ".first"),
        # Draft-path snapshot — when first submission stayed in draft form
        # (most common path: spec_gen_submit writes draft.first).
        (_repo_root() / sess.spec_draft_path).with_suffix(
            (_repo_root() / sess.spec_draft_path).suffix + ".first"
        ) if sess.spec_draft_path else None,
    ]
    spec_first = next(
        (p for p in spec_first_candidates if p is not None and p.is_file()),
        None,
    )
    if spec_first is not None:
        generated_spec = spec_first.read_text(encoding="utf-8")
        spec_source = f"{spec_first.relative_to(_repo_root())} (.first snapshot)"
    else:
        generated_spec = spec_full.read_text(encoding="utf-8")
        spec_source = f"{spec_path} (current — no .first snapshot found)"

    if code_path is None:
        if sess.code_final_paths:
            code_path = sess.code_final_paths[0]
        else:
            code_path = _derive_code_path(sess.module, spec_path)
    code_full = _repo_root() / code_path
    if not code_full.is_file():
        raise FileNotFoundError(
            f"Code file missing: {code_path}. Approve via code_gen_approve "
            f"before running linux_compare."
        )

    code_first_p = code_full.with_suffix(code_full.suffix + ".first")
    if code_first_p.is_file():
        generated_code = code_first_p.read_text(encoding="utf-8")
        code_source = f"{code_first_p.relative_to(_repo_root())} (.first snapshot)"
    else:
        generated_code = code_full.read_text(encoding="utf-8")
        code_source = f"{code_path} (current — no .first snapshot found)"

    # Linux source resolution: try as-given, then repo-rel, then default OH
    # workspace fallback.
    candidates = [
        Path(linux_source_path),
        _repo_root() / linux_source_path,
        Path("/Users/kissa/Codebase/linux") / linux_source_path,
    ]
    linux_full: Optional[Path] = None
    for c in candidates:
        if c.is_file():
            linux_full = c
            break
    if linux_full is None:
        raise FileNotFoundError(
            f"Linux source not found via candidates: "
            + ", ".join(str(c) for c in candidates)
        )
    linux_source = linux_full.read_text(encoding="utf-8", errors="replace")

    target_stage = _stage_from_spec_path_safe(spec_path)
    dag_state = dag_module.load(sess.module)
    node_id = _stage_id(target_stage)
    inv = dag_module.collect_invariants(dag_state, node_id)

    prompt_text = prompts.assemble_linux_compare_prompt(
        module=sess.module,
        stage=target_stage,
        linux_path=str(linux_full),
        linux_source=linux_source,
        spec_path=spec_path,
        generated_spec=generated_spec,
        code_path=code_path,
        generated_code=generated_code,
        common_header=_common_header(sess.module),
        inherited_invariants=inv,
    )
    sess.last_prompt = prompt_text

    return {
        "prompt_for_llm": prompt_text,
        "linux_path": str(linux_full),
        "spec_path": spec_path,
        "spec_source": spec_source,
        "code_path": code_path,
        "code_source": code_source,
        "stage": target_stage,
        "linux_source_size": len(linux_source),
        "spec_size": len(generated_spec),
        "code_size": len(generated_code),
    }


def _format_gaps_md(items: list[dict[str, Any]], side: str) -> str:
    """Render a list of spec_gaps / code_gaps as a markdown bullet list."""
    if not items:
        return f"- (no {side} gaps)\n"
    lines: list[str] = []
    for g in items:
        sev = str(g.get("severity", "?")).lower()
        cat = str(g.get("category", "?"))
        desc = str(g.get("description", "")).strip()
        ev = str(g.get("linux_evidence", "")).strip()
        loc_key = "spec_location" if side == "spec" else "code_location"
        loc = str(g.get(loc_key, "")).strip()
        rc = str(g.get("root_cause", "")).strip() if side == "code" else ""
        fix = str(g.get("fix_suggestion", "")).strip()
        lines.append(f"- **[{sev}/{cat}]** {desc}")
        if ev:
            lines.append(f"  - Linux: `{ev}`")
        if loc:
            lines.append(f"  - {loc_key.replace('_', ' ').title()}: `{loc}`")
        if rc:
            lines.append(f"  - Root cause: `{rc}`")
        if fix:
            lines.append(f"  - Fix: {fix}")
    return "\n".join(lines) + "\n"


def _format_recs_md(items: list[Any]) -> str:
    if not items:
        return "- (none)\n"
    out: list[str] = []
    for r in items:
        out.append(f"- {str(r).strip()}")
    return "\n".join(out) + "\n"


@mcp.tool()
def linux_compare_submit(
    session_id: str,
    comparison_json: str,
) -> dict[str, Any]:
    """Loop C submit — parse the LLM's comparison JSON and persist findings.

    The optimisation TARGET of Loop C is the prompt templates themselves
    (`prompts/linux_to_spec.md`, `prompts/codegen.md`, on-demand fragments) —
    NOT the current stage's spec or generated code. Because of that, this
    tool DOES NOT inject anything into the current session's refine path
    (no FailureRecord, no spec_fine_seed). Findings are accumulated in
    `docs/<module>_prompt_feedback.md` across many stages so a later
    `prompt_optimize_propose` call can roll them up into a concrete
    prompt-template edit proposal.

    Side effects:
      1. Validates the JSON shape (top-level keys + gap arrays).
      2. Appends a stage section to docs/<module>_prompt_feedback.md with
         spec_gaps, code_gaps, and the additive prompt-tuning
         recommendations. The doc is HITL-curated; nothing auto-rewrites
         prompts here.

    Args:
        session_id: session created by linux_compare_start.
        comparison_json: raw JSON body the LLM produced from the
            linux_compare prompt.

    Returns:
        {"feedback_doc_path": <repo-rel path>,
         "n_spec_gaps": N, "n_code_gaps": N,
         "n_high_severity": N,
         "n_spec_recommendations": N,
         "n_codegen_recommendations": N,
         "is_equivalent": bool,
         "next_step": <str — guidance for caller>}
    """
    sess = _get(session_id)

    # Tolerate ```json fences if the LLM wrapped its reply
    raw = comparison_json.strip()
    if raw.startswith("```"):
        # Strip first fence + optional language tag, last fence
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        report = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"comparison_json is not valid JSON: {e}. Expected the JSON "
            f"schema documented in prompts/linux_compare.md."
        )

    if not isinstance(report, dict):
        raise ValueError("comparison_json must be a JSON object at the top level.")

    spec_gaps = report.get("spec_gaps") or []
    code_gaps = report.get("code_gaps") or []
    if not isinstance(spec_gaps, list) or not isinstance(code_gaps, list):
        raise ValueError("spec_gaps and code_gaps must be JSON arrays.")

    spec_recs = report.get("spec_prompt_recommendations") or []
    code_recs = report.get("codegen_prompt_recommendations") or []
    is_equiv = bool(report.get("is_equivalent", False))
    summary = str(report.get("summary", "")).strip()
    stage = str(report.get("stage") or _stage_from_spec_path_safe(
        sess.spec_final_path or sess.code_spec_path or ""
    ))

    # 1. Append section to docs/<module>_prompt_feedback.md
    doc_path = _feedback_doc_path(sess.module)
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not doc_path.exists()

    section: list[str] = []
    if is_new:
        section.append(f"# {sess.module} — Prompt Feedback Log\n")
        section.append(
            "Linux↔generated functional-comparison findings, captured by "
            "Loop C (`linux_compare_submit`). This file is HITL-curated — "
            "the plugin never auto-rewrites prompt templates from these "
            "recommendations. Promote useful items into "
            "`prompts/linux_to_spec.md` or `prompts/codegen.md` (or their "
            "fragments) by hand after review.\n"
        )
    section.append(f"\n## {stage} — {_now_iso()}\n")
    section.append(f"- **is_equivalent**: `{is_equiv}`")
    section.append(f"- **summary**: {summary or '(none)'}")
    section.append(f"- **counts**: spec_gaps={len(spec_gaps)}, code_gaps={len(code_gaps)}\n")

    section.append("### spec_gaps")
    section.append(_format_gaps_md(list(spec_gaps), "spec"))
    section.append("### code_gaps")
    section.append(_format_gaps_md(list(code_gaps), "code"))
    section.append("### spec_prompt_recommendations (additive)")
    section.append(_format_recs_md(list(spec_recs)))
    section.append("### codegen_prompt_recommendations (additive)")
    section.append(_format_recs_md(list(code_recs)))

    block = "\n".join(section)
    with doc_path.open("a", encoding="utf-8") as f:
        f.write(block)

    n_high = sum(
        1 for g in (list(spec_gaps) + list(code_gaps))
        if str(g.get("severity", "")).lower() == "high"
    )

    rel_doc = doc_path.relative_to(_repo_root())
    has_recs = bool(spec_recs) or bool(code_recs)
    if is_equiv and not spec_gaps and not code_gaps and not has_recs:
        next_step = "ok — no gaps; no prompt-template edits proposed."
    elif has_recs:
        next_step = (
            f"Recommendations recorded in {rel_doc}. After accumulating "
            "across multiple stages, call `prompt_optimize_propose` to roll "
            "them up into a concrete prompt-template edit."
        )
    else:
        next_step = (
            f"Gaps recorded in {rel_doc} but no prompt-template "
            "recommendations were produced — gaps may be stage-local."
        )

    # Touch the session so later metrics tie back; we don't mutate failures.
    _ = sess

    return {
        "feedback_doc_path": str(rel_doc),
        "n_spec_gaps": len(spec_gaps),
        "n_code_gaps": len(code_gaps),
        "n_high_severity": n_high,
        "n_spec_recommendations": len(spec_recs),
        "n_codegen_recommendations": len(code_recs),
        "is_equivalent": is_equiv,
        "next_step": next_step,
    }


# ---- Loop C — prompt-template optimisation (P1.6 Wave 2) -------------------
# Targets prompt files in prompts/, NOT the current-stage spec/code. Reads
# accumulated recommendations from docs/<m>_prompt_feedback.md and asks the
# LLM to produce a revised TARGET prompt template; the apply tool backs up
# the current template and writes the new text. HITL is the diff review.

# Whitelist: only these prompt templates are user-facing optimization
# targets. Internal-only templates (linux_compare itself, prompt_optimize
# itself, validation_checklist) are NOT optimised this way — they are the
# meta-layer.
_OPTIMIZABLE_PROMPTS = {
    "linux_to_spec",   # Loop A spec-extraction prompt
    "codegen",         # Loop B code-gen prompt
    "speceval",        # Layer 3 SpecEval
    "spec_fine",       # F3 SpecFine
    "style_audit",     # Layer 1b style audit
    "two_phase_rules", # on-demand fragment
    "linux_to_liteos_table",  # on-demand fragment
    "format_traps",    # on-demand fragment
    "ask_first_rules", # on-demand fragment
    "style_rules",     # on-demand fragment
    "liteos_digest",   # always-on digest
}

# Map a target prompt to which recommendation type from the feedback doc
# applies to it. spec-side recommendations belong to spec-flavoured prompts;
# codegen-side belong to code-flavoured prompts; some prompts are dual.
_TARGET_REC_TYPE: dict[str, str] = {
    "linux_to_spec":          "spec",
    "spec_fine":              "spec",
    "speceval":               "spec",     # validates spec↔code, edits push it spec-side
    "two_phase_rules":        "spec",
    "ask_first_rules":        "spec",
    "codegen":                "codegen",
    "style_audit":            "codegen",
    "linux_to_liteos_table":  "codegen",
    "format_traps":           "codegen",
    "style_rules":            "codegen",
    "liteos_digest":          "codegen",
}


def _parse_feedback_recommendations(
    doc_text: str, rec_type: str
) -> tuple[list[str], int]:
    """Extract bullet lines under ### {rec_type}_prompt_recommendations
    headings from a markdown feedback log. Returns (lines, n_stages).

    n_stages = number of "## <stage> — <ts>" headings present (regardless
    of whether each had a non-empty recommendation list).
    """
    if not doc_text:
        return [], 0
    target_heading = (
        "### spec_prompt_recommendations"
        if rec_type == "spec"
        else "### codegen_prompt_recommendations"
    )
    n_stages = sum(
        1 for line in doc_text.splitlines()
        if line.startswith("## ") and " — " in line
    )

    out: list[str] = []
    in_block = False
    for raw_line in doc_text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("### "):
            in_block = (line.startswith(target_heading))
            continue
        if line.startswith("## ") or line.startswith("# "):
            in_block = False
            continue
        if in_block:
            stripped = line.lstrip()
            if stripped.startswith("- ") and "(none)" not in stripped:
                out.append(stripped[2:].strip())
    return out, n_stages


@mcp.tool()
def prompt_optimize_propose(
    target_prompt_name: str,
    module: str,
) -> dict[str, Any]:
    """Loop C — assemble the meta-prompt that asks an LLM to produce a
    revised version of `prompts/<target_prompt_name>.md`, integrating
    accumulated Loop C recommendations from
    `docs/<module>_prompt_feedback.md`.

    The apply step is a separate `prompt_optimize_apply` tool — this tool
    only PRODUCES the meta-prompt for the LLM round.

    Args:
        target_prompt_name: name without .md, e.g. "linux_to_spec",
            "codegen", "two_phase_rules". Must be in the
            _OPTIMIZABLE_PROMPTS whitelist; the linux_compare meta-prompt
            and prompt_optimize itself are not optimisable this way.
        module: which module's feedback log to roll up (e.g. "exfat").

    Returns:
        {prompt_for_llm, target_prompt_path, current_prompt_size,
         n_recommendations, n_stages, rec_type}
    """
    if target_prompt_name not in _OPTIMIZABLE_PROMPTS:
        raise ValueError(
            f"target_prompt_name {target_prompt_name!r} is not in the "
            f"optimisable whitelist. Valid targets: "
            + ", ".join(sorted(_OPTIMIZABLE_PROMPTS))
        )

    target_path = prompts.PROMPTS_DIR / f"{target_prompt_name}.md"
    if not target_path.is_file():
        raise FileNotFoundError(
            f"Target prompt template not found: {target_path}"
        )
    current_prompt = target_path.read_text(encoding="utf-8")

    doc_path = _feedback_doc_path(module)
    doc_text = doc_path.read_text(encoding="utf-8") if doc_path.is_file() else ""

    rec_type = _TARGET_REC_TYPE.get(target_prompt_name, "spec")
    rec_lines, n_stages = _parse_feedback_recommendations(doc_text, rec_type)

    if not rec_lines:
        return {
            "prompt_for_llm": "",
            "target_prompt_path": str(
                target_path.relative_to(_repo_root())
                if target_path.is_absolute() and _repo_root() in target_path.parents
                else target_path
            ),
            "current_prompt_size": len(current_prompt),
            "n_recommendations": 0,
            "n_stages": n_stages,
            "rec_type": rec_type,
            "skipped_reason": (
                f"No {rec_type}-side recommendations accumulated for module "
                f"{module!r} yet — run linux_compare on at least one stage "
                "and have its LLM produce non-empty "
                f"{rec_type}_prompt_recommendations first."
            ),
        }

    # Dedup recommendations while preserving order (LLM does final dedup but
    # avoid feeding it 10 copies of the same suggestion when many stages
    # noticed the same gap).
    seen: set[str] = set()
    deduped: list[str] = []
    for line in rec_lines:
        key = line.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)

    rec_block = "\n".join(f"- {r}" for r in deduped)
    prompt_text = prompts.assemble_prompt_optimize_prompt(
        target_prompt_name=target_prompt_name,
        module=module,
        rec_type=rec_type,
        n_stages=n_stages,
        current_prompt=current_prompt,
        recommendations=rec_block,
    )

    return {
        "prompt_for_llm": prompt_text,
        "target_prompt_path": str(target_path),
        "current_prompt_size": len(current_prompt),
        "n_recommendations": len(deduped),
        "n_stages": n_stages,
        "rec_type": rec_type,
    }


@mcp.tool()
def prompt_optimize_apply(
    target_prompt_name: str,
    new_prompt_text: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Loop C — apply a revised prompt template after HITL review.

    Side effects when dry_run=False:
      1. Backs up the current `prompts/<target>.md` to
         `prompts/<target>.md.bak.<unix_ts>`.
      2. Overwrites the prompt template with `new_prompt_text`.
      3. Invalidates the in-process template cache so the next assemble_*
         call picks up the new content.

    When dry_run=True, no files are modified — only the unified diff is
    returned for inspection.

    Returns:
        {applied: bool, target_prompt_path, backup_path, diff,
         old_size, new_size, sanity_warnings}

    Sanity warnings (non-fatal — surfaced for HITL):
      - new_size shrank by >20% (likely truncation accident).
      - new_size grew by >50% (likely verbatim recommendation copy).
      - The leading `<!-- ... -->` developer-comment block is missing
        from the new text.
    """
    import difflib

    if target_prompt_name not in _OPTIMIZABLE_PROMPTS:
        raise ValueError(
            f"target_prompt_name {target_prompt_name!r} is not in the "
            f"optimisable whitelist. Valid targets: "
            + ", ".join(sorted(_OPTIMIZABLE_PROMPTS))
        )

    target_path = prompts.PROMPTS_DIR / f"{target_prompt_name}.md"
    if not target_path.is_file():
        raise FileNotFoundError(
            f"Target prompt template not found: {target_path}"
        )
    current = target_path.read_text(encoding="utf-8")
    new_text = new_prompt_text.rstrip() + "\n"

    diff_lines = list(difflib.unified_diff(
        current.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"prompts/{target_prompt_name}.md (current)",
        tofile=f"prompts/{target_prompt_name}.md (proposed)",
        n=3,
    ))
    diff_text = "".join(diff_lines)

    warnings: list[str] = []
    old_size = len(current)
    new_size = len(new_text)
    if new_size < old_size * 0.8:
        warnings.append(
            f"new_size shrank by >20% ({old_size} → {new_size}). "
            "Loop C optimisation should be additive — verify nothing was "
            "accidentally deleted."
        )
    if new_size > old_size * 1.5:
        warnings.append(
            f"new_size grew by >50% ({old_size} → {new_size}). "
            "Likely verbatim recommendation copy — manual trim may be needed."
        )
    if not new_text.lstrip().startswith("<!--"):
        warnings.append(
            "New text does not start with the `<!-- developer comment -->` "
            "block. Convention is to retain & extend that block with a "
            "dated history entry. Manual fix recommended before applying."
        )

    if dry_run:
        return {
            "applied": False,
            "target_prompt_path": str(target_path),
            "backup_path": "",
            "diff": diff_text,
            "old_size": old_size,
            "new_size": new_size,
            "sanity_warnings": warnings,
        }

    ts = int(time.time())
    backup_path = target_path.with_suffix(target_path.suffix + f".bak.{ts}")
    backup_path.write_text(current, encoding="utf-8")
    target_path.write_text(new_text, encoding="utf-8")

    # Invalidate the in-process template cache so subsequent assemble_*
    # calls pick up the new content. (prompts._TEMPLATE_CACHE is a module
    # singleton; pop the stale entry rather than clearing all.)
    prompts._TEMPLATE_CACHE.pop(target_prompt_name, None)

    return {
        "applied": True,
        "target_prompt_path": str(target_path),
        "backup_path": str(backup_path),
        "diff": diff_text,
        "old_size": old_size,
        "new_size": new_size,
        "sanity_warnings": warnings,
    }


@mcp.tool()
def prompt_feedback_summary(module: str) -> dict[str, Any]:
    """Read docs/<module>_prompt_feedback.md and return a summary.

    Used by HITL prompt-evolution sessions to see which Loop C recommendations
    have accumulated across stages without re-reading the whole markdown.
    Returns counts and the raw markdown content (capped to 30 KB).
    """
    p = _feedback_doc_path(module)
    if not p.is_file():
        return {
            "exists": False,
            "path": str(p.relative_to(_repo_root()) if p.is_absolute() else p),
            "stages": 0,
            "content": "",
        }
    text = p.read_text(encoding="utf-8")
    n_stages = text.count("\n## ")
    truncated = text[:30_000]
    if len(text) > 30_000:
        truncated += f"\n\n[... truncated, {len(text) - 30_000} chars more]"
    return {
        "exists": True,
        "path": str(p.relative_to(_repo_root())),
        "stages": n_stages,
        "size_chars": len(text),
        "content": truncated,
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


def _latest_round_failures(failures: list[state.FailureRecord]) -> list[state.FailureRecord]:
    """Paper-aligned (gencode.py:180): only the LATEST retry-round's
    failures get re-injected as [Modification suggestions]. Prior rounds'
    suggestions were already addressed by intervening regenerations and
    should not contaminate the next prompt — keeping them caused
    monotonic prompt growth and stale-hint contamination across long
    iteration sessions.
    """
    if not failures:
        return []
    max_round = max(f.round_idx for f in failures)
    return [f for f in failures if f.round_idx == max_round]


def _rebuild_codegen_prompt(sess: state.Session) -> dict[str, str]:
    """Re-assemble codegen prompt for the next round, including all accumulated failures."""
    spec_p = _repo_root() / sess.code_spec_path
    spec_content = spec_p.read_text(encoding="utf-8")
    target_stage = _stage_from_spec_path(sess.code_spec_path)
    dag_state = dag_module.load(sess.module)
    node_id = _stage_id(target_stage)
    inv = dag_module.collect_invariants(dag_state, node_id)
    ifaces = extract.extract_module_interface(sess.module, _repo_root())

    full_header = _common_header(sess.module)
    keep = prompts.collect_codegen_keep_symbols(spec_content)
    filtered_header = (
        prompts.filter_common_header_by_symbols(full_header, keep) if keep else full_header
    )
    prior_iface_text = extract.render_interface_summary(ifaces, keep_symbols=keep or None)

    latest_failures = _latest_round_failures(sess.failures)
    prompt_text = prompts.assemble_codegen_prompt(
        module=sess.module,
        spec_content=spec_content,
        common_header=filtered_header,
        inherited_invariants=inv,
        prior_code_interface=prior_iface_text,
        previous_code=sess.current_artifact,
        failures=[
            prompts.FailureNote(layer=f.layer, payload=f.payload)
            for f in latest_failures
        ],
    )
    sess.last_prompt = prompt_text
    return {"next_prompt": prompt_text}


_EXTERN_FN_NAME_RE = re.compile(
    r"^\s*extern\s+.+?\b([A-Za-z_]\w*)\s*\(", re.MULTILINE
)


def _existing_extern_fn_names(text: str) -> set[str]:
    """Return set of function names already declared via `extern ... fn(` in text.

    Used to dedupe by SYMBOL NAME rather than by exact line match — pre-fix
    behavior compared raw decl strings, so `int  fn(...)` (double-space) and
    `int fn(...)` (single-space) were treated as distinct and both appended,
    producing 42 dup function-name lines in the exFAT common.header.
    """
    return set(_EXTERN_FN_NAME_RE.findall(text))


def _sync_common_header(module: str, ifaces: dict[str, extract.ExtractedInterface]) -> str:
    """Append new public symbols to spec/<module>/common.header. Returns diff text."""
    if not ifaces:
        return ""
    header_p = _spec_dir(module) / "common.header"
    existing = header_p.read_text(encoding="utf-8") if header_p.exists() else ""

    seen_names = _existing_extern_fn_names(existing)
    new_decls: list[str] = []
    for path, iface in sorted(ifaces.items()):
        for sig in iface.functions:
            # Extract function name from signature for dedup. Falls back to
            # raw-string compare if regex misses (e.g. function-pointer typedef).
            name_match = re.search(r"\b([A-Za-z_]\w*)\s*\(", sig)
            decl = f"extern {sig};"
            if name_match:
                fn_name = name_match.group(1)
                if fn_name in seen_names:
                    continue
                seen_names.add(fn_name)
                new_decls.append(decl)
            else:
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
def metrics_summary(
    session_id: Optional[str] = None, production_only: bool = False,
) -> dict[str, Any]:
    """Read the per-tool telemetry log and return aggregate counters.

    Args:
        session_id: filter to one session (omit for all-sessions rollup).
        production_only: drop events from pytest (PYTEST_CURRENT_TEST set).
            Use this when measuring real-user prompt sizes / churn — the
            test suite's 1099 throwaway sessions otherwise skew everything.

    Returns:
        dict with mcp_tool_calls, mcp_total_duration_s, mcp_per_tool,
        llm_rounds, llm_input_tokens_est, llm_output_tokens_est,
        llm_total_gap_s, errors, expected_rejections, unexpected_errors,
        errors_per_tool.

    Note: this tool itself emits an event before returning, so its own
    call shows up in subsequent reads. The event is tagged
    is_llm_round_trigger=False / is_llm_ingest=False, so it won't
    distort the LLM-side counters — only the mcp_per_tool count.
    """
    events = read_log()
    return metrics_aggregate(
        events, session_id=session_id, production_only=production_only,
    ).as_dict()


@mcp.tool()
def metrics_report(
    session_id: Optional[str] = None, production_only: bool = False,
) -> dict[str, Any]:
    """Render a markdown table summarising the same numbers ``metrics_summary``
    returns. Intended for the user to paste into review notes / PRs.

    Args:
        session_id: filter to one session (omit for all-sessions rollup).
        production_only: drop pytest-generated events.
    """
    events = read_log()
    agg = metrics_aggregate(
        events, session_id=session_id, production_only=production_only,
    ).as_dict()
    per_tool = agg.get("mcp_per_tool", {}) or {}
    per_tool_tok = agg.get("prompt_size_per_tool_tokens", {}) or {}

    lines = [
        f"## specfs metrics — {agg.get('session_id') or 'all sessions'}",
        "",
        "| metric | value |",
        "|---|---|",
        f"| MCP tool calls | {agg.get('mcp_tool_calls', 0)} |",
        f"| MCP total duration | {agg.get('mcp_total_duration_s', 0):.2f} s |",
        f"| LLM rounds | {agg.get('llm_rounds', 0)} |",
        f"| LLM input tokens (sent) | {agg.get('llm_input_tokens_est', 0):,} |",
        f"| LLM output tokens (received) | {agg.get('llm_output_tokens_est', 0):,} |",
        f"| LLM round-trip wall-clock | {agg.get('llm_total_gap_s', 0):.2f} s |",
        f"| prompt size — max | {agg.get('prompt_size_max_tokens', 0):,} tok |",
        f"| prompt size — avg | {agg.get('prompt_size_avg_tokens', 0):,} tok |",
        f"| errors | {agg.get('errors', 0)} |",
    ]
    if per_tool:
        lines += ["", "### per-tool calls", "", "| tool | calls | prompt-size total tokens |", "|---|---|---|"]
        for tool in sorted(per_tool):
            lines.append(f"| `{tool}` | {per_tool[tool]} | {per_tool_tok.get(tool, 0):,} |")
    return {"markdown": "\n".join(lines), "raw": agg}


# ---- Section 4.11 — Hot-reload (no OpenCode restart needed) ------------------


@mcp.tool()
def reload_plugin() -> dict[str, Any]:
    """Hot-reload the plugin's pure-Python modules WITHOUT restarting OpenCode.

    Re-imports ``state``, ``dag``, ``extract``, ``prompts``, ``_metrics``,
    ``_timeout``, and ``_persist_session`` via ``importlib.reload``. The
    ``@mcp.tool`` registrations on this file (specfs_server.py) are NOT
    rebuilt — they bind to the original function objects at module-import
    time, so any pre-existing tool keeps its old code path. New module-level
    helpers, prompt templates, and dataclass schemas DO take effect because
    they are looked up by attribute access at call time.

    Effective for: prompt template tweaks (prompts.py), telemetry math
    (_metrics.py), session schema (state.py), DAG helpers (dag.py),
    interface extractors (extract.py), middleware (_timeout.py /
    _persist_session.py).

    NOT effective for: changes to specfs_server.py itself (tool bodies,
    new @mcp.tool registrations, signature changes). For those you still
    need to restart OpenCode.

    The currently-active in-memory _SESSIONS dict is preserved across the
    reload — the dataclass schema is re-bound but instance attribute access
    keeps working as long as field names didn't change.

    Returns the list of reloaded module names + any per-module import
    errors so the caller can decide whether to retry or restart.
    """
    import importlib
    import sys as _sys

    targets = [
        "state",
        "dag",
        "extract",
        "prompts",
        "_metrics",
        "_timeout",
        "_persist_session",
    ]

    reloaded: list[str] = []
    errors: dict[str, str] = {}

    for name in targets:
        try:
            mod = _sys.modules.get(name)
            if mod is None:
                continue
            importlib.reload(mod)
            reloaded.append(name)
        except Exception as e:  # broad on purpose — per-module isolation
            errors[name] = repr(e)

    global state, dag_module, extract, prompts
    if "state" in reloaded:
        state = _sys.modules["state"]
    if "dag" in reloaded:
        dag_module = _sys.modules["dag"]
    if "extract" in reloaded:
        extract = _sys.modules["extract"]
    if "prompts" in reloaded:
        prompts = _sys.modules["prompts"]

    return {
        "reloaded": reloaded,
        "errors": errors,
        "note": (
            "Tool registrations on specfs_server.py itself are unchanged; "
            "restart OpenCode if you modified tool bodies, signatures, or "
            "added new @mcp.tool entries."
        ),
    }


# ---- Entry -------------------------------------------------------------------


if __name__ == "__main__":
    mcp.run()
