"""Deliver company briefings through private hook credentials.

A credential is registered by the member's own MCP sign-in: this helper binds its
pending claim to the call or thread its host sends to Pensieve, and that signed-in
call registers it. No MCP tool, OAuth credential store or model-directed command
participates. The server owns content and conversation selection. Local state
contains identities and delivery cursors only; capture remains independently
controlled by consent.
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
    private_directory,
    private_file,
    private_lock,
    save_private_json,
    valid_uuid,
)
from capture_onboarding import current_offer
from capture_pairing import (
    API_BASE,
    NoRedirects,
    completed_pairing,
    discard_claim,
    pending_claim,
    poll,
    start,
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

BRIEFING_ENDPOINT = "https://mcp.pensieve.uk/hooks/briefing"
BINDING_ENDPOINT = "https://mcp.pensieve.uk/hooks/tool-binding"
ENROLMENT_ENDPOINT = "https://mcp.pensieve.uk/hooks/enrolment"
MAX_RESPONSE_BYTES = 32 * 1024
HTTP_TIMEOUT_SECONDS = 3


def checked_endpoint(value: str) -> str:
    if value in {BRIEFING_ENDPOINT, BINDING_ENDPOINT, ENROLMENT_ENDPOINT}:
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
        or parsed.path not in {"/hooks/briefing", "/hooks/tool-binding", "/hooks/enrolment"}
        or (parsed.port is not None and parsed.port < 1)
    ):
        raise ValueError("Only the fixed Pensieve endpoint or HTTP loopback fixture is supported")
    return value


def request(
    endpoint: str, key: str | None, body: dict, timeout: float = HTTP_TIMEOUT_SECONDS
) -> tuple[int | str, dict | None]:
    headers = {"Content-Type": "application/json", "User-Agent": "Pensieve-Plugin-Briefing/1.0"}
    if key is not None:
        # Enrolment proves the pending claim's secret in its body instead.
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


CONNECTING = (
    "Pensieve connects this device's hooks on its first Pensieve tool call after MCP "
    "sign-in; company briefings start on the next prompt."
)


def bind_claim(
    config: Path, client: str, session: str, tool_use_id: str | None, endpoint: str, timeout: float
) -> bool:
    """Bind the pending claim to this call (Claude) or thread (Codex). Never a read."""
    claim = pending_claim(config, client)
    if claim is None:
        return False
    body = {
        "pairing_id": claim["id"],
        "poll_secret": claim["poll_secret"],
        "client": client,
        "session_id": session,
    }
    if tool_use_id is not None:
        body["tool_use_id"] = tool_use_id
    code, _ = request(endpoint, None, body, timeout=timeout)
    if code == 410:
        discard_claim(config, client, claim["id"])
    return code == 204


def claim_for(config: Path, client: str, session: str, state: dict, base: str) -> str:
    """Keep one claim pending for the conversation's known account and context."""
    owner, context = state.get("user_id"), state.get("context_id")
    return start(
        config,
        client,
        base=base,
        expected_user_id=owner,
        expected_context_id=context if owner else None,
        timeout=1,
        session_id=session,
    )["status"]


