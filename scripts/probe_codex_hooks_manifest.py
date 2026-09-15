"""Read a copied plugin through the installed Codex loader without installing it.

This starts no model turn and executes no hooks. The optional portable overlay
reproduces Codex 0.154.0's disabled-hook behaviour. Configuration is supplied as
process arguments; the user's configuration and installed plugins are untouched.
"""

from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any


def probe(plugin: Path, output: Path, *, portable: bool = False) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    copied = output / "plugin"
    shutil.copytree(plugin, copied)
    if portable:
        (copied / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    "name": "pensieve",
                    "extensions": {"com.openai": {"hooks": "./hooks/codex.json"}},
                }
            )
        )
        mcp_config = json.loads((copied / ".mcp.json").read_text())
        mcp_config["$schema"] = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
        for server in mcp_config["mcpServers"].values():
            if server.get("type") == "http":
                server["type"] = "streamable-http"
        (copied / "mcp.json").write_text(json.dumps(mcp_config))
    marketplace = output / ".claude-plugin" / "marketplace.json"
    marketplace.parent.mkdir()
    marketplace.write_text(
        json.dumps(
            {
                "name": "pensieve-hooks-fixture",
                "owner": {"name": "Pensieve fixture"},
                "plugins": [{"name": "pensieve", "source": "./plugin"}],
            }
        )
    )
    process = subprocess.Popen(
        [
            "codex",
            "app-server",
            "--stdio",
            "-c",
            "features.apps=false",
            "-c",
            "features.remote_plugins=false",
            "-c",
            f"log_dir={json.dumps(str(output / 'logs'))}",
            "-c",
            f"sqlite_home={json.dumps(str(output / 'state'))}",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    responses: queue.Queue[dict[str, Any]] = queue.Queue()

    def read_responses() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            responses.put(json.loads(line))

    threading.Thread(target=read_responses, daemon=True).start()

    def call(identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": method,
                    "params": params,
                }
            )
            + "\n"
        )
        process.stdin.flush()
        while True:
            response = responses.get(timeout=25)
            if response.get("id") == identifier:
                return response

    try:
        call(
            1,
            "initialize",
            {
                "clientInfo": {"name": "pensieve-hook-fixture", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        response = call(
            2,
            "plugin/read",
            {
                "marketplacePath": str(marketplace),
                "pluginName": "pensieve",
            },
        )
        (output / "manifest.json").write_text(json.dumps(response, indent=2))
        detail = response.get("result", {}).get("plugin", {})
        return {
            "hooks": detail.get("hooks"),
            "mcp_servers": detail.get("mcpServers"),
            "skills": len(detail.get("skills", [])),
            "error": response.get("error"),
        }
    finally:
        process.terminate()
        process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--portable", action="store_true")
    args = parser.parse_args()
    print(json.dumps(probe(args.plugin, args.output, portable=args.portable), indent=2))
