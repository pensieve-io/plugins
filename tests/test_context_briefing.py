"""Command hooks retain context without exposing credentials or a model tool."""

import json

import capture_config as config
import capture_pairing as pairing
import context_briefing as briefing
import pytest

OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
OTHER = "173e0b53-8178-4a3c-8d40-a07414144741"
SESSION = "923bbd76-d864-4b8f-b252-c2b7c3692492"
TURN = "193e0b53-8178-4a3c-8d40-a07414144741"
KEY = "synthetic-briefing-device-key"
TOKEN = "a" * 64 + "." + "b" * 32


def profile(owner=OWNER, context=497, enabled=True):
    return {
        "user_id": owner,
        "context_id": context,
        "client": "codex",
        "upload_key": KEY + str(context),
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
    monkeypatch.setattr(briefing, "poll", lambda *_a, **_k: {})
    monkeypatch.setattr(briefing, "current_offer", lambda *_a: None)
    monkeypatch.setattr(briefing, "latest_receipt", lambda *_a: (TOKEN, "ack"))
    monkeypatch.setattr(briefing, "send_receipt", lambda *a: calls.append(("receipt", a)))
    monkeypatch.setattr(
        briefing,
        "start",
        lambda *_a, **k: calls.append(("pairing", k)) or {"status": "unavailable"},
    )

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


def test_upload_only_keys_never_fetch_briefings(setup):
    path, calls = setup
    old = profile()
    old.pop("briefing_enabled")
    config.install_profile(path, old)
    result = briefing.run_hook(payload(), "codex", path)
    assert [c[0] for c in calls] == ["pairing"]
    assert "context_briefing(" not in json.dumps(result)
    assert KEY not in json.dumps(result)


def test_multiple_accounts_connect_without_network_guess(setup):
    path, calls = setup
    config.install_profile(path, profile(OTHER))
    briefing.run_hook(payload(), "codex", path)
    assert [c[0] for c in calls] == ["pairing"]
    assert calls[0][1]["expected_user_id"] is None


def test_native_account_identity_selects_only_matching_profile(setup, monkeypatch):
    path, calls = setup
    config.install_profile(path, profile(OTHER, 498))
    monkeypatch.setattr(
        briefing, "current_offer", lambda *_a: {"user_id": OTHER, "context_id": 498}
    )
    briefing.run_hook(payload(), "codex", path)
    assert calls[-1][2] == KEY + "498"


def test_context_switch_uses_new_scoped_key_without_resetting_server_selection(setup, monkeypatch):
    path, calls = setup
    config.install_profile(path, profile(context=498))

    def request(endpoint, key, body, **_kwargs):
        calls.append(("request", key, body))
        if key == KEY + "497":
            return 409, {"detail": "pairing_required", "user_id": OWNER, "context_id": 498}
        return 200, {
            "hookSpecificOutput": {
                "hookEventName": body["event"],
                "additionalContext": "new company",
            }
        }

    monkeypatch.setattr(briefing, "request", request)
    assert (
        briefing.run_hook(payload(), "codex", path)["hookSpecificOutput"]["additionalContext"]
        == "new company"
    )
    assert [c[1] for c in calls if c[0] == "request"] == [KEY + "497", KEY + "498"]
    assert json.loads(briefing.state_path(path, "codex", SESSION).read_text())["context_id"] == 498


def test_context_without_key_pairs_expected_destination(setup, monkeypatch):
    path, calls = setup
    monkeypatch.setattr(
        briefing,
        "request",
        lambda *_a, **_k: (
            409,
            {"detail": "pairing_required", "user_id": OWNER, "context_id": 498},
        ),
    )
    briefing.run_hook(payload(), "codex", path)
    assert calls[-1][1]["expected_user_id"] == OWNER
    assert calls[-1][1]["expected_context_id"] == 498


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
    # A credentialed call never falls back to the unauthenticated enrolment.
    assert endpoints == [briefing.BINDING_ENDPOINT]
    assert [c[0] for c in calls] == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.test/hooks/briefing",
        "http://localhost/hooks/briefing?secret=x",
        "http://user@localhost/hooks/briefing",
        "https://evil.test/hooks/enrolment",
        "http://mcp.pensieve.uk/hooks/enrolment",
        "https://127.0.0.1/hooks/enrolment",
        "http://localhost/hooks/enrolment?secret=x",
        "http://localhost/hooks/enrolment#secret",
        "http://user:pass@localhost/hooks/enrolment",
        "http://localhost/hooks/enrolment/",
        "http://localhost/hooks/enrolments",
        "http://localhost/hooks/enrolment/../briefing",
        "http://localhost.evil.test/hooks/enrolment",
    ],
)
def test_bearer_destination_cannot_be_redirected(endpoint):
    with pytest.raises(ValueError):
        briefing.checked_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://mcp.pensieve.uk/hooks/enrolment",
        "http://127.0.0.1:8123/hooks/enrolment",
        "http://localhost/hooks/enrolment",
    ],
)
def test_enrolment_reaches_only_the_fixed_service_or_a_loopback_fixture(endpoint):
    assert briefing.checked_endpoint(endpoint) == endpoint
    assert briefing.ENROLMENT_ENDPOINT == "https://mcp.pensieve.uk/hooks/enrolment"


