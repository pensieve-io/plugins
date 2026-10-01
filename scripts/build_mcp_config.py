"""Embed the portable header helper because Codex HTTP helpers lack a plugin cwd."""

import json
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def manifest():
    source = (ROOT / "pensieve/scripts/plugin_headers.py").read_text()
    return {
        "mcpServers": {
            "pensieve": {
                "type": "http",
                "url": "https://mcp.pensieve.uk/mcp",
                "headersHelper": 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plugin_headers.py" claude',
                "http_headers_helper": "python3 -c " + shlex.quote(source) + " codex",
            }
        }
    }


if __name__ == "__main__":
    (ROOT / "pensieve/.mcp.json").write_text(json.dumps(manifest(), indent=2) + "\n")
