#!/usr/bin/env python3
"""Ubuntu exFAT LTP baseline runner — companion to QEMU LiteOS-A lane.

Runs the same caselist as out/.../ltp_pack/run_exfat.sh against a loop-back
exfat image mounted via exfat-fuse inside the privileged oh-dev container.
Produces a JSON RC table at out/ubuntu_ltp_baseline.json suitable for
diffing against the QEMU lane.

Pre-requisites (inside container):
  * mkfs.exfat / losetup / mount.exfat-fuse on PATH
  * /dev/loop-control + /dev/fuse accessible (privileged or cap_sys_admin)
  * LTP binaries pre-built per-arch at /work/ltp-aarch64/testcases/...

Invocation from host:
  docker exec oh-dev /home/openharmony/kernel/liteos_a/tools/regress/ubuntu_ltp_baseline.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path("/home/openharmony")
KERNEL_ROOT = REPO_ROOT / "kernel/liteos_a"
CASELIST = KERNEL_ROOT / "tools/regress/ltp_exfat_caselist.txt"
LTP_TREE = Path("/work/ltp-aarch64")
SYSCALL_DIR = LTP_TREE / "testcases/kernel/syscalls"
DEFAULT_IMG = Path("/tmp/ubuntu-exfat-baseline/exfat.img")
DEFAULT_MNT = Path("/tmp/ubuntu-exfat-baseline/mnt")
DEFAULT_OUT = REPO_ROOT / "out/ubuntu_ltp_baseline.json"
DEFAULT_IMG_MIB = 64


def log(msg: str) -> None:
    print(f"[ubuntu-baseline] {msg}", flush=True)


def run(cmd, *, check=True, capture=False, timeout=None) -> subprocess.CompletedProcess:
    log(f"$ {' '.join(str(x) for x in cmd)}")
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )


def load_caselist() -> list[str]:
    if not CASELIST.exists():
        sys.exit(f"caselist missing: {CASELIST}")
    cases: list[str] = []
    for raw in CASELIST.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(line)
    if not cases:
        sys.exit(f"caselist empty: {CASELIST}")
    return cases


def resolve_binary(case: str) -> Optional[Path]:
    # "creat01" -> "creat", "unlink05" -> "unlink", "mkdir02" -> "mkdir"
    base = case.rstrip("0123456789")
    candidate = SYSCALL_DIR / base / case
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def setup_loop_mount(img: Path, mnt: Path, img_mib: int) -> str:
    img.parent.mkdir(parents=True, exist_ok=True)
    mnt.mkdir(parents=True, exist_ok=True)
    if img.exists():
        img.unlink()
    run(["truncate", "-s", f"{img_mib}M", str(img)])
    run(["mkfs.exfat", "-L", "LTPBASE", str(img)], capture=True)
    cp = run(["losetup", "-f", "--show", str(img)], capture=True)
    loop = cp.stdout.strip()
    if not loop.startswith("/dev/loop"):
        sys.exit(f"losetup unexpected output: {loop!r}")
    run(["mount.exfat-fuse", loop, str(mnt)])
    return loop


def teardown(loop: str, mnt: Path) -> None:
    try:
        run(["umount", str(mnt)], check=False)
    finally:
        run(["losetup", "-d", loop], check=False)


def run_case(case: str, mnt: Path, per_case_timeout: int) -> dict:
    binary = resolve_binary(case)
    if binary is None:
        return {
            "case": case,
            "rc": None,
            "skipped": True,
            "reason": "binary missing",
            "stdout_tail": "",
        }
    env = os.environ.copy()
    env["TMPDIR"] = str(mnt)
    start = time.time()
    try:
        cp = subprocess.run(
            [str(binary)],
            env=env,
            capture_output=True,
            text=True,
            timeout=per_case_timeout,
        )
        rc = cp.returncode
        duration = time.time() - start
        tail = "\n".join(cp.stdout.splitlines()[-12:])
        log(f"  {case}: RC={rc} ({duration:.2f}s)")
        return {
            "case": case,
            "rc": rc,
            "skipped": False,
            "duration_s": round(duration, 3),
            "stdout_tail": tail,
            "stderr_tail": "\n".join(cp.stderr.splitlines()[-6:]),
        }
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        log(f"  {case}: TIMEOUT @ {duration:.1f}s")
        return {
            "case": case,
            "rc": None,
            "skipped": False,
            "timeout": True,
            "duration_s": round(duration, 3),
            "stdout_tail": "",
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Ubuntu exFAT LTP baseline runner")
    ap.add_argument("--img", type=Path, default=DEFAULT_IMG)
    ap.add_argument("--mnt", type=Path, default=DEFAULT_MNT)
    ap.add_argument("--img-mib", type=int, default=DEFAULT_IMG_MIB)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--per-case-timeout", type=int, default=30,
                    help="seconds per testcase (default 30; matches QEMU lane)")
    args = ap.parse_args()

    for tool in ("mkfs.exfat", "losetup", "mount.exfat-fuse", "umount", "truncate"):
        if shutil.which(tool) is None:
            sys.exit(f"missing tool: {tool}")
    if not SYSCALL_DIR.is_dir():
        sys.exit(f"per-arch LTP tree missing: {SYSCALL_DIR}")

    cases = load_caselist()
    log(f"loaded {len(cases)} testcases from {CASELIST.name}")

    loop = setup_loop_mount(args.img, args.mnt, args.img_mib)
    log(f"loop={loop} mnt={args.mnt}")

    results: list[dict] = []
    try:
        for case in cases:
            results.append(run_case(case, args.mnt, args.per_case_timeout))
    finally:
        teardown(loop, args.mnt)

    summary = {
        "lane": "ubuntu-exfat-fuse-baseline",
        "kernel": subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip(),
        "exfat_driver": "exfat-fuse (userspace)",
        "ltp_tree": str(LTP_TREE),
        "caselist_path": str(CASELIST),
        "case_count": len(cases),
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    log(f"wrote {args.out}")

    rc_counts: dict[str, int] = {}
    for r in results:
        key = "TIMEOUT" if r.get("timeout") else \
              "SKIP" if r["skipped"] else f"RC={r['rc']}"
        rc_counts[key] = rc_counts.get(key, 0) + 1
    log("summary: " + " ".join(f"{k}={v}" for k, v in sorted(rc_counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
