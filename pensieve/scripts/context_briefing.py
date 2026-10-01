"""Deliver briefings with a private installation authorized by native MCP OAuth.

No model tool or access to the harness's credential store is needed. The server
owns company selection and transcript consent; local state tracks delivery only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from capture_config import (
    CONFIG_PATH,
    MAX_CONFIG_BYTES,
    private_file,
    private_lock,
    save_private_json,
    valid_uuid,
)
from context_receipt import (
    DELIVERY_ENDPOINT,
    MAX_INPUT_BYTES,
    conversation_id,
    fallback_primer,
    json_object,
    latest_receipt,
    send_receipt,
)
from plugin_connection import (
    CONNECTION_ENDPOINT,
    connect_message,
    connection,
    installation_token,
)

BRIEFING_ENDPOINT = "https://mcp.pensieve.uk/hooks/briefing"
BINDING_ENDPOINT = "https://mcp.pensieve.uk/hooks/tool-binding"


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


MAX_RESPONSE_BYTES = 32 * 1024
HTTP_TIMEOUT_SECONDS = 3


def checked_endpoint(value: str) -> str:
    if value in {BRIEFING_ENDPOINT, BINDING_ENDPOINT, CONNECTION_ENDPOINT}:
        return value
    parsed = urlsplit(value)
    try:
        local = (
            parsed.hostname == "localhost"
            or ipaddress.ip_address(parsed.hostname or "").is_loopback
        )
    except ValueError:
        local = False
    if (
        not local
        or parsed.scheme != "http"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"/hooks/briefing", "/hooks/tool-binding", "/hooks/connection"}
        or (parsed.port is not None and parsed.port < 1)
    ):
        raise ValueError("Only the fixed Pensieve endpoint or HTTP loopback fixture is supported")
    return value


def request(
    endpoint: str, key: str | None, body: dict, timeout: float = HTTP_TIMEOUT_SECONDS
) -> tuple[int | str, dict | None]:
    headers = {"Content-Type": "application/json", "User-Agent": "Pensieve-Plugin-Briefing/1.0"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = Request(
        checked_endpoint(endpoint),
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers=headers,
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return "invalid_response", None
            return response.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        return exc.code, None
    except (OSError, URLError, ValueError):
        return "unavailable", None


def state_path(config: Path, client: str, session: str) -> Path:
    return config.parent / "briefing" / f"{client}-{session}.json"


def read_state(path: Path) -> dict:
    try:
        value = json.loads(private_file(path, MAX_CONFIG_BYTES))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Invalid private briefing state")
    if value.get("user_id") is not None and not valid_uuid(value["user_id"]):
        raise ValueError("Invalid briefing account")
    context = value.get("context_id")
    if context is not None and (type(context) is not int or context <= 0):
        raise ValueError("Invalid briefing context")
    return value


def failure(event: str, client: str, session: str | None, reason: str = "") -> dict:
    if event == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": "Pensieve could not verify this conversation. "
                "Retry after the plugin reconnects.",
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": fallback_primer(client, session)
            + (" " + reason if reason else ""),
        }
    }


def run_hook(
    payload: dict,
    client: str,
    config: Path = CONFIG_PATH,
    endpoint: str = BRIEFING_ENDPOINT,
    binding_endpoint: str = BINDING_ENDPOINT,
    receipt_endpoint: str = DELIVERY_ENDPOINT,
    connection_endpoint: str = CONNECTION_ENDPOINT,
) -> dict:
    event = payload.get("hook_event_name")
    if event not in {"SessionStart", "UserPromptSubmit", "PreToolUse"}:
        return {}
    session = conversation_id(payload.get("session_id"))
    if session is None:
        return failure(event, client, None)
    if event == "PreToolUse":
        if client != "claude" or not str(payload.get("tool_name", "")).startswith(
            "mcp__plugin_pensieve_pensieve__"
        ):
            return {}
        provenance = payload.get("mcp_server")
        if provenance is not None and provenance != {
            "name": "plugin:pensieve:pensieve",
            "source": "plugin",
        }:
            return failure(event, client, session)
        tool_id = payload.get("tool_use_id")
        if not isinstance(tool_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", tool_id):
            return failure(event, client, session)
    # The native MCP header helper reads this same private proof on connect.
    # Missing registration never launches another authentication flow or leaks
    # a secret through hook output. The member uses the harness's normal login.
    key = installation_token(config, client)
    profile = connection(config, client, key, connection_endpoint, request)
    if profile is None:
        return (
            {}
            if event == "PreToolUse"
            else failure(event, client, session, connect_message(client))
        )
    if event == "PreToolUse":
        code, _ = request(
            binding_endpoint, key, {"client": client, "session_id": session, "tool_use_id": tool_id}
        )
        return {} if code == 204 else failure(event, client, session)
    path = state_path(config, client, session)
    with private_lock(path.with_suffix(".lock")):
        state = read_state(path)
        needs_refresh = (
            state.get("needs_refresh") is True or state.get("user_id") != profile["user_id"]
        )
        state.update(needs_refresh=True, user_id=profile["user_id"])
        save_private_json(path, state)
        receipt = latest_receipt(payload.get("transcript_path"), session, client)
        force = needs_refresh or event == "SessionStart" or receipt is None or receipt[1] == "reset"
        if receipt:
            send_receipt(
                receipt[0], "reset" if event == "SessionStart" else receipt[1], receipt_endpoint
            )
        if event == "SessionStart" or not valid_uuid(state.get("delivery_id")):
            state["delivery_id"] = str(uuid.uuid4())
        body = {
            "client": client,
            "session_id": session,
            "event": event,
            "delivery_id": state["delivery_id"],
        }
        if force:
            body["force_refresh"] = True
        source = payload.get("source")
        if isinstance(source, str) and len(source) <= 100:
            body["source"] = source
        turn = conversation_id(payload.get("turn_id"))
        if turn:
            body["turn_id"] = turn
        code, result = request(endpoint, key, body)
        if code == 200 and isinstance(result, dict):
            available = result.pop("briefing_available", True) is True
            output = result.get("hookSpecificOutput")
            if result == {} or (
                isinstance(output, dict)
                and isinstance(output.get("additionalContext", ""), str)
                and output.get("hookEventName") == event
            ):
                if available:
                    state.pop("needs_refresh", None)
                save_private_json(path, state)
                return result
        save_private_json(path, state)
        print(
            f"Pensieve briefing unavailable ({code}); automatic retry remains enabled.",
            file=sys.stderr,
        )
        return failure(
            event, client, session, connect_message(client) if code in {401, 403} else ""
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True, choices=("claude", "codex"))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("PENSIEVE_CAPTURE_CONFIG", str(CONFIG_PATH))),
    )
    parser.add_argument("--endpoint", type=checked_endpoint, default=BRIEFING_ENDPOINT)
    parser.add_argument("--binding-endpoint", type=checked_endpoint, default=BINDING_ENDPOINT)
    parser.add_argument("--receipt-endpoint", default=DELIVERY_ENDPOINT)
    parser.add_argument("--connection-endpoint", type=checked_endpoint, default=CONNECTION_ENDPOINT)
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    payload = json_object(raw) if len(raw) <= MAX_INPUT_BYTES else None
    if payload is None:
        print("{}")
        return
    try:
        result = run_hook(
            payload,
            args.client,
            args.config,
            args.endpoint,
            args.binding_endpoint,
            args.receipt_endpoint,
            args.connection_endpoint,
        )
    except (OSError, ValueError):
        print(
            "Pensieve hook unavailable: check the private plugin connection; retry is automatic.",
            file=sys.stderr,
        )
        result = failure(
            payload.get("hook_event_name", "SessionStart"),
            args.client,
            conversation_id(payload.get("session_id")),
        )
    print(json.dumps(result))
    if result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
