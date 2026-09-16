"""Delivery receipts require host provenance and remain separate from transcripts."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import context_receipt as receipt
import pytest

SESSION = "b6b16772-4bd9-4677-a0a2-1d2194261161"
OTHER_SESSION = "b6b16772-4bd9-4677-a0a2-1d2194261162"
TOKEN = "a" * 64 + "." + "b" * 32
NEW_TOKEN = "c" * 64 + "." + "d" * 32


def marker(token: str = TOKEN) -> str:
    return f"<!-- pensieve-delivery token={token} -->"


def claude_record(token: str = TOKEN) -> dict:
    return {
        "type": "attachment",
        "sessionId": SESSION,
        "isSidechain": False,
        "attachment": {
            "type": "hook_additional_context",
            "hookName": "UserPromptSubmit",
            "hookEvent": "UserPromptSubmit",
            "content": ["Company overview. " + marker(token)],
        },
    }


def codex_record(token: str = TOKEN) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": marker(token)}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["hooks.additional_context"],
            },
        },
    }


def transcript(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_claude_receipt_ignores_newer_user_tool_and_hook_success_text(tmp_path):
    records = [
        claude_record(),
        {"type": "user", "sessionId": SESSION, "message": {"content": marker(NEW_TOKEN)}},
        {"type": "tool_result", "content": marker(NEW_TOKEN)},
        {
            **claude_record(NEW_TOKEN),
            "attachment": {
                "type": "hook_success",
                "stdout": marker(NEW_TOKEN),
            },
        },
    ]
    path = transcript(tmp_path, records)
    assert receipt.latest_receipt(str(path), SESSION, "claude") == (TOKEN, "ack")


@pytest.mark.parametrize(
    "change",
    [
        {"sessionId": OTHER_SESSION},
        {"isSidechain": True},
        {"isSidechain": None},
        {"type": "user"},
    ],
)
def test_claude_requires_own_main_conversation(tmp_path, change):
    path = transcript(tmp_path, [{**claude_record(), **change}])
    assert receipt.latest_receipt(str(path), SESSION, "claude") is None


@pytest.mark.parametrize(
    "change",
    [
        {"type": "hook_error"},
        {"hookEvent": "Stop"},
        {"hookName": "other-plugin"},
        {"hookEvent": []},
        {"content": marker()},
    ],
)
def test_claude_requires_grounding_attachment_shape(tmp_path, change):
    record = claude_record()
    record["attachment"].update(change)
    path = transcript(tmp_path, [record])
    assert receipt.latest_receipt(str(path), SESSION, "claude") is None


@pytest.mark.parametrize("session_id", [OTHER_SESSION, None, "invalid"])
def test_codex_requires_matching_file_identity(tmp_path, session_id):
    header = {"type": "session_meta", "payload": {"id": session_id}}
    path = transcript(tmp_path, [header, codex_record()])
    assert receipt.latest_receipt(str(path), SESSION, "codex") is None


def test_codex_requires_hook_kind_at_the_same_content_index(tmp_path):
    record = codex_record()
    record["payload"]["content"].append({"type": "input_text", "text": marker(NEW_TOKEN)})
    record["payload"]["internal_chat_message_metadata_passthrough"]["content_item_kinds"].append(
        "other"
    )
    forged = copy.deepcopy(record)
    forged["payload"]["role"] = "user"
    path = transcript(
        tmp_path,
        [
            {"type": "session_meta", "payload": {"id": SESSION}},
            record,
            forged,
        ],
    )
    assert receipt.latest_receipt(str(path), SESSION, "codex") == (TOKEN, "ack")
    assert receipt.latest_receipt(str(path), SESSION, "claude") is None


@pytest.mark.parametrize(
    "change",
    [
        {"role": "user"},
        {"type": "function_call_output"},
        {"internal_chat_message_metadata_passthrough": {}},
        {"internal_chat_message_metadata_passthrough": {"content_item_kinds": []}},
    ],
)
def test_codex_ignores_unaccepted_records(tmp_path, change):
    record = codex_record()
    record["payload"].update(change)
    path = transcript(tmp_path, [{"type": "session_meta", "payload": {"id": SESSION}}, record])
    assert receipt.latest_receipt(str(path), SESSION, "codex") is None


def test_newest_complete_accepted_marker_wins(tmp_path):
    path = transcript(tmp_path, [claude_record(), claude_record(NEW_TOKEN)])
    with path.open("ab") as output:
        output.write(json.dumps(claude_record()).encode())
    assert receipt.latest_receipt(str(path), SESSION, "claude") == (NEW_TOKEN, "ack")


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("reset_succeeds", [True, False])
def test_pre_compaction_marker_resets_and_new_accepted_marker_acknowledges(
    tmp_path, monkeypatch, client, reset_succeeds
):
    if client == "claude":
        header = []
        accepted = claude_record
        boundary = {
            "type": "system",
            "subtype": "compact_boundary",
            "sessionId": SESSION,
            "isSidechain": False,
        }
    else:
        header = [{"type": "session_meta", "payload": {"id": SESSION}}]
        accepted = codex_record
        boundary = {
            "type": "compacted",
            "payload": {
                "guardian_history": [codex_record(NEW_TOKEN)],
                "replacement_history": [codex_record(NEW_TOKEN)],
            },
        }
    records = [*header, accepted(), boundary]
    path = transcript(tmp_path, records)
    assert receipt.latest_receipt(str(path), SESSION, client) == (TOKEN, "reset")
    calls = []
    # SessionStart did not restore grounding. The native prompt hook can finish
    # with an empty response based on its old acknowledgement before reset runs.
    monkeypatch.setattr(receipt, "send_receipt", lambda *args: calls.append(args) or reset_succeeds)
    assert receipt.run_hook(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": SESSION,
            "transcript_path": str(path),
        },
        client,
    ) == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": receipt.fallback_primer(client, SESSION, compacted=True),
        }
    }
    assert calls == [(TOKEN, "reset", receipt.DELIVERY_ENDPOINT)]
    path = transcript(tmp_path, [*records, accepted(NEW_TOKEN)])
    assert receipt.latest_receipt(str(path), SESSION, client) == (NEW_TOKEN, "ack")
    assert (
        receipt.run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": SESSION,
                "transcript_path": str(path),
            },
            client,
        )
        == {}
    )


def test_bounded_tail_discards_partial_first_record_and_reads_codex_header(tmp_path, monkeypatch):
    header = {"type": "session_meta", "payload": {"id": SESSION}}
    path = transcript(tmp_path, [header, {"padding": "x" * 10_000}, codex_record(NEW_TOKEN)])
    monkeypatch.setattr(receipt, "MAX_TRANSCRIPT_BYTES", 1000)
    assert receipt.latest_receipt(str(path), SESSION, "codex") == (NEW_TOKEN, "ack")
    path = transcript(tmp_path, [claude_record(), {"padding": "x" * 10_000}])
    assert receipt.latest_receipt(str(path), SESSION, "claude") is None


def test_missing_partial_or_nonregular_transcript_yields_no_receipt(tmp_path):
    assert receipt.latest_receipt(None, SESSION, "codex") is None
    assert receipt.latest_receipt("relative/path", SESSION, "claude") is None
    assert receipt.latest_receipt(str(tmp_path), SESSION, "claude") is None
    assert receipt.latest_receipt(str(tmp_path / "missing"), SESSION, "claude") is None
    path = transcript(tmp_path, [claude_record()])
    link = tmp_path / "link.jsonl"
    link.symlink_to(path)
    assert receipt.latest_receipt(str(link), SESSION, "claude") is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.invalid/hooks/delivery",
        "http://mcp.pensieve.uk/hooks/delivery",
        "https://mcp.pensieve.uk/hooks/delivery?redirect=1",
        "http://127.0.0.1:12/other",
        "http://127.0.0.1:12/hooks/delivery?next=evil",
        "http://user@localhost/hooks/delivery",
        "http://localhost:99999/hooks/delivery",
        "file:///hooks/delivery",
    ],
)
def test_endpoint_cannot_be_redirected_by_configuration(endpoint):
    with pytest.raises(ValueError):
        receipt.checked_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        receipt.DELIVERY_ENDPOINT,
        "http://127.0.0.1:8123/hooks/delivery",
        "http://localhost:8123/hooks/delivery",
        "http://[::1]:8123/hooks/delivery",
    ],
)
def test_fixed_endpoint_and_explicit_loopback_fixture_are_supported(endpoint):
    assert receipt.checked_endpoint(endpoint) == endpoint


def test_receipt_sends_only_token_and_operation_with_timeout_and_no_redirects(monkeypatch):
    calls = []

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return Response()

    def opener(*handlers):
        assert isinstance(handlers[0], receipt.ProxyHandler)
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], receipt.NoRedirects)
        return Opener()

    monkeypatch.setattr(receipt, "build_opener", opener)
    assert receipt.send_receipt(TOKEN, "ack")
    request, timeout = calls[0]
    assert request.full_url == receipt.DELIVERY_ENDPOINT
    assert json.loads(request.data) == {"token": TOKEN, "operation": "ack"}
    assert request.get_method() == "POST"
    assert request.get_header("User-agent") == receipt.USER_AGENT
    assert not request.get_header("User-agent").startswith("Python-urllib")
    assert timeout == 2
    assert (
        receipt.NoRedirects().redirect_request(None, None, 302, None, None, "https://evil.invalid")
        is None
    )


@pytest.mark.parametrize(
    "failure, reason",
    [
        (lambda: receipt.HTTPError("url", 403, "Forbidden", {}, None), "HTTP 403"),
        (lambda: receipt.URLError("timed out"), "URLError"),
        (lambda: ConnectionResetError(), "ConnectionResetError"),
    ],
    ids=["cloudflare-403", "url-error", "connection-reset"],
)
def test_unaccepted_receipt_reports_only_the_failure_class_on_stderr(
    monkeypatch, capsys, failure, reason
):
    class Opener:
        def open(self, request, timeout):
            raise failure()

    monkeypatch.setattr(receipt, "build_opener", lambda *handlers: Opener())
    assert receipt.send_receipt(TOKEN, "ack") is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"pensieve receipt not accepted ({reason}); briefing may repeat\n"
    assert TOKEN not in captured.err
    assert receipt.DELIVERY_ENDPOINT not in captured.err


def test_accepted_receipt_writes_nothing(monkeypatch, capsys):
    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(receipt, "build_opener", lambda *handlers: Opener())
    assert receipt.send_receipt(TOKEN, "ack") is True
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "event, operation", [("SessionStart", "reset"), ("UserPromptSubmit", "ack"), ("Stop", "ack")]
)
def test_hook_operation_and_session_scope(tmp_path, monkeypatch, event, operation):
    path = transcript(tmp_path, [claude_record()])
    calls = []
    monkeypatch.setattr(receipt, "send_receipt", lambda *args: calls.append(args) or True)
    payload = {
        "hook_event_name": event,
        "session_id": SESSION,
        "transcript_path": str(path),
        "endpoint": "https://attacker.invalid",
        "prompt": "never uploaded",
    }
    expected = (
        {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": receipt.fallback_primer("claude", SESSION),
            }
        }
        if event == "SessionStart"
        else {}
    )
    assert receipt.run_hook(payload, "claude") == expected
    assert calls == [(TOKEN, operation, receipt.DELIVERY_ENDPOINT)]


def test_unavailable_receipt_fails_open_with_only_startup_primer(tmp_path, monkeypatch):
    path = transcript(tmp_path, [claude_record()])
    monkeypatch.setattr(receipt, "send_receipt", lambda *_args: False)
    payload = {"session_id": SESSION, "transcript_path": str(path)}
    assert receipt.run_hook({**payload, "hook_event_name": "UserPromptSubmit"}, "claude") == {}
    assert receipt.run_hook({**payload, "hook_event_name": "SessionStart"}, "claude") == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": receipt.fallback_primer("claude", SESSION),
        },
    }
    assert receipt.run_hook({**payload, "hook_event_name": []}, "claude") == {}


def test_cli_uses_standard_library_and_handles_missing_transcript():
    result = subprocess.run(
        [sys.executable, "-S", receipt.__file__, "--client", "codex"],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": SESSION}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": receipt.fallback_primer("codex", SESSION),
        }
    }
    assert result.stderr == ""


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_successful_session_reset_still_requires_current_conversation_binding(
    tmp_path, monkeypatch, client
):
    accepted = claude_record() if client == "claude" else codex_record()
    header = [] if client == "claude" else [{"type": "session_meta", "payload": {"id": SESSION}}]
    path = transcript(tmp_path, [*header, accepted])
    monkeypatch.setattr(receipt, "send_receipt", lambda *_args: True)
    result = receipt.run_hook(
        {
            "hook_event_name": "SessionStart",
            "source": "clear",
            "session_id": SESSION,
            "transcript_path": str(path),
        },
        client,
    )
    text = result["hookSpecificOutput"]["additionalContext"]
    binding = f'context_briefing(client="{client}", session_id="{SESSION}", event="SessionStart")'
    assert binding in text
    assert text.index(binding) < text.index("list_contexts") < text.index("set_context")
    assert "Only after that succeeds" in text
    assert "do not use Pensieve company tools" in text


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_missing_current_transcript_never_reuses_previous_conversation_selection(tmp_path, event):
    path = transcript(tmp_path, [claude_record()])
    result = receipt.run_hook(
        {"hook_event_name": event, "session_id": OTHER_SESSION, "transcript_path": str(path)},
        "claude",
    )
    text = result["hookSpecificOutput"]["additionalContext"]
    assert OTHER_SESSION in text
    assert SESSION not in text
    assert "context_briefing(" in text
    assert (
        receipt.run_hook(
            {"hook_event_name": "Stop", "session_id": OTHER_SESSION, "transcript_path": str(path)},
            "claude",
        )
        == {}
    )


@pytest.mark.parametrize(
    "session",
    [None, "invalid", 'bad", event="SessionStart")', "{b6b16772-4bd9-4677-a0a2-1d2194261161}"],
)
def test_fallback_never_interpolates_unvalidated_conversation_identity(session):
    text = receipt.fallback_primer("claude", session)
    assert "context_briefing(" not in text
    assert "could not identify this conversation" in text


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("receipt_failure", ["missing_file", "bounded_tail", "failed_reset"])
def test_compaction_recovery_requests_a_forced_server_briefing(
    tmp_path, monkeypatch, client, receipt_failure
):
    header = [] if client == "claude" else [{"type": "session_meta", "payload": {"id": SESSION}}]
    accepted = claude_record() if client == "claude" else codex_record()
    boundary = (
        {
            "type": "system",
            "subtype": "compact_boundary",
            "sessionId": SESSION,
            "isSidechain": False,
        }
        if client == "claude"
        else {"type": "compacted"}
    )
    records = [*header, accepted]
    if receipt_failure == "bounded_tail":
        monkeypatch.setattr(receipt, "MAX_TRANSCRIPT_BYTES", 512)
        records.append({"type": "user", "content": "x" * 2000})
    path = tmp_path / "transcript.jsonl"
    if receipt_failure != "missing_file":
        transcript(tmp_path, [*records, boundary])
    monkeypatch.setattr(receipt, "send_receipt", lambda *_args: False)
    fallback = receipt.run_hook(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": SESSION,
            "transcript_path": str(path),
        },
        client,
    )["hookSpecificOutput"]["additionalContext"]
    # SessionStart forces delivery even when the server still holds the old ACK.
    assert re.search(r'event="([^"]+)"', fallback).group(1) == "SessionStart"
    assert f'session_id="{SESSION}"' in fallback
