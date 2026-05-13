#!/usr/bin/env python3
"""GDB batch trace session for LiteOS-A QEMU.

Starts QEMU with -S -gdb tcp::1234 (paused), connects gdb-multiarch,
sets breakpoints on key syscall + FS functions, continues, waits for
stops, captures state. Writes incremental JSON log. Designed for
nohup background execution (no tty needed).

Usage (inside oh-dev container):
  nohup python3 gdb_trace_session.py > /tmp/gdb_trace.log 2>&1 &
  tail -f /tmp/gdb_trace.log

Then in a second shell window inject LTP via the serial FIFO.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/home/openharmony")
OUT = REPO / "out/arm_virt/qemu_small_system_demo"
BIOS = OUT / "OHOS_Image.bin"
ELF = OUT / "liteos"
FLASH_SRC = REPO / "out/smallmmc_exfat.img"
FLASH_WORK = REPO / "out/smallmmc_gdb.work.img"
SERIAL_LOG = REPO / "out/qemu_gdb_serial.log"
JSON_OUT = REPO / "out/gdb_trace_session.json"
GDB_PORT = 1234
FIFO_IN = Path("/tmp/qemu_gdb_stdin.fifo")

BREAKPOINTS = [
    "SysUtimensat",
    "SysFchmodat",
    "SysFchmod",
    "SysFchownat",
    "SysAccess",
    "VfsExfatChattr",
]

BOOT_NEEDLE = "OHOS:/$"
BOOT_TIMEOUT_S = 90
HIT_TIMEOUT_S = 30
MAX_HITS = 40

def ts() -> str:
    return time.strftime("%H:%M:%S")

def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


def spawn_qemu() -> subprocess.Popen:
    shutil.copy(FLASH_SRC, FLASH_WORK)
    if FIFO_IN.exists():
        FIFO_IN.unlink()
    os.mkfifo(str(FIFO_IN))
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
        "-S",
        "-gdb", f"tcp::{GDB_PORT}",
    ]
    log("spawning qemu: " + " ".join(cmd[-6:]) + " ...")
    serial_log_f = open(SERIAL_LOG, "wb")
    fifo_r = open(FIFO_IN, "rb+", buffering=0)
    proc = subprocess.Popen(
        cmd,
        stdin=fifo_r,
        stdout=serial_log_f,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    proc._fifo_w = open(FIFO_IN, "wb", buffering=0)  # type: ignore
    return proc


def send(proc: subprocess.Popen, line: str) -> None:
    fifo = getattr(proc, "_fifo_w", None)
    if fifo:
        log(f"  > {line!r}")
        fifo.write((line + "\n").encode("utf-8"))


def wait_serial(needle: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = SERIAL_LOG.read_bytes().decode("utf-8", errors="replace")
        except Exception:
            data = ""
        if needle in data:
            return True
        time.sleep(1)
    return False


def gdb_cmd(proc: subprocess.Popen, cmd: str, timeout: float = 8.0) -> list[str]:
    """Send one GDB/MI command, return list of response lines."""
    proc.stdin.write((cmd + "\n").encode("utf-8"))
    proc.stdin.flush()
    lines: list[str] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            continue
        decoded = line.decode("utf-8", errors="replace").rstrip()
        lines.append(decoded)
        if decoded.startswith("(gdb)") or decoded.startswith("^done") or decoded.startswith("^error"):
            break
    return lines


def parse_stopped(lines: list[str]) -> dict:
    for l in lines:
        if "*stopped" in l:
            m = re.search(r'reason="([^"]+)"', l)
            reason = m.group(1) if m else "unknown"
            mf = re.search(r'func="([^"]+)"', l)
            func = mf.group(1) if mf else "?"
            ma = re.search(r'addr="([^"]+)"', l)
            addr = ma.group(1) if ma else "?"
            return {"reason": reason, "func": func, "addr": addr, "raw": l}
    return {}


def regs(gdb: subprocess.Popen) -> dict:
    lines = gdb_cmd(gdb, "-data-list-register-values x 0 1 2 3 13 14 15", timeout=5)
    reg_vals: dict[str, str] = {}
    for l in lines:
        for m in re.finditer(r'number="(\d+)",value="([^"]*)"', l):
            reg_vals[m.group(1)] = m.group(2)
    return reg_vals


def backtrace(gdb: subprocess.Popen) -> list[str]:
    lines = gdb_cmd(gdb, "-stack-list-frames 0 8", timeout=5)
    frames: list[str] = []
    for l in lines:
        for m in re.finditer(r'frame=\{([^}]+)\}', l):
            frames.append(m.group(1))
    return frames


def gdb_connect(qemu: subprocess.Popen) -> subprocess.Popen:
    log(f"launching gdb-multiarch -> tcp::{GDB_PORT}")
    gdb = subprocess.Popen(
        ["gdb-multiarch", "--quiet", "--nx", "--interpreter=mi3"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    assert gdb.stdin and gdb.stdout

    def gc(cmd: str, t: float = 8.0) -> list[str]:
        return gdb_cmd(gdb, cmd, t)

    gc("set pagination off")
    gc("set confirm off")
    gc(f"-file-exec-and-symbols {ELF}")
    gc("-gdb-set architecture arm")
    r = gc(f"-target-select remote localhost:{GDB_PORT}", 15.0)
    log("  connect: " + (r[-1] if r else "(no response)"))
    return gdb


def gdb_arm_breakpoints(gdb: subprocess.Popen) -> None:
    for sym in BREAKPOINTS:
        r = gdb_cmd(gdb, f"-break-insert {sym}", 5.0)
        log(f"  bp {sym}: " + (r[-1] if r else "?"))


def gdb_hit_loop(gdb: subprocess.Popen) -> list[dict]:
    """Continue execution, capture up to MAX_HITS breakpoint hits."""

    def gc(cmd: str, t: float = 8.0) -> list[str]:
        return gdb_cmd(gdb, cmd, t)

    hits: list[dict] = []
    for i in range(MAX_HITS):
        log(f"=== continue (hit #{i+1}) ===")
        gc("-exec-continue", 2.0)
        deadline = time.time() + HIT_TIMEOUT_S
        stop_lines: list[str] = []
        stopped = False
        while time.time() < deadline:
            raw = gdb.stdout.readline()
            if not raw:
                time.sleep(0.05)
                continue
            decoded = raw.decode("utf-8", errors="replace").rstrip()
            stop_lines.append(decoded)
            if "*stopped" in decoded:
                stopped = True
                break
        if not stopped:
            log("  TIMEOUT waiting for stop")
            hits.append({"index": i, "timeout": True})
            break

        info = parse_stopped(stop_lines)
        log(f"  stopped: reason={info.get('reason')} func={info.get('func')} addr={info.get('addr')}")

        snap: dict = {
            "index": i,
            "stop": info,
            "regs": regs(gdb),
            "backtrace": backtrace(gdb),
            "ts": ts(),
        }
        hits.append(snap)

        reason = info.get("reason", "")
        if reason == "exited-normally":
            log("  program exited normally")
            break

    gc("-gdb-exit", 5.0)
    try:
        gdb.wait(timeout=3)
    except Exception:
        gdb.kill()
    return hits


def main() -> None:
    if not BIOS.is_file():
        sys.exit(f"BIOS missing: {BIOS}")

    log("=== gdb trace session start ===")
    session: dict = {"start": ts(), "breakpoints": BREAKPOINTS, "hits": []}
    qemu = spawn_qemu()

    try:
        gdb = gdb_connect(qemu)

        # Boot WITHOUT breakpoints so the kernel reaches the shell.
        log("resuming target to boot (no BPs yet) ...")
        gdb_cmd(gdb, "-exec-continue", 2.0)

        log(f"waiting for shell prompt {BOOT_NEEDLE!r} ({BOOT_TIMEOUT_S}s) ...")
        booted = wait_serial(BOOT_NEEDLE, BOOT_TIMEOUT_S)
        log(f"  shell ready: {booted}")
        if not booted:
            raise RuntimeError("shell prompt never appeared")

        log("interrupting to arm breakpoints ...")
        gdb_cmd(gdb, "-exec-interrupt --all", 3.0)
        time.sleep(0.5)
        # Drain any *stopped from the interrupt.
        deadline = time.time() + 3
        while time.time() < deadline:
            raw = gdb.stdout.readline()
            if not raw:
                time.sleep(0.05)
                continue
            decoded = raw.decode("utf-8", errors="replace").rstrip()
            if "*stopped" in decoded:
                break

        gdb_arm_breakpoints(gdb)

        log("resuming to inject LTP ...")
        gdb_cmd(gdb, "-exec-continue", 2.0)

        time.sleep(1)
        send(qemu, "")
        time.sleep(0.5)
        send(qemu, "mkdir /mnt/exfat 2>/dev/null || true")
        send(qemu, "mount /dev/mmcblk0p4 /mnt/exfat exfat")
        send(qemu, "cd /storage/ltp_pack/bin || cd /userdata/ltp_pack/bin || cd /mnt/ltp_pack/bin")
        time.sleep(1)
        log("=== injecting LTP testcase: chmod01 ===")
        send(qemu, "TMPDIR=/mnt/exfat ./chmod01 2>&1 | head -30")
        time.sleep(0.5)
        log("=== injecting LTP testcase: access01 ===")
        send(qemu, "TMPDIR=/mnt/exfat ./access01 2>&1 | head -30")

        hits = gdb_hit_loop(gdb)
        session["hits"] = hits
        log(f"=== done, {len(hits)} hits ===")

    finally:
        try:
            os.killpg(os.getpgid(qemu.pid), signal.SIGTERM)
            qemu.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(qemu.pid), signal.SIGKILL)
            except Exception:
                pass

        JSON_OUT.write_text(json.dumps(session, indent=2, default=str))
        log(f"wrote {JSON_OUT}")

        if session["hits"]:
            log("=== hit summary ===")
            for h in session["hits"]:
                stop = h.get("stop", {})
                r = h.get("regs", {})
                pc = r.get("15", "?")
                log(f"  #{h['index']} func={stop.get('func','?')} pc={pc} r0={r.get('0','?')} r1={r.get('1','?')} r2={r.get('2','?')} r3={r.get('3','?')}")


if __name__ == "__main__":
    main()
