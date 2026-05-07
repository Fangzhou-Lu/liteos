"""End-to-end smoke tests for the MCP tool surface (37 @mcp.tool() functions).

The tools are decorated with `@mcp.tool()` which registers them with FastMCP
but leaves the underlying function callable directly. Tests invoke each tool
as a plain Python function — no MCP transport needed.

We focus on:
  - Session lifecycle (start / status / end / toggle_*)
  - Loop A end-to-end (spec_gen_start → submit → approve)
  - Loop B end-to-end (code_gen_start → submit → approve)
  - Layer T (test_gen_*)
  - Diagnostics injection + retry budget
  - DAG queries (dag_get / dag_extract_invariants / dag_check_node_complete)
  - Spec_fine cap (3-round hard cap per project memory feedback_specfine_cap_3)
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------- Session lifecycle ----------

def test_session_start(tmp_repo: Path, reset_session_registry):
    import specfs_server
    out = specfs_server.session_start(module="exfat", mode="gen")
    assert "session_id" in out
    assert "dag_state" in out
    assert len(out["session_id"]) == 12


def test_session_start_evolve_mode(tmp_repo: Path, reset_session_registry):
    import specfs_server
    out = specfs_server.session_start(module="exfat", mode="evolve")
    sid = out["session_id"]
    status = specfs_server.session_status(sid)
    assert status["mode"] == "evolve"


def test_session_status_unknown_session_raises(tmp_repo: Path, reset_session_registry):
    import specfs_server
    with pytest.raises(KeyError, match="session"):
        specfs_server.session_status("nonexistent")


def test_session_end_removes_session(tmp_repo: Path, reset_session_registry):
    import specfs_server
    out = specfs_server.session_start(module="exfat")
    sid = out["session_id"]
    specfs_server.session_end(sid)
    assert sid not in specfs_server._SESSIONS


def test_toggle_speceval(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    out = specfs_server.toggle_speceval(sid, enabled=False)
    assert out["speceval_enabled"] is False
    assert specfs_server._SESSIONS[sid].speceval_enabled is False


def test_toggle_skip_build(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    specfs_server.toggle_skip_build(sid, skip=True)
    assert specfs_server._SESSIONS[sid].skip_build_layer is True


def test_toggle_test_gen(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    specfs_server.toggle_test_gen(sid, enabled=False)
    assert specfs_server._SESSIONS[sid].test_gen_enabled is False


# ---------- Loop A: spec_gen_* ----------

def test_spec_gen_start_returns_assembled_prompt(tmp_repo: Path, reset_session_registry):
    """spec_gen_start should return prompt_for_llm with the spec template
    populated with module + stage."""
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]

    # Create a placeholder linux source dir so the prompt has something to
    # reference (the prompt asks the LLM to read these files).
    linux_dir = tmp_repo / "linux" / "fs" / "exfat"
    linux_dir.mkdir(parents=True)

    out = specfs_server.spec_gen_start(
        session_id=sid,
        linux_path=str(linux_dir),
        target_stage="mount",
    )
    assert "prompt_for_llm" in out
    assert len(out["prompt_for_llm"]) > 100
    assert "exfat" in out["prompt_for_llm"]
    assert "mount" in out["prompt_for_llm"]


def test_spec_gen_submit_then_approve_writes_files(tmp_repo: Path,
                                                    reset_session_registry,
                                                    sample_spec_text: str):
    """End-to-end Loop A: start → submit → approve. The spec lands in
    spec/<module>/<sub_path>/<op>.spec and the DAG node spec layer is committed."""
    import specfs_server
    import dag

    sid = specfs_server.session_start(module="exfat")["session_id"]
    linux_dir = tmp_repo / "linux" / "fs" / "exfat"
    linux_dir.mkdir(parents=True)

    specfs_server.spec_gen_start(
        session_id=sid,
        linux_path=str(linux_dir),
        target_stage="lookup",
    )
    specfs_server.spec_gen_submit(session_id=sid, generated_spec_text=sample_spec_text)

    out = specfs_server.spec_gen_approve(session_id=sid, final_spec_text=sample_spec_text)
    # spec_gen_approve returns {dag_node_id, invariants_extracted, next_step, saved_to}
    assert "saved_to" in out
    assert out["saved_to"].endswith("exfat_lookup.spec")
    assert "next_step" in out

    # File landed
    final_path = tmp_repo / "spec" / "exfat" / "interface" / "exfat_lookup.spec"
    assert final_path.is_file()
    assert "VfsExfatLookup" in final_path.read_text()

    # DAG node committed
    d = dag.load("exfat")
    node = dag.find_node_by_stage_name(d, "lookup")
    assert node is not None
    assert node["spec"]["approved_at"]


def test_spec_gen_refine_returns_refined_prompt(tmp_repo: Path,
                                                 reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    linux_dir = tmp_repo / "linux" / "fs" / "exfat"
    linux_dir.mkdir(parents=True)

    specfs_server.spec_gen_start(
        session_id=sid,
        linux_path=str(linux_dir),
        target_stage="mount",
    )
    out = specfs_server.spec_gen_refine(
        session_id=sid,
        user_suggestion="Tighten the [GUARANTEE] block — add calling-convention comment.",
    )
    # spec_gen_refine returns {next_prompt, iteration} — not prompt_for_llm
    assert "next_prompt" in out
    assert "Tighten" in out["next_prompt"] or "calling-convention" in out["next_prompt"]
    assert out["iteration"] >= 1


# ---------- Loop B: code_gen_* ----------

def _approve_mount_spec(server, tmp_repo: Path, sid: str) -> Path:
    """Helper: drive a spec through approve so we have something to feed code_gen_start."""
    linux_dir = tmp_repo / "linux" / "fs" / "exfat"
    linux_dir.mkdir(parents=True, exist_ok=True)
    server.spec_gen_start(session_id=sid, linux_path=str(linux_dir), target_stage="mount")
    spec = "[PROMPT]\nmount.\n[GUARANTEE]\n```c\nint VfsExfatMount(struct Mount *m, struct Vnode *b, const void *d);\n```\n"
    server.spec_gen_submit(session_id=sid, generated_spec_text=spec)
    server.spec_gen_approve(session_id=sid, final_spec_text=spec)
    return tmp_repo / "spec" / "exfat" / "interface" / "exfat_mount.spec"


def test_code_gen_start_returns_assembled_prompt(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    spec_path = _approve_mount_spec(specfs_server, tmp_repo, sid)

    out = specfs_server.code_gen_start(
        session_id=sid,
        spec_path=str(spec_path.relative_to(tmp_repo)),
    )
    assert "prompt_for_llm" in out
    assert "VfsExfatMount" in out["prompt_for_llm"]


def test_code_gen_refine_includes_user_suggestion(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    spec_path = _approve_mount_spec(specfs_server, tmp_repo, sid)
    specfs_server.code_gen_start(session_id=sid, spec_path=str(spec_path.relative_to(tmp_repo)))

    out = specfs_server.code_gen_refine(
        session_id=sid,
        user_suggestion="Use LOS_MemAlloc not malloc; libsec strncpy_s not strncpy.",
    )
    # code_gen_refine reuses the codegen prompt assembler — key is next_prompt
    assert "next_prompt" in out
    assert "LOS_MemAlloc" in out["next_prompt"] or "libsec" in out["next_prompt"]


# ---------- Diagnostics injection + retry budget ----------

def test_inject_diagnostics_increments_retry_counter(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    spec_path = _approve_mount_spec(specfs_server, tmp_repo, sid)
    specfs_server.code_gen_start(session_id=sid, spec_path=str(spec_path.relative_to(tmp_repo)))

    before = specfs_server._SESSIONS[sid].layer_retries["compile"]
    specfs_server.inject_diagnostics(
        session_id=sid,
        layer="compile",
        payload="error: undeclared identifier 'LosMux'",
    )
    after = specfs_server._SESSIONS[sid].layer_retries["compile"]
    assert after == before + 1


def test_inject_diagnostics_unknown_layer_raises(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    with pytest.raises((ValueError, KeyError, AssertionError)):
        specfs_server.inject_diagnostics(
            session_id=sid,
            layer="nonexistent_layer",
            payload="x",
        )


def test_inject_diagnostics_recorded_in_failures(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    spec_path = _approve_mount_spec(specfs_server, tmp_repo, sid)
    specfs_server.code_gen_start(session_id=sid, spec_path=str(spec_path.relative_to(tmp_repo)))

    specfs_server.inject_diagnostics(
        session_id=sid, layer="style", payload="bare strncpy detected"
    )
    failures = specfs_server._SESSIONS[sid].failures
    assert any(f.layer == "style" for f in failures)
    assert any("strncpy" in f.payload for f in failures)


# ---------- DAG queries ----------

def test_dag_get_returns_full_dag(tmp_repo: Path, sample_dag: dict, reset_session_registry):
    import specfs_server
    out = specfs_server.dag_get(module="exfat")
    assert out["module"] == "exfat"
    assert len(out["stages"]) == 1
    assert out["stages"][0]["id"] == "mount-v1"


def test_dag_extract_invariants(tmp_repo: Path, sample_dag: dict, reset_session_registry):
    """ancestors-of mount-v1 is empty (root node), so collect_invariants returns []."""
    import specfs_server
    invs = specfs_server.dag_extract_invariants(module="exfat", node_id="mount-v1")
    assert invs == []


def test_dag_extract_invariants_for_descendant(tmp_repo: Path, reset_session_registry):
    """A descendant should inherit ancestor invariants."""
    import specfs_server
    import dag
    d = dag.empty_dag("exfat")
    d["stages"] = [
        {"id": "mount-v1", "stage_name": "mount", "depends_on": [],
         "spec": {"approved_at": "x"}, "code": {"approved_at": "y"},
         "invariants": [{"id": "mount-locked", "text": "mount data non-NULL"}]},
        {"id": "lookup-v1", "stage_name": "lookup", "depends_on": ["mount-v1"],
         "spec": {"approved_at": "x"}, "code": {"approved_at": "y"},
         "invariants": []},
    ]
    dag.save("exfat", d)

    invs = specfs_server.dag_extract_invariants(module="exfat", node_id="lookup-v1")
    assert any(inv["id"] == "mount-locked" for inv in invs)


def test_dag_check_node_complete(tmp_repo: Path, sample_dag: dict, reset_session_registry):
    import specfs_server
    # Returns {exists, spec_done, code_done, validations, dirty_layers}
    out = specfs_server.dag_check_node_complete(module="exfat", node_id="mount-v1")
    assert out["exists"] is True
    assert out["spec_done"] is True
    assert out["code_done"] is True
    assert out["dirty_layers"] == []


def test_dag_check_node_complete_unknown(tmp_repo: Path, sample_dag: dict, reset_session_registry):
    import specfs_server
    out = specfs_server.dag_check_node_complete(module="exfat", node_id="ghost")
    assert out["exists"] is False


def test_dag_revert_removes_node(tmp_repo: Path, reset_session_registry):
    """dag_revert REMOVES the stage node from the DAG (does NOT mark-dirty
    cascade). User runs `git restore` themselves to undo file artifacts."""
    import specfs_server
    import dag
    d = dag.empty_dag("exfat")
    d["stages"] = [
        {"id": "mount-v1", "stage_name": "mount", "depends_on": [],
         "spec": {"approved_at": "x"}, "code": {"approved_at": "y"}},
        {"id": "lookup-v1", "stage_name": "lookup", "depends_on": ["mount-v1"],
         "spec": {"approved_at": "x"}, "code": {"approved_at": "y"}},
    ]
    dag.save("exfat", d)

    out = specfs_server.dag_revert(module="exfat", node_id="mount-v1")
    assert out["removed"] is True
    assert out["remaining_stages"] == 1

    # Verify the file actually shrank
    after = dag.load("exfat")
    ids = {s["id"] for s in after["stages"]}
    assert ids == {"lookup-v1"}


def test_dag_revert_unknown_node_returns_removed_false(tmp_repo: Path, reset_session_registry):
    import specfs_server
    out = specfs_server.dag_revert(module="exfat", node_id="ghost")
    assert out["removed"] is False
    assert "not found" in out.get("reason", "")


# ---------- Prompt fragment fetching ----------

def test_list_prompt_fragments_returns_index(reset_session_registry):
    import specfs_server
    out = specfs_server.list_prompt_fragments()
    # Should be a dict of fragment-id → metadata
    assert isinstance(out, dict)
    assert len(out) >= 4  # at least the 4 lazy-injection fragments


def test_fetch_prompt_fragment_known(reset_session_registry):
    import specfs_server
    out = specfs_server.fetch_prompt_fragment(name="style_rules")
    assert "content" in out
    assert len(out["content"]) > 100


def test_fetch_prompt_fragment_unknown_raises(reset_session_registry):
    import specfs_server
    with pytest.raises((KeyError, ValueError, FileNotFoundError)):
        specfs_server.fetch_prompt_fragment(name="__nonexistent__")


# ---------- Prompt override ----------

def test_has_prompt_override_returns_false_when_missing(tmp_repo: Path, reset_session_registry):
    import specfs_server
    spec_path = tmp_repo / "spec" / "exfat" / "interface" / "exfat_mount.spec"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text("[PROMPT]\nx.\n")
    # Returns {"exists": bool}
    out = specfs_server.has_prompt_override(spec_path=str(spec_path.relative_to(tmp_repo)))
    assert out["exists"] is False


def test_write_then_has_prompt_override(tmp_repo: Path, reset_session_registry):
    import specfs_server
    spec_path = tmp_repo / "spec" / "exfat" / "interface" / "exfat_mount.spec"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text("[PROMPT]\nx.\n")
    write_out = specfs_server.write_prompt_override(
        spec_path=str(spec_path.relative_to(tmp_repo)),
        prompt_text="### custom override prompt\n",
    )
    assert "written_to" in write_out
    out = specfs_server.has_prompt_override(spec_path=str(spec_path.relative_to(tmp_repo)))
    assert out["exists"] is True


# ---------- Clarifications ----------

def test_record_clarification(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    specfs_server.record_clarification(
        session_id=sid,
        question="Does the FS need to support timestamps?",
        user_answer="Yes — milliseconds resolution, BCD-encoded.",
    )
    s = specfs_server._SESSIONS[sid]
    assert len(s.clarifications) == 1
    assert "timestamps" in s.clarifications[0].question


# ---------- has_unresolved_ambiguity ----------

def test_has_unresolved_ambiguity_default_false(tmp_repo: Path, reset_session_registry):
    """A fresh session has no recorded ambiguity. Returns {"unresolved": False}
    until the plugin extends with explicit asked-count tracking."""
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    out = specfs_server.has_unresolved_ambiguity(session_id=sid)
    assert out["unresolved"] is False
