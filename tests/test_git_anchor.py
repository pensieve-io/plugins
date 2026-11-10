"""Anchors tag only the hook's own, already saved conversation, and fail open."""

from __future__ import annotations

import io
import json
import os
import subprocess

import anchor_queue
import capture_protocol as protocol
import conversation_capture as capture
import git_anchor
import git_workspace
import pytest
from test_conversation_capture import (
    KEY,
    OWNER,
    SESSION,
    append,
    assistant,
    hook_record,
    setup,
    user,
)

ORIGIN = "https://x-access-token:secret-token@github.com/acme/widgets.git"
PUBLIC = "https://github.com/acme/widgets.git"
PR = "https://github.com/acme/widgets/pull/42"
pytestmark = pytest.mark.usefixtures("isolated_git")


def git(cwd, *args, **env):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, **env},
    )


def repository(tmp_path, origin=ORIGIN, branch="feature/anchors"):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", branch)
    if origin:
        git(root, "remote", "add", "origin", origin)
    git(root, "commit", "-q", "--allow-empty", "-m", "first")
    return root


def head(root):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize(
    "remote,public",
    [
        (ORIGIN, PUBLIC),
        ("https://user@github.com/o/r.git", "https://github.com/o/r.git"),
        ("https://github.com/o/r.git?token=x#frag", "https://github.com/o/r.git"),
        ("ssh://git@github.com:22/o/r.git", "ssh://github.com:22/o/r.git"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("/srv/git/r.git", None),
        ("file:///srv/git/r.git", None),
        ("C:/repos/r", None),
        ("https://github.com/" + "o" * 600, None),
        ("", None),
    ],
)
def test_remote_credentials_never_leave_the_device(remote, public):
    assert git_workspace.public_remote(remote) == public


def test_workspace_reports_origin_branch_and_head(tmp_path):
    root = repository(tmp_path)
    assert git_workspace.workspace(str(root)) == {
        "repo_url": PUBLIC,
        "branch": "feature/anchors",
        "head_sha": head(root),
    }
    git(root, "checkout", "-q", "--detach")
    assert git_workspace.workspace(str(root / "."))["branch"] is None


@pytest.mark.parametrize("case", ["no_origin", "not_a_repo", "relative", "missing"])
def test_workspace_is_absent_outside_a_work_tree_with_origin(tmp_path, case):
    if case == "no_origin":
        cwd = str(repository(tmp_path, origin=None))
    elif case == "not_a_repo":
        cwd = str(tmp_path)
    elif case == "relative":
        cwd = "repo"
    else:
        cwd = str(tmp_path / "absent")
    assert git_workspace.workspace(cwd) is None


def test_a_slow_git_fails_open(tmp_path, monkeypatch):
    root = repository(tmp_path)

    def timeout(*_args, **kwargs):
        assert kwargs["timeout"] == git_workspace.GIT_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired("git", kwargs["timeout"])

    monkeypatch.setattr(git_workspace.subprocess, "run", timeout)
    assert git_workspace.workspace(str(root)) is None


@pytest.mark.parametrize(
    "command,kinds",
    [
        ("git commit -m 'Fix'", ["commit"]),
        ("git commit -am 'x; git push'", ["commit"]),
        ("git -C sub commit -m x", ["commit"]),
        ("git add . && git commit -m x && git push -u origin HEAD", ["commit", "push"]),
        ("GIT_AUTHOR_NAME=a git -c user.name=b --no-pager commit", ["commit"]),
        ("git commit -m \"$(cat <<'EOF'\nTitle\n\ngit push later\nEOF\n)\"", ["commit"]),
        ("git add x # stage it\ngit commit -m y", ["commit"]),
        ("gh pr create --fill", ["pull_request"]),
        ("gh -R acme/widgets pr create -t x -b y 2>&1 | tail -1", ["pull_request"]),
        ("gh pr view 42", []),
        ("gh pr merge 42 --squash", []),
        ("git log --oneline", []),
        ("echo git commit", []),
        ("git commit -m 'unbalanced", []),
    ],
)
def test_only_commits_pushes_and_created_pull_requests_are_detected(tmp_path, command, kinds):
    try:
        found = git_anchor.detected(command, str(tmp_path))
    except ValueError:
        found = []
    assert [kind for kind, _ in found] == kinds


def test_directories_follow_cd_and_git_c(tmp_path):
    found = git_anchor.detected("cd a && git -C ../b commit -m x; git push", str(tmp_path))
    assert found == [("commit", tmp_path / "b"), ("push", tmp_path / "a")]


@pytest.mark.parametrize(
    "response,ok",
    [
        ({"stdout": "[main abc] Fix", "stderr": "", "interrupted": False}, True),
        ({"stdout": "", "interrupted": True}, False),
        ({"stdout": "", "backgroundTaskId": "b1"}, False),
        ({"exit_code": 1, "stdout": ""}, False),
        ({"exit_code": 0, "stdout": ""}, True),
        ("Exit code: 1\nWall time: 0.1 seconds\nOutput:\nnothing to commit", False),
        ("Exit code: 0\nWall time: 0.1 seconds\nOutput:\n[main abc] Fix", True),
        ({"output": "Process exited with code 128"}, False),
    ],
)
def test_failed_or_unfinished_commands_are_never_anchored(response, ok):
    assert git_anchor.succeeded(response) is ok


@pytest.fixture
def saved(tmp_path, monkeypatch):
    """A Claude conversation whose first turn the service has accepted."""
    path, cfg, root, _calls, run = setup(tmp_path, monkeypatch, client="claude")
    append(path, user(client="claude"), hook_record("claude"), assistant(client="claude"))
    run()
    assert capture.saving_status(root, "claude", SESSION)["segments"][0]["last_saved_at"]
    return cfg, root, run


def pending(root, client="claude"):
    return anchor_queue.read_pending(anchor_queue.pending_path(root, client, SESSION))


def event(cwd, command, response=None, session=SESSION, tool="Bash", transcript=None):
    return {
        "transcript_path": None if transcript is None else str(transcript),
        "hook_event_name": "PostToolUse",
        "session_id": session,
        "cwd": str(cwd),
        "tool_name": tool,
        "tool_use_id": "toolu_1",
        "tool_input": {"command": command, "description": "Commit"},
        "tool_response": response
        if response is not None
        else {"stdout": "", "stderr": "", "interrupted": False, "isImage": False},
    }


def anchor(saved, service, payload, client="claude"):
    cfg, root, _run = saved
    return git_anchor.run_hook(payload, client, cfg, root, service["endpoint"])


def test_a_commit_anchors_the_hooks_own_saved_conversation(tmp_path, saved, anchor_service):
    repo = repository(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "agent change")
    assert anchor(saved, anchor_service, event(repo, "git commit -m 'agent change'")) == [
        ("commit", 200)
    ]
    [(path, headers, body, size)] = anchor_service["requests"]
    assert path == "/hooks/conversations/anchors"
    assert headers["Authorization"] == "Bearer " + KEY
    assert headers[protocol.HEADER] == "2"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == "Pensieve-Plugin-Capture/1.0"
    assert body == {
        "client": "claude",
        "host_conversation_id": SESSION,
        "context_id": 497,
        "kind": "commit",
        "repo_url": PUBLIC,
        "branch": "feature/anchors",
        "commit_sha": head(repo),
        "pr_number": None,
        "pr_url": None,
        "tool_call_id": "toolu_1",
        "turn_id": None,
    }
    assert size <= git_anchor.MAX_REQUEST_BYTES
    assert "secret-token" not in json.dumps(body)


def test_commit_and_push_in_one_command_send_both(tmp_path, saved, anchor_service):
    repo = repository(tmp_path)
    sent = anchor(saved, anchor_service, event(repo, "git commit -m x && git push"))
    assert sent == [("commit", 200), ("push", 200)]
    assert [r[2]["kind"] for r in anchor_service["requests"]] == ["commit", "push"]


@pytest.mark.parametrize(
    "output", [PR + "\n", {"stdout": "Creating pull request\n" + PR + "\n", "stderr": ""}]
)
def test_created_pull_request_is_parsed_from_tool_output(tmp_path, saved, anchor_service, output):
    repo = repository(tmp_path)
    anchor(saved, anchor_service, event(repo, "gh pr create --fill", output))
    body = anchor_service["requests"][0][2]
    assert body["kind"] == "pull_request"
    assert body["pr_url"] == PR and body["pr_number"] == 42
    assert body["commit_sha"] == head(repo)


def test_pull_request_without_a_url_is_skipped(tmp_path, saved, anchor_service):
    repo = repository(tmp_path)
    assert anchor(saved, anchor_service, event(repo, "gh pr create --web")) == []
    assert anchor_service["requests"] == []


def test_a_stale_head_is_not_this_commands_commit(tmp_path, saved, anchor_service):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "remote", "add", "origin", ORIGIN)
    old = "2020-01-01T00:00:00+00:00"
    git(root, "commit", "-q", "--allow-empty", "-m", "old", GIT_COMMITTER_DATE=old)
    # `git commit ...; echo done` exits 0 even when nothing was committed.
    assert anchor(saved, anchor_service, event(root, "git commit -m x; echo done")) == []
    assert anchor_service["requests"] == []


