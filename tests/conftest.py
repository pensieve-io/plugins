import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture(autouse=True)
def no_live_onboarding(monkeypatch):
    # Fixture native markers must never open a real browser page.
    monkeypatch.setattr("capture_onboarding.open_page", lambda url: None)


@pytest.fixture
def isolated_git(monkeypatch, tmp_path):
    # The member's own git configuration (signing, hooks, templates) never applies.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{name}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{name}_EMAIL", "fixture@example.com")


@pytest.fixture
def anchor_service():
    state = {"status": 200, "body": {"anchor_id": "a", "conversation_id": "c", "created": True}}
    state["requests"] = []

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
    state["endpoint"] = f"http://127.0.0.1:{server.server_port}/hooks/conversations"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
