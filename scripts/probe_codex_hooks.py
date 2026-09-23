"""Probe installed Codex hooks against synthetic local MCP and model servers.

Run with Python's standard library. No credentials, real model calls, global
configuration changes or full prompt logging are required. Probe receipts are
written beneath a disposable output directory. ``--persist`` additionally saves
the synthetic conversation in Codex's ordinary local session store for resume
and transcript inspection; omit it for an ephemeral probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

RECEIPT_TOKEN = "a" * 64 + "." + "b" * 32
CAPTURE_OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
CAPTURE_KEY = "synthetic-upload-only-key-not-a-real-credential"


def append(path: Path, value: dict[str, Any]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(value) + "\n")


def mcp_server(output: Path) -> None:
    """Serve synthetic tools over stdio, retaining only fixture request data."""
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if "id" not in request:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pensieve-hook-probe", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": "Synthetic hook probe; returns a fixture marker.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        },
                        "annotations": {"readOnlyHint": True},
                    }
                    for name in ("context_briefing", "ordinary", "set_context", "edit_page")
                ]
            }
        elif method == "tools/call":
            params = request["params"]
            append(output / "mcp.jsonl", {"pid": os.getpid(), **params})
            event = params.get("arguments", {}).get("event", "ordinary")
            marker = "HOOK_PROBE_" + event.upper()
            if source := params.get("arguments", {}).get("source"):
                marker += "_" + source.upper()
            result = {"content": [{"type": "text", "text": marker}]}
            if params["name"] == "ordinary":
                result["content"][0]["text"] += " SOURCE_CONTEXT_SENTINEL"
            if params["name"] == "set_context":
                result["content"][0]["text"] = (
                    "DESTINATION_CONTEXT_SENTINEL\n<!-- pensieve-capture-context "
                    + json.dumps(
                        {
                            "v": 2,
                            "capture_generation": CAPTURE_OWNER,
                            "kind": "selection",
                            "user_id": CAPTURE_OWNER,
                            "client": "codex",
                            "conversation_id": params["_meta"]["threadId"],
                            "context_id": 12,
                            "turn_id": None,
                        }
                    )
                    + " -->"
                )
            if params["name"] == "context_briefing":
                args = params.get("arguments", {})
                context_marker = ""
                if event == "UserPromptSubmit":
                    context_marker = (
                        "\n<!-- pensieve-capture-context "
                        + json.dumps(
                            {
                                "v": 2,
                                "capture_generation": CAPTURE_OWNER,
                                "kind": "prompt",
                                "user_id": CAPTURE_OWNER,
                                "client": "codex",
                                "conversation_id": args["session_id"],
                                "context_id": 497,
                                "turn_id": args.get("turn_id"),
                            }
                        )
                        + " -->"
                    )
                result["content"][0]["text"] = json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": event,
                            "additionalContext": marker
                            + f"\n<!-- pensieve-delivery token={RECEIPT_TOKEN} -->"
                            + context_marker,
                        }
                    }
                )
                result["structuredContent"] = {"ignored_marker": "HOOK_PROBE_STRUCTURED_ONLY"}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


def run_probe(
    output: Path,
    *,
    persist: bool = False,
    resume: str | None = None,
    fork: str | None = None,
    interrupt: bool = False,
    compact: bool = False,
    capture: bool = False,
    capture_state: Path | None = None,
    context_switch: str | None = None,
) -> dict[str, Any]:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = output / "workspace"
    workspace.mkdir()
    plugin = output / "plugin"
    shutil.copytree(Path(__file__).resolve().parents[1] / "pensieve", plugin)
    sequence = 0
    capture_attempts = []
    model_waiting = threading.Event()
    release_model = threading.Event()
    capture_batch_scopes = {}
    capture_config = output / "capture-config.json"
    if capture:
        if not persist:
            raise ValueError("capture probe requires --persist for a real local transcript")
        capture_config.write_text(
            json.dumps(
                {
                    "version": 3,
                    "profiles": [
                        {
                            "user_id": CAPTURE_OWNER,
                            "upload_key": CAPTURE_KEY,
                            "client": "codex",
                            "installation_id": "11111111-1111-4111-8111-111111111111",
                            "runtime": "unknown",
                            "host_version": "",
                        }
                    ],
                }
            )
        )
        capture_config.chmod(0o600)

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_GET(self) -> None:
            payload = json.dumps({"models": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            nonlocal sequence
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            request = json.loads(raw)
            if self.path == "/hooks/conversations":
                assert self.headers.get("Authorization") == "Bearer " + CAPTURE_KEY
                scope = request["context_id"]
                assert scope in {497, 12}
                capture_batch_scopes[request["batch_id"]] = scope
                capture_attempts.append((request, hashlib.sha256(raw).hexdigest()))
                append(
                    output / "capture.jsonl",
                    {
                        "body": request,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "context_id": scope,
                    },
                )
                if len(capture_attempts) == 1 and not interrupt:
                    self.send_response(503)
                    self.end_headers()
                    return
                payload = json.dumps(
                    {
                        "batch_id": request["batch_id"],
                        "batch_sha256": hashlib.sha256(raw).hexdigest(),
                        "conversation_id": "c73e0b53-8178-4a3c-8d40-a07414144741",
                        "segment_id": request["segment_id"],
                        "expires_at": None,
                        "accepted_events": len(request["events"]),
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/hooks/delivery":
                append(output / "receipts.jsonl", request)
                self.send_response(204)
                self.end_headers()
                return
            sequence += 1
            items = request.get("input", [])
            markers = []
            for item in items:
                markers.extend(re.findall(r"HOOK_PROBE_[A-Z_]+", json.dumps(item)))
            append(
                output / "model.jsonl",
                {
                    "sequence": sequence,
                    "path": self.path,
                    "markers": markers,
                    "local_primer_delivered": "Pensieve is the company's shared, curated context layer"
                    in json.dumps(items),
                    "capture_key_in_model": any(key in json.dumps(items) for key in (CAPTURE_KEY,)),
                },
            )
            has_result = any(
                item.get("type") in {"function_call_output", "custom_tool_call_output"}
                for item in items
            )
            if interrupt:
                model_waiting.set()
                release_model.wait(timeout=120)
                return
            if not has_result and (not compact or sequence == 1):
                item = {
                    "type": "function_call",
                    "call_id": "probe-ordinary",
                    "namespace": "mcp__pensieve",
                    "name": "set_context" if context_switch else "ordinary",
                    "arguments": "{}",
                }
                if context_switch == "code-mode":
                    item = {
                        "type": "custom_tool_call",
                        "call_id": "probe-wrapper",
                        "namespace": "functions",
                        "name": "exec",
                        "input": "text(await tools.mcp__pensieve__edit_page({})); text(await tools.mcp__pensieve__set_context({})); text(await tools.mcp__pensieve__edit_page({}));",
                    }
            else:
                item = {
                    "type": "message",
                    "role": "assistant",
                    "id": "probe-answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "DESTINATION_ANSWER_SENTINEL"
                            if context_switch
                            else "Synthetic probe complete.",
                        }
                    ],
                }
            events = [
                {"type": "response.created", "response": {"id": f"probe-{sequence}"}},
                {"type": "response.output_item.done", "item": item},
                {
                    "type": "response.completed",
                    "response": {
                        "id": f"probe-{sequence}",
                        "usage": {
                            "input_tokens": 100000 if compact and sequence == 1 else 0,
                            "output_tokens": 0,
                            "total_tokens": 100000 if compact and sequence == 1 else 0,
                        },
                    },
                },
            ]
            payload = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hooks = json.loads((plugin / "hooks/codex.json").read_text())["hooks"]
    # Direct CLI settings need the plugin-root expansion normally supplied by
    # the loader. Otherwise execute the generated adapters and bundled helper,
    # changing only its receipt destination to the synthetic local service.
    for groups in hooks.values():
        for group in groups:
            for hook in group["hooks"]:
                if hook["type"] == "command":
                    hook["command"] = hook["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(plugin))
                    if "context_receipt.py" in hook["command"]:
                        hook["command"] += (
                            f" --endpoint http://127.0.0.1:{server.server_port}/hooks/delivery"
                        )
                    else:
                        hook["command"] += (
                            f' --config "{capture_config}" --state "{capture_state or output / "capture-spool"}" --endpoint http://127.0.0.1:{server.server_port}/hooks/conversations'
                        )

    # CLI overrides are parsed as TOML; serialise through a deliberately tiny
    # encoder rather than shell interpolation. All subprocess arguments are raw.
    def toml(value: Any) -> str:
        if isinstance(value, dict):
            return "{" + ",".join(json.dumps(k) + "=" + toml(v) for k, v in value.items()) + "}"
        if isinstance(value, list):
            return "[" + ",".join(toml(v) for v in value) + "]"
        return json.dumps(value)

    settings = {
        "model": "gpt-5.4",
        "model_provider": "hookprobe",
        "model_providers.hookprobe": {
            "name": "Synthetic local probe",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "wire_api": "responses",
            "requires_openai_auth": False,
        },
        "mcp_servers.pensieve": {
            "command": sys.executable,
            "args": [str(Path(__file__).resolve()), "--mcp", str(output)],
        },
        "hooks": hooks,
        "features.hooks": True,
        "features.code_mode": context_switch == "code-mode",
        "features.apps": False,
        "features.memories": False,
        "features.shell_snapshot": False,
        "sqlite_home": str(output / "state"),
        "log_dir": str(output / "logs"),
        "history.persistence": "none",
        "analytics.enabled": False,
        "feedback.enabled": False,
    }
    if compact:
        settings["model_auto_compact_token_limit"] = 50000
    command = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--dangerously-bypass-hook-trust",
        "--json",
        "-C",
        str(workspace),
    ]
    if not persist:
        command.append("--ephemeral")
    for key, value in settings.items():
        command.extend(["-c", key + "=" + toml(value)])
    if fork:
        command.extend(["fork", fork])
    elif resume:
        command.extend(["resume", resume])
    command.append("Run the synthetic fixture.")
    try:
        # Codex 0.154.0 can drain its in-process client through two 45-second
        # shutdown waits after turn.completed. Allow those plus the fixture
        # turn; individual generated hooks retain their five-second timeout.
        if interrupt:
            with subprocess.Popen(
                command,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                if not model_waiting.wait(timeout=30):
                    process.kill()
                    raise RuntimeError("Codex never reached the interrupt fixture")
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=120)
                result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        else:
            result = subprocess.run(
                command, input="", text=True, capture_output=True, timeout=120, check=False
            )
    except subprocess.TimeoutExpired as exc:
        (output / "events.jsonl").write_bytes(exc.stdout or b"")
        (output / "stderr.txt").write_bytes(exc.stderr or b"")
        raise RuntimeError(f"Codex timed out; inspect {output / 'events.jsonl'}") from None
    finally:
        release_model.set()
        server.shutdown()
        server.server_close()
    (output / "events.jsonl").write_text(result.stdout)
    (output / "stderr.txt").write_text(result.stderr)
    if result.returncode and not interrupt:
        raise RuntimeError(f"Codex exited {result.returncode}; inspect {output / 'stderr.txt'}")
    calls = [json.loads(line) for line in (output / "mcp.jsonl").read_text().splitlines()]
    model_requests = [
        json.loads(line) for line in (output / "model.jsonl").read_text().splitlines()
    ]
    assert model_requests[0]["local_primer_delivered"]
    assert "HOOK_PROBE_USERPROMPTSUBMIT" in model_requests[0]["markers"]
    assert all(
        call["arguments"].get("source") not in {"startup", "resume"}
        for call in calls
        if call["name"] == "context_briefing"
    )
    assert all("HOOK_PROBE_STRUCTURED_ONLY" not in request["markers"] for request in model_requests)
    thread_ids = {call["_meta"]["threadId"] for call in calls}
    assert len(thread_ids) == 1
    assert all(
        call["arguments"]["session_id"] == call["_meta"]["threadId"]
        for call in calls
        if call["name"] == "context_briefing"
    )
    if compact:
        assert model_requests[-1]["markers"] == ["HOOK_PROBE_SESSIONSTART_COMPACT"]
    receipt_path = output / "receipts.jsonl"
    receipts = (
        [json.loads(line) for line in receipt_path.read_text().splitlines()]
        if receipt_path.exists()
        else []
    )
    assert all(set(receipt) == {"token", "operation"} for receipt in receipts)
    operations = {receipt["operation"] for receipt in receipts}
    if persist and not interrupt:
        assert "ack" in operations
    capture_checks = {}
    if capture:
        accepted = {body["batch_id"]: body for body, _ in capture_attempts[0 if interrupt else 1 :]}
        captured = [event for body in accepted.values() for event in body["events"]]
        capture_checks = {
            "native_turn_identity": all(
                event.get("capture", {}).get("turn_id")
                in {
                    call["arguments"].get("turn_id")
                    for call in calls
                    if call["name"] == "context_briefing"
                    and call["arguments"].get("event") == "UserPromptSubmit"
                }
                for event in captured
            ),
            "explicit_completion": sum(
                event["kind"] == "turn_end"
                and event.get("capture", {}).get("completion") == "completed"
                for event in captured
            )
            == 1,
            "tool_call_ids": all(
                event.get("capture", {}).get("tool_call_id")
                for event in captured
                if event["kind"] in {"tool_call", "tool_result"}
            ),
            "prompt_and_answer_captured": {"user", "assistant"}
            <= {event["kind"] for event in captured},
            "retry_identical_bytes": len(capture_attempts) > 1
            and capture_attempts[0] == capture_attempts[1],
            "captured_visible_work": any(body["events"] for body in accepted.values()),
            "credentials_never_reach_model": all(
                not row["capture_key_in_model"] for row in model_requests
            ),
            "no_hook_or_reasoning_payload": all(
                "pensieve-capture-context" not in event["content"]
                and "pensieve-delivery" not in event["content"]
                for event in captured
            ),
            "stable_distinct_events": len({event["event_id"] for event in captured})
            == len(captured),
        }
        if interrupt:
            capture_checks.pop("prompt_and_answer_captured")
            capture_checks.pop("retry_identical_bytes")
            capture_checks.pop("explicit_completion")
            capture_checks["interrupted_without_completion"] = (
                any(event["kind"] == "user" for event in captured)
                and any(
                    event.get("capture", {}).get("completion") == "interrupted"
                    for event in captured
                )
                and not any(
                    event.get("capture", {}).get("completion") == "completed" for event in captured
                )
            )
        for segment in {body["segment_id"] for body in accepted.values()}:
            ordered = sorted(
                (
                    event
                    for body in accepted.values()
                    if body["segment_id"] == segment
                    for event in body["events"]
                ),
                key=lambda event: event["sequence"],
            )
            capture_checks["ordered_" + segment] = all(
                current["capture"].get("parent")
                == {
                    "host_conversation_id": next(iter(thread_ids)),
                    "event_id": previous["event_id"],
                }
                for previous, current in zip(ordered, ordered[1:])
            )
        if fork:
            first = min(captured, key=lambda event: event["sequence"])
            capture_checks["exact_fork_parent"] = (
                first["capture"].get("parent", {}).get("host_conversation_id") == fork
                and next(iter(thread_ids)) != fork
            )
        elif resume:
            first = min(captured, key=lambda event: event["sequence"])
            capture_checks["resumed_parent"] = (
                first["capture"].get("parent", {}).get("host_conversation_id") == resume
            )
        if context_switch:
            source_events = [
                event
                for batch in accepted.values()
                if capture_batch_scopes[batch["batch_id"]] == 497
                for event in batch["events"]
            ]
            destination_events = [
                event
                for batch in accepted.values()
                if capture_batch_scopes[batch["batch_id"]] == 12
                for event in batch["events"]
            ]
            capture_checks.pop("prompt_and_answer_captured")
            capture_checks.update(
                {
                    "source_prompt_consent": any(
                        event["kind"] == "user" for event in source_events
                    ),
                    "destination_answer_consent": any(
                        event["content"] == "DESTINATION_ANSWER_SENTINEL"
                        for event in destination_events
                    ),
                    "company_isolation": "DESTINATION_" not in json.dumps(source_events)
                    and "SOURCE_CONTEXT_SENTINEL" not in json.dumps(destination_events),
                    "selection_result_once": sum(
                        "DESTINATION_CONTEXT_SENTINEL" in event["content"]
                        for event in destination_events
                    )
                    == 1,
                }
            )
            if context_switch == "code-mode":
                writes = [call for call in calls if call["name"] == "edit_page"]
                for scope, write in zip((source_events, destination_events), writes):
                    native_id = write["_meta"]["callId"]
                    capture_checks["native_write_" + native_id] = (
                        sum(
                            event.get("capture", {}).get("tool_call_id") == native_id
                            for event in scope
                        )
                        == 1
                    )
                capture_checks["two_native_writes"] = len(writes) == 2
                capture_checks["combined_output_omitted"] = (
                    "SOURCE_CONTEXT_SENTINEL" not in json.dumps(captured)
                )
        if not all(capture_checks.values()):
            raise RuntimeError("Codex capture probe failed: " + json.dumps(capture_checks))
    return {
        "exit_code": result.returncode,
        "output": str(output),
        "model_requests": sequence,
        "thread_id": next(iter(thread_ids)),
        "ordinary_call_seen": any(call["name"] == "ordinary" for call in calls),
        "same_mcp_process": len({call["pid"] for call in calls}) == 1,
        "receipt_operations": sorted(operations),
        "capture_checks": capture_checks,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--resume")
    parser.add_argument("--fork")
    parser.add_argument("--interrupt", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--capture-state", type=Path)
    parser.add_argument("--context-switch", choices=("direct", "code-mode"))
    args = parser.parse_args()
    if args.mcp:
        mcp_server(args.mcp)
    else:
        destination = (
            args.output or Path(tempfile.mkdtemp(prefix="pensieve-hook-probe-")) / "receipts"
        )
        print(
            json.dumps(
                run_probe(
                    destination,
                    persist=args.persist,
                    resume=args.resume,
                    fork=args.fork,
                    interrupt=args.interrupt,
                    compact=args.compact,
                    capture=args.capture,
                    capture_state=args.capture_state,
                    context_switch=args.context_switch,
                ),
                indent=2,
            )
        )
