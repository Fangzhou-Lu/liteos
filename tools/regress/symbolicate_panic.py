#!/usr/bin/env python3
"""
symbolicate_panic.py — resolve LiteOS-A kernel panic dumps to function symbols.

Reads a QEMU serial log (or any file containing a LiteOS-A panic / OsBackTrace
dump) and uses the kernel ELF's debug info to translate each raw kernel-text
address into `function at file:line`.

Patterns recognized (matches `arch/arm/arm/src/los_exc.c` PrintExcInfo strings):

  pc    = 0x40123abc
  klr   = 0x40123abc
  lr    = 0x40123abc
  traceback N -- lr = 0x40123abc    fp = 0x...
  traceback N -- lr = 0x40123abc
  OsBackTrace fp = 0x40123abc

Use cases:
  1. mount/umount panic — feed serial log + OHOS_Image (with -g) → symbol dump
  2. ad-hoc OsBackTrace() prints from a sleeping path
  3. a raw `lr=0x...` echoed by PRINT_ERR diagnostic

Usage:
    python3 symbolicate_panic.py --log /tmp/qemu_serial.log \
        --image out/arm_virt/qemu_small_system_demo/OHOS_Image \
        [--load-addr 0x40000000] [--toolchain llvm|gcc] [--addr2line PATH]

The script first auto-detects the kernel text base from the ELF; pass
`--load-addr` only if the kernel is being relocated (rare on LiteOS-A which
has a fixed link address).

Exit codes: 0 = at least one address resolved, 1 = no addresses found,
2 = toolchain or ELF error.
"""
from __future__ import annotations

import argparse
import bisect
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Iterable


# Code-address patterns only (registers that hold instruction pointers).
# FP/SP/KSP/USP are deliberately excluded — they're data, not code, and
# addr2line on them yields garbage. We *do* still recognize traceback rows
# even when they include a trailing fp= field; we just ignore the fp value.
CODE_ADDR_PATTERNS = [
    # traceback row: `traceback N -- lr = 0x...    [fp = 0x...]`
    # Matched first so it wins over the bare-register pattern below.
    re.compile(r"traceback\s+\d+\s*--\s*lr\s*=\s*0x(?P<addr>[0-9a-fA-F]{6,8})"),
    # Bare register dump: `pc = 0x...`, `klr = 0x...`, `ulr = 0x...`, `lr = 0x...`
    re.compile(r"\b(?P<reg>pc|klr|ulr|lr)\b\s*=\s*0x(?P<addr>[0-9a-fA-F]{6,8})"),
]


def detect_toolchain(explicit: str | None) -> tuple[str, str]:
    """Return (addr2line, objdump) cmd names. Prefer llvm-*; fall back to gcc."""
    if explicit == "gcc":
        cands = [("arm-linux-gnueabi-addr2line", "arm-linux-gnueabi-objdump"),
                 ("arm-none-eabi-addr2line", "arm-none-eabi-objdump"),
                 ("addr2line", "objdump")]
    elif explicit == "llvm" or explicit is None:
        cands = [("llvm-addr2line", "llvm-objdump"),
                 ("addr2line", "objdump")]
    else:
        cands = [(f"{explicit}-addr2line", f"{explicit}-objdump")]
    for a, o in cands:
        if shutil.which(a):
            return a, o
    print(f"ERROR: no addr2line found (tried: {[a for a, _ in cands]}). "
          "Install llvm or pass --addr2line.", file=sys.stderr)
    sys.exit(2)


