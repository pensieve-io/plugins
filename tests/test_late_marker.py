"""A turn whose briefing failed takes the marker its own Stop fetches, or is counted unsaved."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def proof(tmp_path, client):
    """The private installation proof the briefing hook keeps beside the config."""
    path = tmp_path / f"mcp-headers-{client}.json"
    path.write_text(json.dumps({"X-Pensieve-Plugin": f"{client} {PROOF}"}))
    path.chmod(0o600)


def answering(monkeypatch, text):
    """Pensieve gives ``text`` for every late marker, and records who asked."""
    asked = []

    def fetch(endpoint, token, client, session, turn_id, timeout):
        asked.append({"token": token, "client": client, "session": session, "turn_id": turn_id})
        return text

    monkeypatch.setattr(capture, "fetch_marker", fetch)
    return asked


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_a_turn_whose_briefing_failed_is_saved_with_the_marker_its_stop_fetches(
    tmp_path, monkeypatch, client
):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    proof(tmp_path, client)
    asked = answering(monkeypatch, marker(client))
    append(path, user("Asked while Pensieve stalled", client), assistant("Answered", client))
    run()
    assert contents(calls) == ["Asked while Pensieve stalled", "Answered"]
    assert asked == [{"token": PROOF, "client": client, "session": SESSION, "turn_id": None}]
    assert pending(state) == 0
    assert "unsaved_turns" not in capture.spool_status(state, client, SESSION)
    # The next turn, briefed as usual, continues the same saved conversation.
    append(path, user("Next", client), hook_record(client), assistant("Next answer", client))
    run()
    saved = events(calls)
    assert [event["content"] for event in saved[-2:]] == ["Next", "Next answer"]
    assert saved[-2]["capture"]["parent"]["event_id"] == saved[-3]["event_id"]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_only_an_unmarked_turn_at_its_stop_asks_for_a_marker(tmp_path, monkeypatch, client):
    path, _cfg, _state, calls, run = setup(tmp_path, monkeypatch, client)
    proof(tmp_path, client)
    asked = answering(monkeypatch, marker(client))
    append(path, user("Briefed", client), hook_record(client), assistant("Answer", client))
    run()
    append(path, user("Unmarked", client), assistant("Mid-turn", client))
    run("UserPromptSubmit")
    run("SessionEnd")
    assert asked == []
    assert contents(calls) == ["Briefed", "Answer"]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_a_turn_no_marker_reaches_is_counted_and_noticed_once(tmp_path, monkeypatch, client):
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch, client)
    proof(tmp_path, client)
    append(path, user("Lost", client), assistant("Lost answer", client))
    run()
    assert calls == [] and pending(state) == 0
    assert capture.spool_status(state, client, SESSION)["unsaved_turns"] == 1
    notice = prompt(run)
    assert "did not save 1 earlier turn" in notice and f"--status {SESSION}" in notice
    assert prompt(run) is None
    status = capture.conversation_status(
        {"session_id": SESSION, "hook_event_name": "UserPromptSubmit"}, client, SESSION, cfg, state
    )
    assert "Not saved: 1 earlier turn" in json.dumps(status)
    # A later turn is saved as usual; a second loss is noticed again.
    append(path, user("Saved", client), hook_record(client), assistant("Kept", client))
    append(path, user("Lost again", client), assistant("Lost again answer", client))
    run()
    assert contents(calls) == ["Saved", "Kept"]
    assert "did not save 1 earlier turn" in prompt(run)


def test_without_an_installation_proof_the_turn_is_given_up(tmp_path, monkeypatch):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    asked = answering(monkeypatch, marker())
    append(path, user("Lost"), assistant("Lost answer"))
    run()
    assert asked == [] and calls == []
    assert capture.spool_status(state, "codex", SESSION)["unsaved_turns"] == 1


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
    answering(monkeypatch, late)
    append(path, user("Asked"), assistant("Answered"))
    run()
    assert calls == [] and pending(state) == 0


def test_a_late_marker_cannot_cross_a_key_change(tmp_path, monkeypatch):
    """The turn's own starting position decides: a key replaced since refuses the marker."""
    path, cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, marker())
    append(path, user("Under the old key"), assistant("Answered"))
    config(tmp_path, [{"user_id": OWNER, "upload_key": "synthetic-replacement-key"}])
    run()
    assert calls == [] and pending(state) == 0


