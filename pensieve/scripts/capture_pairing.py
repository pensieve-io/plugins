"""Browser-approved pairing and opportunistic hooks. Never emit credentials."""

from __future__ import annotations

import ipaddress
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
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

API_BASE = "https://api.pensieve.uk/users/me/conversation-capture"
PLUGIN_VERSION = "capture-pairing-1"
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
    headers = {"Content-Type": "application/json", "User-Agent": "Pensieve-Plugin-Pairing/1.0"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = Request(
        checked_base(base) + path,
        data=encoded(body) if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(req, timeout=max(0.05, timeout)) as response:
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


def public_status(pending: dict) -> dict:
    return {
        "status": "awaiting_approval",
        "verification_url": pending["verification_url"],
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
    parsed = urlsplit(value["verification_url"])
    if (
        parsed.scheme != "https"
        or parsed.netloc != "app.pensieve.uk"
        or parsed.path != "/oauth/conversation-capture"
        or parsed.fragment
        or parse_qs(parsed.query) != {"pairing_id": [value["id"]]}
    ):
        raise ValueError("Invalid approval address")
    if value.get("runtime") not in RUNTIMES:
        raise ValueError("Invalid pairing runtime")
    interval = value.get("poll_interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) or not 1 <= interval <= 60:
        raise ValueError("Invalid pairing interval")
    return value


def start(
    config: Path,
    client: str,
    runtime: str = "unknown",
    host_version: str = "",
    base: str = API_BASE,
    restart: bool = False,
    expected_user_id: str | None = None,
    expected_context_id: int | None = None,
    timeout: float = 5,
) -> dict:
    if runtime not in RUNTIMES or len(host_version) > 100:
        raise ValueError("Unsupported runtime")
    if runtime.startswith("codex") and client != "codex":
        raise ValueError("Runtime does not match client")
    if runtime.startswith("claude") and client != "claude":
        raise ValueError("Runtime does not match client")
    if (expected_user_id is None) != (expected_context_id is None):
        raise ValueError("Pairing identity must include account and context")
    if expected_user_id is not None and (
        not valid_uuid(expected_user_id)
        or type(expected_context_id) is not int
        or expected_context_id <= 0
    ):
        raise ValueError("Invalid pairing identity")
    path = pairing_path(config, client)
    with private_lock(path.with_suffix(".lock")):
        if path.exists() or path.is_symlink():
            old = validate_pending(json.loads(private_file(path, MAX_CONFIG_BYTES)), client)
            if not restart and timestamp(old["expires_at"]) > time.time():
                if (
                    old.get("expected_user_id") == expected_user_id
                    and old.get("expected_context_id") == expected_context_id
                ):
                    return public_status(old)
                return {"status": "another_connection_pending"}
            path.unlink()
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
                "verification_url": response.get("verification_url"),
                "expires_at": response.get("expires_at"),
                "poll_interval_seconds": response.get("poll_interval_seconds"),
                "base": checked_base(base),
                "client": client,
                "runtime": runtime,
                "host_version": host_version,
                "next_poll_at": 0,
                "expected_user_id": expected_user_id,
                "expected_context_id": expected_context_id,
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
        if timestamp(pending["expires_at"]) <= time.time():
            path.unlink()
            return {"status": "expired", "message": "Pairing expired. Start again."}
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
        if code in {401, 403, 404, 410}:
            path.unlink()
            return {
                "status": "restart_required",
                "message": "This pairing is unavailable. Start again.",
            }
        if code != 200 or not isinstance(response, dict):
            return {"status": "offline", "message": "Pairing will retry at the next agent hook."}
        if response.get("status") == "pending":
            return public_status(pending)
        if response.get("status") != "approved":
            return {"status": "offline", "message": "Pairing will retry at the next agent hook."}
        if not isinstance(response.get("context_id"), int) or isinstance(
            response["context_id"], bool
        ):
            raise ValueError("Invalid approval")
        if not valid_uuid(response.get("installation_id")):
            raise ValueError("Invalid approval")
        if pending.get("expected_user_id") is not None and (
            response.get("user_id") != pending["expected_user_id"]
            or response.get("context_id") != pending["expected_context_id"]
        ):
            raise ValueError("Approval does not match the initiating connection")
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
            },
        )
        path.unlink()
        return {
            "status": "paired",
            "client": client,
            "context_id": response["context_id"],
            "message": "Device paired. Capture and company contribution are controlled in Pensieve.",
        }


def wait(config: Path, client: str, seconds: float) -> dict:
    deadline = time.monotonic() + min(max(seconds, 0), 50)
    while True:
        result = poll(config, client, timeout=min(2, max(0.05, deadline - time.monotonic())))
        if result["status"] not in {"awaiting_approval", "offline"}:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        time.sleep(min(1, remaining))
