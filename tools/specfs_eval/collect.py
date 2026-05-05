#!/usr/bin/env python3
"""specfs-port plugin 评估指标采集器。

用法：
    python3 collect.py --stage <stage_name> [--repo <repo_root>]
    python3 collect.py --all                              # 扫所有 DAG stage
    python3 collect.py --baseline > baseline_metrics.json # 批量回填基线

输出：JSON 单 stage 记录或字典 {stage: record}。

本脚本仅依赖 Python 3 stdlib + git CLI；不调 LLM；不连远端。
schema 见 ./schema.md。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ---------- repo helpers --------------------------------------------------

def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / ".git").exists():
            return p
    return here.parents[2]


def _run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False).stdout


# ---------- spec metrics --------------------------------------------------

def _spec_metrics(spec_path: Path) -> dict:
    if not spec_path.is_file():
        return {"spec_path": str(spec_path), "spec_present": False}
    text = spec_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    segs = {seg: f"[{seg}]" in text for seg in ("PROMPT", "RELY", "GUARANTEE", "SPECIFICATION")}
    invariants = re.findall(r"^\s*\*\*Invariant\*\*\s*\(id=([^)]+)\)", text, flags=re.M)
    refine = len(re.findall(r"^##\s*Refine Prompt", text, flags=re.M))
    rely_externs = re.findall(r"^\s*extern\s+\S", text, flags=re.M)
    # Pull function symbols out of [GUARANTEE] (use as code-export hint when DAG empty).
    guarantee_match = re.search(r"\[GUARANTEE\](.*?)(?=^\[[A-Z]|^## |\Z)",
                                text, flags=re.M | re.DOTALL)
    spec_exports: list[str] = []
    if guarantee_match:
        for m in re.finditer(r"\bextern\s+[\w\s\*]+?\b([A-Za-z_]\w+)\s*\(",
                              guarantee_match.group(1)):
            sym = m.group(1)
            if sym not in spec_exports:
                spec_exports.append(sym)
    return {
        "spec_path": str(spec_path),
        "spec_present": True,
        "spec_loc": len(lines),
        "spec_segments_ok": all(segs.values()),
        "spec_segments_missing": [k for k, v in segs.items() if not v],
        "spec_invariants": len(invariants),
        "spec_invariant_ids": invariants,
        "spec_refine_prompts": refine,
        "spec_rely_extern_count": len(rely_externs),
        "spec_guarantee_exports": spec_exports,
    }


# ---------- code metrics --------------------------------------------------

_FUNC_DEF_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_ \t\*]*\s+([A-Za-z_][\w]+)\s*\([^;]*\)\s*$")


def _code_metrics(repo: Path, exports: list[str]) -> dict:
    """Find function definitions for `exports` symbols across fs/<module>/*.c.

    Returns aggregate LOC across the files that contain at least one export."""
    if not exports:
        return {"code_files": 0, "code_loc": 0, "function_count": 0, "exported_files": []}
    fs_dir = repo / "fs" / "exfat"
    if not fs_dir.is_dir():
        return {"code_files": 0, "code_loc": 0, "function_count": 0, "exported_files": []}
    matched_files: set[Path] = set()
    func_count = 0
    for cfile in sorted(fs_dir.glob("*.c")):
        text = cfile.read_text(encoding="utf-8", errors="replace")
        for sym in exports:
            # Look for a top-level definition (return-type line + sym before '(')
            if re.search(rf"^[a-zA-Z_][\w \t\*]*?\b{re.escape(sym)}\s*\(", text, flags=re.M):
                matched_files.add(cfile)
                func_count += 1
                break_outer = False  # do not break — count multi-export files
        # No break; iterate all symbols.
    # Total LOC across matched files (overcount if file holds many stages).
    loc = sum(len(p.read_text(encoding="utf-8", errors="replace").splitlines())
              for p in matched_files)
    # Per-export hit count (fresh pass for accuracy).
    func_count = 0
    for cfile in matched_files:
        text = cfile.read_text(encoding="utf-8", errors="replace")
        for sym in exports:
            if re.search(rf"^[a-zA-Z_][\w \t\*]*?\b{re.escape(sym)}\s*\(", text, flags=re.M):
                func_count += 1
    return {
        "code_files": len(matched_files),
        "code_loc": loc,
        "function_count": func_count,
        "exported_files": sorted(str(p.relative_to(repo)) for p in matched_files),
    }


# ---------- test metrics --------------------------------------------------

def _test_metrics(repo: Path, stage: str) -> dict:
    test_dir = repo / "testsuites" / "unittest" / "exfat"
    if not test_dir.is_dir():
        return {"test_present": False, "testpoint_count": 0}
    candidates = [test_dir / f"test_{stage}.c"]
    # Wave 4a/4b/4e use stage-name files; some Wave A tests are merged.
    for c in candidates:
        if c.is_file():
            text = c.read_text(encoding="utf-8")
            # cmocka has multiple variant names: cmocka_unit_test,
            # cmocka_unit_test_setup_teardown, cmocka_unit_test_prestate*, etc.
            tps = len(re.findall(r"\bcmocka_unit_test\w*\s*\(", text))
            return {
                "test_present": True,
                "test_path": str(c.relative_to(repo)),
                "testpoint_count": tps,
            }
    return {"test_present": False, "testpoint_count": 0}


# ---------- commit metrics ------------------------------------------------

def _pick_canonical_commit(repo: Path, stage: str) -> Optional[str]:
    """Find the canonical 'full-stage delivery' commit for a stage by subject pattern.

    Heuristic: scan recent commits, pick the one whose subject contains the
    stage name AND whose body has the most `Qn=` ask-first decision markers
    (these mark the canonical full-stage commit, not a fix-up commit).
    """
    log = _run(["git", "log", "--format=%H%x09%s", "-200"], repo)
    candidates: list[tuple[str, str]] = []
    for ln in log.splitlines():
        if "\t" not in ln:
            continue
        sha, subj = ln.split("\t", 1)
        if re.search(rf"\b{re.escape(stage)}\b", subj, flags=re.I):
            candidates.append((sha, subj))
    if not candidates:
        return None
    # Score each candidate by # of Q\d= markers in body.
    best = (None, -1)
    for sha, subj in candidates:
        body = _run(["git", "log", "--format=%B", "-1", sha], repo)
        score = len(set(re.findall(r"^[\s\-\*]*Q([0-9])\b", body, flags=re.M)))
        # Boost commits that mention "code 层" (canonical code delivery).
        if "code 层" in body or "code 层完整交付" in body:
            score += 5
        # Penalize "fix" subjects.
        if re.search(r"\bfix\b|^Description:\s*fix", subj, flags=re.I):
            score -= 3
        if score > best[1]:
            best = (sha, score)
    return best[0]


def _commit_metrics(repo: Path, stage: str, spec_path: Optional[Path]) -> dict:
    """Find the canonical commit for stage and extract decision markers + diff stat."""
    sha = _pick_canonical_commit(repo, stage)
    if not sha:
        # Fallback: most recent commit touching spec file.
        if spec_path and spec_path.is_file():
            path_rel = spec_path.relative_to(repo)
            log = _run(["git", "log", "--format=%H%x09%aI%x09%s", "-1", "--", str(path_rel)], repo)
            if log.strip():
                sha, ts, subj = log.strip().split("\t", 2)
            else:
                return {"commit_sha": None}
        else:
            return {"commit_sha": None}
    # Read commit metadata + diff stat.
    meta = _run(["git", "log", "--format=%H%x09%aI%x09%s", "-1", sha], repo).strip()
    if not meta:
        return {"commit_sha": None}
    sha, ts, subj = meta.split("\t", 2)
    body = _run(["git", "log", "--format=%B", "-1", sha], repo)
    ask_first = len(set(re.findall(r"^[\s\-\*]*Q([0-9])\b", body, flags=re.M)))
    flagged = 0
    m = re.search(r"agent\s+派生\s*(\d+)\s*个", body)
    if m:
        flagged = int(m.group(1))
    invs = re.findall(r"-\s+(exfat-[a-z0-9-]+)", body)
    # Diff stat: sum of additions to fs/<module>/*.c excluding test/.h files.
    numstat = _run(["git", "show", "--format=", "--numstat", sha], repo)
    code_loc_added = 0
    test_loc_added = 0
    for ln in numstat.splitlines():
        parts = ln.split("\t")
        if len(parts) != 3:
            continue
        added, _deleted, path = parts
        try:
            n = int(added)
        except ValueError:
            continue
        if path.startswith("fs/exfat/") and path.endswith(".c"):
            code_loc_added += n
        elif path.startswith("testsuites/unittest/exfat/") and path.endswith(".c"):
            test_loc_added += n
    return {
        "commit_sha": sha,
        "commit_ts": ts,
        "commit_subject": subj,
        "ask_first_questions": ask_first,
        "agent_flagged_decisions": flagged,
        "commit_body_invariant_refs": len(invs),
        "commit_code_loc_added": code_loc_added,
        "commit_test_loc_added": test_loc_added,
    }


# ---------- prompt assembly metrics --------------------------------------

# Section headers that appear in the assembled Loop-A prompt; used for
# breaking it into chunks and counting bytes per role.
_PROMPT_SECTION_RE = re.compile(r"^\[([A-Z][A-Za-z0-9 \-→/]*)\]\s*$", re.M)


def _parse_prompt_sections(text: str) -> dict[str, int]:
    """Split the assembled prompt by [SECTION] headers; return {name: chars}.
    Pre-amble (before first header) is keyed as '_preamble'."""
    parts: dict[str, int] = {}
    headers = list(_PROMPT_SECTION_RE.finditer(text))
    if not headers:
        return {"_full": len(text)}
    if headers[0].start() > 0:
        parts["_preamble"] = headers[0].start()
    for i, m in enumerate(headers):
        name = m.group(1)
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        parts[name] = end - m.start()
    return parts


def _prompt_metrics(repo: Path, stage: str, linux_path: Optional[str],
                    module: str = "exfat") -> dict:
    """Invoke plugin's _driver_loop_a.py to capture the assembled spec_gen prompt
    for stage. Returns prompt_chars + per-section breakdown."""
    if not linux_path:
        # Heuristic: pick a Linux fs/exfat/ source by stage name
        linux_root = Path("/Users/kissa/Codebase/linux/fs/exfat")
        candidates = {
            "mount": linux_root / "super.c",
            "mkdir": linux_root / "namei.c",
            "create": linux_root / "namei.c",
            "unlink": linux_root / "namei.c",
            "rmdir": linux_root / "namei.c",
            "rename": linux_root / "namei.c",
            "lookup": linux_root / "namei.c",
            "read": linux_root / "file.c",
            "write": linux_root / "file.c",
            "readdir": linux_root / "dir.c",
        }
        guess = candidates.get(stage)
        linux_path = str(guess) if guess and guess.is_file() else str(linux_root / "namei.c")
    driver = repo / ".claude" / "plugins" / "specfs-port" / "server" / "_driver_loop_a.py"
    if not driver.is_file():
        return {"prompt_metrics_error": "driver not found"}
    res = subprocess.run(
        ["python3", str(driver), module, stage, linux_path],
        cwd=driver.parent, capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        return {"prompt_metrics_error": f"driver rc={res.returncode}: {res.stderr[:200]}"}
    prompt_file = driver.parent / ".assembled_prompt.txt"
    if not prompt_file.is_file():
        return {"prompt_metrics_error": "assembled prompt file missing"}
    text = prompt_file.read_text(encoding="utf-8")
    return {
        "prompt_chars": len(text),
        "prompt_sections": _parse_prompt_sections(text),
        "prompt_linux_path": linux_path,
    }


# ---------- DAG ----------------------------------------------------------

def _load_dag(repo: Path) -> dict:
    p = repo / "spec" / "exfat" / ".specfs.dag.json"
    if not p.is_file():
        return {"stages": []}
    return json.loads(p.read_text(encoding="utf-8"))


def _stage_record(repo: Path, stage_node: dict,
                  cmocka_pass: Optional[bool] = None,
                  build_pass: Optional[bool] = None,
                  include_prompt: bool = False,
                  linux_path: Optional[str] = None) -> dict:
    stage = stage_node.get("id") or stage_node.get("stage_name")
    spec = stage_node.get("spec", {})
    code = stage_node.get("code", {})
    spec_files = spec.get("files") or []
    spec_path = (repo / spec_files[0]) if spec_files else None
    rec = {
        "stage": stage,
        "depends_on": stage_node.get("depends_on") or [],
        "spec_approval_iterations": spec.get("approval_iterations"),
        "code_approval_iterations": code.get("approval_iterations"),
    }
    spec_rec = _spec_metrics(spec_path) if spec_path else {"spec_present": False}
    rec.update(spec_rec)
    # exports come from various places (DAG → spec [GUARANTEE] fallback).
    # DAG entries may be plain strings OR dicts {name|symbol: ...}.
    def _norm_exports(raw) -> list[str]:
        out: list[str] = []
        for e in (raw or []):
            if isinstance(e, str):
                out.append(e)
            elif isinstance(e, dict):
                sym = e.get("name") or e.get("symbol")
                if sym:
                    out.append(sym)
        return out
    exports = _norm_exports(stage_node.get("exports"))
    if not exports:
        exports = _norm_exports(spec.get("exports"))
    if not exports:
        exports = _norm_exports(code.get("exports"))
    if not exports:
        exports = spec_rec.get("spec_guarantee_exports", [])
    rec.update(_code_metrics(repo, exports))
    rec["exports"] = exports
    rec.update(_test_metrics(repo, stage))
    rec.update(_commit_metrics(repo, stage, spec_path))
    # Prefer commit_code_loc_added (per-stage diff) over file-aggregate code_loc.
    spec_loc = rec.get("spec_loc")
    code_added = rec.get("commit_code_loc_added")
    if spec_loc and code_added:
        rec["spec_code_ratio"] = round(spec_loc / code_added, 2)
    elif spec_loc and rec.get("code_loc"):
        rec["spec_code_ratio_filewise"] = round(spec_loc / rec["code_loc"], 2)
    # Quality gates: explicit override → fallback "committed implies passed".
    if cmocka_pass is not None:
        rec["cmocka_pass"] = cmocka_pass
    elif rec.get("commit_sha"):
        rec["cmocka_pass"] = True  # baseline assumption: committed = passed
    if build_pass is not None:
        rec["kernel_build_pass"] = build_pass
    elif rec.get("commit_sha"):
        rec["kernel_build_pass"] = True
    if include_prompt:
        rec.update(_prompt_metrics(repo, rec["stage"], linux_path))
    return rec


# ---------- main ----------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="specfs-port metric collector")
    ap.add_argument("--stage", help="single stage name")
    ap.add_argument("--all", action="store_true", help="dump all DAG stages")
    ap.add_argument("--baseline", action="store_true",
                    help="dump all DAG stages + summary aggregates (alias for --all + summary)")
    ap.add_argument("--repo", help="repo root (default: auto-detect)")
    ap.add_argument("--cmocka-pass", choices=["true", "false"],
                    help="experiment override: did cmocka pass?")
    ap.add_argument("--build-pass", choices=["true", "false"],
                    help="experiment override: did kernel build pass?")
    ap.add_argument("--include-prompt", action="store_true",
                    help="invoke plugin's _driver_loop_a.py to capture "
                         "assembled prompt size + section breakdown")
    ap.add_argument("--linux-path",
                    help="Linux source path (for --include-prompt; auto-guessed if omitted)")
    args = ap.parse_args()
    cmocka_pass = (args.cmocka_pass == "true") if args.cmocka_pass else None
    build_pass = (args.build_pass == "true") if args.build_pass else None
    include_prompt = args.include_prompt
    linux_path = args.linux_path

    repo = Path(args.repo) if args.repo else _repo_root()
    dag = _load_dag(repo)
    stages = dag.get("stages", [])

    if args.stage:
        node = next((s for s in stages if (s.get("id") or s.get("stage_name")) == args.stage), None)
        if node is None:
            print(f"stage not found: {args.stage}", file=sys.stderr)
            return 2
        print(json.dumps(_stage_record(repo, node, cmocka_pass=cmocka_pass, build_pass=build_pass,
                                include_prompt=include_prompt, linux_path=linux_path), indent=2, ensure_ascii=False))
        return 0

    if args.all or args.baseline:
        records = [_stage_record(repo, s) for s in stages]
        out = {"stages": records}
        if args.baseline:
            # Aggregate over "atomic-commit" stages only (one commit per stage):
            # code_loc_added bounded; excludes Wave A bundled-PR rows whose code
            # diff stat covers many stages and biases the ratio.
            # Atomic stage = single-stage delivery commit
            #   - code_loc_added in [50, 1000]: bigger than a fix-up; smaller than
            #     a Wave A bundled PR
            #   - testpoint_count > 0 OR ask_first_questions >= 1 (positive signal
            #     this is the canonical full-delivery commit, not a tweak)
            ATOMIC_LOC_LO, ATOMIC_LOC_HI = 50, 1000
            valid = [r for r in records if r.get("spec_present")]
            def _is_atomic(r):
                code = r.get("commit_code_loc_added") or 0
                if not (ATOMIC_LOC_LO <= code <= ATOMIC_LOC_HI):
                    return False
                # Avoid fix-up commits matched as canonical: require some
                # testpoint coverage OR ask-first decision evidence in body.
                if (r.get("testpoint_count") or 0) > 0:
                    return True
                if (r.get("ask_first_questions") or 0) >= 1:
                    return True
                return False
            atomic = [r for r in valid if _is_atomic(r)]
            for r in records:
                r["is_atomic_commit"] = _is_atomic(r) and r.get("spec_present", False)

            def avg(records_subset, key):
                vals = [r[key] for r in records_subset if isinstance(r.get(key), (int, float))]
                return round(sum(vals) / len(vals), 2) if vals else None

            out["summary"] = {
                "stage_count": len(records),
                "stages_with_spec": len(valid),
                "atomic_stages": len(atomic),
                "avg_spec_loc_all": avg(valid, "spec_loc"),
                "avg_spec_invariants_all": avg(valid, "spec_invariants"),
                "avg_testpoint_count_all": avg(valid, "testpoint_count"),
                "atomic_avg_spec_loc": avg(atomic, "spec_loc"),
                "atomic_avg_code_loc_added": avg(atomic, "commit_code_loc_added"),
                "atomic_avg_spec_code_ratio": avg(atomic, "spec_code_ratio"),
                "atomic_avg_ask_first_questions": avg(atomic, "ask_first_questions"),
                "atomic_avg_agent_flagged_decisions": avg(atomic, "agent_flagged_decisions"),
            }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
