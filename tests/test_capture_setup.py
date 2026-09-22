"""Browser pairing is private, bounded, client-scoped and safe to retry."""

from __future__ import annotations

import json
import stat
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import capture_config as credentials
import capture_pairing as pairing
import capture_setup as setup
import conversation_capture as capture
import pytest

OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
OTHER = "173e0b53-8178-4a3c-8d40-a07414144741"
KEY = "synthetic-upload-only-key"
POLL_SECRET = "p" * 43
SESSION = "923bbd76-d864-4b8f-b252-c2b7c3692492"


@pytest.fixture
def service():
    state = {"mode": "pending", "requests": [], "owner": OWNER, "context": 497}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, body, self.headers.get("Authorization")))
            code = 200
            if self.path.endswith("/pairings/start"):
                code = 201
                identity = str(uuid.uuid4())
                state["id"] = identity
                response = {
                    "id": identity,
                    "poll_secret": POLL_SECRET,
                    "verification_url": "https://app.pensieve.uk/oauth/conversation-capture?pairing_id="
                    + identity,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                    "poll_interval_seconds": 5,
                }
            elif self.path.endswith("/exchange"):
                assert body == {"poll_secret": POLL_SECRET}
                mode = state["mode"]
                if mode == "approved":
                    response = {
                        "status": "approved",
                        "user_id": state["owner"],
                        "context_id": state["context"],
                        "installation_id": str(uuid.uuid4()),
                        "upload_key": KEY,
                    }
                    state["mode"] = "consumed"
                elif mode in {"expired", "consumed", "revoked"}:
                    code, response = 410, {"detail": POLL_SECRET}
                elif mode == "offline":
                    code, response = 503, {"detail": POLL_SECRET}
                else:
                    response = {"status": "pending"}
            elif self.path.endswith("/installations/heartbeat"):
                code, response = 204, None
            else:
                code, response = 404, None
            raw = json.dumps(response).encode() if response is not None else b""
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{server.server_port}/users/me/conversation-capture"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def new_config(tmp_path):
    return tmp_path / "private" / "capture.json"


def ready(config, client="codex"):
    path = pairing.pairing_path(config, client)
    state = json.loads(credentials.private_file(path, credentials.MAX_CONFIG_BYTES))
    state["next_poll_at"] = 0
    credentials.save_private_json(path, state)


def start(config, service, client="codex"):
    return pairing.start(config, client, base=service["base"])


def approve(config, service, client="codex"):
    start(config, service, client)
    service["mode"] = "approved"
    ready(config, client)
    return pairing.poll(config, client)


def test_pairing_returns_only_public_link_then_installs_private_client_credential(
    tmp_path, service
):
    config = new_config(tmp_path)
    public = start(config, service)
    assert public["status"] == "awaiting_approval"
    assert POLL_SECRET not in json.dumps(public)
    assert KEY not in json.dumps(public)
    assert credentials.profiles(config, "codex") == {}
    path = pairing.pairing_path(config, "codex")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    service["mode"] = "approved"
    result = pairing.poll(config, "codex")
    assert result["status"] == "paired"
    assert KEY not in json.dumps(result)
    assert credentials.profiles(config, "codex") == {OWNER: KEY}
    assert credentials.profiles(config, "claude") == {}
    assert not path.exists()
    assert pairing.poll(config, "codex")["status"] == "no_pending_pairing"
    assert len([r for r in service["requests"] if r[0].endswith("exchange")]) == 1
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert "enabled" not in config.read_text()


def test_pending_rate_limit_and_offline_retry_preserve_challenge(tmp_path, service):
    config = new_config(tmp_path)
    first = start(config, service)
    assert start(config, service) == first
    assert len(service["requests"]) == 1
    assert pairing.poll(config, "codex")["status"] == "awaiting_approval"
    assert pairing.poll(config, "codex")["status"] == "awaiting_approval"
    assert len(service["requests"]) == 2
    ready(config)
    service["mode"] = "offline"
    assert pairing.poll(config, "codex")["status"] == "offline"
    assert pairing.pairing_path(config, "codex").exists()
    ready(config)
    service["mode"] = "approved"
    assert pairing.poll(config, "codex")["status"] == "paired"


