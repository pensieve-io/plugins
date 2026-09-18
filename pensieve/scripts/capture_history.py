"""Bounded, browser-authorised local history import. Python 3.9+, stdlib only.

The server grant supplies the destination, folder and original-time window.
Nothing infers a company from an old chat, and no user-supplied path is opened.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path

from capture_config import config_lock, encoded, load_config, valid_uuid
from capture_pairing import API_BASE, request
from conversation_capture import (
    MAX_BATCH_BYTES,
    MAX_BATCH_EVENTS,
    MAX_RECORD_BYTES,
    MAX_SCAN_BYTES,
    STATE_PATH,
    UPLOAD_ENDPOINT,
    aware_time,
    clean_content,
    connect_state,
    normalise,
    save_state,
    upload,
)

MAX_FILES = 10000
MAX_DIRECTORIES = 10000


def history_roots(client: str) -> list[Path]:
    # These are the host's standard stores, never a path supplied by the UI.
    if client == "codex":
        return [Path.home() / ".codex/sessions", Path.home() / ".codex/archived_sessions"]
    return [Path.home() / ".claude/projects"]


def validate_grant(value: object, client: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid history grant")
    result = {
        key: value.get(key)
        for key in (
            "id",
            "context_id",
            "client",
            "capture_generation",
            "publication_generation",
            "since",
            "until",
            "project_path",
        )
    }
    if (
        not valid_uuid(result["id"])
        or result["client"] != client
        or type(result["context_id"]) is not int
        or result["context_id"] <= 0
        or not valid_uuid(result["capture_generation"])
        or (
            result["publication_generation"] is not None
            and not valid_uuid(result["publication_generation"])
        )
        or aware_time(result["until"]) is None
        or (result["since"] is not None and aware_time(result["since"]) is None)
        or not isinstance(result["project_path"], str)
        or not Path(result["project_path"]).is_absolute()
        or ".." in Path(result["project_path"]).parts
        or len(result["project_path"]) > 4096
    ):
        raise ValueError("Invalid history grant")
    if result["since"] is not None and aware_time(result["since"]) > aware_time(result["until"]):
        raise ValueError("Invalid history window")
    return result


def within_project(cwd: object, project: str) -> bool:
    # Lexical comparison of native recorded paths. Do not resolve or read an
    # arbitrary project path, and do not interpret a home-directory shorthand.
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or ".." in Path(cwd).parts:
        return False
    try:
        Path(cwd).relative_to(project)
        return True
    except ValueError:
        return False


def open_source(root: Path, relative: str, *, directory: bool = False):
    """Open each component without following links, including during races."""
    parts = Path(relative).parts
    if Path(relative).is_absolute() or ".." in parts:
        raise ValueError("Invalid history source")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if not root.is_absolute():
        raise ValueError("History root must be absolute")
    current = os.open(root.anchor, flags | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
        for index, part in enumerate(parts):
            child = os.open(
                part,
                flags | (os.O_DIRECTORY if directory or index < len(parts) - 1 else 0),
                dir_fd=current,
            )
            os.close(current)
            current = child
        details = os.fstat(current)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(details.st_mode) or details.st_uid != os.getuid():
            raise ValueError("History source must be a locally owned regular file")
        result, current = current, None
        return result
    finally:
        if current is not None:
            os.close(current)


def discover(state: dict, roots: list[Path], deadline: float) -> None:
    """Checkpoint a bounded directory frontier, without reading chat bodies."""
    while state["directories"] and time.monotonic() < deadline and len(state["files"]) < 100:
        root_index, relative = state["directories"].pop(0)
        state["directory_count"] += 1
        if state["directory_count"] > MAX_DIRECTORIES:
            raise ValueError("history_limit")
        try:
            fd = open_source(roots[root_index], relative, directory=True)
        except FileNotFoundError:
            continue
        try:
            # One directory is atomic. A corrupt/huge store fails visibly rather
            # than dropping an unvisited suffix and claiming full completion.
            with os.scandir(fd) as entries:
                for count, entry in enumerate(entries):
                    if count >= MAX_FILES:
                        raise ValueError("history_limit")
                    child = str(Path(relative) / entry.name)
                    if entry.is_dir(follow_symlinks=False):
                        state["directories"].append([root_index, child])
                    elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                        state["files"].append([root_index, child])
                        state["file_count"] += 1
                    if (
                        state["file_count"] > MAX_FILES
                        or len(state["directories"]) > MAX_DIRECTORIES
                    ):
                        raise ValueError("history_limit")
        finally:
            os.close(fd)


def source_identity(record: dict, client: str):
    if client == "codex":
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
            return record["payload"].get("id"), record["payload"].get("cwd")
    elif record.get("isSidechain") is False:
        return record.get("sessionId"), record.get("cwd")
    return None, None


def scan_file(state: dict, grant: dict, profile: dict, roots: list[Path], deadline: float):
    current = state["current"]
    fd = open_source(roots[current["root"]], current["path"])
    events = []
    with os.fdopen(fd, "rb") as handle:
        details = os.fstat(handle.fileno())
        identity = [details.st_dev, details.st_ino]
        if "file_id" not in current:
            current.update(file_id=identity, size=details.st_size)
        if current["file_id"] != identity or details.st_size < current["size"]:
            raise ValueError("source_changed")
        handle.seek(current["offset"])
        scanned = 0
        while (
            handle.tell() < current["size"]
            and scanned < MAX_SCAN_BYTES
            and time.monotonic() < deadline
            and len(events) < MAX_BATCH_EVENTS
        ):
            offset = handle.tell()
            line = handle.readline(min(MAX_RECORD_BYTES + 1, current["size"] - offset))
            scanned += len(line)
            if not line.endswith(b"\n"):
                if len(line) > MAX_RECORD_BYTES:
                    raise ValueError("source_record_too_large")
                # A writer's unfinished suffix was never part of this snapshot.
                current["offset"] = current["size"]
                break
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                raise ValueError("invalid_source") from None
            if not isinstance(record, dict):
                current["offset"] = handle.tell()
                continue
            session, cwd = source_identity(record, grant["client"])
            if current.get("session") is None and valid_uuid(session):
                current.update(session=session, cwd=cwd)
            elif session is not None and session != current.get("session"):
                raise ValueError("source_changed")
            if grant["client"] == "codex" and record.get("type") == "turn_context":
                cwd = record.get("payload", {}).get("cwd")
            elif grant["client"] == "claude":
                cwd = record.get("cwd")
            if isinstance(cwd, str):
                current["cwd"] = cwd
            if not current.get("session"):
                current["offset"] = handle.tell()
                continue
            # Always process native call correlation, including records outside
            # the time window. Control markers cannot change a historical grant.
            before = json.loads(json.dumps(current["parser"]))
            items = normalise(record, grant["client"], current["session"], current["parser"])
            occurred = aware_time(record.get("timestamp"))
            selected = []
            if (
                occurred is not None
                and occurred <= aware_time(grant["until"])
                and (grant["since"] is None or occurred >= aware_time(grant["since"]))
                and within_project(current.get("cwd"), grant["project_path"])
            ):
                for index, item in enumerate(items):
                    if "kind" not in item:
                        continue
                    text, truncated = clean_content(item["content"], [profile["upload_key"]])
                    if not text:
                        continue
                    event = {
                        "event_id": f"{grant['client']}:{current['session']}:{offset}:{index}",
                        "sequence": current["sequence"] + len(selected) + 1,
                        "kind": item["kind"],
                        "content": text,
                        "occurred_at": occurred.isoformat(),
                        "truncated": truncated or item.get("truncated", False),
                    }
                    if item.get("mutation_receipt_id"):
                        event["mutation_receipt_id"] = item["mutation_receipt_id"]
                    selected.append(event)
            if events and (
                len(events) + len(selected) > MAX_BATCH_EVENTS
                or len(encoded(events + selected)) > MAX_BATCH_BYTES - 4096
            ):
                current["parser"] = before
                break
            if len(selected) > MAX_BATCH_EVENTS or len(encoded(selected)) > MAX_BATCH_BYTES - 4096:
                raise ValueError("source_record_too_large")
            events.extend(selected)
            current["sequence"] += len(selected)
            current["offset"] = handle.tell()
            if not current.get("title"):
                current["title"] = next(
                    (e["content"][:200] for e in selected if e["kind"] == "user"), ""
                )
    if events:
        segment = str(uuid.uuid5(uuid.UUID(grant["id"]), f"{grant['client']}:{current['session']}"))
        body = {
            "batch_id": str(uuid.uuid5(uuid.UUID(grant["id"]), events[0]["event_id"])),
            "history_import_id": grant["id"],
            "history_project_path": grant["project_path"],
            "client": grant["client"],
            "host_conversation_id": current["session"],
            "segment_id": segment,
            "context_id": grant["context_id"],
            "capture_generation": grant["capture_generation"],
            "events": events,
            "title": current.get("title", ""),
        }
        raw = encoded(body)
        state["pending"] = {
            "body": body,
            "segment": segment,
            "sha": hashlib.sha256(raw).hexdigest(),
            "id": body["batch_id"],
            "history_import_id": grant["id"],
            "event_ids": json.dumps([e["event_id"] for e in events]),
        }
    if current["offset"] >= current["size"]:
        state["processed_conversations"] += int(current["sequence"] > 0)
        state["current"] = None


def run_import(
    profile: dict,
    grant: dict,
    state_root: Path,
    deadline: float,
    base: str,
    endpoint: str,
    roots: list[Path],
) -> None:
    db = connect_state(state_root / "history" / profile["installation_id"], "history", grant["id"])
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
        fingerprint = hashlib.sha256(profile["upload_key"].encode()).hexdigest()
        state = (
            json.loads(row[0])
            if row
            else {
                "grant": grant,
                "key": fingerprint,
                "directories": [[i, ""] for i in range(len(roots))],
                "directory_count": 0,
                "file_count": 0,
                "files": [],
                "current": None,
                "processed_conversations": 0,
                "processed_events": 0,
                "pending": None,
                "status": "running",
            }
        )
        if state["grant"] != grant or state["key"] != fingerprint:
            state.update(status="failed", error_code="import_failed", pending=None, current=None)
        work_deadline = deadline - min(0.3, max(0, deadline - time.monotonic()) / 3)
        while state["status"] == "running" and time.monotonic() < work_deadline - 0.05:
            if state["pending"]:
                pending = state["pending"]
                batch = {**pending, "body": encoded(pending["body"])}
                outcome = upload(
                    batch, profile["upload_key"], endpoint, work_deadline - time.monotonic()
                )
                if isinstance(outcome, dict) and outcome["status"] == "accepted":
                    state["processed_events"] += len(pending["body"]["events"])
                    state["pending"] = None
                elif outcome == "forbidden" or isinstance(outcome, dict):
                    state.update(
                        status="failed", error_code="import_failed", pending=None, current=None
                    )
                else:
                    break
            elif state["current"]:
                try:
                    scan_file(state, grant, profile, roots, work_deadline)
                except FileNotFoundError:
                    state["current"] = None  # Host retention can remove old local files.
                except OSError:
                    state.update(
                        status="failed",
                        error_code="history_unavailable",
                        pending=None,
                        current=None,
                    )
                except ValueError:
                    state.update(
                        status="failed",
                        error_code="unsupported_format",
                        pending=None,
                        current=None,
                    )
            elif state["files"]:
                root, path = state["files"].pop(0)
                state["current"] = {
                    "root": root,
                    "path": path,
                    "offset": 0,
                    "sequence": 0,
                    "parser": {"calls": {}},
                }
            elif state["directories"]:
                try:
                    discover(state, roots, work_deadline)
                except (OSError, ValueError):
                    state.update(status="failed", error_code="import_failed")
            else:
                state["status"] = "completed"
            # Exact body, parser and offset become durable together before HTTP.
            save_state(db, state)
            db.commit()
            db.execute("BEGIN IMMEDIATE")
        save_state(db, state)
        db.commit()
        if time.monotonic() < deadline - 0.05:
            request(
                base,
                f"/installations/history-imports/{grant['id']}/progress",
                {
                    "state": state["status"],
                    "processed_conversations": state["processed_conversations"],
                    "processed_events": state["processed_events"],
                    "error_code": state.get("error_code"),
                },
                deadline - time.monotonic(),
                key=profile["upload_key"],
            )
    finally:
        db.close()


def sync(
    config: Path,
    client: str,
    *,
    state_root: Path = STATE_PATH,
    seconds: float = 1,
    base: str = API_BASE,
    endpoint: str = UPLOAD_ENDPOINT,
) -> None:
    """No directory discovery before a fresh server grant; no credential output."""
    deadline = time.monotonic() + min(max(seconds, 0), 45)
    if not config.exists():
        return
    with config_lock(config):
        profiles = load_config(config, client)["profiles"]
    for profile in profiles:
        if (
            profile["client"] != client
            or not profile["installation_id"]
            or time.monotonic() >= deadline - 0.05
        ):
            continue
        code, result = request(
            base,
            "/installations/history-imports",
            None,
            deadline - time.monotonic(),
            key=profile["upload_key"],
        )
        if (
            code != 200
            or not isinstance(result, dict)
            or not isinstance(result.get("imports"), list)
        ):
            continue
        if len(result["imports"]) > 20:
            raise ValueError("Too many active history grants")
        grants = [validate_grant(raw, client) for raw in result["imports"]]
        active = {g["id"] for g in grants}
        private_root = state_root / "history" / profile["installation_id"]
        # A fresh authenticated response is also a revocation check. Clear only
        # this installation's own obsolete pending content, never host history.
        for path in private_root.glob("history-*.sqlite3"):
            identity = path.stem.removeprefix("history-")
            if valid_uuid(identity) and identity not in active:
                db = connect_state(private_root, "history", identity)
                try:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("DELETE FROM state")
                    db.commit()
                finally:
                    db.close()
        for grant in grants:
            if time.monotonic() >= deadline - 0.05:
                break
            run_import(profile, grant, state_root, deadline, base, endpoint, history_roots(client))
