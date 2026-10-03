"""Command hooks retain context without exposing credentials or a model tool."""

import json

import capture_config as config
import context_briefing as briefing
import plugin_connection as connection
import pytest

OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
OTHER = "173e0b53-8178-4a3c-8d40-a07414144741"
SESSION = "923bbd76-d864-4b8f-b252-c2b7c3692492"
TURN = "193e0b53-8178-4a3c-8d40-a07414144741"
KEY = "pcap_" + "a" * 43
TOKEN = "a" * 64 + "." + "b" * 32


def profile(owner=OWNER, context=None, enabled=True):
    return {
        "user_id": owner,
        "context_id": context,
        "client": "codex",
        "upload_key": KEY,
        "installation_id": TURN,
        "runtime": "unknown",
        "host_version": "",
        "briefing_enabled": enabled,
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "private" / "capture.json"
    config.install_profile(path, profile())
    calls = []
    for client in ("codex", "claude"):
        config.save_private_json(
            connection.header_path(path, client), {connection.HEADER: f"{client} {KEY}"}
        )
    monkeypatch.setattr(briefing, "latest_receipt", lambda *_a: (TOKEN, "ack"))
    monkeypatch.setattr(briefing, "send_receipt", lambda *a: calls.append(("receipt", a)))
    config.save_private_json(briefing.state_path(path, "codex", SESSION), {"user_id": OWNER})

    def request(endpoint, key, body, **_kwargs):
        calls.append(("request", endpoint, key, body))
        if endpoint == briefing.BINDING_ENDPOINT:
            return 204, None
        return 200, {
            "hookSpecificOutput": {
                "hookEventName": body["event"],
                "additionalContext": "company primer",
            }
        }

    monkeypatch.setattr(briefing, "request", request)
    return path, calls


def payload(event="UserPromptSubmit"):
    return {"hook_event_name": event, "session_id": SESSION, "turn_id": TURN}


def test_receipt_precedes_fetch_and_native_event_turn_are_retained(setup):
    path, calls = setup
    result = briefing.run_hook(payload(), "codex", path)
    assert result["hookSpecificOutput"]["additionalContext"] == "company primer"
    assert calls[0][0] == "receipt"
    request = calls[1][3]
    assert request["event"] == "UserPromptSubmit"
    assert request["turn_id"] == TURN
    assert "force_refresh" not in request
    state = json.loads(briefing.state_path(path, "codex", SESSION).read_text())
    assert state["user_id"] == OWNER
    assert KEY not in json.dumps(state) + json.dumps(result)
    first_delivery = request["delivery_id"]
    briefing.run_hook(payload(), "codex", path)
    assert calls[-1][3]["delivery_id"] == first_delivery


@pytest.mark.parametrize("receipt", [None, (TOKEN, "reset")])
def test_missing_or_compacted_transcript_forces_refresh_without_losing_prompt_marker(
    setup, monkeypatch, receipt
):
    path, calls = setup
    monkeypatch.setattr(briefing, "latest_receipt", lambda *_a: receipt)
    result = briefing.run_hook(payload(), "codex", path)
    body = calls[-1][3]
    assert body["force_refresh"] is True
    assert body["event"] == "UserPromptSubmit"
    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


@pytest.mark.parametrize("failure", ["unavailable", 503, "root_unavailable", "interrupted"])
def test_failed_refresh_cannot_reuse_an_older_acknowledged_briefing(setup, monkeypatch, failure):
    path, calls = setup
    briefing.run_hook(payload(), "codex", path)
    failed = False

    def request(_endpoint, _key, body, **_kwargs):
        nonlocal failed
        calls.append(("retry", body))
        if not failed:
            failed = True
            if failure == "interrupted":
                raise KeyboardInterrupt
            if failure == "root_unavailable":
                return 200, {
                    "briefing_available": False,
                    "hookSpecificOutput": {
                        "hookEventName": body["event"],
                        "additionalContext": "Root unavailable; retry on the next prompt.",
                    },
                }
            return failure, None
        # Model the server's ACK suppression for an unchanged root. A fresh
        # fetch alone cannot supersede the failure instruction in the chat.
        return 200, {
            "hookSpecificOutput": {
                "hookEventName": body["event"],
                "additionalContext": "restored company primer" if body.get("force_refresh") else "",
            }
        }

    monkeypatch.setattr(briefing, "request", request)
    if failure == "interrupted":
        with pytest.raises(KeyboardInterrupt):
            briefing.run_hook(payload(), "codex", path)
    else:
        unavailable = briefing.run_hook(payload(), "codex", path)
        assert "unavailable" in unavailable["hookSpecificOutput"]["additionalContext"]
        assert "briefing_available" not in unavailable
    restored = briefing.run_hook(payload(), "codex", path)
    assert restored["hookSpecificOutput"]["additionalContext"] == "restored company primer"
    assert calls[-1][1]["force_refresh"] is True
    assert "needs_refresh" not in json.loads(
        briefing.state_path(path, "codex", SESSION).read_text()
    )
    briefing.run_hook(payload(), "codex", path)
    assert "force_refresh" not in calls[-1][1]


def test_session_start_resets_receipt_and_delivery_scope(setup):
    path, calls = setup
    briefing.run_hook(payload(), "codex", path)
    prior = calls[-1][3]["delivery_id"]
    briefing.run_hook(payload("SessionStart"), "codex", path)
    assert calls[-2] == ("receipt", (TOKEN, "reset", briefing.DELIVERY_ENDPOINT))
    assert calls[-1][3]["delivery_id"] != prior
    assert calls[-1][3]["force_refresh"] is True


def test_claude_binding_registers_exact_call_before_tool_runs(setup):
    path, calls = setup
    key = profile()
    key["client"] = "claude"
    config.install_profile(path, key)
    event = dict(
        payload("PreToolUse"),
        tool_name="mcp__plugin_pensieve_pensieve__read",
        tool_use_id="toolu_123",
        mcp_server={"name": "plugin:pensieve:pensieve", "source": "plugin"},
    )
    assert briefing.run_hook(event, "claude", path) == {}
    assert calls[-1][3] == {"client": "claude", "session_id": SESSION, "tool_use_id": "toolu_123"}
    assert all(c[0] != "receipt" for c in calls)


@pytest.mark.parametrize(
    "tool_name,provenance,bound",
    [
        # The claude.ai connector is a second Pensieve connection; its calls
        # must carry the conversation too, whatever provenance it reports.
        ("mcp__claude_ai_Pensieve__search", {"name": "claude.ai Pensieve"}, True),
        ("mcp__claude_ai_Pensieve__search", None, True),
        ("mcp__other_server__search", None, False),
    ],
)
def test_claude_binding_covers_the_claude_ai_connector(setup, tool_name, provenance, bound):
    path, calls = setup
    key = profile()
    key["client"] = "claude"
    config.install_profile(path, key)
    event = dict(payload("PreToolUse"), tool_name=tool_name, tool_use_id="toolu_456")
    if provenance is not None:
        event["mcp_server"] = provenance
    assert briefing.run_hook(event, "claude", path) == {}
    bindings = [c[3] for c in calls if c[3] and c[3].get("tool_use_id") == "toolu_456"]
    assert bool(bindings) is bound


def test_claude_binding_failure_denies_without_exposing_error_or_secret(setup, monkeypatch):
    path, calls = setup
    key = profile()
    key["client"] = "claude"
    config.install_profile(path, key)
    endpoints = []
    monkeypatch.setattr(
        briefing,
        "request",
        lambda endpoint, *_a, **_k: endpoints.append(endpoint) or (503, {"secret": KEY}),
    )
    event = dict(
        payload("PreToolUse"),
        tool_name="mcp__plugin_pensieve_pensieve__read",
        tool_use_id="toolu_123",
    )
    result = briefing.run_hook(event, "claude", path)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == (
        "Pensieve could not verify this conversation. Retry after the plugin reconnects."
    )
    assert KEY not in json.dumps(result)
    # A credentialed call never falls back to the unauthenticated connection.
    assert endpoints == [briefing.BINDING_ENDPOINT]
    assert [c[0] for c in calls] == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.test/hooks/briefing",
        "http://localhost/hooks/briefing?secret=x",
        "http://user@localhost/hooks/briefing",
        "https://evil.test/hooks/connection",
        "http://mcp.pensieve.uk/hooks/connection",
        "https://127.0.0.1/hooks/connection",
        "http://localhost/hooks/connection?secret=x",
        "http://localhost/hooks/connection#secret",
        "http://user:pass@localhost/hooks/connection",
        "http://localhost/hooks/connection/",
        "http://localhost/hooks/connections",
        "http://localhost/hooks/connection/../briefing",
        "http://localhost.evil.test/hooks/connection",
    ],
)
def test_bearer_destination_cannot_be_redirected(endpoint):
    with pytest.raises(ValueError):
        briefing.checked_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://mcp.pensieve.uk/hooks/connection",
        "http://127.0.0.1:8123/hooks/connection",
        "http://localhost/hooks/connection",
    ],
)
def test_connection_reaches_only_the_fixed_service_or_a_loopback_fixture(endpoint):
    assert briefing.checked_endpoint(endpoint) == endpoint
    assert briefing.CONNECTION_ENDPOINT == "https://mcp.pensieve.uk/hooks/connection"