@pytest.mark.parametrize("mode", ["expired", "consumed", "revoked"])
def test_rejected_exchange_keeps_existing_credentials_and_requires_new_pairing(
    tmp_path, service, mode
):
    config = new_config(tmp_path)
    approve(config, service)
    before = config.read_bytes()
    start(config, service)
    service["mode"] = mode
    result = pairing.poll(config, "codex")
    assert result["status"] == "restart_required"
    assert config.read_bytes() == before
    assert POLL_SECRET not in json.dumps(result)
    assert not pairing.pairing_path(config, "codex").exists()


def test_expired_local_challenge_never_calls_exchange(tmp_path, service):
    config = new_config(tmp_path)
    start(config, service)
    path = pairing.pairing_path(config, "codex")
    pending = json.loads(path.read_text())
    pending["expires_at"] = "2000-01-01T00:00:00+00:00"
    credentials.save_private_json(path, pending)
    assert pairing.poll(config, "codex")["status"] == "expired"
    assert len(service["requests"]) == 1


def test_new_accounts_and_clients_do_not_overwrite_or_broaden_each_other(tmp_path, service):
    config = new_config(tmp_path)
    approve(config, service)
    service.update(owner=OTHER, context=508)
    assert approve(config, service)["context_id"] == 508
    assert credentials.profiles(config, "codex") == {OWNER: KEY, OTHER: KEY}
    assert credentials.profiles(config, "claude") == {}
    approve(config, service, "claude")
    assert credentials.profiles(config, "claude") == {OTHER: KEY}
    assert credentials.profiles(config, "codex") == {OWNER: KEY, OTHER: KEY}


@pytest.mark.parametrize("version", [3])
def test_obsolete_config_requires_reconnect_without_rewriting_or_reading_transcripts(
    tmp_path, monkeypatch, version
):
    config = new_config(tmp_path)
    obsolete = {"user_id": OWNER, "upload_key": KEY}
    if version == 3:
        obsolete.update(client="codex", installation_id=None, runtime="unknown", host_version="")
    credentials.save_private_json(config, {"version": version, "profiles": [obsolete]})
    before = config.read_bytes()
    for client in ("claude", "codex"):
        with pytest.raises(credentials.ReconnectRequired, match="Reconnect through Conversations"):
            credentials.profiles(config, client)
    monkeypatch.setattr(capture, "scan", lambda *a, **kw: pytest.fail("old setup read history"))
    monkeypatch.setattr(capture, "upload", lambda *a, **kw: pytest.fail("old setup uploaded"))
    with pytest.raises(credentials.ReconnectRequired):
        capture.run_hook(
            {"session_id": SESSION, "hook_event_name": "Stop", "transcript_path": "/unused"},
            "codex",
            config,
            tmp_path / "spool",
        )
    assert config.read_bytes() == before
    assert not (tmp_path / "spool").exists()


@pytest.mark.parametrize("version", [3])
@pytest.mark.parametrize("action", ["status"])
def test_old_setup_has_actionable_secret_free_cli_status(
    tmp_path, monkeypatch, capsys, version, action
):
    config = new_config(tmp_path)
    obsolete = {"user_id": OWNER, "upload_key": KEY}
    if version == 3:
        obsolete.update(client="codex", installation_id=None, runtime="unknown", host_version="")
    credentials.save_private_json(config, {"version": version, "profiles": [obsolete]})
    monkeypatch.setattr("sys.argv", ["setup", action, "--client", "codex", "--config", str(config)])
    setup.main()
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "status": "reconnect_required",
        "message": "Reconnect through Conversations to save work with this app.",
    }
    assert KEY not in output


