"""Opt-in visible-conversation capture. Python 3.9+, standard library only.

The host owns its transcript. This helper reads only that supplied file, uses
non-secret authenticated context markers, and uploads with a separately issued
upload-only key from a private local config. It never reads MCP/OAuth credentials.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import ipaddress
import json
import os
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

from capture_adapters import aware_time, normalise
from capture_config import (
    credential_revocations,
    encoded,
    key_for,
    observe_credentials,
    private_directory,
    profiles,
)
from capture_protocol import HEADER, MAX_BATCH_BYTES, MAX_BATCH_EVENTS, VERSION, check
from capture_state import apply_item, block, migrate_state, scope_revoked
from context_receipt import conversation_id

UPLOAD_ENDPOINT = "https://mcp.pensieve.uk/hooks/conversations"
CONFIG_PATH = Path.home() / ".config/pensieve/capture.json"
STATE_PATH = Path.home() / ".local/state/pensieve/capture"
MAX_INPUT_BYTES = 65536
MAX_RECORD_BYTES = 1024 * 1024
MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_STATE_PAGES = 4096  # 16 MiB with SQLite's 4096-byte pages; never evict an unacked event.
HOST_EVENTS = {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}


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
        CREATE TABLE IF NOT EXISTS anchors (
            ordinal INTEGER PRIMARY KEY, event_id TEXT NOT NULL, segment TEXT);
        CREATE TABLE IF NOT EXISTS delivery (
            segment TEXT PRIMARY KEY, status TEXT NOT NULL,
            last_attempt_at TEXT NOT NULL, last_saved_at TEXT);
    """)
    return db


