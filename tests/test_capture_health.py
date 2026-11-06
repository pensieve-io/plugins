"""Upload health, the one-time not-saving notice and local status stay content-free."""

from __future__ import annotations

import io
import json
import stat
from urllib.error import HTTPError

import capture_health
import conversation_capture as capture
import pytest
from test_conversation_capture import (
    ACCEPTED,
    KEY,
    SESSION,
    append,
    assistant,
    config,
    events,
    hook_record,
    setup,
    user,
)


def health(root, client="codex"):
    return capture_health.read_health(root, client)


def test_success_and_failures_are_counted_without_content_or_keys(tmp_path, monkeypatch):
    path, _cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    append(path, user("Private words"), hook_record(), assistant("Private answer"))
    run()
    first = health(root)
    assert first["last_outcome"] == "uploaded" and first["consecutive_failures"] == 0
    assert first["last_success_at"] == first["last_attempt_at"]
    file = root / "health-codex.json"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    monkeypatch.setattr(capture, "upload", lambda *a: capture.unavailable("http_503"))
    for _ in range(3):
        append(path, user("More private words"), hook_record(), assistant())
        run()
    failing = health(root)
    assert failing["last_outcome"] == "http_503" and failing["consecutive_failures"] == 3
    assert failing["last_success_at"] == first["last_success_at"]
    raw = file.read_text()
    assert "Private" not in raw and KEY not in raw and SESSION not in raw
    monkeypatch.setattr(capture, "upload", lambda *a: ACCEPTED)
    run()
    assert health(root)["consecutive_failures"] == 0


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(capture, "check", lambda *args: "compatible")
    response = {"raise": None}

    class Opener:
        def open(self, request, timeout):
            failure = response["raise"]
            if isinstance(failure, tuple):
                code, body = failure
                if code == 410:
                    # A 410 must name the refused segment before it erases anything.
                    body = dict(body, segment_id=json.loads(request.data)["segment_id"])
                stream = io.BytesIO(json.dumps(body).encode())
                raise HTTPError(request.full_url, code, "fixture", {}, stream)
            raise failure

    monkeypatch.setattr(capture, "build_opener", lambda *args: Opener())
    return response


@pytest.mark.parametrize(
    "failure,outcome",
    [
        ((503, {}), "http_503"),
        ((500, {}), "http_500"),
        ((403, {"reason": "service_hold"}), "service_hold"),
        ((403, {"detail": "Upload key is unavailable"}), "forbidden"),
        ((401, {}), "forbidden"),
        ((426, {}), "incompatible"),
        ((410, {"reason": "capture_disabled"}), "capture_disabled"),
        (
            (410, {"reason": "capture_disabled", "conversation_stopped": True}),
            "conversation_stopped",
        ),
        ((410, {"reason": "other"}), "http_410"),
        (OSError("offline"), "network_error"),
    ],
)
def test_every_upload_resolution_names_its_outcome(
    tmp_path, monkeypatch, transport, failure, outcome
):
    real_upload = capture.upload
    path, _cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", real_upload)
    transport["raise"] = failure
    append(path, user(), hook_record(), assistant())
    run()
    assert health(root)["last_outcome"] == outcome


def test_an_unreachable_service_check_is_a_network_error(tmp_path, monkeypatch):
    real_upload = capture.upload
    path, _cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", real_upload)
    monkeypatch.setattr(capture, "check", lambda *args: "unavailable")
    append(path, user(), hook_record(), assistant())
    run()
    assert health(root)["last_outcome"] == "network_error"


def test_a_busy_health_file_never_delays_an_upload(tmp_path):
    root = tmp_path / "spool"
    root.mkdir(mode=0o700)
    lock = capture_health.health_path(root, "codex").with_suffix(".lock")
    with capture.private_lock(lock):
        capture_health.record_health(root, "codex", "uploaded")
    assert health(root) is None


def prompt(run):
    return run("UserPromptSubmit").get("hookSpecificOutput", {}).get("additionalContext")


@pytest.mark.parametrize(
    "outcome,reason",
    [
        ({"status": "service_hold"}, "service is paused"),
        ({"status": "capture_disabled"}, "sharing is off"),
        ({"status": "capture_disabled", "conversation_stopped": True}, "stopped saving"),
    ],
)
def test_a_not_saving_notice_is_shown_once_per_conversation_and_reason(
    tmp_path, monkeypatch, outcome, reason
):
    path, _cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    assert prompt(run) is None
    monkeypatch.setattr(capture, "upload", lambda *a: outcome)
    append(path, user(), hook_record(), assistant())
    run()
    notice = prompt(run)
    assert reason in notice
    assert f"--status {SESSION}" in notice and "--client codex" in notice
    assert KEY not in notice
    assert notice.count(". ") <= 2
    append(path, user("Again"), hook_record(), assistant())
    run()
    assert prompt(run) is None