@pytest.mark.parametrize("version", [2, 3])
def test_browser_approved_reconnect_replaces_obsolete_keys_and_preserves_paired_profiles(
    tmp_path, service, version
):
    config = new_config(tmp_path)
    obsolete = {"user_id": OWNER, "upload_key": "obsolete-synthetic-key"}
    preserved = []
    if version == 3:
        approve(config, service, "claude")
        preserved = json.loads(config.read_text())["profiles"]
        obsolete.update(client="codex", installation_id=None, runtime="unknown", host_version="")
    credentials.save_private_json(config, {"version": version, "profiles": preserved + [obsolete]})
    # Explicit setup may replace credentials, never the existing transcript spool.
    spool = tmp_path / "spool.sqlite3"
    spool.write_bytes(b"synthetic durable transcript state")
    before = config.read_bytes()
    service["mode"] = "pending"
    start(config, service)
    assert config.read_bytes() == before  # Starting/awaiting approval grants nothing.
    assert pairing.poll(config, "codex")["status"] == "awaiting_approval"
    assert config.read_bytes() == before
    service["mode"] = "approved"
    ready(config, "codex")
    assert pairing.poll(config, "codex")["status"] == "paired"
    value = credentials.load_config(config, "codex")
    assert credentials.profiles(config, "codex") == {OWNER: KEY}
    assert [p for p in value["profiles"] if p["client"] == "claude"] == preserved
    assert all(credentials.valid_uuid(p["installation_id"]) for p in value["profiles"])
    assert b"obsolete-synthetic-key" not in config.read_bytes()
    assert spool.read_bytes() == b"synthetic durable transcript state"


def test_insecure_or_symlink_state_refuses_to_read_secrets(tmp_path, service):
    config = new_config(tmp_path)
    start(config, service)
    path = pairing.pairing_path(config, "codex")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        pairing.poll(config, "codex")
    path.chmod(0o600)
    original = path.with_suffix(".real")
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(OSError):
        pairing.poll(config, "codex")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.invalid/users/me/conversation-capture",
        "http://127.0.0.1:1234/other",
        "http://user:pass@127.0.0.1/users/me/conversation-capture",
        "http://127.0.0.1/users/me/conversation-capture?secret=oops",
    ],
)
def test_pairing_never_sends_secrets_to_unapproved_endpoint(endpoint):
    with pytest.raises(ValueError):
        pairing.checked_base(endpoint)


def test_cli_never_prints_response_credentials(tmp_path, service, monkeypatch, capsys):
    config = new_config(tmp_path)
    args = [
        "setup",
        "start",
        "--client",
        "codex",
        "--config",
        str(config),
        "--endpoint",
        service["base"],
    ]
    monkeypatch.setattr("sys.argv", args)
    setup.main()
    assert "verification_url" in capsys.readouterr().out
    service["mode"] = "approved"
    monkeypatch.setattr("sys.argv", ["setup", "poll", "--client", "codex", "--config", str(config)])
    setup.main()
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "paired"
    assert KEY not in output and POLL_SECRET not in output


def test_hook_finishes_pairing_without_reading_or_backfilling_old_work(
    tmp_path, service, monkeypatch
):
    config = new_config(tmp_path)
    start(config, service)
    service["mode"] = "approved"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": SESSION}})
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Old private work"},
            }
        )
        + "\n"
    )
    monkeypatch.setattr(
        capture, "upload", lambda *a, **kw: pytest.fail("pairing backfilled prior work")
    )
    assert (
        capture.run_hook(
            {"session_id": SESSION, "hook_event_name": "Stop", "transcript_path": str(transcript)},
            "codex",
            config,
            tmp_path / "spool",
        )
        == {}
    )
    assert credentials.profiles(config, "codex") == {OWNER: KEY}


def test_concurrent_hook_cannot_claim_a_pairing_while_setup_holds_it(tmp_path, service):
    config = new_config(tmp_path)
    start(config, service)
    service["mode"] = "approved"
    lock = pairing.pairing_path(config, "codex").with_suffix(".lock")
    with credentials.private_lock(lock):
        with pytest.raises(BlockingIOError):
            pairing.poll(config, "codex")
    assert len(service["requests"]) == 1
    assert pairing.poll(config, "codex")["status"] == "paired"


def test_legacy_profile_remains_readable_without_broadening_new_pairing(tmp_path):
    config = new_config(tmp_path)
    credentials.save_private_json(
        config, {"version": 2, "profiles": [{"user_id": OWNER, "upload_key": KEY}]}
    )
    before = config.read_bytes()
    assert credentials.profiles(config, "codex") == {OWNER: KEY}
    assert credentials.profiles(config, "claude") == {OWNER: KEY}
    assert config.read_bytes() == before