@pytest.mark.parametrize(
    "change",
    [
        "failed",
        "interrupted",
        "background",
        "other_tool",
        "other_session",
        "not_a_repo",
        "no_origin",
        "not_git",
    ],
)
def test_nothing_is_sent_unless_a_saved_conversation_ran_a_successful_git_command(
    tmp_path, saved, anchor_service, change
):
    repo = repository(tmp_path, origin=None if change == "no_origin" else ORIGIN)
    payload = event(repo, "git commit -m x")
    if change == "failed":
        payload["tool_response"] = "Exit code: 1\nOutput:\nnothing to commit"
    if change == "interrupted":
        payload["tool_response"]["interrupted"] = True
    if change == "background":
        payload["tool_input"]["run_in_background"] = True
    if change == "other_tool":
        payload["tool_name"] = "mcp__github__create_pull_request"
    if change == "other_session":
        payload["session_id"] = "173e0b53-8178-4a3c-8d40-a07414144741"
    if change == "not_a_repo":
        payload["cwd"] = str(tmp_path)
    if change == "not_git":
        payload["tool_input"]["command"] = "ls"
    assert anchor(saved, anchor_service, payload) == []
    assert anchor_service["requests"] == []


@pytest.mark.parametrize("delivery", ["service_hold", "forbidden", "capture_disabled"])
def test_held_or_refused_conversations_are_not_anchored(tmp_path, saved, anchor_service, delivery):
    cfg, root, _run = saved
    db = capture.connect_state(root, "claude", SESSION)
    db.execute("UPDATE delivery SET status=?", (delivery,))
    db.commit()
    db.close()
    repo = repository(tmp_path)
    assert anchor(saved, anchor_service, event(repo, "git commit -m x")) == []
    assert anchor_service["requests"] == []


