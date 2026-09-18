"""History import uses synthetic stores only. No host files or live credentials."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import capture_history as history
import conversation_capture as capture
import pytest
from capture_config import install_profile

SESSION = "923bbd76-d864-4b8f-b252-c2b7c3692492"
OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
GENERATION = "217e0b53-8178-4a3c-8d40-a07414144741"
RECEIPT = "017e0b53-8178-4a3c-8d40-a07414144741"
NOW = "2026-09-15T10:00:00+00:00"


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))


def user(text, client="codex", timestamp=NOW, cwd="/work/acme"):
    if client == "claude":
        return {
            "type": "user",
            "timestamp": timestamp,
            "sessionId": SESSION,
            "isSidechain": False,
            "cwd": cwd,
            "message": {"role": "user", "content": text},
        }
    return {
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": {"type": "user_message", "message": text},
    }


def assistant(text="Visible answer", client="codex", **kwargs):
    payload = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text" if client == "codex" else "text", "text": text}],
    }
    if client == "claude":
        return {
            "type": "assistant",
            "timestamp": NOW,
            "sessionId": SESSION,
            "isSidechain": False,
            "cwd": "/work/acme",
            "message": payload,
            **kwargs,
        }
    return {"type": "response_item", "timestamp": NOW, "payload": {**payload, **kwargs}}


def setup(tmp_path, monkeypatch, client="codex"):
    root = tmp_path / "host-store"
    root.mkdir()
    grant = {
        "id": str(uuid.uuid4()),
        "context_id": 497,
        "client": client,
        "capture_generation": GENERATION,
        "publication_generation": GENERATION,
        "since": "2026-09-01T00:00:00+00:00",
        "until": "2026-09-18T00:00:00+00:00",
        "project_path": "/work/acme",
    }
    profile = {
        "user_id": OWNER,
        "client": client,
        "upload_key": "synthetic-history-key",
        "installation_id": str(uuid.uuid4()),
        "runtime": "unknown",
        "host_version": "",
    }
    config = tmp_path / "private" / "capture.json"
    install_profile(config, profile)
    state = tmp_path / "state"
    sent, progress, requests = [], [], []
    service = {"imports": [grant], "result": {"status": "accepted", "expires_at": None}}

    def request(base, path, body, timeout, key=None):
        assert key == profile["upload_key"]
        requests.append((path, body))
        if path.endswith("/history-imports"):
            return 200, {"imports": service["imports"]}
        progress.append(body)
        return 200, {}

    def upload(batch, key, endpoint, timeout):
        assert key == profile["upload_key"]
        assert json.loads(batch["event_ids"]) == [
            e["event_id"] for e in json.loads(batch["body"])["events"]
        ]
        sent.append(batch)
        return service["result"]

    monkeypatch.setattr(history, "history_roots", lambda _client: [root])
    monkeypatch.setattr(history, "request", request)
    monkeypatch.setattr(history, "upload", upload)

    def run():
        history.sync(config, client, state_root=state, seconds=2)

    return root, grant, service, sent, progress, run, state


def codex_records(*records, cwd="/work/acme"):
    return [{"type": "session_meta", "payload": {"id": SESSION, "cwd": cwd}}, *records]


def events(sent):
    return [e for batch in sent for e in json.loads(batch["body"])["events"]]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_explicit_folder_and_original_time_window_only(tmp_path, monkeypatch, client):
    root, grant, service, sent, progress, run, _ = setup(tmp_path, monkeypatch, client)
    records = [
        user("too old", client, "2026-08-31T00:00:00+00:00"),
        user("Work decision", client),
        assistant(client=client),
        user("future", client, "2026-09-19T00:00:00+00:00"),
        user("undated", client, None),
    ]
    write(root / "nested/session.jsonl", codex_records(*records) if client == "codex" else records)
    run()
    assert [e["content"] for e in events(sent)] == ["Work decision", "Visible answer"]
    assert {e["occurred_at"] for e in events(sent)} == {NOW}
    assert all(json.loads(batch["body"])["history_import_id"] == grant["id"] for batch in sent)
    assert progress[-1] == {
        "state": "completed",
        "processed_conversations": 1,
        "processed_events": 2,
        "error_code": None,
    }
    run()
    assert len(sent) == 1


def test_changed_cwd_sibling_private_content_and_reasoning_are_excluded(tmp_path, monkeypatch):
    root, _, _, sent, _, run, _ = setup(tmp_path, monkeypatch)
    write(
        root / "chat.jsonl",
        codex_records(
            user("Work password=secret"),
            assistant("Hidden reasoning", channel="analysis"),
            {"type": "turn_context", "payload": {"cwd": "/work/acme-other", "turn_id": "other"}},
            user("Other company"),
            {
                "type": "turn_context",
                "payload": {"cwd": "/work/acme/subproject", "turn_id": "ours"},
            },
            user("Subproject"),
        ),
    )
    run()
    assert [e["content"] for e in events(sent)] == ["Work [REDACTED_SECRET]", "Subproject"]


def test_unrelated_folder_and_symlinks_are_never_followed(tmp_path, monkeypatch):
    root, _, _, sent, progress, run, _ = setup(tmp_path, monkeypatch)
    write(root / "unrelated.jsonl", codex_records(user("private"), cwd="/private"))
    outside = tmp_path / "outside.jsonl"
    write(outside, codex_records(user("symlink content")))
    (root / "linked.jsonl").symlink_to(outside)
    (root / "linked-folder").symlink_to(tmp_path, target_is_directory=True)
    run()
    assert not sent
    assert progress[-1]["state"] == "completed"


def test_no_grant_does_not_discover_history(tmp_path, monkeypatch):
    _, _, service, sent, _, run, _ = setup(tmp_path, monkeypatch)
    service["imports"] = []
    monkeypatch.setattr(history, "history_roots", lambda client: pytest.fail("No grant, no scan"))
    run()
    assert not sent


def test_exact_retry_survives_source_deletion_and_revocation_erases_pending(tmp_path, monkeypatch):
    root, _, service, sent, progress, run, state = setup(tmp_path, monkeypatch)
    path = root / "chat.jsonl"
    write(path, codex_records(user("Retried once")))
    service["result"] = False
    run()
    first = sent[-1]["body"]
    path.unlink()
    service["result"] = {"status": "accepted", "expires_at": None}
    run()
    assert sent[-1]["body"] == first
    assert progress[-1]["state"] == "completed"
    service["imports"] = []
    run()
    db = sqlite3.connect(next(state.rglob("*.sqlite3")))
    assert db.execute("SELECT COUNT(*) FROM state").fetchone()[0] == 0
    db.close()


def test_batches_are_bounded_and_ids_match_live_offsets(tmp_path, monkeypatch):
    root, _, _, sent, progress, run, _ = setup(tmp_path, monkeypatch)
    path = root / "chat.jsonl"
    records = codex_records(*(user(f"Event {i}") for i in range(230)))
    write(path, records)
    run()
    assert len(sent) == 3
    assert len(events(sent)) == 230
    offset = len(json.dumps(records[0]).encode()) + 1
    assert events(sent)[0]["event_id"] == f"codex:{SESSION}:{offset}:0"
    assert [e["sequence"] for e in events(sent)] == list(range(1, 231))
    assert progress[-1]["processed_events"] == 230
    assert all(len(b["body"]) <= capture.MAX_BATCH_BYTES for b in sent)


def test_authority_failure_never_rolls_old_history_into_new_segment(tmp_path, monkeypatch):
    root, _, service, sent, progress, run, _ = setup(tmp_path, monkeypatch)
    write(root / "chat.jsonl", codex_records(user("Old decision")))
    service["result"] = {"status": "deleted"}
    run()
    run()
    assert len(sent) == 1
    assert progress[-1]["state"] == "failed"
    assert progress[-1]["error_code"] == "import_failed"


def test_changed_grant_and_traversal_fail_closed(tmp_path, monkeypatch):
    root, grant, service, sent, progress, run, _ = setup(tmp_path, monkeypatch)
    write(root / "chat.jsonl", codex_records(user("Work")))
    service["result"] = False
    run()
    grant["context_id"] = 999
    run()
    assert len(sent) == 1
    assert progress[-1]["state"] == "failed"
    with pytest.raises(ValueError):
        history.validate_grant({**grant, "project_path": "/work/../private"}, "codex")


def test_mutation_receipts_require_native_success_and_matching_tool():
    text = json.dumps({"kind": "edit", "conversation_receipt_id": RECEIPT})
    state = {"turn_id": "turn1", "calls": {}}
    native = {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "thread_id": SESSION,
            "turn_id": "turn1",
            "item": {
                "type": "McpToolCall",
                "server": "pensieve",
                "tool": "edit_page",
                "status": "completed",
                "id": "native-call",
                "result": {"isError": False, "content": [{"type": "text", "text": text}]},
            },
        },
    }
    result = capture.normalise(native, "codex", SESSION, state)
    assert result[0]["mutation_receipt_id"] == RECEIPT
    assert "mutation_receipt_id" not in capture.normalise(native, "codex", SESSION, state)[0]
    native["payload"]["turn_id"] = "wrong-turn"
    assert not capture.normalise(native, "codex", SESSION, state)
    assert capture.mutation_receipt("create_page", text, {}) is None
    assert capture.mutation_receipt(None, text, {}) is None
    assert (
        capture.mutation_receipt(
            "edit_page", {"isError": True, "content": [{"type": "text", "text": text}]}, {}
        )
        is None
    )


def test_claude_save_receipt_only_correlated_tool_result_and_not_visible_marker():
    state = {"calls": {}}
    record = assistant(client="claude")
    record["message"]["content"] = [
        {"type": "tool_use", "id": "call", "name": "mcp__pensieve__save_data"}
    ]
    capture.normalise(record, "claude", SESSION, state)
    text = f"Saved. <!-- pensieve-mutation-receipt:{RECEIPT} -->"
    record = user("", "claude")
    record["message"]["content"] = [{"type": "tool_result", "tool_use_id": "call", "content": text}]
    result = capture.normalise(record, "claude", SESSION, state)
    assert result[0]["mutation_receipt_id"] == RECEIPT
    assert capture.clean_content(text, [])[0] == "Saved. "
    forged = capture.normalise(user(text, "claude"), "claude", SESSION, {})
    assert "mutation_receipt_id" not in forged[0]


def test_history_protocol_get_grant_and_exact_upload_receipt(tmp_path, monkeypatch):
    root = tmp_path / "synthetic-store"
    write(root / "session.jsonl", codex_records(user("Original work")))
    grant = {
        "id": str(uuid.uuid4()),
        "context_id": 497,
        "client": "codex",
        "capture_generation": GENERATION,
        "publication_generation": GENERATION,
        "since": None,
        "until": "2026-09-18T00:00:00+00:00",
        "project_path": "/work/acme",
    }
    config = tmp_path / "config" / "capture.json"
    install_profile(
        config,
        {
            "user_id": OWNER,
            "client": "codex",
            "upload_key": "synthetic-history-key",
            "installation_id": str(uuid.uuid4()),
            "runtime": "unknown",
            "host_version": "",
        },
    )
    requests, progress = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_GET(self):
            assert self.path.endswith("/installations/history-imports")
            assert self.headers["Authorization"] == "Bearer synthetic-history-key"
            self.respond({"imports": [grant]})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            if self.path == "/hooks/conversations":
                requests.append(body)
                self.respond(
                    {
                        "batch_id": body["batch_id"],
                        "batch_sha256": hashlib.sha256(raw).hexdigest(),
                        "segment_id": body["segment_id"],
                        "conversation_id": str(uuid.uuid4()),
                        "accepted_events": len(body["events"]),
                        "expires_at": None,
                    }
                )
            else:
                progress.append(body)
                self.respond({})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(history, "history_roots", lambda client: [root])
    try:
        address = f"http://127.0.0.1:{server.server_port}"
        history.sync(
            config,
            "codex",
            state_root=tmp_path / "state",
            seconds=2,
            base=address + "/users/me/conversation-capture",
            endpoint=address + "/hooks/conversations",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert requests[0]["history_import_id"] == grant["id"]
    assert requests[0]["history_project_path"] == grant["project_path"]
    assert progress[-1]["state"] == "completed"


def test_history_and_live_receipts_share_native_identity(tmp_path, monkeypatch):
    root, _, _, sent, _, run, _ = setup(tmp_path, monkeypatch)
    result = json.dumps({"kind": "edit", "conversation_receipt_id": RECEIPT})
    records = codex_records(
        user("Edit this"),
        {
            "type": "response_item",
            "timestamp": NOW,
            "payload": {
                "type": "function_call",
                "name": "mcp__pensieve__edit_page",
                "call_id": "call",
                "arguments": "Never captured",
            },
        },
        {
            "type": "response_item",
            "timestamp": NOW,
            "payload": {"type": "function_call_output", "call_id": "call", "output": result},
        },
    )
    write(root / "session.jsonl", records)
    run()
    captured = events(sent)
    assert captured[-1]["mutation_receipt_id"] == RECEIPT
    assert "Never captured" not in json.dumps(captured)


def test_import_does_not_follow_symlinked_parent_store(tmp_path):
    actual = tmp_path / "actual"
    write(actual / "sessions" / "chat.jsonl", codex_records(user("private")))
    (tmp_path / "link").symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError):
        history.open_source(tmp_path / "link" / "sessions", "chat.jsonl")


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_history_import_does_not_adopt_transcript_search_results(tmp_path, monkeypatch, client):
    root, _, _, sent, _, run, _ = setup(tmp_path, monkeypatch, client)
    records = [user("Work question", client)]
    for index, node_type in enumerate(("transcript", "page")):
        arguments = {"query": "Do not persist this query", "node_types": [node_type]}
        output = f"{node_type} search result"
        if client == "codex":
            records.extend(
                [
                    {
                        "type": "response_item",
                        "timestamp": NOW,
                        "payload": {
                            "type": "function_call",
                            "call_id": str(index),
                            "name": "mcp__pensieve__search",
                            "arguments": json.dumps(arguments),
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": NOW,
                        "payload": {
                            "type": "function_call_output",
                            "call_id": str(index),
                            "output": output,
                        },
                    },
                ]
            )
        else:
            call = assistant(client="claude")
            call["message"]["content"] = [
                {
                    "type": "tool_use",
                    "id": str(index),
                    "name": "mcp__pensieve__search",
                    "input": arguments,
                }
            ]
            result = user("", "claude")
            result["message"]["content"] = [
                {"type": "tool_result", "tool_use_id": str(index), "content": output}
            ]
            records.extend([call, result])
    write(root / "chat.jsonl", codex_records(*records) if client == "codex" else records)
    run()
    captured = [e["content"] for e in events(sent)]
    assert "transcript search result" not in captured
    assert "page search result" in captured
    assert "Do not persist this query" not in json.dumps(captured)
