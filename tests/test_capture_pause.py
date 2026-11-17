"""A pause, of the machine or of one conversation, saves nothing written during it."""

from __future__ import annotations

import json

import anchor_queue
import capture_adapters
import capture_pause
import conversation_capture as capture
import git_anchor
import pytest
from capture_config import private_lock, profiles
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


def command(name, client, path, cfg, root):
    """Send one pause command the way its host delivers it."""
    payload = {"session_id": SESSION, "transcript_path": str(path)}
    if client == "claude":
        payload.update(
            hook_event_name="UserPromptExpansion",
            expansion_type="slash_command",
            command_name="pensieve:" + name,
            command_source="plugin",
            prompt="/" + name,
        )
    else:
        payload.update(hook_event_name="UserPromptSubmit", prompt="$pensieve:" + name)
    return capture.run_hook(payload, client, cfg, root)


def turn(path, text, client):
    append(path, user(text, client), hook_record(client), assistant(text + " answer", client))


@pytest.mark.parametrize(
    "client,payload,expected",
    [
        ("codex", {"hook_event_name": "UserPromptSubmit", "prompt": "$pensieve:pause"}, True),
        ("codex", {"hook_event_name": "UserPromptSubmit", "prompt": " $pensieve:resume\n"}, False),
        ("codex", {"hook_event_name": "UserPromptSubmit", "prompt": "$pensieve:pause now"}, None),
        ("codex", {"hook_event_name": "UserPromptSubmit", "prompt": "run $pensieve:pause"}, None),
        ("codex", {"hook_event_name": "UserPromptSubmit", "prompt": "pensieve:pause"}, None),
        ("codex", {"hook_event_name": "UserPromptSubmit", "prompt": "$other:pause"}, None),
        ("codex", {"hook_event_name": "UserPromptSubmit", "prompt": None}, None),
        ("codex", {"hook_event_name": "Stop", "prompt": "$pensieve:pause"}, None),
        (
            "claude",
            {"hook_event_name": "UserPromptExpansion", "command_name": "pensieve:pause"},
            True,
        ),
        (
            "claude",
            {"hook_event_name": "UserPromptExpansion", "command_name": "pensieve:resume"},
            False,
        ),
        ("claude", {"hook_event_name": "UserPromptExpansion", "command_name": "other:pause"}, None),
        ("claude", {"hook_event_name": "UserPromptExpansion", "command_name": "pause"}, None),
        (
            "claude",
            {"hook_event_name": "UserPromptExpansion", "command_name": ["pensieve:pause"]},
            None,
        ),
        # Claude's own prompt hook sees the typed line; only the expansion names the command.
        ("claude", {"hook_event_name": "UserPromptSubmit", "prompt": "/pensieve:pause"}, None),
        ("claude", {"hook_event_name": "UserPromptSubmit", "prompt": "$pensieve:pause"}, None),
    ],
)
def test_only_the_command_itself_pauses_a_conversation(client, payload, expected):
    assert capture_pause.conversation_command(payload, client) is expected


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_a_paused_conversation_saves_what_came_before_and_after_only(tmp_path, monkeypatch, client):
    path, cfg, root, calls, run = setup(tmp_path, monkeypatch, client=client)
    turn(path, "Before", client)
    run()
    # Written after the last hook ran: the pause reads it before taking effect.
    turn(path, "Tail", client)
    answer = command("pause", client, path, cfg, root)
    sigil = "/" if client == "claude" else "$"
    assert answer["decision"] == "block"
    assert "saving is paused for this conversation" in answer["reason"]
    assert sigil + "pensieve:resume" in answer["reason"]
    # Codex refuses output fields it does not know; Claude hides the typed command.
    assert ("hookSpecificOutput" in answer) is (client == "claude")
    turn(path, "Private", client)
    run("UserPromptSubmit")
    run()
    assert capture.saving_status(root, client, SESSION)["conversation_paused"] is True
    answer = command("resume", client, path, cfg, root)
    assert answer["decision"] == "block" and "saving is back on" in answer["reason"]
    assert "conversation_paused" not in capture.saving_status(root, client, SESSION)
    turn(path, "After", client)
    run()
    contents = [e["content"] for e in events(calls) if e["kind"] in {"user", "assistant"}]
    assert contents == ["Before", "Before answer", "Tail", "Tail answer", "After", "After answer"]
    assert pending(root) == 0


def test_a_conversation_never_resumed_saves_nothing_more(tmp_path, monkeypatch):
    path, cfg, root, calls, run = setup(tmp_path, monkeypatch)
    command("pause", "codex", path, cfg, root)
    turn(path, "Private", "codex")
    for hook in ("UserPromptSubmit", "Stop", "SessionEnd"):
        run(hook)
    # Another conversation's recovery pass must not read it either.
    capture.recover_sessions(
        "codex", "00000000-0000-4000-8000-000000000000", profiles(cfg, "codex"), root, "", 5
    )
    assert calls == [] and pending(root) == 0