def test_a_late_marker_names_the_codex_turn_it_was_asked_for(tmp_path, monkeypatch):
    turn = "0a5e0b53-8178-4a3c-8d40-a07414144741"
    path, _cfg, _state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    asked = answering(monkeypatch, marker(turn=turn))
    append(path, {"type": "turn_context", "payload": {"turn_id": turn}}, user(), assistant())
    run()
    assert asked[0]["turn_id"] == turn
    assert contents(calls) == ["Visible question", "Visible answer"]


def test_a_late_marker_for_another_codex_turn_is_refused(tmp_path, monkeypatch):
    current, other = "0a5e0b53-8178-4a3c-8d40-a07414144741", "1b5e0b53-8178-4a3c-8d40-a07414144741"
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, marker(turn=other))
    append(path, {"type": "turn_context", "payload": {"turn_id": current}}, user(), assistant())
    run()
    assert calls == [] and pending(state) == 0


def test_a_late_marker_without_a_company_holds_the_turn_until_one(tmp_path, monkeypatch):
    path, _cfg, _state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, marker(context=None, generation=None))
    append(path, user("Before any company"), assistant("Held"))
    run()
    assert calls == []
    append(path, user("In a company"), hook_record(), assistant("Company answer"))
    run()
    assert contents(calls) == ["Before any company", "Held", "In a company", "Company answer"]


def test_a_late_marker_with_sharing_off_saves_nothing_and_is_not_a_loss(tmp_path, monkeypatch):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, marker(generation=None))
    append(path, user("Not shared"), assistant("Answer"))
    run()
    assert calls == [] and pending(state) == 0
    assert "unsaved_turns" not in capture.spool_status(state, "codex", SESSION)


def test_a_late_transcript_marker_still_cannot_attribute_the_turn(tmp_path, monkeypatch):
    """Only the fetch asks for this turn; a marker written after its answer is someone else's."""
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, None)
    append(path, user("Unmarked"), assistant("Answer"), hook_record())
    run()
    assert calls == [] and pending(state) == 0


def test_an_unmarked_turn_past_its_limit_is_given_up_early(tmp_path, monkeypatch):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    asked = answering(monkeypatch, marker())
    monkeypatch.setattr(capture_state, "UNMARKED_LIMIT_BYTES", 2048)
    append(path, user("Long"), *(assistant("x" * 1500) for _ in range(3)))
    run()
    assert calls == [] and asked == [] and pending(state) == 0
    assert capture.spool_status(state, "codex", SESSION)["unsaved_turns"] == 1


def test_an_unmarked_turn_never_takes_room_queued_uploads_need(tmp_path, monkeypatch):
    path, _cfg, state, calls, run = setup(tmp_path, monkeypatch)
    proof(tmp_path, "codex")
    answering(monkeypatch, marker())
    monkeypatch.setattr(capture, "MAX_STATE_PAGES", 40)
    append(path, user("x" * 32000), *(assistant("x" * 32000) for _ in range(3)))
    append(path, user("Valid prompt"), hook_record(), assistant("Valid answer"))
    run()
    assert contents(calls) == ["Valid prompt", "Valid answer"]
    assert pending(state) == 0


@pytest.fixture
def marker_service():
    seen = []
    reply = {"status": 200, "body": {"marker": marker()}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append(
                {
                    "path": self.path,
                    "authorization": self.headers["Authorization"],
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            body = json.dumps(reply["body"]).encode() if reply["status"] == 200 else b""
            self.send_response(reply["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hooks/conversations", seen, reply
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_the_marker_is_asked_for_beside_the_upload_route(marker_service):
    endpoint, seen, _reply = marker_service
    text = capture.fetch_marker(endpoint, PROOF, "codex", SESSION, None, 1.0)
    assert text == marker()
    assert seen == [
        {
            "path": "/hooks/marker",
            "authorization": f"Bearer {PROOF}",
            "body": {"client": "codex", "session_id": SESSION},
        }
    ]
    capture.fetch_marker(endpoint, PROOF, "codex", SESSION, GENERATION, 1.0)
    assert seen[-1]["body"]["turn_id"] == GENERATION


@pytest.mark.parametrize("status", [204, 401, 404, 503])
def test_no_marker_is_taken_from_a_refusal_or_an_older_service(marker_service, status):
    endpoint, _seen, reply = marker_service
    reply["status"] = status
    assert capture.fetch_marker(endpoint, PROOF, "codex", SESSION, None, 1.0) is None


def test_an_unreachable_service_gives_no_marker():
    assert (
        capture.fetch_marker(
            "http://127.0.0.1:9/hooks/conversations", PROOF, "codex", SESSION, None, 0.2
        )
        is None
    )
    assert (
        capture.fetch_marker(
            "https://example.com/hooks/conversations", PROOF, "codex", SESSION, None, 0.2
        )
        is None
    )
