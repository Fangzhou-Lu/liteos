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


def test_session_persisted_to_disk(tmp_repo: Path, reset_session_registry):
    import specfs_server
    out = specfs_server.session_start(module="exfat")
    sid = out["session_id"]
    p = tmp_repo / ".specfs" / "sessions" / f"{sid}.json"
    assert p.exists(), "session_start must mirror to disk"


def test_session_rehydrates_after_in_memory_drop(tmp_repo: Path, reset_session_registry):
    import specfs_server
    out = specfs_server.session_start(module="exfat")
    sid = out["session_id"]
    specfs_server._SESSIONS.pop(sid)
    status = specfs_server.session_status(sid)
    assert status["module"] == "exfat"


def test_session_end_unlinks_disk_file(tmp_repo: Path, reset_session_registry):
    import specfs_server
    out = specfs_server.session_start(module="exfat")
    sid = out["session_id"]
    p = tmp_repo / ".specfs" / "sessions" / f"{sid}.json"
    assert p.exists()
    specfs_server.session_end(sid)
    assert not p.exists(), "session_end must unlink disk mirror"


def test_reload_plugin_returns_module_list(reset_session_registry):
    import specfs_server
    out = specfs_server.reload_plugin()
    assert "reloaded" in out
    assert "errors" in out
    assert isinstance(out["reloaded"], list)
    assert "prompts" in out["reloaded"] or "state" in out["reloaded"]


