"""
Prompt template loading and assembly.

The plugin's prompts/ directory holds 8 markdown fragments. This module:
  1. Loads each template lazily and caches it
  2. Assembles final prompts by substituting {NAMED_PLACEHOLDERS}
  3. Drops empty placeholder sections (along with their preceding header)
  4. Tags multi-source [Modification suggestions] with <source: ...> blocks

Placeholder convention:
  - {UPPER_SNAKE_CASE} — substituted from a context dict
  - If value is empty/None, the entire enclosing block (delimited by [SECTION] header
    and following blank line) is omitted to keep prompts compact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


_TEMPLATE_CACHE: dict[str, str] = {}


def load(name: str) -> str:
    """Load a prompt template by name (without .md). Cached."""
    if name in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[name]
    p = PROMPTS_DIR / f"{name}.md"
    if not p.is_file():
        raise FileNotFoundError(f"Prompt template not found: {p}")
    text = p.read_text(encoding="utf-8")
    # Strip the leading <!-- ... --> comment block if present (it's developer notes,
    # not part of the LLM-facing prompt)
    text = re.sub(r"^\s*<!--.*?-->\s*", "", text, count=1, flags=re.DOTALL)
    _TEMPLATE_CACHE[name] = text
    return text


def _drop_empty_sections(text: str) -> str:
    """Remove [SECTION HEADER] paragraphs whose body is empty or only whitespace.

    A section is delimited by a line starting with [Capital] in square brackets,
    spanning until the next [SECTION] header or end-of-file.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\s*\[([A-Z][A-Za-z0-9 \-→/]*)\]", line)
        if m:
            # Find end of this section
            j = i + 1
            body: list[str] = []
            while j < len(lines):
                nxt = lines[j]
                if re.match(r"^\s*\[[A-Z][A-Za-z0-9 \-→/]*\]", nxt):
                    break
                body.append(nxt)
                j += 1
            # If body is empty (after stripping whitespace), drop the section
            joined = "\n".join(body).strip()
            if joined and not _looks_like_unfilled_placeholder(joined):
                out.append(line)
                out.extend(body)
            i = j
        else:
            out.append(line)
            i += 1
    return "\n".join(out)


def _looks_like_unfilled_placeholder(body: str) -> bool:
    """A body that is JUST '{NAME}' or empty after stripping is considered empty."""
    stripped = body.strip()
    if not stripped:
        return True
    if re.fullmatch(r"\{[A-Z_]+\}", stripped):
        return True
    return False


def substitute(template: str, context: dict[str, str]) -> str:
    """Replace {KEY} placeholders with context[key]; missing keys → empty string."""
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        return context.get(key, "")
    text = re.sub(r"\{([A-Z_]+)\}", repl, template)
    return _drop_empty_sections(text)


def filter_common_header_by_symbols(common_header: str, keep_symbols: set[str]) -> str:
    """Shrink a common.header by dropping extern function declarations whose name
    is not in keep_symbols.

    Preserves: leading import line, /* */ comment blocks, #define macros, enums,
    typedefs, struct forward declarations, extern *variable* declarations
    (no parens). Only `extern <ret> <name>(...);` function declarations are
    candidates for elision.

    Used by Loop B codegen and Loop A spec-refine when the [RELY] symbol set is
    known. For first-pass Loop A generation the set is unknown and the full
    header is passed.
    """
    if not keep_symbols:
        return common_header
    pattern = re.compile(r"extern\s+[^;{}]+;", re.DOTALL)

    def repl(match: re.Match[str]) -> str:
        stmt = match.group(0)
        fm = re.search(r"\b(\w+)\s*\(", stmt)
        if not fm:
            return stmt
        name = fm.group(1)
        return stmt if name in keep_symbols else ""

    text = pattern.sub(repl, common_header)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text


