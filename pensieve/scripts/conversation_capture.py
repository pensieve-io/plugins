"""Opt-in visible-conversation capture. Python 3.9+, standard library only.

The host owns its transcript. This helper reads only that supplied file, uses
non-secret authenticated context markers, and uploads with a separately issued
upload-only key from a private local config. It never reads MCP/OAuth credentials.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from context_receipt import accepted_contexts, conversation_id

UPLOAD_ENDPOINT = "https://mcp.pensieve.uk/hooks/conversations"
CONFIG_PATH = Path.home() / ".config/pensieve/capture.json"
STATE_PATH = Path.home() / ".local/state/pensieve/capture"
MAX_INPUT_BYTES = 65536
MAX_CONFIG_BYTES = 65536
MAX_RECORD_BYTES = 1024 * 1024
MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_EVENT_CHARS = 32000
MAX_BATCH_BYTES = 262144
MAX_BATCH_EVENTS = 100
MAX_STATE_PAGES = 4096  # 16 MiB with SQLite's 4096-byte pages; never evict an unacked event.
CONTEXT_MARKER = re.compile(r"<!-- pensieve-capture-context (\{[^\r\n]*?\}) -->")
INTERNAL_MARKER = re.compile(r"<!-- pensieve-(?:capture-context|delivery)\b.*?-->", re.DOTALL)
SECRET = re.compile(
    r"(?i)(\b(?:Bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,})|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|upload[_-]?key)"
    r"[\"']?\s*[=:]\s*[\"']?[^\s\"',}]+)"
)
HOST_EVENTS = {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}
SET_CONTEXT_NAMES = {
    "mcp__pensieve__set_context",
    "mcp__plugin_pensieve_pensieve__set_context",
    "mcp__plugin:pensieve:pensieve__set_context",
}


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def private_file(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise ValueError("capture config/state must be an owned regular file with mode 0600")
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError("capture config exceeds its size limit")
        return data


def profiles(path: Path) -> dict[str, str]:
    try:
        value = json.loads(private_file(path, MAX_CONFIG_BYTES))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or set(value) != {"version", "profiles"}:
        raise ValueError("capture config must contain version and profiles")
    if value["version"] != 2 or not isinstance(value["profiles"], list):
        raise ValueError("unsupported capture config")
    result = {}
    for profile in value["profiles"]:
        if not isinstance(profile, dict) or set(profile) != {"user_id", "upload_key"}:
            raise ValueError("invalid capture profile")
        owner = conversation_id(profile["user_id"])
        key = profile["upload_key"]
        if (
            owner is None
            or not isinstance(key, str)
            or not 16 <= len(key) <= 4096
            or any(character.isspace() for character in key)
        ):
            raise ValueError("invalid capture profile")
        if owner in result:
            raise ValueError("duplicate capture profile")
        result[owner] = key
    return result


def parse_marker(text: str, client: str, session: str, kind: str) -> dict | None:
    matches = CONTEXT_MARKER.findall(text)
    if len(matches) != 1:
        return None
    try:
        marker = json.loads(matches[0])
    except ValueError:
        return None
    if not isinstance(marker, dict) or set(marker) != {
        "v",
        "capture_generation",
        "kind",
        "user_id",
        "client",
        "conversation_id",
        "context_id",
        "turn_id",
    }:
        return None
    context, turn = marker["context_id"], marker["turn_id"]
    if (
        marker["v"] != 2
        or (
            marker["capture_generation"] is not None
            and conversation_id(marker["capture_generation"]) is None
        )
        or marker["kind"] != kind
        or marker["client"] != client
        or conversation_id(marker["conversation_id"]) != session
        or conversation_id(marker["user_id"]) is None
        or (context is not None and (type(context) is not int or context <= 0))
        or (turn is not None and (not isinstance(turn, str) or not 0 < len(turn) <= 255))
    ):
        return None
    return marker


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


def visible_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"text", "input_text", "output_text"} and isinstance(
            item.get("text"), str
        ):
            parts.append(item["text"])
        elif item.get("type") in {"image", "input_image", "image_url", "document", "audio"}:
            parts.append("[Attachment omitted; original remains in the host conversation]")
    return "\n".join(parts)


def aware_time(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
    return None


def is_code_mode_tool(name: object, namespace: object) -> bool:
    return isinstance(name, str) and name in {"exec", "wait"} and namespace in {None, "functions"}


def is_set_context(name: object, namespace: object = None) -> bool:
    return isinstance(name, str) and (
        name in SET_CONTEXT_NAMES or (name == "set_context" and namespace == "mcp__pensieve")
    )


def is_hook_tool(name: object, namespace: object = None) -> bool:
    return isinstance(name, str) and (
        name in {name.replace("set_context", "context_briefing") for name in SET_CONTEXT_NAMES}
        or (name == "context_briefing" and namespace == "mcp__pensieve")
    )


def normalise(record: dict, client: str, session: str, state: dict) -> list[dict]:
    """Return only allowed visible events and provenance-checked boundaries.

    Source IDs are created by the caller from committed file position, so two
    identical visible messages remain distinct. We never recurse through JSON
    looking for text; reasoning and embedded instruction records stay local.
    """
    result = []
    if client == "claude" and (
        record.get("sessionId") != session or record.get("isSidechain") is not False
    ):
        return result
    if client == "codex" and record.get("type") == "turn_context":
        payload = record.get("payload", {})
        if isinstance(payload, dict) and isinstance(payload.get("turn_id"), str):
            state["turn_id"] = payload["turn_id"]
        return result
    # Context receipts require accepted hook provenance, never text quoted by
    # a user, assistant, tool, hook error, or nested compaction history.
    for text in accepted_contexts(record, session, session, client):
        marker = parse_marker(text, client, session, "prompt")
        if marker:
            result.append({"boundary": marker})
    if result:
        return result
    payload = record.get("payload") if client == "codex" else record.get("message")
    if client == "codex":
        if not isinstance(payload, dict):
            return []
        if record.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message")
            if isinstance(text, str):
                return [{"kind": "user", "content": text, "new_turn": True}]
        if record.get("type") == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item")
            if (
                payload.get("thread_id") == session
                and payload.get("turn_id") == state.get("turn_id")
                and isinstance(item, dict)
                and item.get("type") == "McpToolCall"
                and item.get("server") == "pensieve"
                and item.get("tool") == "set_context"
                and item.get("status") == "completed"
                and not state.get("calls", {}).get(item.get("id"), {}).get("selection")
            ):
                # Nested code-mode calls have native MCP provenance even though
                # the model-visible call is only exec/wait. Never infer a
                # selection from JavaScript source or its combined output.
                output = item.get("result")
                if not isinstance(output, dict) or output.get("isError"):
                    return []
                text = visible_text(output.get("content"))
                marker = parse_marker(text, client, session, "selection")
                if marker:
                    return [{"boundary": marker}, {"kind": "tool_result", "content": text}]
                return [{"unknown_boundary": True}]
            if (
                payload.get("thread_id") == session
                and isinstance(item, dict)
                and item.get("type") == "UserMessage"
                and isinstance(payload.get("turn_id"), str)
            ):
                state["turn_id"] = payload["turn_id"]
                text = visible_text(item.get("content"))
                return [{"kind": "user", "content": text, "new_turn": True}] if text else []
        if record.get("type") != "response_item":
            return []
        kind = payload.get("type")
        if kind == "message" and payload.get("role") == "assistant":
            if payload.get("channel") not in {None, "commentary", "final"} or payload.get(
                "phase"
            ) not in {None, "commentary", "final_answer", "final"}:
                return []
            text = visible_text(payload.get("content"))
            return [{"kind": "assistant", "content": text}] if text else []
        if kind in {"function_call", "custom_tool_call"}:
            call = payload.get("call_id")
            internal = is_hook_tool(payload.get("name"), payload.get("namespace"))
            if isinstance(call, str):
                state.setdefault("calls", {})[call] = {
                    "selection": is_set_context(payload.get("name"), payload.get("namespace")),
                    "internal": internal,
                    "aggregate": is_code_mode_tool(payload.get("name"), payload.get("namespace")),
                }
            if internal:
                return []
            # Tool arguments can contain shell/environment secrets. The first
            # version retains the tool's name and results, not arbitrary input.
            name = payload.get("name")
            return (
                [{"kind": "tool_call", "content": str(name)[:255]}] if isinstance(name, str) else []
            )
        if kind in {"function_call_output", "custom_tool_call_output"}:
            output = payload.get("output")
            text = visible_text(output)
            call = state.setdefault("calls", {}).pop(payload.get("call_id"), {})
            if call.get("internal"):
                return []
            if call.get("aggregate"):
                # One exec/wait result can combine calls made before and after
                # a selection, including concurrent or yielded work. Its text
                # has no single proven company; retain only an omission notice.
                return [
                    {
                        "kind": "tool_result",
                        "content": "[Combined code-mode tool output omitted; original remains in the host conversation]",
                    }
                ]
            if call.get("selection"):
                marker = parse_marker(text, client, session, "selection")
                if marker:
                    result.append({"boundary": marker})
                else:
                    # A selection result with no valid receipt could change
                    # account/context; stop attributing later text to the old one.
                    result.append({"unknown_boundary": True})
            if text:
                result.append({"kind": "tool_result", "content": text})
            return result
        return []
    if (
        record.get("isMeta") is True
        or record.get("isCompactSummary") is True
        or record.get("isVisibleInTranscriptOnly") is True
        or not isinstance(payload, dict)
    ):
        return []
    kind = record.get("type")
    content = payload.get("content")
    if kind == "assistant" and payload.get("role") == "assistant":
        text = visible_text(content)
        if text:
            result.append({"kind": "assistant", "content": text})
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                call, name = item.get("id"), item.get("name")
                if isinstance(call, str) and isinstance(name, str):
                    internal = is_hook_tool(name)
                    state.setdefault("calls", {})[call] = {
                        "selection": is_set_context(name),
                        "internal": internal,
                    }
                    if not internal:
                        result.append({"kind": "tool_call", "content": name[:255]})
        return result
    if kind != "user" or payload.get("role") != "user":
        return []
    blocks = content if isinstance(content, list) else []
    tool_results = [
        item for item in blocks if isinstance(item, dict) and item.get("type") == "tool_result"
    ]
    if not tool_results:
        text = visible_text(content)
        if (
            "permissionMode" not in record
            and "promptSource" not in record
            and text.startswith(
                ("<command-name>", "<local-command-stdout>", "<local-command-stderr>")
            )
        ):
            # Claude's local /compact and /clear records are synthetic user
            # messages without prompt provenance, not fresh model turns.
            return []
        return [{"kind": "user", "content": text, "new_turn": True}] if text else []
    for item in tool_results:
        text = visible_text(item.get("content"))
        call = state.setdefault("calls", {}).pop(item.get("tool_use_id"), {})
        if call.get("internal"):
            continue
        if call.get("selection"):
            marker = (
                None if item.get("is_error") else parse_marker(text, client, session, "selection")
            )
            if marker:
                result.append({"boundary": marker})
            elif not item.get("is_error"):
                result.append({"unknown_boundary": True})
        if text:
            result.append({"kind": "tool_result", "content": text})
    return result


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("capture state directory must be owned, private (0700), and not a symlink")


def connect_state(root: Path, client: str, session: str) -> sqlite3.Connection:
    private_directory(root)
    path = root / f"{client}-{session}.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise ValueError("capture state file must be owned and private (0600)")
    db = sqlite3.connect(path, timeout=0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA page_size=4096")
    db.execute(f"PRAGMA max_page_count={MAX_STATE_PAGES}")
    db.execute("PRAGMA secure_delete=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS segments (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, context INTEGER NOT NULL,
            generation TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', retired INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT);
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, turn_group TEXT,
            segment TEXT, body TEXT NOT NULL, host_timestamp INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS pending_events ON events(segment, sequence);
        CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY, segment TEXT UNIQUE NOT NULL, body BLOB NOT NULL,
            sha TEXT NOT NULL, event_ids TEXT NOT NULL);
    """)
    return db