def test_reload_plugin_preserves_active_sessions(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    specfs_server.reload_plugin()
    status = specfs_server.session_status(sid)
    assert status["module"] == "exfat"


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


# ---------- Loop C — linux_compare + prompt_optimize (P1.6 Wave 2) ----------

def _setup_compare_session(server, tmp_repo: Path) -> tuple[str, Path, Path]:
    """Drive a session through approve and write a fake generated code file.
    Returns (session_id, linux_source_path, code_path)."""
    sid = server.session_start(module="exfat")["session_id"]
    spec_path = _approve_mount_spec(server, tmp_repo, sid)

    # Fake Linux source — only needs to exist for linux_compare_start to read it.
    linux_src = tmp_repo / "linux" / "fs" / "exfat" / "namei.c"
    linux_src.parent.mkdir(parents=True, exist_ok=True)
    linux_src.write_text(
        "int exfat_unlink(struct inode *dir, struct dentry *d) {\n"
        "    /* Linux unlink: tombstone + free clusters */\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )

    # Fake generated code that linux_compare_start expects on disk
    code_rel = "fs/exfat/exfat_super.c"
    code_path = tmp_repo / code_rel
    code_path.parent.mkdir(parents=True, exist_ok=True)
    code_path.write_text(
        "int VfsExfatMount(struct Mount *m, struct Vnode *b, const void *d) {\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    # Pin the session to this generated file
    sess = server._SESSIONS[sid]
    sess.code_final_paths = [code_rel]
    sess.spec_final_path = str(spec_path.relative_to(tmp_repo))
    return sid, linux_src, code_path


def test_linux_compare_start_assembles_prompt(tmp_repo: Path, reset_session_registry):
    import specfs_server
    sid, linux_src, _ = _setup_compare_session(specfs_server, tmp_repo)
    out = specfs_server.linux_compare_start(
        session_id=sid,
        linux_source_path=str(linux_src),
    )
    assert "prompt_for_llm" in out
    p = out["prompt_for_llm"]
    assert "exfat" in p
    assert "exfat_unlink" in p
    assert "VfsExfatMount" in p
    assert "spec_prompt_recommendations" in p


def test_first_snapshot_written_on_first_spec_submit(
    tmp_repo: Path, reset_session_registry
):
    """spec_gen_submit must write a sibling .first file for Loop C linux_compare."""
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    linux_dir = tmp_repo / "linux" / "fs" / "exfat"
    linux_dir.mkdir(parents=True, exist_ok=True)
    specfs_server.spec_gen_start(
        session_id=sid, linux_path=str(linux_dir), target_stage="mount",
    )
    sess = specfs_server._SESSIONS[sid]
    draft_p = tmp_repo / sess.spec_draft_path
    first_p = draft_p.parent / (draft_p.name + ".first")

    specfs_server.spec_gen_submit(session_id=sid, generated_spec_text="V1.\n")
    assert first_p.is_file(), f"missing .first snapshot at {first_p}"
    assert first_p.read_text(encoding="utf-8") == "V1.\n"

    # A second submit (refine) must NOT overwrite the .first snapshot.
    specfs_server.spec_gen_submit(session_id=sid, generated_spec_text="V2.\n")
    assert first_p.read_text(encoding="utf-8") == "V1.\n", (
        ".first snapshot was overwritten on second submit — Loop C signal lost"
    )
    # But the live draft does reflect V2
    assert draft_p.read_text(encoding="utf-8") == "V2.\n"


def test_linux_compare_uses_first_spec_snapshot_when_available(
    tmp_repo: Path, reset_session_registry
):
    """linux_compare_start must read the .first snapshot, not the iterated spec."""
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    linux_dir = tmp_repo / "linux" / "fs" / "exfat"
    linux_dir.mkdir(parents=True, exist_ok=True)
    specfs_server.spec_gen_start(
        session_id=sid, linux_path=str(linux_dir), target_stage="mount",
    )
    specfs_server.spec_gen_submit(
        session_id=sid,
        generated_spec_text=(
            "[PROMPT]\nFIRST_SHOT_MARKER\n[GUARANTEE]\n```c\n"
            "int VfsExfatMount(struct Mount *m, struct Vnode *b, const void *d);\n```\n"
        ),
    )
    # Approve overwrites the final path, simulating "polish" rounds before approve.
    specfs_server.spec_gen_approve(
        session_id=sid,
        final_spec_text=(
            "[PROMPT]\nPOLISHED_AFTER_REFINE\n[GUARANTEE]\n```c\n"
            "int VfsExfatMount(struct Mount *m, struct Vnode *b, const void *d);\n```\n"
        ),
    )

    # Stage a fake generated code with a .first sibling
    sess = specfs_server._SESSIONS[sid]
    code_rel = "fs/exfat/exfat_super.c"
    code_full = tmp_repo / code_rel
    code_full.parent.mkdir(parents=True, exist_ok=True)
    code_full.write_text("/* polished code */\n", encoding="utf-8")
    code_first = code_full.parent / (code_full.name + ".first")
    code_first.write_text("/* FIRST_CODE_MARKER */\n", encoding="utf-8")
    sess.code_final_paths = [code_rel]

    linux_src = linux_dir / "namei.c"
    linux_src.write_text("int exfat_unlink(void){return 0;}\n", encoding="utf-8")

    out = specfs_server.linux_compare_start(
        session_id=sid, linux_source_path=str(linux_src),
    )
    p = out["prompt_for_llm"]
    # First-shot artifacts MUST appear; polished-only content MUST NOT.
    assert "FIRST_SHOT_MARKER" in p
    assert "FIRST_CODE_MARKER" in p
    assert "POLISHED_AFTER_REFINE" not in p
    assert "polished code" not in p
    # Source provenance reported
    assert ".first snapshot" in out["spec_source"]
    assert ".first snapshot" in out["code_source"]


def test_linux_compare_falls_back_to_current_when_no_snapshot(
    tmp_repo: Path, reset_session_registry
):
    """Legacy stages without a .first sibling fall back to the current file."""
    import specfs_server
    sid, linux_src, code_path = _setup_compare_session(specfs_server, tmp_repo)
    # _setup_compare_session approves a spec via spec_gen_submit, which DOES
    # write .first. Delete the spec .first to simulate a legacy stage.
    sess = specfs_server._SESSIONS[sid]
    spec_full = tmp_repo / sess.spec_final_path
    for cand in [
        spec_full.parent / (spec_full.name + ".first"),
        (tmp_repo / sess.spec_draft_path).parent /
        ((tmp_repo / sess.spec_draft_path).name + ".first") if sess.spec_draft_path else None,
    ]:
        if cand is not None and cand.exists():
            cand.unlink()
    out = specfs_server.linux_compare_start(
        session_id=sid, linux_source_path=str(linux_src),
    )
    assert "no .first snapshot" in out["spec_source"]
    assert "no .first snapshot" in out["code_source"]


def test_linux_compare_start_missing_linux_source_raises(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    sid, _, _ = _setup_compare_session(specfs_server, tmp_repo)
    with pytest.raises(FileNotFoundError, match="Linux source"):
        specfs_server.linux_compare_start(
            session_id=sid,
            linux_source_path="/nope/no/such/file.c",
        )


def test_linux_compare_submit_writes_feedback_doc(
    tmp_repo: Path, reset_session_registry
):
    import json as _json
    import specfs_server
    sid, linux_src, _ = _setup_compare_session(specfs_server, tmp_repo)
    specfs_server.linux_compare_start(
        session_id=sid, linux_source_path=str(linux_src),
    )

    body = _json.dumps({
        "is_equivalent": False,
        "stage": "mount",
        "summary": "Code drops one error-unwind branch.",
        "spec_gaps": [{
            "category": "missing_branch", "severity": "high",
            "description": "Spec lacks the EROFS pre-condition.",
            "linux_evidence": "fs/exfat/super.c:42 - check sb_rdonly()",
            "spec_location": "[SPECIFICATION] Pre-Condition",
            "fix_suggestion": "Add Pre-Condition: parent partition not read-only.",
        }],
        "code_gaps": [{
            "category": "missing_step", "severity": "med",
            "description": "Code skips set_volume_dirty bracket.",
            "linux_evidence": "fs/exfat/super.c:120 - exfat_set_volume_dirty",
            "code_location": "VfsExfatMount:line 8",
            "root_cause": "codegen_drift",
            "fix_suggestion": "Add LITEOS_DIGEST reminder.",
        }],
        "spec_prompt_recommendations": [
            "Add to [SCOPE GUARDRAILS]: enumerate every error-unwind branch.",
        ],
        "codegen_prompt_recommendations": [
            "LITEOS_DIGEST: bracket all mutating phases with set_volume_dirty/clear_volume_dirty.",
        ],
    })

    failures_before = list(specfs_server._SESSIONS[sid].failures)
    out = specfs_server.linux_compare_submit(
        session_id=sid, comparison_json=body,
    )
    assert out["n_spec_gaps"] == 1
    assert out["n_code_gaps"] == 1
    assert out["n_high_severity"] == 1
    assert out["n_spec_recommendations"] == 1
    assert out["n_codegen_recommendations"] == 1
    assert out["is_equivalent"] is False

    # Doc was created with the expected sections
    doc = tmp_repo / out["feedback_doc_path"]
    assert doc.is_file()
    text = doc.read_text(encoding="utf-8")
    assert "## mount —" in text
    assert "spec_gaps" in text
    assert "Add to [SCOPE GUARDRAILS]" in text
    assert "set_volume_dirty" in text

    # CRITICAL: linux_compare_submit must NOT inject anything into the
    # session's refine path (Loop C optimises the PROMPT TEMPLATE, not the
    # current generated artifact).
    failures_after = list(specfs_server._SESSIONS[sid].failures)
    assert failures_before == failures_after, (
        "linux_compare_submit leaked into sess.failures — Loop C feedback "
        "must NOT trigger code_gen_refine"
    )


def test_linux_compare_submit_strips_json_fences(
    tmp_repo: Path, reset_session_registry
):
    """LLMs sometimes wrap JSON in ```json ... ```. The submit tool tolerates that."""
    import specfs_server
    sid, linux_src, _ = _setup_compare_session(specfs_server, tmp_repo)
    specfs_server.linux_compare_start(session_id=sid, linux_source_path=str(linux_src))

    fenced = (
        "```json\n"
        '{"is_equivalent": true, "stage": "mount", "summary": "ok",'
        ' "spec_gaps": [], "code_gaps": [],'
        ' "spec_prompt_recommendations": [],'
        ' "codegen_prompt_recommendations": []}\n'
        "```"
    )
    out = specfs_server.linux_compare_submit(session_id=sid, comparison_json=fenced)
    assert out["is_equivalent"] is True
    assert out["n_spec_gaps"] == 0


def test_linux_compare_submit_invalid_json_raises(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    sid, linux_src, _ = _setup_compare_session(specfs_server, tmp_repo)
    specfs_server.linux_compare_start(session_id=sid, linux_source_path=str(linux_src))
    with pytest.raises(ValueError, match="not valid JSON"):
        specfs_server.linux_compare_submit(
            session_id=sid, comparison_json="this is not json {",
        )


def test_prompt_optimize_propose_returns_skipped_when_no_recs(
    tmp_repo: Path, reset_session_registry
):
    """No accumulated recommendations → propose returns skipped_reason, not a prompt."""
    import specfs_server
    out = specfs_server.prompt_optimize_propose(
        target_prompt_name="linux_to_spec", module="exfat",
    )
    assert out["n_recommendations"] == 0
    assert "skipped_reason" in out
    assert out["prompt_for_llm"] == ""


def test_prompt_optimize_propose_rolls_up_recommendations(
    tmp_repo: Path, reset_session_registry, monkeypatch
):
    """Two stages each writing a spec-side recommendation get rolled up
    into one meta-prompt."""
    import specfs_server, prompts as P

    # Patch the feedback doc path so we don't pollute the real docs/
    fake_doc = tmp_repo / "docs" / "exfat_prompt_feedback.md"
    fake_doc.parent.mkdir(parents=True, exist_ok=True)
    fake_doc.write_text(
        "# exfat — Prompt Feedback Log\n\n"
        "## mount — 2026-05-08T10:00:00Z\n"
        "### spec_prompt_recommendations (additive)\n"
        "- Add reminder about ENOSPC fast path.\n"
        "### codegen_prompt_recommendations (additive)\n"
        "- (none)\n"
        "\n## lookup — 2026-05-08T11:00:00Z\n"
        "### spec_prompt_recommendations (additive)\n"
        "- Enumerate every Linux error-unwind branch.\n"
        "### codegen_prompt_recommendations (additive)\n"
        "- LITEOS_DIGEST should mention volume-dirty bracketing.\n",
        encoding="utf-8",
    )
    # The doc-path resolver builds from _repo_root() / "docs" — tmp_repo
    # already overrides repo_root via the fixture, so this just works.

    out = specfs_server.prompt_optimize_propose(
        target_prompt_name="linux_to_spec", module="exfat",
    )
    assert out["rec_type"] == "spec"
    assert out["n_recommendations"] == 2
    assert out["n_stages"] == 2
    assert "ENOSPC" in out["prompt_for_llm"]
    assert "error-unwind" in out["prompt_for_llm"]
    # codegen-side must NOT leak into a spec-side proposal
    assert "volume-dirty bracket" not in out["prompt_for_llm"]


def test_prompt_optimize_propose_unknown_target_raises(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    with pytest.raises(ValueError, match="not in the optimisable whitelist"):
        specfs_server.prompt_optimize_propose(
            target_prompt_name="not_a_real_prompt", module="exfat",
        )


def test_prompt_optimize_apply_dry_run_returns_diff_only(
    tmp_repo: Path, reset_session_registry, monkeypatch
):
    import specfs_server, prompts as P
    target = P.PROMPTS_DIR / "linux_to_spec.md"
    pre = target.read_text(encoding="utf-8")

    out = specfs_server.prompt_optimize_apply(
        target_prompt_name="linux_to_spec",
        new_prompt_text=pre + "\n\n[NEW SECTION]\nAdded by Loop C.\n",
        dry_run=True,
    )
    assert out["applied"] is False
    assert "diff" in out
    assert "[NEW SECTION]" in out["diff"]
    # Real file untouched
    assert target.read_text(encoding="utf-8") == pre


def test_prompt_optimize_apply_writes_backup(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server, prompts as P
    # Use a fragment as the target so we don't churn the main prompts.
    target = P.PROMPTS_DIR / "format_traps.md"
    pre = target.read_text(encoding="utf-8")
    new_text = pre + "\n\n## Loop C addendum\nAdded for test.\n"

    out = specfs_server.prompt_optimize_apply(
        target_prompt_name="format_traps",
        new_prompt_text=new_text,
        dry_run=False,
    )
    try:
        assert out["applied"] is True
        assert "backup_path" in out and out["backup_path"]
        backup = Path(out["backup_path"])
        assert backup.is_file()
        assert backup.read_text(encoding="utf-8") == pre
        post = target.read_text(encoding="utf-8")
        assert "Loop C addendum" in post
    finally:
        # Restore
        target.write_text(pre, encoding="utf-8")
        if "backup_path" in out and out["backup_path"]:
            try:
                Path(out["backup_path"]).unlink()
            except FileNotFoundError:
                pass


def test_prompt_optimize_apply_warns_on_shrink(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    out = specfs_server.prompt_optimize_apply(
        target_prompt_name="format_traps",
        new_prompt_text="<!-- truncated --> tiny",
        dry_run=True,
    )
    assert any("shrank" in w for w in out["sanity_warnings"])


def test_prompt_optimize_apply_warns_on_missing_comment_block(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    out = specfs_server.prompt_optimize_apply(
        target_prompt_name="format_traps",
        new_prompt_text="No leading comment block here.\n" * 200,
        dry_run=True,
    )
    assert any("developer comment" in w for w in out["sanity_warnings"])


def test_prompt_feedback_summary_exists_false_when_doc_missing(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    out = specfs_server.prompt_feedback_summary(module="exfat")
    assert out["exists"] is False
    assert out["stages"] == 0
    assert out["content"] == ""


# ---------- fast_eval mode (P1.6 Wave 2) ----------

def test_session_start_fast_eval_disables_repair_layers(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    out = specfs_server.session_start(module="exfat", mode="fast_eval")
    sid = out["session_id"]
    assert out["fast_eval_mode"] is True
    s = specfs_server._SESSIONS[sid]
    assert s.fast_eval_mode is True
    assert s.speceval_enabled is False
    assert s.style_audit_enabled is False
    assert s.test_gen_enabled is False
    assert s.skip_build_layer is True


def test_session_start_unknown_mode_raises(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    with pytest.raises(ValueError, match="mode must be one of"):
        specfs_server.session_start(module="exfat", mode="lunch")


def test_fast_eval_refuses_spec_gen_refine(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    sid = specfs_server.session_start(module="exfat", mode="fast_eval")["session_id"]
    linux_dir = tmp_repo / "linux" / "fs" / "exfat"
    linux_dir.mkdir(parents=True, exist_ok=True)
    specfs_server.spec_gen_start(
        session_id=sid, linux_path=str(linux_dir), target_stage="mount",
    )
    with pytest.raises(ValueError, match="disabled in fast_eval"):
        specfs_server.spec_gen_refine(session_id=sid, user_suggestion="tighten X")


def test_fast_eval_refuses_code_gen_refine(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    sid = specfs_server.session_start(module="exfat", mode="fast_eval")["session_id"]
    spec_path = _approve_mount_spec(specfs_server, tmp_repo, sid)
    specfs_server.code_gen_start(
        session_id=sid, spec_path=str(spec_path.relative_to(tmp_repo)),
    )
    with pytest.raises(ValueError, match="disabled in fast_eval"):
        specfs_server.code_gen_refine(
            session_id=sid, user_suggestion="use libsec",
        )


def test_fast_eval_refuses_inject_diagnostics(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    sid = specfs_server.session_start(module="exfat", mode="fast_eval")["session_id"]
    spec_path = _approve_mount_spec(specfs_server, tmp_repo, sid)
    specfs_server.code_gen_start(
        session_id=sid, spec_path=str(spec_path.relative_to(tmp_repo)),
    )
    with pytest.raises(ValueError, match="disabled in fast_eval"):
        specfs_server.inject_diagnostics(
            session_id=sid, layer="compile", payload="error: missing semicolon",
        )


def test_fast_eval_refuses_spec_fine(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    sid = specfs_server.session_start(module="exfat", mode="fast_eval")["session_id"]
    _approve_mount_spec(specfs_server, tmp_repo, sid)
    with pytest.raises(ValueError, match="disabled in fast_eval"):
        specfs_server.spec_fine(
            session_id=sid, speceval_comments="invariant missing",
        )


def test_toggle_fast_eval_mode_can_disable(
    tmp_repo: Path, reset_session_registry
):
    """Flipping back to non-fast lets refine work again — useful for the
    rare case where eval surfaces a bug requiring a quick repair."""
    import specfs_server
    sid = specfs_server.session_start(module="exfat", mode="fast_eval")["session_id"]
    out = specfs_server.toggle_fast_eval_mode(session_id=sid, enabled=False)
    assert out["fast_eval_mode"] is False
    spec_path = _approve_mount_spec(specfs_server, tmp_repo, sid)
    specfs_server.code_gen_start(
        session_id=sid, spec_path=str(spec_path.relative_to(tmp_repo)),
    )
    # Should NOT raise now
    res = specfs_server.code_gen_refine(
        session_id=sid, user_suggestion="use LOS_MemAlloc",
    )
    assert "next_prompt" in res


def test_fast_eval_e2e_single_shot_round_trip(
    tmp_repo: Path, reset_session_registry
):
    """Loop C end-to-end: session_start(fast_eval) → spec_gen → code_gen →
    linux_compare_submit. No refine / inject_diagnostics anywhere."""
    import json as _json
    import specfs_server
    sid = specfs_server.session_start(module="exfat", mode="fast_eval")["session_id"]

    # spec
    linux_dir = tmp_repo / "linux" / "fs" / "exfat"
    linux_dir.mkdir(parents=True, exist_ok=True)
    specfs_server.spec_gen_start(
        session_id=sid, linux_path=str(linux_dir), target_stage="mount",
    )
    spec = (
        "[PROMPT]\nmount.\n[GUARANTEE]\n```c\n"
        "int VfsExfatMount(struct Mount *m, struct Vnode *b, const void *d);\n```\n"
    )
    specfs_server.spec_gen_submit(session_id=sid, generated_spec_text=spec)
    specfs_server.spec_gen_approve(session_id=sid, final_spec_text=spec)

    # code
    spec_p = tmp_repo / specfs_server._SESSIONS[sid].spec_final_path
    specfs_server.code_gen_start(
        session_id=sid, spec_path=str(spec_p.relative_to(tmp_repo)),
    )
    code = (
        "int VfsExfatMount(struct Mount *m, struct Vnode *b, const void *d) {\n"
        "    return 0;\n"
        "}\n"
    )
    specfs_server.code_gen_submit(session_id=sid, generated_code=code)
    # Stamp code path manually rather than driving full code_gen_approve
    sess = specfs_server._SESSIONS[sid]
    code_rel = "fs/exfat/exfat_super.c"
    (tmp_repo / code_rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_repo / code_rel).write_text(code, encoding="utf-8")
    sess.code_final_paths = [code_rel]

    # linux_compare
    linux_src = linux_dir / "namei.c"
    linux_src.write_text("int exfat_unlink(void){return 0;}\n", encoding="utf-8")
    out = specfs_server.linux_compare_start(
        session_id=sid, linux_source_path=str(linux_src),
    )
    assert "prompt_for_llm" in out
    body = _json.dumps({
        "is_equivalent": False, "stage": "mount", "summary": "x",
        "spec_gaps": [],
        "code_gaps": [{
            "category": "missing_step", "severity": "high",
            "description": "no volume-dirty bracket",
            "linux_evidence": "fs/exfat/super.c:1",
            "code_location": "VfsExfatMount",
            "root_cause": "codegen_drift",
            "fix_suggestion": "add the bracket",
        }],
        "spec_prompt_recommendations": [],
        "codegen_prompt_recommendations": ["LITEOS_DIGEST: bracket mutating phases."],
    })
    sub = specfs_server.linux_compare_submit(
        session_id=sid, comparison_json=body,
    )
    assert sub["n_high_severity"] == 1
    # No FailureRecord injected → fast_eval session never tries to refine
    assert sess.failures == [], (
        "fast_eval session leaked findings into sess.failures — single-shot "
        "evaluation must not feed back into iterative repair"
    )


def test_prompt_feedback_summary_counts_stages(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    doc = tmp_repo / "docs" / "exfat_prompt_feedback.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "# exfat — Prompt Feedback Log\n\n"
        "## mount — 2026-05-08T10:00:00Z\nbody1\n"
        "## lookup — 2026-05-08T11:00:00Z\nbody2\n",
        encoding="utf-8",
    )
    out = specfs_server.prompt_feedback_summary(module="exfat")
    assert out["exists"] is True
    assert out["stages"] == 2
    assert "mount" in out["content"]


def test_sync_common_header_dedups_by_function_name(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    header = tmp_repo / "spec" / "exfat" / "common.header"
    header.write_text(
        "extern int  exfat_load_bitmap(exfat_sb_info *sbi);\n",
        encoding="utf-8",
    )
    ifaces = {
        "fs/exfat/exfat_bitmap.c": extract_stub(
            functions=["int exfat_load_bitmap(exfat_sb_info *sbi)"]
        ),
    }
    diff = specfs_server._sync_common_header("exfat", ifaces)
    assert diff == "", (
        "second sync of exfat_load_bitmap should be a no-op: pre-fix the "
        "single-vs-double-space whitespace difference defeated the dedup, "
        "now we compare by function name"
    )


def test_sync_common_header_appends_distinct_function(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    header = tmp_repo / "spec" / "exfat" / "common.header"
    header.write_text(
        "extern int exfat_load_bitmap(exfat_sb_info *sbi);\n",
        encoding="utf-8",
    )
    ifaces = {
        "fs/exfat/exfat_bitmap.c": extract_stub(
            functions=["int exfat_set_bitmap(exfat_sb_info *sbi, uint32_t clu)"]
        ),
    }
    diff = specfs_server._sync_common_header("exfat", ifaces)
    assert "exfat_set_bitmap" in diff
    assert "exfat_load_bitmap" not in diff


def test_dedup_common_header_drops_duplicate_signatures(
    tmp_repo: Path, reset_session_registry
):
    import specfs_server
    header = tmp_repo / "spec" / "exfat" / "common.header"
    header.write_text(
        "/* part 1 */\n"
        "extern int  exfat_load_bitmap(exfat_sb_info *sbi);\n"
        "extern int exfat_set_bitmap(exfat_sb_info *sbi, uint32_t clu);\n"
        "/* part 2 */\n"
        "extern int exfat_load_bitmap(exfat_sb_info *sbi);\n"
        "typedef struct foo foo_t;\n",
        encoding="utf-8",
    )
    out = specfs_server.dedup_common_header(module="exfat", dry_run=True)
    assert out["removed_count"] == 1
    assert out["before_lines"] == 6
    assert out["after_lines"] == 5
    assert header.read_text() == (
        "/* part 1 */\n"
        "extern int  exfat_load_bitmap(exfat_sb_info *sbi);\n"
        "extern int exfat_set_bitmap(exfat_sb_info *sbi, uint32_t clu);\n"
        "/* part 2 */\n"
        "extern int exfat_load_bitmap(exfat_sb_info *sbi);\n"
        "typedef struct foo foo_t;\n"
    ), "dry_run must not modify the file"

    out2 = specfs_server.dedup_common_header(module="exfat", dry_run=False)
    assert out2["removed_count"] == 1
    final = header.read_text()
    assert final.count("exfat_load_bitmap") == 1
    assert "exfat_set_bitmap" in final
    assert "typedef struct foo" in final
    assert "/* part 1 */" in final and "/* part 2 */" in final


def test_spec_gen_prompt_includes_full_common_header(
    tmp_repo: Path, reset_session_registry
):
    """Regression: spec_gen used to call filter_common_header_for_spec_gen
    which dropped every function extern; user pushed back since [RELY] often
    cites ancestor functions. Spec_gen now ships common.header verbatim."""
    import specfs_server
    header = tmp_repo / "spec" / "exfat" / "common.header"
    header.write_text(
        "extern int exfat_load_bitmap(exfat_sb_info *sbi);\n"
        "extern struct VnodeOps g_exfatVops;\n",
        encoding="utf-8",
    )
    linux_src = tmp_repo / "linux_namei.c"
    linux_src.write_text("int exfat_unlink(void){return 0;}\n", encoding="utf-8")
    sess = specfs_server.session_start(module="exfat")
    out = specfs_server.spec_gen_start(
        session_id=sess["session_id"],
        linux_path=str(linux_src),
        target_stage="unlink",
    )
    prompt = out["prompt_for_llm"]
    assert "exfat_load_bitmap" in prompt, (
        "function externs must reach spec_gen — [RELY] sections cite them"
    )
    assert "g_exfatVops" in prompt


def extract_stub(functions: list[str]):
    """Build a minimal ExtractedInterface for sync_common_header tests."""
    import extract
    return extract.ExtractedInterface(
        src_file="stub.c",
        functions=functions,
    )


def _seed_session_for_speceval(tmp_repo, specfs_server, sample_spec_text):
    sess_d = specfs_server.session_start(module="exfat")
    sid = sess_d["session_id"]
    sess = specfs_server._SESSIONS[sid]
    spec_p = tmp_repo / "spec" / "exfat" / "interface" / "exfat_x.spec"
    spec_p.parent.mkdir(parents=True, exist_ok=True)
    spec_p.write_text(sample_spec_text, encoding="utf-8")
    sess.code_spec_path = str(spec_p.relative_to(tmp_repo))
    sess.spec_final_path = sess.code_spec_path
    sess.code_final_text = "int VfsExfatLookup(struct Vnode *p, const char *n, struct Vnode **o){return 0;}\n"
    sess.code_final_paths = []
    sess.speceval_pending = True
    return sid, sess


def test_enforce_speceval_returns_prompt_and_reviewer_contract(
    tmp_repo, reset_session_registry, sample_spec_text
):
    import specfs_server
    sid, _ = _seed_session_for_speceval(tmp_repo, specfs_server, sample_spec_text)
    out = specfs_server.enforce_speceval(session_id=sid)
    assert "prompt_for_llm" in out and "[Generated code]" in out["prompt_for_llm"]
    assert "VfsExfatLookup" in out["prompt_for_llm"]
    rc = out["reviewer_contract"]
    assert "Momus" in rc and "task(" in rc and "FRESH context" in rc


def test_enforce_speceval_refuses_when_gate_not_set(
    tmp_repo, reset_session_registry, sample_spec_text
):
    import specfs_server
    sid, sess = _seed_session_for_speceval(tmp_repo, specfs_server, sample_spec_text)
    sess.speceval_pending = False
    with pytest.raises(RuntimeError, match="speceval_pending=False"):
        specfs_server.enforce_speceval(session_id=sid)


def test_record_speceval_verdict_pass_clears_gate(
    tmp_repo, reset_session_registry, sample_spec_text
):
    import specfs_server
    sid, sess = _seed_session_for_speceval(tmp_repo, specfs_server, sample_spec_text)
    sess.test_gen_enabled = True
    out = specfs_server.record_speceval_verdict(
        session_id=sid, verdict_json='{"is_good": true, "comments": ""}',
    )
    assert out["is_good"] is True and out["next"] == "test_gen"
    assert sess.speceval_pending is False
    assert sess.phase == "test_drafting"


def test_record_speceval_verdict_fail_keeps_gate_armed(
    tmp_repo, reset_session_registry, sample_spec_text
):
    import specfs_server
    sid, sess = _seed_session_for_speceval(tmp_repo, specfs_server, sample_spec_text)
    out = specfs_server.record_speceval_verdict(
        session_id=sid,
        verdict_json='{"is_good": false, "comments": "missing HOST_TO_LE16 wraps"}',
    )
    assert out["is_good"] is False and out["next"] == "code_gen_refine"
    assert "HOST_TO_LE16" in out["comments"]
    assert sess.speceval_pending is True, (
        "Gate must remain armed on fail so the next code_gen_refine + re-eval "
        "round still has to satisfy enforce_speceval"
    )
    assert sess.layer_retries["speceval"] == 1
    assert any(f.layer == "speceval" for f in sess.failures)


def test_test_gen_start_refuses_while_speceval_pending(
    tmp_repo, reset_session_registry, sample_spec_text
):
    import specfs_server
    sid, _ = _seed_session_for_speceval(tmp_repo, specfs_server, sample_spec_text)
    with pytest.raises(RuntimeError, match="speceval_pending"):
        specfs_server.test_gen_start(session_id=sid)


def test_record_speceval_verdict_rejects_invalid_json(
    tmp_repo, reset_session_registry, sample_spec_text
):
    import specfs_server
    sid, _ = _seed_session_for_speceval(tmp_repo, specfs_server, sample_spec_text)
    with pytest.raises(RuntimeError, match="not valid JSON"):
        specfs_server.record_speceval_verdict(
            session_id=sid, verdict_json="not json{",
        )


def test_latest_round_failures_returns_only_max_round():
    import specfs_server
    import state as state_mod
    failures = [
        state_mod.FailureRecord(layer="lsp", payload="round1 err", round_idx=1),
        state_mod.FailureRecord(layer="speceval", payload="round1 spec", round_idx=1),
        state_mod.FailureRecord(layer="lsp", payload="round2 err", round_idx=2),
        state_mod.FailureRecord(layer="user", payload="round2 hint", round_idx=2),
        state_mod.FailureRecord(layer="qemu", payload="round3 panic", round_idx=3),
    ]
    out = specfs_server._latest_round_failures(failures)
    assert len(out) == 1
    assert out[0].layer == "qemu"
    assert out[0].round_idx == 3


def test_latest_round_failures_handles_empty():
    import specfs_server
    assert specfs_server._latest_round_failures([]) == []


def test_inject_diagnostics_uses_global_round_idx_not_layer_counter(
    tmp_repo, reset_session_registry, sample_spec_text
):
    """Paper alignment: round_idx must be a single global counter
    (sess.code_iterations) so _latest_round_failures can correctly select
    the most recent retry across heterogeneous layer sources. Pre-fix,
    inject_diagnostics used per-layer counters which made round_idx
    non-comparable across layers."""
    import specfs_server
    sid, sess = _seed_session_for_speceval(tmp_repo, specfs_server, sample_spec_text)
    sess.speceval_pending = False
    sess.code_iterations = 5
    specfs_server.inject_diagnostics(
        session_id=sid, layer="compile", payload="some compile error",
    )
    assert sess.failures[-1].round_idx == 6
    specfs_server.inject_diagnostics(
        session_id=sid, layer="compile", payload="another compile error",
    )
    assert sess.failures[-1].round_idx == 7
    specfs_server.inject_diagnostics(
        session_id=sid, layer="qemu", payload="kernel panic",
    )
    assert sess.failures[-1].round_idx == 8
    assert sess.layer_retries["compile"] == 2
    assert sess.layer_retries["qemu"] == 1


def test_spec_gen_refine_only_injects_current_round_suggestion(
    tmp_repo: Path, reset_session_registry
):
    """Paper alignment: spec_gen_refine round N injects ONLY the current
    round's suggestion. Earlier rounds' suggestions were already
    materialised into the previous_spec draft when the LLM rewrote it,
    so re-injecting them would inflate the prompt and confuse the LLM
    about which hints are still actionable."""
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    linux_dir = tmp_repo / "linux" / "fs" / "exfat"
    linux_dir.mkdir(parents=True)
    specfs_server.spec_gen_start(
        session_id=sid, linux_path=str(linux_dir), target_stage="mount",
    )
    specfs_server.spec_gen_refine(
        session_id=sid,
        user_suggestion="ROUND_1_HINT: tighten [GUARANTEE]",
    )
    out2 = specfs_server.spec_gen_refine(
        session_id=sid,
        user_suggestion="ROUND_2_HINT: add tombstone semantics",
    )
    prompt = out2["next_prompt"]
    assert "ROUND_2_HINT" in prompt, "current round must be injected"
    assert "ROUND_1_HINT" not in prompt, (
        "stale round 1 leaked — its effect was already materialised in "
        "previous_spec, re-injecting here violates paper-aligned single-round semantics"
    )
    sess = specfs_server._SESSIONS[sid]
    assert len(sess.user_suggestions) == 2, (
        "history must be retained for metric/debug surface; only the prompt "
        "wire-format is single-round"
    )


def test_spec_gen_start_does_not_inject_session_history_suggestions(
    tmp_repo: Path, reset_session_registry
):
    """Paper alignment: spec_gen_start is a fresh-stage entry point.
    A session that has lingering user_suggestions from a previous stage
    (e.g. via session reuse) must NOT leak those into the new stage's
    prompt — they were addressed by the earlier stage's approved spec."""
    import specfs_server
    sid = specfs_server.session_start(module="exfat")["session_id"]
    sess = specfs_server._SESSIONS[sid]
    sess.user_suggestions = ["LEAKED_FROM_PRIOR_STAGE: redo locking"]
    linux_dir = tmp_repo / "linux" / "fs" / "exfat"
    linux_dir.mkdir(parents=True)
    out = specfs_server.spec_gen_start(
        session_id=sid, linux_path=str(linux_dir), target_stage="lookup",
    )
    assert "LEAKED_FROM_PRIOR_STAGE" not in out["prompt_for_llm"]


def test_rebuild_codegen_prompt_only_renders_latest_round_failures(
    tmp_repo, reset_session_registry, sample_spec_text
):
    """Paper alignment regression: pre-fix, _rebuild_codegen_prompt
    expanded EVERY accumulated failure into [Modification suggestions],
    causing monotonic prompt growth across long retry sessions and
    stale-hint contamination. Post-fix, only the latest round's
    failures are injected."""
    import specfs_server
    import state as state_mod
    sid, sess = _seed_session_for_speceval(tmp_repo, specfs_server, sample_spec_text)
    sess.speceval_pending = False
    sess.failures = [
        state_mod.FailureRecord(layer="lsp", payload="STALE_ROUND_1_LSP", round_idx=1),
        state_mod.FailureRecord(layer="user", payload="STALE_ROUND_2_USER", round_idx=2),
        state_mod.FailureRecord(layer="speceval", payload="LATEST_ROUND_3_SPEC", round_idx=3),
        state_mod.FailureRecord(layer="qemu", payload="LATEST_ROUND_3_QEMU", round_idx=3),
    ]
    sess.current_artifact = "int dummy(void) { return 0; }"
    out = specfs_server._rebuild_codegen_prompt(sess)
    prompt = out["next_prompt"]
    assert "LATEST_ROUND_3_SPEC" in prompt
    assert "LATEST_ROUND_3_QEMU" in prompt
    assert "STALE_ROUND_1_LSP" not in prompt, "Stale round 1 leaked into next prompt"
    assert "STALE_ROUND_2_USER" not in prompt, "Stale round 2 leaked into next prompt"
