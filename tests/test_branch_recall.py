"""Branch recall arrives right after a shell command, once per branch, and fails open."""

from __future__ import annotations

import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import branch_recall
import capture_config as config
import context_briefing as briefing
import git_anchor
import plugin_connection as connection
import pytest
from test_git_anchor import ORIGIN, PUBLIC, git, repository

OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
INSTALLATION = "193e0b53-8178-4a3c-8d40-a07414144741"
SESSION = "923bbd76-d864-4b8f-b252-c2b7c3692492"
KEY = "pcap_" + "r" * 43
LISTED = {
    "status": "listed",
    "hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "Other saved conversations worked on this branch:\n- [Fix]",
    },
}
pytestmark = pytest.mark.usefixtures("isolated_git")


@pytest.fixture
def service():
    state = {"status": 200, "body": LISTED, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append((self.path, dict(self.headers), json.loads(raw), len(raw)))
            body = json.dumps(state["body"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    state["endpoint"] = f"http://127.0.0.1:{server.server_port}/hooks/recall"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def connected(tmp_path):
    """A Claude installation whose briefing hook has connected."""
    path = tmp_path / "private" / "capture.json"
    config.install_profile(
        path,
        {
            "user_id": OWNER,
            "context_id": None,
            "client": "claude",
            "upload_key": KEY,
            "installation_id": INSTALLATION,
            "runtime": "claude_code_cli",
            "host_version": "",
            "briefing_enabled": True,
        },
    )
    config.save_private_json(
        connection.header_path(path, "claude"), {connection.HEADER: f"claude {KEY}"}
    )
    return path


def event(cwd, command="ls", session=SESSION, tool="Bash"):
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session,
        "cwd": str(cwd),
        "tool_name": tool,
        "tool_input": {"command": command},
        "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
    }


def recall(connected, service, payload, client="claude"):
    deadline = time.monotonic() + git_anchor.HOOK_BUDGET_SECONDS
    return git_anchor.recall_hook(payload, client, connected, service["endpoint"], deadline)


def state(connected):
    return json.loads(branch_recall.state_path(connected, "claude", SESSION).read_text())


def test_arriving_on_a_branch_asks_once_and_injects_the_answer(tmp_path, connected, service):
    repo = repository(tmp_path)
    assert recall(connected, service, event(repo)) == {
        "hookSpecificOutput": LISTED["hookSpecificOutput"]
    }
    [(path, headers, body, size)] = service["requests"]
    assert path == "/hooks/recall"
    assert headers["Authorization"] == "Bearer " + KEY
    assert headers["Content-Type"] == "application/json"
    assert body == {
        "client": "claude",
        "session_id": SESSION,
        "workspace": {"repo_url": PUBLIC, "branch": "feature/anchors"},
    }
    assert size <= briefing.MAX_REQUEST_BYTES
    assert "secret-token" not in json.dumps(body)
    assert state(connected)["status"] == "listed"
    # The same branch is never asked again, whatever the next command is.
    assert recall(connected, service, event(repo, "git status")) is None
    assert len(service["requests"]) == 1


def test_an_empty_branch_is_not_asked_again(tmp_path, connected, service):
    service["body"] = {"status": "empty"}
    repo = repository(tmp_path)
    assert recall(connected, service, event(repo)) is None
    assert recall(connected, service, event(repo)) is None
    assert len(service["requests"]) == 1
    assert state(connected)["status"] == "empty"


def test_a_checkout_mid_turn_asks_about_the_new_branch(tmp_path, connected, service):
    repo = repository(tmp_path)
    recall(connected, service, event(repo))
    git(repo, "checkout", "-q", "-b", "euan/slack-pacing")
    assert recall(connected, service, event(repo, "git checkout euan/slack-pacing"))
    assert [r[2]["workspace"]["branch"] for r in service["requests"]] == [
        "feature/anchors",
        "euan/slack-pacing",
    ]


@pytest.mark.parametrize(
    "command", ["git -C ../repo switch main", "cd ../repo && git log -1", "cd ../repo"]
)
def test_recall_follows_the_directory_the_command_worked_in(tmp_path, connected, service, command):
    repository(tmp_path, branch="feature/there")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert recall(connected, service, event(elsewhere, command))
    assert service["requests"][0][2]["workspace"]["branch"] == "feature/there"


@pytest.mark.parametrize(
    ("origin_head", "branch", "asked"),
    [
        (None, "main", False),
        (None, "master", False),
        ("develop", "develop", False),
        # `main` is ordinary work where the repository's default is another branch.
        ("develop", "main", True),
    ],
)
def test_the_default_branch_is_never_asked_about(
    tmp_path, connected, service, origin_head, branch, asked
):
    repo = repository(tmp_path, branch=branch)
    if origin_head:
        git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{origin_head}")
    assert bool(recall(connected, service, event(repo))) is asked
    assert bool(recall(connected, service, event(repo))) is False
    assert len(service["requests"]) == int(asked)
    assert state(connected)["status"] == ("listed" if asked else "default")
    # Leaving the default branch asks as usual.
    git(repo, "checkout", "-q", "-b", "feature/next")
    assert recall(connected, service, event(repo))


def test_an_unavailable_answer_is_asked_again_only_after_a_minute(tmp_path, connected, service):
    service["body"] = {"status": "unavailable"}
    repo = repository(tmp_path)
    assert recall(connected, service, event(repo)) is None
    assert recall(connected, service, event(repo)) is None
    assert len(service["requests"]) == 1
    saved = state(connected)
    assert saved["status"] == "unavailable"
    saved["checked_at"] -= branch_recall.RETRY_UNAVAILABLE_SECONDS
    config.save_private_json(branch_recall.state_path(connected, "claude", SESSION), saved)
    service["body"] = LISTED
    assert recall(connected, service, event(repo))
    assert len(service["requests"]) == 2


@pytest.mark.parametrize(
    "status,body",
    [
        (401, {"detail": "A plugin credential is required"}),
        (404, {"detail": "Not Found"}),
        (500, {}),
        (200, {"status": "listed", "hookSpecificOutput": {"hookEventName": "Stop"}}),
        (200, {"status": "listed"}),
        (200, ["not", "an", "object"]),
    ],
)
def test_refusals_and_odd_answers_inject_nothing_and_back_off(
    tmp_path, connected, service, status, body
):
    service.update(status=status, body=body)
    repo = repository(tmp_path)
    assert recall(connected, service, event(repo)) is None
    assert state(connected)["status"] == "unavailable"


def test_an_unreachable_service_fails_open(tmp_path, connected):
    repo = repository(tmp_path)
    unreachable = {"endpoint": "http://127.0.0.1:9/hooks/recall"}
    assert recall(connected, unreachable, event(repo)) is None
    assert state(connected)["status"] == "unavailable"


@pytest.mark.parametrize("case", ["detached", "no_origin", "not_a_repo", "other_tool", "no_time"])
def test_nothing_is_asked_without_a_named_branch_or_time(tmp_path, connected, service, case):
    repo = repository(tmp_path, origin=None if case == "no_origin" else ORIGIN)
    payload = event(tmp_path if case == "not_a_repo" else repo)
    if case == "detached":
        git(repo, "checkout", "-q", "--detach")
    if case == "other_tool":
        payload["tool_name"] = "Read"
    deadline = time.monotonic() + (0 if case == "no_time" else git_anchor.HOOK_BUDGET_SECONDS)
    assert (
        git_anchor.recall_hook(payload, "claude", connected, service["endpoint"], deadline) is None
    )
    assert service["requests"] == []


def test_an_installation_its_briefing_never_connected_asks_nothing(tmp_path, service):
    repo = repository(tmp_path)
    unconnected = tmp_path / "private" / "capture.json"
    assert recall(unconnected, service, event(repo)) is None
    assert service["requests"] == []
    # Recall never creates the installation proof the MCP helper owns.
    assert not connection.header_path(unconnected, "claude").exists()


def test_a_held_lock_skips_rather_than_asking_twice(tmp_path, connected, service):
    repo = repository(tmp_path)
    path = branch_recall.state_path(connected, "claude", SESSION)
    with config.private_lock(path.with_suffix(".lock")):
        assert recall(connected, service, event(repo)) is None
    assert service["requests"] == []
    assert recall(connected, service, event(repo))


def test_hook_prints_the_recall_for_the_host_to_inject(
    tmp_path, connected, service, monkeypatch, capsys
):
    repo = repository(tmp_path)
    argv = ["git_anchor.py", "--client", "claude", "--recall-endpoint", service["endpoint"]]
    monkeypatch.setattr(git_anchor.sys, "argv", argv)
    monkeypatch.setenv("PENSIEVE_CAPTURE_CONFIG", str(connected))
    monkeypatch.setenv("PENSIEVE_CAPTURE_STATE", str(tmp_path / "absent-state"))

    def run():
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(event(repo)).encode()))
        monkeypatch.setattr(git_anchor.sys, "stdin", stdin)
        git_anchor.main()
        return json.loads(capsys.readouterr().out)

    assert run() == {"hookSpecificOutput": LISTED["hookSpecificOutput"]}
    assert run() == {}


@pytest.mark.parametrize(
    "endpoint",
    ["https://evil.test/hooks/recall", "http://localhost/hooks/recall/../briefing"],
)
def test_the_briefing_credential_cannot_be_sent_elsewhere(endpoint):
    with pytest.raises(ValueError):
        briefing.checked_endpoint(endpoint)
    assert briefing.checked_endpoint("http://127.0.0.1:8123/hooks/recall")
    assert briefing.RECALL_ENDPOINT == "https://mcp.pensieve.uk/hooks/recall"
