"""A machine-wide pause saves nothing, sends nothing new and fails closed."""

from __future__ import annotations

import json

import anchor_queue
import capture_pause
import conversation_capture as capture
import git_anchor
import pytest
from test_conversation_capture import (
    ACCEPTED,
    SESSION,
    append,
    assistant,
    config,
    events,
    hook_record,
    pending,
    setup,
    user,
)
from test_git_anchor import event, repository

PAUSED_AT = "2026-11-07T09:30:00.000Z"


def pause(root, content=None):
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / capture_pause.PAUSE_FILE
    path.write_text(json.dumps({"paused_at": PAUSED_AT}) if content is None else content)
    return path


@pytest.mark.parametrize(
    "content,paused_at",
    [
        (None, PAUSED_AT),
        (json.dumps({"paused_at": "2026-11-07T09:30:00+00:00"}), "2026-11-07T09:30:00+00:00"),
        (json.dumps({"paused_at": "2026-11-07T09:30:00"}), None),  # No timezone.
        (json.dumps({"paused_at": "yesterday"}), None),
        (json.dumps({}), None),
        ("not json", None),
        ("[" * 3000, None),
        (" " * (capture_pause.MAX_PAUSE_BYTES + 1), None),
        ("", None),
    ],
)
def test_any_pause_file_pauses_and_only_a_valid_time_is_reported(tmp_path, content, paused_at):
    root = tmp_path / "state"
    assert capture_pause.pause_state(root) is None
    pause(root, content)
    assert capture_pause.pause_state(root) == {"paused_at": paused_at}


@pytest.mark.parametrize("kind", ["directory", "symlink", "unreadable"])
def test_an_unreadable_pause_file_still_pauses(tmp_path, kind):
    root = tmp_path / "state"
    root.mkdir()
    path = root / capture_pause.PAUSE_FILE
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"paused_at": PAUSED_AT}))
        path.symlink_to(target)
    else:
        path.write_text(json.dumps({"paused_at": PAUSED_AT}))
        path.chmod(0)
    try:
        assert capture_pause.pause_state(root) == {"paused_at": None}
    finally:
        if kind == "unreadable":
            path.chmod(0o600)


@pytest.mark.parametrize("content", [None, "malformed"])
def test_turns_written_while_paused_are_never_uploaded_even_after_resume(
    tmp_path, monkeypatch, content
):
    path, _cfg, root, calls, run = setup(tmp_path, monkeypatch)
    append(path, user("Before"), hook_record(), assistant("Before answer"))
    run()
    flag = pause(root, content)
    append(path, user("Private"), hook_record(), assistant("Private answer"))
    run("UserPromptSubmit")
    run()
    # Nothing was read into the spool or sent while paused.
    assert pending(root) == 0
    assert [e["content"] for e in events(calls)] == ["Before", "Before answer"]
    flag.unlink()
    run()  # The first hook after resume fences what was written while paused.
    append(path, user("After"), hook_record(), assistant("After answer"))
    run()
    contents = [e["content"] for e in events(calls)]
    assert contents == ["Before", "Before answer", "After", "After answer"]


def test_work_queued_before_the_pause_waits_and_uploads_after_resume(tmp_path, monkeypatch):
    path, _cfg, root, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *a: capture.unavailable("network_error"))
    append(path, user("Queued"), hook_record(), assistant("Queued answer"))
    run()
    assert pending(root) == 2
    flag = pause(root)
    monkeypatch.setattr(
        capture, "upload", lambda batch, key, *a: calls.append((batch, key)) or ACCEPTED
    )
    run()
    run("UserPromptSubmit")
    assert calls == []  # Held, not sent, while paused.
    assert pending(root) == 2
    flag.unlink()
    run()
    assert [e["content"] for e in events(calls)] == ["Queued", "Queued answer"]
    assert pending(root) == 0


