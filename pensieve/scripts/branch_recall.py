"""Tell the agent which saved conversations worked on its branch, after a shell command.

The anchor hook (`git_anchor.py`, PostToolUse on the shell tool) calls this
after every command. It reads the branch of the directory the command ran in
and, when that differs from the branch last recalled for this conversation,
asks `POST /hooks/recall` which other saved conversations committed, pushed or
opened pull requests there. A listed answer becomes the hook's output, so the
host adds it to the model's context straight after the tool result: mid-turn,
right after a checkout, rather than at the next prompt. The briefing never
carries this list.

Local state is one private file per conversation beside the briefing state:
each `{repo_url, branch}` asked about, the answer's status and its time. A
branch is not asked again after `listed` or `empty`, however often the agent
leaves and returns; `unavailable` is asked again after a minute, and is
recorded before the request so a hook killed mid-request backs off too. The
repository's default branch is never asked about: most conversations start
there, and what was committed straight to it is rarely the work at hand.
SessionStart clears the file, so a new, resumed or compacted window is told
again. It uses the plugin's own briefing credential, never an upload key, and
fails open: nothing here can change or delay the agent's command. Standard
library, Python 3.9+.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from capture_config import (
    CLIENTS,
    MAX_CONFIG_BYTES,
    private_file,
    private_lock,
    save_private_json,
)

RETRY_UNAVAILABLE_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 3
LOCK_WAIT_SECONDS = 0.2
# The service refuses a larger recall request outright.
MAX_REQUEST_BYTES = 2048
# Branches remembered per conversation; the oldest is asked about again.
MAX_BRANCHES = 20


def state_path(config: Path, client: str, session: str) -> Path:
    if client not in CLIENTS:
        raise ValueError("Invalid plugin client")
    return config.parent / "briefing" / f"{client}-{session}.recall.json"


def read_state(path: Path) -> dict:
    """`{"<repo_url>\\n<branch>": {status, checked_at}}`, oldest first."""
    try:
        value = json.loads(private_file(path, MAX_CONFIG_BYTES))
    except (FileNotFoundError, ValueError):
        return {}  # Damaged state only costs one more question.
    branches = value.get("branches") if isinstance(value, dict) else None
    return branches if isinstance(branches, dict) else {}


def record(path: Path, branch: str, status: str) -> None:
    """Remember one branch's answer; call under the state's lock."""
    branches = read_state(path)
    branches.pop(branch, None)
    branches[branch] = {"status": status, "checked_at": time.time()}
    save_private_json(path, {"branches": dict(list(branches.items())[-MAX_BRANCHES:])})


def locked(path: Path, action):
    """`action()` under the state's lock; None while another hook holds it."""
    try:
        with private_lock(path.with_suffix(".lock"), LOCK_WAIT_SECONDS):
            return action()
    except BlockingIOError:
        return None


def reset(config: Path, client: str, session: str) -> None:
    """Forget what this conversation was told, so the next command asks again."""
    path = state_path(config, client, session)
    if path.exists() or path.is_symlink():
        locked(path, lambda: path.unlink(missing_ok=True))


def due(branches: dict, branch: str, now: float) -> bool:
    known = branches.get(branch)
    if not isinstance(known, dict):
        return True
    checked = known.get("checked_at")
    return known.get("status") == "unavailable" and (
        not isinstance(checked, (int, float)) or now - checked >= RETRY_UNAVAILABLE_SECONDS
    )


def briefing_key(config: Path, client: str) -> str | None:
    """The installation's briefing credential, once its briefing hook has connected."""
    from capture_config import load_config
    from plugin_connection import connected_profile
    from plugin_headers import HEADER, header_path, read_headers

    try:
        token = read_headers(header_path(config, client), client)[HEADER].split(" ", 1)[1]
        profiles = load_config(config, client)["profiles"]
    except (OSError, ValueError, KeyError):
        return None
    return token if connected_profile(profiles, client, token) else None


def answer(code: int | str, result: object) -> tuple[str, dict | None]:
    """`(status, hook output)` from the service's reply; anything odd is unavailable."""
    if code != 200 or not isinstance(result, dict):
        return "unavailable", None
    status = result.get("status")
    if status == "empty":
        return "empty", None
    output = result.get("hookSpecificOutput")
    if (
        status == "listed"
        and isinstance(output, dict)
        and output.get("hookEventName") == "PostToolUse"
        and isinstance(output.get("additionalContext"), str)
        and output["additionalContext"].strip()
    ):
        # Rebuilt rather than forwarded: Codex refuses unknown output fields.
        text = output["additionalContext"]
        return "listed", {
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}
        }
    return "unavailable", None


def recall(
    client: str,
    session: str,
    directory: Path,
    config: Path,
    endpoint: str,
    deadline: float,
) -> dict | None:
    """Hook output naming this branch's other saved conversations, or None."""
    from context_briefing import request, request_bytes
    from git_workspace import on_default_branch, workspace

    repository = workspace(str(directory), head=False)
    if repository is None or repository["branch"] is None:
        return None  # Detached, or outside a work tree with `origin`.
    branch = f"{repository['repo_url']}\n{repository['branch']}"
    path = state_path(config, client, session)
    if not due(read_state(path), branch, time.time()):
        return None  # Cheap check before the credential and the lock.
    if on_default_branch(directory, repository["branch"]):
        # Recorded, so later commands here skip the probe as well.
        locked(path, lambda: record(path, branch, "default"))
        return None
    key = briefing_key(config, client)
    if key is None:
        return None
    body = {"client": client, "session_id": session, "workspace": repository}
    if len(request_bytes(body)) > MAX_REQUEST_BYTES:
        return None

    def ask():
        if not due(read_state(path), branch, time.time()):
            return None  # Another command on this branch asked meanwhile.
        remaining = deadline - time.monotonic()
        if remaining < 0.25:
            return None  # Unrecorded: the next command asks instead.
        # Before the request: the host kills a hook whose request outlives its
        # timeout, and without this every later command would ask and hang too.
        record(path, branch, "unavailable")
        code, result = request(endpoint, key, body, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining))
        status, output = answer(code, result)
        if status != "unavailable":
            record(path, branch, status)
        return output

    return locked(path, ask)