def load_state(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
    return json.loads(row[0]) if row else {"offset": None, "sequence": 0, "calls": {}}


def save_state(db: sqlite3.Connection, state: dict) -> None:
    db.execute("INSERT OR REPLACE INTO state(id,body) VALUES (1,?)", (encoded(state).decode(),))


def renewal_segment(segment: str, user_event: str) -> str:
    return str(uuid.uuid5(uuid.UUID(segment), "renewal:" + user_event))


def apply_item(db, state, item, identity, occurred_at, client, session, configured, host_timestamp):
    if item.get("new_turn"):
        # A later turn can never authorise an earlier unmarked one.
        db.execute("DELETE FROM events WHERE segment IS NULL")
        state.update(
            candidate_segment=state.get("segment"),
            candidate_scope=state.get("scope"),
            segment=None,
            scope=None,
            turn_group=identity,
            awaiting_marker=True,
            ambiguous=False,
            discard_until_prompt=False,
            turn_occurred_at=occurred_at if host_timestamp else None,
        )
    if item.get("unknown_boundary"):
        db.execute("DELETE FROM events WHERE segment IS NULL")
        state.update(segment=None, scope=None, ambiguous=True, awaiting_marker=False)
        return
    marker = item.get("boundary")
    if marker:
        if marker["kind"] == "prompt":
            if not state.get("awaiting_marker") or state.get("ambiguous"):
                # Never apply a late/unpaired hook marker to another user turn.
                return
            if marker["turn_id"] is not None and marker["turn_id"] != state.get("turn_id"):
                state.update(segment=None, scope=None, ambiguous=True)
                return
            state["awaiting_marker"] = False
        elif state.get("ambiguous") or state.get("awaiting_marker"):
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
            and previous[0] == owner
            and previous[2] != generation
        ):
            # A changed opt-in period cannot authorise the rest of an old turn.
            # Wait for the next user prompt and its matching receipt.
            state.update(segment=None, scope=None, discard_until_prompt=True)
            db.execute("DELETE FROM events WHERE segment IS NULL")
            return
        if marker["kind"] == "selection" and previous != [owner, context, generation]:
            # The preceding prompt belongs to the previous company/account.
            # A destination reached by a tool has no destination user title yet.
            state.update(title="", title_segment=None)
        state["scope"] = [owner, context, generation]
        if context is None or generation is None or owner not in configured:
            state["segment"] = None
            state.update(title="", title_segment=None)
            # An explicit disabled scope is not an upload backlog. Only its
            # current provisional user row is removed, never acknowledged work.
            if marker["kind"] == "prompt":
                db.execute(
                    "DELETE FROM events WHERE segment IS NULL AND turn_group=?",
                    (state.get("turn_group"),),
                )
            return
        prior = db.execute(
            "SELECT expires_at FROM segments WHERE id=?", (previous_segment,)
        ).fetchone()
        expiry = aware_time(prior[0]) if prior else None
        turn_time = aware_time(state.get("turn_occurred_at"))
        if (
            marker["kind"] == "prompt"
            and previous_segment
            and expiry
            and turn_time
            and turn_time >= expiry
        ):
            segment = renewal_segment(previous_segment, state["turn_group"])
        elif previous == [owner, context, generation] and previous_segment:
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
            state.update(segment=None, scope=None, awaiting_marker=False, ambiguous=True, title="")
            db.execute(
                "DELETE FROM events WHERE segment IS NULL AND turn_group=?",
                (state.get("turn_group"),),
            )
            return
        state["segment"] = segment
        state["title_segment"] = segment
        db.execute(
            "INSERT OR IGNORE INTO segments(id,owner,context,generation,title) VALUES (?,?,?,?,?)",
            (segment, owner, context, generation, state.get("title", "")),
        )
        if marker["kind"] == "prompt":
            db.execute(
                "UPDATE events SET segment=? WHERE segment IS NULL AND turn_group=?",
                (segment, state.get("turn_group")),
            )
        return
    if "kind" not in item:
        return
    if state.get("discard_until_prompt"):
        return
    if state.get("segment") is None and not state.get("awaiting_marker"):
        # A baseline may begin mid-turn. Only an observed user prompt can
        # wait provisionally for attribution; orphan outputs are never queued.
        db.execute("DELETE FROM events WHERE segment IS NULL")
        return
    if item["kind"] != "user" and state.get("awaiting_marker"):
        state["ambiguous"] = True
    if state.get("ambiguous"):
        # Its own prompt receipt can no longer repair this turn. Keep no
        # unuploadable text that could fill the spool and block future work.
        db.execute("DELETE FROM events WHERE segment IS NULL")
        return
    # Only the current user prompt may wait provisionally for its own receipt.
    # Known disabled scopes never retain transcript text.
    if state.get("scope") is not None and state.get("segment") is None:
        return
    text, truncated = clean_content(item["content"], list(configured.values()))
    if not text:
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