def load_state(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
    state = json.loads(row[0]) if row else {"offset": None, "sequence": 0, "calls": {}}
    if not isinstance(state, dict):
        raise ValueError("Capture state must be an object")
    migrate_state(db, state)
    return state


def save_state(db: sqlite3.Connection, state: dict) -> None:
    db.execute("INSERT OR REPLACE INTO state(id,body) VALUES (1,?)", (encoded(state).decode(),))


def fork_parent(db, metadata, session):
    """Resolve an exact Codex boundary from this device's captured metadata.

    Missing/deleted/legacy endpoints stay unknown. Never open the source
    transcript, search other sessions, or substitute the source's latest head.
    """
    source = conversation_id(metadata.get("forked_from_id"))
    end = metadata.get("forked_from_ordinal_exclusive")
    if not source or source == session or type(end) is not int or end <= 0:
        return None
    root = Path(db.execute("PRAGMA database_list").fetchone()["file"]).parent
    path = root / f"codex-{source}.sqlite3"
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            return None
        source_db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
        try:
            row = source_db.execute(
                "SELECT a.event_id,s.owner,s.context,s.generation "
                "FROM anchors a JOIN segments s ON s.id=a.segment "
                "WHERE a.ordinal=? AND s.retired=0",
                (end - 1,),
            ).fetchone()
        finally:
            source_db.close()
        if row:
            return {
                "reference": {"host_conversation_id": source, "event_id": row[0]},
                "scope": list(row[1:4]),
            }
    except (OSError, sqlite3.DatabaseError):
        pass
    return None


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
    # Anchors contain identities only and share the spool's size bounds.
    db.execute(
        "DELETE FROM anchors WHERE segment IS NULL AND event_id NOT IN (SELECT id FROM events)"
    )
    db.execute("DELETE FROM anchors WHERE segment IN (SELECT id FROM segments WHERE retired=1)")
    if not path.is_absolute():
        raise ValueError("host transcript path must be absolute")
    fingerprints = {
        owner: hashlib.sha256(key.encode()).hexdigest() for owner, key in configured.items()
    }
    previous = state.get("profile_keys")
    revocations = credential_revocations(
        Path(db.execute("PRAGMA database_list").fetchone()["file"]).parent,
        client,
        legacy_profiles=previous
        if state["offset"] is not None and "observed_revocations" not in state
        else False,
    )
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except FileNotFoundError:
        if allow_new_file and state["offset"] is None:
            # Claude can create a fork without SessionStart and only writes its
            # transcript after UserPromptSubmit. Absence at either boundary
            # proves there is no older content to import at this path. Remember
            # that empty baseline so the first Stop captures the first turn.
            state.update(
                offset=0,
                path=str(path),
                awaiting_source_creation=True,
                profile_keys=fingerprints,
                observed_revocations=revocations,
            )
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
        changed = [
            scope
            for scope, epoch in revocations.items()
            if state.get("observed_revocations", {}).get(scope) != epoch
        ]
        if changed and state["offset"] is not None:
            cutover = {
                "offset": details.st_size,
                "scopes": changed,
                # Removing an owner-wide fallback does not revoke an explicit
                # context credential that was unchanged throughout. Its own
                # removal epoch still wins if it was removed and restored.
                "preserved_scopes": [
                    key
                    for key, digest in (previous or {}).items()
                    if ":" in key and fingerprints.get(key) == digest
                ],
            }
            state.setdefault("revocation_cutovers", []).append(cutover)
            scope = state.get("scope") or state.get("candidate_scope")
            if scope and scope_revoked(cutover, scope[0], scope[1]):
                block(db, state, "credential_removed")
        state["observed_revocations"] = revocations
        if state["offset"] is not None and previous is not None and previous != fingerprints:
            # Only changed credentials start a new capture interval. Keep scanning
            # unread work for unaffected scopes, including across bounded scans.
            state.setdefault("profile_cutovers", []).append(
                {"offset": details.st_size, "profiles": previous}
            )
            scope = state.get("scope") or state.get("candidate_scope")
            if scope and key_for(previous, scope[0], scope[1]) != key_for(
                fingerprints, scope[0], scope[1]
            ):
                block(db, state, "credential_changed")
        state["profile_keys"] = fingerprints
        if state["offset"] is None:
            state.pop("profile_cutovers", None)
            state.pop("revocation_cutovers", None)
            # First invocation establishes a baseline; never import older work.
            # SessionStart installs this before the first user prompt on both hosts.
            handle.seek(max(0, details.st_size - MAX_RECORD_BYTES))
            tail = handle.read(MAX_RECORD_BYTES)
            final_newline = tail.rfind(b"\n")
            baseline = max(0, details.st_size - MAX_RECORD_BYTES) + final_newline + 1
            state.update(
                offset=baseline,
                file_id=file_id,
                path=str(path),
                segment=None,
                tail=None,
                capture_turn_id=None,
            )
            state["fork_parent"] = (
                fork_parent(db, metadata["payload"], session) if client == "codex" else None
            )
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
                    block(db, state, "oversized_record")
                    state.update(
                        adapter={}, turn_group=None, turn_occurred_at=None, capture_turn_id=None
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
                items = normalise(
                    record, client, session, state["adapter"], state.get("capture_turn_id")
                )
                discards_unmarked = (
                    state.get("phase") == "awaiting_attribution"
                    and any(item["kind"] not in {"attribution", "user"} for item in items)
                    and not any(item["kind"] == "attribution" for item in items)
                )
                if items and not discards_unmarked:
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
                        source_offset=offset,
                    )
                    ordinal = record.get("ordinal")
                    if client == "codex" and type(ordinal) is int and ordinal >= 0:
                        # Several visible blocks may share a native record;
                        # its last retained event is the exact fork endpoint.
                        row = db.execute(
                            "SELECT segment,body FROM events WHERE id=?", (identity,)
                        ).fetchone()
                        if row and "capture" in json.loads(row["body"]):
                            db.execute(
                                "INSERT OR REPLACE INTO anchors VALUES (?,?,?)",
                                (ordinal, identity, row["segment"]),
                            )
            state["offset"] = handle.tell()
            # The caller commits this scan and cursor atomically while holding
            # its write lock. A full spool rolls back the entire scan, so the
            # next invocation rereads those records without losing them.

        for cutovers in ("profile_cutovers", "revocation_cutovers"):
            state[cutovers] = [
                cutover
                for cutover in state.get(cutovers, [])
                if cutover["offset"] > state["offset"]
                or (
                    state.get("phase") == "awaiting_attribution"
                    and state.get("turn_source_offset", 0) < cutover["offset"]
                )
            ]


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
    endpoint = checked_endpoint(endpoint)
    deadline = time.monotonic() + timeout
    compatibility = check(endpoint, "upload", min(0.25, timeout / 2))
    if compatibility != "compatible":
        return {"status": compatibility}
    request = Request(
        endpoint,
        data=batch["body"],
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "Pensieve-Plugin-Capture/1.0",
            HEADER: str(VERSION),
        },
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(request, timeout=max(0.05, deadline - time.monotonic())) as response:
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
                and result.get("reason") in {"capture_disabled", "deleted"}
            ):
                return {"status": result["reason"]}
            return False
        if exc.code in {401, 403}:
            return "forbidden"
        if exc.code == 426:
            return {"status": "incompatible"}
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
        and "expires_at" in result
        and (result["expires_at"] is None or aware_time(result["expires_at"]) is not None)
    )
    return {"status": "accepted", "expires_at": result["expires_at"]} if accepted else False


