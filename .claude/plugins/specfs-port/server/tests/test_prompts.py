"""Unit tests for prompts.py — template loading + assembly + filtering helpers."""
from __future__ import annotations

import pytest


# ---------- template loading ----------

def test_load_caches_template():
    import prompts
    a = prompts.load("codegen")
    b = prompts.load("codegen")
    assert a is b  # cached, same identity


def test_load_strips_leading_html_comment():
    """The <!-- ... --> developer notes block at the top is stripped before
    the template reaches the LLM."""
    import prompts
    text = prompts.load("style_audit")
    # Developer notes in style_audit.md start with <!-- LiteOS-A coding-style
    # audit ... -->. After load() the template should NOT begin with <!--.
    assert not text.lstrip().startswith("<!--"), (
        "load() failed to strip developer-notes <!-- ... --> block"
    )


def test_load_missing_raises():
    import prompts
    with pytest.raises(FileNotFoundError):
        prompts.load("__nonexistent_template__")


# ---------- substitute / drop_empty_sections ----------

def test_substitute_replaces_known_keys():
    import prompts
    template = "Hello {NAME}, you have {COUNT} messages."
    out = prompts.substitute(template, {"NAME": "world", "COUNT": "3"})
    assert "Hello world, you have 3 messages." in out


def test_substitute_unknown_key_becomes_empty():
    import prompts
    template = "value: {UNKNOWN}"
    out = prompts.substitute(template, {})
    assert out.strip() == "value:"


def test_drop_empty_sections():
    """A [SECTION] header with empty body should be dropped entirely (header + body)."""
    import prompts
    text = """[FIRST]
some content.

[EMPTY]


[THIRD]
more content."""
    out = prompts._drop_empty_sections(text)
    assert "[FIRST]" in out
    assert "[THIRD]" in out
    assert "[EMPTY]" not in out


def test_drop_empty_sections_unfilled_placeholder():
    """A section whose body is JUST '{NAME}' (unfilled placeholder) is dropped."""
    import prompts
    text = """[FIRST]
content.

[INVARIANTS]
{INHERITED_INVARIANTS}

[LAST]
content."""
    out = prompts._drop_empty_sections(text)
    assert "[FIRST]" in out
    assert "[LAST]" in out
    assert "[INVARIANTS]" not in out


# ---------- filter_common_header_by_symbols ----------

def test_filter_common_header_keeps_referenced():
    import prompts
    header = """#include <vfs.h>

extern int VnodeAlloc(struct VnodeOps *ops, struct Vnode **vp);
extern int VfsHashGet(struct Mount *m, uint32_t h, struct Vnode **vp);
extern int OtherFunc(int x);
"""
    out = prompts.filter_common_header_by_symbols(header, {"VnodeAlloc"})
    assert "VnodeAlloc" in out
    assert "VfsHashGet" not in out
    assert "OtherFunc" not in out
    assert "#include <vfs.h>" in out


def test_filter_common_header_empty_symbols_returns_unchanged():
    import prompts
    header = "extern int Foo(int x);\n"
    assert prompts.filter_common_header_by_symbols(header, set()) == header


def test_filter_preserves_extern_variables():
    """`extern struct VnodeOps g_xVops;` (no parens) is a variable, not function —
    must not be elided just because it isn't in keep_symbols."""
    import prompts
    header = "extern struct VnodeOps g_xVops;\n"
    out = prompts.filter_common_header_by_symbols(header, {"VfsXMount"})
    # Implementation note: filter regex matches ALL `extern ... ;` blocks; the
    # callable check (`fm = re.search(\b\w+\s*\(`)`) returns None for var decls,
    # which falls through to `return stmt`, so the var is preserved.
    assert "g_xVops" in out


# ---------- extract_rely_symbols ----------

def test_extract_rely_symbols(sample_spec_text: str):
    import prompts
    syms = prompts.extract_rely_symbols(sample_spec_text)
    assert "VnodeAlloc" in syms
    assert "VfsHashGet" in syms
    # Words inside [GUARANTEE] / [SPECIFICATION] should NOT be picked up
    assert "VfsExfatLookup" not in syms


