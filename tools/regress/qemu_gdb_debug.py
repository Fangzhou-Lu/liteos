#!/usr/bin/env python3
"""QEMU + GDB Python debug harness for LiteOS-A exFAT investigation.

Spawns `qemu-system-arm -s -S ...` (gdbstub on TCP 1234, paused at reset),
drives `gdb-multiarch` via pygdbmi MI, sets breakpoints on named symbols
(default: `VfsExfatChattr`, `SysUtimensat`, `chattr`), continues, captures
register / argument / call-site snapshots at each hit, dumps JSON.

Output: out/qemu_gdb_session.json + live stdout log.

Invocation (inside oh-dev container):
  python3 tools/regress/qemu_gdb_debug.py \
    --bp VfsExfatChattr --bp SysUtimensat \
    --max-hits 8 --boot-timeout 60

Default symbols target the chmod01/unlink05 EPERM investigation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from pygdbmi.gdbcontroller import GdbController

REPO_ROOT = Path("/home/openharmony")
OUT = REPO_ROOT / "out/arm_virt/qemu_small_system_demo"
BIOS = OUT / "OHOS_Image.bin"
ELF = OUT / "liteos"
FLASH = REPO_ROOT / "out/smallmmc_exfat.img"
FLASH_WORK = REPO_ROOT / "out/smallmmc_exfat.work.img"
SESSION_OUT = REPO_ROOT / "out/qemu_gdb_session.json"
GDB_PORT = 1234


def log(msg: str) -> None:
    print(f"[qemu-gdb] {msg}", flush=True)


def spawn_qemu(serial_fifo_path: Path, extra_args: Optional[list[str]] = None) -> subprocess.Popen:
    if FLASH.is_file():
        shutil.copy(FLASH, FLASH_WORK)
    if serial_fifo_path.exists():
        serial_fifo_path.unlink()
    os.mkfifo(str(serial_fifo_path))
    cmd = [
        "qemu-system-arm",
        "-M", "virt,gic-version=2,secure=on",
        "-cpu", "cortex-a7",
        "-smp", "cpus=1",
        "-m", "1G",
        "-bios", str(BIOS),
        "-global", "virtio-mmio.force-legacy=false",
        "-nographic",
        "-drive", f"if=none,file={FLASH_WORK},format=raw,id=mmc",
        "-device", "virtio-blk-device,drive=mmc",
        "-device", "virtio-rng-device",
        "-gdb", f"tcp::{GDB_PORT}",
    ]
    if extra_args:
        cmd.extend(extra_args)
    log("spawning: " + " ".join(cmd))
    qemu_log = open(REPO_ROOT / "out/qemu_gdb_serial.log", "wb")
    fifo_r = open(serial_fifo_path, "rb+", buffering=0)
    proc = subprocess.Popen(
        cmd,
        stdin=fifo_r,
        stdout=qemu_log,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    proc._fifo = open(serial_fifo_path, "wb", buffering=0)  # type: ignore[attr-defined]
    proc._serial_log_path = qemu_log.name  # type: ignore[attr-defined]
    return proc


def send_serial(qemu_proc: subprocess.Popen, line: str) -> None:
    fifo = getattr(qemu_proc, "_fifo", None)
    if fifo is None:
        return
    fifo.write((line + "\n").encode("utf-8"))


def wait_for_log(qemu_proc: subprocess.Popen, needle: str, timeout: float) -> bool:
    path = getattr(qemu_proc, "_serial_log_path", None)
    if not path:
        time.sleep(timeout)
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = open(path, "rb").read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            data = ""
        if needle in data:
            return True
        time.sleep(0.5)
    return False


def gdb_cmds(gdb: GdbController, cmds: list[str], timeout: float = 15.0) -> list[dict]:
    out: list[dict] = []
    for c in cmds:
        log(f"$ {c}")
        resp = gdb.write(c, timeout_sec=timeout, raise_error_on_timeout=False)
        out.extend(resp)
    return out


def extract_payloads(resp: list[dict], kind: str) -> list[dict]:
    return [m for m in resp if m.get("type") == kind]


def collect_at_hit(gdb: GdbController) -> dict:
    info: dict = {}
    regs = gdb_cmds(gdb, ["-data-list-register-values x"], timeout=10)
    for m in regs:
        if m.get("type") == "result" and m.get("payload"):
            info["registers"] = m["payload"].get("register-values", [])
            break
    names = gdb_cmds(gdb, ["-data-list-register-names"], timeout=10)
    for m in names:
        if m.get("type") == "result" and m.get("payload"):
            info["register_names"] = m["payload"].get("register-names", [])
            break
    bt = gdb_cmds(gdb, ["-stack-list-frames 0 12"], timeout=10)
    for m in bt:
        if m.get("type") == "result" and m.get("payload"):
            info["backtrace"] = m["payload"].get("stack", [])
            break
    args = gdb_cmds(gdb, ["-stack-list-arguments --all-values 0 4"], timeout=10)
    for m in args:
        if m.get("type") == "result" and m.get("payload"):
            info["arguments"] = m["payload"].get("stack-args", [])
            break
    locs = gdb_cmds(gdb, ["-stack-list-locals --all-values"], timeout=10)
    for m in locs:
        if m.get("type") == "result" and m.get("payload"):
            info["locals"] = m["payload"].get("locals", [])
            break
    info["timestamp"] = time.time()
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bp", action="append", default=[],
                    help="Symbol or *0xaddr breakpoint; repeat. Defaults: VfsExfatChattr, SysUtimensat")
    ap.add_argument("--max-hits", type=int, default=8)
    ap.add_argument("--boot-timeout", type=int, default=60)
    ap.add_argument("--gdb", default="gdb-multiarch")
    ap.add_argument("--no-qemu", action="store_true",
                    help="Don't spawn QEMU; assume gdbstub already on tcp::1234")
    ap.add_argument("--ltp-case", default="chmod01",
                    help="LTP testcase to TMPDIR-run after mount (drives the breakpoint)")
    ap.add_argument("--out", default=str(SESSION_OUT))
    args = ap.parse_args()

    bps = args.bp or ["VfsExfatChattr", "SysUtimensat"]

    if not ELF.is_file():
        sys.exit(f"ELF missing: {ELF}")
    if not args.no_qemu and not BIOS.is_file():
        sys.exit(f"BIOS missing: {BIOS}")

    qemu_proc = None
    serial_fifo = REPO_ROOT / "out/qemu_gdb_stdin.fifo"
    if not args.no_qemu:
        qemu_proc = spawn_qemu(serial_fifo)
        time.sleep(2)

    log(f"launching {args.gdb}")
    gdb = GdbController(command=[args.gdb, "--nx", "--quiet", "--interpreter=mi3"])

    session: dict = {
        "started": time.time(),
        "breakpoints": bps,
        "elf": str(ELF),
        "qemu_port": GDB_PORT,
        "hits": [],
    }

    try:
        gdb_cmds(gdb, [
            "set pagination off",
            "set confirm off",
            "set print pretty on",
            "set print elements 200",
            f"-file-exec-and-symbols {ELF}",
            "-gdb-set architecture arm",
            f"-target-select remote localhost:{GDB_PORT}",
        ], timeout=30)

        for sym in bps:
            location = sym if sym.startswith("*") else sym
            gdb_cmds(gdb, [f"-break-insert {location}"], timeout=10)

        log("=== waiting for boot prompt ===")
        gdb.write("-exec-continue", timeout_sec=2, raise_error_on_timeout=False)
        booted = wait_for_log(qemu_proc, "OHOS:/$", timeout=args.boot_timeout) if qemu_proc else True
        log(f"  boot prompt seen: {booted}")
        if qemu_proc:
            time.sleep(1)
            send_serial(qemu_proc, "")
            time.sleep(0.3)
            send_serial(qemu_proc, "mkdir -p /mnt/exfat")
            time.sleep(0.3)
            send_serial(qemu_proc, "mount -t exfat /dev/mmcblk0p3 /mnt/exfat")
            time.sleep(0.5)
            send_serial(qemu_proc, "cd /storage/ltp_pack/bin")
            time.sleep(0.3)
            send_serial(qemu_proc, f"TMPDIR=/mnt/exfat ./{args.ltp_case}")
        gdb.write("-exec-interrupt", timeout_sec=2, raise_error_on_timeout=False)

        for hit_idx in range(args.max_hits):
            log(f"=== continue, awaiting hit #{hit_idx+1} ===")
            resp = gdb.write("-exec-continue", timeout_sec=args.boot_timeout,
                             raise_error_on_timeout=False)
            stopped = [m for m in resp if m.get("type") == "notify"
                       and m.get("message") == "stopped"]
            if not stopped:
                log("no stop event within timeout — recording last 8 messages")
                session["hits"].append({
                    "index": hit_idx,
                    "timeout": True,
                    "tail": resp[-8:],
                })
                break
            stop = stopped[-1].get("payload", {})
            log(f"  stopped: reason={stop.get('reason')} frame={stop.get('frame', {}).get('func')}")
            snap = collect_at_hit(gdb)
            snap["stop"] = stop
            snap["index"] = hit_idx
            session["hits"].append(snap)

    finally:
        try:
            gdb_cmds(gdb, ["-gdb-exit"], timeout=5)
        except Exception:
            pass
        try:
            gdb.exit()
        except Exception:
            pass
        if qemu_proc is not None:
            try:
                os.killpg(os.getpgid(qemu_proc.pid), signal.SIGTERM)
                qemu_proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(qemu_proc.pid), signal.SIGKILL)
                except Exception:
                    pass

        session["ended"] = time.time()
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(session, f, indent=2, default=str)
        log(f"wrote {args.out}  hits={len(session['hits'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
