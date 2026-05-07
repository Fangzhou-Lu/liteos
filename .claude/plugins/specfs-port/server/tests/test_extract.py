"""Unit tests for extract.py — C interface extractor."""
from __future__ import annotations

from pathlib import Path


def test_extract_simple_function():
    import extract
    src = """\
int VfsExfatMount(struct Mount *mount, struct Vnode *blk, const void *data)
{
    return 0;
}
"""
    iface = extract.extract_from_text(src, "test.c")
    assert "VfsExfatMount" in iface.fn_names
    assert any("VfsExfatMount" in s for s in iface.functions)


def test_extract_skips_static_helpers():
    """`static` functions must NOT be exposed in the public interface."""
    import extract
    src = """\
static int helper(int x)
{
    return x;
}

int public_fn(void)
{
    return 0;
}
"""
    iface = extract.extract_from_text(src, "test.c")
    assert "public_fn" in iface.fn_names
    assert "helper" not in iface.fn_names


def test_extract_strips_block_comments_v0_3_4_1_fix():
    """v0.3.4.1 regex fix: block comments before a function must NOT be treated
    as part of the return type. Without _strip_c_comments the regex captures
    the comment fragment as a bogus signature.

    See specfs-port CHANGELOG v0.3.4 §"Known server bugs" item (1).
    """
    import extract
    src = """\
/* exfat_get_dentry_set — fetch the dentry set rooted at clu/idx.
 * Caller must hold the inode lock.
 * (per Invariant exfat-dset-immutable)
 */
int exfat_get_dentry_set(struct exfat_sb_info *sbi, uint32_t clu)
{
    return 0;
}
"""
    iface = extract.extract_from_text(src, "test.c")
    assert "exfat_get_dentry_set" in iface.fn_names
    # The bug pre-fix would emit a bogus signature like
    # "* exfat_get_dentry_set ... */ int exfat_get_dentry_set(...)" — single
    # malformed entry. After the fix exactly one clean signature is captured.
    matches = [s for s in iface.functions if "exfat_get_dentry_set" in s]
    assert len(matches) == 1
    # No comment fragment should leak into the captured signature
    assert "/*" not in matches[0]
    assert "Invariant" not in matches[0]


def test_extract_extern_var():
    import extract
    src = "extern struct VnodeOps g_exfatVops;\n"
    iface = extract.extract_from_text(src, "test.c")
    assert any("g_exfatVops" in v for v in iface.extern_vars)


def test_extract_fsmap_entry():
    import extract
    src = """\
FSMAP_ENTRY(exfat_fsmap, "exfat", g_exfatMops, FALSE, TRUE);
"""
    iface = extract.extract_from_text(src, "test.c")
    assert iface.fsmap_entries == [("exfat_fsmap", "exfat")]


def test_extract_pub_global():
    import extract
    src = """\
struct VnodeOps g_exfatVops = {
    .Lookup = VfsExfatLookup,
    .Reclaim = VfsExfatReclaim,
};
"""
    iface = extract.extract_from_text(src, "test.c")
    assert any("g_exfatVops" in g for g in iface.pub_globals)


def test_extract_loscfg_guard():
    import extract
    src = """\
#ifdef LOSCFG_FS_EXFAT
int foo(void) { return 0; }
#endif
"""
    iface = extract.extract_from_text(src, "test.c")
    assert "LOSCFG_FS_EXFAT" in iface.loscfg_guards


def test_extract_module_interface_walks_fs_dir(tmp_repo: Path):
    """extract_module_interface walks fs/<module>/**/*.c and *.h."""
    import extract
    fs_dir = tmp_repo / "fs" / "exfat"
    (fs_dir / "exfat_super.c").write_text(
        "int VfsExfatMount(struct Mount *m, struct Vnode *b, const void *d)\n{\n    return 0;\n}\n"
    )
    (fs_dir / "include").mkdir()
    (fs_dir / "include" / "exfat.h").write_text("extern int g_exfatFlag;\n")

    ifaces = extract.extract_module_interface("exfat", tmp_repo)
    keys = list(ifaces.keys())
    assert any("exfat_super.c" in k for k in keys)
    assert any("exfat.h" in k for k in keys)


def test_extract_render_interface_summary_empty():
    import extract
    out = extract.render_interface_summary({})
    assert "first stage" in out


def test_extract_render_interface_summary_nonempty():
    import extract
    iface = extract.ExtractedInterface(src_file="fs/exfat/exfat_super.c")
    iface.functions = ["int VfsExfatMount(struct Mount *m, struct Vnode *b, const void *d)"]
    iface.fn_names = ["VfsExfatMount"]
    iface.fsmap_entries = [("exfat_fsmap", "exfat")]
    out = extract.render_interface_summary({"fs/exfat/exfat_super.c": iface})
    assert "// from fs/exfat/exfat_super.c" in out
    assert "VfsExfatMount" in out
    assert 'FSMAP_ENTRY(exfat_fsmap, "exfat", ...)' in out


def test_extract_collect_all_symbols():
    import extract
    iface = extract.ExtractedInterface(src_file="x.c")
    iface.fn_names = ["A", "B"]
    iface.extern_vars = ["int g_flag"]
    iface.fsmap_entries = [("exfat_fsmap", "exfat")]
    out = extract.collect_all_symbols({"x.c": iface})
    assert {"A", "B", "g_flag", "exfat_fsmap"} <= out


def test_extract_normalize_signature_collapses_whitespace():
    import extract
    sig = "int   foo (\n    int x,\n    int y\n)"
    assert extract._normalize_signature(sig) == "int foo ( int x, int y )"
