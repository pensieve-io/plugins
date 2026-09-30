"""Deliver company briefings through private, browser-approved hook credentials.

No MCP tool, OAuth credential store or model-directed command participates. The
server owns content and conversation selection. Local state contains identities
and delivery cursors only; capture remains independently controlled by consent.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from capture_config import (
    CONFIG_PATH,
    MAX_CONFIG_BYTES,
    load_config,
    private_file,
    private_lock,
    save_private_json,
    valid_uuid,
)
from capture_onboarding import current_offer, open_approval
from capture_pairing import API_BASE, NoRedirects, pairing_path, poll, start
from context_receipt import (
    DELIVERY_ENDPOINT,
    MAX_INPUT_BYTES,
    conversation_id,
    fallback_primer,
    json_object,
    latest_receipt,
    send_receipt,
)

BRIEFING_ENDPOINT = "https://mcp.pensieve.uk/hooks/briefing"
BINDING_ENDPOINT = "https://mcp.pensieve.uk/hooks/tool-binding"
MAX_RESPONSE_BYTES = 32 * 1024
HTTP_TIMEOUT_SECONDS = 3


def checked_endpoint(value: str) -> str:
    if value in {BRIEFING_ENDPOINT, BINDING_ENDPOINT}:
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
        or parsed.path not in {"/hooks/briefing", "/hooks/tool-binding"}
        or (parsed.port is not None and parsed.port < 1)
    ):
        raise ValueError("Only the fixed Pensieve endpoint or HTTP loopback fixture is supported")
    return value


def request(
    endpoint: str, key: str, body: dict, timeout: float = HTTP_TIMEOUT_SECONDS
) -> tuple[int | str, dict | None]:
    req = Request(
        checked_endpoint(endpoint),
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "Pensieve-Plugin-Briefing/1.0",
        },
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
        # Only the structured context-scope challenge is useful. Never echo a
        # provider error, credential or response body into the host transcript.
        if exc.code == 409:
            try:
                value = json.loads(exc.read(4097))
                if (
                    isinstance(value, dict)
                    and value.get("detail") == "pairing_required"
                    and valid_uuid(value.get("user_id"))
                    and type(value.get("context_id")) is int
                    and value["context_id"] > 0
                ):
                    return 409, {k: value[k] for k in ("detail", "user_id", "context_id")}
            except (OSError, ValueError):
                pass
        return exc.code, None
    except (OSError, URLError, ValueError):
        return "unavailable", None


def available_profiles(
    config: Path, client: str, *, include_upload_only: bool = False
) -> list[dict]:
    # Config replacement is atomic; independent read-only hooks need no shared
    # exclusive lock, which would spuriously deny parallel tool calls.
    value = load_config(config, client)
    return [
        p
        for p in value["profiles"]
        if value["version"] == 3
        and p["client"] == client
        and (include_upload_only or p.get("briefing_enabled") is True)
    ]


def select_profile(profiles: list[dict], identity: dict) -> dict | None:
    owner, context = identity.get("user_id"), identity.get("context_id")
    if owner:
        candidates = [p for p in profiles if p["user_id"] == owner]
    else:
        # One signed-in account is unambiguous. Never guess between accounts.
        if len({p["user_id"] for p in profiles}) != 1:
            return None
        candidates = profiles
    pending = identity.get("pending_context_id")
    if pending is not None:
        scoped = [p for p in candidates if p.get("context_id") == pending]
        if scoped:
            return scoped[0]
    preferred = identity.get("preferred_credential")
    for candidate in candidates:
        if preferred == hashlib.sha256(candidate["upload_key"].encode()).hexdigest():
            return candidate
    exact = [p for p in candidates if p.get("context_id") == context]
    if exact:
        return exact[0]
    # This only establishes the account. The server resolves the conversation's
    # saved selection and challenges for a correctly scoped key if necessary.
    return sorted(candidates, key=lambda p: p.get("context_id") or 0)[0] if candidates else None


def try_profiles(endpoint: str, profile: dict, profiles: list[dict], body: dict, deadline: float):
    """Recover definitive key rejection within the same authenticated account.

    Context selection remains server-owned. A timeout or network failure never
    licenses switching credentials. Reserve time for each alternative so a slow
    rejection cannot consume the entire host deadline.
    """
    candidates = [profile] + [
        p
        for p in profiles
        if p["user_id"] == profile["user_id"] and p["upload_key"] != profile["upload_key"]
    ]
    rejected = set()
    for index, candidate in enumerate(candidates):
        remaining = deadline - time.monotonic() - 0.25
        if remaining < 0.1:
            return "unavailable", None, candidate, rejected
        timeout = min(HTTP_TIMEOUT_SECONDS, remaining / min(2, len(candidates) - index))
        code, result = request(endpoint, candidate["upload_key"], body, timeout=timeout)
        if code not in {401, 403}:
            return code, result, candidate, rejected
        rejected.add(candidate["upload_key"])
    return code, result, candidate, rejected


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


def offer_pairing(config: Path, client: str, session: str, state: dict, base: str) -> str:
    pending = pairing_path(config, client)
    if pending.exists() or pending.is_symlink():
        return "Complete the Pensieve browser connection already awaiting approval."
    owner, context = state.get("user_id"), state.get("context_id")
    identity = f"{owner}:{context}"
    previous = state.get("offer", {})
    if previous.get("identity") == identity and previous.get("session") == session:
        if previous.get("opened") or previous.get("retry_at", 0) > time.time():
            return "Complete the Pensieve browser connection, or reconnect in Pensieve settings."
    state["offer"] = {"identity": identity, "session": session, "retry_at": time.time() + 300}
    result = start(
        config,
        client,
        base=base,
        expected_user_id=owner,
        expected_context_id=context if owner else None,
        timeout=1,
    )
    if result["status"] == "awaiting_approval":
        if sys.platform == "darwin":
            open_approval(result["verification_url"])
        state["offer"]["opened"] = True
        return "Approve this device for company briefings: " + result["verification_url"]
    return "Pensieve could not start the browser connection; it will retry automatically."


def failure(event: str, client: str, session: str | None, reason: str = "") -> dict:
    if event == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": "Pensieve could not verify this conversation. "
                "Complete the plugin browser connection or retry after it reconnects.",
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
    pairing_base: str = API_BASE,
) -> dict:
    event = payload.get("hook_event_name")
    deadline = time.monotonic() + (4.25 if event == "PreToolUse" else 8.5)
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
    path = state_path(config, client, session)
    if event == "PreToolUse":
        # Each tool has its own exact server binding. Reading atomic identity
        # state needs no conversation lock, so parallel calls remain parallel.
        state = read_state(path)
        offer = current_offer(payload.get("transcript_path"), client, session)
        if offer:
            state.update(user_id=offer["user_id"], context_id=offer["context_id"])
        profiles = available_profiles(config, client)
        profile = select_profile(profiles, state)
        if profile is None:
            return failure(event, client, session)
        code, _, _, _ = try_profiles(
            binding_endpoint,
            profile,
            profiles,
            {"client": client, "session_id": session, "tool_use_id": tool_id},
            deadline,
        )
        return {} if code == 204 else failure(event, client, session)
    with private_lock(path.with_suffix(".lock")):
        state = read_state(path)
        # Native accepted attribution outranks local identity state, including
        # switching to another account after reconnecting the host.
        offer = current_offer(payload.get("transcript_path"), client, session)
        if offer:
            if offer["user_id"] != state.get("user_id"):
                state.pop("pending_context_id", None)
                state.pop("preferred_credential", None)
            state.update(
                user_id=offer["user_id"],
                context_id=state.get("pending_context_id", offer["context_id"]),
            )
        # Upgrading an existing installation keeps its known account even if
        # an older pending browser claim completes on this hook. Never use the
        # old credential for a read or inherit a different browser account.
        if not state.get("user_id"):
            prior = select_profile(available_profiles(config, client, include_upload_only=True), {})
            if prior:
                state.update(user_id=prior["user_id"], context_id=prior.get("context_id"))
        # Pairing locks serialize with capture hooks. If another hook is already
        # exchanging, use existing credentials and let the next event retry.
        paired = {}
        try:
            # A one-time exchange needs its full established response budget;
            # never start one in the shorter pre-tool authorization hook.
            paired = poll(config, client, timeout=2)
        except BlockingIOError:
            pass
        if paired.get("status") == "paired" and state.get("user_id") in {None, paired["user_id"]}:
            state.update(user_id=paired["user_id"], context_id=paired["context_id"])
            state.pop("pending_context_id", None)
            state.pop("preferred_credential", None)
        profiles = available_profiles(config, client)
        profile = select_profile(profiles, state)
        if profile is None:
            message = offer_pairing(config, client, session, state, pairing_base)
            save_private_json(path, state)
            return failure(event, client, session, message)
        state.setdefault("user_id", profile["user_id"])
        state.setdefault("context_id", profile.get("context_id"))
        receipt = latest_receipt(payload.get("transcript_path"), session, client)
        force = event == "SessionStart" or receipt is None or receipt[1] == "reset"
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
        # Reserve one full pairing-start attempt after a definitive rejection.
        # Otherwise slow rejected keys could consume every prompt's budget and
        # prevent the browser recovery from ever starting.
        request_deadline = deadline - 1.25
        code, result, profile, rejected = try_profiles(
            endpoint, profile, profiles, body, request_deadline
        )
        selected_challenge = False
        if code == 409 and result and result["user_id"] == profile["user_id"]:
            selected_challenge = True
            state.update(
                user_id=result["user_id"],
                context_id=result["context_id"],
                pending_context_id=result["context_id"],
            )
            scoped = [
                p
                for p in profiles
                if p["user_id"] == result["user_id"]
                and p.get("context_id") == result["context_id"]
                and p["upload_key"] not in rejected
            ]
            if scoped:
                if deadline - time.monotonic() < HTTP_TIMEOUT_SECONDS + 0.25:
                    save_private_json(path, state)
                    return failure(
                        event, client, session, "The updated Context will load on the next prompt."
                    )
                profile = scoped[0]
                code, result, profile, _ = try_profiles(
                    endpoint, profile, scoped, body, request_deadline
                )
            else:
                message = (
                    offer_pairing(config, client, session, state, pairing_base)
                    if deadline - time.monotonic() >= 1.25
                    else "The browser connection will retry on the next prompt."
                )
                save_private_json(path, state)
                return failure(event, client, session, message)
        if code == 200 and isinstance(result, dict):
            output = result.get("hookSpecificOutput")
            if result == {} or (
                isinstance(output, dict)
                and isinstance(output.get("additionalContext", ""), str)
                and output.get("hookEventName") == event
            ):
                # Credentials remain solely in the config; state carries no key.
                state.pop("pending_context_id", None)
                state["preferred_credential"] = hashlib.sha256(
                    profile["upload_key"].encode()
                ).hexdigest()
                save_private_json(path, state)
                return result
        message = ""
        if code in {401, 403} and deadline - time.monotonic() >= 1.25:
            if not selected_challenge:
                # Every approved key rejected. Keep the account but let the
                # browser choose among current memberships, not a deleted one.
                state["context_id"] = None
                state.pop("pending_context_id", None)
            message = offer_pairing(config, client, session, state, pairing_base)
        save_private_json(path, state)
        print(
            f"Pensieve briefing unavailable ({code}); automatic retry remains enabled.",
            file=sys.stderr,
        )
        return failure(event, client, session, message)


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
    parser.add_argument("--pairing-base", default=API_BASE)
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
            args.pairing_base,
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
