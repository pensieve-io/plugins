"""A turn whose briefing failed takes the marker Pensieve later gives it, or is counted unsaved."""

from __future__ import annotations

import json

import capture_state
import conversation_capture as capture
import pytest
from test_capture_health import prompt
from test_conversation_capture import (
    GENERATION,
    OWNER,
    SESSION,
    append,
    assistant,
    config,
    contents,
    events,
    hook_record,
    marker,
    pending,
    setup,
    user,
)

PROOF = "pcap_" + "b" * 43
OTHER_GENERATION = "317e0b53-8178-4a3c-8d40-a07414144741"
FETCH = capture.fetch_marker  # The real request, which `setup` stubs.
NO_ANSWER = (False, None)  # A timeout, an outage or a server error.
NONE_GIVEN = (True, None)  # Pensieve answered, with no marker.


def given(text):
    return True, text


def proof(tmp_path, client):
    """The private installation proof the briefing hook keeps beside the config."""
    path = tmp_path / f"mcp-headers-{client}.json"
    path.write_text(json.dumps({"X-Pensieve-Plugin": f"{client} {PROOF}"}))
    path.chmod(0o600)


def answering(monkeypatch, *replies):
    """Pensieve gives each reply in turn, then the last for good, and records who asked."""
    asked = []

    def fetch(endpoint, token, client, session, turn_id, timeout):
        asked.append({"token": token, "client": client, "session": session, "turn_id": turn_id})
        return replies[min(len(asked), len(replies)) - 1]

    monkeypatch.setattr(capture, "fetch_marker", fetch)
    return asked


def turn_context(turn):
    return {"type": "turn_context", "payload": {"turn_id": turn}}


def unsaved(state, client="codex"):
    return capture.spool_status(state, client, SESSION).get("unsaved_turns", 0)


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_a_turn_whose_briefing_failed_is_saved_with_the_marker_its_stop_fetches(
    tmp_path, monkeypatch, client
):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    proof(tmp_path, client)
    asked = answering(monkeypatch, given(marker(client)))
    append(path, user("Asked while Pensieve stalled", client), assistant("Answered", client))
    run()
    assert contents(calls) == ["Asked while Pensieve stalled", "Answered"]
    assert asked == [{"token": PROOF, "client": client, "session": SESSION, "turn_id": None}]
    assert pending(state) == 0 and unsaved(state, client) == 0
    # The next turn, briefed as usual, continues the same saved conversation.
    append(path, user("Next", client), hook_record(client), assistant("Next answer", client))
    run()
    saved = events(calls)
    assert [event["content"] for event in saved[-2:]] == ["Next", "Next answer"]
    assert saved[-2]["capture"]["parent"]["event_id"] == saved[-3]["event_id"]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_only_an_unmarked_turn_asks_for_a_marker(tmp_path, monkeypatch, client):
    path, _cfg, _state, calls, run = setup(tmp_path, monkeypatch, client)
    proof(tmp_path, client)
    asked = answering(monkeypatch, given(marker(client)))
    append(path, user("Briefed", client), hook_record(client), assistant("Answer", client))
    run()
    # A prompt not yet answered may still receive its marker from the transcript.
    append(path, user("Not yet answered", client))
    run("UserPromptSubmit")
    run("SessionEnd")
    assert asked == []
    assert contents(calls) == ["Briefed", "Answer"]


@pytest.mark.parametrize("client", ["codex", "claude"])
@pytest.mark.parametrize("retry", ["Stop", "UserPromptSubmit", "SessionEnd"])
def test_a_turn_pensieve_did_not_answer_for_asks_again_at_the_next_hook(
    tmp_path, monkeypatch, client, retry
):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    proof(tmp_path, client)
    asked = answering(monkeypatch, NO_ANSWER, given(marker(client)))
    append(path, user("Asked while Pensieve stalled", client), assistant("Answered", client))
    run()
    assert calls == [] and pending(state) == 2 and unsaved(state, client) == 0
    run(retry)
    assert contents(calls) == ["Asked while Pensieve stalled", "Answered"]
    run()
    assert len(asked) == 2  # A saved turn asks no more.


@pytest.mark.parametrize("written", [False, True])
def test_an_interrupted_turn_is_saved_at_the_next_prompt(tmp_path, monkeypatch, written):
    """Claude raises no Stop for an interrupted turn; the next prompt asks for it."""
    path, _cfg, _state, calls, run = setup(tmp_path, monkeypatch, "claude")
    proof(tmp_path, "claude")
    answering(monkeypatch, given(marker("claude")))
    append(path, user("Interrupted", "claude"), assistant("Partial", "claude"))
    if written:
        # The host may write the next prompt before its hook reads the transcript.
        append(path, user("Next", "claude"))
    run("UserPromptSubmit")
    assert contents(calls) == ["Interrupted", "Partial"]


