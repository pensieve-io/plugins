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
    config_lock,
    encoded,
    install_profile,
    load_config,
    private_file,
    private_lock,
    save_private_json,
    valid_uuid,
)

API_BASE = "https://api.pensieve.uk/users/me/conversation-capture"
PLUGIN_VERSION = "capture-pairing-1"
RUNTIMES = {
    "codex_cli",
    "codex_desktop",
    "claude_code_cli",
    "claude_code_desktop",
    "claude_cowork",
    "unknown",
}


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


def request(base: str, path: str, body: dict, timeout: float, key: str | None = None):
    headers = {"Content-Type": "application/json", "User-Agent": "Pensieve-Plugin-Pairing/1.0"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = Request(checked_base(base) + path, data=encoded(body), headers=headers, method="POST")
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
) -> dict:
    if runtime not in RUNTIMES or len(host_version) > 100:
        raise ValueError("Unsupported runtime")
    if runtime.startswith("codex") and client != "codex":
        raise ValueError("Runtime does not match client")
    if runtime.startswith("claude") and client != "claude":
        raise ValueError("Runtime does not match client")
    path = pairing_path(config, client)
    with private_lock(path.with_suffix(".lock")):
        if path.exists() or path.is_symlink():
            old = validate_pending(json.loads(private_file(path, MAX_CONFIG_BYTES)), client)
            if not restart and timestamp(old["expires_at"]) > time.time():
                return public_status(old)
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
            },
            5,
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
        install_profile(
            config,
            {
                "user_id": response.get("user_id"),
                "client": client,
                "upload_key": response.get("upload_key"),
                "installation_id": response["installation_id"],
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


def heartbeat(
    config: Path, client: str, transcript_available: bool, timeout: float, base: str = API_BASE
) -> None:
    """Report only observed hook availability, without identifying private work."""
    if not config.exists():
        return
    path = config.with_name(f"capture-heartbeat-{client}.json")
    with private_lock(path.with_suffix(".lock")):
        try:
            state = json.loads(private_file(path, MAX_CONFIG_BYTES))
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            raise ValueError("Invalid heartbeat state")
        with config_lock(config):
            value = load_config(config, client)
        deadline = time.monotonic() + timeout
        for profile in value["profiles"]:
            identity = profile["installation_id"]
            if profile["client"] != client or identity is None or time.monotonic() >= deadline:
                continue
            if state.get(identity, 0) > time.time() - 60:
                continue
            # Existing hooks do not establish which desktop/CLI runtime invoked
            # them. Do not turn successful hook execution into a platform claim.
            request(
                base,
                "/installations/heartbeat",
                {
                    "runtime": "unknown",
                    "plugin_version": PLUGIN_VERSION,
                    "host_version": "",
                    "transcript_available": transcript_available,
                },
                deadline - time.monotonic(),
                key=profile["upload_key"],
            )
            state[identity] = time.time()
        live = {p["installation_id"] for p in value["profiles"] if p["installation_id"]}
        save_private_json(
            path, {identity: at for identity, at in state.items() if identity in live}
        )
