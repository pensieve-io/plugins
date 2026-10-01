"""Register this device through the member's MCP sign-in. Never emit credentials.

A pending pairing holds a private poll secret. The hooks bind it to the call or
thread their host is about to send to Pensieve's MCP server; that signed-in call
registers it, and the next hook exchanges the secret for a scoped credential.
"""

from __future__ import annotations

import ipaddress
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from capture_config import (
    CLIENTS,
    MAX_CONFIG_BYTES,
    encoded,
    install_profile,
    private_file,
    private_lock,
    save_private_json,
    valid_uuid,
)
from capture_protocol import HEADER, VERSION, check

API_BASE = "https://api.pensieve.uk/users/me/conversation-capture"
PLUGIN_VERSION = "mcp-handoff-1"
RUNTIMES = {"codex_cli", "claude_code_cli", "unknown"}


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def checked_base(value: str) -> str:
    if value == API_BASE:
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
        or parsed.path != "/users/me/conversation-capture"
        or (parsed.port is not None and parsed.port < 1)
    ):
        raise ValueError("Pairing requires the fixed Pensieve service or an HTTP loopback fixture")
    return value


def timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError("Invalid pairing expiry")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Invalid pairing expiry")
    return parsed.timestamp()


def request(base: str, path: str, body: dict | None, timeout: float, key: str | None = None):
    base = checked_base(base)
    deadline = time.monotonic() + timeout
    compatibility = check(base, "pairing", min(0.25, timeout / 2))
    if compatibility != "compatible":
        return compatibility, None
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Pensieve-Plugin-Pairing/1.0",
        HEADER: str(VERSION),
    }
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = Request(
        base + path,
        data=encoded(body) if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(req, timeout=max(0.05, deadline - time.monotonic())) as response:
            raw = response.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                return "unavailable", None
            return response.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        # Error bodies can contain provider detail. Only status drives recovery.
        return exc.code, None
    except (OSError, URLError, ValueError):
        return "unavailable", None


def pairing_path(config: Path, client: str) -> Path:
    if client not in CLIENTS:
        raise ValueError("Invalid client")
    return config.with_name(f"capture-pairing-{client}.json")


def pairing_receipt_path(config: Path, client: str, session_id: str) -> Path:
    if client not in CLIENTS or not valid_uuid(session_id):
        raise ValueError("Invalid pairing conversation")
    return config.parent / "briefing-pairings" / f"{client}-{session_id}.json"


def completed_pairing(config: Path, client: str, session_id: str) -> dict:
    """Recover this conversation's registration, whichever hook exchanged it."""
    try:
        result = json.loads(
            private_file(pairing_receipt_path(config, client, session_id), MAX_CONFIG_BYTES)
        )
    except FileNotFoundError:
        return {}
    if (
        not isinstance(result, dict)
        or not valid_uuid(result.get("pairing_id"))
        or not valid_uuid(result.get("user_id"))
        or type(result.get("context_id")) is not int
        or result["context_id"] <= 0
    ):
        raise ValueError("Invalid completed pairing")
    return result


def public_status(pending: dict) -> dict:
    return {
        "status": "awaiting_registration",
        "expires_at": pending["expires_at"],
        "client": pending["client"],
    }


def validate_pending(value: object, client: str) -> dict:
    if (
        not isinstance(value, dict)
        or value.get("client") != client
        or not valid_uuid(value.get("id"))
    ):
        raise ValueError("Invalid pairing state")
    secret = value.get("poll_secret")
    if (
        not isinstance(secret, str)
        or not 16 <= len(secret) <= 4096
        or any(c.isspace() for c in secret)
    ):
        raise ValueError("Invalid pairing state")
    checked_base(value["base"])
    timestamp(value["expires_at"])
    if value.get("runtime") not in RUNTIMES:
        raise ValueError("Invalid pairing runtime")
    if value.get("session_id") is not None and not valid_uuid(value["session_id"]):
        raise ValueError("Invalid pairing conversation")
    interval = value.get("poll_interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) or not 1 <= interval <= 60:
        raise ValueError("Invalid pairing interval")
    return value


def pending_claim(config: Path, client: str) -> dict | None:
    """The private claim awaiting registration. Replacement is atomic: no lock."""
    try:
        raw = private_file(pairing_path(config, client), MAX_CONFIG_BYTES)
    except FileNotFoundError:
        return None
    return validate_pending(json.loads(raw), client)


def discard_claim(config: Path, client: str, pairing_id: str) -> None:
    """Drop a claim the server reports closed, unless another hook replaced it."""
    path = pairing_path(config, client)
    with private_lock(path.with_suffix(".lock")):
        current = pending_claim(config, client)
        if current is not None and current["id"] == pairing_id:
            path.unlink()


def start(
    config: Path,
    client: str,
    runtime: str = "unknown",
    host_version: str = "",
    base: str = API_BASE,
    expected_user_id: str | None = None,
    expected_context_id: int | None = None,
    timeout: float = 5,
    session_id: str | None = None,
) -> dict:
    if runtime not in RUNTIMES or len(host_version) > 100:
        raise ValueError("Unsupported runtime")
    if runtime.startswith("codex") and client != "codex":
        raise ValueError("Runtime does not match client")
    if runtime.startswith("claude") and client != "claude":
        raise ValueError("Runtime does not match client")
    if session_id is not None and not valid_uuid(session_id):
        raise ValueError("Invalid pairing conversation")
    if expected_context_id is not None and expected_user_id is None:
        raise ValueError("Pairing context must include its account")
    if expected_user_id is not None and (
        not valid_uuid(expected_user_id)
        or (
            expected_context_id is not None
            and (type(expected_context_id) is not int or expected_context_id <= 0)
        )
    ):
        raise ValueError("Invalid pairing identity")
    path = pairing_path(config, client)
    with private_lock(path.with_suffix(".lock")):
        if path.exists() or path.is_symlink():
            old = validate_pending(json.loads(private_file(path, MAX_CONFIG_BYTES)), client)
            # Expiry is not claim expiry: a signed-in call may already have
            # registered it while exchange was offline. Only a terminal server
            # response in poll() can discard this private claim.
            if (
                old.get("expected_user_id") == expected_user_id
                and old.get("expected_context_id") == expected_context_id
                and old.get("session_id") == session_id
            ):
                return public_status(old)
            return {"status": "another_connection_pending"}
        code, response = request(
            base,
            "/pairings/start",
            {
                "client": client,
                "runtime": runtime,
                "label": "Codex" if client == "codex" else "Claude Code",
                "plugin_version": PLUGIN_VERSION,
                "host_version": host_version,
                "expected_user_id": expected_user_id,
                "expected_context_id": expected_context_id,
            },
            timeout,
        )
        if code != 201 or not isinstance(response, dict):
            return {
                "status": "unavailable",
                "message": "Pensieve could not start pairing. Try again.",
            }
        pending = validate_pending(
            {
                "id": response.get("id"),
                "poll_secret": response.get("poll_secret"),
                "expires_at": response.get("expires_at"),
                "poll_interval_seconds": response.get("poll_interval_seconds"),
                "base": checked_base(base),
                "client": client,
                "runtime": runtime,
                "host_version": host_version,
                "next_poll_at": 0,
                "expected_user_id": expected_user_id,
                "expected_context_id": expected_context_id,
                "session_id": session_id,
            },
            client,
        )
        if not time.time() < timestamp(pending["expires_at"]) <= time.time() + 3600:
            raise ValueError("Invalid pairing lifetime")
        save_private_json(path, pending)
        return public_status(pending)


def poll(config: Path, client: str, timeout: float = 2) -> dict:
    """One bounded exchange. Hooks share this lock with the interactive helper."""
    path = pairing_path(config, client)
    if not path.exists() and not path.is_symlink():
        return {"status": "no_pending_pairing"}
    with private_lock(path.with_suffix(".lock")):
        try:
            pending = validate_pending(json.loads(private_file(path, MAX_CONFIG_BYTES)), client)
        except FileNotFoundError:
            return {"status": "no_pending_pairing"}
        # Only unregistered claims expire. A registered one can finish on a
        # later hook, even after an offline interval.
        if pending.get("next_poll_at", 0) > time.time():
            return public_status(pending)
        # Persist the rate limit before a network attempt; concurrent hooks and
        # an offline machine must not hammer the exchange endpoint.
        pending["next_poll_at"] = time.time() + pending["poll_interval_seconds"]
        save_private_json(path, pending)
        code, response = request(
            pending["base"],
            f"/pairings/{pending['id']}/exchange",
            {"poll_secret": pending["poll_secret"]},
            timeout,
        )
        if code in {401, 403, 410}:
            path.unlink()
            return {
                "status": "restart_required",
                "message": "This pairing is unavailable. Start again.",
            }
        if code in {"incompatible", 404, 426}:
            return {
                "status": "incompatible",
                "message": "Waiting for a compatible Pensieve service; pairing retained.",
            }
        if code != 200 or not isinstance(response, dict):
            return {"status": "offline", "message": "Pairing will retry at the next agent hook."}
        if response.get("status") == "pending":
            return public_status(pending)
        if response.get("status") != "registered":
            return {"status": "offline", "message": "Pairing will retry at the next agent hook."}
        if not isinstance(response.get("context_id"), int) or isinstance(
            response["context_id"], bool
        ):
            raise ValueError("Invalid registration")
        if not valid_uuid(response.get("installation_id")):
            raise ValueError("Invalid registration")
        if (
            pending.get("expected_user_id") is not None
            and response.get("user_id") != pending["expected_user_id"]
        ) or (
            pending.get("expected_context_id") is not None
            and response.get("context_id") != pending["expected_context_id"]
        ):
            raise ValueError("Registration does not match the initiating connection")
        install_profile(
            config,
            {
                "user_id": response.get("user_id"),
                "client": client,
                "upload_key": response.get("upload_key"),
                "installation_id": response["installation_id"],
                "context_id": response["context_id"],
                "runtime": pending["runtime"],
                "host_version": pending["host_version"],
                "briefing_enabled": response.get("briefing_enabled") is True,
            },
        )
        if pending.get("session_id") is not None:
            # Capture, manual setup and other conversations can all win poll().
            # Commit the initiating conversation's registration before removing
            # the claim; config alone cannot identify it across accounts.
            save_private_json(
                pairing_receipt_path(config, client, pending["session_id"]),
                {
                    "pairing_id": pending["id"],
                    "user_id": response["user_id"],
                    "context_id": response["context_id"],
                },
            )
        path.unlink()
        return {
            "status": "paired",
            "client": client,
            "context_id": response["context_id"],
            "user_id": response["user_id"],
            "briefing_enabled": response.get("briefing_enabled") is True,
            "message": "Device connected. Transcript sharing is controlled in Pensieve.",
        }
