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
the last `{repo_url, branch}` asked about, the answer's status and its time.
The same branch is not asked again after `listed` or `empty`; `unavailable` is
asked again after a minute. The repository's default branch is never asked
about: most conversations start there, and what was committed straight to it
is rarely the work at hand. SessionStart clears it, so a new, resumed or
compacted window is told again. It uses the plugin's own briefing credential,
never an upload key, and fails open: nothing here can change or delay the
agent's command. Standard library, Python 3.9+.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from capture_config import MAX_CONFIG_BYTES, private_file, private_lock, save_private_json

RETRY_UNAVAILABLE_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 3
LOCK_WAIT_SECONDS = 0.2


def state_path(config: Path, client: str, session: str) -> Path:
    if client not in {"claude", "codex"}:
        raise ValueError("Invalid plugin client")
    return config.parent / "briefing" / f"{client}-{session}.recall.json"


def read_state(path: Path) -> dict:
    try:
        value = json.loads(private_file(path, MAX_CONFIG_BYTES))
    except FileNotFoundError:
        return {}
    except ValueError:
        return {}  # Damaged state only costs one more question.
    return value if isinstance(value, dict) else {}


def locked(path: Path, action):
    """`action()` under the state's lock; None while another hook holds it."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            with private_lock(path.with_suffix(".lock")):
                return action()
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)


def reset(config: Path, client: str, session: str) -> None:
    """Forget what this conversation was told, so the next command asks again."""
    path = state_path(config, client, session)
    if path.exists() or path.is_symlink():
        locked(path, lambda: path.unlink(missing_ok=True))


def due(state: dict, repository: dict, now: float) -> bool:
    if state.get("workspace") != repository:
        return True
    checked = state.get("checked_at")
    return state.get("status") == "unavailable" and (
        not isinstance(checked, (int, float)) or now - checked >= RETRY_UNAVAILABLE_SECONDS
    )


def briefing_key(config: Path, client: str) -> str | None:
    """The installation's briefing credential, once its briefing hook has connected."""
    from capture_config import load_config
    from plugin_headers import HEADER, header_path, read_headers

    try:
        token = read_headers(header_path(config, client), client)[HEADER].split(" ", 1)[1]
        profiles = load_config(config, client)["profiles"]
    except (OSError, ValueError, KeyError):
        return None
    connected = any(
        isinstance(profile, dict)
        and profile.get("client") == client
        and profile.get("upload_key") == token
        and profile.get("briefing_enabled") is True
        and profile.get("context_id") is None
        for profile in profiles
    )
    return token if connected else None


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
    from context_briefing import MAX_REQUEST_BYTES, request, request_bytes
    from git_workspace import on_default_branch, workspace

    repository = workspace(str(directory), head=False)
    if repository is None or repository["branch"] is None:
        return None  # Detached, or outside a work tree with `origin`.
    path = state_path(config, client, session)
    if not due(read_state(path), repository, time.time()):
        return None  # Cheap check before the credential and the lock.
    if on_default_branch(directory, repository["branch"]):
        # Recorded, so later commands here skip the probe as well.
        locked(
            path, lambda: save_private_json(path, {"workspace": repository, "status": "default"})
        )
        return None
    key = briefing_key(config, client)
    if key is None:
        return None
    body = {"client": client, "session_id": session, "workspace": repository}
    if len(request_bytes(body)) > MAX_REQUEST_BYTES:
        return None

    def ask():
        if not due(read_state(path), repository, time.time()):
            return None  # Another command on this branch asked meanwhile.
        remaining = deadline - time.monotonic()
        if remaining < 0.25:
            return None  # Unrecorded: the next command asks instead.
        code, result = request(endpoint, key, body, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining))
        status, output = answer(code, result)
        save_private_json(
            path, {"workspace": repository, "status": status, "checked_at": time.time()}
        )
        return output

    return locked(path, ask)