def retire_segment(db, segment_id):
    """Erase a terminal segment without replaying any of its queued content."""
    db.execute("BEGIN IMMEDIATE")
    state = load_state(db)
    db.execute("DELETE FROM events WHERE segment=?", (segment_id,))
    db.execute("DELETE FROM anchors WHERE segment=?", (segment_id,))
    db.execute("DELETE FROM batches WHERE segment=?", (segment_id,))
    db.execute("UPDATE segments SET retired=1,title='' WHERE id=?", (segment_id,))
    if state.get("title_segment") == segment_id:
        state.update(title="", title_segment=None)
    if state.get("segment") == segment_id or state.get("candidate_segment") == segment_id:
        block(db, state, "segment_retired")
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
            key = key_for(configured, segment["owner"], segment["context"])
            remaining = deadline - time.monotonic()
            if key is None or scope in denied or remaining < 0.05:
                continue
            batch = next_batch(db, segment, client, session)
            if batch is None:
                continue
            outcome = upload(batch, key, endpoint, remaining)
            status = (
                outcome.get("status", "unavailable")
                if isinstance(outcome, dict)
                else ("forbidden" if outcome == "forbidden" else "unavailable")
            )
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                """INSERT INTO delivery VALUES (?,?,?,?) ON CONFLICT(segment) DO UPDATE SET
                   status=excluded.status,last_attempt_at=excluded.last_attempt_at,
                   last_saved_at=coalesce(excluded.last_saved_at,delivery.last_saved_at)""",
                (segment["id"], status, now, now if status == "accepted" else None),
            )
            db.commit()
            if isinstance(outcome, dict) and outcome["status"] in {"capture_disabled", "deleted"}:
                retire_segment(db, segment["id"])
                progressed = True
                continue
            if outcome == "forbidden":
                denied.add(scope)
                continue
            if not isinstance(outcome, dict) or outcome["status"] != "accepted":
                if status == "incompatible":
                    print(
                        "Pensieve capture waiting for a compatible service; queued work retained.",
                        file=sys.stderr,
                    )
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


def capture_session(
    client,
    session,
    path,
    configured,
    state_root,
    endpoint,
    deadline,
    *,
    allow_new_file=False,
    recover=False,
):
    """Resume one known spool, preserving its original ownership and identities."""
    db = connect_state(state_root, client, session)
    source_error = None
    try:
        db.execute("BEGIN IMMEDIATE")
        state = load_state(db)
        disabled = any(
            scope is not None and key_for(configured, scope[0], scope[1]) is None
            for scope in (state.get("scope"), state.get("candidate_scope"))
        )
        if disabled:
            # Observe removal even when this hook has no transcript path. A later
            # restoration of the same key must not capture the disabled interval.
            block(db, state, "credential_removed")
        paused = state.pop("capture_paused", False)
        if paused:
            block(db, state, "capture_paused")
            state.update(offset=None, discarding_record=False, adapter={}, capture_turn_id=None)
        if recover:
            path = state.get("path")
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
                    allow_new_file=allow_new_file,
                )
            except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                # Loss of the source must not strand already-durable uploads.
                # Roll back this scan and flush only the previously committed
                # events, with their original ownership and exact batch bytes.
                db.rollback()
                db.execute("BEGIN IMMEDIATE")
                state = load_state(db)
                # A failed file read cannot undo a consent boundary observed by
                # this hook. Otherwise restoring credentials could save work
                # from the interval in which capture was disabled.
                if disabled:
                    block(db, state, "credential_removed")
                if paused:
                    block(db, state, "capture_paused")
                    state.update(
                        offset=None, discarding_record=False, adapter={}, capture_turn_id=None
                    )
                    state.pop("capture_paused", None)
                source_error = exc
        save_state(db, state)
        db.commit()
        flush(db, configured, client, session, endpoint, deadline)
    finally:
        db.close()
    return source_error


