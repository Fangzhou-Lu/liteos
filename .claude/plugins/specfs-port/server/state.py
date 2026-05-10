"""
specfs-port plugin — session state.

A session is the runtime container for one /specfs-port-spec or
/specfs-port-code invocation. The MCP server holds these in an in-memory
dict keyed by session_id and (since 2026-05-09) mirrors them to
``.specfs/sessions/<id>.json`` so that an OpenCode / MCP-server restart
does not orphan an in-flight workflow. Persistent stage state lives in
the DAG file (see dag.py).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


# repo root resolution: plugin lives at <repo>/.claude/plugins/specfs-port/server/
# walk up 4 levels to get the kernel/liteos_a root
def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


SessionPhase = Literal[
    "idle",                # session_start just returned
    "spec_drafting",       # Loop A in flight
    "code_drafting",       # Loop B Step 4
    "compile_check",       # Layer 1a: clangd LSP only (P1.3 dropped gcc -fsyntax-only fallback)
    "build_check",         # Layer 2 (build)
    "qemu_check",          # Layer 2 (qemu)
    "speceval",            # Layer 3
    "test_drafting",       # Layer T (v0.3.4: spec-derived cmocka test gen)
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
    # P1.6 Wave 2 (2026-05-08): "fast_eval" added for prompt-optimisation
    # measurement runs — spec+code each generated ONCE, no feedback loops
    # (no SpecEval, no spec_fine, no refine, no inject_diagnostics, no
    # build/qemu validation). The whole point of fast_eval is to measure
    # the prompt's first-shot quality without contamination from the LLM's
    # iterative repair ability.
    mode: Literal["gen", "evolve", "fast_eval"]
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
    code_final_text: str = ""            # captured at code_gen_approve for Layer T re-use

    # Layer T state (v0.3.4 — spec-derived cmocka test gen)
    # Pipeline runs Layer T BETWEEN SpecEval (Layer 3) and user review (Layer 4),
    # so the user reviews code+test together in one HITL pass.
    test_stage: str = ""                 # echo of code_spec_path's stage
    test_draft_path: str = ""            # testsuites/unittest/<module>/test_<stage>.c.draft
    test_final_path: str = ""            # testsuites/unittest/<module>/test_<stage>.c
    test_iterations: int = 0

    # current generated artifact (between submit and next phase)
    current_artifact: str = ""           # spec text or code text

    # Layered defense per-layer counters
    # v0.3.3: Layer 0 (LSP) merged into Layer 1 (compile).
    # P1.3 (2026-05-07): gcc -fsyntax-only fallback removed; the "compile"
    # layer is now LSP-only (clangd via OMC LSP). LSP is a hard prerequisite.
    # v0.3.4: "test_gen" added (Layer T retry budget; default 3 rounds, see DESIGN §10).
    # P1.2 (2026-05-07): "style" repositioned as a SIBLING of "compile" — both
    # gate Layer 2 in parallel (was a serial Layer S after compile in v0.3).
    # P1.4 (2026-05-07): "style" folded back into Layer 1a as a SEQUENTIAL
    # second sub-step (LSP first, then style). Retry budgets stay separate
    # ("compile" 4 / "style" 5). Layer 2 also absorbed SpecValidator: build,
    # cmocka exec, qemu now share the SAME 3-round budget. SpecEval moved
    # BEFORE Layer 2 (cheap-first ordering). Layer T moved from Loop A to
    # Loop B — same retry slot ("test_gen", cap 3), different trigger point.
    layer_retries: dict[str, int] = field(default_factory=lambda: {
        "compile": 0, "style": 0, "build": 0, "qemu": 0, "speceval": 0, "test_gen": 0,
        "spec_fine": 0,  # F3 SpecFine: spec polish via SpecEval feedback (cap 3)
    })

    # Plugin v0.2: SpecEvaluator self-audit ON by default. The user explicitly
    # required self-audit BEFORE user review (see commands/specfs-port-code.md
    # Step 5 hard contract — P1.4 moved this layer BEFORE Layer 2 build/QEMU,
    # was Step 6 in P1.2). Disable per-session with --speceval-off.
    speceval_enabled: bool = True

    # Plugin v0.3 introduced Layer S (style audit) as a serial step AFTER
    # compile. P1.2 (2026-05-07) repositioned this layer as a SIBLING of
    # compile (Layer 1b), running in parallel. P1.4 (2026-05-07) folded
    # it back into Layer 1a as a SEQUENTIAL second sub-step — LSP runs
    # first; style runs only if LSP is clean. Keeps each retry round's
    # diagnostic source crisp (LSP error vs. style violation never mix).
    # SpecEval (Layer 3) is spec-conformance only; it does not inline
    # style rules. Toggle this sub-step with `--style-off`.
    style_audit_enabled: bool = True

    # Build/QEMU opt-out (--no-build)
    skip_build_layer: bool = False

    # Plugin v0.3.4: Layer T (spec-derived cmocka test gen) ON by default.
    # Was advertised in v0.3.2 CHANGELOG but never wired in the server until
    # v0.3.4 — Wave A 9 stages accumulated test debt under the v0.3.2 era,
    # see commit 149487a9 for the catch-up batch. Disable with --test-off.
    test_gen_enabled: bool = True

    # P1.6 Wave 2 (2026-05-08): single-shot evaluation mode for Loop C
    # prompt-optimisation runs. When True the server REFUSES every
    # iterative-repair tool (spec_gen_refine / code_gen_refine / spec_fine /
    # inject_diagnostics) so the only artefacts a stage produces are its
    # FIRST submissions. Set automatically when session_start receives
    # mode="fast_eval"; can also be flipped mid-session via
    # toggle_fast_eval_mode (rare).
    fast_eval_mode: bool = False

    # P1.7 (2026-05-10): SpecEval gate. Set to True by code_gen_approve when
    # speceval_enabled. Cleared by enforce_speceval after a verdict is
    # recorded. test_gen_start refuses to run while this flag is True so
    # SpecEval can never be silently skipped — caller MUST spawn an
    # independent reviewer agent (e.g. Momus subagent) before proceeding.
    speceval_pending: bool = False

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


_SESSIONS_DIR_NAME = ".specfs/sessions"


def _sessions_dir() -> Path:
    return repo_root() / _SESSIONS_DIR_NAME


def _session_path(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.json"


def _to_jsonable(sess: Session) -> dict[str, Any]:
    return asdict(sess)


def _from_jsonable(d: dict[str, Any]) -> Session:
    clars_raw = d.pop("clarifications", []) or []
    fails_raw = d.pop("failures", []) or []
    sess = Session(**d)
    sess.clarifications = [Clarification(**c) for c in clars_raw]
    sess.failures = [FailureRecord(**f) for f in fails_raw]
    return sess


def save_session(sess: Session) -> Path:
    """Persist session to ``.specfs/sessions/<id>.json``. Best-effort —
    failures (e.g. read-only fs in tests) are silently ignored; in-memory
    state remains the source of truth and the disk copy is purely a
    restart-survival aid.
    """
    p = _session_path(sess.session_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_to_jsonable(sess), indent=2), encoding="utf-8")
    except OSError:
        pass
    return p


def load_session(session_id: str) -> Session | None:
    """Return persisted session or None when no on-disk copy exists."""
    p = _session_path(session_id)
    if not p.exists():
        return None
    try:
        return _from_jsonable(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def drop_session(session_id: str) -> None:
    """Remove the on-disk mirror, if any. Idempotent."""
    p = _session_path(session_id)
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
