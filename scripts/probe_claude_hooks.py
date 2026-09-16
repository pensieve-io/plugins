"""Probe Claude Code hooks against disposable, synthetic local MCP/model servers.

Run with Python 3.12 and an installed ``claude`` CLI. This uses a temporary
CLAUDE_CONFIG_DIR, a session-only plugin and a loopback fake Anthropic endpoint;
it neither loads Pensieve credentials nor pays for inference. No personal
settings are changed. Output reports delivery and identity, never raw prompts.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

MARKER = "PENSIEVE_SYNTHETIC_GROUNDING_"
RECEIPT_TOKEN = "a" * 64 + "." + "b" * 32
CAPTURE_OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
CAPTURE_KEY = "synthetic-upload-only-key-not-a-real-credential"


class ModelStub(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []
    mcp_requests: ClassVar[list[dict[str, Any]]] = []
    receipt_requests: ClassVar[list[dict[str, Any]]] = []
    capture_requests: ClassVar[list[dict[str, Any]]] = []
    fail_hook: bool = False

    def reply_json(self, value: dict[str, Any], *, session: str | None = None) -> None:
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw)
        if self.path == "/hooks/conversations":
            assert self.headers.get("Authorization") == "Bearer " + CAPTURE_KEY
            self.capture_requests.append({"body": body, "sha": hashlib.sha256(raw).hexdigest()})
            if len(self.capture_requests) == 1:
                self.send_response(503)
                self.end_headers()
                return
            self.reply_json(
                {
                    "batch_id": body["batch_id"],
                    "batch_sha256": hashlib.sha256(raw).hexdigest(),
                    "conversation_id": "c73e0b53-8178-4a3c-8d40-a07414144741",
                    "segment_id": body["segment_id"],
                    "expires_at": "2026-12-31T00:00:00+00:00",
                    "accepted_events": len(body["events"]),
                }
            )
            return
        if self.path == "/hooks/delivery":
            self.receipt_requests.append(body)
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/mcp":
            session = self.headers.get("Mcp-Session-Id")
            method = body.get("method")
            if method == "initialize":
                session = str(uuid.uuid4())
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "pensieve-synthetic-probe", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "context_briefing",
                            "description": "Synthetic hook probe",
                            "inputSchema": {"type": "object", "additionalProperties": True},
                        }
                    ]
                }
            elif method == "tools/call":
                args = body.get("params", {}).get("arguments", {})
                event, source = args.get("event", "unknown"), args.get("source", "")
                capture_marker = ""
                if event == "UserPromptSubmit":
                    capture_marker = (
                        "\n<!-- pensieve-capture-context "
                        + json.dumps(
                            {
                                "v": 1,
                                "kind": "prompt",
                                "user_id": CAPTURE_OWNER,
                                "client": "claude",
                                "conversation_id": args["session_id"],
                                "context_id": 497,
                                "turn_id": None,
                            }
                        )
                        + " -->"
                    )
                payload = {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "additionalContext": f"{MARKER}{event}_{source}\n<!-- pensieve-delivery token={RECEIPT_TOKEN} -->"
                        + capture_marker,
                    }
                }
                result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
                if self.fail_hook and event == "UserPromptSubmit":
                    result = {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": "Synthetic temporary hook failure",
                            }
                        ],
                    }
            else:
                result = {}
            self.mcp_requests.append(
                {"method": method, "params": body.get("params"), "transport_id": session}
            )
            if "id" not in body:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.reply_json(
                    {"jsonrpc": "2.0", "id": body["id"], "result": result}, session=session
                )
            return
        self.requests.append({"path": self.path, "body": body})
        if "count_tokens" in self.path:
            response = json.dumps({"input_tokens": 100}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        response = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-sonnet-4-6"),
            "content": [{"type": "text", "text": "Synthetic probe complete."}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 5},
        }
        messages = body.get("messages", [])
        if "Synthetic ordinary tool probe" in json.dumps(messages) and not any(
            entry.get("type") == "tool_result"
            for message in messages
            for entry in message.get("content", [])
            if isinstance(entry, dict)
        ):
            tool_name = next(
                tool["name"]
                for tool in body.get("tools", [])
                if tool["name"].endswith("context_briefing")
            )
            response["content"] = [
                {
                    "type": "tool_use",
                    "id": "probe_tool_call",
                    "name": tool_name,
                    "input": {"ordinary": True},
                }
            ]
            response["stop_reason"] = "tool_use"
        if not body.get("stream"):
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        tool_use = response["content"][0]["type"] == "tool_use"
        block = (
            {**response["content"][0], "input": {}} if tool_use else {"type": "text", "text": ""}
        )
        delta = (
            {"type": "input_json_delta", "partial_json": '{"ordinary":true}'}
            if tool_use
            else {
                "type": "text_delta",
                "text": "Synthetic probe complete.",
            }
        )
        events = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        **response,
                        "content": [],
                        "stop_reason": None,
                    },
                },
            ),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": block},
            ),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": response["stop_reason"],
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": 5},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        data = "".join(
            f"event: {name}\ndata: {json.dumps(value)}\n\n" for name, value in events
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def stream_probe(common: list[str], env: dict[str, str], root: Path) -> list[dict[str, Any]]:
    """Exercise clear while the host and its MCP connection stay running."""
    events: queue.Queue[dict[str, Any]] = queue.Queue()
    process = subprocess.Popen(
        [*common, "--input-format", "stream-json"],
        cwd=root,
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            with contextlib.suppress(json.JSONDecodeError):
                events.put(json.loads(line))

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    report = []
    try:
        for prompt in ["Synthetic stream first prompt.", "/clear", "Synthetic stream after clear."]:
            before = len(ModelStub.requests)
            before_mcp = len(ModelStub.mcp_requests)
            assert process.stdin is not None
            process.stdin.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": prompt,
                        },
                        "parent_tool_use_id": None,
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            received = []
            while True:
                event = events.get(timeout=30)
                received.append(event)
                if event.get("type") == "result":
                    break
            messages = [
                entry["body"].get("messages", [])
                for entry in ModelStub.requests[before:]
                if "count_tokens" not in entry["path"]
            ]
            report.append(
                {
                    "prompt": prompt,
                    "model_requests": len(messages),
                    "grounding_delivered": MARKER in json.dumps(messages),
                    "mcp_calls": ModelStub.mcp_requests[before_mcp:],
                    "session_ids": sorted(
                        {entry["session_id"] for entry in received if "session_id" in entry}
                    ),
                    "hook_events": [
                        entry.get("hook_name")
                        for entry in received
                        if entry.get("subtype") == "hook_response"
                    ],
                }
            )
        return report
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=5)


def run_probe(claude: str, *, capture: bool = False) -> dict[str, Any]:
    ModelStub.requests.clear()
    ModelStub.mcp_requests.clear()
    ModelStub.receipt_requests.clear()
    ModelStub.capture_requests.clear()
    with tempfile.TemporaryDirectory(prefix="pensieve-claude-hooks-") as directory:
        root = Path(directory)
        plugin = root / "plugin"
        capture_config = root / "capture.json"
        if capture:
            capture_config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "profiles": [{"user_id": CAPTURE_OWNER, "upload_key": CAPTURE_KEY}],
                    }
                )
            )
            capture_config.chmod(0o600)
        shutil.copytree(Path(__file__).resolve().parents[1] / "pensieve", plugin)
        server = ThreadingHTTPServer(("127.0.0.1", 0), ModelStub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        hook_path = plugin / "hooks/hooks.json"
        hooks = json.loads(hook_path.read_text())
        # Keep generated inputs, matchers, timeouts and the bundled helper;
        # only its receipt destination changes to the synthetic local service.
        for groups in hooks["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    if hook["type"] == "command" and "context_receipt.py" in hook["command"]:
                        hook["command"] += (
                            f" --endpoint http://127.0.0.1:{server.server_port}/hooks/delivery"
                        )
                    elif hook["type"] == "command":
                        hook["command"] += (
                            f' --config "{capture_config}" --state "{root / "capture-spool"}" --endpoint http://127.0.0.1:{server.server_port}/hooks/conversations'
                        )
        hook_path.write_text(json.dumps(hooks))
        mcp_path = plugin / ".mcp.json"
        mcp = json.loads(mcp_path.read_text())
        mcp["mcpServers"]["pensieve"]["url"] = f"http://127.0.0.1:{server.server_port}/mcp"
        mcp_path.write_text(json.dumps(mcp))
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(
                (
                    "ANTHROPIC_",
                    "CLAUDE_",
                    "AWS_",
                    "AZURE_",
                    "GOOGLE_",
                    "OTEL_",
                )
            )
        }
        env.update(
            {
                "ANTHROPIC_API_KEY": "synthetic-local-probe-key",
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "CLAUDE_CONFIG_DIR": str(root / "config"),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_ERROR_REPORTING": "1",
            }
        )
        session = str(uuid.uuid4())
        failed_session = str(uuid.uuid4())
        common = [
            claude,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--setting-sources",
            "",
            "--plugin-dir",
            str(plugin),
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "mcp__plugin_pensieve_pensieve__context_briefing",
            "--model",
            "claude-sonnet-4-6",
            "--system-prompt",
            "You are a synthetic local lifecycle probe.",
        ]
        report: dict[str, Any] = {
            "claude_version": subprocess.check_output([claude, "--version"], text=True).strip(),
            "cases": [],
        }
        try:
            for label, extra, prompt in [
                ("startup", ["--session-id", session], "Synthetic first prompt."),
                ("resume", ["--resume", session], "Synthetic resumed prompt."),
                ("compact", ["--resume", session], "/compact"),
                ("after_compact", ["--resume", session], "Synthetic post-compaction prompt."),
                ("ordinary_tool", [], "Synthetic ordinary tool probe."),
                ("hook_failure", ["--session-id", failed_session], "Synthetic failed hook."),
                ("retry_after_failure", ["--resume", failed_session], "Synthetic retry hook."),
            ]:
                ModelStub.fail_hook = label == "hook_failure"
                before = len(ModelStub.requests)
                before_mcp = len(ModelStub.mcp_requests)
                debug = root / f"{label}.debug"
                result = subprocess.run(
                    [*common, "--debug-file", str(debug), *extra, prompt],
                    cwd=root,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=45,
                )
                requests = ModelStub.requests[before:]
                messages = [
                    entry["body"].get("messages", [])
                    for entry in requests
                    if "count_tokens" not in entry["path"]
                ]
                output_events = []
                for line in result.stdout.splitlines():
                    with contextlib.suppress(json.JSONDecodeError):
                        output_events.append(json.loads(line))
                report["cases"].append(
                    {
                        "case": label,
                        "exit_code": result.returncode,
                        "model_requests": len(messages),
                        "grounding_delivered": MARKER in json.dumps(messages),
                        "mcp_calls": ModelStub.mcp_requests[before_mcp:],
                        "compact_briefing_delivered": f"{MARKER}SessionStart_compact"
                        in json.dumps(messages),
                        "mcp_status": [
                            entry.get("mcp_servers")
                            for entry in output_events
                            if entry.get("subtype") == "init"
                        ],
                        "hook_events": [
                            entry
                            for entry in output_events
                            if str(entry.get("subtype", "")).startswith("hook_")
                        ],
                        "errors": [
                            entry.get("errors") for entry in output_events if entry.get("is_error")
                        ],
                        "local_primer_delivered": "Pensieve is the company's shared, curated context layer"
                        in json.dumps(messages),
                    }
                )
            report["stream_cases"] = stream_probe(common, env, root)
            report["mcp_events"] = ModelStub.mcp_requests
            report["receipt_requests"] = ModelStub.receipt_requests
            if capture:
                attempts = ModelStub.capture_requests
                accepted = {row["body"]["batch_id"]: row["body"] for row in attempts[1:]}
                captured = [event for body in accepted.values() for event in body["events"]]
                resumed_batches = [
                    body for body in accepted.values() if body["host_conversation_id"] == session
                ]
                report["capture_sessions"] = [
                    {
                        "conversation_id": conversation,
                        "segment_ids": sorted(
                            {
                                body["segment_id"]
                                for body in accepted.values()
                                if body["host_conversation_id"] == conversation
                            }
                        ),
                        "event_count": sum(
                            len(body["events"])
                            for body in accepted.values()
                            if body["host_conversation_id"] == conversation
                        ),
                    }
                    for conversation in sorted(
                        {body["host_conversation_id"] for body in accepted.values()}
                    )
                ]
                report["capture_checks"] = {
                    "prompt_and_answer_captured": {"user", "assistant"}
                    <= {event["kind"] for event in captured},
                    "first_prompt_captured": any(
                        event["kind"] == "user" and event["content"] == "Synthetic first prompt."
                        for event in captured
                    ),
                    "retry_identical_bytes": len(attempts) > 1 and attempts[0] == attempts[1],
                    "session_end_flushed": any(not body["is_active"] for body in accepted.values()),
                    "credentials_never_reach_model": CAPTURE_KEY
                    not in json.dumps(ModelStub.requests),
                    "no_hook_or_reasoning_payload": all(
                        "pensieve-capture-context" not in event["content"]
                        and "pensieve-delivery" not in event["content"]
                        and "hookSpecificOutput" not in event["content"]
                        and "<command-name>" not in event["content"]
                        and "This session is being continued" not in event["content"]
                        for event in captured
                    ),
                    "stable_distinct_events": len({event["event_id"] for event in captured})
                    == len(captured),
                    "resumed_session_keeps_one_segment": len(
                        {body["segment_id"] for body in resumed_batches}
                    )
                    == 1
                    and sum(
                        event["kind"] == "user"
                        for body in resumed_batches
                        for event in body["events"]
                    )
                    == 3,
                }
                report["captured_event_count"] = len(captured)
                if not all(report["capture_checks"].values()):
                    # Preserve only synthetic fixture diagnostics on failure.
                    report["capture_attempts"] = attempts
                    report["capture_pending"] = []
                    import sqlite3

                    for path in (root / "capture-spool").glob("*.sqlite3"):
                        db = sqlite3.connect(path)
                        report["capture_pending"].append(
                            {
                                "session": path.stem,
                                "state": db.execute("SELECT body FROM state").fetchall(),
                                "events": db.execute("SELECT body FROM events").fetchall(),
                            }
                        )
                        db.close()
            report["transcript_markers"] = []
            report["compaction_records"] = []
            report["transcript_record_types"] = []
            for path in (root / "config").rglob("*.jsonl"):
                records = [json.loads(line) for line in path.read_text().splitlines()]
                report["transcript_record_types"].append(
                    {
                        "file": str(path.relative_to(root)),
                        "types": sorted({str(entry.get("type")) for entry in records}),
                    }
                )
                report["transcript_markers"].extend(
                    entry for entry in records if MARKER in json.dumps(entry)
                )
                report["compaction_records"].extend(
                    entry for entry in records if "compact" in str(entry.get("subtype", ""))
                )
            return report
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def verify_report(report: dict[str, Any]) -> dict[str, bool]:
    cases = {entry["case"]: entry for entry in report["cases"]}

    def calls(case: dict[str, Any]) -> list[dict[str, Any]]:
        return [entry for entry in case["mcp_calls"] if entry["method"] == "tools/call"]

    first = calls(cases["startup"])[0]
    resumed = calls(cases["resume"])[0]
    stream_first, clear, after_clear = report["stream_cases"]
    clear_call = calls(clear)[0]
    stream_call = calls(stream_first)[0]
    ordinary = next(
        entry
        for entry in calls(cases["ordinary_tool"])
        if entry["params"]["arguments"].get("ordinary")
    )
    checks = {
        "first_prompt_grounded": cases["startup"]["grounding_delivered"],
        "startup_uses_local_primer": cases["startup"]["local_primer_delivered"]
        and all(
            call["params"]["arguments"].get("event") != "SessionStart"
            for call in calls(cases["startup"])
        ),
        "resume_keeps_conversation": first["params"]["arguments"]["session_id"]
        == resumed["params"]["arguments"]["session_id"],
        "resume_reconnects_transport": first["transport_id"] != resumed["transport_id"],
        "compaction_restores_context": cases["after_compact"]["compact_briefing_delivered"],
        "clear_changes_conversation_on_same_transport": clear_call["transport_id"]
        == stream_call["transport_id"]
        and clear_call["params"]["arguments"]["session_id"]
        != stream_call["params"]["arguments"]["session_id"],
        "after_clear_grounded": after_clear["grounding_delivered"],
        "ordinary_tool_has_no_conversation_metadata": set(ordinary["params"].get("_meta", {}))
        == {"claudecode/toolUseId", "progressToken"},
        "failed_hook_not_injected": not cases["hook_failure"]["grounding_delivered"],
        "failed_hook_retries": cases["retry_after_failure"]["grounding_delivered"],
        "receipt_helper_sends_ack_and_reset": {
            entry["operation"] for entry in report["receipt_requests"]
        }
        == {"ack", "reset"},
        "receipt_helper_sends_only_token_and_operation": all(
            set(entry) == {"token", "operation"} for entry in report["receipt_requests"]
        ),
    }
    failures = [
        name
        for name, passed in {**checks, **report.get("capture_checks", {})}.items()
        if not passed
    ]
    if failures:
        raise RuntimeError("Claude host probe failed: " + ", ".join(failures))
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", default=shutil.which("claude"))
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    if not args.claude:
        parser.error("An installed Claude Code CLI is required")
    else:
        report = run_probe(args.claude, capture=args.capture)
        try:
            report["checks"] = verify_report(report)
        finally:
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