def test_turns_held_for_a_first_company_do_not_survive_a_pause(tmp_path, monkeypatch):
    path, _cfg, root, calls, run = setup(tmp_path, monkeypatch)
    append(
        path,
        user("Before any company"),
        hook_record(context=None, generation=None),
        assistant("Early answer"),
    )
    run()
    assert pending(root) == 2
    flag = pause(root)
    run()
    flag.unlink()
    run()
    # Unlike queued work, a held turn was never attributed to a company the
    # member shares with, so a pause ends its wait.
    assert pending(root) == 0
    append(path, user("After"), hook_record(), assistant("After answer"))
    run("UserPromptSubmit")
    append(path, user("Later"), hook_record(), assistant("Later answer"))
    run()
    saved = [e["content"] for e in events(calls)]
    assert "Before any company" not in saved and "Early answer" not in saved
    assert saved[-2:] == ["Later", "Later answer"]


def test_a_conversation_started_while_paused_creates_no_spool(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    root = tmp_path / "spool"
    pause(root)
    transcript = tmp_path / "new.jsonl"
    append(transcript, {"type": "session_meta", "payload": {"id": SESSION}})
    monkeypatch.setattr(capture, "upload", lambda *a: pytest.fail("uploaded while paused"))
    for hook in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        payload = {
            "session_id": SESSION,
            "hook_event_name": hook,
            "transcript_path": str(transcript),
        }
        assert capture.run_hook(payload, "codex", cfg, root) == {}
    assert not list(root.glob("*.sqlite3"))


def notice(run):
    return run("UserPromptSubmit").get("hookSpecificOutput", {}).get("additionalContext")


def test_a_paused_conversation_is_told_once(tmp_path, monkeypatch):
    path, _cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    append(path, user(), hook_record(), assistant())
    run()
    pause(root)
    text = notice(run)
    assert "paused on this machine" in text and "`pensieve resume`" in text
    assert notice(run) is None


def test_a_member_who_does_not_share_is_not_told_and_gets_no_spool(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    root = tmp_path / "spool"
    pause(root)
    transcript = tmp_path / "unshared.jsonl"
    append(transcript, {"type": "session_meta", "payload": {"id": SESSION}})
    append(transcript, user(), hook_record(generation=None), assistant())
    payload = {
        "session_id": SESSION,
        "hook_event_name": "UserPromptSubmit",
        "transcript_path": str(transcript),
    }
    assert capture.run_hook(payload, "codex", cfg, root) == {}
    assert not list(root.glob("*.sqlite3"))


def test_status_reports_the_pause(tmp_path, monkeypatch):
    path, _cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    status = capture.saving_status(root, "codex", SESSION)
    assert status["paused"] is False and status["paused_at"] is None
    pause(root)
    status = capture.saving_status(root, "codex", SESSION)
    assert status["paused"] is True and status["paused_at"] == PAUSED_AT
    pause(root, "malformed")
    status = capture.saving_status(root, "codex", SESSION)
    assert status["paused"] is True and status["paused_at"] is None


@pytest.mark.usefixtures("isolated_git")
def test_anchors_are_neither_sent_nor_kept_while_paused(tmp_path, monkeypatch, anchor_service):
    path, cfg, root, _calls, run = setup(tmp_path, monkeypatch, client="claude")
    append(path, user(client="claude"), hook_record("claude"), assistant(client="claude"))
    run()
    repo = repository(tmp_path)
    payload = event(repo, "git commit -m x", transcript=path)
    flag = pause(root)
    assert git_anchor.run_hook(payload, "claude", cfg, root, anchor_service["endpoint"]) == []
    assert anchor_service["requests"] == []
    assert not anchor_queue.pending_path(root, "claude", SESSION).exists()
    flag.unlink()
    assert git_anchor.run_hook(payload, "claude", cfg, root, anchor_service["endpoint"]) == [
        ("commit", 200)
    ]


def test_the_pause_is_read_from_the_state_override(tmp_path, monkeypatch, capsys):
    root = tmp_path / "override"
    pause(root)
    monkeypatch.setenv("PENSIEVE_CAPTURE_STATE", str(root))
    monkeypatch.setattr(
        capture.sys, "argv", ["conversation_capture.py", "--client", "codex", "--status", SESSION]
    )
    capture.main()
    assert json.loads(capsys.readouterr().out)["paused"] is True
