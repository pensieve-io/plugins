"""Ask once whether to share transcripts, using only authenticated hook attribution.

No transcript is uploaded and no credential is issued here: the helper is already
connected through MCP sign-in, so the page only records the member's choice. A
remembered decline suppresses the prompt on every installation; later changes
happen in the context's connector settings.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from capture_adapters import parse_marker
from capture_config import (
    MAX_CONFIG_BYTES,
    key_for,
    private_file,
    private_lock,
    save_private_json,
)
from context_receipt import accepted_contexts, compaction_boundary, json_object, transcript_tail

CONSENT_MARKER = re.compile(r"<!-- pensieve-capture-consent (\{[^\r\n]*?\}) -->")
CONSENT_PAGE = "https://app.pensieve.uk/oauth/conversation-capture"
OFFER_INTERVAL_SECONDS = 600


def current_offer(path: object, client: str, session: str) -> dict | None:
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
            return marker
    return None


def open_page(url: str) -> None:
    # v1's accepted local runtime is macOS. The URL is built from fixed parts;
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
    if offer is None or offer["consent"] != "unknown":
        return
    owner, context = offer["user_id"], offer["context_id"]
    # Only a connected helper can record a choice; connecting is MCP's job.
    if key_for(configured, owner, context) is None:
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
        # Once per conversation, and not again for a while: closing the page
        # without choosing asks again later, never on every prompt.
        if previous.get("session") == session or previous.get("retry_at", 0) > time.time():
            return
        state[identity] = {"session": session, "retry_at": time.time() + OFFER_INTERVAL_SECONDS}
        save_private_json(path, state)
    open_page(f"{CONSENT_PAGE}?client={client}&context_id={int(context)}")
