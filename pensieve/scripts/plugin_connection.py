"""Private installation proof shared with the harness's MCP header helper.

Native OAuth remains in the harness. This proof only gains hook authority after
Pensieve receives it alongside a verified OAuth request. It is never model text.
"""

from __future__ import annotations

from pathlib import Path

from capture_config import (
    CLIENTS,
    MAX_CONFIG_BYTES,
    ReconnectRequired,
    config_lock,
    install_profile,
    load_config,
    private_file,
    private_lock,
    save_private_json,
    valid_uuid,
)
from plugin_headers import HEADER, header_path, prepare_headers

CONNECTION_ENDPOINT = "https://mcp.pensieve.uk/hooks/connection"


def installation_token(config: Path, client: str) -> str:
    return prepare_headers(config, client)[HEADER].split(" ", 1)[1]


def connect_message(client: str) -> str:
    return (
        "Authenticate or reconnect the Pensieve plugin in /mcp to connect its tools and hooks. "
        "Then send another prompt to load the company briefing. Transcript sharing is a separate choice. If revoked or switching accounts, reset the plugin connection first: https://github.com/pensieve-io/plugins#reset-a-connection"
        if client == "claude"
        else "Authenticate the Pensieve plugin in the harness's MCP settings (or run `codex mcp login pensieve`), "
        "then reconnect the MCP server and send another prompt. Transcript sharing is a separate choice. If revoked or switching accounts, reset the plugin connection first: https://github.com/pensieve-io/plugins#reset-a-connection"
    )


def connection(config: Path, client: str, token: str, endpoint: str, request) -> dict | None:
    """Resolve the installation's authenticated owner, with no pairing exchange."""
    try:
        value = load_config(config, client)
    except ReconnectRequired:
        value = {"profiles": []}
    for profile in value["profiles"]:
        if (
            profile.get("client") == client
            and profile.get("upload_key") == token
            and profile.get("briefing_enabled") is True
            and profile.get("context_id") is None
        ):
            return profile
    code, identity = request(endpoint, token, {"client": client}, timeout=2)
    if code != 200 or not isinstance(identity, dict):
        return None
    if (
        identity.get("client") != client
        or identity.get("context_id") is not None
        or not valid_uuid(identity.get("user_id"))
        or not valid_uuid(identity.get("installation_id"))
    ):
        raise ValueError("Invalid authenticated plugin identity")
    profile = {
        "user_id": identity["user_id"],
        "installation_id": identity["installation_id"],
        "client": client,
        "upload_key": token,
        "briefing_enabled": True,
        "runtime": "claude_code_cli" if client == "claude" else "codex_cli",
        "host_version": "",
    }
    install_profile(config, profile, replace_client=True)
    return profile


def reset_connection(config: Path, client: str, state_root: Path) -> None:
    """Explicit local reset; never called in response to a failed hook."""
    from capture_config import observe_credentials

    path = header_path(config, client)
    with private_lock(path.with_suffix(".lock")), config_lock(config):
        value = load_config(config, client)
        profiles = [p for p in value["profiles"] if p.get("client") not in {None, client}]
        # Fence dormant spools before removing credentials, including a reset
        # followed by reauthentication before any capture hook runs.
        observe_credentials(state_root, client, {})
        save_private_json(config, {"version": 3, "profiles": profiles})
        if path.exists() or path.is_symlink():
            private_file(path, MAX_CONFIG_BYTES)
            path.unlink()
    installation_token(config, client)


if __name__ == "__main__":
    import argparse

    from capture_config import CONFIG_PATH

    parser = argparse.ArgumentParser(
        description="Reset a revoked or account-switched Pensieve plugin connection."
    )
    parser.add_argument("--client", required=True, choices=sorted(CLIENTS))
    parser.add_argument("--reset", required=True, action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--state-root", type=Path, default=Path.home() / ".local/state/pensieve/capture"
    )
    args = parser.parse_args()
    try:
        reset_connection(args.config, args.client, args.state_root)
    except (OSError, ValueError):
        print(
            "Pensieve reset could not complete; close active sessions and check private file permissions.",
            file=__import__("sys").stderr,
        )
        raise SystemExit(1)
    print(connect_message(args.client))