@pytest.mark.parametrize("client", ["codex", "claude"])
@pytest.mark.parametrize("shared", [True, False])
def test_turns_lost_while_pensieve_is_unreachable_are_counted_and_noticed_once_each(
    tmp_path, monkeypatch, client, shared
):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    proof(tmp_path, client)
    answering(monkeypatch, NO_ANSWER)
    generation = GENERATION if shared else None
    append(path, user("Saved", client), hook_record(client, generation=generation))
    append(path, assistant("Kept", client))
    run()
    # Every briefing and every request for a missed marker fails from here on.
    append(path, user("Lost", client), assistant("Lost answer", client))
    run()
    assert prompt(run) is None  # Still waiting: nothing is lost yet.
    append(path, user("Lost again", client), assistant("Lost again answer", client))
    run()
    assert unsaved(state, client) == 1
    notice = prompt(run)
    assert prompt(run) is None
    append(path, user("Third", client), assistant("Third answer", client))
    run()
    assert unsaved(state, client) == 2
    later = prompt(run)
    if shared:
        assert "could not be reached in time to save 1 earlier turn" in notice
        assert f"--status {SESSION}" in notice
        assert "to save 1 earlier turn" in later
    else:
        # A member who does not share transcripts lost nothing to be told about.
        assert notice is None and later is None
    assert contents(calls) == (["Saved", "Kept"] if shared else [])
    status = capture.conversation_status(
        {"session_id": SESSION, "hook_event_name": "UserPromptSubmit"}, client, SESSION, cfg, state
    )
    assert "Not saved: 2 earlier turns, as Pensieve could not be reached in time" in json.dumps(
        status
    )


def test_without_an_installation_proof_the_turn_is_given_up(tmp_path, monkeypatch):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    asked = answering(monkeypatch, given(marker()))
    append(path, user("Lost"), assistant("Lost answer"))
    run()
    assert asked == [] and calls == []
    assert unsaved(state) == 1
    # No marker yet shows this conversation shared, so nobody is told.
    assert prompt(run) is None


@pytest.mark.parametrize(
    "late",
    [
        marker(owner="173e0b53-8178-4a3c-8d40-a07414144741"),  # unconfigured account
        marker(session="00000000-0000-4000-8000-000000000099"),  # another conversation
        marker(client="claude"),  # another host
        "<!-- not a marker -->",
    ],
)
def test_a_late_marker_must_belong_to_this_conversation_and_account(tmp_path, monkeypatch, late):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, given(late))
    append(path, user("Asked"), assistant("Answered"))
    run()
    assert calls == [] and pending(state) == 0


@pytest.mark.parametrize("changed", ["before its Stop", "before its retry"])
def test_a_late_marker_cannot_cross_a_key_change(tmp_path, monkeypatch, changed):
    """The turn's own starting position decides: a key replaced since refuses the marker."""
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    retried = changed == "before its retry"
    answering(monkeypatch, *([NO_ANSWER] if retried else []), given(marker()))
    append(path, user("Under the old key"), assistant("Answered"))
    if retried:
        run()
    config(tmp_path, [{"user_id": OWNER, "upload_key": "synthetic-replacement-key"}])
    run("UserPromptSubmit" if retried else "Stop")
    assert calls == [] and pending(state) == 0


def test_a_failed_codex_turn_asks_with_its_own_turn_id(tmp_path, monkeypatch):
    """Even once the next turn has begun, so its marker cannot attribute this one."""
    first, second = "0a5e0b53-8178-4a3c-8d40-a07414144741", "1b5e0b53-8178-4a3c-8d40-a07414144741"
    path, _cfg, _state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    asked = answering(monkeypatch, given(marker(turn=first)))
    append(path, turn_context(first), user("First"), assistant("First answer"))
    append(path, turn_context(second), user("Second"))
    run("UserPromptSubmit")
    assert [request["turn_id"] for request in asked] == [first]
    assert contents(calls) == ["First", "First answer"]


def test_a_late_marker_for_another_codex_turn_is_refused_and_counted(tmp_path, monkeypatch):
    current, other = "0a5e0b53-8178-4a3c-8d40-a07414144741", "1b5e0b53-8178-4a3c-8d40-a07414144741"
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, given(marker(turn=other)))
    append(path, turn_context(current), user(), assistant())
    run()
    assert calls == [] and pending(state) == 0
    assert unsaved(state) == 1


def test_a_late_marker_without_a_company_holds_the_turn_until_one(tmp_path, monkeypatch):
    path, _cfg, _state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, given(marker(context=None, generation=None)))
    append(path, user("Before any company"), assistant("Held"))
    run()
    assert calls == []
    append(path, user("In a company"), hook_record(), assistant("Company answer"))
    run()
    assert contents(calls) == ["Before any company", "Held", "In a company", "Company answer"]


