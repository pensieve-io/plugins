"""Capture transitions shared by native hosts, independent of file and HTTP IO."""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from capture_adapters import CaptureEvent, capture_id
from capture_config import encoded, key_for
from capture_protocol import MAX_EVENT_CHARS

INTERNAL_MARKER = re.compile(
    r"<!-- pensieve-(?:capture-context|capture-consent|capture-setup|delivery)\b.*?-->", re.DOTALL
)
SECRET = re.compile(
    r"(?i)(\b(?:Bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,})|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|upload[_-]?key|poll[_-]?secret)"
    r"[\"']?\s*[=:]\s*[\"']?[^\s\"',}]+)"
)


def clean_content(text: str, keys: list[str]) -> tuple[str, bool]:
    text = INTERNAL_MARKER.sub("", text)
    for key in keys:
        text = text.replace(key, "[REDACTED_SECRET]")
    text = SECRET.sub("[REDACTED_SECRET]", text)
    # Do not upload inline binary attachments or PEM private keys.
    text = re.sub(r"data:[^\s]+;base64,[A-Za-z0-9+/=]+", "[Inline attachment omitted]", text)
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED_SECRET]",
        text,
        flags=re.DOTALL,
    )
    if len(text) > MAX_EVENT_CHARS:
        suffix = "\n[Content truncated by Pensieve capture]"
        return text[: MAX_EVENT_CHARS - len(suffix)] + suffix, True
    return text, False


def block(db, state: dict, reason: str) -> None:
    """A fresh attributed prompt is required; keep immutable queued work."""
    db.execute("DELETE FROM events WHERE segment IS NULL")
    state.update(
        phase="blocked",
        blocked_reason=reason,
        segment=None,
        scope=None,
        candidate_segment=None,
        candidate_scope=None,
        tail=None,
        fork_parent=None,
        title="",
        title_segment=None,
    )


def migrate_state(db, state: dict) -> None:
    """Upgrade private state once without rewriting any queued event or batch."""
    if "phase" not in state:
        state["phase"] = (
            "blocked"
            if state.get("ambiguous") or state.get("discard_until_prompt")
            else "awaiting_attribution"
            if state.get("awaiting_marker")
            else "capturing"
            if state.get("segment")
            else "ready"
        )
        if state["phase"] == "blocked":
            block(db, state, "unproven_boundary")
    for key in ("awaiting_marker", "ambiguous", "discard_until_prompt"):
        state.pop(key, None)
    adapter = state.setdefault("adapter", {})
    for key in ("turn_id", "pending_turn_id", "calls", "nested_calls", "claude_end_turn"):
        if key in state:
            adapter[key] = state.pop(key)


def scope_revoked(cutover, owner, context):
    explicit = f"{owner}:{context}"
    return (
        "*" in cutover["scopes"]
        or explicit in cutover["scopes"]
        or (owner in cutover["scopes"] and explicit not in cutover.get("preserved_scopes", []))
    )


def scope_authorised(state, configured, owner, context, offset):
    key = key_for(configured, owner, context)
    if key is None:
        return False
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    if any(
        offset < cutover["offset"] and scope_revoked(cutover, owner, context)
        for cutover in state.get("revocation_cutovers", [])
    ):
        return False
    return all(
        offset >= cutover["offset"] or key_for(cutover["profiles"], owner, context) == fingerprint
        for cutover in state.get("profile_cutovers", [])
    )