def test_extract_rely_symbols_no_rely_block_returns_empty():
    import prompts
    text = "[PROMPT]\nNo RELY here.\n[GUARANTEE]\nint foo(void);\n"
    assert prompts.extract_rely_symbols(text) == set()


# ---------- assembly (golden-path) ----------

def test_assemble_codegen_prompt_contains_required_blocks(sample_spec_text: str):
    import prompts
    out = prompts.assemble_codegen_prompt(
        module="exfat",
        spec_content=sample_spec_text,
        common_header="extern int Foo(void);",
        inherited_invariants=[{"id": "anc-1", "text": "ancestor invariant"}],
        prior_code_interface="// from fs/exfat/exfat_super.c\nint VfsExfatMount(...);",
    )
    assert "exfat" in out
    assert "VfsExfatLookup" in out  # from spec [GUARANTEE]
    assert "ancestor invariant" in out
    assert "VfsExfatMount" in out  # from prior code interface


def test_assemble_codegen_first_stage_empty_blocks(sample_spec_text: str):
    """First-stage: no common.header, no inherited invariants, no prior interface.
    The prompt should still assemble (use placeholder fillers, not crash)."""
    import prompts
    out = prompts.assemble_codegen_prompt(
        module="exfat",
        spec_content=sample_spec_text,
        common_header="",
        inherited_invariants=[],
        prior_code_interface="",
    )
    assert "first stage" in out  # the "(empty — first stage)" placeholder
    assert "VfsExfatLookup" in out


def test_assemble_codegen_with_failures(sample_spec_text: str):
    """[Modification suggestions] must tag each failure with <source: layer>."""
    import prompts
    failures = [
        prompts.FailureNote(layer="compile", payload="error: undeclared identifier"),
        prompts.FailureNote(layer="style", payload="violation: bare strncpy"),
    ]
    out = prompts.assemble_codegen_prompt(
        module="exfat",
        spec_content=sample_spec_text,
        common_header="",
        inherited_invariants=[],
        prior_code_interface="",
        failures=failures,
    )
    assert "<source: compile>" in out
    assert "undeclared identifier" in out
    assert "<source: style>" in out
    assert "bare strncpy" in out


def test_assemble_speceval_prompt_is_spec_only():
    """P1.2 + P1.4: speceval prompt must NOT contain {STYLE_RULES} placeholder
    or any reference to libsec/naming/locking style rules. Style stays in
    Layer 1a.2 audit."""
    import prompts
    out = prompts.assemble_speceval_prompt(
        generated_code="int main(void) { return 0; }",
        original_spec="[PROMPT]\ntest.\n",
    )
    assert "{STYLE_RULES}" not in out
    # Spec eval should reference spec / Pre-Condition / Post-Condition / Invariant
    # — all spec-conformance language. We check for at least one of these.
    assert any(kw in out for kw in ("Pre-Condition", "Post-Condition", "Invariant", "spec"))


def test_assemble_style_audit_prompt():
    import prompts
    out = prompts.assemble_style_audit_prompt(
        generated_code="int Foo(void) { return 0; }",
        auto_checks="clang-format: clean. libsec scan: 0 unsafe.",
    )
    assert "Foo" in out
    assert "clang-format: clean" in out


def test_assemble_style_audit_no_auto_checks():
    """When auto_checks is empty, prompt must say so (LLM does pure self-judge)."""
    import prompts
    out = prompts.assemble_style_audit_prompt(generated_code="int x;")
    assert "no auto checks ran" in out


def test_assemble_unittest_gen_prompt(sample_spec_text: str):
    import prompts
    out = prompts.assemble_unittest_gen_prompt(
        generated_code="int VfsExfatLookup(...) { return 0; }",
        original_spec=sample_spec_text,
        harness_layout="testsuites/unittest/exfat/Makefile\nmain.c\n",
    )
    assert "VfsExfatLookup" in out
    assert "testsuites/unittest/exfat/Makefile" in out