def extract_rely_symbols(spec_text: str) -> set[str]:
    """Extract identifiers referenced in a spec's [RELY] block. Used to drive
    filter_common_header_by_symbols when shrinking prompts for refine/code-gen
    rounds where the spec is approved."""
    m = re.search(r"\[RELY\](.*?)(?=^\[[A-Z])", spec_text, re.DOTALL | re.MULTILINE)
    if not m:
        return set()
    body = m.group(1)
    return set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", body))


# ---- Top-level assembly helpers ----------------------------------------------


@dataclass
class FailureNote:
    layer: str       # "lsp" | "compile" | "build" | "qemu" | "speceval" | "user"
    payload: str


def assemble_codegen_prompt(
    *,
    module: str,
    spec_content: str,
    common_header: str,
    inherited_invariants: list[dict[str, str]],
    prior_code_interface: str,
    style_rules: Optional[str] = None,
    linux_to_liteos_table: Optional[str] = None,
    format_traps: Optional[str] = None,
    ask_first_rules: Optional[str] = None,
    previous_code: str = "",
    failures: Optional[list[FailureNote]] = None,
    user_clarifications: Optional[list[dict[str, str]]] = None,
) -> str:
    """Assemble the Loop B codegen prompt from prompts/codegen.md.

    Default code-gen injects the compact LITEOS_DIGEST (~1.7K). The four big
    legacy fragments (style_rules / linux_to_liteos_table / format_traps /
    ask_first_rules) are kept as opt-in kwargs for the lazy-injection retry
    path: when a SpecEval / compile / build round flags a specific class, the
    plugin passes the matching detailed fragment to expand the digest.
    """
    template = load("codegen")
    liteos_digest = load("liteos_digest")

    inv_block = _render_invariants(inherited_invariants)
    refine_block = _render_failures(failures or [])

    ctx = {
        "MODULE": module,
        "LITEOS_DIGEST": liteos_digest,
        # Lazy-injected detailed fragments (empty by default; opt-in on retry).
        "STYLE_RULES": style_rules or "",
        "LINUX_TO_LITEOS_TABLE": linux_to_liteos_table or "",
        "FORMAT_TRAPS": format_traps or "",
        "ASK_FIRST_RULES": ask_first_rules or "",
        "COMMON_HEADER": common_header.strip() or "(empty — first stage)",
        "INHERITED_INVARIANTS": inv_block,
        "PRIOR_CODE_INTERFACE": prior_code_interface or "(none — first stage)",
        "ORIG_SPEC_CONTENT": spec_content.strip(),
        "PREVIOUS_CODE": previous_code,
        "REFINE_SPEC": refine_block,
    }
    return substitute(template, ctx)


def assemble_linux_to_spec_prompt(
    *,
    module: str,
    linux_path: str,
    target_stage: str,
    sub_path: str,
    common_header: str,
    inherited_invariants: list[dict[str, str]],
    prior_spec_index: str,
    ask_first_rules: Optional[str] = None,
    user_clarifications: Optional[list[dict[str, str]]] = None,
    user_suggestions: Optional[list[str]] = None,
    previous_spec: str = "",
) -> str:
    """Assemble the Loop A spec-drafting prompt from prompts/linux_to_spec.md.

    Spec stage is intentionally LEAN — no style/map/traps. Those are code-stage
    concerns and leak implementation choices into the spec when injected here,
    inflating spec LOC. Only ask-first rules (for disambiguation) are loaded.
    """
    template = load("linux_to_spec")
    ask_first_rules = ask_first_rules if ask_first_rules is not None else load("ask_first_rules")

    ctx = {
        "MODULE": module,
        "LINUX_PATH": linux_path,
        "TARGET_STAGE": target_stage,
        "SUB_PATH": sub_path,
        "OP": target_stage,
        "ASK_FIRST_RULES": ask_first_rules,
        "COMMON_HEADER": common_header.strip() or "(empty — first stage)",
        "INHERITED_INVARIANTS": _render_invariants(inherited_invariants),
        "PRIOR_SPEC_INDEX": prior_spec_index or "(none)",
        "USER_CLARIFICATIONS": _render_clarifications(user_clarifications or []),
        "USER_SUGGESTIONS": _render_suggestions(user_suggestions or []),
        "PREVIOUS_SPEC": previous_spec,
    }
    return substitute(template, ctx)