def recover_sessions(client, current, configured, state_root, endpoint, deadline):
    """Retry at most eight existing same-client spools within the hook's budget."""
    cursor_path = state_root / f"recovery-{client}.json"
    try:
        fd = os.open(
            cursor_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        with os.fdopen(fd, "r+") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                raise ValueError("Recovery cursor must be a private owned file")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                value = json.loads(handle.read(4096))
                cursor = value.get("after", "") if isinstance(value, dict) else ""
            except ValueError:
                cursor = ""
            if not isinstance(cursor, str):
                cursor = ""
            candidates = []
            with os.scandir(state_root) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline - 0.05:
                        return
                    prefix = client + "-"
                    if not entry.name.startswith(prefix) or not entry.name.endswith(".sqlite3"):
                        continue
                    session = entry.name[len(prefix) : -8]
                    if session != current and conversation_id(session) is not None:
                        candidates.append(session)
            candidates.sort(key=lambda session: (session <= cursor, session))
            for session in candidates[:8]:
                if time.monotonic() >= deadline - 0.05:
                    break
                try:
                    capture_session(
                        client,
                        session,
                        None,
                        configured,
                        state_root,
                        endpoint,
                        deadline,
                        recover=True,
                    )
                except (OSError, ValueError, sqlite3.DatabaseError):
                    pass  # Busy or damaged spools must not strand other conversations.
                handle.seek(0)
                json.dump({"after": session}, handle)
                handle.truncate()
                handle.flush()
    except (OSError, ValueError, sqlite3.DatabaseError):
        pass  # Recovery is opportunistic; ordinary hooks retain the durable backlog.


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
    event = payload["hook_event_name"]
    deadline = time.monotonic() + (0.9 if event == "SessionEnd" else 2.5)
    from capture_pairing import poll

    try:
        poll(config, client, timeout=0.25)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    configured = profiles(config, client)
    observe_credentials(state_root, client, configured)
    if event != "SessionEnd":
        from capture_onboarding import offer_connection

        try:
            offer_connection(payload, client, session, config, configured)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Setup must never interrupt a conversation or an existing upload.
    if not configured:
        # The client-wide credential observation fences every spool, including
        # dormant sessions beyond this hook's bounded recovery page.
        return {}
    # Plugin SessionEnd runs inside Claude's default 1.5s total budget and
    # Codex's 3s cap. Durability comes from earlier checkpoints, not this flush.
    ordinary = event != "SessionEnd"
    source_error = capture_session(
        client,
        session,
        payload.get("transcript_path"),
        configured,
        state_root,
        endpoint,
        deadline - (0.6 if ordinary else 0),
        allow_new_file=event in {"SessionStart", "UserPromptSubmit"},
    )
    if ordinary:
        recover_sessions(client, session, configured, state_root, endpoint, deadline)
    if source_error is not None:
        raise source_error
    return {}


def saving_status(root: Path, client: str, session: str) -> dict:
    """Inspect a known private spool without creating it or reading event bodies."""
    path = root / f"{client}-{session}.sqlite3"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"status": "not_started", "segments": []}
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise ValueError("Capture state must be an owned private file")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
    db.row_factory = sqlite3.Row
    try:
        # A pre-upgrade spool has no delivery evidence yet; do not invent it.
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery'"
        ).fetchone():
            return {"status": "not_observed", "segments": []}
        rows = db.execute(
            """SELECT s.context,d.status,d.last_attempt_at,d.last_saved_at,
                      (SELECT count(*) FROM events e WHERE e.segment=s.id) AS queued_events
               FROM segments s LEFT JOIN delivery d ON d.segment=s.id
               WHERE s.retired=0 ORDER BY s.rowid LIMIT 101"""
        ).fetchall()
        return {"segments": [dict(row) for row in rows[:100]], "truncated": len(rows) > 100}
    finally:
        db.close()


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
    parser.add_argument(
        "--status",
        metavar="SESSION_ID",
        help="Read local saving status for one known conversation; no transcript text or network calls",
    )
    args = parser.parse_args()
    if args.status is not None:
        session = conversation_id(args.status)
        if session is None:
            parser.error("--status requires a native conversation UUID")
        print(json.dumps(saving_status(args.state, args.client, session)))
        return
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
