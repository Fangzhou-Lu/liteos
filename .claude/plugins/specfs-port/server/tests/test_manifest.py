"""Plugin-level structural tests — manifest, .mcp.json, SKILL.md frontmatter."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


# ---------- plugin.json ----------

def test_plugin_json_is_valid_json():
    p = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_plugin_json_has_required_name_field():
    p = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data.get("name") == "specfs-port"


def test_plugin_json_version_semver():
    """Version must be MAJOR.MINOR.PATCH semver."""
    p = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert re.match(r"^\d+\.\d+\.\d+$", data.get("version", ""))


def test_plugin_json_has_no_legacy_bundles_skills():
    """v0.5.0 dropped `bundles.skills` — auto-discovery via skills/ subdir
    handles the same job. The presence of this field is non-standard
    per plugin-dev:plugin-structure."""
    p = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "bundles" not in data, "v0.5.0 removed bundles field; re-introduction would regress"


def test_plugin_json_has_keywords():
    p = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    keywords = data.get("keywords", [])
    assert "specfs" in keywords
    assert "liteos-a" in keywords
    assert "filesystem" in keywords


# ---------- .mcp.json ----------

def test_mcp_json_is_valid_json():
    p = PLUGIN_ROOT / ".mcp.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "mcpServers" in data


def test_mcp_json_uses_plugin_root_placeholder():
    """Per plugin-dev:plugin-structure portability rule: paths in .mcp.json
    must use ${CLAUDE_PLUGIN_ROOT} not absolute paths."""
    p = PLUGIN_ROOT / ".mcp.json"
    text = p.read_text(encoding="utf-8")
    assert "${CLAUDE_PLUGIN_ROOT}" in text
    # Negative check: no hardcoded user-home or absolute paths
    assert "/Users/" not in text
    assert "/home/" not in text


def test_mcp_json_specfs_server_command():
    p = PLUGIN_ROOT / ".mcp.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    server = data["mcpServers"].get("specfs")
    assert server is not None
    assert server["command"] == "uv"
    # The args invoke `python -u specfs_server.py` under server/
    assert "specfs_server.py" in server["args"]


# ---------- bundled skill ----------

def test_bundled_skill_dir_exists():
    p = PLUGIN_ROOT / "skills" / "specfs-port"
    assert p.is_dir(), "v0.5.0: skill must live at skills/specfs-port/ inside plugin"


def test_bundled_skill_md_has_frontmatter():
    p = PLUGIN_ROOT / "skills" / "specfs-port" / "SKILL.md"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    # Must start with YAML frontmatter (--- ... ---)
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    fm = m.group(1)
    assert "name:" in fm
    assert "description:" in fm


def test_bundled_skill_references_dir_present():
    p = PLUGIN_ROOT / "skills" / "specfs-port" / "references"
    assert p.is_dir()
    refs = list(p.glob("*.md"))
    # Expect at least the canonical 7 references
    assert len(refs) >= 5


# ---------- commands/ ----------

@pytest.mark.parametrize("cmd_name", ["specfs-port", "specfs-port-spec", "specfs-port-code"])
def test_command_file_exists_with_frontmatter(cmd_name: str):
    p = PLUGIN_ROOT / "commands" / f"{cmd_name}.md"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, f"{cmd_name}.md missing YAML frontmatter"
    fm = m.group(1)
    assert "description:" in fm


# ---------- prompts/ canonical fragments ----------

@pytest.mark.parametrize("frag", [
    "codegen", "speceval", "linux_to_spec", "ask_first_rules",
    "style_rules", "style_audit", "linux_to_liteos_table",
    "format_traps", "validation_checklist", "unittest_gen",
    "spec_fine", "liteos_digest",
])
def test_prompt_fragment_present(frag: str):
    p = PLUGIN_ROOT / "prompts" / f"{frag}.md"
    assert p.is_file()
    assert p.stat().st_size > 0


# ---------- pyproject.toml dev-deps ----------

def test_pyproject_has_pytest_in_dev_deps():
    """v0.5.0 server pyproject should declare pytest as a dev dependency
    so `uv pip install -e '.[dev]'` provisions the test stack."""
    p = PLUGIN_ROOT / "server" / "pyproject.toml"
    text = p.read_text(encoding="utf-8")
    assert "pytest" in text, (
        "pyproject.toml should declare pytest under [project.optional-dependencies].dev "
        "or [dependency-groups].dev"
    )