def test_a_stopped_conversation_is_not_anchored(tmp_path, saved, anchor_service, monkeypatch):
    cfg, root, run = saved
    monkeypatch.setattr(
        capture, "upload", lambda *a: {"status": "capture_disabled", "conversation_stopped": True}
    )
    path = tmp_path / "transcript.jsonl"
    append(path, user("More", client="claude"), hook_record("claude"))
    append(path, assistant("Answer", client="claude"))
    run()
    repo = repository(tmp_path)
    assert anchor(saved, anchor_service, event(repo, "git commit -m x")) == []
    assert anchor_service["requests"] == []


def test_a_conversation_with_nothing_saved_yet_queues_without_posting(
    tmp_path, monkeypatch, anchor_service
):
    path, cfg, root, _calls, run = setup(tmp_path, monkeypatch, client="claude")
    monkeypatch.setattr(capture, "upload", lambda *a: {"status": "unavailable"})
    append(path, user(client="claude"), hook_record("claude"), assistant(client="claude"))
    run()
    repo = repository(tmp_path)
    payload = event(repo, "git commit -m x")
    sent = git_anchor.run_hook(payload, "claude", cfg, root, anchor_service["endpoint"])
    assert sent == [("commit", "queued")]
    assert anchor_service["requests"] == []
    assert [entry["anchor"]["kind"] for entry in pending(root)] == ["commit"]


@pytest.mark.parametrize("from_transcript", [False, True])
def test_a_disabled_consent_generation_is_not_anchored(
    tmp_path, monkeypatch, anchor_service, from_transcript
):
    path, cfg, root, _calls, run = setup(tmp_path, monkeypatch, client="claude")
    append(path, user(client="claude"), hook_record("claude"), assistant(client="claude"))
    run()
    append(path, user("Off", client="claude"), hook_record("claude", generation=None))
    if not from_transcript:
        run()
    repo = repository(tmp_path)
    payload = event(repo, "git commit -m x", transcript=path if from_transcript else None)
    assert git_anchor.run_hook(payload, "claude", cfg, root, anchor_service["endpoint"]) == []
    assert pending(root) == []