def parse_objdump_sym_table(text: str) -> list[tuple[int, str]]:
    """Parse `llvm-objdump -t` output (a.k.a. `.sym.sorted` files in OH builds).

    Format examples:
        4002be18 g     F .text	00000128 VnodeFree
        4002904c l       .text	00000000 $d.5
        40097d80 g     F .text	000000c4 SysUmount

    Only `F` (function) symbols in `.text` are kept — that's what valid LR/PC
    addresses can land in. Local non-function tags ($a, $d, $t) are ignored.
    """
    syms: list[tuple[int, str]] = []
    for line in text.splitlines():
        m = re.match(
            r"^([0-9a-fA-F]+)\s+\S+\s+(\S+)?\s*\.text\s+[0-9a-fA-F]+\s+(\S+)",
            line,
        )
        if not m:
            continue
        type_field = m.group(2) or ""
        if type_field != "F":
            continue
        addr = int(m.group(1), 16)
        name = m.group(3)
        syms.append((addr, name))
    syms.sort(key=lambda x: x[0])
    return syms


def parse_nm_sym_table(text: str) -> list[tuple[int, str]]:
    """Parse `nm -n -C <elf>` output. Format: `40001000 T _start`."""
    syms: list[tuple[int, str]] = []
    for line in text.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            a = int(parts[0], 16)
        except ValueError:
            continue
        kind = parts[1]
        name = parts[2]
        if kind in ("T", "t", "W", "w"):
            syms.append((a, name))
    syms.sort(key=lambda x: x[0])
    return syms