def test_connection_endpoint_argument_is_checked_before_any_input(monkeypatch, capsys):
    monkeypatch.setattr(
        briefing.sys,
        "argv",
        [
            "context_briefing.py",
            "--client",
            "codex",
            "--connection-endpoint",
            "https://evil.test/hooks/connection",
        ],
    )
    monkeypatch.setattr(briefing.sys, "stdin", None)
    with pytest.raises(SystemExit) as exited:
        briefing.main()
    assert exited.value.code == 2
    assert "--connection-endpoint" in capsys.readouterr().err


@pytest.mark.parametrize("tool_id", ["x" * 256, "tool/use", "tool use", "", "☃"])
def test_invalid_claude_call_ids_never_register(setup, tool_id):
    path, calls = setup
    event = dict(
        payload("PreToolUse"), tool_name="mcp__plugin_pensieve_pensieve__read", tool_use_id=tool_id
    )
    assert (
        briefing.run_hook(event, "claude", path)["hookSpecificOutput"]["permissionDecision"]
        == "deny"
    )
    assert calls == []


def test_parallel_claude_calls_bind_independently_of_lifecycle_lock(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path, _ = setup
    key = profile()
    key["client"] = "claude"
    config.install_profile(path, key)
    barrier = Barrier(2)
    bound = []

    def request(_endpoint, _key, body, **_kwargs):
        bound.append(body["tool_use_id"])
        barrier.wait(timeout=2)
        return 204, None

    monkeypatch.setattr(briefing, "request", request)

    def call(tool_id):
        event = dict(
            payload("PreToolUse"),
            tool_name="mcp__plugin_pensieve_pensieve__read",
            tool_use_id=tool_id,
        )
        return briefing.run_hook(event, "claude", path)

    state = briefing.state_path(path, "claude", SESSION)
    with (
        config.private_lock(state.with_suffix(".lock")),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        results = list(executor.map(call, ["toolu_one", "toolu_two"]))
    assert results == [{}, {}]
    assert set(bound) == {"toolu_one", "toolu_two"}