def scan(
    db,
    state,
    path: Path,
    client: str,
    session: str,
    configured: dict,
    deadline: float,
    *,
    allow_new_file: bool = False,
) -> None:
    if not path.is_absolute():
        raise ValueError("host transcript path must be absolute")
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except FileNotFoundError:
        if allow_new_file and state["offset"] is None:
            # Claude creates a new transcript after SessionStart. Its absence
            # proves there is no older content to import at this path. Remember
            # that empty baseline so the first Stop captures the first turn.
            state.update(offset=0, path=str(path), awaiting_source_creation=True)
            return
        raise
    with os.fdopen(fd, "rb") as handle:
        details = os.fstat(handle.fileno())
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("host transcript must be a regular file")
        header = handle.readline(MAX_RECORD_BYTES)
        if client == "codex":
            try:
                metadata = json.loads(header)
            except ValueError:
                raise ValueError("Codex transcript has no valid session header") from None
            if (
                metadata.get("type") != "session_meta"
                or conversation_id(metadata.get("payload", {}).get("id")) != session
            ):
                raise ValueError("Codex transcript belongs to a different conversation")
        file_id = [details.st_dev, details.st_ino]
        if state.get("awaiting_source_creation") and state.get("path") == str(path):
            state.pop("awaiting_source_creation")
            state["file_id"] = file_id
        if state["offset"] is None:
            # First invocation establishes a baseline; never import older work.
            # SessionStart installs this before the first user prompt on both hosts.
            handle.seek(max(0, details.st_size - MAX_RECORD_BYTES))
            tail = handle.read(MAX_RECORD_BYTES)
            final_newline = tail.rfind(b"\n")
            baseline = max(0, details.st_size - MAX_RECORD_BYTES) + final_newline + 1
            state.update(offset=baseline, file_id=file_id, path=str(path), segment=None)
            return
        if (
            state.get("file_id") != file_id
            or state.get("path") != str(path)
            or details.st_size < state["offset"]
        ):
            raise ValueError("host transcript replaced or truncated; pending capture retained")
        handle.seek(state["offset"])
        initial_offset = state["offset"]
        scanned = 0
        while scanned < MAX_SCAN_BYTES and time.monotonic() < deadline:
            offset = handle.tell()
            line = handle.readline(min(MAX_RECORD_BYTES + 1, MAX_SCAN_BYTES - scanned))
            if not line:
                break
            scanned += len(line)
            if state.get("discarding_record") or len(line) > MAX_RECORD_BYTES:
                if not state.get("discarding_record"):
                    # The skipped record could contain a prompt or selection.
                    # Keep accepted/pending work, but trust no later attribution
                    # until a fresh user prompt receives its own marker.
                    db.execute("DELETE FROM events WHERE segment IS NULL")
                    state.update(
                        segment=None,
                        scope=None,
                        candidate_segment=None,
                        candidate_scope=None,
                        awaiting_marker=False,
                        ambiguous=True,
                        title="",
                        title_segment=None,
                        calls={},
                        turn_id=None,
                        turn_group=None,
                        turn_occurred_at=None,
                        discard_until_prompt=True,
                    )
                # Never decode the skipped bytes. Checkpoint partial progress so
                # even a record larger than one scan cannot wedge future turns.
                state["discarding_record"] = not line.endswith(b"\n")
                state["offset"] = handle.tell()
                continue
            if not line.endswith(b"\n"):
                # Retry a bounded partial record when the host finishes it or
                # the next hook has a full scan budget available.
                break
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                raise ValueError("invalid complete transcript record; capture paused") from None
            if isinstance(record, dict):
                previous_state = copy.deepcopy(state)
                items = normalise(record, client, session, state)
                if items:
                    # Reserve room for the exact-byte HTTP outbox even when
                    # pending content has filled its budget. Otherwise creating
                    # the batch needed to drain the spool could itself fail.
                    used_pages = (
                        db.execute("PRAGMA page_count").fetchone()[0]
                        - db.execute("PRAGMA freelist_count").fetchone()[0]
                    )
                    limit_pages = db.execute("PRAGMA max_page_count").fetchone()[0]
                    reserve = min(MAX_BATCH_BYTES * 2, limit_pages * 4096 // 3)
                    estimate = sum(len(encoded(item)) + 4096 for item in items)
                    if used_pages * 4096 + estimate > limit_pages * 4096 - reserve:
                        state.clear()
                        state.update(previous_state)
                        if state["offset"] > initial_offset:
                            # Commit the prefix that fits, then drain it. A
                            # backlog larger than the spool must make progress
                            # across checkpoints instead of rolling back forever.
                            break
                        raise sqlite3.OperationalError(
                            "capture spool full; outbox reserve retained"
                        )
                for index, item in enumerate(items):
                    identity = f"{client}:{session}:{offset}:{index}"
                    original_time = aware_time(record.get("timestamp"))
                    apply_item(
                        db,
                        state,
                        item,
                        identity,
                        (original_time or datetime.now(timezone.utc)).isoformat(),
                        client,
                        session,
                        configured,
                        original_time is not None,
                    )
            state["offset"] = handle.tell()
            # The caller commits this scan and cursor atomically while holding
            # its write lock. A full spool rolls back the entire scan, so the
            # next invocation rereads those records without losing them.


def next_batch(db, segment, client, session):
    existing = db.execute("SELECT * FROM batches WHERE segment=?", (segment["id"],)).fetchone()
    if existing:
        return dict(existing)
    rows = db.execute(
        "SELECT id,body FROM events WHERE segment=? ORDER BY sequence LIMIT ?",
        (segment["id"], MAX_BATCH_EVENTS),
    ).fetchall()
    if not rows:
        return None
    body = {
        "batch_id": str(uuid.uuid4()),
        "client": client,
        "host_conversation_id": session,
        "segment_id": segment["id"],
        "context_id": segment["context"],
        "events": [],
        "capture_generation": segment["generation"],
        "title": segment["title"],
    }
    selected = []
    for row in rows:
        event = json.loads(row["body"])
        body["events"].append(event)
        if len(encoded(body)) > MAX_BATCH_BYTES:
            body["events"].pop()
            break
        selected.append(row["id"])
    raw = encoded(body)
    checksum = hashlib.sha256(raw).hexdigest()
    db.execute(
        "INSERT INTO batches(id,segment,body,sha,event_ids) VALUES (?,?,?,?,?)",
        (
            body["batch_id"],
            segment["id"],
            raw,
            checksum,
            json.dumps(selected),
        ),
    )
    db.commit()
    return {
        "id": body["batch_id"],
        "segment": segment["id"],
        "body": raw,
        "sha": checksum,
        "event_ids": json.dumps(selected),
    }


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def checked_endpoint(endpoint: str) -> str:
    if endpoint == UPLOAD_ENDPOINT:
        return endpoint
    parsed = urlsplit(endpoint)
    try:
        loopback = (
            parsed.hostname == "localhost"
            or ipaddress.ip_address(parsed.hostname or "").is_loopback
        )
    except ValueError:
        loopback = False
    if (
        not loopback
        or parsed.scheme != "http"
        or parsed.path != "/hooks/conversations"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("only the fixed upload endpoint or an HTTP loopback fixture is supported")
    if parsed.port is not None and parsed.port < 1:
        raise ValueError("invalid loopback port")
    return endpoint


def upload(batch: dict, key: str, endpoint: str, timeout: float) -> dict | bool | str:
    request = Request(
        checked_endpoint(endpoint),
        data=batch["body"],
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            result = json.loads(response.read(4097))
    except HTTPError as exc:
        if exc.code == 410:
            try:
                result = json.loads(exc.read(4097))
            except (OSError, ValueError):
                return False
            if (
                isinstance(result, dict)
                and result.get("segment_id") == batch["segment"]
                and result.get("reason") == "capture_disabled"
            ):
                return {"status": "capture_disabled"}
            if (
                isinstance(result, dict)
                and result.get("segment_id") == batch["segment"]
                and result.get("reason") == "expired"
                and aware_time(result.get("expires_at")) is not None
            ):
                return {"status": result["reason"], "expires_at": result["expires_at"]}
            return False
        if exc.code in {401, 403}:
            return "forbidden"
        return False
    except (OSError, URLError, ValueError):
        return False
    accepted = (
        isinstance(result, dict)
        and result.get("batch_id") == batch["id"]
        and result.get("batch_sha256") == batch["sha"]
        and result.get("segment_id") == batch["segment"]
        and result.get("accepted_events") == len(json.loads(batch["event_ids"]))
        and conversation_id(result.get("conversation_id")) is not None
        and aware_time(result.get("expires_at")) is not None
    )
    return {"status": "accepted", "expires_at": result["expires_at"]} if accepted else False


def retire_segment(db, segment_id, expires_at):
    """Erase old content; an expiry can preserve a proven fresh user-turn suffix."""
    db.execute("BEGIN IMMEDIATE")
    state = load_state(db)
    expiry = aware_time(expires_at)
    if expiry:
        rows = db.execute(
            "SELECT id,sequence,body,host_timestamp FROM events WHERE segment=? ORDER BY sequence",
            (segment_id,),
        ).fetchall()
        fresh = next(
            (
                row
                for row in rows
                if row["host_timestamp"]
                and (event := json.loads(row["body"]))["kind"] == "user"
                and (occurred := aware_time(event["occurred_at"])) is not None
                and occurred >= expiry
            ),
            None,
        )
        if fresh:
            old = db.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
            replacement = renewal_segment(segment_id, fresh["id"])
            title = json.loads(fresh["body"])["content"][:200]
            db.execute(
                "INSERT OR IGNORE INTO segments(id,owner,context,generation,title) VALUES (?,?,?,?,?)",
                (
                    replacement,
                    old["owner"],
                    old["context"],
                    old["generation"],
                    title,
                ),
            )
            db.execute(
                "UPDATE events SET segment=? WHERE segment=? AND sequence>=?",
                (replacement, segment_id, fresh["sequence"]),
            )
            for name in ("segment", "candidate_segment", "title_segment"):
                if state.get(name) == segment_id:
                    state[name] = replacement
            if state.get("title_segment") == replacement:
                state["title"] = title
        # A prompt checkpoint may precede its concurrent hook receipt. Preserve
        # that fresh provisional turn so its own receipt can still authorize it.
        turn_time = aware_time(state.get("turn_occurred_at"))
        if (
            state.get("candidate_segment") == segment_id
            and state.get("awaiting_marker")
            and not state.get("ambiguous")
            and turn_time
            and turn_time >= expiry
        ):
            state.update(candidate_segment=None, candidate_scope=None)
    db.execute("DELETE FROM events WHERE segment=?", (segment_id,))
    db.execute("DELETE FROM batches WHERE segment=?", (segment_id,))
    db.execute("UPDATE segments SET retired=1,title='' WHERE id=?", (segment_id,))
    if state.get("title_segment") == segment_id:
        state.update(title="", title_segment=None)
    if state.get("segment") == segment_id or state.get("candidate_segment") == segment_id:
        state.update(
            segment=None,
            scope=None,
            candidate_segment=None,
            candidate_scope=None,
            awaiting_marker=False,
            ambiguous=True,
            title="",
            title_segment=None,
            discard_until_prompt=True,
        )
    save_state(db, state)
    db.commit()


def flush(db, configured, client, session, endpoint, deadline):
    denied = set()
    while time.monotonic() < deadline:
        progressed = False
        segments = db.execute(
            "SELECT * FROM segments WHERE retired=0 AND (id IN (SELECT segment FROM events) OR id IN (SELECT segment FROM batches)) ORDER BY rowid"
        ).fetchall()
        for segment in segments:
            scope = (segment["owner"], segment["context"])
            key = configured.get(segment["owner"])
            remaining = deadline - time.monotonic()
            if key is None or scope in denied or remaining < 0.05:
                continue
            batch = next_batch(db, segment, client, session)
            if batch is None:
                continue
            outcome = upload(batch, key, endpoint, min(0.65, remaining))
            if isinstance(outcome, dict) and outcome["status"] == "capture_disabled":
                retire_segment(db, segment["id"], None)
                progressed = True
                continue
            if isinstance(outcome, dict) and outcome["status"] == "expired":
                retire_segment(
                    db,
                    segment["id"],
                    outcome["expires_at"],
                )
                progressed = True
                continue
            if outcome == "forbidden":
                denied.add(scope)
                continue
            if not isinstance(outcome, dict) or outcome["status"] != "accepted":
                return
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE segments SET expires_at=? WHERE id=?",
                (outcome["expires_at"], segment["id"]),
            )
            for identity in json.loads(batch["event_ids"]):
                db.execute("DELETE FROM events WHERE id=?", (identity,))
            db.execute("DELETE FROM batches WHERE id=?", (batch["id"],))
            db.commit()
            progressed = True
        if not progressed:
            break


def run_hook(
    payload: dict,
    client: str,
    config: Path = CONFIG_PATH,
    state_root: Path = STATE_PATH,
    endpoint: str = UPLOAD_ENDPOINT,
) -> dict:
    if client not in {"codex", "claude"} or payload.get("hook_event_name") not in HOST_EVENTS:
        return {}
    session = conversation_id(payload.get("session_id"))
    if session is None:
        return {}
    configured = profiles(config)
    if not configured:
        # Capture-off must be remembered without reading transcript text, or a
        # later re-enable would scan and upload the disabled interval. Never
        # create state for a session that has never enabled capture.
        existing = state_root / f"{client}-{session}.sqlite3"
        if existing.exists():
            db = connect_state(state_root, client, session)
            try:
                db.execute("BEGIN IMMEDIATE")
                state = load_state(db)
                state["capture_paused"] = True
                save_state(db, state)
                db.commit()
            finally:
                db.close()
        return {}
    event = payload["hook_event_name"]
    # Plugin SessionEnd runs inside Claude's default 1.5s total budget and
    # Codex's 3s cap. Durability comes from earlier checkpoints, not this flush.
    deadline = time.monotonic() + (0.9 if event == "SessionEnd" else 2.5)
    db = connect_state(state_root, client, session)
    source_error = None
    try:
        db.execute("BEGIN IMMEDIATE")
        state = load_state(db)
        disabled = any(
            scope is not None and scope[0] not in configured
            for scope in (state.get("scope"), state.get("candidate_scope"))
        )
        if disabled:
            # Consent is per account; another enabled account grants no upload rights.
            state.update(
                segment=None,
                candidate_segment=None,
                candidate_scope=None,
                awaiting_marker=False,
                ambiguous=True,
                title="",
                title_segment=None,
            )
        fingerprints = {
            owner: hashlib.sha256(key.encode()).hexdigest() for owner, key in configured.items()
        }
        previous_fingerprints = state.get("profile_keys")
        if previous_fingerprints is not None and previous_fingerprints != fingerprints:
            state["capture_paused"] = True
        state["profile_keys"] = fingerprints
        if state.pop("capture_paused", False):
            state.update(
                offset=None,
                discarding_record=False,
                segment=None,
                scope=None,
                candidate_segment=None,
                candidate_scope=None,
                awaiting_marker=False,
                ambiguous=True,
                title="",
                title_segment=None,
                calls={},
                discard_until_prompt=True,
            )
        path = payload.get("transcript_path")
        if isinstance(path, str):
            try:
                scan(
                    db,
                    state,
                    Path(path),
                    client,
                    session,
                    configured,
                    deadline,
                    allow_new_file=event == "SessionStart",
                )
            except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                # Loss of the source must not strand already-durable uploads.
                # Roll back this scan and flush only the previously committed
                # events, with their original ownership and exact batch bytes.
                db.rollback()
                db.execute("BEGIN IMMEDIATE")
                state = load_state(db)
                source_error = exc
        save_state(db, state)
        db.commit()
        flush(db, configured, client, session, endpoint, deadline)
    finally:
        db.close()
    if source_error is not None:
        raise source_error
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("claude", "codex"), required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("PENSIEVE_CAPTURE_CONFIG", str(CONFIG_PATH))),
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(os.environ.get("PENSIEVE_CAPTURE_STATE", str(STATE_PATH))),
    )
    parser.add_argument("--endpoint", type=checked_endpoint, default=UPLOAD_ENDPOINT)
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        payload = json.loads(raw) if len(raw) <= MAX_INPUT_BYTES else None
        if isinstance(payload, dict):
            run_hook(payload, args.client, args.config, args.state, args.endpoint)
    except sqlite3.OperationalError:
        # Concurrent hooks never wait on a lock; the next lifecycle hook retries.
        print(
            "Pensieve capture deferred: local spool busy or full; pending events retained.",
            file=sys.stderr,
        )
    except (OSError, ValueError, sqlite3.DatabaseError):
        print(
            "Pensieve capture paused: check private config and transcript access; pending events retained.",
            file=sys.stderr,
        )
    print("{}")


if __name__ == "__main__":
    main()