def apply_item(
    db,
    state,
    item: CaptureEvent,
    identity,
    occurred_at,
    client,
    session,
    configured,
    host_timestamp,
    source_offset=0,
):
    if item["kind"] == "user":
        state.pop("blocked_reason", None)
        if state.get("phase") == "awaiting_attribution":
            state["tail"] = None
            state["fork_parent"] = None
        # A later turn can never authorise an earlier unmarked one.
        db.execute("DELETE FROM events WHERE segment IS NULL")
        state.update(
            candidate_segment=state.get("segment"),
            candidate_scope=state.get("scope"),
            segment=None,
            scope=None,
            turn_group=identity,
            turn_source_offset=source_offset,
            phase="awaiting_attribution",
            turn_occurred_at=occurred_at if host_timestamp else None,
            capture_turn_id=capture_id(item.get("native_turn_id")) or identity,
            turn_closed=False,
        )
    if item["kind"] == "unknown_boundary":
        block(db, state, "unproven_boundary")
        return
    marker = item.get("marker") if item["kind"] == "attribution" else None
    if marker:
        if marker["kind"] == "prompt":
            if state.get("phase") != "awaiting_attribution":
                # Never apply a late/unpaired hook marker to another user turn.
                return
            if marker["turn_id"] is not None and marker["turn_id"] != state.get("adapter", {}).get(
                "turn_id"
            ):
                block(db, state, "native_turn_mismatch")
                return
            state["phase"] = "ready"
        elif state.get("phase") not in {"ready", "capturing"}:
            return
        owner, context = marker["user_id"], marker["context_id"]
        generation = marker["capture_generation"]
        previous = (
            state.get("candidate_scope") if marker["kind"] == "prompt" else state.get("scope")
        )
        previous_segment = (
            state.get("candidate_segment") if marker["kind"] == "prompt" else state.get("segment")
        )
        state.update(candidate_segment=None, candidate_scope=None)
        if (
            marker["kind"] == "selection"
            and previous is not None
            and previous[:2] == [owner, context]
            and previous[2] != generation
        ):
            # A changed opt-in period cannot authorise the rest of an old turn.
            # Wait for the next user prompt and its matching receipt.
            block(db, state, "consent_changed")
            return
        if marker["kind"] == "selection" and previous != [owner, context, generation]:
            # The preceding prompt belongs to the previous company/account.
            # A destination reached by a tool has no destination user title yet.
            state.update(title="", title_segment=None)
        state["scope"] = [owner, context, generation]
        authorised_offset = (
            state.get("turn_source_offset", source_offset)
            if marker["kind"] == "prompt"
            else source_offset
        )
        if (
            context is None
            or generation is None
            or not scope_authorised(state, configured, owner, context, authorised_offset)
        ):
            state["phase"] = "ready"
            state["segment"] = None
            state["tail"] = None
            state["fork_parent"] = None
            state.update(title="", title_segment=None)
            # An explicit disabled scope is not an upload backlog. Only its
            # current provisional user row is removed, never acknowledged work.
            if marker["kind"] == "prompt":
                db.execute(
                    "DELETE FROM events WHERE segment IS NULL AND turn_group=?",
                    (state.get("turn_group"),),
                )
            return
        if previous == [owner, context, generation] and previous_segment:
            # A fresh marker restores the candidate; the prior scope alone
            # never authorizes a new user turn. Resume keeps one segment.
            segment = previous_segment
        else:
            segment = str(
                uuid.uuid5(
                    uuid.UUID(session), f"{client}:{owner}:{context}:{generation}:{identity}"
                )
            )
        retired = db.execute("SELECT retired FROM segments WHERE id=?", (segment,)).fetchone()
        if retired and retired[0]:
            block(db, state, "segment_retired")
            return
        state["phase"] = "capturing"
        state["segment"] = segment
        state["title_segment"] = segment
        db.execute(
            "INSERT OR IGNORE INTO segments(id,owner,context,generation,title) VALUES (?,?,?,?,?)",
            (segment, owner, context, generation, state.get("title", "")),
        )
        if marker["kind"] == "prompt":
            # Only a provisional, never-uploaded prompt can be completed here.
            # Existing queued events and exact-byte batches are immutable.
            rows = db.execute(
                "SELECT id,body FROM events WHERE segment IS NULL AND turn_group=? ORDER BY sequence",
                (state.get("turn_group"),),
            ).fetchall()
            for row in rows:
                event = json.loads(row["body"])
                if "capture" in event:
                    fork = state.pop("fork_parent", None)
                    if fork and fork["scope"] == state["scope"]:
                        event["capture"]["parent"] = fork["reference"]
                    link_event(state, event, segment, session)
                    db.execute(
                        "UPDATE events SET body=? WHERE id=?", (encoded(event).decode(), row["id"])
                    )
                    db.execute(
                        "UPDATE anchors SET segment=? WHERE event_id=?", (segment, row["id"])
                    )
            db.execute(
                "UPDATE events SET segment=? WHERE segment IS NULL AND turn_group=?",
                (segment, state.get("turn_group")),
            )
        return
    scope = state.get("scope")
    if scope and not scope_authorised(state, configured, scope[0], scope[1], source_offset):
        block(db, state, "credential_changed")
    if item["kind"] != "user" and state.get("phase") == "awaiting_attribution":
        block(db, state, "missing_attribution")
    if state.get("phase") not in {"capturing", "awaiting_attribution"}:
        return
    # Only the current user prompt may wait provisionally for its own receipt.
    # Known disabled scopes never retain transcript text.
    if state.get("scope") is not None and state.get("segment") is None:
        return
    text, truncated = clean_content(item["content"], list(configured.values()))
    if item["kind"] == "turn_end":
        if not state.get("capture_turn_id") or state.get("turn_closed"):
            return
    elif not text and not item.get("tool_name"):
        return
    state["sequence"] += 1
    event = {
        "event_id": identity,
        "sequence": state["sequence"],
        "kind": item["kind"],
        "content": text,
        "occurred_at": occurred_at,
        "truncated": truncated or item.get("truncated", False),
    }
    if state.get("capture_turn_id"):
        event["capture"] = {"turn_id": state["capture_turn_id"]}
        if call_id := capture_id(item.get("tool_call_id")):
            event["capture"]["tool_call_id"] = call_id
        if name := item.get("tool_name"):
            event["capture"]["tool_name"] = name
        if item["kind"] == "turn_end":
            event["capture"]["completion"] = item["completion"]
            state["turn_closed"] = True
        if state.get("segment"):
            link_event(state, event, state["segment"], session)
    db.execute(
        "INSERT OR IGNORE INTO events(id,sequence,turn_group,segment,body,host_timestamp) VALUES (?,?,?,?,?,?)",
        (
            identity,
            state["sequence"],
            state.get("turn_group"),
            state.get("segment"),
            encoded(event).decode(),
            int(host_timestamp),
        ),
    )
    if item["kind"] == "user":
        state["title"] = text[:200]
        state["title_segment"] = None
    if state.get("segment"):
        db.execute(
            "UPDATE segments SET title=? WHERE id=? AND title=''",
            (state.get("title", ""), state["segment"]),
        )


def link_event(state, event, segment, session):
    """Link only to the previous retained event in this authorised portion."""
    tail = state.get("tail")
    if tail and tail["segment"] == segment:
        event["capture"]["parent"] = {"host_conversation_id": session, "event_id": tail["event_id"]}
    state["tail"] = {"segment": segment, "event_id": event["event_id"]}
