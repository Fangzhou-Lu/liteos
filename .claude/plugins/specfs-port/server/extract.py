"""
Regex-based C interface extractor.

Given a frozen .c (or .h) file under fs/<module>/, pull out:
  - Top-level function declarations (signatures only, no body)
  - extern variable declarations
  - struct typedefs (the public ones — those defined OR referenced in headers)
  - FSMAP_ENTRY macro invocations (so we know what FS names are registered)
  - LOSCFG_FS_<NAME> ifdef wrappers (informational)

This is regex-based on purpose: we need a fast, dependency-free extractor
that works with the LiteOS-A coding style without dragging in libclang. The
output feeds the [PRIOR CODE INTERFACE] segment of the codegen prompt.

Limitations:
  - Function pointer typedefs are not extracted
  - Macro-defined declarations are not extracted
  - Multi-line attributes may break the parse (caller can fall back to grep)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# Match a top-level function definition like:
#     int VfsExfatMount(struct Mount *mount, struct Vnode *blk, const void *data)
#     {
# Capture the whole signature (one line ahead of '{').
# We require return type + identifier + parens + open brace on the next line
# (LiteOS-A coding style is K&R-like for top-level funcs).
_FUNC_DEF_RE = re.compile(
    r"^(?P<sig>"
    r"(?:static\s+)?"
    r"(?:inline\s+)?"
    r"(?:[\w*\s]+?)\s+"             # return type (greedy match up to last whitespace before name)
    r"(?P<name>\w+)\s*"             # function name
    r"\([^;{}]*\)"                  # parameter list (no nested parens / no semicolons)
    r")\s*\n\s*\{",                 # opening brace on its own or next line
    re.MULTILINE,
)


_EXTERN_VAR_RE = re.compile(
    r"^extern\s+(?P<decl>[^;{}]+);",
    re.MULTILINE,
)


_FSMAP_ENTRY_RE = re.compile(
    r"FSMAP_ENTRY\s*\(\s*(?P<sym>\w+)\s*,\s*\"(?P<name>[^\"]+)\"\s*,",
)


_PUB_GLOBAL_DECL_RE = re.compile(
    # Match top-level non-static declarations with explicit assignment, e.g.
    #     struct VnodeOps g_exfatVops = {
    # Captures the full declaration up to '='.
    r"^(?!static\s)"
    r"(?P<decl>"
    r"(?:struct|union|enum)\s+\w+\s+"
    r"(?P<name>g_\w+|exfat_\w+)"     # naming heuristic: g_* or exfat_*
    r")\s*=",
    re.MULTILINE,
)


_LOSCFG_GUARD_RE = re.compile(
    r"#ifdef\s+(LOSCFG_FS_\w+)",
)


@dataclass
class ExtractedInterface:
    src_file: str
    functions: list[str] = field(default_factory=list)             # one-line signatures
    fn_names: list[str] = field(default_factory=list)
    extern_vars: list[str] = field(default_factory=list)            # full decls
    fsmap_entries: list[tuple[str, str]] = field(default_factory=list)  # (symbol, fs_name)
    pub_globals: list[str] = field(default_factory=list)            # e.g., 'struct VnodeOps g_exfatVops'
    loscfg_guards: list[str] = field(default_factory=list)


def _normalize_signature(sig: str) -> str:
    """Collapse internal whitespace and ensure single-line."""
    return re.sub(r"\s+", " ", sig).strip()


def extract_from_file(path: Path) -> ExtractedInterface:
    """Parse a single .c or .h file, return its public interface."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return extract_from_text(text, str(path))


def extract_from_text(text: str, src_file: str = "<unknown>") -> ExtractedInterface:
    iface = ExtractedInterface(src_file=src_file)

    for m in _FUNC_DEF_RE.finditer(text):
        sig = _normalize_signature(m.group("sig"))
        # filter out static helpers — we only want public interface
        if sig.startswith("static "):
            continue
        iface.functions.append(sig)
        iface.fn_names.append(m.group("name"))

    for m in _EXTERN_VAR_RE.finditer(text):
        iface.extern_vars.append(_normalize_signature(m.group("decl")))

    for m in _FSMAP_ENTRY_RE.finditer(text):
        iface.fsmap_entries.append((m.group("sym"), m.group("name")))

    for m in _PUB_GLOBAL_DECL_RE.finditer(text):
        iface.pub_globals.append(_normalize_signature(m.group("decl")))

    for m in _LOSCFG_GUARD_RE.finditer(text):
        sym = m.group(1)
        if sym not in iface.loscfg_guards:
            iface.loscfg_guards.append(sym)

    return iface


def extract_module_interface(module: str, repo_root: Path) -> dict[str, ExtractedInterface]:
    """Extract interfaces from all .c and .h files under fs/<module>/.

    Returns dict keyed by file path (relative to repo_root).
    Skips files under fs/<module>_backup/ (those are pre-port references, not frozen contract).
    """
    out: dict[str, ExtractedInterface] = {}
    fs_dir = repo_root / "fs" / module
    if not fs_dir.is_dir():
        return out
    for path in sorted(fs_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in (".c", ".h"):
            continue
        rel = path.relative_to(repo_root)
        out[str(rel)] = extract_from_file(path)
    return out


def render_interface_summary(ifaces: dict[str, ExtractedInterface]) -> str:
    """Render interfaces as a markdown-friendly summary for [PRIOR CODE INTERFACE]
    segment of the codegen prompt."""
    if not ifaces:
        return "(no prior code in fs/<module>/ — this is the first stage)"

    sections: list[str] = []
    for src, iface in sorted(ifaces.items()):
        block: list[str] = [f"// from {src}"]
        if iface.fsmap_entries:
            for sym, name in iface.fsmap_entries:
                block.append(f'FSMAP_ENTRY({sym}, "{name}", ...);')
        for fn in iface.functions:
            block.append(f"{fn};")
        for var in iface.extern_vars:
            block.append(f"extern {var};")
        for g in iface.pub_globals:
            block.append(f"{g} = {{ ... }};")
        if iface.loscfg_guards:
            block.append(f"// guarded by: {', '.join(iface.loscfg_guards)}")
        if len(block) > 1:
            sections.append("\n".join(block))
    return "\n\n".join(sections) if sections else "(no public interface extracted)"


def collect_all_symbols(ifaces: dict[str, ExtractedInterface]) -> set[str]:
    """Flat set of all public function names + extern var names."""
    out: set[str] = set()
    for iface in ifaces.values():
        out.update(iface.fn_names)
        for ext in iface.extern_vars:
            # Try to pull the identifier from "type *name" → "name"
            tok = re.search(r"(\w+)\s*$", ext)
            if tok:
                out.add(tok.group(1))
        for sym, _ in iface.fsmap_entries:
            out.add(sym)
    return out