def test_turns_queued_before_a_conversation_pause_still_upload(tmp_path, monkeypatch):
    path, cfg, root, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *a: capture.unavailable("network_error"))
    turn(path, "Queued", "codex")
    run()
    assert pending(root) == 2
    command("pause", "codex", path, cfg, root)
    monkeypatch.setattr(
        capture, "upload", lambda batch, key, *a: calls.append((batch, key)) or ACCEPTED
    )
    run()
    assert [e["content"] for e in events(calls)] == ["Queued", "Queued answer"]


def test_a_second_company_while_paused_still_stops_saving(tmp_path, monkeypatch):
    path, cfg, root, calls, run = setup(tmp_path, monkeypatch)
    turn(path, "Before", "codex")
    run()
    command("pause", "codex", path, cfg, root)
    append(path, user("Elsewhere"), hook_record(context=501), assistant("Elsewhere answer"))
    answer = command("resume", "codex", path, cfg, root)
    assert "still not saved: Pensieve stopped saving it earlier" in answer["reason"]
    turn(path, "After", "codex")
    run()
    assert [e["content"] for e in events(calls)] == ["Before", "Before answer"]


def test_resuming_a_conversation_that_was_not_paused_changes_nothing(tmp_path, monkeypatch):
    path, cfg, root, calls, run = setup(tmp_path, monkeypatch)
    append(path, user("Open"), hook_record())
    answer = command("resume", "codex", path, cfg, root)
    assert answer["reason"] == "Pensieve: saving was not paused for this conversation."
    append(path, assistant("Open answer"))
    run()
    assert [e["content"] for e in events(calls)] == ["Open", "Open answer"]


def test_pausing_twice_is_one_pause(tmp_path, monkeypatch):
    path, cfg, root, calls, run = setup(tmp_path, monkeypatch)
    for _ in range(2):
        assert "saving is paused" in command("pause", "codex", path, cfg, root)["reason"]
    turn(path, "Private", "codex")
    command("resume", "codex", path, cfg, root)
    turn(path, "After", "codex")
    run()
    assert [e["content"] for e in events(calls)] == ["After", "After answer"]


def test_a_pause_says_when_nothing_was_being_saved(tmp_path, monkeypatch):
    path, cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    append(path, user(), hook_record(generation=None), assistant())
    run()
    answer = command("pause", "codex", path, cfg, root)
    assert "It was not being saved before this either" in answer["reason"]
    assert "transcript sharing is not on for it" in answer["reason"]
    answer = command("resume", "codex", path, cfg, root)
    assert "the pause is lifted, but this conversation is still not saved" in answer["reason"]


def test_a_conversation_pause_under_a_machine_pause_reads_nothing(tmp_path, monkeypatch):
    path, cfg, root, calls, run = setup(tmp_path, monkeypatch)
    turn(path, "Before", "codex")
    flag = pause(root)
    answer = command("pause", "codex", path, cfg, root)
    assert "saving is paused on this machine" in answer["reason"]
    assert calls == [] and pending(root) == 0
    flag.unlink()
    run()
    assert calls == []  # Still paused for this conversation.


def test_a_pause_that_cannot_be_recorded_is_not_confirmed(tmp_path, monkeypatch):
    path, cfg, root, _calls, _run = setup(tmp_path, monkeypatch)
    with private_lock(root / f"codex-{SESSION}.lock"):
        # Raising leaves the prompt unblocked, so the skill's own text reaches the
        # model and says saving was not paused.
        with pytest.raises(BlockingIOError):
            command("pause", "codex", path, cfg, root)
    assert "conversation_paused" not in capture.saving_status(root, "codex", SESSION)


@pytest.mark.usefixtures("isolated_git")
def test_anchors_are_not_sent_from_a_paused_conversation(tmp_path, monkeypatch, anchor_service):
    path, cfg, root, _calls, run = setup(tmp_path, monkeypatch, client="claude")
    append(path, user(client="claude"), hook_record("claude"), assistant(client="claude"))
    run()
    repo = repository(tmp_path)
    payload = event(repo, "git commit -m x", transcript=path)
    command("pause", "claude", path, cfg, root)
    assert git_anchor.run_hook(payload, "claude", cfg, root, anchor_service["endpoint"]) == []
    assert anchor_service["requests"] == []
    command("resume", "claude", path, cfg, root)
    assert git_anchor.run_hook(payload, "claude", cfg, root, anchor_service["endpoint"]) == [
        ("commit", 200)
    ]


def test_claude_records_of_a_blocked_command_are_not_transcript_content():
    # What Claude Code 2.1.326 writes for a blocked `/pensieve:pause`.
    records = [
        {"type": "queue-operation", "operation": "enqueue", "content": "/pensieve:pause"},
        {"type": "queue-operation", "operation": "dequeue"},
        {
            "type": "system",
            "subtype": "informational",
            "isSidechain": False,
            "sessionId": SESSION,
            "content": "UserPromptExpansion operation blocked by hook:\nPensieve: saving is paused",
        },
    ]
    for record in records:
        assert capture_adapters.normalise(record, "claude", SESSION, {}) == []
