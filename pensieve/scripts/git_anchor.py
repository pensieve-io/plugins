"""Anchor a saved conversation to the commits, pushes and pull requests it made.

A PostToolUse hook on the agent's shell tool. It reads only the hook's own
payload: the command the agent ran, its reported result and working directory.
It tags the conversation named by the hook's own `session_id`, never one found
by working directory, so a human's commit or another conversation in the same
repository is never attributed here. It acts only for a conversation whose
transcript is being shared, with the key that conversation uploads with: an
anchor waits in `anchor_queue` until the conversation's first upload is
accepted. It fails open: nothing it does can change or delay the agent's command. Python 3.9+,
standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import stat
import sys
import time
from pathlib import Path

MAX_INPUT_BYTES = 1024 * 1024
MAX_ANCHORS = 3
MAX_REQUEST_BYTES = 2048
MAX_PR_URL_CHARS = 500
REQUEST_TIMEOUT_SECONDS = 3
HOOK_BUDGET_SECONDS = 4
# A commit reported as made by this command is younger than this. An older HEAD
# means the commit itself did not happen, whatever the chain's exit status said.
COMMIT_FRESH_SECONDS = 600
SEPARATORS = {"&&", "||", ";", "|", "&", "\n", "(", ")", ";;", "|&"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
# git options that take their value as the next argument.
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
GH_VALUE_OPTIONS = {"-R", "--repo"}
PR_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/(\d+)")
EXIT_CODE = re.compile(r"(?:exit code|exited with code)\W*(-?\d+)", re.IGNORECASE)


def command_text(tool_input: object) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if isinstance(command, str):
        return command
    if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        # An argv such as `["bash", "-lc", "<script>"]` runs its script.
        if len(command) == 3 and command[1] in {"-c", "-lc"}:
            return command[2]
        return shlex.join(command)
    return None


def segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands; quoting is honoured."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    # shlex would swallow a comment's newline and join the next command to it.
    lexer.commenters = ""
    result, current = [], []
    for token in lexer:
        if token in SEPARATORS or set(token) <= set("();|&\n"):
            if current:
                result.append(current)
            current = []
        else:
            current.append(token)
    if current:
        result.append(current)
    return result


def resolve(base: Path, target: str) -> Path:
    return Path(os.path.normpath(base / os.path.expanduser(target)))


def detected(command: str, cwd: str) -> list[tuple[str, Path]]:
    """`(kind, directory)` for each commit, push or PR creation, in order."""
    found = []
    directory = Path(cwd)
    for words in segments(command):
        while words and ASSIGNMENT.fullmatch(words[0]):
            words = words[1:]  # `GIT_AUTHOR_NAME=x git commit`
        if not words:
            continue
        if words[0] == "cd" and len(words) == 2 and words[1] != "-":
            directory = resolve(directory, words[1])
            continue
        if words[0] == "git":
            where, index = directory, 1
            while index < len(words) and words[index].startswith("-"):
                option = words[index]
                if option in GIT_VALUE_OPTIONS and index + 1 < len(words):
                    if option == "-C":
                        where = resolve(where, words[index + 1])
                    index += 2
                    continue
                index += 1
            if index < len(words) and words[index] in {"commit", "push"}:
                found.append((words[index], where))
        elif words[0] == "gh":
            rest = [
                word
                for position, word in enumerate(words[1:], 1)
                if word not in GH_VALUE_OPTIONS and words[position - 1] not in GH_VALUE_OPTIONS
            ]
            if rest[:2] == ["pr", "create"]:
                found.append(("pull_request", directory))
    unique = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique[:MAX_ANCHORS]


def output_text(response: object) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        parts = [
            response.get(field)
            for field in ("stdout", "output", "aggregated_output", "formatted_output", "stderr")
        ]
        return "\n".join(part for part in parts if isinstance(part, str))
    return ""


def succeeded(response: object) -> bool:
    """False when the host reported a failed, interrupted or unfinished command."""
    if isinstance(response, dict):
        if response.get("interrupted") is True or response.get("is_error") is True:
            return False
        if response.get("backgroundTaskId") or response.get("success") is False:
            return False  # A background command has not finished yet.
        for field in ("exit_code", "exitCode", "returncode"):
            if field in response:
                return response[field] == 0
        response = response.get("output")
    if isinstance(response, str):
        # Codex's shell output opens with `Exit code: N`.
        match = EXIT_CODE.search(response[:200])
        if match:
            return int(match.group(1)) == 0
    # Otherwise unknown: the commit HEAD guard still applies.
    return True


def capture_scope(
    state_root: Path, client: str, session: str, transcript: object
) -> tuple[str, int, bool] | None:
    """`(owner, context, saved)` while this conversation is being saved, else None.

    The newest native capture marker in the hook's own transcript names the
    current turn's owner, context and consent generation, even in a first turn
    the spool has yet to read; the spool's attributed scope stands in without
    one. The spool is read read-only and lock-free: the capture hooks own it. A
    conversation with no spool, or stopped, disabled, held or refused, is not
    anchored. `saved` says whether an upload in that context was accepted.
    """
    from capture_onboarding import latest_attribution

    path = state_root / f"{client}-{session}.sqlite3"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        return None
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else None
        if not isinstance(state, dict) or state.get("capture_stopped"):
            return None
        found = latest_attribution(transcript, client, session)
        if found is not None:
            marker = found[0]
            scope = [marker["user_id"], marker["context_id"], marker["capture_generation"]]
        else:
            scope = state.get("scope") or state.get("candidate_scope")
        if (
            not isinstance(scope, list)
            or len(scope) != 3
            or not isinstance(scope[0], str)
            or type(scope[1]) is not int
            or scope[2] is None
        ):
            return None  # No company, or sharing is off for this turn.
        owner, context, generation = scope
        latest = db.execute(
            """SELECT d.status FROM segments s JOIN delivery d ON d.segment=s.id
               WHERE s.owner=? AND s.context=? ORDER BY d.last_attempt_at DESC LIMIT 1""",
            (owner, context),
        ).fetchone()
        if latest is not None and latest["status"] in {"service_hold", "forbidden"}:
            return None
        segment = db.execute(
            """SELECT s.retired,d.status FROM segments s LEFT JOIN delivery d ON d.segment=s.id
               WHERE s.owner=? AND s.context=? AND s.generation=? ORDER BY s.rowid DESC LIMIT 1""",
            (owner, context, generation),
        ).fetchone()
        if segment is not None and (
            segment["retired"] or segment["status"] in {"capture_disabled", "deleted"}
        ):
            return None
        saved = db.execute(
            """SELECT 1 FROM segments s JOIN delivery d ON d.segment=s.id
               WHERE s.owner=? AND s.context=? AND d.last_saved_at IS NOT NULL LIMIT 1""",
            (owner, context),
        ).fetchone()
        return owner, context, saved is not None
    finally:
        db.close()


def anchor(kind: str, directory: Path, output: str) -> dict | None:
    """The anchor's repository fields, or None outside a usable work tree."""
    from git_workspace import git, workspace

    repository = workspace(str(directory))
    if repository is None:
        return None
    body = {
        "kind": kind,
        "repo_url": repository["repo_url"],
        "branch": repository["branch"],
        "commit_sha": repository["head_sha"],
        "pr_number": None,
        "pr_url": None,
    }
    if kind == "commit":
        committed = git(directory, "log", "-1", "--format=%ct", "HEAD")
        if (
            body["commit_sha"] is None
            or not committed
            or not committed.isdigit()
            or time.time() - int(committed) > COMMIT_FRESH_SECONDS
        ):
            return None
    if kind == "pull_request":
        matches = list(PR_URL.finditer(output))
        if not matches or len(matches[-1].group(0)) > MAX_PR_URL_CHARS:
            return None  # `gh pr create --web` prints no pull request.
        body.update(pr_url=matches[-1].group(0), pr_number=int(matches[-1].group(1)))
    return body


