"""Anchor a saved conversation to the commits, pushes and pull requests it made.

A PostToolUse hook on the agent's shell tool. After every command it also asks
`branch_recall` whether the agent has just arrived on a branch other saved
conversations worked on; that answer is the hook's only output.

For anchors it starts from the hook's own payload: the command the agent ran,
its reported result and working directory, then reads that directory's git
state and the conversation's own capture spool. It tags the conversation named by
the hook's own `session_id`, never one found by working directory, so a
human's commit or another conversation in the same repository is never
attributed here. It acts only for a conversation whose
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
# A commit made by this command is the work tree's newest HEAD move, and
# younger than this. Anything else means the commit itself did not happen,
# whatever the chain's exit status said.
COMMIT_FRESH_SECONDS = 600
REFLOG_COMMIT = re.compile(r"HEAD@\{(\d+)\} commit\b.*", re.DOTALL)
SEPARATOR_CHARACTERS = set("();|&\n")
# `<<EOF`, `<<-EOF` or `<<'EOF'`, but not a `<<<` here-string.
HEREDOC = re.compile(r"(?<!<)<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
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


def without_heredocs(command: str) -> str:
    """The command without its heredoc bodies: they are data, and shlex cannot read them."""
    kept, closing = [], None
    for line in command.split("\n"):
        if closing is not None:
            if line.strip() == closing:
                closing = None
            continue
        kept.append(line)
        match = HEREDOC.search(line)
        if match:
            closing = match.group(2)
    return "\n".join(kept)


def segments(command: str) -> list[list[str] | str]:
    """Split a shell command into simple commands; quoting is honoured.

    A subshell's `(` and `)` are kept as items of their own, so a `cd` inside
    one can be undone where it closes.
    """
    lexer = shlex.shlex(without_heredocs(command), posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    # shlex would swallow a comment's newline and join the next command to it.
    lexer.commenters = ""
    result: list[list[str] | str] = []
    current: list[str] = []
    try:
        for token in lexer:
            if set(token) <= SEPARATOR_CHARACTERS:
                if current:
                    result.append(current)
                current = []
                result.extend(character for character in token if character in "()")
            else:
                current.append(token)
    except ValueError:
        # shlex is not a shell: quotes inside `"$(…)"` or an apostrophe in a
        # comment are valid shell it cannot read. What it read before that
        # still names the command, since a message follows `git commit`.
        pass
    if current:
        result.append(current)
    return result


def resolve(base: Path | None, target: str) -> Path | None:
    """`target` from `base`; None when it depends on shell state the hook cannot see."""
    target = os.path.expanduser(target)
    if "$" in target or "`" in target or (base is None and not os.path.isabs(target)):
        return None
    return Path(os.path.normpath((base or Path(os.sep)) / target))


def git_target(words: list[str], directory: Path | None) -> tuple[Path | None, int]:
    """For `git [options] <subcommand> …`: its working directory and subcommand index."""
    where, index = directory, 1
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        if option in GIT_VALUE_OPTIONS and index + 1 < len(words):
            if option == "-C":
                where = resolve(where, words[index + 1])
            index += 2
            continue
        index += 1
    return where, index


def walk(command: str, cwd: str) -> tuple[list[tuple[str, Path]], Path]:
    """Each commit, push or PR creation with its directory, and where the work ended up.

    The second value is the command's last git or gh target, else its last
    `cd`. A directory the hook cannot follow (`cd "$REPO"`) anchors nothing,
    rather than naming the wrong repository, and leaves the host's `cwd` as
    where the work ended up.
    """
    found: list[tuple[str, Path]] = []
    directory: Path | None = Path(cwd)
    target: Path | None = None
    subshells: list[Path | None] = []
    for words in segments(command):
        if words == "(":
            subshells.append(directory)
            continue
        if words == ")":
            if subshells:
                directory = subshells.pop()
            continue
        while words and ASSIGNMENT.fullmatch(words[0]):
            words = words[1:]  # `GIT_AUTHOR_NAME=x git commit`
        if not words:
            continue
        if words[0] == "cd" and len(words) == 2 and words[1] != "-":
            directory = resolve(directory, words[1])
        elif words[0] == "git":
            where, index = git_target(words, directory)
            target = where or target
            if (
                where is not None
                and index < len(words)
                and words[index] in {"commit", "push"}
                and "--dry-run" not in words
            ):
                found.append((words[index], where))
        elif words[0] == "gh" and directory is not None:
            target = directory
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
    return unique[:MAX_ANCHORS], target or directory or Path(cwd)


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
        if response.get("interrupted") is True:
            return False
        if response.get("backgroundTaskId"):
            return False  # A background command has not finished yet.
        for field in ("exit_code", "exitCode"):
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
        # A pull, a checkout or someone's older commit also leaves a recent
        # HEAD; only this work tree's own reflog says a commit was just made.
        moved = REFLOG_COMMIT.fullmatch(
            git(directory, "reflog", "-1", "--date=unix", "--format=%gd %gs", "HEAD") or ""
        )
        if (
            body["commit_sha"] is None
            or moved is None
            or time.time() - int(moved.group(1)) > COMMIT_FRESH_SECONDS
        ):
            return None
    if kind == "pull_request":
        matches = list(PR_URL.finditer(output))
        if not matches or len(matches[-1].group(0)) > MAX_PR_URL_CHARS:
            return None  # `gh pr create --web` prints no pull request.
        body.update(pr_url=matches[-1].group(0), pr_number=int(matches[-1].group(1)))
    return body


def shell_call(payload: dict, client: str) -> tuple[str, str, str] | None:
    """`(command, cwd, session)` for a finished shell call from a known host, else None."""
    from context_receipt import conversation_id

    if client not in {"claude", "codex"} or payload.get("hook_event_name") != "PostToolUse":
        return None
    if payload.get("tool_name") != "Bash":
        return None
    session = conversation_id(payload.get("session_id"))
    cwd = payload.get("cwd")
    if session is None or not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    return command_text(payload.get("tool_input")) or "", cwd, session


def run_hook(
    payload: dict,
    client: str,
    config: Path,
    state_root: Path,
    endpoint: str,
    deadline: float | None = None,
) -> list[tuple[str, int | str]]:
    """Post this command's anchors; returns `(kind, response)` for each one sent."""
    call = shell_call(payload, client)
    if call is None:
        return []
    command, cwd, session = call
    # Cheap rejection first: almost every shell command is not one of these.
    if "git" not in command and "gh" not in command:
        return []
    found, _ = walk(command, cwd)
    response = payload.get("tool_response")
    if not found or (
        isinstance(payload.get("tool_input"), dict)
        and payload["tool_input"].get("run_in_background") is True
    ):
        return []
    if not succeeded(response):
        return []

    from anchor_queue import post_anchor, queue_anchor, retryable
    from capture_adapters import capture_id
    from capture_pause import is_paused

    if is_paused(state_root):
        return []  # Saving is paused on this machine: nothing is sent or kept.
    from capture_config import key_for, profiles

    scope = capture_scope(state_root, client, session, payload.get("transcript_path"))
    if scope is None:
        return []
    owner, context, saved = scope
    key = key_for(profiles(config, client), owner, context)
    if key is None:
        return []
    # The captured tool call carries this id too, so recall can open the
    # conversation at the turn that ran this command.
    tool_call_id = capture_id(payload.get("tool_use_id"))
    # Codex names the call by an id its transcript never saves, but gives the
    # turn id its transcript does save; Claude Code gives none.
    turn_id = capture_id(payload.get("turn_id"))
    if deadline is None:
        deadline = time.monotonic() + HOOK_BUDGET_SECONDS
    output = output_text(response)
    sent = []
    for kind, directory in found:
        fields = anchor(kind, directory, output)
        remaining = deadline - time.monotonic()
        if fields is None or remaining < 0.05:
            continue
        body = {
            "client": client,
            "host_conversation_id": session,
            "context_id": context,
            **fields,
            "tool_call_id": tool_call_id,
            "turn_id": turn_id,
        }
        if len(json.dumps(body, separators=(",", ":")).encode()) > MAX_REQUEST_BYTES:
            continue
        if not saved:
            # A first turn uploads at Stop: until then the service could only
            # answer `not_saved`. The upload that saves it sends this anchor.
            queue_anchor(state_root, client, session, owner, body)
            sent.append((kind, "queued"))
            continue
        code = post_anchor(endpoint, key, body, min(REQUEST_TIMEOUT_SECONDS, remaining))
        sent.append((kind, code))
        if code == 404 or retryable(code):
            # `not_saved`, or no answer: the next accepted upload sends it.
            queue_anchor(state_root, client, session, owner, body)
        elif code != 200:
            print(f"Pensieve could not anchor this {kind} ({code}).", file=sys.stderr)
    return sent


