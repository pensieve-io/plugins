"""First use offers consent, never silently imports history or reopens dismissal."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import capture_config as credentials
import capture_onboarding as onboarding
import pytest
from test_conversation_capture import OWNER, SESSION, append, hook_record, marker


def transcript(tmp_path, client="codex", intent=None):
    path = tmp_path / "transcript.jsonl"
    path.write_text("")
    if client == "codex":
        append(path, {"type": "session_meta", "payload": {"id": SESSION}})
    record = hook_record(client=client, generation=None)
    if intent:
        text = (
            "<!-- pensieve-capture-setup " + json.dumps(intent) + " -->\n" + marker(client=client)
        )
        if client == "codex":
            record["payload"]["content"][0]["text"] = text
        else:
            record["attachment"]["content"] = [text]
    append(path, record)
    return path


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    start = Mock(return_value={"status": "awaiting_approval", "verification_url": "fixture"})
    opened = Mock()
    monkeypatch.setattr(onboarding, "start", start)
    monkeypatch.setattr(onboarding, "open_approval", opened)
    return tmp_path / "private" / "capture.json", start, opened


def intent():
    return {
        "id": str(uuid4()),
        "user_id": OWNER,
        "client": "codex",
        "context_id": 497,
        "conversation_id": SESSION,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    }


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_first_native_offer_is_bound_to_account_context_and_only_opens_once(
    tmp_path, monkeypatch, client
):
    config, start, opened = setup(tmp_path, monkeypatch)
    payload = {"transcript_path": str(transcript(tmp_path, client))}
    for _ in range(3):
        onboarding.offer_connection(payload, client, SESSION, config, {})
    assert start.call_count == opened.call_count == 1
    assert start.call_args.kwargs["expected_user_id"] == OWNER
    assert start.call_args.kwargs["expected_context_id"] == 497
    assert start.call_args.kwargs["timeout"] <= 0.5
    assert not config.exists()  # Offering approval cannot issue an upload credential.


def test_clients_request_retries_decline_once_and_expiry_never_reopens(tmp_path, monkeypatch):
    config, start, opened = setup(tmp_path, monkeypatch)
    path = transcript(tmp_path)
    payload = {"transcript_path": str(path)}
    onboarding.offer_connection(payload, "codex", SESSION, config, {})
    request = intent()
    transcript(tmp_path, intent=request)
    onboarding.offer_connection(payload, "codex", SESSION, config, {})
    onboarding.offer_connection(payload, "codex", SESSION, config, {})
    assert start.call_count == opened.call_count == 2
    assert start.call_args.kwargs["restart"] is True
    # Expiry/removal of the explicit request must not reset first-use dismissal.
    transcript(tmp_path)
    onboarding.offer_connection(payload, "codex", SESSION, config, {})
    assert start.call_count == 2


def test_reconnect_existing_profile_requires_explicit_request(tmp_path, monkeypatch):
    config, start, _ = setup(tmp_path, monkeypatch)
    path = transcript(tmp_path)
    configured = {f"{OWNER}:497": "synthetic-private-key"}
    payload = {"transcript_path": str(path)}
    onboarding.offer_connection(payload, "codex", SESSION, config, configured)
    start.assert_not_called()
    transcript(tmp_path, intent=intent())
    onboarding.offer_connection(payload, "codex", SESSION, config, configured)
    start.assert_called_once()


def test_quoted_or_wrong_session_marker_cannot_offer_browser_approval(tmp_path, monkeypatch):
    config, start, _ = setup(tmp_path, monkeypatch)
    path = tmp_path / "transcript.jsonl"
    path.write_text("")
    append(path, {"type": "session_meta", "payload": {"id": SESSION}})
    record = hook_record()
    record["payload"]["role"] = "assistant"
    append(path, record)
    onboarding.offer_connection({"transcript_path": str(path)}, "codex", SESSION, config, {})
    start.assert_not_called()
    transcript(tmp_path)
    onboarding.offer_connection({"transcript_path": str(path)}, "codex", str(uuid4()), config, {})
    start.assert_not_called()


def test_connecting_second_context_preserves_first_and_keys_do_not_cross_context(tmp_path):
    config = tmp_path / "private" / "capture.json"
    for context in (497, 508):
        credentials.install_profile(
            config,
            {
                "user_id": OWNER,
                "client": "codex",
                "context_id": context,
                "upload_key": f"synthetic-private-key-{context}",
                "installation_id": str(uuid4()),
                "runtime": "unknown",
                "host_version": "",
            },
        )
    profiles = credentials.profiles(config, "codex")
    assert credentials.key_for(profiles, OWNER, 497) == "synthetic-private-key-497"
    assert credentials.key_for(profiles, OWNER, 508) == "synthetic-private-key-508"
    assert credentials.key_for(profiles, OWNER, 509) is None
    assert credentials.profiles(config, "claude") == {}


def test_offline_first_use_retries_with_backoff_without_marking_consent(tmp_path, monkeypatch):
    config, start, opened = setup(tmp_path, monkeypatch)
    start.return_value = {"status": "unavailable"}
    payload = {"transcript_path": str(transcript(tmp_path))}
    onboarding.offer_connection(payload, "codex", SESSION, config, {})
    onboarding.offer_connection(payload, "codex", SESSION, config, {})
    assert start.call_count == 1
    opened.assert_not_called()
    path = config.with_name("capture-onboarding.json")
    state = json.loads(credentials.private_file(path, credentials.MAX_CONFIG_BYTES))
    for entry in state.values():
        assert entry["offered"] is False
        entry["retry_at"] = 0
    credentials.save_private_json(path, state)
    onboarding.offer_connection(payload, "codex", SESSION, config, {})
    assert start.call_count == 2
    assert not config.exists()


def test_compaction_requires_a_new_native_marker_before_onboarding(tmp_path, monkeypatch):
    config, start, _ = setup(tmp_path, monkeypatch)
    path = transcript(tmp_path)
    append(path, {"type": "compacted"})
    onboarding.offer_connection({"transcript_path": str(path)}, "codex", SESSION, config, {})
    start.assert_not_called()
