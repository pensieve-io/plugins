"""The distributed package must install one connected, complete plugin."""

import json
import re
import struct
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
    server = read_json(PLUGIN / ".mcp.json")["mcpServers"]["pensieve"]
    assert server["type"] == "http"
    assert server["url"] == "https://mcp.pensieve.uk/mcp"
    for field, client in (("headersHelper", "claude"), ("http_headers_helper", "codex")):
        assert client in server[field]
        assert "Authorization" not in server[field]
    assert not re.search(r"pcap_[A-Za-z0-9_-]{43}", json.dumps(server))


def test_codex_presentation_assets_are_bundled_pngs():
    interface = read_json(PLUGIN / ".codex-plugin/plugin.json")["interface"]
    for field in ("composerIcon", "logo", "logoDark"):
        relative = interface[field]
        assert relative.startswith("./assets/")
        path = (PLUGIN / relative).resolve()
        assert PLUGIN.resolve() in path.parents
        assert path.suffix == ".png"
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", field
        assert data[12:16] == b"IHDR", field
        width, height = struct.unpack(">II", data[16:24])
        assert width == height and 128 <= width <= 2048, field
    prompts = interface["defaultPrompt"]
    assert 1 <= len(prompts) <= 3
    assert all(0 < len(prompt) <= 128 for prompt in prompts)
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", interface["brandColor"])


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


@pytest.mark.parametrize("client,filename", [("claude", "hooks.json"), ("codex", "codex.json")])
def test_host_adapters_use_command_briefing_and_preserve_capture(client, filename):
    hooks = read_json(PLUGIN / "hooks" / filename)["hooks"]
    expected = {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}
    assert set(hooks) == expected | ({"PreToolUse"} if client == "claude" else set())
    flattened = [hook for entries in hooks.values() for entry in entries for hook in entry["hooks"]]
    assert all(hook["type"] == "command" for hook in flattened)
    assert all(" --client " + client in hook["command"] for hook in flattened)
    for event in ("SessionStart", "UserPromptSubmit"):
        scripts = [h["command"] for g in hooks[event] for h in g["hooks"]]
        assert len(scripts) == 2
        assert "context_briefing.py" in scripts[0]
        assert "conversation_capture.py" in scripts[1]
        assert all("context_receipt.py" not in command for command in scripts)
    if client == "claude":
        assert hooks["PreToolUse"][0]["matcher"] == (
            "^mcp__(plugin_pensieve_pensieve|claude_ai_Pensieve|pensieve)__.*$"
        )
    for script in ("context_briefing", "context_receipt", "conversation_capture"):
        assert (PLUGIN / "scripts" / (script + ".py")).is_file()
    assert all(hook["timeout"] == 1 for group in hooks["SessionEnd"] for hook in group["hooks"])