def test_turns_held_for_a_first_company_are_counted_when_a_lost_turn_drops_them(
    tmp_path, monkeypatch
):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, NONE_GIVEN)
    append(path, user("Held"), hook_record(context=None, generation=None), assistant("Waits"))
    run()
    append(path, user("Lost"), assistant("Lost answer"))
    run()
    assert calls == [] and pending(state) == 0
    assert unsaved(state) == 2


def test_a_late_marker_with_sharing_off_saves_nothing_and_is_not_a_loss(tmp_path, monkeypatch):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, given(marker(generation=None)))
    append(path, user("Not shared"), assistant("Answer"))
    run()
    assert calls == [] and pending(state) == 0
    assert unsaved(state) == 0


@pytest.mark.parametrize("lost_between", [False, True])
def test_a_late_marker_after_sharing_changed_saves_nothing_and_is_not_a_loss(
    tmp_path, monkeypatch, lost_between
):
    """It may have been written while sharing was off, before the generation it names."""
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    append(path, user("Saved"), hook_record(), assistant("Kept"))
    run()
    first = [NONE_GIVEN] if lost_between else []
    answering(monkeypatch, *first, given(marker(generation=OTHER_GENERATION)))
    if lost_between:
        # A turn given up meanwhile leaves the conversation without a current scope.
        append(path, user("Lost"), assistant("Lost answer"))
        run()
    append(path, user("Asked after sharing changed"), assistant("Answer"))
    run()
    assert contents(calls) == ["Saved", "Kept"] and pending(state) == 0
    assert unsaved(state) == int(lost_between)
    # A briefed prompt under the new generation saves as usual.
    append(path, user("Next"), hook_record(generation=OTHER_GENERATION), assistant("Next answer"))
    run()
    assert contents(calls)[-2:] == ["Next", "Next answer"]


def test_a_late_transcript_marker_still_cannot_attribute_the_turn(tmp_path, monkeypatch):
    """Only the fetch asks for this turn; a marker written after its answer is someone else's."""
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, NONE_GIVEN)
    append(path, user("Unmarked"), assistant("Answer"), hook_record())
    run()
    assert calls == [] and pending(state) == 0


def test_an_unmarked_turn_past_its_limit_is_given_up_early(tmp_path, monkeypatch):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    asked = answering(monkeypatch, given(marker()))
    monkeypatch.setattr(capture_state, "UNMARKED_LIMIT_BYTES", 2048)
    append(path, user("Long"), *(assistant("x" * 1500) for _ in range(3)))
    run()
    assert calls == [] and asked == [] and pending(state) == 0
    assert unsaved(state) == 1


def test_an_unmarked_turn_never_takes_room_queued_uploads_need(tmp_path, monkeypatch):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, given(marker()))
    monkeypatch.setattr(capture, "MAX_STATE_PAGES", 40)
    append(path, user("x" * 32000), *(assistant("x" * 32000) for _ in range(3)))
    append(path, user("Valid prompt"), hook_record(), assistant("Valid answer"))
    run()
    assert contents(calls) == ["Valid prompt", "Valid answer"]
    assert pending(state) == 0


def test_the_marker_is_asked_for_beside_the_upload_route(anchor_service):
    anchor_service["body"] = {"marker": marker()}
    assert FETCH(anchor_service["endpoint"], PROOF, "codex", SESSION, None, 1.0) == given(marker())
    path, headers, body, _size = anchor_service["requests"][-1]
    assert path == "/hooks/marker" and headers["Authorization"] == f"Bearer {PROOF}"
    assert body == {"client": "codex", "session_id": SESSION}
    FETCH(anchor_service["endpoint"], PROOF, "codex", SESSION, GENERATION, 1.0)
    assert anchor_service["requests"][-1][2]["turn_id"] == GENERATION


@pytest.mark.parametrize(
    "status,body,final",
    [
        (204, {}, True),  # None may be given.
        (401, {}, True),  # The proof is refused.
        (403, {}, True),
        (404, {}, True),  # An older service without the route.
        (500, {}, False),
        (503, {}, False),
        (200, {"marker": None}, False),
    ],
)
def test_only_an_answer_from_pensieve_gives_the_turn_up_at_once(
    tmp_path, monkeypatch, anchor_service, status, body, final
):
    path, cfg, state, calls, _run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    monkeypatch.setattr(capture, "fetch_marker", FETCH)
    anchor_service.update(status=status, body=body)

    def run(event):
        payload = {"session_id": SESSION, "hook_event_name": event, "transcript_path": str(path)}
        capture.run_hook(payload, "codex", cfg, state, anchor_service["endpoint"])

    append(path, user("Asked"), assistant("Answered"))
    run("Stop")
    run("UserPromptSubmit")
    assert calls == []
    assert len(anchor_service["requests"]) == (1 if final else 2)
    assert unsaved(state) == int(final) and pending(state) == (0 if final else 2)