def test_not_saved_is_kept_silently_for_the_next_accepted_upload(
    tmp_path, saved, anchor_service, capsys
):
    anchor_service.update(status=404, body={"reason": "not_saved"})
    repo = repository(tmp_path)
    assert anchor(saved, anchor_service, event(repo, "git commit -m x")) == [("commit", 404)]
    assert len(anchor_service["requests"]) == 1
    assert capsys.readouterr().err == ""
    assert len(pending(saved[1])) == 1


def test_service_failure_is_reported_on_stderr_only(tmp_path, saved, anchor_service, capsys):
    anchor_service.update(status=403, body={"reason": "service_hold"})
    repo = repository(tmp_path)
    assert anchor(saved, anchor_service, event(repo, "git commit -m x")) == [("commit", 403)]
    captured = capsys.readouterr()
    assert captured.out == "" and "403" in captured.err
    assert KEY not in captured.err
    assert pending(saved[1]) == []


def test_unreachable_service_fails_open(tmp_path, saved):
    cfg, root, _run = saved
    repo = repository(tmp_path)
    payload = event(repo, "git commit -m x")
    sent = git_anchor.run_hook(
        payload, "claude", cfg, root, "http://127.0.0.1:9/hooks/conversations"
    )
    assert sent == [("commit", "unavailable")]
    assert len(pending(root)) == 1


def test_codex_shell_output_is_understood(tmp_path, monkeypatch, anchor_service):
    path, cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    append(path, user(), hook_record(), assistant())
    run()
    repo = repository(tmp_path)
    payload = event(repo, "git commit -m x", "Exit code: 0\nWall time: 0.2 seconds\nOutput:\n")
    payload.update(turn_id="turn", model="gpt", permission_mode="default", transcript_path=None)
    assert git_anchor.run_hook(payload, "codex", cfg, root, anchor_service["endpoint"]) == [
        ("commit", 200)
    ]
    assert anchor_service["requests"][0][2]["client"] == "codex"


def test_hook_prints_only_empty_json(monkeypatch, capsys, tmp_path):
    payload = event(tmp_path, "git commit -m x")
    monkeypatch.setattr(git_anchor.sys, "argv", ["git_anchor.py", "--client", "claude"])
    monkeypatch.setenv("PENSIEVE_CAPTURE_STATE", str(tmp_path / "absent-state"))
    monkeypatch.setenv("PENSIEVE_CAPTURE_CONFIG", str(tmp_path / "absent.json"))
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(git_anchor.sys, "stdin", stdin)
    git_anchor.main()
    assert capsys.readouterr().out.strip() == "{}"
    assert not (tmp_path / "absent-state").exists()


@pytest.mark.parametrize(
    "endpoint", ["https://evil.test/hooks/conversations", "http://localhost/hooks/anchors"]
)
def test_upload_key_cannot_be_sent_elsewhere(endpoint):
    with pytest.raises(ValueError):
        anchor_queue.post_anchor(endpoint, KEY, {}, 1)


def test_capture_scope_is_the_saved_conversations_owner_and_context(saved):
    _cfg, root, _run = saved
    assert git_anchor.capture_scope(root, "claude", SESSION, None) == (OWNER, 497, True)


@pytest.mark.parametrize("tool_use_id", [None, "", "has space", 7, "x" * 256])
def test_an_unusable_tool_call_id_is_sent_as_null(tmp_path, saved, anchor_service, tool_use_id):
    repo = repository(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "agent change")
    payload = event(repo, "git commit -m 'agent change'")
    payload["tool_use_id"] = tool_use_id
    assert anchor(saved, anchor_service, payload) == [("commit", 200)]
    assert anchor_service["requests"][0][2]["tool_call_id"] is None


def test_codex_sends_the_turn_its_transcript_saves(tmp_path, saved, anchor_service):
    repo = repository(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "agent change")
    payload = event(repo, "git commit -m 'agent change'")
    payload.update(tool_use_id="exec-7712b828", turn_id="01a1c61c-1e04")
    assert anchor(saved, anchor_service, payload) == [("commit", 200)]
    body = anchor_service["requests"][0][2]
    assert (body["tool_call_id"], body["turn_id"]) == ("exec-7712b828", "01a1c61c-1e04")
