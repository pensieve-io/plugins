"""Anchors made before a conversation is saved wait for its first accepted upload."""

from __future__ import annotations

import json
import time

import anchor_queue
import conversation_capture as capture
import git_anchor
import pytest
from capture_config import save_private_json
from test_conversation_capture import (
    OWNER,
    SESSION,
    append,
    assistant,
    hook_record,
    setup,
    user,
)
from test_git_anchor import event, head, repository

pytestmark = pytest.mark.usefixtures("isolated_git")


def pending(root, client="claude"):
    return anchor_queue.read_pending(anchor_queue.pending_path(root, client, SESSION))


def stop(path, cfg, root, service, client="claude", event_name="Stop"):
    payload = {"session_id": SESSION, "hook_event_name": event_name, "transcript_path": str(path)}
    return capture.run_hook(payload, client, cfg, root, service["endpoint"])


def body(kind="commit", sha="a" * 40, pr=None, context=497):
    return {
        "client": "claude",
        "host_conversation_id": SESSION,
        "context_id": context,
        "kind": kind,
        "repo_url": "https://github.com/acme/widgets.git",
        "branch": "main",
        "commit_sha": sha,
        "pr_number": None if pr is None else int(pr.rsplit("/", 1)[1]),
        "pr_url": pr,
    }


@pytest.fixture
def saved_conversation(tmp_path, monkeypatch):
    path, cfg, root, _calls, run = setup(tmp_path, monkeypatch, client="claude")
    append(path, user(client="claude"), hook_record("claude"), assistant(client="claude"))
    run()
    return path, cfg, root


def test_a_first_turn_commit_is_anchored_by_the_stop_upload(tmp_path, monkeypatch, anchor_service):
    path, cfg, root, calls, _run = setup(tmp_path, monkeypatch, client="claude")
    # Mid-way through the first turn: its prompt and marker are written, but
    # nothing of the conversation has been uploaded.
    append(path, user(client="claude"), hook_record("claude"))
    repo = repository(tmp_path)
    payload = event(repo, "git commit -m x", transcript=path)
    sent = git_anchor.run_hook(payload, "claude", cfg, root, anchor_service["endpoint"])
    assert sent == [("commit", "queued")]
    assert anchor_service["requests"] == []
    append(path, assistant(client="claude"))
    stop(path, cfg, root, anchor_service)
    assert calls, "the turn itself uploaded"
    [(route, headers, posted, _size)] = anchor_service["requests"]
    assert route == "/hooks/conversations/anchors"
    assert headers["Authorization"] == "Bearer synthetic-upload-key-owner-one"
    assert posted["host_conversation_id"] == SESSION and posted["context_id"] == 497
    assert posted["commit_sha"] == head(repo)
    assert pending(root) == []


def test_nothing_is_replayed_before_an_upload_is_accepted(tmp_path, monkeypatch, anchor_service):
    path, cfg, root, _calls, _run = setup(tmp_path, monkeypatch, client="claude")
    monkeypatch.setattr(capture, "upload", lambda *a: capture.unavailable("network_error"))
    append(path, user(client="claude"), hook_record("claude"))
    anchor_queue.queue_anchor(root, "claude", SESSION, OWNER, body())
    append(path, assistant(client="claude"))
    stop(path, cfg, root, anchor_service)
    assert anchor_service["requests"] == []
    assert len(pending(root)) == 1


def test_duplicates_collapse_and_the_queue_is_bounded(tmp_path):
    root = tmp_path / "spool"
    owner = OWNER
    pr = "https://github.com/acme/widgets/pull/7"
    for _ in range(3):
        anchor_queue.queue_anchor(root, "claude", SESSION, owner, body())
        anchor_queue.queue_anchor(root, "claude", SESSION, owner, body("pull_request", pr=pr))
    assert [entry["anchor"]["kind"] for entry in pending(root)] == ["commit", "pull_request"]
    for index in range(anchor_queue.MAX_PENDING + 5):
        anchor_queue.queue_anchor(root, "claude", SESSION, owner, body(sha=f"{index:040x}"))
    kept = pending(root)
    assert len(kept) == anchor_queue.MAX_PENDING
    # The newest are kept.
    assert kept[-1]["anchor"]["commit_sha"] == f"{anchor_queue.MAX_PENDING + 4:040x}"
    raw = anchor_queue.pending_path(root, "claude", SESSION).read_text()
    assert "synthetic-upload-key" not in raw