def test_repeated_upload_failures_are_noticed_once(tmp_path, monkeypatch):
    path, _cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *a: capture.unavailable("network_error"))
    append(path, user(), hook_record(), assistant())
    run()
    assert prompt(run) is None  # Two failures so far.
    notice = prompt(run)
    assert "uploads failed" in notice
    assert prompt(run) is None


def test_a_conversation_already_saved_needs_no_notice(tmp_path, monkeypatch):
    path, _cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    append(path, user(), hook_record(), assistant())
    run()
    assert prompt(run) is None


@pytest.mark.parametrize("generation", ["217e0b53-8178-4a3c-8d40-a07414144741", None])
def test_an_unproven_start_is_noticed_only_when_sharing_is_on(tmp_path, monkeypatch, generation):
    path = tmp_path / "transcript.jsonl"
    append(path, {"type": "session_meta", "payload": {"id": SESSION}})
    append(path, user(), hook_record(generation=generation), assistant())
    cfg = config(tmp_path)
    root = tmp_path / "spool"
    monkeypatch.setattr(capture, "upload", lambda *a: ACCEPTED)
    payload = {
        "session_id": SESSION,
        "hook_event_name": "UserPromptSubmit",
        "transcript_path": str(path),
    }
    result = capture.run_hook(payload, "codex", cfg, root)
    notice = result.get("hookSpecificOutput", {}).get("additionalContext")
    assert (notice is not None) is (generation is not None)
    if notice:
        assert "did not see this conversation start" in notice
        assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert capture.run_hook(payload, "codex", cfg, root) == {}
    recorded = health(root)
    assert recorded["last_outcome"] == "unproven_start"
    assert recorded["consecutive_failures"] == 0


def test_status_reports_health_and_why_saving_stopped(tmp_path, monkeypatch):
    path, _cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        capture, "upload", lambda *a: {"status": "capture_disabled", "conversation_stopped": True}
    )
    append(path, user(), hook_record(), assistant())
    run()
    status = capture.saving_status(root, "codex", SESSION)
    assert status["stopped"] is True
    assert status["blocked_reason"] == "conversation_stopped"
    assert status["health"]["last_outcome"] == "conversation_stopped"
    assert KEY not in json.dumps(status) and "Visible" not in json.dumps(status)


def test_status_of_a_saving_conversation_has_no_reason(tmp_path, monkeypatch):
    path, _cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    append(path, user(), hook_record(), assistant())
    run()
    status = capture.saving_status(root, "codex", SESSION)
    assert "blocked_reason" not in status and status["health"]["last_outcome"] == "uploaded"


def test_status_creates_nothing(tmp_path):
    root = tmp_path / "absent"
    assert capture.saving_status(root, "codex", SESSION) == {
        "status": "not_started",
        "segments": [],
        "health": None,
    }
    assert not root.exists()


def test_the_notice_reaches_hook_output(tmp_path, monkeypatch, capsys):
    path, cfg, root, _calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *a: {"status": "service_hold"})
    append(path, user(), hook_record(), assistant())
    run()
    payload = {
        "session_id": SESSION,
        "hook_event_name": "UserPromptSubmit",
        "transcript_path": str(path),
    }
    argv = ["conversation_capture.py", "--client", "codex", "--config", str(cfg)]
    monkeypatch.setattr(capture.sys, "argv", [*argv, "--state", str(root)])
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(capture.sys, "stdin", stdin)
    capture.main()
    output = json.loads(capsys.readouterr().out)
    assert "service is paused" in output["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_a_shown_notice_is_never_captured_or_mistaken_for_attribution(
    tmp_path, monkeypatch, client
):
    path, _cfg, root, calls, run = setup(tmp_path, monkeypatch, client=client)
    monkeypatch.setattr(capture, "upload", lambda *a: {"status": "service_hold"})
    append(path, user(client=client), hook_record(client), assistant(client=client))
    run()
    notice = prompt(run)
    shown = hook_record(client)
    if client == "claude":
        shown["attachment"]["content"] = [notice]
    else:
        shown["payload"]["content"][0]["text"] = notice
    monkeypatch.setattr(
        capture, "upload", lambda batch, key, *a: calls.append((batch, key)) or ACCEPTED
    )
    append(path, user("Next", client=client), shown, hook_record(client))
    append(path, assistant("Reply", client=client))
    run()
    contents = [event["content"] for event in events(calls)]
    assert contents == ["Visible question", "Visible answer", "Next", "Reply"]
    assert all("not saving" not in content for content in contents)
