"""
specfs-port plugin — in-memory session state.

A session is the runtime container for one /specfs-port-spec or /specfs-port-code
invocation. The MCP server holds these in a dict keyed by session_id; they live
across tool calls within a slash-command execution but do not persist beyond
plugin restart. Persistent state lives in the DAG file (see dag.py).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


# repo root resolution: plugin lives at <repo>/.claude/plugins/specfs-port/server/
# walk up 4 levels to get the kernel/liteos_a root
def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


SessionPhase = Literal[
    "idle",                # session_start just returned
    "spec_drafting",       # Loop A in flight
    "code_drafting",       # Loop B Step 4
    "compile_check",       # Layer 1: clangd LSP (preferred) + gcc -fsyntax-only fallback
    "build_check",         # Layer 2 (build)
    "qemu_check",          # Layer 2 (qemu)
    "speceval",            # Layer 3
    "user_review",         # Layer 4
    "approved",            # terminal
    "aborted",             # terminal
]


@dataclass
class Clarification:
    question: str
    user_answer: str
    asked_at: float = field(default_factory=time.time)


@dataclass
class FailureRecord:
    layer: str               # "compile" | "style" | "build" | "qemu" | "speceval" | "user"
    payload: str             # stderr / diagnostics / comments / user feedback
    round_idx: int           # 0-based round counter within this layer
    occurred_at: float = field(default_factory=time.time)


@dataclass
class Session:
    session_id: str
    module: str                          # e.g., "exfat"
    mode: Literal["gen", "evolve"]
    phase: SessionPhase = "idle"

    # Loop A state
    spec_target_stage: str = ""          # e.g., "lookup"
    spec_linux_path: str = ""            # absolute or relative path
    spec_draft_path: str = ""            # spec/<module>/.../<op>.spec.draft
    spec_final_path: str = ""            # spec/<module>/.../<op>.spec
    spec_iterations: int = 0

    # Loop B state
    code_spec_path: str = ""             # input spec (must be approved)
    code_draft_paths: list[str] = field(default_factory=list)
    code_final_paths: list[str] = field(default_factory=list)
    code_iterations: int = 0

    # current generated artifact (between submit and next phase)
    current_artifact: str = ""           # spec text or code text

    # Layered defense per-layer counters
    # v0.3.3: Layer 0 (LSP) merged into Layer 1 (compile). The "compile" layer
    # now runs clangd diagnostics (preferred) with gcc -fsyntax-only as fallback.
    layer_retries: dict[str, int] = field(default_factory=lambda: {
        "compile": 0, "style": 0, "build": 0, "qemu": 0, "speceval": 0,
    })

    # Plugin v0.2: SpecEvaluator self-audit ON by default. The user explicitly
    # required self-audit BEFORE user review (see commands/specfs-port-code.md
    # Step 8 hard contract). Disable per-session with --speceval-off.
    speceval_enabled: bool = True

    # Plugin v0.3: Layer S (Style audit) ON by default. User directive
    # "加入编码风格评估环节". Evaluates 6 dimensions: naming, complexity, layout,
    # memory/libsec, locking, error path. Disable with --style-off.
    style_audit_enabled: bool = True

    # Build/QEMU opt-out (--no-build)
    skip_build_layer: bool = False

    # Accumulated context that will be re-injected on refine rounds
    clarifications: list[Clarification] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    user_suggestions: list[str] = field(default_factory=list)

    # Last assembled prompt (for show_assembled_prompt debugging)
    last_prompt: str = ""

    created_at: float = field(default_factory=time.time)


def new_session(module: str, mode: str = "gen") -> Session:
    return Session(
        session_id=uuid.uuid4().hex[:12],
        module=module,
        mode=mode,  # type: ignore[arg-type]
    )