def run_hook(
    payload: dict,
    client: str,
    config: Path,
    state_root: Path,
    endpoint: str,
) -> list[tuple[str, int | str]]:
    """Post this command's anchors; returns `(kind, response)` for each one sent."""
    if client not in {"claude", "codex"} or payload.get("hook_event_name") != "PostToolUse":
        return []
    if payload.get("tool_name") != "Bash":
        return []
    command = command_text(payload.get("tool_input"))
    cwd = payload.get("cwd")
    if command is None or not isinstance(cwd, str) or not os.path.isabs(cwd):
        return []
    # Cheap rejection first: almost every shell command is not one of these.
    if "git" not in command and "gh" not in command:
        return []
    try:
        found = detected(command, cwd)
    except ValueError:
        return []  # Unbalanced quoting: the shell refused it too.
    response = payload.get("tool_response")
    if not found or (
        isinstance(payload.get("tool_input"), dict)
        and payload["tool_input"].get("run_in_background") is True
    ):
        return []
    if not succeeded(response):
        return []

    from anchor_queue import post_anchor, queue_anchor, retryable
    from capture_pause import is_paused

    if is_paused(state_root):
        return []  # Saving is paused on this machine: nothing is sent or kept.
    from capture_config import key_for, profiles
    from context_receipt import conversation_id

    session = conversation_id(payload.get("session_id"))
    if session is None:
        return []
    scope = capture_scope(state_root, client, session, payload.get("transcript_path"))
    if scope is None:
        return []
    owner, context, saved = scope
    key = key_for(profiles(config, client), owner, context)
    if key is None:
        return []
    deadline = time.monotonic() + HOOK_BUDGET_SECONDS
    output = output_text(response)
    sent = []
    for kind, directory in found:
        fields = anchor(kind, directory, output)
        remaining = deadline - time.monotonic()
        if fields is None or remaining < 0.05:
            continue
        body = {"client": client, "host_conversation_id": session, "context_id": context, **fields}
        if len(json.dumps(body, separators=(",", ":")).encode()) > MAX_REQUEST_BYTES:
            continue
        if not saved:
            # A first turn uploads at Stop: until then the service could only
            # answer `not_saved`. The upload that saves it sends this anchor.
            queue_anchor(state_root, client, session, owner, body)
            sent.append((kind, "queued"))
            continue
        code, _ = post_anchor(endpoint, key, body, min(REQUEST_TIMEOUT_SECONDS, remaining))
        sent.append((kind, code))
        if code == 404 or retryable(code):
            # `not_saved`, or no answer: the next accepted upload sends it.
            queue_anchor(state_root, client, session, owner, body)
        elif code != 200:
            print(f"Pensieve could not anchor this {kind} ({code}).", file=sys.stderr)
    return sent


def main() -> None:
    from conversation_capture import CONFIG_PATH, STATE_PATH, UPLOAD_ENDPOINT, checked_endpoint

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
    except (OSError, ValueError, sqlite3.DatabaseError):
        pass  # An anchor is never worth interrupting the agent's command.
    # Nothing for the model: an anchor changes no tool result.
    print("{}")


if __name__ == "__main__":
    main()