def load_symbol_table(elf: Path, nm: str | None,
                      symfile: Path | None) -> list[tuple[int, str]]:
    """Build the address→name lookup table.

    Priority: explicit `--symfile` > nm against current ELF.
    `--symfile` is the escape hatch when the ELF was rebuilt and no longer
    matches the panic; pass the matching `.sym.sorted` (objdump -t format)
    or any `nm -n -C` dump from the build that produced the panic.
    """
    if symfile is not None:
        try:
            text = symfile.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"# WARN: cannot read --symfile: {e}", file=sys.stderr)
            return []
        # Auto-detect format: objdump -t lines contain ".text" as a column;
        # nm lines have type as 2nd whitespace-separated token (single char).
        if ".text" in text:
            return parse_objdump_sym_table(text)
        return parse_nm_sym_table(text)

    nm_cmd = nm or shutil.which("llvm-nm") or shutil.which("nm")
    if nm_cmd is None:
        return []
    try:
        out = subprocess.run(
            [nm_cmd, "-n", "-C", str(elf)],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    return parse_nm_sym_table(out)


def nearest_symbol(syms: list[tuple[int, str]], addr: int) -> str | None:
    """Return `<name>+0xOFF` for the closest preceding text symbol, or None."""
    if not syms:
        return None
    addrs = [a for a, _ in syms]
    idx = bisect.bisect_right(addrs, addr) - 1
    if idx < 0:
        return None
    sym_addr, sym_name = syms[idx]
    off = addr - sym_addr
    if off > 0x10000:  # > 64 KiB past any symbol → almost certainly not a real frame
        return None
    return f"{sym_name}+0x{off:x}"


def detect_text_base(elf: Path, objdump: str) -> int | None:
    """Read the .text VMA from objdump -h to confirm the link address."""
    try:
        out = subprocess.run(
            [objdump, "-h", str(elf)],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    for line in out.splitlines():
        # `  1 .text     00012000  40000000  40000000  00010000  2**5`
        m = re.match(r"\s*\d+\s+\.text\s+\S+\s+([0-9a-fA-F]+)\s", line)
        if m:
            return int(m.group(1), 16)
    return None


def extract_addrs(log_text: str) -> "OrderedDict[str, list[str]]":
    """Return ordered map: address(hex, lowercase) → list of context tags.
    Each line contributes at most ONE address to dedupe traceback rows that
    contain both `lr = 0x...` and `fp = 0x...`."""
    found: "OrderedDict[str, list[str]]" = OrderedDict()
    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        for pat in CODE_ADDR_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            addr = m.group("addr").lower()
            ctx = m.groupdict().get("reg") or "lr"
            found.setdefault(addr, []).append(f"{ctx}: {line}")
            break  # one match per line, traceback pattern wins over bare-reg
    return found


def looks_like_kernel_addr(addr_int: int, text_base: int | None) -> bool:
    """LiteOS-A kernel text typically lives in the 0x4xxx_xxxx VA range
    (KERNEL_VMM_BASE = 0x40000000 for arm_virt). User-mode addresses are
    < 0x40000000 and need a different ELF — we skip them in this tool."""
    if text_base is None:
        # Heuristic: ARM Cortex-A LiteOS-A kernel always lives ≥ 0x40000000.
        return 0x40000000 <= addr_int <= 0x7FFFFFFF
    # Allow ±32 MiB slack around .text base.
    return text_base - 0x02000000 <= addr_int <= text_base + 0x10000000


def resolve_addrs(
    addrs: Iterable[str],
    elf: Path,
    addr2line: str,
    slide: int,
) -> dict[str, list[str]]:
    """Run addr2line in a single batch. Returns addr → list of frames
    (multiple when addr2line emits inlined-call chains via -i)."""
    cleaned = []
    for a in addrs:
        try:
            v = int(a, 16) - slide
        except ValueError:
            continue
        cleaned.append(f"0x{v:08x}")
    if not cleaned:
        return {}
    cmd = [addr2line, "-C", "-f", "-i", "-p", "-e", str(elf)] + cleaned
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.SubprocessError as e:
        print(f"ERROR: addr2line failed: {e}", file=sys.stderr)
        sys.exit(2)
    if proc.returncode != 0:
        print(f"ERROR: addr2line stderr:\n{proc.stderr}", file=sys.stderr)
        sys.exit(2)

    # `-i -p` produces one frame per line; inlined calls share the addr index
    # via lines starting with " (inlined by) ". We accumulate per input address.
    out_lines = proc.stdout.splitlines()
    result: dict[str, list[str]] = {a: [] for a in addrs}
    addr_iter = iter(addrs)
    cur_addr: str | None = None
    for line in out_lines:
        if line.startswith(" (inlined by) "):
            if cur_addr is not None:
                result[cur_addr].append(line.strip())
            continue
        # New top-of-frame for the next input address
        try:
            cur_addr = next(addr_iter)
        except StopIteration:
            cur_addr = None
        if cur_addr is not None:
            result[cur_addr].append(line.strip())
    return result


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Symbolicate a LiteOS-A kernel panic / backtrace dump.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--log", required=True, type=Path,
                   help="QEMU serial log or any file containing the panic dump")
    p.add_argument("--image", required=True, type=Path,
                   help="kernel ELF with DWARF (e.g. OHOS_Image, NOT .bin)")
    p.add_argument("--load-addr", type=lambda s: int(s, 0), default=None,
                   help="kernel runtime load addr (only if relocated; "
                        "LiteOS-A normally is link=load so omit this)")
    p.add_argument("--toolchain", choices=("llvm", "gcc"), default=None,
                   help="prefer llvm-* or gcc cross-tools (auto-detect if absent)")
    p.add_argument("--addr2line", default=None,
                   help="explicit addr2line path (overrides --toolchain)")
    p.add_argument("--objdump", default=None,
                   help="explicit objdump path (used for .text base detection)")
    p.add_argument("--nm", default=None,
                   help="explicit nm path (used as fallback when DWARF is "
                        "incomplete — auto-detected via llvm-nm/nm)")
    p.add_argument("--symfile", type=Path, default=None,
                   help="pre-extracted symbol table (objdump -t or nm -n -C "
                        "output, e.g. OHOS_Image.sym.sorted). USE THIS when "
                        "the current ELF has been rebuilt and no longer "
                        "matches the panic addresses — pass the .sym.sorted "
                        "saved alongside the matching .bin.")
    p.add_argument("--quiet", action="store_true",
                   help="suppress raw log echo; only print resolved frames")
    args = p.parse_args(argv)

    if not args.log.is_file():
        print(f"ERROR: log file not found: {args.log}", file=sys.stderr)
        return 2
    if not args.image.is_file():
        print(f"ERROR: image file not found: {args.image}", file=sys.stderr)
        return 2

    if args.addr2line:
        addr2line = args.addr2line
        objdump = args.objdump or shutil.which("llvm-objdump") or shutil.which("objdump") or "objdump"
    else:
        addr2line, objdump = detect_toolchain(args.toolchain)
        if args.objdump:
            objdump = args.objdump

    text_base = detect_text_base(args.image, objdump)
    if text_base is not None and not args.quiet:
        print(f"# kernel .text VMA from ELF: 0x{text_base:08x}")

    log_text = args.log.read_text(encoding="utf-8", errors="replace")
    raw_addrs = extract_addrs(log_text)
    if not raw_addrs:
        print("No kernel-text addresses found in log. "
              "Patterns scanned: pc/klr/lr/fp = 0x..., traceback N -- lr = 0x...",
              file=sys.stderr)
        return 1

    # Filter to plausibly-kernel addresses
    kernel_addrs = []
    skipped: list[str] = []
    for a in raw_addrs:
        if looks_like_kernel_addr(int(a, 16), text_base):
            kernel_addrs.append(a)
        else:
            skipped.append(a)

    if not kernel_addrs:
        print(f"Found {len(raw_addrs)} addresses but none are in the kernel "
              f"text range (text_base=0x{text_base or 0:08x}). "
              "These look like user-mode — re-run against the user binary's ELF.",
              file=sys.stderr)
        for a in skipped[:10]:
            print(f"  skipped: 0x{a}", file=sys.stderr)
        return 1

    slide = 0
    if args.load_addr is not None and text_base is not None:
        slide = args.load_addr - text_base
        if not args.quiet:
            print(f"# applying slide: runtime=0x{args.load_addr:08x} "
                  f"link=0x{text_base:08x} delta=0x{slide:x}")

    resolved = resolve_addrs(kernel_addrs, args.image, addr2line, slide)

    # Fallback: ELF symbol table for addresses where DWARF is sparse.
    # On LiteOS-A only some compilation units are built with -g, so
    # `addr2line` returns "?? at ??:0" or a nearest-but-wrong symbol for
    # the rest. nm-based nearest-preceding-symbol is reliable in those cases.
    syms = load_symbol_table(args.image, args.nm, args.symfile)
    if not syms and not args.quiet:
        print("# WARN: symbol table empty — fallback resolution unavailable",
              file=sys.stderr)
    elif args.symfile and not args.quiet:
        print(f"# using --symfile {args.symfile} for symbol resolution "
              f"({len(syms)} text symbols loaded)")

    print()
    print("=" * 78)
    print(f"Symbolicated callstack ({len(kernel_addrs)} kernel frame(s))")
    print("=" * 78)
    for i, addr in enumerate(kernel_addrs, 1):
        frames = resolved.get(addr, ["?? at ??:0"])
        ctx_tags = raw_addrs[addr]
        # Top-of-frame on its own line; inlined frames indented.
        print(f"\n#{i:02d}  0x{addr}")
        for tag in ctx_tags:
            print(f"     ({tag})")
        # Always also compute symtable-based answer; show it when addr2line
        # didn't produce file:line info (the `??:0` marker), OR when the
        # caller explicitly passed --symfile (trusts the table over the ELF
        # since the ELF is known not to match the panic).
        sym_label = nearest_symbol(syms, int(addr, 16) - slide)
        for j, frame in enumerate(frames):
            prefix = "   →" if j == 0 else "    ↳"
            print(f"     {prefix} {frame}")
        if sym_label and (args.symfile is not None
                          or "??:0" in frames[0]
                          or " at ??" in frames[0]):
            tag = "sym" if args.symfile is not None else "nm"
            print(f"      [{tag}] {sym_label}")
    if skipped and not args.quiet:
        print()
        print(f"# skipped {len(skipped)} non-kernel addr(s) "
              "(likely user-mode — symbolicate against the user ELF separately)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
