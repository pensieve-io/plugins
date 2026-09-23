"""Capture requires fresh, correctly scoped provenance and durable acknowledgement."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

import conversation_capture as capture
import pytest

SESSION = "923bbd76-d864-4b8f-b252-c2b7c3692492"
OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
OTHER_OWNER = "173e0b53-8178-4a3c-8d40-a07414144741"
KEY = "synthetic-upload-key-owner-one"
OTHER_KEY = "synthetic-upload-key-owner-two"
NOW = "2026-09-15T10:00:00+00:00"
GENERATION = "217e0b53-8178-4a3c-8d40-a07414144741"
EXPIRY = "2026-12-14T10:00:00+00:00"
ACCEPTED = {"status": "accepted", "expires_at": None}
DELETED = {"status": "deleted"}


def marker(
    client="codex",
    owner=OWNER,
    context=497,
    kind="prompt",
    turn=None,
    session=SESSION,
    generation=GENERATION,
):
    value = {
        "v": 2,
        "capture_generation": generation,
        "kind": kind,
        "user_id": owner,
        "context_id": context,
        "client": client,
        "conversation_id": session,
        "turn_id": turn,
    }
    return "<!-- pensieve-capture-context " + json.dumps(value) + " -->"


def hook_record(client="codex", **kwargs):
    content = marker(client=client, **kwargs)
    if client == "claude":
        return {
            "type": "attachment",
            "sessionId": SESSION,
            "isSidechain": False,
            "attachment": {
                "type": "hook_additional_context",
                "hookName": "UserPromptSubmit",
                "hookEvent": "UserPromptSubmit",
                "content": [content],
            },
        }
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": content}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["hooks.additional_context"]
            },
        },
    }


def user(text="Visible question", client="codex"):
    if client == "claude":
        return {
            "type": "user",
            "sessionId": SESSION,
            "isSidechain": False,
            "uuid": str(uuid.uuid4()),
            "timestamp": NOW,
            "message": {"role": "user", "content": text},
        }
    return {
        "type": "event_msg",
        "timestamp": NOW,
        "payload": {"type": "user_message", "message": text},
    }


def assistant(text="Visible answer", client="codex"):
    payload = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text" if client == "codex" else "text", "text": text}],
    }
    if client == "claude":
        return {
            "type": "assistant",
            "sessionId": SESSION,
            "isSidechain": False,
            "uuid": str(uuid.uuid4()),
            "timestamp": NOW,
            "message": payload,
        }
    return {"type": "response_item", "timestamp": NOW, "payload": payload}


def append(path, *records):
    with path.open("ab") as handle:
        for record in records:
            handle.write(json.dumps(record).encode() + b"\n")


def config(tmp_path, profiles=None):
    path = tmp_path / "capture.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "profiles": profiles
                if profiles is not None
                else [{"user_id": OWNER, "upload_key": KEY}],
            }
        )
    )
    path.chmod(0o600)
    return path


def setup(tmp_path, monkeypatch, client="codex", prior=(), profiles=None):
    path = tmp_path / "transcript.jsonl"
    path.touch()
    if client == "codex":
        append(path, {"type": "session_meta", "payload": {"id": SESSION}})
    append(path, *prior)
    cfg = config(tmp_path, profiles)
    state = tmp_path / "spool"
    calls = []

    def send(batch, key, endpoint, timeout):
        calls.append((dict(batch), key))
        return ACCEPTED

    monkeypatch.setattr(capture, "upload", send)

    def run(event="Stop"):
        return capture.run_hook(
            {"session_id": SESSION, "hook_event_name": event, "transcript_path": str(path)},
            client,
            cfg,
            state,
        )

    run("SessionStart")
    return path, cfg, state, calls, run


def events(calls):
    return [event for batch, _key in calls for event in json.loads(batch["body"])["events"]]


def pending(state):
    db = sqlite3.connect(next(state.glob("*.sqlite3")))
    try:
        return db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        db.close()


def codex_lifecycle(kind, turn="native-turn", ordinal=None):
    return {
        "type": "event_msg",
        "timestamp": NOW,
        "ordinal": ordinal,
        "payload": {"type": kind, "turn_id": turn},
    }


def claude_stop(**changes):
    return {
        "type": "system",
        "subtype": "stop_hook_summary",
        "sessionId": SESSION,
        "isSidechain": False,
        "timestamp": NOW,
        "stopReason": "",
        "preventedContinuation": False,
        "hookErrors": [],
        **changes,
    }


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_native_turns_link_after_ack_and_resume(tmp_path, monkeypatch, client):
    path, _, _, calls, run = setup(tmp_path, monkeypatch, client)
    prompt = user(client=client)
    if client == "codex":
        append(path, codex_lifecycle("task_started"))
    append(path, prompt, hook_record(client), assistant(client=client))
    run()
    first = events(calls)
    turn = "native-turn" if client == "codex" else prompt["uuid"]
    assert first[0]["capture"] == {"turn_id": turn}
    assert first[1]["capture"] == {
        "turn_id": turn,
        "parent": {"host_conversation_id": SESSION, "event_id": first[0]["event_id"]},
    }
    run("SessionEnd")
    run("SessionStart")
    if client == "codex":
        append(path, codex_lifecycle("task_started", "second-turn"))
    append(path, user("Second", client), hook_record(client), assistant("Second answer", client))
    run()
    saved = events(calls)
    assert len(saved) == 4
    assert saved[2]["capture"]["turn_id"] != turn
    assert saved[2]["capture"]["parent"]["event_id"] == first[-1]["event_id"]
    assert saved[3]["capture"]["turn_id"] == saved[2]["capture"]["turn_id"]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_tool_identity_and_terminal_event_are_retry_stable(tmp_path, monkeypatch, client):
    path, _, _, calls, run = setup(tmp_path, monkeypatch, client)
    if client == "codex":
        append(path, codex_lifecycle("task_started"))
        call = {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "read", "call_id": "native-call"},
        }
        result = {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "native-call",
                "output": "Result",
            },
        }
        end = codex_lifecycle("task_complete")
    else:
        call = assistant(client=client)
        call["message"]["content"] = [{"type": "tool_use", "id": "native-call", "name": "read"}]
        result = user(client=client)
        result["message"]["content"] = [
            {"type": "tool_result", "tool_use_id": "native-call", "content": "Result"}
        ]
        end = claude_stop()
    answer = assistant(client=client)
    if client == "claude":
        answer["message"]["stop_reason"] = "end_turn"
    append(path, user(client=client), hook_record(client), call, result, answer)
    run()
    # A Stop invocation alone does not certify completion, even at EOF.
    assert "turn_end" not in {event["kind"] for event in events(calls)}
    append(path, end)
    run("SessionEnd")
    saved = events(calls)
    assert [event["kind"] for event in saved] == [
        "user",
        "tool_call",
        "tool_result",
        "assistant",
        "turn_end",
    ]
    assert {event["capture"]["tool_call_id"] for event in saved[1:3]} == {"native-call"}
    assert saved[-1]["content"] == ""
    assert saved[-1]["capture"]["completion"] == "completed"
    assert saved[-1]["capture"]["parent"]["event_id"] == saved[-2]["event_id"]
    assert len({event["capture"]["turn_id"] for event in saved}) == 1
    append(path, end)
    run()
    assert events(calls) == saved


@pytest.mark.parametrize("client,freeform", [("codex", False), ("codex", True), ("claude", False)])
@pytest.mark.parametrize("large", [False, True])
def test_tool_inputs_are_captured_redacted_bounded_and_retry_stable(
    tmp_path, monkeypatch, client, freeform, large
):
    path, _, _, calls, run = setup(tmp_path, monkeypatch, client)
    arguments = {
        "query": "Find onboarding evidence 👋",
        "password": "synthetic-password",
        "configured_credential": KEY,
        "notes": "x" * (40000 if large else 3),
    }
    if client == "codex":
        call = {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call" if freeform else "function_call",
                "name": "search",
                "call_id": "native-input-call",
                "input" if freeform else "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        }
    else:
        call = assistant(client=client)
        call["message"]["content"] = [
            {
                "type": "tool_use",
                "id": "native-input-call",
                "name": "search",
                "input": arguments,
            }
        ]
    append(path, user(client=client), hook_record(client), call)
    run()
    captured = [e for e in events(calls) if e["kind"] == "tool_call"]
    assert len(captured) == 1
    saved = captured[0]
    assert saved["capture"]["tool_name"] == "search"
    assert saved["capture"]["tool_call_id"] == "native-input-call"
    assert "Find onboarding evidence 👋" in saved["content"]
    assert "synthetic-password" not in saved["content"] and KEY not in saved["content"]
    assert "[REDACTED_SECRET]" in saved["content"]
    assert len(saved["content"]) <= capture.MAX_EVENT_CHARS
    assert saved["truncated"] is large
    assert "query" not in saved["capture"]
    before = json.dumps(events(calls), sort_keys=True)
    run()
    assert json.dumps(events(calls), sort_keys=True) == before


@pytest.mark.parametrize(
    "value", ["", {}, [], 0, False, None, "*** Begin Patch\nhello\n*** End Patch"]
)
def test_tool_input_preserves_empty_scalar_and_freeform_values(value):
    call = capture.tool_call("apply_patch", "c", {"input": value}, "input")
    assert call["tool_name"] == "apply_patch"
    assert call["content"] == (value if isinstance(value, str) else json.dumps(value, indent=2))
    assert "tool_name" not in capture.tool_call("read", "c", {}, "input")


@pytest.mark.parametrize(
    "native_kind,expected", [("turn_aborted", "interrupted"), ("task_complete", "completed")]
)
def test_codex_completion_requires_current_native_turn(
    tmp_path, monkeypatch, native_kind, expected
):
    path, _, _, calls, run = setup(tmp_path, monkeypatch)
    append(
        path,
        codex_lifecycle("task_started"),
        user(),
        hook_record(),
        codex_lifecycle(native_kind, "different-turn"),
    )
    run()
    assert len(events(calls)) == 1
    append(path, codex_lifecycle(native_kind))
    run()
    assert events(calls)[-1]["capture"]["completion"] == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"stopReason": "continue"},
        {"preventedContinuation": True},
        {"hookErrors": ["failed"]},
        {"isSidechain": True},
    ],
)
def test_claude_unsettled_stop_never_marks_complete(tmp_path, monkeypatch, changes):
    path, _, _, calls, run = setup(tmp_path, monkeypatch, "claude")
    answer = assistant(client="claude")
    answer["message"]["stop_reason"] = "end_turn"
    append(path, user(client="claude"), hook_record("claude"), answer, claude_stop(**changes))
    run("SessionEnd")
    assert [event["kind"] for event in events(calls)] == ["user", "assistant"]


def test_legacy_turn_ids_are_distinct_and_malformed_native_ids_are_omitted(tmp_path, monkeypatch):
    path, _, _, calls, run = setup(tmp_path, monkeypatch)
    for turn in ("bad id", "x" * 256, None):
        append(path, codex_lifecycle("task_started", turn), user(), hook_record(), assistant())
        run()
    saved = events(calls)
    assert len({event["capture"]["turn_id"] for event in saved}) == 3
    assert all(
        event["capture"]["turn_id"] == saved[i // 2 * 2]["event_id"]
        for i, event in enumerate(saved)
    )


@pytest.mark.parametrize("boundary", ["context", "generation", "unmarked", "rollback"])
def test_lineage_stops_at_unproven_or_changed_scope(tmp_path, monkeypatch, boundary):
    path, _, _, calls, run = setup(tmp_path, monkeypatch)
    append(path, user(), hook_record(), assistant())
    run()
    args = {}
    if boundary == "context":
        args["context"] = 12
    elif boundary == "generation":
        args["generation"] = OTHER_OWNER
    elif boundary == "unmarked":
        append(path, user("Unmarked"), assistant("Unattributed"))
    else:
        append(path, codex_lifecycle("thread_rolled_back"))
    append(path, user("New"), hook_record(**args), assistant("New answer"))
    run()
    saved = events(calls)
    assert "parent" not in saved[-2]["capture"]
    assert saved[-1]["capture"]["parent"]["event_id"] == saved[-2]["event_id"]


def test_upgrade_keeps_legacy_outbox_exact_and_does_not_enrich_old_turn(tmp_path, monkeypatch):
    path, _, state, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    append(path, user(), hook_record(), assistant())
    run()
    db = capture.connect_state(state, "codex", SESSION)
    with db:
        for row in db.execute("SELECT id,body FROM events").fetchall():
            event = json.loads(row["body"])
            event.pop("capture")
            db.execute(
                "UPDATE events SET body=? WHERE id=?", (capture.encoded(event).decode(), row["id"])
            )
        db.execute("DELETE FROM batches")
        st = capture.load_state(db)
        for key in ("capture_turn_id", "tail", "turn_closed", "claude_end_turn"):
            st.pop(key, None)
        capture.save_state(db, st)
    old_batch = capture.next_batch(
        db, db.execute("SELECT * FROM segments").fetchone(), "codex", SESSION
    )
    db.execute("DROP TABLE anchors")  # Previous plugin schema.
    db.commit()
    db.close()

    def send(batch, *args):
        calls.append((batch, KEY))
        return ACCEPTED

    monkeypatch.setattr(capture, "upload", send)
    append(path, assistant("Old turn continues"))
    run()
    assert calls[0][0]["body"] == old_batch["body"]
    assert calls[0][0]["sha"] == old_batch["sha"]
    assert all("capture" not in event for event in events(calls))
    append(path, user("New turn"), hook_record(), assistant("New answer"))
    run()
    assert "parent" not in events(calls)[-2]["capture"]


@pytest.mark.parametrize(
    "scope,endpoint",
    [
        ("same", 4),
        ("same", 6),
        ("same", 5),
        ("other", 4),
        ("legacy_deadline", 4),
        ("deleted", 4),
        ("missing", 4),
    ],
)
def test_codex_fork_uses_exact_captured_endpoint_only(tmp_path, monkeypatch, scope, endpoint):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    answer = assistant()
    answer["ordinal"] = 3
    append(
        path,
        codex_lifecycle("task_started"),
        user(),
        hook_record(),
        answer,
        codex_lifecycle("task_complete", ordinal=5),
    )
    run()
    source_events = events(calls)
    if scope in {"legacy_deadline", "deleted", "missing"}:
        db = capture.connect_state(state, "codex", SESSION)
        with db:
            if scope == "legacy_deadline":
                db.execute("UPDATE segments SET expires_at='2020-01-01T00:00:00Z'")
            elif scope == "deleted":
                db.execute("UPDATE segments SET retired=1")
            else:
                db.execute("DELETE FROM anchors")
        db.close()
    fork = str(uuid.uuid4())
    fork_path = tmp_path / "fork.jsonl"
    append(
        fork_path,
        {
            "type": "session_meta",
            "payload": {
                "id": fork,
                "forked_from_id": SESSION,
                "forked_from_ordinal_exclusive": endpoint,
            },
        },
    )

    def fork_run(event):
        capture.run_hook(
            {"session_id": fork, "hook_event_name": event, "transcript_path": str(fork_path)},
            "codex",
            cfg,
            state,
        )

    fork_run("SessionStart")
    append(
        fork_path,
        user("Fork prompt"),
        hook_record(session=fork, context=12 if scope == "other" else 497),
        assistant("Fork answer"),
    )
    fork_run("Stop")
    first = events(calls)[-2]
    if scope in {"same", "legacy_deadline"} and endpoint in {4, 6}:
        parent = source_events[1 if endpoint == 4 else 2]
        assert first["capture"]["parent"] == {
            "host_conversation_id": SESSION,
            "event_id": parent["event_id"],
        }
    else:
        assert "parent" not in first["capture"]
    # The source remains independently resumable and is never relinked to the fork.
    append(path, user("Original continues"), hook_record(), assistant("Original answer"))
    run()
    resumed = events(calls)[-2]["capture"]
    if scope == "deleted":
        assert "parent" not in resumed
    else:
        assert resumed["parent"]["host_conversation_id"] == SESSION


def test_hosted_receipt_latency_does_not_stall_later_turns(tmp_path, monkeypatch):
    real_upload = capture.upload
    path, cfg, state, _, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", real_upload)
    accepted = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            # The production edge rejects urllib's generic default agent.
            if self.headers.get("User-Agent") != "Pensieve-Plugin-Capture/1.0":
                self.send_response(403)
                self.end_headers()
                return
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            batch = json.loads(raw)
            accepted.append(batch)
            # Hosted receipts can take longer than the former 650 ms cap,
            # while still fitting comfortably within an ordinary hook.
            time.sleep(0.9)
            body = json.dumps(
                {
                    "batch_id": batch["batch_id"],
                    "batch_sha256": hashlib.sha256(raw).hexdigest(),
                    "conversation_id": SESSION,
                    "segment_id": batch["segment_id"],
                    "accepted_events": len(batch["events"]),
                    "expires_at": None,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for turn in ("first", "resumed"):
            append(path, user(turn), hook_record(), assistant(turn))
            capture.run_hook(
                {"session_id": SESSION, "hook_event_name": "Stop", "transcript_path": str(path)},
                "codex",
                cfg,
                state,
                f"http://127.0.0.1:{server.server_port}/hooks/conversations",
            )
            assert pending(state) == 0
        assert len(accepted) == 2
        assert [event["content"] for batch in accepted for event in batch["events"]] == [
            "first",
            "first",
            "resumed",
            "resumed",
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_capture_off_does_not_read_transcript_or_create_state(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "scan", lambda *args: pytest.fail("capture off read transcript"))
    capture.run_hook(
        {"session_id": SESSION, "hook_event_name": "Stop", "transcript_path": "/does-not-exist"},
        "codex",
        tmp_path / "absent",
        tmp_path / "spool",
    )
    assert not (tmp_path / "spool").exists()


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666, 0o400])
def test_config_requires_exact_private_permissions(tmp_path, mode):
    path = config(tmp_path)
    path.chmod(mode)
    with pytest.raises(ValueError, match="0600"):
        capture.profiles(path)


def test_config_and_transcript_symlinks_rejected(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    link = tmp_path / "config-link"
    link.symlink_to(cfg)
    with pytest.raises(OSError):
        capture.profiles(link)
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    real = tmp_path / "real"
    path.rename(real)
    path.symlink_to(real)
    with pytest.raises(OSError):
        run()
    assert not calls


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_checkpoint_and_resume_do_not_duplicate_or_backfill(tmp_path, monkeypatch, client):
    path, cfg, state, calls, run = setup(
        tmp_path,
        monkeypatch,
        client,
        prior=[
            user("Old unconsented history", client),
            hook_record(client),
            assistant("Old answer", client),
        ],
    )
    append(path, user(client=client), hook_record(client), assistant(client=client))
    run()
    assert [event["content"] for event in events(calls)] == ["Visible question", "Visible answer"]
    assert pending(state) == 0
    ids = [event["event_id"] for event in events(calls)]
    assert len(set(ids)) == 2
    count = len(calls)
    run("SessionEnd")
    run("SessionStart")
    run()
    assert len(calls) == count  # No empty presence updates.
    append(
        path,
        user("Resumed prompt", client),
        hook_record(client),
        assistant("Resumed answer", client),
    )
    run("SessionEnd")
    assert {json.loads(batch["body"])["segment_id"] for batch, _ in calls} == {
        json.loads(calls[0][0]["body"])["segment_id"]
    }
    assert [event["event_id"] for event in events(calls)][:2] == ids
    assert len(events(calls)) == 4


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_new_unmarked_turn_never_reuses_previous_owner(tmp_path, monkeypatch, client):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    append(path, user("One", client), hook_record(client), assistant("One answer", client))
    run()
    calls.clear()
    append(
        path, user("Other account's question", client), assistant("Other account's answer", client)
    )
    run()
    assert events(calls) == []
    assert pending(state) == 0
    append(
        path, hook_record(client)
    )  # late marker cannot retroactively repair the missing boundary
    run()
    assert events(calls) == []
    append(path, user("Three", client), hook_record(client), assistant("Three answer", client))
    run()
    assert [event["content"] for event in events(calls)] == ["Three", "Three answer"]
    assert pending(state) == 0


def test_codex_turn_identity_must_match_prompt_marker(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(
        path,
        {"type": "turn_context", "payload": {"turn_id": "current"}},
        user(),
        hook_record(turn="previous"),
        assistant(),
    )
    run()
    assert not calls and pending(state) == 0


def test_account_and_context_switches_use_only_matching_configured_keys(tmp_path, monkeypatch):
    profiles = [
        {"user_id": OWNER, "upload_key": KEY},
        {"user_id": OTHER_OWNER, "upload_key": OTHER_KEY},
    ]
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, profiles=profiles)
    append(path, user("Owner one"), hook_record(), assistant("First answer"))
    append(
        path,
        user("Owner two"),
        hook_record(owner=OTHER_OWNER, context=12),
        assistant("Second answer"),
    )
    run()
    by_key = {key: json.loads(batch["body"]) for batch, key in calls}
    assert [event["content"] for event in by_key[KEY]["events"]] == ["Owner one", "First answer"]
    assert [event["content"] for event in by_key[OTHER_KEY]["events"]] == [
        "Owner two",
        "Second answer",
    ]
    assert by_key[KEY]["segment_id"] != by_key[OTHER_KEY]["segment_id"]


def test_unconfigured_account_and_null_selection_are_capture_off(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(
        path,
        user("Other scope"),
        hook_record(owner=OTHER_OWNER, context=12),
        assistant("Other scope answer"),
    )
    append(path, user("No context"), hook_record(context=None), assistant("No context answer"))
    run()
    assert not calls and pending(state) == 0


@pytest.mark.parametrize("generation", [GENERATION, "317e0b53-8178-4a3c-8d40-a07414144741"])
def test_set_context_output_is_assigned_to_new_context_before_result_capture(
    tmp_path, monkeypatch, generation
):
    profiles = [{"user_id": OWNER, "upload_key": KEY}]
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, profiles=profiles)
    append(
        path,
        user(),
        hook_record(),
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "set_context",
                "namespace": "mcp__pensieve",
                "call_id": "switch",
                "arguments": "{}",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "switch",
                "output": "Second company overview\n"
                + marker(context=12, kind="selection", generation=generation),
            },
        },
        assistant("Second company answer"),
    )
    run()
    by_key = {
        json.loads(batch["body"])["context_id"]: json.loads(batch["body"]) for batch, key in calls
    }
    assert [event["kind"] for event in by_key[497]["events"]] == ["user", "tool_call"]
    assert [event["kind"] for event in by_key[12]["events"]] == ["tool_result", "assistant"]
    assert "Second company overview" in by_key[12]["events"][0]["content"]
    assert "pensieve-capture-context" not in str(events(calls))


def test_quoted_markers_and_non_pensieve_tool_results_do_not_authorize(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(
        path,
        user(marker()),
        assistant(marker()),
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "read", "call_id": "other"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "other",
                "output": marker(kind="selection"),
            },
        },
    )
    run()
    assert not calls


def test_hidden_reasoning_instructions_and_compaction_history_are_never_captured(
    tmp_path, monkeypatch
):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(
        path,
        user(),
        hook_record(),
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [{"text": "HIDDEN_THOUGHT"}],
                "encrypted_content": "SECRET_CIPHER",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "HARNESS_USER_INSTRUCTIONS"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "DEVELOPER_INSTRUCTIONS"}],
            },
        },
        {"type": "compacted", "payload": {"guardian_history": [assistant("OLD_HIDDEN")]}},
        assistant(),
    )
    run()
    assert [event["content"] for event in events(calls)] == ["Visible question", "Visible answer"]


def test_claude_mixed_thinking_and_text_only_keeps_visible_text(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, "claude")
    record = assistant(client="claude")
    record["message"]["content"].append({"type": "thinking", "thinking": "hidden thought"})
    append(path, user(client="claude"), hook_record("claude"), record)
    run()
    assert [event["content"] for event in events(calls)] == ["Visible question", "Visible answer"]


def test_partial_record_retries_and_identical_messages_keep_distinct_ids(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(path, user(), hook_record())
    raw = json.dumps(assistant("Same answer")).encode()
    with path.open("ab") as output:
        output.write(raw[:50])
    run()
    assert [event["kind"] for event in events(calls)] == ["user"]
    with path.open("ab") as output:
        output.write(raw[50:] + b"\n" + raw + b"\n")
    run()
    answers = [event for event in events(calls) if event["kind"] == "assistant"]
    assert len(answers) == 2 and answers[0]["event_id"] != answers[1]["event_id"]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_oversized_record_recovery_is_bounded_and_requires_fresh_attribution(
    tmp_path, monkeypatch, client
):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    monkeypatch.setattr(capture, "MAX_RECORD_BYTES", 4096)
    monkeypatch.setattr(capture, "MAX_SCAN_BYTES", 8192)
    append(
        path,
        user("Before", client),
        hook_record(client),
        assistant("Before answer", client),
        assistant("OVERSIZED_PRIVATE_TEXT" * 2000, client),
        hook_record(client),  # A late receipt cannot repair the skipped turn.
        assistant("Uncertain old answer", client),
        user("Unmarked prompt", client),
        assistant("Unmarked answer", client),
        hook_record(client),
        user("Fresh prompt", client),
        hook_record(client, context=12),
        assistant("Fresh answer", client),
    )
    db = capture.connect_state(state, client, SESSION)
    previous = capture.load_state(db)["offset"]
    try:
        for _ in range(10):
            run()
            checkpoint = capture.load_state(db)
            assert 0 < checkpoint["offset"] - previous <= capture.MAX_SCAN_BYTES
            previous = checkpoint["offset"]
            if previous == path.stat().st_size:
                break
        else:
            pytest.fail("Oversized record prevented later capture")
    finally:
        db.close()
    by_context = {}
    for batch, _ in calls:
        body = json.loads(batch["body"])
        by_context.setdefault(body["context_id"], []).extend(
            event["content"] for event in body["events"]
        )
    assert by_context == {497: ["Before", "Before answer"], 12: ["Fresh prompt", "Fresh answer"]}
    assert pending(state) == 0
    count = len(calls)
    run("SessionStart")
    run()
    assert len(calls) == count


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_oversized_partial_record_waits_for_its_newline_without_retaining_unmarked_work(
    tmp_path, monkeypatch, client
):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    monkeypatch.setattr(capture, "MAX_RECORD_BYTES", 4096)
    append(path, user("Unmarked old prompt", client))
    raw = json.dumps(assistant("PRIVATE" * 1000, client)).encode()
    with path.open("ab") as output:
        output.write(raw[:5000])
    run()
    assert not calls and pending(state) == 0
    run()  # EOF does not end a record that the host is still writing.
    with path.open("ab") as output:
        output.write(raw[5000:] + b"\n")
    append(
        path,
        hook_record(client),
        assistant("Old answer", client),
        user("Fresh prompt", client),
        hook_record(client),
        assistant("Fresh answer", client),
    )
    run()
    assert [event["content"] for event in events(calls)] == ["Fresh prompt", "Fresh answer"]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_oversized_record_does_not_change_pending_retry_bytes(tmp_path, monkeypatch, client):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    append(
        path, user("Queued prompt", client), hook_record(client), assistant("Queued answer", client)
    )
    monkeypatch.setattr(
        capture,
        "upload",
        lambda batch, key, *args: calls.append((dict(batch), key)) or False,
    )
    run()
    original = calls.pop()[0]
    monkeypatch.setattr(
        capture,
        "upload",
        lambda batch, key, *args: calls.append((dict(batch), key)) or ACCEPTED,
    )
    append(
        path,
        assistant("x" * capture.MAX_RECORD_BYTES, client),
        assistant("Uncertain answer", client),
        user("Fresh prompt", client),
        hook_record(client),
        assistant("Fresh answer", client),
    )
    run()
    assert calls[0][0]["body"] == original["body"]
    assert [event["content"] for event in events(calls)] == [
        "Queued prompt",
        "Queued answer",
        "Fresh prompt",
        "Fresh answer",
    ]
    assert pending(state) == 0


def test_failed_upload_retries_identical_bytes_before_removing_events(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(path, user(), hook_record(), assistant())
    monkeypatch.setattr(
        capture,
        "upload",
        lambda batch, key, endpoint, timeout: calls.append((dict(batch), key)) or False,
    )
    run()
    assert pending(state) == 2
    original = calls[-1][0]
    append(path, assistant("More work"))
    monkeypatch.setattr(
        capture,
        "upload",
        lambda batch, key, endpoint, timeout: calls.append((dict(batch), key)) or ACCEPTED,
    )
    run()
    assert calls[1][0]["body"] == original["body"]
    assert calls[1][0]["sha"] == hashlib.sha256(original["body"]).hexdigest()
    assert pending(state) == 0
    assert len(events(calls[1:])) == 3


def test_batch_size_event_limit_and_explicit_content_truncation(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(
        path,
        user(),
        hook_record(),
        *[assistant("汉" * 40000) for _ in range(7)],
        *[assistant("small") for _ in range(105)],
    )
    run()
    assert all(len(batch["body"]) <= capture.MAX_BATCH_BYTES for batch, key in calls)
    assert all(
        len(json.loads(batch["body"])["events"]) <= capture.MAX_BATCH_EVENTS for batch, key in calls
    )
    truncated = [event for event in events(calls) if event["truncated"]]
    assert len(truncated) == 7
    assert all(len(event["content"]) == capture.MAX_EVENT_CHARS for event in truncated)
    assert len(events(calls)) == 113


def test_configured_keys_and_common_secrets_are_redacted(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(
        path,
        user(),
        hook_record(),
        assistant(KEY + " password=hunter2 Bearer secret-token " + "ghp_" + "a" * 30),
    )
    run()
    content = str(events(calls))
    for secret in [KEY, "hunter2", "secret-token", "ghp_" + "a" * 30]:
        assert secret not in content
    assert "[REDACTED_SECRET]" in content


def test_unassignable_turns_cannot_fill_spool_or_block_later_capture(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "MAX_STATE_PAGES", 40)
    for _ in range(30):
        append(path, user("x" * 32000), assistant("x" * 32000))
    append(path, user("Valid prompt"), hook_record(), assistant("Valid answer"))
    run()
    assert [event["content"] for event in events(calls)] == ["Valid prompt", "Valid answer"]
    assert pending(state) == 0


def test_private_spool_and_removed_profile_preserve_pending_without_upload(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    append(path, user(), hook_record(), assistant())
    run()
    assert pending(state) == 2
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(next(state.glob("*.sqlite3")).stat().st_mode) == 0o600
    cfg.write_text(json.dumps({"version": 2, "profiles": []}))
    run()
    assert pending(state) == 2


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.test/hooks/conversations",
        "http://127.0.0.1:123/hooks/delivery",
        "http://user:password@localhost/hooks/conversations",
        "http://localhost/hooks/conversations?token=x",
    ],
)
def test_endpoint_cannot_redirect_upload_keys(endpoint):
    with pytest.raises(ValueError):
        capture.checked_endpoint(endpoint)


def test_source_replacement_pauses_without_removing_unacked_events(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    append(path, user(), hook_record(), assistant())
    run()
    replacement = path.with_suffix(".new")
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    with pytest.raises(ValueError, match="replaced"):
        run()
    assert pending(state) == 2


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_fresh_prompt_restores_same_segment_only_after_matching_marker(
    tmp_path, monkeypatch, client
):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    append(path, user("First", client), hook_record(client), assistant("First answer", client))
    run()
    original = json.loads(calls[0][0]["body"])["segment_id"]
    append(path, user("Second", client))
    run()
    assert [event["content"] for event in events(calls)] == ["First", "First answer"]
    append(path, hook_record(client), assistant("Second answer", client))
    run()
    run("SessionEnd")
    run("SessionStart")
    append(path, user("Third", client), hook_record(client), assistant("Third answer", client))
    run()
    assert {json.loads(batch["body"])["segment_id"] for batch, key in calls} == {original}
    assert [event["content"] for event in events(calls)] == [
        "First",
        "First answer",
        "Second",
        "Second answer",
        "Third",
        "Third answer",
    ]


def test_current_codex_visible_user_item_excludes_harness_response_messages(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(
        path,
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "HARNESS"}],
            },
        },
        {
            "type": "event_msg",
            "timestamp": NOW,
            "payload": {
                "type": "item_completed",
                "thread_id": SESSION,
                "turn_id": "native-turn",
                "item": {
                    "type": "UserMessage",
                    "id": "message-id",
                    "content": [{"type": "text", "text": "Actual visible prompt"}],
                },
            },
        },
        hook_record(turn="native-turn"),
        assistant(),
    )
    run()
    assert [event["content"] for event in events(calls)] == [
        "Actual visible prompt",
        "Visible answer",
    ]


def test_missing_transcript_still_flushes_previously_durable_outbox(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(path, user(), hook_record(), assistant())
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    run()
    assert pending(state) == 2
    path.unlink()
    monkeypatch.setattr(
        capture,
        "upload",
        lambda batch, key, endpoint, timeout: calls.append((dict(batch), key)) or ACCEPTED,
    )
    with pytest.raises(FileNotFoundError):
        run("SessionEnd")
    assert len(events(calls)) == 2 and pending(state) == 0


def test_server_erasure_retires_old_work_and_next_fresh_prompt_uses_new_segment(
    tmp_path, monkeypatch
):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(path, user("Private old title"), hook_record(), assistant("Private old answer"))
    monkeypatch.setattr(
        capture,
        "upload",
        lambda batch, key, endpoint, timeout: calls.append((dict(batch), key)) or DELETED,
    )
    run()
    old = json.loads(calls[0][0]["body"])["segment_id"]
    db = sqlite3.connect(next(state.glob("*.sqlite3")))
    assert db.execute("SELECT title,retired FROM segments WHERE id=?", (old,)).fetchone() == ("", 1)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    saved = db.execute("SELECT body FROM state").fetchone()[0]
    assert "Private old" not in saved
    db.close()
    calls.clear()
    append(path, assistant("Late old answer"))
    monkeypatch.setattr(
        capture,
        "upload",
        lambda batch, key, endpoint, timeout: calls.append((dict(batch), key)) or ACCEPTED,
    )
    run()
    assert not calls
    append(path, user("Fresh work"), hook_record(), assistant("Fresh answer"))
    run()
    assert [event["content"] for event in events(calls)] == ["Fresh work", "Fresh answer"]
    assert {json.loads(batch["body"])["segment_id"] for batch, key in calls} != {old}
    run()
    assert pending(state) == 0  # late text from the retired turn is not captured


def test_revoked_scope_does_not_block_another_configured_context(tmp_path, monkeypatch):
    prof = [{"user_id": OWNER, "upload_key": KEY}]
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, profiles=prof)
    append(
        path,
        user("Denied"),
        hook_record(),
        assistant("Denied answer"),
        user("Allowed"),
        hook_record(context=12),
        assistant("Allowed answer"),
    )

    def send(batch, key, endpoint, timeout):
        if json.loads(batch["body"])["context_id"] == 497:
            return "forbidden"
        calls.append((dict(batch), key))
        return ACCEPTED

    monkeypatch.setattr(capture, "upload", send)
    run()
    assert [event["content"] for event in events(calls)] == ["Allowed", "Allowed answer"]
    assert pending(state) == 2


@pytest.mark.parametrize(
    "field,value", [("channel", "analysis"), ("channel", "summary"), ("phase", "analysis")]
)
def test_codex_nonvisible_assistant_channels_are_excluded(tmp_path, monkeypatch, field, value):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    hidden = assistant("HIDDEN_REASONING_SENTINEL")
    hidden["payload"][field] = value
    append(path, user(), hook_record(), hidden, assistant())
    run()
    assert [event["content"] for event in events(calls)] == ["Visible question", "Visible answer"]


def test_retiring_old_context_does_not_clear_new_context_attribution(tmp_path, monkeypatch):
    prof = [{"user_id": OWNER, "upload_key": KEY}]
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, profiles=prof)
    append(
        path,
        user("Old company"),
        hook_record(),
        assistant("Old answer"),
        user("New company"),
        hook_record(context=12),
        assistant("New answer"),
    )

    def send(batch, key, endpoint, timeout):
        if json.loads(batch["body"])["context_id"] == 497:
            return DELETED
        calls.append((dict(batch), key))
        return ACCEPTED

    monkeypatch.setattr(capture, "upload", send)
    run()
    append(path, assistant("More new-company work"))
    run()
    assert [event["content"] for event in events(calls)] == [
        "New company",
        "New answer",
        "More new-company work",
    ]
    assert pending(state) == 0


@pytest.mark.parametrize("missing", [False, True])
def test_disabling_and_reenabling_never_backfills_disabled_history(tmp_path, monkeypatch, missing):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(path, user("Consented before disable"), hook_record(), assistant("Consented answer"))
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    run()
    original_config = cfg.read_text()
    if missing:
        cfg.unlink()
    else:
        cfg.write_text(json.dumps({"version": 2, "profiles": []}))
    append(path, user("Disabled prompt"), hook_record(), assistant("Disabled answer"))
    original_scan = capture.scan
    monkeypatch.setattr(
        capture, "scan", lambda *args: pytest.fail("disabled capture read transcript")
    )
    run()
    monkeypatch.setattr(capture, "scan", original_scan)
    cfg.write_text(original_config)
    cfg.chmod(0o600)
    monkeypatch.setattr(
        capture,
        "upload",
        lambda batch, key, endpoint, timeout: calls.append((dict(batch), key)) or ACCEPTED,
    )
    run()
    assert [event["content"] for event in events(calls)] == [
        "Consented before disable",
        "Consented answer",
    ]
    append(path, user("Fresh enabled prompt"), hook_record(), assistant("Fresh enabled answer"))
    run()
    assert [event["content"] for event in events(calls)] == [
        "Consented before disable",
        "Consented answer",
        "Fresh enabled prompt",
        "Fresh enabled answer",
    ]


def test_full_spool_still_retries_previously_committed_upload(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "MAX_STATE_PAGES", 60)
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    append(path, user(), hook_record(), assistant("x" * 32000))
    run()
    assert pending(state) == 2
    append(path, *[assistant("y" * 32000) for _ in range(20)])
    monkeypatch.setattr(
        capture,
        "upload",
        lambda batch, key, endpoint, timeout: calls.append((dict(batch), key)) or ACCEPTED,
    )
    for _ in range(30):
        try:
            run()
        except sqlite3.OperationalError:
            pass
    assert len(events(calls)) == 22 and pending(state) == 0
    assert events(calls)[0]["content"] == "Visible question"
    assert events(calls)[1]["content"] == "x" * 32000


def test_selection_destination_never_inherits_previous_owner_title(tmp_path, monkeypatch):
    prof = [
        {"user_id": OWNER, "upload_key": KEY},
        {"user_id": OTHER_OWNER, "upload_key": OTHER_KEY},
    ]
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, profiles=prof)
    append(
        path,
        user("Owner A confidential prompt"),
        hook_record(),
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "namespace": "mcp__pensieve",
                "name": "set_context",
                "call_id": "switch",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "switch",
                "output": "Owner B overview "
                + marker(owner=OTHER_OWNER, context=12, kind="selection"),
            },
        },
        assistant("Owner B answer"),
    )
    run()
    destination = [json.loads(batch["body"]) for batch, key in calls if key == OTHER_KEY]
    assert destination and all(body["title"] == "" for body in destination)
    assert "Owner A confidential prompt" not in json.dumps(destination)


@pytest.mark.parametrize("first_hook", ["SessionStart", "UserPromptSubmit"])
def test_startup_missing_transcript_captures_first_turn_without_backfill(
    tmp_path, monkeypatch, first_hook
):
    path = tmp_path / "new-transcript.jsonl"
    cfg = config(tmp_path)
    state = tmp_path / "spool"
    calls = []
    monkeypatch.setattr(
        capture, "upload", lambda batch, *args: calls.append(dict(batch)) or ACCEPTED
    )
    payload = {
        "session_id": SESSION,
        "hook_event_name": first_hook,
        "transcript_path": str(path),
    }
    capture.run_hook(payload, "claude", cfg, state)
    append(path, user(client="claude"), hook_record("claude"), assistant(client="claude"))
    payload["hook_event_name"] = "Stop"
    capture.run_hook(payload, "claude", cfg, state)
    assert [
        event["content"] for batch in calls for event in json.loads(batch["body"])["events"]
    ] == ["Visible question", "Visible answer"]


def test_claude_compaction_and_local_commands_preserve_segment(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, "claude")
    append(path, user("First", "claude"), hook_record("claude"), assistant(client="claude"))
    run()
    original = json.loads(calls[0][0]["body"])["segment_id"]
    summary = user("Internal compaction instructions", "claude")
    summary.update(isCompactSummary=True, isVisibleInTranscriptOnly=True)
    append(
        path,
        summary,
        user("<command-name>/compact</command-name>", "claude"),
        user("<local-command-stdout>Compacted</local-command-stdout>", "claude"),
    )
    run("SessionStart")
    append(path, user("Second", "claude"), hook_record("claude"), assistant(client="claude"))
    run()
    assert {json.loads(batch["body"])["segment_id"] for batch, _ in calls} == {original}
    assert [e["content"] for e in events(calls) if e["kind"] == "user"] == ["First", "Second"]
    assert pending(state) == 0


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_internal_hook_tool_results_are_excluded_even_when_called_normally(
    tmp_path, monkeypatch, client
):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    if client == "codex":
        tool_call = {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "hook",
                "name": "mcp__pensieve__context_briefing",
            },
        }
        tool_result = {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "hook",
                "output": "Internal hook payload",
            },
        }
    else:
        tool_call = assistant(client=client)
        tool_call["message"]["content"] = [
            {
                "type": "tool_use",
                "id": "hook",
                "name": "mcp__plugin_pensieve_pensieve__context_briefing",
                "input": {},
            }
        ]
        tool_result = user(client=client)
        tool_result["message"]["content"] = [
            {"type": "tool_result", "tool_use_id": "hook", "content": "Internal hook payload"}
        ]
    append(
        path,
        user(client=client),
        hook_record(client),
        tool_call,
        tool_result,
        assistant(client=client),
    )
    run()
    assert [event["kind"] for event in events(calls)] == ["user", "assistant"]


def native_selection(
    context=12, turn="turn-one", call_id="nested-call", server="pensieve", generation=GENERATION
):
    return {
        "type": "event_msg",
        "timestamp": NOW,
        "payload": {
            "type": "item_completed",
            "thread_id": SESSION,
            "turn_id": turn,
            "item": {
                "type": "McpToolCall",
                "id": call_id,
                "server": server,
                "tool": "set_context",
                "status": "completed",
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Destination company result "
                            + marker(context=context, kind="selection", generation=generation),
                        }
                    ]
                },
            },
        },
    }


@pytest.mark.parametrize("wrapper", ["exec", "wait"])
@pytest.mark.parametrize("generation", [GENERATION, "317e0b53-8178-4a3c-8d40-a07414144741"])
def test_codex_native_selection_fences_combined_code_mode_output(
    tmp_path, monkeypatch, wrapper, generation
):
    prof = [{"user_id": OWNER, "upload_key": KEY}]
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, profiles=prof)
    append(
        path,
        {"type": "turn_context", "payload": {"turn_id": "turn-one"}},
        user("Original company prompt"),
        hook_record(turn="turn-one"),
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "wrapper",
                "namespace": "functions",
                "name": wrapper,
            },
        },
        native_selection(generation=generation),
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "wrapper",
                "output": "Original company secret and destination company secret",
            },
        },
        assistant("Destination company answer"),
    )
    run()
    original = [
        event
        for batch, key in calls
        if json.loads(batch["body"])["context_id"] == 497
        for event in json.loads(batch["body"])["events"]
    ]
    destination = [
        event
        for batch, key in calls
        if json.loads(batch["body"])["context_id"] == 12
        for event in json.loads(batch["body"])["events"]
    ]
    assert "Destination company" not in json.dumps(original)
    assert "Original company" not in json.dumps(destination)
    assert "company secret" not in json.dumps(events(calls))
    assert any("tool output omitted" in event["content"] for event in destination)
    assert destination[-1]["content"] == "Destination company answer"


@pytest.mark.parametrize(
    "field,value",
    [("server", "other-server"), ("thread_id", "other-thread"), ("turn_id", "other-turn")],
)
def test_native_selection_requires_host_and_pensieve_provenance(
    tmp_path, monkeypatch, field, value
):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    selection = native_selection()
    target = selection["payload"]["item"] if field == "server" else selection["payload"]
    target[field] = value
    append(
        path,
        {"type": "turn_context", "payload": {"turn_id": "turn-one"}},
        user(),
        hook_record(turn="turn-one"),
        selection,
        assistant(),
    )
    run()
    assert [event["content"] for event in events(calls)] == ["Visible question", "Visible answer"]


def test_removing_one_profile_excludes_its_disabled_interval_and_keeps_old_backlog(
    tmp_path, monkeypatch
):
    prof = [
        {"user_id": OWNER, "upload_key": KEY},
        {"user_id": OTHER_OWNER, "upload_key": OTHER_KEY},
    ]
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, profiles=prof)
    append(path, user("Consented prompt"), hook_record(), assistant("Consented answer"))
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    run()
    cfg.write_text(json.dumps({"version": 2, "profiles": prof[1:]}))
    run()  # Observe the per-profile removal before the disabled text exists.
    append(path, assistant("Disabled interval answer"))
    run()
    cfg.write_text(json.dumps({"version": 2, "profiles": prof}))
    monkeypatch.setattr(
        capture, "upload", lambda batch, key, *args: calls.append((dict(batch), key)) or ACCEPTED
    )
    run()
    assert [event["content"] for event in events(calls)] == ["Consented prompt", "Consented answer"]
    append(path, assistant("Still no fresh consent marker"))
    run()
    append(path, user("Fresh prompt"), hook_record(), assistant("Fresh answer"))
    run()
    assert [event["content"] for event in events(calls)] == [
        "Consented prompt",
        "Consented answer",
        "Fresh prompt",
        "Fresh answer",
    ]


def at(record, timestamp):
    return {**record, "timestamp": timestamp}


def test_legacy_expiry_is_ignored_at_the_next_fresh_user_turn(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(path, user("Original prompt"), hook_record(), assistant())
    run()
    original = json.loads(calls[0][0]["body"])["segment_id"]
    db = capture.connect_state(state, "codex", SESSION)
    with db:
        db.execute("UPDATE segments SET expires_at=?", (EXPIRY,))
    db.close()
    append(
        path,
        at(user("New prompt after expiry"), "2026-12-15T10:00:00+00:00"),
        hook_record(),
        assistant("New answer"),
    )
    run()
    fresh = [
        json.loads(batch["body"])
        for batch, key in calls
        if any(
            e["content"] == "New prompt after expiry" for e in json.loads(batch["body"])["events"]
        )
    ]
    assert fresh and all(batch["segment_id"] == original for batch in fresh)
    assert all("parent_conversation_id" not in json.loads(batch["body"]) for batch, key in calls)


@pytest.mark.parametrize("valid_host_time", [True, False])
def test_deleted_segment_never_replays_queued_turns(tmp_path, monkeypatch, valid_host_time):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    attempts = []
    monkeypatch.setattr(
        capture, "upload", lambda batch, *args: attempts.append(dict(batch)) or False
    )
    append(path, user("Old prompt"), hook_record(), assistant("Old answer"))
    run()
    old_batch = attempts[0]
    queued_user = user("Queued prompt after legacy deadline")
    if valid_host_time:
        queued_user["timestamp"] = "2026-12-15T10:00:00+00:00"
    else:
        queued_user.pop("timestamp")
    append(path, queued_user, hook_record(), assistant("Queued answer"))
    run()
    assert attempts[-1]["body"] == old_batch["body"]

    def send(batch, key, *args):
        if batch["segment"] == old_batch["segment"]:
            assert batch["body"] == old_batch["body"]
            return DELETED
        calls.append((dict(batch), key))
        return ACCEPTED

    monkeypatch.setattr(capture, "upload", send)
    run()
    assert not calls and pending(state) == 0
    append(path, assistant("Late response to deleted work"))
    run()
    assert not calls
    append(path, user("New prompt after deletion"), hook_record(), assistant("New answer"))
    run()
    assert [event["content"] for event in events(calls)] == [
        "New prompt after deletion",
        "New answer",
    ]
    assert calls[0][0]["segment"] != old_batch["segment"]
    assert "parent" not in events(calls)[0]["capture"]


def test_deletion_discards_a_provisional_prompt_from_the_deleted_segment(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    append(path, user("Old prompt"), hook_record(), assistant())
    run()
    append(path, at(user("Queued awaiting prompt"), "2026-12-15T10:00:00+00:00"))
    monkeypatch.setattr(capture, "upload", lambda *args: DELETED)
    run("UserPromptSubmit")
    append(path, hook_record(), assistant("Response to deleted work"))
    monkeypatch.setattr(
        capture, "upload", lambda batch, key, *args: calls.append((dict(batch), key)) or ACCEPTED
    )
    run()
    assert not calls and pending(state) == 0
    append(path, user("New prompt"), hook_record(), assistant("New response"))
    run()
    assert [event["content"] for event in events(calls)] == ["New prompt", "New response"]


def test_reenable_with_new_key_excludes_disabled_interval_without_an_intermediate_hook(
    tmp_path, monkeypatch
):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    append(path, user("Enabled prompt"), hook_record(), assistant("Enabled answer"))
    run()
    cfg.write_text(json.dumps({"version": 2, "profiles": []}))
    append(path, user("Disabled prompt"), hook_record(), assistant("Disabled answer"))
    cfg.write_text(
        json.dumps(
            {
                "version": 2,
                "profiles": [{"user_id": OWNER, "upload_key": OTHER_KEY}],
            }
        )
    )
    monkeypatch.setattr(
        capture, "upload", lambda batch, key, *args: calls.append((dict(batch), key)) or ACCEPTED
    )
    run()
    assert [event["content"] for event in events(calls)] == ["Enabled prompt", "Enabled answer"]
    append(path, user("Reenabled prompt"), hook_record(), assistant("Reenabled answer"))
    run()
    assert [event["content"] for event in events(calls)] == [
        "Enabled prompt",
        "Enabled answer",
        "Reenabled prompt",
        "Reenabled answer",
    ]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_server_opt_out_and_fresh_generation_never_backfill_old_work(tmp_path, monkeypatch, client):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    append(
        path,
        user("Old queued prompt", client),
        hook_record(client),
        assistant("Old queued answer", client),
    )
    run()
    append(
        path,
        user("Disabled prompt", client),
        hook_record(client, generation=None),
        assistant("Disabled answer", client),
    )
    fresh = str(uuid.uuid4())

    def send(batch, key, *args):
        if json.loads(batch["body"])["capture_generation"] != fresh:
            return {"status": "capture_disabled"}
        calls.append((dict(batch), key))
        return ACCEPTED

    monkeypatch.setattr(capture, "upload", send)
    run()  # The setting changed without any local config change.
    assert not calls and pending(state) == 0
    append(
        path,
        user("New prompt", client),
        hook_record(client, generation=fresh),
        assistant("New answer", client),
    )
    run()
    assert [event["content"] for event in events(calls)] == ["New prompt", "New answer"]


def test_new_opt_in_generation_during_selection_waits_for_fresh_prompt(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    append(path, user(), hook_record(generation=None))
    run()
    db = capture.connect_state(state, "codex", SESSION)
    state_value = capture.load_state(db)
    boundary = json.loads(
        marker(kind="selection").split("pensieve-capture-context ")[1].removesuffix(" -->")
    )
    capture.apply_item(
        db,
        state_value,
        {"boundary": boundary},
        "selection",
        NOW,
        "codex",
        SESSION,
        {OWNER: KEY},
        True,
    )
    capture.save_state(db, state_value)
    db.commit()
    db.close()
    append(path, assistant("Old turn must stay private"))
    run()
    assert not calls
    append(path, user("Fresh prompt"), hook_record(), assistant("Fresh answer"))
    run()
    assert [event["content"] for event in events(calls)] == ["Fresh prompt", "Fresh answer"]


@pytest.mark.parametrize(
    "content,secret",
    [
        ('{"password":"quoted-json-secret"}', "quoted-json-secret"),
        ('{"access_token": "quoted-token"}', "quoted-token"),
        ("{'api_key': 'python-key'}", "python-key"),
    ],
)
def test_quoted_credential_fields_are_redacted(content, secret):
    cleaned, _ = capture.clean_content(content, [])
    assert secret not in cleaned
    assert "[REDACTED_SECRET]" in cleaned


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_baseline_mid_turn_does_not_queue_orphan_outputs(tmp_path, monkeypatch, client):
    path, cfg, state, calls, run = setup(
        tmp_path, monkeypatch, client, prior=[user("Prompt before setup", client)]
    )
    monkeypatch.setattr(capture, "MAX_STATE_PAGES", 40)
    append(path, hook_record(client), *[assistant("x" * 32000, client) for _ in range(30)])
    append(
        path, user("Fresh prompt", client), hook_record(client), assistant("Fresh answer", client)
    )
    run()
    assert [event["content"] for event in events(calls)] == ["Fresh prompt", "Fresh answer"]
    assert pending(state) == 0


def test_nested_write_keeps_native_identity_once_without_inputs_or_outputs(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    native = native_selection(call_id="exec-write")
    native["payload"]["item"].update(tool="edit_page", arguments={"secret": "PRIVATE"})
    append(
        path,
        {"type": "turn_context", "payload": {"turn_id": "turn-one"}},
        user(),
        hook_record(turn="turn-one"),
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "namespace": "functions",
                "name": "exec",
                "call_id": "wrapper",
            },
        },
        native,
        native,
    )
    run()
    run()
    captured = [
        e for e in events(calls) if e.get("capture", {}).get("tool_call_id") == "exec-write"
    ]
    assert len(captured) == 1
    assert captured[0]["content"] == "mcp__pensieve__edit_page"
    assert "PRIVATE" not in json.dumps(events(calls))
    assert "Destination company" not in json.dumps(events(calls))


@pytest.mark.parametrize(
    "field,value",
    [("server", "foreign"), ("thread_id", "other"), ("turn_id", "other"), ("status", "running")],
)
def test_nested_write_requires_native_completed_call(tmp_path, monkeypatch, field, value):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    native = native_selection(call_id="exec-write")
    native["payload"]["item"]["tool"] = "save_data"
    target = native["payload"] if field in {"thread_id", "turn_id"} else native["payload"]["item"]
    target[field] = value
    append(
        path,
        {"type": "turn_context", "payload": {"turn_id": "turn-one"}},
        user(),
        hook_record(turn="turn-one"),
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "namespace": "functions",
                "name": "exec",
                "call_id": "wrapper",
            },
        },
        native,
    )
    run()
    assert not any(e.get("capture", {}).get("tool_call_id") == "exec-write" for e in events(calls))


def run_other_session(path, cfg, state, client="codex"):
    other_session = "f23bbd76-d864-4b8f-b252-c2b7c3692492"
    other = path.with_name("other.jsonl")
    if not other.exists():
        other.touch()
        if client == "codex":
            append(other, {"type": "session_meta", "payload": {"id": other_session}})
    capture.run_hook(
        {
            "session_id": other_session,
            "hook_event_name": "SessionStart",
            "transcript_path": str(other),
        },
        client,
        cfg,
        state,
    )


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_new_chat_recovers_closed_chat_offline_outbox(tmp_path, monkeypatch, client):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    append(path, user(client=client), hook_record(client), assistant(client=client))
    run("SessionEnd")
    assert pending(state) == 2
    # Removing the source must not prevent already committed events from retrying.
    path.unlink()
    monkeypatch.setattr(
        capture, "upload", lambda batch, key, *args: calls.append((batch, key)) or ACCEPTED
    )
    run_other_session(path, cfg, state, client)
    assert [event["content"] for event in events(calls)] == ["Visible question", "Visible answer"]
    assert pending(state) == 0


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_new_chat_recovers_final_record_written_after_last_hook(tmp_path, monkeypatch, client):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    append(path, user(client=client), hook_record(client))
    run("Stop")
    run("SessionEnd")
    append(path, assistant("Late final reply", client))
    run_other_session(path, cfg, state, client)
    run_other_session(path, cfg, state, client)
    assert [event["content"] for event in events(calls)] == ["Visible question", "Late final reply"]
    assert len({event["event_id"] for event in events(calls)}) == 2


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_recovery_never_rescans_a_replaced_source(tmp_path, monkeypatch, client):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    append(path, user(client=client), hook_record(client))
    run()
    replacement = path.with_suffix(".new")
    replacement.write_bytes(path.read_bytes())
    append(replacement, assistant("Untrusted replacement", client))
    os.replace(replacement, path)
    run_other_session(path, cfg, state, client)
    assert [event["content"] for event in events(calls)] == ["Visible question"]


def test_recovery_rotates_across_spools_and_excludes_other_apps(tmp_path, monkeypatch):
    state = tmp_path / "spool"
    state.mkdir(mode=0o700)
    sessions = [str(uuid.UUID(int=i + 1)) for i in range(10)]
    for session in sessions:
        (state / f"codex-{session}.sqlite3").touch(mode=0o600)
    (state / f"claude-{SESSION}.sqlite3").touch(mode=0o600)
    recovered = []
    monkeypatch.setattr(
        capture, "capture_session", lambda client, session, *a, **kw: recovered.append(session)
    )
    # A corrupt but valid JSON cursor must not stop capture or recovery.
    cursor = state / "recovery-codex.json"
    cursor.write_text("[]")
    cursor.chmod(0o600)
    capture.recover_sessions(
        "codex", SESSION, {}, state, capture.UPLOAD_ENDPOINT, time.monotonic() + 1
    )
    assert recovered == sessions[:8]
    capture.recover_sessions(
        "codex", SESSION, {}, state, capture.UPLOAD_ENDPOINT, time.monotonic() + 1
    )
    assert recovered[8:10] == sessions[8:]
    assert set(recovered) == set(sessions)


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_recovery_captures_late_native_completion_once(tmp_path, monkeypatch, client):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    if client == "codex":
        append(path, codex_lifecycle("task_started"))
    append(path, user(client=client), hook_record(client))
    run("SessionEnd")
    answer = assistant("Late final reply", client)
    if client == "claude":
        answer["message"]["stop_reason"] = "end_turn"
    append(path, answer, codex_lifecycle("task_complete") if client == "codex" else claude_stop())
    run_other_session(path, cfg, state, client)
    run_other_session(path, cfg, state, client)
    saved = events(calls)
    assert [e["kind"] for e in saved] == ["user", "assistant", "turn_end"]
    assert len({e["capture"]["turn_id"] for e in saved}) == 1
    assert saved[-1]["capture"]["parent"]["event_id"] == saved[-2]["event_id"]
    assert saved[-1]["capture"]["completion"] == "completed"


def test_recovery_skips_malformed_spool_without_stranding_healthy_backlog(tmp_path, monkeypatch):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    append(path, user(), hook_record(), assistant())
    run("SessionEnd")
    malformed = capture.connect_state(state, "codex", str(uuid.UUID(int=1)))
    malformed.execute("INSERT OR REPLACE INTO state(id,body) VALUES (1,'[]')")
    malformed.commit()
    malformed.close()
    monkeypatch.setattr(
        capture, "upload", lambda batch, key, *args: calls.append((batch, key)) or ACCEPTED
    )
    run_other_session(path, cfg, state)
    assert [e["content"] for e in events(calls)] == ["Visible question", "Visible answer"]
    assert json.loads((state / "recovery-codex.json").read_text())["after"] == SESSION


@pytest.fixture
def upload_transport(monkeypatch):
    response = {"status": 200, "overrides": {}, "omit": [], "requests": []}

    class Opener:
        def open(self, request, timeout):
            response["requests"].append(request.data)
            body = json.loads(request.data)
            receipt = {
                "batch_id": body["batch_id"],
                "segment_id": body["segment_id"],
                "conversation_id": SESSION,
                "batch_sha256": hashlib.sha256(request.data).hexdigest(),
                "accepted_events": len(body["events"]),
                "expires_at": None,
            }
            receipt.update(response["overrides"])
            for field in response["omit"]:
                receipt.pop(field, None)
            stream = io.BytesIO(json.dumps(receipt).encode())
            if response["status"] != 200:
                raise HTTPError(request.full_url, response["status"], "fixture", {}, stream)
            stream.status = 200
            return stream

    monkeypatch.setattr(capture, "build_opener", lambda *args: Opener())
    return response


@pytest.mark.parametrize(
    "overrides,omit,accepted",
    [
        ({}, [], True),
        ({"expires_at": EXPIRY}, [], True),
        ({}, ["expires_at"], False),
        ({"expires_at": "invalid"}, [], False),
        ({"expires_at": "2026-12-14T10:00:00"}, [], False),
        ({"batch_id": str(uuid.uuid4())}, [], False),
        ({"segment_id": str(uuid.uuid4())}, [], False),
        ({"batch_sha256": "wrong"}, [], False),
        ({"accepted_events": 3}, [], False),
        ({"conversation_id": "invalid"}, [], False),
    ],
)
def test_indefinite_receipt_still_requires_exact_acknowledgement(
    tmp_path, monkeypatch, upload_transport, overrides, omit, accepted
):
    real_upload = capture.upload
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", real_upload)
    upload_transport.update(overrides=overrides, omit=omit)
    append(path, user(), hook_record(), assistant())
    run()
    assert pending(state) == (0 if accepted else 2)
    first = upload_transport["requests"][0]
    upload_transport.update(overrides={}, omit=[])
    run()
    assert pending(state) == 0
    if not accepted:
        assert upload_transport["requests"][-1] == first


@pytest.mark.parametrize(
    "reason,matching",
    [("deleted", True), ("capture_disabled", True), ("deleted", False), ("unknown", True)],
)
def test_terminal_http_response_is_scoped_and_allows_only_new_work(
    tmp_path, monkeypatch, upload_transport, reason, matching
):
    real_upload = capture.upload
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", real_upload)
    overrides = {"reason": reason}
    if not matching:
        overrides["segment_id"] = str(uuid.uuid4())
    upload_transport.update(status=410, overrides=overrides)
    append(path, user("Deleted prompt"), hook_record(), assistant("Deleted answer"))
    run()
    retired = matching and reason in {"deleted", "capture_disabled"}
    assert pending(state) == (0 if retired else 2)
    first = upload_transport["requests"][0]
    append(path, assistant("Late response"))
    run()
    if retired:
        assert len(upload_transport["requests"]) == 1
    else:
        assert upload_transport["requests"][-1] == first
    if not retired:
        return
    upload_transport.update(status=200, overrides={})
    append(path, user("New prompt"), hook_record(), assistant("New response"))
    run()
    latest = json.loads(upload_transport["requests"][-1])
    assert latest["segment_id"] != json.loads(first)["segment_id"]
    assert [event["content"] for event in latest["events"]] == ["New prompt", "New response"]
    assert "parent" not in latest["events"][0]["capture"]
    assert pending(state) == 0