def connect(
    config: Path, client: str, session: str, state: dict, base: str, enrolment_endpoint: str
) -> str:
    """Codex binds its thread now; Claude binds its next Pensieve tool call."""
    retry = "Pensieve could not start connecting this device; it retries automatically."
    # Back off after a failed start so an offline machine does not spend each
    # prompt's budget retrying; a Pensieve tool call still starts one at once.
    if state.get("connect_retry_at", 0) > time.time() and pending_claim(config, client) is None:
        return retry
    status = claim_for(config, client, session, state, base)
    if status not in {"awaiting_registration", "another_connection_pending"}:
        state["connect_retry_at"] = time.time() + 300
        return retry
    state.pop("connect_retry_at", None)
    if client == "codex":
        bind_claim(config, client, session, None, enrolment_endpoint, timeout=1)
    return CONNECTING


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
    pairing_base: str = API_BASE,
    enrolment_endpoint: str = ENROLMENT_ENDPOINT,
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
            # No credential yet: bind this exact call so the member's signed-in
            # MCP request registers the device. Never block their tool call.
            try:
                if pending_claim(config, client) is None:
                    claim_for(config, client, session, state, pairing_base)
                bind_claim(
                    config,
                    client,
                    session,
                    tool_id,
                    enrolment_endpoint,
                    timeout=max(0.1, deadline - time.monotonic() - 0.25),
                )
            except BlockingIOError:
                pass
            return {}
        code, _, _, _ = try_profiles(
            binding_endpoint,
            profile,
            profiles,
            {"client": client, "session_id": session, "tool_use_id": tool_id},
            deadline,
        )
        return {} if code == 204 else failure(event, client, session)
    # Create the config folder privately before its state subfolder: mkdir would
    # otherwise give this parent umask permissions, which pairing then refuses.
    private_directory(config.parent)
    with private_lock(path.with_suffix(".lock")):
        state = read_state(path)
        needs_refresh = state.get("needs_refresh") is True
        # Persist before network work: a timeout or killed hook must not let an
        # older accepted receipt suppress the briefing that restores grounding.
        state["needs_refresh"] = True
        save_private_json(path, state)
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
        # an older pending claim completes on this hook. Never use the
        # old credential for a read or inherit a different account.
        if not state.get("user_id"):
            prior = select_profile(available_profiles(config, client, include_upload_only=True), {})
            if prior:
                state.update(user_id=prior["user_id"], context_id=prior.get("context_id"))
        # Pairing locks serialize with capture hooks. If another hook is already
        # exchanging, use existing credentials and let the next event retry.
        try:
            # A one-time exchange needs its full established response budget;
            # never start one in the shorter pre-tool authorization hook.
            poll(config, client, timeout=2)
        except BlockingIOError:
            pass
        paired = completed_pairing(config, client, session)
        if (
            paired
            and paired["pairing_id"] != state.get("pairing_id")
            and state.get("user_id") in {None, paired["user_id"]}
        ):
            state.update(
                user_id=paired["user_id"],
                context_id=paired["context_id"],
                pairing_id=paired["pairing_id"],
            )
            state.pop("pending_context_id", None)
            state.pop("preferred_credential", None)
        profiles = available_profiles(config, client)
        profile = select_profile(profiles, state)
        if profile is None:
            message = connect(config, client, session, state, pairing_base, enrolment_endpoint)
            save_private_json(path, state)
            return failure(event, client, session, message)
        state.setdefault("user_id", profile["user_id"])
        state.setdefault("context_id", profile.get("context_id"))
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
        # Reserve one full pairing-start attempt after a definitive rejection.
        # Otherwise slow rejected keys could consume every prompt's budget and
        # prevent reconnection from ever starting.
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
                    connect(config, client, session, state, pairing_base, enrolment_endpoint)
                    if deadline - time.monotonic() >= 1.25
                    else "Connecting this context retries on the next prompt."
                )
                save_private_json(path, state)
                return failure(event, client, session, message)
        if code == 200 and isinstance(result, dict):
            available = result.pop("briefing_available", True) is True
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
                if available:
                    state.pop("needs_refresh", None)
                save_private_json(path, state)
                return result
        message = ""
        if code in {401, 403} and deadline - time.monotonic() >= 1.25:
            if not selected_challenge:
                # Every key rejected. Keep the account but let registration use
                # the conversation's current context, not a deleted one.
                state["context_id"] = None
                state.pop("pending_context_id", None)
            message = connect(config, client, session, state, pairing_base, enrolment_endpoint)
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
    parser.add_argument("--enrolment-endpoint", type=checked_endpoint, default=ENROLMENT_ENDPOINT)
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
            args.enrolment_endpoint,
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
