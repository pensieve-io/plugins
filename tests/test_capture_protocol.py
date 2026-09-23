"""Compatibility failures cannot consume queued work or leak upload credentials."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import capture_protocol as protocol
import conversation_capture as capture
import pytest
from test_conversation_capture import SESSION, append, assistant, hook_record, pending, setup, user

MANIFEST = {
    "protocol_version": 1,
    "service": "upload",
    "clients": ["codex", "claude"],
    "max_batch_bytes": 262144,
    "max_events": 100,
    "max_content_chars": 32000,
}


@pytest.mark.parametrize(
    "failure", [404, 503, "version", "client", "limit", "malformed", "oversized"]
)
def test_incompatible_service_preserves_exact_outbox_then_recovers(tmp_path, monkeypatch, failure):
    real_upload = capture.upload
    path, cfg, root, _, run = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, "upload", lambda *args: False)
    append(path, user(), hook_record(), assistant())
    run()
    db = capture.connect_state(root, "codex", SESSION)
    before = tuple(db.execute("SELECT body,sha,event_ids FROM batches").fetchone())
    db.close()
    state = {"failure": failure, "uploads": [], "gets": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.headers.get("Authorization") is None
            state["gets"] += 1
            value = dict(MANIFEST)
            problem = state["failure"]
            if problem == "version":
                value["protocol_version"] = 99
            if problem == "client":
                value["clients"] = ["codex"]
            if problem == "limit":
                value["max_events"] = 1
            raw = (
                b"bad"
                if problem == "malformed"
                else b" " * 4097
                if problem == "oversized"
                else json.dumps(value).encode()
            )
            self.send_response(problem if type(problem) is int else 200)
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            assert self.headers[protocol.HEADER] == "1"
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            state["uploads"].append(raw)
            batch = json.loads(raw)
            result = {
                "batch_id": batch["batch_id"],
                "batch_sha256": hashlib.sha256(raw).hexdigest(),
                "segment_id": batch["segment_id"],
                "conversation_id": SESSION,
                "accepted_events": len(batch["events"]),
                "expires_at": None,
            }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(capture, "upload", real_upload)
    endpoint = f"http://127.0.0.1:{server.server_port}/hooks/conversations"

    def retry():
        capture.run_hook(
            {"session_id": SESSION, "hook_event_name": "Stop"}, "codex", cfg, root, endpoint
        )

    try:
        protocol._cache.clear()
        retry()
        retry()
        assert state["gets"] == 1 and not state["uploads"]
        db = capture.connect_state(root, "codex", SESSION)
        assert tuple(db.execute("SELECT body,sha,event_ids FROM batches").fetchone()) == before
        db.close()
        assert pending(root) == 2
        status = capture.saving_status(root, "codex", SESSION)["segments"][0]
        assert status["last_saved_at"] is None and status["queued_events"] == 2
        state["failure"] = None
        protocol._cache.clear()
        retry()
        assert state["uploads"] == [before[0]] and pending(root) == 0
        status = capture.saving_status(root, "codex", SESSION)["segments"][0]
        assert status["status"] == "accepted" and status["last_saved_at"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        protocol._cache.clear()


def test_status_does_not_create_capture_state(tmp_path):
    root = tmp_path / "absent"
    assert capture.saving_status(root, "codex", SESSION)["status"] == "not_started"
    assert not root.exists()
