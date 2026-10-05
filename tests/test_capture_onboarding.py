"""A connected helper asks once whether to share transcripts, then backs off."""

import json
import time
from unittest.mock import Mock
from uuid import uuid4

import capture_config as credentials
import capture_onboarding as onboarding
import pytest
from test_conversation_capture import OTHER_OWNER, OWNER, SESSION, append, hook_record, marker

CONNECTED = {f"{OWNER}:497": "synthetic-private-key"}


def page(client):
    return f"https://app.pensieve.uk/oauth/conversation-capture?client={client}&context_id=497&user_id={OWNER}"


def consent_record(client="codex", consent="unknown", generation=None, session=SESSION):
    record = hook_record(client=client, generation=generation, session=session)
    if client == "claude":
        record["sessionId"] = session
    status = {
        "user_id": OWNER,
        "client": client,
        "context_id": 497,
        "conversation_id": session,
        "status": consent,
    }
    text = (
        "<!-- pensieve-capture-consent "
        + json.dumps(status)
        + " -->\n"
        + marker(client=client, generation=generation, session=session)
    )
    if client == "codex":
        record["payload"]["content"][0]["text"] = text
    else:
        record["attachment"]["content"] = [text]
    return record


def transcript(tmp_path, client="codex", consent="unknown", generation=None, session=SESSION):
    path = tmp_path / "transcript.jsonl"
    path.write_text("")
    if client == "codex":
        append(path, {"type": "session_meta", "payload": {"id": session}})
    append(path, consent_record(client, consent, generation, session))
    return path


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    opened = Mock()
    monkeypatch.setattr(onboarding, "open_page", opened)
    return tmp_path / "private" / "capture.json", opened


def onboarding_state(config):
    path = config.with_name("capture-onboarding.json")
    return path, json.loads(credentials.private_file(path, credentials.MAX_CONFIG_BYTES))


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_unknown_consent_opens_the_exact_page_once_per_conversation(tmp_path, monkeypatch, client):
    config, opened = setup(tmp_path, monkeypatch)
    payload = {"transcript_path": str(transcript(tmp_path, client))}
    for _ in range(3):
        onboarding.offer_connection(payload, client, SESSION, config, CONNECTED)
    opened.assert_called_once_with(page(client))
    # The page only records a choice: nothing is paired, issued or stored secretly.
    assert not config.exists()
    path, state = onboarding_state(config)
    assert "synthetic-private-key" not in path.read_text()
    [entry] = state.values()
    assert entry["session"] == SESSION
    assert 0 < entry["retry_at"] - time.time() <= onboarding.OFFER_INTERVAL_SECONDS == 600


@pytest.mark.parametrize(
    "configured",
    [
        {},
        {f"{OWNER}:508": "synthetic-private-key"},
        {f"{OTHER_OWNER}:497": "synthetic-private-key"},
    ],
    ids=["no_key", "other_context", "other_account"],
)
def test_without_a_key_for_this_context_connecting_is_left_to_mcp(
    tmp_path, monkeypatch, configured
):
    config, opened = setup(tmp_path, monkeypatch)
    payload = {"transcript_path": str(transcript(tmp_path))}
    onboarding.offer_connection(payload, "codex", SESSION, config, configured)
    opened.assert_not_called()
    assert not config.with_name("capture-onboarding.json").exists()


@pytest.mark.parametrize("configured", [{}, CONNECTED], ids=["new_installation", "connected"])
def test_declined_or_approved_consent_never_opens_the_page(tmp_path, monkeypatch, configured):
    config, opened = setup(tmp_path, monkeypatch)
    for consent, generation in [("declined", None), ("approved", str(uuid4()))]:
        path = transcript(tmp_path, consent=consent, generation=generation)
        onboarding.offer_connection(
            {"transcript_path": str(path)}, "codex", SESSION, config, configured
        )
    opened.assert_not_called()


def test_quoted_or_wrong_session_marker_cannot_open_the_page(tmp_path, monkeypatch):
    config, opened = setup(tmp_path, monkeypatch)
    path = tmp_path / "transcript.jsonl"
    path.write_text("")
    append(path, {"type": "session_meta", "payload": {"id": SESSION}})
    record = consent_record()
    record["payload"]["role"] = "assistant"
    append(path, record)
    onboarding.offer_connection({"transcript_path": str(path)}, "codex", SESSION, config, CONNECTED)
    opened.assert_not_called()
    transcript(tmp_path)
    onboarding.offer_connection(
        {"transcript_path": str(path)}, "codex", str(uuid4()), config, CONNECTED
    )
    opened.assert_not_called()


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


def test_compaction_requires_a_new_native_marker_before_onboarding(tmp_path, monkeypatch):
    config, opened = setup(tmp_path, monkeypatch)
    path = transcript(tmp_path)
    append(path, {"type": "compacted"})
    payload = {"transcript_path": str(path)}
    onboarding.offer_connection(payload, "codex", SESSION, config, CONNECTED)
    opened.assert_not_called()
    append(path, consent_record())
    onboarding.offer_connection(payload, "codex", SESSION, config, CONNECTED)
    opened.assert_called_once_with(page("codex"))


def test_non_macos_hosts_never_open_the_page(tmp_path, monkeypatch):
    config, opened = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(onboarding.sys, "platform", "linux")
    payload = {"transcript_path": str(transcript(tmp_path))}
    onboarding.offer_connection(payload, "codex", SESSION, config, CONNECTED)
    opened.assert_not_called()
    assert not config.with_name("capture-onboarding.json").exists()


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_unanswered_page_is_offered_again_only_in_a_later_conversation_after_the_interval(
    tmp_path, monkeypatch, client
):
    config, opened = setup(tmp_path, monkeypatch)
    path = transcript(tmp_path, client)
    payload = {"transcript_path": str(path)}
    onboarding.offer_connection(payload, client, SESSION, config, CONNECTED)
    assert opened.call_count == 1
    # Closing the page without choosing never re-asks within the interval,
    # even from another conversation.
    next_session = str(uuid4())
    transcript(tmp_path, client, session=next_session)
    onboarding.offer_connection(payload, client, next_session, config, CONNECTED)
    assert opened.call_count == 1
    state_path, state = onboarding_state(config)
    for entry in state.values():
        entry["retry_at"] = 0
    credentials.save_private_json(state_path, state)
    # The original conversation is never asked twice, even after the interval.
    transcript(tmp_path, client)
    onboarding.offer_connection(payload, client, SESSION, config, CONNECTED)
    assert opened.call_count == 1
    transcript(tmp_path, client, session=next_session)
    onboarding.offer_connection(payload, client, next_session, config, CONNECTED)
    onboarding.offer_connection(payload, client, next_session, config, CONNECTED)
    assert opened.call_count == 2
    assert opened.call_args.args == (page(client),)
