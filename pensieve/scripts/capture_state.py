"""Capture transitions shared by native hosts, independent of file and HTTP IO."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from capture_adapters import IMAGE_REFERENCE, CaptureEvent, capture_id
from capture_config import encoded, key_for
from capture_protocol import MAX_EVENT_CHARS
from secret_masking import MASK, mask_secrets

# Local exposure coverage, independent of the upload protocol. Increment when
# previously consumed parent records could contain newly recognised exposure.
CONTEXT_BOUNDARY_VERSION = 3
# Turns held for a conversation's first company stay in the private spool. Past
# this many bytes the conversation stops holding, so one that never names a
# company cannot fill the spool that queued uploads share.
HELD_LIMIT_BYTES = 2 * 1024 * 1024
# A held turn waits this long for the next marked turn. An abandoned
# conversation raises no event, so only the gap can show that one resumed later
# is no longer the conversation in progress.
HELD_MAX_AGE = timedelta(days=7)
# A turn whose briefing never arrived keeps its events in the private spool
# until its own Stop asks Pensieve for the marker it missed. Past this many
# bytes it is given up early, so one long unmarked turn cannot fill the spool.
UNMARKED_LIMIT_BYTES = 2 * 1024 * 1024

INTERNAL_MARKER = re.compile(
    r"<!-- pensieve-(?:capture-context|capture-consent|capture-setup|delivery)\b.*?-->", re.DOTALL
)


def clean_content(text: str, keys: list[str]) -> tuple[str, bool]:
    text = INTERNAL_MARKER.sub("", text)
    for key in keys:
        text = text.replace(key, MASK)
    # Do not upload inline binary attachments. The media type is bounded so a
    # long run of `data:` cannot make the hook rescan it.
    text = re.sub(r"data:[^\s,]{0,200};base64,[A-Za-z0-9+/=]+", "[Inline attachment omitted]", text)
    text = mask_secrets(text)
    if len(text) > MAX_EVENT_CHARS:
        suffix = "\n[Content truncated by Pensieve capture]"
        return text[: MAX_EVENT_CHARS - len(suffix)] + suffix, True
    return text, False


def drop_held(db, state: dict, *, abandon: bool = False) -> None:
    """Forget every turn held for this conversation's first company.

    `abandon` also stops the conversation holding later turns.
    """
    for turn in state.pop("held_turns", []):
        db.execute(
            "DELETE FROM events WHERE segment IS NULL AND turn_group=?",
            (turn["group"],),
        )
    state.pop("held_bytes", None)
    state.pop("held_title", None)
    if state.get("phase") == "holding":
        state["phase"] = "ready"
    if abandon:
        state["hold_abandoned"] = True


def may_hold(state: dict, configured: dict, marker: dict, offset: int) -> bool:
    """Whether this marked turn may wait for the conversation's first company.

    A marker without a destination is held only before any company was used,
    never after a second. One naming a company with sharing off is held only
    while the member has not yet answered the sharing page: a declined or
    unreadable choice retains nothing. Like any turn, it needs a key unchanged
    since it was written: every later change drops what is held.
    """
    owner, context = marker["user_id"], marker["context_id"]
    if state.get("hold_abandoned") or not scope_authorised(
        state, configured, owner, context, offset
    ):
        return False
    if context is None:
        return not state.get("contexts_used")
    return marker["capture_generation"] is None and marker.get("consent") == "unknown"


def block(db, state: dict, reason: str) -> None:
    """A fresh attributed prompt is required; keep immutable queued work."""
    drop_held(db, state)
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


def abandon_unmarked(db, state: dict) -> None:
    """Give up a turn whose marker never came: count it, and require a fresh marker.

    The count is what `--status` and the next prompt's notice report, since an
    unsaved turn otherwise leaves no trace once later turns save again.
    """
    if state.get("phase") != "unmarked":
        return
    state.pop("unmarked_bytes", None)
    state["unsaved_turns"] = state.get("unsaved_turns", 0) + 1
    block(db, state, "missing_attribution")


def attribute_late(db, state: dict, marker: dict, client, session, configured) -> bool:
    """Attribute the unmarked turn with the marker Pensieve gave for it at its own Stop.

    The marker passes every check a prompt's own marker does, against the
    turn's starting position, so a key changed or removed since the prompt was
    written still refuses it. Only the turn awaiting it can take it: a later
    turn never authorises an earlier one, and a transcript marker that arrives
    after the turn's first answer is still ignored.
    """
    if state.get("phase") != "unmarked" or state.get("capture_stopped"):
        return False
    waited = state.pop("unmarked_bytes", 0)
    state["phase"] = "awaiting_attribution"
    apply_item(
        db,
        state,
        {"kind": "attribution", "marker": marker},
        f"{state.get('turn_group')}:late",
        datetime.now(timezone.utc).isoformat(),
        client,
        session,
        configured,
        False,
        source_offset=state.get("turn_source_offset", 0),
    )
    if state.get("phase") == "awaiting_attribution":
        state.update(phase="unmarked", unmarked_bytes=waited)
        return False
    return state.get("phase") in {"capturing", "holding"}


def withdraw_turn(db, state: dict) -> None:
    """Drop the current turn's unsent events; exact-byte batches stay immutable.

    The turn's prompt was attributed before its tools named a second context,
    and may describe that company. Hooks checkpoint between turns, so the turn
    is normally still unsent when the switch is seen.
    """
    sent = set()
    for (ids,) in db.execute("SELECT event_ids FROM batches"):
        sent.update(json.loads(ids))
    rows = db.execute("SELECT id FROM events WHERE turn_group=?", (state.get("turn_group"),))
    for (event_id,) in rows.fetchall():
        if event_id not in sent:
            db.execute("DELETE FROM anchors WHERE event_id=?", (event_id,))
            db.execute("DELETE FROM events WHERE id=?", (event_id,))


def stop_capture(db, state: dict) -> None:
    """A second company stops saving for the rest of this host conversation.

    Earlier turns sit in the agent's window and could reach any later
    transcript, so nothing restarts it, not even a return to the first company.
    """
    state["capture_stopped"] = True
    withdraw_turn(db, state)
    block(db, state, "multiple_contexts")


def record_context_use(db, state: dict, context_id: int) -> None:
    """A conversation is saved to one context: the first its tools or briefings expose."""
    used = state.setdefault("contexts_used", [])
    if context_id not in used:
        used.append(context_id)
    if len(used) > 1:
        stop_capture(db, state)


def discard_conversation(db, state: dict, reason: str) -> None:
    """Stop for good and erase every unsent byte; accepted server rows remain."""
    state["capture_stopped"] = True
    for table in ("events", "anchors", "batches"):
        db.execute(f"DELETE FROM {table}")
    db.execute("UPDATE segments SET retired=1,title=''")
    state.pop("images", None)
    block(db, state, reason)


def migrate_state(db, state: dict) -> None:
    """Fence a spool whose history was consumed with older exposure detection.

    Older helpers did not observe every company exposure, so their pending bytes
    are never treated as verified by the new parser and the conversation never resumes.
    """
    if state.get("context_boundary_version") != CONTEXT_BOUNDARY_VERSION:
        discard_conversation(db, state, "capture_upgrade")
        state.update(context_boundary_version=CONTEXT_BOUNDARY_VERSION, adapter={})


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
    if state.get("capture_stopped"):
        return
    if item["kind"] == "context_use":
        # Marker contexts are recorded below.
        record_context_use(db, state, item["context_id"])
        return
    if item["kind"] == "user":
        # The previous turn's Stop never attributed it: it is not saved.
        abandon_unmarked(db, state)
        state.pop("blocked_reason", None)
        if state.get("phase") == "awaiting_attribution":
            state["tail"] = None
            state["fork_parent"] = None
        # A later turn can never authorise an earlier unmarked one. Turns held
        # for the conversation's first company each carried their own marker.
        held = [turn["group"] for turn in state.get("held_turns", [])]
        db.execute(
            "DELETE FROM events WHERE segment IS NULL"
            " AND (turn_group IS NULL OR turn_group NOT IN (%s))" % ",".join("?" * len(held)),
            held,
        )
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
        owner, context = marker["user_id"], marker["context_id"]
        generation = marker["capture_generation"]
        if context is not None:
            # A briefing is company exposure even without transcript consent or
            # a pairing user turn. Retain it across account changes.
            record_context_use(db, state, context)
        if state.get("capture_stopped"):
            return
        if state.get("phase") != "awaiting_attribution":
            # Never apply a late/unpaired hook marker to another user turn.
            return
        if marker["turn_id"] is not None and marker["turn_id"] != state.get("adapter", {}).get(
            "turn_id"
        ):
            block(db, state, "native_turn_mismatch")
            return
        previous = state.get("candidate_scope")
        previous_segment = state.get("candidate_segment")
        state.update(phase="ready", candidate_segment=None, candidate_scope=None)
        state["scope"] = [owner, context, generation]
        turn_offset = state.get("turn_source_offset", source_offset)
        # The prompt's own time where the host recorded one, else this marker's.
        turn_at = state.get("turn_occurred_at") or occurred_at
        held = state.get("held_turns")
        if held and (
            held[-1]["owner"] != owner
            or datetime.fromisoformat(turn_at) - datetime.fromisoformat(held[-1]["at"])
            > HELD_MAX_AGE
        ):
            # Another account's turns, or a conversation resumed too late.
            drop_held(db, state)
        if (
            context is None
            or generation is None
            or not scope_authorised(state, configured, owner, context, turn_offset)
        ):
            if may_hold(state, configured, marker, turn_offset):
                # The destination is not known yet. Keep this marked turn in
                # the private spool until the conversation's first company
                # names one with sharing on; nothing uploads before then.
                state.setdefault("held_turns", []).append(
                    {"group": state.get("turn_group"), "owner": owner, "at": turn_at}
                )
                state.setdefault("held_title", state.get("title", ""))
                state["held_bytes"] = (
                    state.get("held_bytes", 0)
                    + db.execute(
                        "SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0) FROM events"
                        " WHERE segment IS NULL AND turn_group=?",
                        (state.get("turn_group"),),
                    ).fetchone()[0]
                )
                state.update(phase="holding", segment=None, tail=None, fork_parent=None)
                return
            # Sharing is off or the key changed: none of the held turns can
            # be attributed either.
            drop_held(db, state)
            state.update(segment=None, tail=None, fork_parent=None, title="", title_segment=None)
            # An explicit disabled scope is not an upload backlog. Only its
            # current provisional user row is removed, never acknowledged work.
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
        # What is still held is this owner's, under a key unchanged since.
        held = state.pop("held_turns", [])
        title = state.pop("held_title", None)
        state.pop("held_bytes", None)
        state["phase"] = "capturing"
        state["segment"] = segment
        state["title_segment"] = segment
        db.execute(
            "INSERT OR IGNORE INTO segments(id,owner,context,generation,title) VALUES (?,?,?,?,?)",
            (segment, owner, context, generation, title or state.get("title", "")),
        )
        # Only never-uploaded work can be completed here: this turn's
        # provisional prompt, and the marked turns held since the conversation
        # began. Existing queued events and exact-byte batches are immutable.
        groups = [turn["group"] for turn in held] + [state.get("turn_group")]
        marks = ",".join("?" * len(groups))
        rows = db.execute(
            "SELECT id,body FROM events WHERE segment IS NULL AND turn_group IN (%s) "
            "ORDER BY sequence" % marks,
            groups,
        ).fetchall()
        for row in rows:
            event = json.loads(row["body"])
            if "capture" in event:
                link_event(state, event, segment, session)
                db.execute(
                    "UPDATE events SET body=? WHERE id=?", (encoded(event).decode(), row["id"])
                )
                db.execute("UPDATE anchors SET segment=? WHERE event_id=?", (segment, row["id"]))
        db.execute(
            "UPDATE events SET segment=? WHERE segment IS NULL AND turn_group IN (%s)" % marks,
            [segment, *groups],
        )
        return
    scope = state.get("scope")
    if scope and not scope_authorised(state, configured, scope[0], scope[1], source_offset):
        block(db, state, "credential_changed")
    if item["kind"] != "user" and state.get("phase") == "awaiting_attribution":
        # The prompt's marker did not come before its answer, so its briefing
        # failed. The turn waits, unsent, for the marker its Stop asks for.
        state.update(phase="unmarked", unmarked_bytes=0)
    if state.get("phase") not in {"capturing", "awaiting_attribution", "holding", "unmarked"}:
        return
    # Only the current user prompt may wait provisionally for its own receipt.
    # Known disabled scopes never retain transcript text; a turn held for the
    # conversation's first company is the one marked exception.
    holding = state.get("phase") == "holding"
    if state.get("scope") is not None and state.get("segment") is None and not holding:
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
    body = encoded(event).decode()
    db.execute(
        "INSERT OR IGNORE INTO events(id,sequence,turn_group,segment,body,host_timestamp) VALUES (?,?,?,?,?,?)",
        (
            identity,
            state["sequence"],
            state.get("turn_group"),
            state.get("segment"),
            body,
            int(host_timestamp),
        ),
    )
    if holding:
        state["held_bytes"] = state.get("held_bytes", 0) + len(body.encode())
        if state["held_bytes"] > HELD_LIMIT_BYTES:
            drop_held(db, state, abandon=True)
            return
    if state.get("phase") == "unmarked":
        state["unmarked_bytes"] = state.get("unmarked_bytes", 0) + len(body.encode())
        if state["unmarked_bytes"] > UNMARKED_LIMIT_BYTES:
            abandon_unmarked(db, state)
            return
    if item["kind"] == "user":
        # A title is read as words: an image's name is not one.
        state["title"] = IMAGE_REFERENCE.sub("[image]", text)[:200]
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
