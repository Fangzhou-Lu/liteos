#!/usr/bin/env python3
"""Long-running QEMU+GDB interactive debug harness for LiteOS-A.

Spawns qemu-system-arm with -S -gdb tcp::1234 and launches gdb-multiarch
via pygdbmi. Stays alive in a control loop, polling a JSON command file
for instructions (break, continue, info, eval, send_serial, snapshot, quit).
Replies are appended to a JSON results file. Stop events are also recorded.

Designed for AI agents to drive interactively without owning the tty.

Files (inside container, all under /home/openharmony/out/):
  qemu_gdb_cmd.jsonl       (input)  - one JSON command per line
  qemu_gdb_reply.jsonl     (output) - one JSON reply per processed command
  qemu_gdb_events.jsonl    (output) - one JSON entry per *stopped event
  qemu_gdb_serial.log      (output) - QEMU serial console capture
  qemu_gdb_run.log         (output) - harness diagnostic log

Command schema (one JSON object per line in qemu_gdb_cmd.jsonl):
  {"id": "<unique>", "op": "gdb",          "cmd": "<gdb/MI or CLI>"}
  {"id": "<unique>", "op": "serial",       "line": "<text to send to guest tty>"}
  {"id": "<unique>", "op": "snapshot"}
  {"id": "<unique>", "op": "wait_stopped", "timeout": <seconds>}
  {"id": "<unique>", "op": "quit"}
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import threading
from pathlib import Path

from pygdbmi.gdbcontroller import GdbController

REPO = Path("/home/openharmony")
OUT = REPO / "out/arm_virt/qemu_small_system_demo"
BIOS = OUT / "OHOS_Image.bin"
ELF = OUT / "liteos"
FLASH_SRC = REPO / "out/smallmmc_exfat.img"
FLASH_WORK = REPO / "out/smallmmc_gdbi.work.img"
SERIAL_LOG = REPO / "out/qemu_gdb_serial.log"
RUN_LOG = REPO / "out/qemu_gdb_run.log"
CMD_FILE = REPO / "out/qemu_gdb_cmd.jsonl"
REPLY_FILE = REPO / "out/qemu_gdb_reply.jsonl"
EVENTS_FILE = REPO / "out/qemu_gdb_events.jsonl"
FIFO_IN = Path("/tmp/qemu_gdbi_stdin.fifo")
GDB_PORT = 1234

_state = {"stop_events": [], "lock": threading.Lock(), "quit": False}


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    with open(RUN_LOG, "a") as f:
        f.write(line)


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
        "-S", "-gdb", f"tcp::{GDB_PORT}",
    ]
    log("spawning qemu (paused, gdbstub on :1234)")
    serial_f = open(SERIAL_LOG, "wb")
    fifo_r = open(FIFO_IN, "rb+", buffering=0)
    proc = subprocess.Popen(
        cmd,
        stdin=fifo_r,
        stdout=serial_f,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    proc._fifo_w = open(FIFO_IN, "wb", buffering=0)  # type: ignore
    return proc


def send_serial(qemu: subprocess.Popen, line: str) -> None:
    fifo = getattr(qemu, "_fifo_w", None)
    if fifo:
        fifo.write((line + "\n").encode("utf-8"))


def append_jsonl(path: Path, obj: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def gdb_collect_async(gdb: GdbController, timeout: float = 0.2) -> list[dict]:
    """Drain any pending async messages from gdb."""
    try:
        return gdb.get_gdb_response(timeout_sec=timeout, raise_error_on_timeout=False)
    except Exception:
        return []


def gdb_drain_stopped(gdb: GdbController, timeout: float = 0.2) -> list[dict]:
    msgs = gdb_collect_async(gdb, timeout)
    stops: list[dict] = []
    for m in msgs:
        if m.get("type") == "notify" and m.get("message") == "stopped":
            payload = m.get("payload", {}) or {}
            entry = {
                "ts": time.strftime("%H:%M:%S"),
                "reason": payload.get("reason"),
                "frame": payload.get("frame", {}),
                "bkptno": payload.get("bkptno"),
                "thread-id": payload.get("thread-id"),
            }
            stops.append(entry)
            append_jsonl(EVENTS_FILE, entry)
            with _state["lock"]:
                _state["stop_events"].append(entry)
    return stops


def gdb_write(gdb: GdbController, cmd: str, timeout: float = 8.0) -> list[dict]:
    resp = gdb.write(cmd, timeout_sec=timeout, raise_error_on_timeout=False)
    # Pick out any stopped events that came in this batch
    for m in resp:
        if m.get("type") == "notify" and m.get("message") == "stopped":
            payload = m.get("payload", {}) or {}
            entry = {
                "ts": time.strftime("%H:%M:%S"),
                "reason": payload.get("reason"),
                "frame": payload.get("frame", {}),
                "bkptno": payload.get("bkptno"),
                "thread-id": payload.get("thread-id"),
            }
            append_jsonl(EVENTS_FILE, entry)
            with _state["lock"]:
                _state["stop_events"].append(entry)
    return resp


def snapshot(gdb: GdbController) -> dict:
    info: dict = {"ts": time.strftime("%H:%M:%S")}
    for label, cmd in [
        ("regs", "-data-list-register-values x"),
        ("reg_names", "-data-list-register-names"),
        ("backtrace", "-stack-list-frames 0 16"),
        ("args", "-stack-list-arguments --all-values 0 6"),
        ("locals", "-stack-list-locals --all-values"),
    ]:
        resp = gdb_write(gdb, cmd, timeout=8.0)
        for m in resp:
            if m.get("type") == "result" and m.get("payload"):
                info[label] = m["payload"]
                break
    return info


def process_cmd(gdb: GdbController, qemu: subprocess.Popen, cmd: dict) -> dict:
    op = cmd.get("op")
    rid = cmd.get("id", "?")
    if op == "gdb":
        resp = gdb_write(gdb, cmd["cmd"], timeout=cmd.get("timeout", 10.0))
        return {"id": rid, "op": op, "cmd": cmd["cmd"], "resp": resp[-12:]}
    if op == "serial":
        send_serial(qemu, cmd.get("line", ""))
        return {"id": rid, "op": op, "sent": cmd.get("line", "")}
    if op == "snapshot":
        return {"id": rid, "op": op, "snap": snapshot(gdb)}
    if op == "wait_stopped":
        timeout = float(cmd.get("timeout", 30))
        deadline = time.time() + timeout
        start_count = len(_state["stop_events"])
        while time.time() < deadline:
            stops = gdb_drain_stopped(gdb, 0.5)
            with _state["lock"]:
                if len(_state["stop_events"]) > start_count:
                    return {"id": rid, "op": op,
                            "new_stops": _state["stop_events"][start_count:]}
        return {"id": rid, "op": op, "timeout": True}
    if op == "quit":
        _state["quit"] = True
        return {"id": rid, "op": op, "ack": True}
    return {"id": rid, "op": op, "error": "unknown op"}


def main() -> None:
    for p in [SERIAL_LOG, RUN_LOG, CMD_FILE, REPLY_FILE, EVENTS_FILE]:
        if p.exists():
            p.unlink()
    CMD_FILE.touch()

    log("=== qemu_gdb_interactive START ===")
    qemu = spawn_qemu()
    time.sleep(1)

    log("launching gdb-multiarch (mi3)")
    gdb = GdbController(command=["gdb-multiarch", "--nx", "--quiet", "--interpreter=mi3"])

    gdb_write(gdb, "set pagination off")
    gdb_write(gdb, "set confirm off")
    gdb_write(gdb, f"-file-exec-and-symbols {ELF}")
    gdb_write(gdb, "-gdb-set architecture arm")
    r = gdb_write(gdb, f"-target-select remote localhost:{GDB_PORT}", timeout=15)
    log(f"target-select: {[m.get('message') for m in r[-3:]]}")
    log("ready; awaiting commands on qemu_gdb_cmd.jsonl")

    cmd_offset = 0
    try:
        while not _state["quit"]:
            gdb_drain_stopped(gdb, 0.2)
            try:
                with open(CMD_FILE, "r") as f:
                    f.seek(cmd_offset)
                    new = f.read()
                    cmd_offset = f.tell()
            except FileNotFoundError:
                new = ""
            if new.strip():
                for raw in new.splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        c = json.loads(raw)
                    except Exception as e:
                        append_jsonl(REPLY_FILE, {"error": f"bad json: {e}", "raw": raw})
                        continue
                    log(f"cmd #{c.get('id')} op={c.get('op')}")
                    try:
                        reply = process_cmd(gdb, qemu, c)
                    except Exception as e:
                        reply = {"id": c.get("id"), "op": c.get("op"), "error": str(e)}
                    append_jsonl(REPLY_FILE, reply)
            else:
                time.sleep(0.3)
    finally:
        log("=== shutting down ===")
        try:
            gdb_write(gdb, "-gdb-exit", timeout=3)
        except Exception:
            pass
        try:
            gdb.exit()
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(qemu.pid), signal.SIGTERM)
            qemu.wait(timeout=3)
        except Exception:
            try:
                os.killpg(os.getpgid(qemu.pid), signal.SIGKILL)
            except Exception:
                pass
        log("=== exit ===")


if __name__ == "__main__":
    main()