def test_enrolment_endpoint_argument_is_checked_before_any_input(monkeypatch, capsys):
    monkeypatch.setattr(
        briefing.sys,
        "argv",
        [
            "context_briefing.py",
            "--client",
            "codex",
            "--enrolment-endpoint",
            "https://evil.test/hooks/enrolment",
        ],
    )
    monkeypatch.setattr(briefing.sys, "stdin", None)
    with pytest.raises(SystemExit) as exited:
        briefing.main()
    assert exited.value.code == 2
    assert "--enrolment-endpoint" in capsys.readouterr().err


@pytest.mark.parametrize("granted", [True, False, None])
def test_claim_from_an_earlier_plugin_finishes_with_exactly_the_registered_permission(
    tmp_path, monkeypatch, granted
):
    # Earlier versions stored a browser address and their own briefing request.
    # Both are now inert: the server's registration alone grants briefing reads.
    path = tmp_path / "private" / "capture.json"
    pending_path = pairing.pairing_path(path, "codex")
    pending = {
        "id": SESSION,
        "poll_secret": "p" * 32,
        "verification_url": "https://app.pensieve.uk/oauth/conversation-capture?pairing_id="
        + SESSION,
        "expires_at": "2099-01-01T00:00:00Z",
        "poll_interval_seconds": 1,
        "base": pairing.API_BASE,
        "client": "codex",
        "runtime": "unknown",
        "host_version": "",
        "next_poll_at": 0,
    }
    config.save_private_json(pending_path, pending)
    response = {
        "status": "registered",
        "user_id": OWNER,
        "context_id": 497,
        "installation_id": TURN,
        "upload_key": KEY,
    }
    if granted is not None:
        response["briefing_enabled"] = granted
    monkeypatch.setattr(pairing, "request", lambda *_a: (200, response))
    assert pairing.pending_claim(path, "codex")["id"] == SESSION
    assert pairing.poll(path, "codex")["briefing_enabled"] is (granted is True)
    profile = config.load_config(path, "codex")["profiles"][0]
    assert profile["briefing_enabled"] is (granted is True)


def test_legacy_context_profile_pins_upgrade_to_existing_account(setup):
    path, calls = setup
    old = profile()
    old.pop("briefing_enabled")
    config.install_profile(path, old)
    briefing.run_hook(payload(), "codex", path)
    assert calls[-1][0] == "pairing"
    assert calls[-1][1]["expected_user_id"] == OWNER
    assert calls[-1][1]["expected_context_id"] == 497


def test_pretool_never_exchanges_one_time_credentials(setup, monkeypatch):
    path, calls = setup
    key = profile()
    key["client"] = "claude"
    config.install_profile(path, key)
    monkeypatch.setattr(briefing, "poll", lambda *_a, **_k: pytest.fail("pretool must not poll"))
    event = dict(
        payload("PreToolUse"),
        tool_name="mcp__plugin_pensieve_pensieve__read",
        tool_use_id="toolu_123",
    )
    assert briefing.run_hook(event, "claude", path) == {}


def test_lifecycle_uses_full_one_time_exchange_budget(setup, monkeypatch):
    path, calls = setup
    seen = []
    monkeypatch.setattr(briefing, "poll", lambda *_a, **k: seen.append(k["timeout"]) or {})
    briefing.run_hook(payload(), "codex", path)
    assert seen == [2]


