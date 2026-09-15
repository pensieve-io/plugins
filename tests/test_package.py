"""The distributed package must install one connected, complete plugin."""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "pensieve"


def read_json(path):
    return json.loads(path.read_text())


def test_marketplace_preserves_existing_install_identity():
    catalogue = read_json(ROOT / ".claude-plugin/marketplace.json")
    assert catalogue["name"] == "pensieve"
    assert len(catalogue["plugins"]) == 1
    entry = catalogue["plugins"][0]
    assert entry["name"] == "pensieve"
    assert (ROOT / entry["source"]).resolve() == PLUGIN


def test_manifests_resolve_the_installed_components():
    claude = read_json(PLUGIN / ".claude-plugin/plugin.json")
    codex = read_json(PLUGIN / ".codex-plugin/plugin.json")
    assert claude["name"] == codex["name"] == "pensieve"
    assert "version" not in claude and "version" not in codex
    for field in ("skills", "mcpServers", "hooks"):
        assert (PLUGIN / codex[field]).exists(), field
    assert (PLUGIN / "hooks/hooks.json").is_file()
    # Codex 0.154.0's portable-root loader disables hooks. Keep the native
    # manifest until the package-reader probe proves the other format works.
    assert not (PLUGIN / "plugin.json").exists()


def test_plugin_bundles_the_hosted_mcp_without_credentials():
    assert read_json(PLUGIN / ".mcp.json") == {
        "mcpServers": {"pensieve": {"type": "http", "url": "https://mcp.pensieve.uk/mcp"}}
    }


def test_skills_have_valid_identity_and_a_visible_readme_entry():
    readme = (ROOT / "README.md").read_text()
    skills = list((PLUGIN / "skills").glob("*/SKILL.md"))
    assert skills
    for path in skills:
        source = path.read_text()
        match = re.match(r"\A---\n(.*?)\n---\n", source, re.DOTALL)
        assert match, path
        metadata = yaml.safe_load(match.group(1))
        name = metadata["name"]
        assert name == path.parent.name
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) and len(name) <= 64
        assert 0 < len(metadata["description"]) <= 1024
        assert metadata["metadata"]["role"]
        assert len(metadata["metadata"].get("summary", "")) <= 200
        assert f"`{name}`" in readme


@pytest.mark.parametrize(
    "client,filename,server",
    [("claude", "hooks.json", "plugin:pensieve:pensieve"), ("codex", "codex.json", "pensieve")],
)
def test_host_adapters_use_the_right_mcp_namespace_and_bundled_helper(client, filename, server):
    hooks = read_json(PLUGIN / "hooks" / filename)["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop"}
    flattened = [hook for entries in hooks.values() for entry in entries for hook in entry["hooks"]]
    assert len(flattened) == 5
    for hook in flattened:
        assert hook["timeout"] == 5
        if hook["type"] == "mcp_tool":
            assert hook["server"] == server
            assert hook["tool"] == "context_briefing"
            assert hook["input"]["client"] == client
            assert hook["input"]["session_id"] == "${session_id}"
            assert hook["input"]["event"] == "${hook_event_name}"
        else:
            assert hook["type"] == "command"
            assert hook["command"] == (
                'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/context_receipt.py" --client ' + client
            )
    assert (PLUGIN / "scripts/context_receipt.py").is_file()