def assemble_speceval_prompt(*, generated_code: str, original_spec: str) -> str:
    """Layer 3 SpecEvaluator prompt from prompts/speceval.md."""
    template = load("speceval")
    return substitute(template, {
        "GENERATED_CODE": generated_code.strip(),
        "ORIGINAL_SPEC": original_spec.strip(),
    })


def assemble_spec_fine_prompt(*, original_spec: str, speceval_comments: str) -> str:
    """F3 SpecFine prompt — polishes existing spec from SpecEval comments.

    Used when SpecEval flags a defect rooted in the spec (not the generated
    code). Hard cap on 3 rounds is enforced server-side, not in the prompt.
    """
    template = load("spec_fine")
    return substitute(template, {
        "ORIGINAL_SPEC": original_spec.strip(),
        "SPECEVAL_COMMENTS": speceval_comments.strip() or "(empty — no comments captured)",
    })


def assemble_unittest_gen_prompt(
    *,
    generated_code: str,
    original_spec: str,
    harness_layout: str,
) -> str:
    """Layer T (v0.3.4) cmocka test synthesis prompt from prompts/unittest_gen.md.

    Pulled out of the docs into actual server-side wiring: was advertised in the
    v0.3.2 CHANGELOG as 'Spec-derived cmocka test generation (default ON)' but
    no MCP tool consumed this template until v0.3.4. See commit 149487a9 for
    the Wave A test-debt catch-up that motivated wiring it.

    harness_layout: a snapshot of testsuites/unittest/<module>/ contents so the
    prompt can pin file naming + extern-decl conventions to what already exists.
    """
    template = load("unittest_gen")
    return substitute(template, {
        "GENERATED_CODE": generated_code.strip(),
        "ORIGINAL_SPEC": original_spec.strip(),
        "HARNESS_LAYOUT": harness_layout.strip() or "(harness directory not found)",
    })


def assemble_style_audit_prompt(*, generated_code: str, auto_checks: str = "") -> str:
    """Layer S coding-style audit prompt from prompts/style_audit.md.

    auto_checks: pre-collected output from runtime tools (clang-format dry-run,
    libsec scan, length/complexity heuristics) — passed verbatim into the
    [AUTO CHECKS] segment so the LLM judges with full context. Empty string
    is acceptable when no static tool ran (LLM does pure self-judge).
    """
    template = load("style_audit")
    return substitute(template, {
        "GENERATED_CODE": generated_code.strip(),
        "AUTO_CHECKS": auto_checks.strip() or "(no auto checks ran — pure LLM self-judge)",
    })


# ---- Sub-renderers -----------------------------------------------------------


def _render_invariants(invs: list[dict[str, str]]) -> str:
    if not invs:
        return ""
    lines: list[str] = []
    for inv in invs:
        lines.append(f"- (id={inv.get('id', '?')}) {inv.get('text', '').strip()}")
    return "\n".join(lines)


def _render_clarifications(clarifications: list[dict[str, str]]) -> str:
    if not clarifications:
        return ""
    lines: list[str] = []
    for c in clarifications:
        q = c.get("question", "").strip()
        a = c.get("user_answer", "").strip()
        lines.append(f"Q: {q}\nA: {a}")
    return "\n\n".join(lines)


def _render_suggestions(suggestions: list[str]) -> str:
    if not suggestions:
        return ""
    return "\n\n".join(f"- {s.strip()}" for s in suggestions if s.strip())


def _render_failures(failures: list[FailureNote]) -> str:
    """Multi-source [Modification suggestions] body with <source: X> tags."""
    if not failures:
        return ""
    out: list[str] = []
    for f in failures:
        out.append(f"<source: {f.layer}>\n{f.payload.strip()}\n</source>")
    return "\n\n".join(out)