def test_late_scope_retry_defers_without_losing_destination(setup, monkeypatch):
    path, calls = setup
    config.install_profile(path, profile(context=498))
    now = [100.0]
    monkeypatch.setattr(briefing.time, "monotonic", lambda: now[0])

    def slow_challenge(*_a, **_k):
        now[0] = 107.0
        calls.append(("challenge",))
        return 409, {"detail": "pairing_required", "user_id": OWNER, "context_id": 498}

    monkeypatch.setattr(briefing, "request", slow_challenge)
    result = briefing.run_hook(payload(), "codex", path)
    assert "next prompt" in result["hookSpecificOutput"]["additionalContext"]
    assert len([c for c in calls if c[0] == "challenge"]) == 1
    state = json.loads(briefing.state_path(path, "codex", SESSION).read_text())
    assert state["pending_context_id"] == 498
    # The previous accepted prompt predates the server's explicit scope challenge.
    monkeypatch.setattr(
        briefing, "current_offer", lambda *_a: {"user_id": OWNER, "context_id": 497}
    )
    monkeypatch.setattr(
        briefing, "request", lambda _e, key, _b, **_k: calls.append(("retry", key)) or (200, {})
    )
    briefing.run_hook(payload(), "codex", path)
    assert calls[-1] == ("retry", KEY + "498")


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


def test_pending_exchange_cannot_silently_replace_existing_account(setup, monkeypatch):
    path, calls = setup
    config.install_profile(path, profile(enabled=False))

    def exchange(*_a, **_k):
        config.install_profile(path, profile(owner=OTHER))
        return {"status": "paired", "user_id": OTHER, "context_id": 497, "briefing_enabled": True}

    monkeypatch.setattr(briefing, "poll", exchange)
    briefing.run_hook(payload(), "codex", path)
    assert [c[0] for c in calls] == ["pairing"]
    assert calls[-1][1]["expected_user_id"] == OWNER


def test_rejected_key_recovers_same_account_and_prefers_live_key_next_prompt(setup, monkeypatch):
    path, calls = setup
    config.install_profile(path, profile(context=498))

    def request(_endpoint, key, body, **_kwargs):
        calls.append(("request", key))
        if key == KEY + "497":
            return 403, None
        return 200, {
            "hookSpecificOutput": {
                "hookEventName": body["event"],
                "additionalContext": "current company",
            }
        }

    monkeypatch.setattr(briefing, "request", request)
    result = briefing.run_hook(payload(), "codex", path)
    assert result["hookSpecificOutput"]["additionalContext"] == "current company"
    assert [c[1] for c in calls if c[0] == "request"] == [KEY + "497", KEY + "498"]
    calls.clear()
    monkeypatch.setattr(
        briefing, "current_offer", lambda *_a: {"user_id": OWNER, "context_id": 497}
    )
    briefing.run_hook(payload(), "codex", path)
    assert [c[1] for c in calls if c[0] == "request"] == [KEY + "498"]


def test_claude_rejected_key_uses_same_account_alternative_within_hook_deadline(setup, monkeypatch):
    path, calls = setup
    for context in (497, 498):
        item = profile(context=context)
        item["client"] = "claude"
        config.install_profile(path, item)
    now = [100.0]
    monkeypatch.setattr(briefing.time, "monotonic", lambda: now[0])

    def request(_endpoint, key, _body, timeout):
        calls.append((key, timeout))
        now[0] += timeout
        return (403, None) if key == KEY + "497" else (204, None)

    monkeypatch.setattr(briefing, "request", request)
    event = dict(
        payload("PreToolUse"),
        tool_name="mcp__plugin_pensieve_pensieve__set_context",
        tool_use_id="toolu_recover",
    )
    assert briefing.run_hook(event, "claude", path) == {}
    assert [key for key, _timeout in calls] == [KEY + "497", KEY + "498"]
    assert now[0] <= 104.25


def test_auth_recovery_never_switches_accounts_or_retries_ambiguous_failure(setup, monkeypatch):
    path, calls = setup
    config.install_profile(path, profile(owner=OTHER, context=498))
    monkeypatch.setattr(
        briefing, "current_offer", lambda *_a: {"user_id": OWNER, "context_id": 497}
    )

    def request(_endpoint, key, _body, **_kwargs):
        calls.append(("request", key))
        return 503, None

    monkeypatch.setattr(briefing, "request", request)
    briefing.run_hook(payload(), "codex", path)
    assert [c[1] for c in calls if c[0] == "request"] == [KEY + "497"]