@pytest.mark.parametrize("status", [403, 410, 422, 404, 401])
def test_a_permanent_refusal_after_a_saved_upload_drops_the_anchor(
    saved_conversation, anchor_service, status
):
    path, cfg, root = saved_conversation
    anchor_service.update(status=status, body={"reason": "refused"})
    anchor_queue.queue_anchor(root, "claude", SESSION, OWNER, body())
    stop(path, cfg, root, anchor_service)
    assert len(anchor_service["requests"]) == 1
    assert pending(root) == []


@pytest.mark.parametrize("status", [500, 503, 426])
def test_a_server_error_keeps_the_anchor_for_the_next_accepted_upload(
    saved_conversation, anchor_service, status
):
    path, cfg, root = saved_conversation
    anchor_service.update(status=status, body={})
    anchor_queue.queue_anchor(root, "claude", SESSION, OWNER, body())
    stop(path, cfg, root, anchor_service)
    assert len(pending(root)) == 1
    anchor_service.update(status=200, body={"anchor_id": "a", "conversation_id": "c"})
    stop(path, cfg, root, anchor_service)
    assert len(anchor_service["requests"]) == 2
    assert pending(root) == []


def test_a_network_error_keeps_the_anchor(saved_conversation):
    path, cfg, root = saved_conversation
    anchor_queue.queue_anchor(root, "claude", SESSION, OWNER, body())
    unreachable = {"endpoint": "http://127.0.0.1:9/hooks/conversations"}
    stop(path, cfg, root, unreachable)
    assert len(pending(root)) == 1


def test_anchors_older_than_a_day_are_dropped(saved_conversation, anchor_service):
    path, cfg, root = saved_conversation
    anchor_queue.queue_anchor(root, "claude", SESSION, OWNER, body())
    file = anchor_queue.pending_path(root, "claude", SESSION)
    entries = json.loads(file.read_text())
    entries[0]["queued_at"] = time.time() - anchor_queue.PENDING_TTL_SECONDS - 1
    save_private_json(file, entries)
    stop(path, cfg, root, anchor_service)
    assert anchor_service["requests"] == []
    assert pending(root) == []


def test_a_stopped_conversation_discards_its_pending_anchors(tmp_path, monkeypatch, anchor_service):
    path, cfg, root, _calls, _run = setup(tmp_path, monkeypatch, client="claude")
    monkeypatch.setattr(
        capture, "upload", lambda *a: {"status": "capture_disabled", "conversation_stopped": True}
    )
    append(path, user(client="claude"), hook_record("claude"))
    anchor_queue.queue_anchor(root, "claude", SESSION, OWNER, body())
    append(path, assistant(client="claude"))
    stop(path, cfg, root, anchor_service)
    assert anchor_service["requests"] == []
    assert pending(root) == []


def test_an_anchor_for_another_context_waits_for_that_context(saved_conversation, anchor_service):
    path, cfg, root = saved_conversation
    anchor_queue.queue_anchor(root, "claude", SESSION, OWNER, body(context=12))
    stop(path, cfg, root, anchor_service)
    assert anchor_service["requests"] == []
    assert len(pending(root)) == 1


def test_a_held_conversation_queues_nothing(tmp_path, monkeypatch, anchor_service):
    path, cfg, root, _calls, run = setup(tmp_path, monkeypatch, client="claude")
    monkeypatch.setattr(capture, "upload", lambda *a: {"status": "service_hold"})
    append(path, user(client="claude"), hook_record("claude"), assistant(client="claude"))
    run()
    append(path, user("Next", client="claude"), hook_record("claude"))
    repo = repository(tmp_path)
    payload = event(repo, "git commit -m x", transcript=path)
    assert git_anchor.run_hook(payload, "claude", cfg, root, anchor_service["endpoint"]) == []
    assert pending(root) == []