def test_assemble_spec_fine_prompt():
    import prompts
    out = prompts.assemble_spec_fine_prompt(
        original_spec="[PROMPT]\nold.\n",
        speceval_comments="Invariant id=foo missing post-condition.",
    )
    assert "old." in out
    assert "missing post-condition" in out


def test_assemble_linux_to_spec_prompt():
    import prompts
    out = prompts.assemble_linux_to_spec_prompt(
        module="exfat",
        linux_path="/path/to/linux/fs/exfat",
        target_stage="lookup",
        sub_path="interface",
        common_header="",
        inherited_invariants=[],
        prior_spec_index="",
    )
    assert "exfat" in out
    assert "lookup" in out
    assert "first stage" in out  # empty common_header → placeholder
    assert "(none)" in out  # empty prior_spec_index


# ---------- Loop C linux_compare + prompt_optimize (P1.6 Wave 2) ----------


def test_assemble_linux_compare_prompt_carries_inputs():
    import prompts
    out = prompts.assemble_linux_compare_prompt(
        module="exfat",
        stage="unlink",
        linux_path="/Users/kissa/Codebase/linux/fs/exfat/namei.c",
        linux_source="int exfat_unlink(struct inode *dir, struct dentry *d) { return 0; }",
        spec_path="spec/exfat/inode/exfat_unlink.spec",
        generated_spec="[PROMPT]\nSentinel-spec.\n",
        code_path="fs/exfat/exfat_inode.c",
        generated_code="int VfsExfatUnlink(void) { return 0; }",
        common_header="extern int helper(void);",
        inherited_invariants=[{"id": "exfat-unlink-foo", "text": "must hold s_lock"}],
    )
    assert "exfat" in out
    assert "unlink" in out
    assert "Sentinel-spec" in out
    assert "VfsExfatUnlink" in out
    assert "exfat_unlink" in out  # Linux fn name carried through
    assert "exfat-unlink-foo" in out  # invariant rendered
    # Must ask for JSON output and additive recommendations
    assert "JSON" in out
    assert "spec_prompt_recommendations" in out
    assert "codegen_prompt_recommendations" in out


def test_assemble_linux_compare_drops_empty_invariants():
    import prompts
    out = prompts.assemble_linux_compare_prompt(
        module="exfat", stage="lookup", linux_path="x.c", linux_source="x",
        spec_path="s", generated_spec="g", code_path="c", generated_code="cc",
        common_header="", inherited_invariants=[],
    )
    # Empty common_header → "first stage" placeholder
    assert "first stage" in out
    # Empty inherited_invariants → its enclosing [INHERITED INVARIANTS] block dropped
    assert "[INHERITED INVARIANTS" not in out


def test_assemble_prompt_optimize_carries_recommendations():
    import prompts
    out = prompts.assemble_prompt_optimize_prompt(
        target_prompt_name="linux_to_spec",
        module="exfat",
        rec_type="spec",
        n_stages=3,
        current_prompt="[ROLE]\nDraft a spec.\n",
        recommendations="- Recommendation A\n- Recommendation B",
    )
    assert "linux_to_spec" in out
    assert "exfat" in out
    assert "Recommendation A" in out
    assert "Recommendation B" in out
    # Must enforce additive-only output and dedup constraints
    assert "ADDITIVE" in out or "Additive" in out or "additive" in out


def test_assemble_prompt_optimize_empty_recommendations():
    import prompts
    out = prompts.assemble_prompt_optimize_prompt(
        target_prompt_name="codegen", module="exfat", rec_type="codegen",
        n_stages=0, current_prompt="x", recommendations="",
    )
    assert "(none accumulated)" in out


# ---------- failure-rendering edge cases ----------

def test_render_failures_empty():
    import prompts
    assert prompts._render_failures([]) == ""


def test_render_invariants_orders_by_input():
    import prompts
    out = prompts._render_invariants([
        {"id": "a", "text": "first"},
        {"id": "b", "text": "second"},
    ])
    assert out.index("first") < out.index("second")
    assert "(id=a)" in out
    assert "(id=b)" in out
