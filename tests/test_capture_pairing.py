"""Pairing is private, bounded, client-scoped and safe to retry.

A pending claim is registered by the member's signed-in MCP call: hooks bind it
without a credential, and a later hook exchanges it for a scoped device key.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import capture_config as credentials
import capture_onboarding as onboarding
import capture_pairing as pairing
import context_briefing as briefing
import conversation_capture as capture
import pytest

OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
OTHER = "173e0b53-8178-4a3c-8d40-a07414144741"
KEY = "synthetic-upload-only-key"
POLL_SECRET = "p" * 43
SESSION = "923bbd76-d864-4b8f-b252-c2b7c3692492"


@pytest.fixture
def service():
    state = {"mode": "pending", "requests": [], "hooks": [], "owner": OWNER, "context": 497}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            raw = json.dumps(
                {
                    "protocol_version": 1,
                    "service": "pairing",
                    "clients": ["codex", "claude"],
                    "max_batch_bytes": 262144,
                    "max_events": 100,
                    "max_content_chars": 32000,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def reply(self, code, response, headers=()):
            raw = json.dumps(response).encode() if response is not None else b""
            self.send_response(code)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def hook(self, raw, body):
            # MCP-side hook endpoints. Record exactly what crossed the wire.
            headers = {k.lower(): v for k, v in self.headers.items()}
            state["hooks"].append((self.path, body, headers, raw))
            if self.path == "/hooks/enrolment":
                code = state.get("enrolment_status", 204)
                location = [("Location", state["root"] + "/hooks/briefing")]
                return self.reply(code, None, location if 300 <= code < 400 else ())
            if self.path == "/hooks/tool-binding":
                return self.reply(204, None)
            if self.path == "/hooks/briefing":
                return self.reply(
                    200,
                    {
                        "hookSpecificOutput": {
                            "hookEventName": body["event"],
                            "additionalContext": "company primer",
                        }
                    },
                )
            return self.reply(404, None)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            if self.path.startswith("/hooks/"):
                return self.hook(raw, body)
            state["requests"].append((self.path, body, self.headers.get("Authorization")))
            code = 200
            if self.path.endswith("/pairings/start"):
                code = 201
                identity = str(uuid.uuid4())
                state["id"] = identity
                response = {
                    "id": identity,
                    "poll_secret": POLL_SECRET,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                    "poll_interval_seconds": 5,
                }
            elif self.path.endswith("/exchange"):
                assert body == {"poll_secret": POLL_SECRET}
                mode = state["mode"]
                # "approved" was the retired browser status; it must never install.
                if mode in {"registered", "approved"}:
                    response = {
                        "status": mode,
                        "user_id": state["owner"],
                        "context_id": state["context"],
                        "installation_id": str(uuid.uuid4()),
                        "upload_key": KEY,
                        "briefing_enabled": state.get("briefing_enabled", True),
                    }
                    state["mode"] = "consumed"
                elif mode in {"expired", "consumed", "revoked"}:
                    code, response = 410, {"detail": POLL_SECRET}
                elif mode == "offline":
                    code, response = 503, {"detail": POLL_SECRET}
                else:
                    response = {"status": "pending"}
                time.sleep(state.get("exchange_delay", 0))
            elif self.path.endswith("/installations/heartbeat"):
                code, response = 204, None
            else:
                code, response = 404, None
            self.reply(code, response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["root"] = f"http://127.0.0.1:{server.server_port}"
    state["base"] = state["root"] + "/users/me/conversation-capture"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def new_config(tmp_path):
    return tmp_path / "private" / "capture.json"


def existing_config(tmp_path):
    # An installation whose private directory already exists.
    config = new_config(tmp_path)
    credentials.private_directory(config.parent)
    return config


def ready(config, client="codex"):
    path = pairing.pairing_path(config, client)
    state = json.loads(credentials.private_file(path, credentials.MAX_CONFIG_BYTES))
    state["next_poll_at"] = 0
    credentials.save_private_json(path, state)


def start(config, service, client="codex"):
    return pairing.start(config, client, base=service["base"])


def register(config, service, client="codex"):
    start(config, service, client)
    service["mode"] = "registered"
    ready(config, client)
    return pairing.poll(config, client)


def endpoints(service, enrolment=None):
    root = service["root"]
    return {
        "endpoint": root + "/hooks/briefing",
        "binding_endpoint": root + "/hooks/tool-binding",
        "enrolment_endpoint": enrolment or root + "/hooks/enrolment",
        "pairing_base": service["base"],
    }


def tool_call(tool_use_id, session=SESSION):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": "mcp__plugin_pensieve_pensieve__read",
        "tool_use_id": tool_use_id,
        "mcp_server": {"name": "plugin:pensieve:pensieve", "source": "plugin"},
    }


def starts(service):
    return [body for path, body, _ in service["requests"] if path.endswith("/pairings/start")]


def closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def no_browser(monkeypatch):
    def fail(*args, **_kwargs):
        pytest.fail(f"opened a browser: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", fail)
    monkeypatch.setattr(onboarding, "open_page", fail)


def test_pairing_returns_only_public_status_then_installs_private_client_credential(
    tmp_path, service
):
    config = new_config(tmp_path)
    public = start(config, service)
    path = pairing.pairing_path(config, "codex")
    claim = json.loads(path.read_text())
    assert public == {
        "status": "awaiting_registration",
        "expires_at": claim["expires_at"],
        "client": "codex",
    }
    # Registration happens through MCP sign-in: nothing asks for a browser screen
    # or a separately requested briefing scope.
    assert "briefing_enabled" not in starts(service)[0]
    assert starts(service)[0]["plugin_version"] == pairing.PLUGIN_VERSION == "mcp-handoff-1"
    assert not {"verification_url", "briefing_enabled"} & set(claim)
    assert POLL_SECRET not in json.dumps(public)
    assert KEY not in json.dumps(public)
    assert credentials.profiles(config, "codex") == {}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    service["mode"] = "registered"
    result = pairing.poll(config, "codex")
    assert result["status"] == "paired"
    assert KEY not in json.dumps(result)
    assert credentials.profiles(config, "codex") == {f"{OWNER}:497": KEY}
    assert credentials.profiles(config, "claude") == {}
    assert not path.exists()
    assert pairing.poll(config, "codex")["status"] == "no_pending_pairing"
    assert len([r for r in service["requests"] if r[0].endswith("exchange")]) == 1
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert json.loads(config.read_text())["profiles"][0]["briefing_enabled"] is True


def test_pending_rate_limit_and_offline_retry_preserve_challenge(tmp_path, service):
    config = new_config(tmp_path)
    first = start(config, service)
    assert start(config, service) == first
    assert len(service["requests"]) == 1
    assert pairing.poll(config, "codex")["status"] == "awaiting_registration"
    assert pairing.poll(config, "codex")["status"] == "awaiting_registration"
    assert len(service["requests"]) == 2
    ready(config)
    service["mode"] = "offline"
    assert pairing.poll(config, "codex")["status"] == "offline"
    assert pairing.pairing_path(config, "codex").exists()
    ready(config)
    service["mode"] = "registered"
    assert pairing.poll(config, "codex")["status"] == "paired"


def test_retired_approval_status_never_installs_a_credential(tmp_path, service):
    config = new_config(tmp_path)
    start(config, service)
    service["mode"] = "approved"
    assert pairing.poll(config, "codex")["status"] == "offline"
    assert credentials.profiles(config, "codex") == {}
    assert not config.exists()
    assert pairing.pairing_path(config, "codex").exists()


@pytest.mark.parametrize("granted", [True, False, None, "true"])
def test_briefing_permission_is_exactly_what_the_registration_grants(tmp_path, service, granted):
    config = new_config(tmp_path)
    start(config, service)
    service.update(mode="registered", briefing_enabled=granted)
    assert pairing.poll(config, "codex")["briefing_enabled"] is (granted is True)
    profile = credentials.load_config(config, "codex")["profiles"][0]
    assert profile["briefing_enabled"] is (granted is True)


@pytest.mark.parametrize("consumer", ["capture", "other_conversation"])
def test_registration_survives_another_hook_exchanging_it(tmp_path, service, no_browser, consumer):
    config = new_config(tmp_path)
    for owner, context in [(OWNER, 497), (OTHER, 529)]:
        credentials.install_profile(
            config,
            {
                "user_id": owner,
                "context_id": context,
                "client": "codex",
                "upload_key": KEY + str(context),
                "installation_id": str(uuid.uuid4()),
                "runtime": "unknown",
                "host_version": "",
                "briefing_enabled": True,
            },
        )
    hooks = endpoints(service)
    event = {"hook_event_name": "UserPromptSubmit", "session_id": SESSION}
    # Two accounts are ambiguous: never guess, keep a claim for this thread.
    first = briefing.run_hook(event, "codex", config, **hooks)
    assert briefing.CONNECTING in str(first)
    pending = json.loads(pairing.pairing_path(config, "codex").read_text())
    assert pending["session_id"] == SESSION
    assert "session_id" not in starts(service)[0]
    assert [path for path, *_ in service["hooks"]] == ["/hooks/enrolment"]
    service.update(mode="registered", owner=OTHER, context=529)
    other_session = str(uuid.uuid4())
    if consumer == "capture":
        capture.run_hook(
            {"hook_event_name": "Stop", "session_id": other_session},
            "codex",
            config,
            tmp_path / "spool",
        )
    else:
        # A concurrent briefing may finish the exchange, but cannot treat that
        # registration as its own permission to select an account.
        result = briefing.run_hook(dict(event, session_id=other_session), "codex", config, **hooks)
        assert "company primer" not in str(result)
        assert (
            briefing.read_state(briefing.state_path(config, "codex", other_session)).get("user_id")
            is None
        )
    assert not any(path == "/hooks/briefing" for path, *_ in service["hooks"])
    receipt = pairing.completed_pairing(config, "codex", SESSION)
    assert receipt == {"pairing_id": pending["id"], "user_id": OTHER, "context_id": 529}
    assert pairing.completed_pairing(config, "codex", other_session) == {}
    assert pairing.completed_pairing(config, "claude", SESSION) == {}
    assert KEY not in json.dumps(receipt) and POLL_SECRET not in json.dumps(receipt)
    result = briefing.run_hook(event, "codex", config, **hooks)
    assert "company primer" in str(result)
    assert service["hooks"][-1][0] == "/hooks/briefing"
    assert service["hooks"][-1][2]["authorization"] == "Bearer " + KEY
    saved = briefing.read_state(briefing.state_path(config, "codex", SESSION))
    assert saved["user_id"] == OTHER and saved["pairing_id"] == pending["id"]

    # Once adopted, the durable receipt must not undo a later context choice.
    saved.update(context_id=601, pending_context_id=601)
    credentials.save_private_json(briefing.state_path(config, "codex", SESSION), saved)
    briefing.run_hook(event, "codex", config, **hooks)
    assert briefing.read_state(briefing.state_path(config, "codex", SESSION))["context_id"] == 601


@pytest.mark.parametrize("mode", ["expired", "consumed", "revoked"])
def test_rejected_exchange_keeps_existing_credentials_and_requires_new_pairing(
    tmp_path, service, mode
):
    config = new_config(tmp_path)
    register(config, service)
    before = config.read_bytes()
    start(config, service)
    service["mode"] = mode
    result = pairing.poll(config, "codex")
    assert result["status"] == "restart_required"
    assert config.read_bytes() == before
    assert POLL_SECRET not in json.dumps(result)
    assert not pairing.pairing_path(config, "codex").exists()


def test_expired_claim_can_still_finish_a_registration(tmp_path, service):
    config = new_config(tmp_path)
    start(config, service)
    path = pairing.pairing_path(config, "codex")
    pending = json.loads(path.read_text())
    pending["expires_at"] = "2000-01-01T00:00:00+00:00"
    credentials.save_private_json(path, pending)
    service["mode"] = "registered"
    assert pairing.poll(config, "codex")["status"] == "paired"
    assert len(service["requests"]) == 2
    assert credentials.profiles(config, "codex")
    assert POLL_SECRET not in json.dumps(credentials.profiles(config, "codex"))


def test_new_accounts_and_clients_do_not_overwrite_or_broaden_each_other(tmp_path, service):
    config = new_config(tmp_path)
    register(config, service)
    service.update(owner=OTHER, context=508)
    assert register(config, service)["context_id"] == 508
    assert credentials.profiles(config, "codex") == {f"{OWNER}:497": KEY, f"{OTHER}:508": KEY}
    assert credentials.profiles(config, "claude") == {}
    register(config, service, "claude")
    assert credentials.profiles(config, "claude") == {f"{OTHER}:508": KEY}
    assert credentials.profiles(config, "codex") == {f"{OWNER}:497": KEY, f"{OTHER}:508": KEY}


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
        with pytest.raises(
            credentials.ReconnectRequired, match="Reconnect through Data → Connectors"
        ):
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


@pytest.mark.parametrize("version", [2, 3])
def test_registered_reconnect_replaces_obsolete_keys_and_preserves_paired_profiles(
    tmp_path, service, version
):
    config = new_config(tmp_path)
    obsolete = {"user_id": OWNER, "upload_key": "obsolete-synthetic-key"}
    preserved = []
    if version == 3:
        register(config, service, "claude")
        preserved = json.loads(config.read_text())["profiles"]
        obsolete.update(client="codex", installation_id=None, runtime="unknown", host_version="")
    credentials.save_private_json(config, {"version": version, "profiles": preserved + [obsolete]})
    # Explicit setup may replace credentials, never the existing transcript spool.
    spool = tmp_path / "spool.sqlite3"
    spool.write_bytes(b"synthetic durable transcript state")
    before = config.read_bytes()
    service["mode"] = "pending"
    start(config, service)
    assert config.read_bytes() == before  # Starting/awaiting registration grants nothing.
    assert pairing.poll(config, "codex")["status"] == "awaiting_registration"
    assert config.read_bytes() == before
    service["mode"] = "registered"
    ready(config, "codex")
    assert pairing.poll(config, "codex")["status"] == "paired"
    value = credentials.load_config(config, "codex")
    assert credentials.profiles(config, "codex") == {f"{OWNER}:497": KEY}
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
    with pytest.raises(ValueError, match="0600"):
        pairing.pending_claim(config, "codex")
    path.chmod(0o600)
    original = path.with_suffix(".real")
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(OSError):
        pairing.poll(config, "codex")
    with pytest.raises(OSError):
        pairing.pending_claim(config, "codex")
    with pytest.raises(OSError):
        pairing.discard_claim(config, "codex", service["id"])
    assert original.exists()


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


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_hook_finishes_hosted_latency_pairing_without_backfilling_old_work(
    tmp_path, service, monkeypatch, client
):
    config = new_config(tmp_path)
    start(config, service, client)
    # The service commits its single-use exchange before sending the response.
    # Ordinary hosted latency must not consume a credential the hook cannot save.
    service.update(mode="registered", exchange_delay=0.35)
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
            client,
            config,
            tmp_path / "spool",
        )
        == {}
    )
    assert credentials.profiles(config, client) == {f"{OWNER}:497": KEY}
    assert not pairing.pairing_path(config, client).exists()


def test_session_end_preserves_registered_claim_for_next_ordinary_hook(tmp_path, service):
    config = new_config(tmp_path)
    start(config, service)
    service["mode"] = "registered"
    capture.run_hook(
        {"session_id": SESSION, "hook_event_name": "SessionEnd"},
        "codex",
        config,
        tmp_path / "spool",
    )
    assert credentials.profiles(config, "codex") == {}
    assert pairing.pairing_path(config, "codex").exists()
    assert not any(path.endswith("/exchange") for path, _, _ in service["requests"])


def test_concurrent_hook_cannot_claim_a_pairing_while_setup_holds_it(tmp_path, service):
    config = new_config(tmp_path)
    start(config, service)
    service["mode"] = "registered"
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


@pytest.mark.parametrize("wrong", ["owner", "context"])
def test_bound_pairing_never_installs_another_registered_identity(tmp_path, service, wrong):
    config = new_config(tmp_path)
    pairing.start(
        config, "codex", base=service["base"], expected_user_id=OWNER, expected_context_id=497
    )
    service.update(mode="registered", **{wrong: OTHER if wrong == "owner" else 508})
    with pytest.raises(ValueError, match="Registration does not match the initiating connection"):
        pairing.poll(config, "codex")
    assert credentials.profiles(config, "codex") == {}


def test_capture_hook_never_starts_pairing_and_registration_never_backfills(
    tmp_path, service, monkeypatch
):
    from test_conversation_capture import append, hook_record, user

    config = new_config(tmp_path)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")
    append(
        transcript,
        {"type": "session_meta", "payload": {"id": SESSION}},
        user("Old private work"),
        hook_record(generation=None),
    )
    record = hook_record(generation=None)
    record["payload"]["content"][0]["text"] += (
        "\n<!-- pensieve-capture-consent "
        + json.dumps(
            {
                "user_id": OWNER,
                "client": "codex",
                "context_id": 497,
                "conversation_id": SESSION,
                "status": "unknown",
            }
        )
        + " -->"
    )
    append(transcript, record)
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    opened = []
    monkeypatch.setattr(onboarding, "open_page", opened.append)
    monkeypatch.setattr(
        capture, "upload", lambda *args: pytest.fail("onboarding imported old history")
    )
    payload = {"session_id": SESSION, "hook_event_name": "Stop", "transcript_path": str(transcript)}
    # Connecting is MCP's job: without a key, capture neither pairs nor asks.
    capture.run_hook(payload, "codex", config, tmp_path / "spool")
    assert service["requests"] == [] and opened == []
    assert not config.exists()
    assert not pairing.pairing_path(config, "codex").exists()
    # The briefing hook's claim is registered by the signed-in MCP call; the
    # next ordinary capture hook exchanges it without uploading earlier work.
    pairing.start(
        config,
        "codex",
        base=service["base"],
        expected_user_id=OWNER,
        expected_context_id=497,
        session_id=SESSION,
    )
    service["mode"] = "registered"
    capture.run_hook(payload, "codex", config, tmp_path / "spool")
    assert credentials.key_for(credentials.profiles(config, "codex"), OWNER, 497) == KEY
    # Only now can the sharing choice be recorded, and it is asked once.
    assert opened == [f"{onboarding.CONSENT_PAGE}?client=codex&context_id=497"]
    capture.run_hook(payload, "codex", config, tmp_path / "spool")
    assert len(opened) == 1


def test_automatic_second_context_waits_for_pending_registration(tmp_path, service):
    config = new_config(tmp_path)
    first = pairing.start(
        config, "codex", base=service["base"], expected_user_id=OWNER, expected_context_id=497
    )
    second = pairing.start(
        config, "codex", base=service["base"], expected_user_id=OWNER, expected_context_id=508
    )
    assert first["status"] == "awaiting_registration"
    assert second["status"] == "another_connection_pending"
    assert len(service["requests"]) == 1
    service["mode"] = "registered"
    result = pairing.poll(config, "codex")
    assert result["context_id"] == 497


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("next_scope", [(OWNER, 497), (OWNER, 508), (OTHER, 497)])
def test_start_preserves_unclaimed_credentials_across_scope_and_expiry(
    tmp_path, service, expired, next_scope
):
    config = new_config(tmp_path)
    pairing.start(
        config, "codex", base=service["base"], expected_user_id=OWNER, expected_context_id=497
    )
    path = pairing.pairing_path(config, "codex")
    if expired:
        pending = json.loads(path.read_text())
        pending["expires_at"] = "2000-01-01T00:00:00+00:00"
        credentials.save_private_json(path, pending)
    service["mode"] = "offline"
    assert pairing.poll(config, "codex")["status"] == "offline"
    before = path.read_bytes()
    result = pairing.start(
        config,
        "codex",
        base=service["base"],
        expected_user_id=next_scope[0],
        expected_context_id=next_scope[1],
    )
    assert path.read_bytes() == before
    assert len([r for r in service["requests"] if r[0].endswith("/start")]) == 1
    if next_scope == (OWNER, 497):
        assert result == pairing.public_status(json.loads(before))
    else:
        assert result["status"] == "another_connection_pending"
    service["mode"] = "registered"
    ready(config)
    assert pairing.poll(config, "codex")["status"] == "paired"
    assert credentials.profiles(config, "codex") == {f"{OWNER}:497": KEY}
    assert not path.exists()
    assert (
        pairing.start(
            config,
            "codex",
            base=service["base"],
            expected_user_id=next_scope[0],
            expected_context_id=next_scope[1],
        )["status"]
        == "awaiting_registration"
    )


@pytest.mark.parametrize("status", ["incompatible", "unavailable", 404, 426])
def test_incompatible_service_preserves_private_pending_claim(
    tmp_path, service, monkeypatch, status
):
    config = new_config(tmp_path)
    pairing.start(config, "codex", "codex_cli", base=service["base"])
    ready(config)
    path = pairing.pairing_path(config, "codex")
    pending = json.loads(path.read_text())
    monkeypatch.setattr(pairing, "request", lambda *args, **kwargs: (status, None))
    assert pairing.poll(config, "codex")["status"] in {"incompatible", "offline"}
    assert path.exists() and json.loads(path.read_text())["poll_secret"] == pending["poll_secret"]
    assert pairing.start(config, "codex", "codex_cli", base=service["base"]) == (
        pairing.public_status(pending)
    )
    assert len(starts(service)) == 1


def test_pending_claim_is_read_privately_and_discard_spares_a_replacement(tmp_path, service):
    config = new_config(tmp_path)
    assert pairing.pending_claim(config, "codex") is None
    start(config, service)
    claim = pairing.pending_claim(config, "codex")
    assert claim["id"] == service["id"] and claim["poll_secret"] == POLL_SECRET
    assert pairing.pending_claim(config, "claude") is None
    # A stale closure report must not drop a claim another hook replaced.
    pairing.discard_claim(config, "codex", str(uuid.uuid4()))
    assert pairing.pending_claim(config, "codex") == claim
    with credentials.private_lock(pairing.pairing_path(config, "codex").with_suffix(".lock")):
        with pytest.raises(BlockingIOError):
            pairing.discard_claim(config, "codex", claim["id"])
    assert pairing.pending_claim(config, "codex") == claim
    pairing.discard_claim(config, "codex", claim["id"])
    assert pairing.pending_claim(config, "codex") is None
    pairing.discard_claim(config, "codex", claim["id"])


def test_claude_tool_call_without_credential_binds_its_exact_call_and_is_allowed(
    tmp_path, service, no_browser
):
    config = new_config(tmp_path)
    hooks = endpoints(service)
    assert briefing.run_hook(tool_call("toolu_first"), "claude", config, **hooks) == {}
    claim = pairing.pending_claim(config, "claude")
    assert len(starts(service)) == 1 and claim["session_id"] == SESSION
    path, body, headers, _ = service["hooks"][0]
    assert path == "/hooks/enrolment"
    assert body == {
        "pairing_id": claim["id"],
        "poll_secret": POLL_SECRET,
        "client": "claude",
        "session_id": SESSION,
        "tool_use_id": "toolu_first",
    }
    assert "authorization" not in headers
    # A parallel call binds the same claim. Binding never exchanges or grants.
    assert briefing.run_hook(tool_call("toolu_second"), "claude", config, **hooks) == {}
    assert len(starts(service)) == 1
    assert service["hooks"][1][1] == dict(body, tool_use_id="toolu_second")
    assert not any(path.endswith("/exchange") for path, *_ in service["requests"])
    assert not config.exists()
    # The signed-in MCP call registered it: the next prompt exchanges and briefs.
    service["mode"] = "registered"
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": SESSION}
    result = briefing.run_hook(prompt, "claude", config, **hooks)
    assert result["hookSpecificOutput"]["additionalContext"] == "company primer"
    assert pairing.pending_claim(config, "claude") is None
    # With a credential, each call binds through the authenticated endpoint.
    assert briefing.run_hook(tool_call("toolu_third"), "claude", config, **hooks) == {}
    path, body, headers, _ = service["hooks"][-1]
    assert path == "/hooks/tool-binding"
    assert body == {"client": "claude", "session_id": SESSION, "tool_use_id": "toolu_third"}
    assert headers["authorization"] == "Bearer " + KEY
    assert [path for path, *_ in service["hooks"]].count("/hooks/enrolment") == 2
    # The poll secret only ever reaches enrolment and exchange.
    assert all(
        POLL_SECRET.encode() not in raw
        for path, _, _, raw in service["hooks"]
        if path != "/hooks/enrolment"
    )


@pytest.mark.parametrize("failure", ["unreachable", 503, 307, "pairing_unavailable", "locked"])
def test_enrolment_failure_never_blocks_the_tool_call(tmp_path, service, no_browser, failure):
    config = new_config(tmp_path)
    hooks = endpoints(service)
    if failure == "unreachable":
        hooks["enrolment_endpoint"] = f"http://127.0.0.1:{closed_port()}/hooks/enrolment"
    elif failure == "pairing_unavailable":
        hooks["pairing_base"] = f"http://localhost:{closed_port()}/users/me/conversation-capture"
    elif failure != "locked":
        service["enrolment_status"] = failure
    if failure == "locked":
        # Another hook holds the pairing lock: never wait inside the deadline.
        lock = pairing.pairing_path(config, "claude").with_suffix(".lock")
        with credentials.private_lock(lock):
            assert briefing.run_hook(tool_call("toolu_first"), "claude", config, **hooks) == {}
    else:
        assert briefing.run_hook(tool_call("toolu_first"), "claude", config, **hooks) == {}
    # A retryable failure keeps the claim for the next call; a redirect is
    # never followed with the poll secret.
    kept = failure in {"unreachable", 503, 307}
    assert (pairing.pending_claim(config, "claude") is not None) is kept
    reached = ["/hooks/enrolment"] if failure in {503, 307} else []
    assert [path for path, *_ in service["hooks"]] == reached
    assert not config.exists()


def test_closed_claim_is_discarded_and_the_next_call_starts_afresh(tmp_path, service, no_browser):
    config = new_config(tmp_path)
    hooks = endpoints(service)
    service["enrolment_status"] = 410
    assert briefing.run_hook(tool_call("toolu_first"), "claude", config, **hooks) == {}
    closed = service["hooks"][0][1]["pairing_id"]
    assert pairing.pending_claim(config, "claude") is None
    service["enrolment_status"] = 204
    assert briefing.run_hook(tool_call("toolu_second"), "claude", config, **hooks) == {}
    claim = pairing.pending_claim(config, "claude")
    assert len(starts(service)) == 2 and claim["id"] != closed
    assert service["hooks"][1][1]["pairing_id"] == claim["id"]
    assert service["hooks"][1][1]["tool_use_id"] == "toolu_second"


def test_enrolment_carries_only_the_claim_secret(tmp_path, service, no_browser):
    config = new_config(tmp_path)
    # An upload-only installation holds a key but no briefing permission.
    credentials.install_profile(
        config,
        {
            "user_id": OWNER,
            "context_id": 497,
            "client": "claude",
            "upload_key": KEY,
            "installation_id": str(uuid.uuid4()),
            "runtime": "unknown",
            "host_version": "",
        },
    )
    hooks = endpoints(service)
    assert briefing.run_hook(tool_call("toolu_first"), "claude", config, **hooks) == {}
    [(path, body, headers, raw)] = service["hooks"]
    assert path == "/hooks/enrolment"
    assert "authorization" not in headers
    assert KEY.encode() not in raw and KEY not in json.dumps(headers)
    assert set(body) == {"pairing_id", "poll_secret", "client", "session_id", "tool_use_id"}
    assert body["poll_secret"] == POLL_SECRET


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
@pytest.mark.parametrize("client", ["codex", "claude"])
def test_lifecycle_without_credential_keeps_one_claim_and_explains_connecting(
    tmp_path, service, no_browser, client, event
):
    config = existing_config(tmp_path)
    hooks = endpoints(service)
    payload = {"hook_event_name": event, "session_id": SESSION}
    result = briefing.run_hook(payload, client, config, **hooks)
    assert result == {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": briefing.fallback_primer(client, SESSION)
            + " "
            + briefing.CONNECTING,
        }
    }
    claim = pairing.pending_claim(config, client)
    assert claim["session_id"] == SESSION
    if client == "codex":
        # Codex binds its thread now: no tool call, no credential.
        [(path, body, headers, _)] = service["hooks"]
        assert path == "/hooks/enrolment" and "authorization" not in headers
        assert body == {
            "pairing_id": claim["id"],
            "poll_secret": POLL_SECRET,
            "client": "codex",
            "session_id": SESSION,
        }
    else:
        # Claude binds its next Pensieve tool call instead.
        assert service["hooks"] == []
    # A later prompt keeps the same claim rather than starting another.
    briefing.run_hook(dict(payload, hook_event_name="UserPromptSubmit"), client, config, **hooks)
    assert len(starts(service)) == 1
    assert pairing.pending_claim(config, client)["id"] == claim["id"]
    assert len(service["hooks"]) == (2 if client == "codex" else 0)


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_lifecycle_connect_failure_retries_later_without_binding(
    tmp_path, service, no_browser, client
):
    config = existing_config(tmp_path)
    hooks = endpoints(service)
    hooks["pairing_base"] = f"http://localhost:{closed_port()}/users/me/conversation-capture"
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": SESSION}
    result = briefing.run_hook(payload, client, config, **hooks)
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.endswith("it retries automatically.")
    assert briefing.CONNECTING not in context
    assert pairing.pending_claim(config, client) is None
    assert service["hooks"] == []


def test_first_lifecycle_hook_on_a_fresh_machine_starts_connecting(tmp_path, service, no_browser):
    config = new_config(tmp_path)
    previous = os.umask(0o022)
    try:
        result = briefing.run_hook(
            {"hook_event_name": "UserPromptSubmit", "session_id": SESSION},
            "codex",
            config,
            **endpoints(service),
        )
    finally:
        os.umask(previous)
    assert briefing.CONNECTING in str(result)
    assert pairing.pending_claim(config, "codex") is not None


def test_failed_start_backs_off_lifecycle_hooks(tmp_path, monkeypatch):
    config = existing_config(tmp_path)
    calls = []
    monkeypatch.setattr(briefing, "start", lambda *a, **k: calls.append(1) or {"status": "x"})
    state = {}
    first = briefing.connect(config, "codex", SESSION, state, "unused", "unused")
    assert "retries automatically" in first and calls == [1]
    assert state["connect_retry_at"] > time.time()
    # Offline: the next prompt does not spend its budget on another start.
    assert briefing.connect(config, "codex", SESSION, state, "unused", "unused") == first
    assert calls == [1]
    state["connect_retry_at"] = 0
    briefing.connect(config, "codex", SESSION, state, "unused", "unused")
    assert calls == [1, 1]
