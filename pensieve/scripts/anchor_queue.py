"""Pending conversation anchors, kept until the conversation's transcript is saved.

An agent commonly commits in a conversation's first turn, before its first
upload at Stop, when the service could only answer `not_saved`. Such anchors
wait beside the conversation's spool in `<client>-<session>.anchors.json` and
are sent after an upload in their context has been accepted. Entries hold the
anchor body (repository identities only) and its owner: never transcript text
or a key. Standard library, Python 3.9+.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from capture_config import MAX_CONFIG_BYTES, private_file, private_lock, save_private_json

MAX_PENDING = 20
PENDING_TTL_SECONDS = 24 * 60 * 60
LOCK_WAIT_SECONDS = 0.2
REQUEST_TIMEOUT_SECONDS = 3
REPLAY_BUDGET_SECONDS = 4
MAX_REQUEST_BYTES = 2048


def retryable(code: int | str) -> bool:
    """Kept for the next accepted upload: the service may be unreachable or upgrading."""
    return code == "unavailable" or code in {426, 429} or (type(code) is int and code >= 500)


def pending_path(root: Path, client: str, session: str) -> Path:
    if client not in {"claude", "codex"}:
        raise ValueError("Invalid capture client")
    return root / f"{client}-{session}.anchors.json"


def identity(anchor: dict) -> list:
    """Two anchors are the same when kind, commit and pull request agree."""
    return [anchor.get("kind"), anchor.get("commit_sha"), anchor.get("pr_url")]


def read_pending(path: Path) -> list[dict]:
    try:
        value = json.loads(private_file(path, MAX_CONFIG_BYTES))
    except FileNotFoundError:
        return []
    if not isinstance(value, list):
        raise ValueError("Invalid pending anchors")
    return [
        entry
        for entry in value
        if isinstance(entry, dict)
        and isinstance(entry.get("anchor"), dict)
        and isinstance(entry.get("owner"), str)
        and isinstance(entry.get("queued_at"), (int, float))
    ]


def update_pending(path: Path, change) -> list[dict] | None:
    """Apply `change(entries) -> entries` under the queue's lock; None when busy."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            with private_lock(path.with_suffix(".lock")):
                try:
                    entries = read_pending(path)
                except ValueError:
                    entries = []  # A damaged queue holds nothing worth keeping.
                fresh = time.time() - PENDING_TTL_SECONDS
                entries = change([entry for entry in entries if entry["queued_at"] >= fresh])
                if entries:
                    save_private_json(path, entries[-MAX_PENDING:])
                elif path.exists():
                    path.unlink()
                return entries
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)


def queue_anchor(root: Path, client: str, session: str, owner: str, anchor: dict) -> bool:
    """Keep one anchor for later, newest last; the oldest go beyond the bound."""
    key = identity(anchor)

    def add(entries):
        kept = [entry for entry in entries if identity(entry["anchor"]) != key]
        return [*kept, {"queued_at": time.time(), "owner": owner, "anchor": anchor}]

    return update_pending(pending_path(root, client, session), add) is not None


def post_anchor(endpoint: str, key: str, body: dict, timeout: float) -> tuple[int | str, dict]:
    from urllib.error import HTTPError, URLError
    from urllib.request import ProxyHandler, Request, build_opener

    from capture_protocol import HEADER, VERSION, NoRedirects
    from conversation_capture import checked_endpoint

    request = Request(
        checked_endpoint(endpoint) + "/anchors",
        data=json.dumps(body, separators=(",", ":")).encode(),
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
        with opener.open(request, timeout=max(0.05, timeout)) as response:
            return response.status, json_body(response.read(4097))
    except HTTPError as exc:
        try:
            return exc.code, json_body(exc.read(4097))
        except OSError:
            return exc.code, {}
    except (OSError, URLError, ValueError):
        return "unavailable", {}


def json_body(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def replay(
    root: Path,
    client: str,
    session: str,
    saved: set,
    key_for_scope,
    endpoint: str,
    deadline: float,
    *,
    stopped: bool = False,
) -> None:
    """Send pending anchors whose context already holds an accepted upload.

    `saved` is the `(owner, context)` pairs with an accepted upload. Each anchor
    leaves the queue on 200 or a permanent refusal, including 404 now that the
    transcript is known to be saved; a network failure or 5xx keeps it for the
    next accepted upload. A stopped conversation's anchors are discarded.
    """
    path = pending_path(root, client, session)
    if not path.exists():
        return
    if stopped:
        update_pending(path, lambda entries: [])
        return
    entries = update_pending(path, lambda entries: entries)
    if not entries:
        return
    deadline = min(deadline, time.monotonic() + REPLAY_BUDGET_SECONDS)
    resolved = []
    for entry in entries:
        anchor = entry["anchor"]
        scope = (entry["owner"], anchor.get("context_id"))
        remaining = deadline - time.monotonic()
        if remaining < 0.05:
            break
        if scope not in saved or anchor.get("host_conversation_id") != session:
            continue
        key = key_for_scope(*scope)
        if key is None:
            resolved.append(identity(anchor))  # Its upload credential was removed.
            continue
        if len(json.dumps(anchor, separators=(",", ":")).encode()) > MAX_REQUEST_BYTES:
            resolved.append(identity(anchor))
            continue
        code, _ = post_anchor(endpoint, key, anchor, min(REQUEST_TIMEOUT_SECONDS, remaining))
        if not retryable(code):
            resolved.append(identity(anchor))
    if resolved:
        update_pending(
            path,
            lambda entries: [e for e in entries if identity(e["anchor"]) not in resolved],
        )