def recall_hook(payload: dict, client: str, config: Path, endpoint: str, deadline: float):
    """Hook output naming the branch's other saved conversations, or None."""
    from branch_recall import recall

    call = shell_call(payload, client)
    if call is None or deadline - time.monotonic() < 0.5:
        return None  # No time left after anchors: the next command asks.
    command, cwd, session = call
    return recall(client, session, walk(command, cwd)[1], config, endpoint, deadline)


def main() -> None:
    from context_briefing import RECALL_ENDPOINT
    from context_briefing import checked_endpoint as checked_recall_endpoint
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
    parser.add_argument("--recall-endpoint", type=checked_recall_endpoint, default=RECALL_ENDPOINT)
    args = parser.parse_args()
    deadline = time.monotonic() + HOOK_BUDGET_SECONDS
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        payload = json.loads(raw) if len(raw) <= MAX_INPUT_BYTES else None
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        print("{}")
        return
    try:
        run_hook(payload, args.client, args.config, args.state, args.endpoint, deadline)
    except (OSError, ValueError, sqlite3.DatabaseError):
        pass  # An anchor is never worth interrupting the agent's command.
    output = None
    try:
        # After anchors: an anchor skipped for time is lost, an unasked
        # recall is simply asked by the next command.
        output = recall_hook(payload, args.client, args.config, args.recall_endpoint, deadline)
    except (OSError, ValueError):
        pass  # Recall is a hint; it never interrupts the agent's command.
    # An anchor changes no tool result; only recall adds model context.
    print(json.dumps(output or {}))


if __name__ == "__main__":
    main()