def test_all_keys_rejected_reconnects_account_only(setup, monkeypatch):
    path, calls = setup
    monkeypatch.setattr(briefing, "request", lambda *_a, **_k: (403, None))
    briefing.run_hook(payload(), "codex", path)
    assert calls[-1][0] == "pairing"
    assert calls[-1][1]["expected_user_id"] == OWNER
    assert calls[-1][1]["expected_context_id"] is None


def test_account_only_pairing_adopts_chosen_live_profile(setup, monkeypatch):
    path, calls = setup
    state = briefing.state_path(path, "codex", SESSION)
    config.save_private_json(state, {"user_id": OWNER, "context_id": 497})

    def exchange(*_a, **_k):
        config.install_profile(path, profile(context=498))
        config.save_private_json(
            pairing.pairing_receipt_path(path, "codex", SESSION),
            {"pairing_id": TURN, "user_id": OWNER, "context_id": 498},
        )
        return {"status": "paired", "user_id": OWNER, "context_id": 498, "briefing_enabled": True}

    monkeypatch.setattr(briefing, "poll", exchange)
    briefing.run_hook(payload(), "codex", path)
    assert calls[-1][2] == KEY + "498"


def test_owner_only_pending_pairing_checks_account_independently_of_context(tmp_path, monkeypatch):
    path = tmp_path / "private" / "capture.json"
    pending_path = pairing.pairing_path(path, "codex")
    pending = {
        "id": SESSION,
        "poll_secret": "p" * 32,
        "expires_at": "2099-01-01T00:00:00Z",
        "poll_interval_seconds": 1,
        "base": pairing.API_BASE,
        "client": "codex",
        "runtime": "unknown",
        "host_version": "",
        "next_poll_at": 0,
        "expected_user_id": OWNER,
        "expected_context_id": None,
    }
    config.save_private_json(pending_path, pending)
    monkeypatch.setattr(
        pairing,
        "request",
        lambda *_a: (
            200,
            {
                "status": "registered",
                "user_id": OWNER,
                "context_id": 498,
                "installation_id": TURN,
                "upload_key": KEY,
                "briefing_enabled": True,
            },
        ),
    )
    assert pairing.poll(path, "codex")["context_id"] == 498


def test_slow_rejected_keys_leave_time_to_start_account_recovery(setup, monkeypatch):
    path, calls = setup
    config.install_profile(path, profile(context=498))
    now = [100.0]
    monkeypatch.setattr(briefing.time, "monotonic", lambda: now[0])

    def poll(*_a, **_k):
        now[0] += 2
        return {}

    def receipt(*_a):
        now[0] += 2

    def request(_endpoint, _key, _body, timeout):
        now[0] += timeout
        return 403, None

    monkeypatch.setattr(briefing, "poll", poll)
    monkeypatch.setattr(briefing, "send_receipt", receipt)
    monkeypatch.setattr(briefing, "request", request)
    briefing.run_hook(payload(), "codex", path)
    assert calls[-1][0] == "pairing"
    assert calls[-1][1]["expected_user_id"] == OWNER
    assert calls[-1][1]["expected_context_id"] is None
    assert now[0] <= 107.25


def test_deferred_scope_outranks_previously_preferred_credential(setup):
    import hashlib

    path, _ = setup
    config.install_profile(path, profile(context=498))
    selected = briefing.select_profile(
        briefing.available_profiles(path, "codex"),
        {
            "user_id": OWNER,
            "context_id": 497,
            "pending_context_id": 498,
            "preferred_credential": hashlib.sha256((KEY + "497").encode()).hexdigest(),
        },
    )
    assert selected["context_id"] == 498


def test_scope_challenge_does_not_retry_a_key_already_rejected_this_hook(setup, monkeypatch):
    path, calls = setup
    config.install_profile(path, profile(context=498))

    def request(_endpoint, key, _body, **_kwargs):
        calls.append(("request", key))
        if key == KEY + "497":
            return 403, None
        return 409, {"detail": "pairing_required", "user_id": OWNER, "context_id": 497}

    monkeypatch.setattr(briefing, "request", request)
    briefing.run_hook(payload(), "codex", path)
    assert [c[1] for c in calls if c[0] == "request"] == [KEY + "497", KEY + "498"]
    assert calls[-1][0] == "pairing"
    assert calls[-1][1]["expected_user_id"] == OWNER
    assert calls[-1][1]["expected_context_id"] == 497
