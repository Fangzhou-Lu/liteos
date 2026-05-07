#!/usr/bin/env python3
"""A/B 测试评估 driver。

用法：
    python3 ab_run.py <baseline.json> <experiment.json> [--gate-only]

输入两份 metrics JSON（来自 collect.py），按 AB_PROTOCOL.md 4 个硬门 +
6 个改进信号判定优化是否合入。

退出码：
    0  通过 4 门 + ≥1 信号 → 合入
    1  4 门有失败 → 必须 revert
    2  4 门通过但无改进信号 → 不合入（中性）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---- AB_PROTOCOL.md 门标准 -----------------------------------------------

# ---- baseline backward-compat: reclassify legacy IDs by prefix ------------
# When a baseline JSON predates the v0.4 classifier (no behavioral_invariant_ids
# field), derive its behavioral subset via the same ID-prefix rule that
# collect.py applies. Prefers Linux exFAT public-header anchors + VFS VOP
# names. Implementation-class IDs (orchestrator internals, helper-only
# concerns) are excluded — they were misclassified pre-v0.4 and should not
# block the gate.

_LEGACY_VOP_PREFIXES = {
    "mount", "umount", "lookup", "open", "close", "read", "write",
    "mkdir", "unlink", "rmdir", "rename", "truncate", "getattr",
    "setattr", "statfs", "sync", "readdir", "fsync", "reclaim", "seek",
    "create",
}
_LEGACY_LINUX_PUBLIC_PREFIXES = {
    "zeroed-cluster", "set-volume-dirty", "clear-volume-dirty",
    "alloc-cluster", "free-cluster", "ent-set", "ent-get",
    "count-ext-entries", "load-bitmap", "free-bitmap",
    "set-bitmap", "clear-bitmap", "count-used-clusters",
    "find-last-cluster", "count-num-clusters",
}


def _id_prefix_class(inv_id: str) -> str:
    """Return 'behavioral' or 'implementation' from ID prefix only."""
    if not inv_id.startswith("exfat-"):
        return "implementation"
    rest = inv_id[6:]
    for p in sorted(_LEGACY_VOP_PREFIXES | _LEGACY_LINUX_PUBLIC_PREFIXES,
                    key=len, reverse=True):
        if rest == p or rest.startswith(p + "-"):
            return "behavioral"
    return "implementation"


def _behavioral_set(rec: dict) -> set:
    if "behavioral_invariant_ids" in rec and rec["behavioral_invariant_ids"]:
        return set(rec["behavioral_invariant_ids"])
    legacy = rec.get("spec_invariant_ids", []) or rec.get("module_invariant_ids", [])
    return {i for i in legacy if _id_prefix_class(i) == "behavioral"}


GATES = [
    ("cmocka_pass",       "硬门 1：cmocka 通过",
     lambda b, e: e.get("cmocka_pass") is True),
    ("kernel_build_pass", "硬门 2：kernel build 通过",
     lambda b, e: e.get("kernel_build_pass") is True),
    ("invariant_preserve","硬门 3：baseline behavioral invariants 全部保留",
     # v0.4: behavioral set 比对 (impl-class IDs 是 noise, 不阻门).
     # Baseline 缺字段时, _behavioral_set 按 ID 前缀派生 (Linux exFAT 公共
     # 接口 + VFS VOP 锚点).
     lambda b, e: _behavioral_set(b) <= _behavioral_set(e)),
    ("coverage_preserve", "硬门 4：testpoint ≥ baseline × 0.8",
     lambda b, e: (e.get("testpoint_count") or 0) >=
                  0.8 * (b.get("testpoint_count") or 0)),
]

SIGNALS = [
    ("prompt_chars",            -0.20,  "prompt 字符 ≥20% 减少"),
    ("wall_min",                -0.20,  "端到端时间 ≥20% 减少"),
    ("agent_flagged_decisions", -0.50,  "agent 派生决策 ≥50% 减少（绝对值≤1）"),
    ("ask_first_questions",     -0.50,  "用户决策 ≥50% 减少（绝对值≤1）"),
    ("speceval_rounds",         -1.0,   "SpecEval 减少 ≥1 轮"),
    ("total_tokens",            -0.25,  "agent 总 token ≥25% 减少"),
]


def _eval_signal(key: str, threshold: float, baseline: dict, experiment: dict) -> dict:
    bv, ev = baseline.get(key), experiment.get(key)
    if bv is None or ev is None:
        return {"key": key, "status": "skip", "reason": "missing data",
                "baseline": bv, "experiment": ev}
    if isinstance(bv, bool) or isinstance(ev, bool):
        return {"key": key, "status": "skip", "reason": "non-numeric"}
    if bv == 0:
        # 绝对值阈值（≤1 时算改进）
        if ev <= 1 and ev < (bv + 1):
            return {"key": key, "status": "pass", "delta_abs": ev - bv,
                    "baseline": bv, "experiment": ev}
        return {"key": key, "status": "fail", "delta_abs": ev - bv,
                "baseline": bv, "experiment": ev}
    delta_pct = (ev - bv) / bv
    if threshold < 0:  # decrease desired
        passed = delta_pct <= threshold
    else:
        passed = delta_pct >= threshold
    return {"key": key, "status": "pass" if passed else "fail",
            "delta_pct": round(delta_pct, 3),
            "baseline": bv, "experiment": ev}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline", help="baseline metrics JSON file")
    ap.add_argument("experiment", help="experiment metrics JSON file")
    ap.add_argument("--gate-only", action="store_true",
                    help="只跑 4 门，不跑信号")
    args = ap.parse_args()

    b_data = json.loads(Path(args.baseline).read_text())
    e_data = json.loads(Path(args.experiment).read_text())
    # Allow either single-stage record or {stages: [...]} bundle (pick first).
    b = b_data["stages"][0] if "stages" in b_data else b_data
    e = e_data["stages"][0] if "stages" in e_data else e_data

    print(f"## A/B 报告：{b.get('stage', '?')} → {e.get('stage', '?')}\n")

    print("### 4 道硬门\n")
    print("| # | 门 | 结果 |")
    print("|---|---|---|")
    gate_fail = False
    for key, label, fn in GATES:
        try:
            ok = fn(b, e)
        except Exception as ex:  # noqa: BLE001
            ok = False
            label = f"{label} (eval err: {ex})"
        mark = "PASS" if ok else "FAIL"
        if not ok:
            gate_fail = True
        print(f"| {key} | {label} | {mark} |")
    print()

    if args.gate_only:
        return 0 if not gate_fail else 1

    print("### 6 个改进信号\n")
    print("| 指标 | baseline | experiment | Δ | 阈值 | 结果 |")
    print("|---|---|---|---|---|---|")
    pass_count = 0
    skip_count = 0
    for key, threshold, label in SIGNALS:
        r = _eval_signal(key, threshold, b, e)
        bv, ev = r.get("baseline"), r.get("experiment")
        delta = (r.get("delta_pct") and f"{r['delta_pct']:+.1%}") or \
                (r.get("delta_abs") is not None and str(r['delta_abs'])) or "-"
        thr = f"≥{abs(threshold):.0%} 减少" if isinstance(threshold, float) else str(threshold)
        status = r["status"].upper()
        if r["status"] == "pass":
            pass_count += 1
        elif r["status"] == "skip":
            skip_count += 1
        print(f"| {key} | {bv} | {ev} | {delta} | {thr} | {status} |")
    print()
    print(f"信号通过：{pass_count} / {len(SIGNALS)}（{skip_count} 项数据缺失跳过）")

    print("\n### 合入决策\n")
    if gate_fail:
        print("**REJECT** — 至少 1 道硬门失败；优化必须 revert。")
        return 1
    if pass_count == 0:
        print("**REJECT** — 4 门通过但无改进信号；优化无效，不合入。")
        return 2
    print(f"**ACCEPT** — 4 门通过 + {pass_count} 个改进信号；优化合入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
