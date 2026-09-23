"""Offer browser consent once, using only authenticated native hook attribution.

No transcript is uploaded here. A remembered decline suppresses onboarding on every
installation. Changing the context/harness setting can offer connection again.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from capture_config import (
    MAX_CONFIG_BYTES,
    key_for,
    private_file,
    private_lock,
    save_private_json,
    valid_uuid,
)
from capture_pairing import pairing_path, start, timestamp
from context_receipt import accepted_contexts, compaction_boundary, json_object, transcript_tail
from conversation_capture import parse_marker

CONSENT_MARKER = re.compile(r"<!-- pensieve-capture-consent (\{[^\r\n]*?\}) -->")

SETUP_MARKER = re.compile(r"<!-- pensieve-capture-setup (\{[^\r\n]*?\}) -->")


def current_offer(path: object, client: str, session: str) -> tuple[dict, str | None] | None:
    tail = transcript_tail(path)
    if tail is None:
        return None
    lines, codex_session = tail
    for line in reversed(lines):
        record = json_object(line)
        if record is None:
            continue
        if compaction_boundary(record, session, codex_session, client):
            return None
        for content in reversed(accepted_contexts(record, session, codex_session, client)):
            marker = parse_marker(content, client, session, "prompt")
            if marker is None:
                continue
            # The newest native attribution wins, including a cleared context.
            if marker["context_id"] is None:
                return None
            consents = CONSENT_MARKER.findall(content)
            if len(consents) != 1:
                return None
            try:
                consent = json.loads(consents[0])
            except (ValueError, TypeError):
                return None
            if (
                not isinstance(consent, dict)
                or any(
                    consent.get(k) != marker[k]
                    for k in ("user_id", "client", "context_id", "conversation_id")
                )
                or consent.get("status") not in {"approved", "declined", "unknown"}
            ):
                return None
            marker = dict(marker, consent=consent["status"])
            request_id = None
            matches = SETUP_MARKER.findall(content)
            if len(matches) == 1:
                try:
                    request = json.loads(matches[0])
                    if (
                        isinstance(request, dict)
                        and all(
                            request.get(k) == marker[k]
                            for k in ("user_id", "client", "context_id", "conversation_id")
                        )
                        and valid_uuid(request.get("id"))
                        and timestamp(request.get("expires_at")) > time.time()
                    ):
                        request_id = request["id"]
                except (ValueError, TypeError):
                    pass
            return marker, request_id
    return None


def open_approval(url: str) -> None:
    # v1's accepted local runtime is macOS. The URL was validated by pairing;
    # no shell, credentials, model-directed command or transcript is involved.
    subprocess.Popen(
        ["/usr/bin/open", url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def offer_connection(payload: dict, client: str, session: str, config: Path, configured: dict):
    if sys.platform != "darwin":
        return
    offer = current_offer(payload.get("transcript_path"), client, session)
    if offer is None:
        return
    marker, request_id = offer
    if marker["consent"] == "declined":
        return
    owner, context = marker["user_id"], marker["context_id"]
    if key_for(configured, owner, context) is not None and request_id is None:
        return
    identity = f"{client}:{owner}:{context}"
    path = config.with_name("capture-onboarding.json")
    with private_lock(path.with_suffix(".lock")):
        try:
            state = json.loads(private_file(path, MAX_CONFIG_BYTES))
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            raise ValueError("Invalid onboarding state")
        previous = state.get(identity, {})
        if not isinstance(previous, dict):
            raise ValueError("Invalid onboarding state")
        generation = marker["capture_generation"]
        changed = previous.get("generation") != generation
        # An already-approved pending claim may simply be waiting for its next
        # poll. Do not replace that credential exchange when the marker changes.
        if changed and pairing_path(config, client).exists():
            try:
                pending = json.loads(private_file(pairing_path(config, client), MAX_CONFIG_BYTES))
                if (
                    pending.get("expected_user_id") == owner
                    and pending.get("expected_context_id") == context
                    and timestamp(pending["expires_at"]) > time.time()
                ):
                    return
            except (ValueError, KeyError, TypeError):
                pass
        if (request_id is None and previous.get("offered") and not changed) or (
            request_id is not None and previous.get("request_id") == request_id
        ):
            return
        if request_id is None and not changed and previous.get("retry_at", 0) > time.time():
            return
        # Remember attempts before I/O. Offline first-use attempts may retry
        # after five minutes; a displayed approval is never reopened unaided.
        state[identity] = {
            "offered": bool(previous.get("offered")) or request_id is not None,
            "request_id": request_id,
            "generation": generation,
            "retry_at": time.time() + 300,
        }
        save_private_json(path, state)
        result = start(
            config,
            client,
            expected_user_id=owner,
            expected_context_id=context,
            restart=request_id is not None or changed,
            timeout=0.5,
        )
        if result["status"] == "awaiting_approval":
            state[identity]["offered"] = True
            save_private_json(path, state)
            open_approval(result["verification_url"])
