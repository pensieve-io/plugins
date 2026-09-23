"""Native host decoding into one visible-event and attribution contract.

Adapter state contains native call/turn bookkeeping only. It never chooses a
capture scope, file cursor, consent interval or upload batch.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal, TypedDict

from context_receipt import accepted_contexts, conversation_id


class EventFields(TypedDict, total=False):
    content: str
    native_turn_id: str | None
    tool_call_id: str | None
    tool_name: str
    completion: Literal["completed", "interrupted", "failed"]
    marker: dict
    truncated: bool


class CaptureEvent(EventFields):
    kind: Literal[
        "user",
        "assistant",
        "tool_call",
        "tool_result",
        "turn_end",
        "attribution",
        "unknown_boundary",
    ]


CONTEXT_MARKER = re.compile(r"<!-- pensieve-capture-context (\{[^\r\n]*?\}) -->")
SET_CONTEXT_NAMES = {
    "mcp__pensieve__set_context",
    "mcp__plugin_pensieve_pensieve__set_context",
    "mcp__plugin:pensieve:pensieve__set_context",
}


def parse_marker(text: str, client: str, session: str, kind: str) -> dict | None:
    matches = CONTEXT_MARKER.findall(text)
    if len(matches) != 1:
        return None
    try:
        marker = json.loads(matches[0])
    except ValueError:
        return None
    if not isinstance(marker, dict) or set(marker) != {
        "v",
        "capture_generation",
        "kind",
        "user_id",
        "client",
        "conversation_id",
        "context_id",
        "turn_id",
    }:
        return None
    context, turn = marker["context_id"], marker["turn_id"]
    if (
        marker["v"] != 2
        or (
            marker["capture_generation"] is not None
            and conversation_id(marker["capture_generation"]) is None
        )
        or marker["kind"] != kind
        or marker["client"] != client
        or conversation_id(marker["conversation_id"]) != session
        or conversation_id(marker["user_id"]) is None
        or (context is not None and (type(context) is not int or context <= 0))
        or (turn is not None and (not isinstance(turn, str) or not 0 < len(turn) <= 255))
    ):
        return None
    return marker


def visible_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"text", "input_text", "output_text"} and isinstance(
            item.get("text"), str
        ):
            parts.append(item["text"])
        elif item.get("type") in {"image", "input_image", "image_url", "document", "audio"}:
            parts.append("[Attachment omitted; original remains in the host conversation]")
    return "\n".join(parts)


def tool_call(name: str, call_id: object, source: dict, input_field: str) -> dict:
    """Keep input in the bounded/redacted body, and only its name in metadata.

    A missing input remains a legacy name-only call; an explicitly empty input
    is still captured. Never derive inputs from results or completion-only events.
    """
    event = {"kind": "tool_call", "content": name[:255], "tool_call_id": call_id}
    if input_field in source and capture_id(name[:255]):
        value = source[input_field]
        if input_field == "arguments" and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        event["content"] = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        )
        event["tool_name"] = name[:255]
    return event


def aware_time(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
    return None


def is_code_mode_tool(name: object, namespace: object) -> bool:
    return isinstance(name, str) and name in {"exec", "wait"} and namespace in {None, "functions"}


def is_set_context(name: object, namespace: object = None) -> bool:
    return isinstance(name, str) and (
        name in SET_CONTEXT_NAMES or (name == "set_context" and namespace == "mcp__pensieve")
    )


def is_hook_tool(name: object, namespace: object = None) -> bool:
    return isinstance(name, str) and (
        name in {name.replace("set_context", "context_briefing") for name in SET_CONTEXT_NAMES}
        or (name == "context_briefing" and namespace == "mcp__pensieve")
    )


def capture_id(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 255 and not re.search(r"[\s\x00]", value):
        return value
    return None


def prompt(state: dict, content: str, native_turn_id: str | None) -> CaptureEvent:
    state.pop("pending_turn_id", None)
    state["claude_end_turn"] = False
    return {"kind": "user", "content": content, "native_turn_id": native_turn_id}


def normalise(
    record: dict, client: str, session: str, state: dict, capture_turn_id: str | None = None
) -> list[CaptureEvent]:
    """Return only allowed visible events and provenance-checked boundaries.

    Source IDs are created by the caller from committed file position, so two
    identical visible messages remain distinct. We never recurse through JSON
    looking for text; reasoning and embedded instruction records stay local.
    """
    result = []
    if client == "claude" and (
        record.get("sessionId") != session or record.get("isSidechain") is not False
    ):
        return result
    if client == "codex" and record.get("type") == "turn_context":
        payload = record.get("payload", {})
        if isinstance(payload, dict) and isinstance(payload.get("turn_id"), str):
            if state.get("turn_id") != payload["turn_id"]:
                state["nested_calls"] = {}
            state["turn_id"] = payload["turn_id"]
            state["pending_turn_id"] = payload["turn_id"]
        return result
    # Context receipts require accepted hook provenance, never text quoted by
    # a user, assistant, tool, hook error, or nested compaction history.
    for text in accepted_contexts(record, session, session, client):
        marker = parse_marker(text, client, session, "prompt")
        if marker:
            result.append({"kind": "attribution", "marker": marker})
    if result:
        return result
    payload = record.get("payload") if client == "codex" else record.get("message")
    if client == "codex":
        if not isinstance(payload, dict):
            return []
        if record.get("type") == "event_msg":
            if payload.get("type") == "task_started":
                if state.get("turn_id") != payload.get("turn_id"):
                    state["nested_calls"] = {}
                state["turn_id"] = capture_id(payload.get("turn_id"))
                state["pending_turn_id"] = state["turn_id"]
            if payload.get("type") == "thread_rolled_back":
                # The discarded suffix remains captured, but is no longer the
                # ancestor of new work. Do not link to its former head.
                return [{"kind": "unknown_boundary"}]
            completion = {"task_complete": "completed", "turn_aborted": "interrupted"}.get(
                payload.get("type")
            )
            if completion and payload.get("turn_id") == capture_turn_id:
                return [{"kind": "turn_end", "content": "", "completion": completion}]
        if record.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message")
            if isinstance(text, str):
                return [prompt(state, text, state.get("pending_turn_id"))]
        if record.get("type") == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item")
            if (
                payload.get("thread_id") == session
                and payload.get("turn_id") == state.get("turn_id")
                and isinstance(item, dict)
                and item.get("type") == "McpToolCall"
                and item.get("server") == "pensieve"
                and item.get("tool")
                in {
                    "create_page",
                    "edit_page",
                    "move_page",
                    "merge_pages",
                    "delete_page",
                    "save_data",
                }
                and item.get("status") in {"completed", "failed"}
                and capture_id(item.get("id"))
                and any(call.get("aggregate") for call in state.get("calls", {}).values())
                and item["id"] not in state.get("calls", {})
            ):
                # Native nested calls are emitted at completion, not invocation.
                # Retain identity only: concurrent context switches can make the
                # result belong elsewhere. The server's receipt matching decides
                # whether this call belongs to this captured context.
                seen = state.setdefault("nested_calls", {})
                if seen.get(item["id"]) == state.get("turn_id"):
                    return []
                seen[item["id"]] = state.get("turn_id")
                return [
                    {
                        "kind": "tool_call",
                        "content": "mcp__pensieve__" + item["tool"],
                        "tool_call_id": item["id"],
                    }
                ]
            if (
                payload.get("thread_id") == session
                and payload.get("turn_id") == state.get("turn_id")
                and isinstance(item, dict)
                and item.get("type") == "McpToolCall"
                and item.get("server") == "pensieve"
                and item.get("tool") == "set_context"
                and item.get("status") == "completed"
                and not state.get("calls", {}).get(item.get("id"), {}).get("selection")
            ):
                # Nested code-mode calls have native MCP provenance even though
                # the model-visible call is only exec/wait. Never infer a
                # selection from JavaScript source or its combined output.
                output = item.get("result")
                if not isinstance(output, dict) or output.get("isError"):
                    return []
                text = visible_text(output.get("content"))
                marker = parse_marker(text, client, session, "selection")
                if marker:
                    return [
                        {"kind": "attribution", "marker": marker},
                        {"kind": "tool_result", "content": text, "tool_call_id": item.get("id")},
                    ]
                return [{"kind": "unknown_boundary"}]
            if (
                payload.get("thread_id") == session
                and isinstance(item, dict)
                and item.get("type") == "UserMessage"
                and isinstance(payload.get("turn_id"), str)
            ):
                state["turn_id"] = payload["turn_id"]
                text = visible_text(item.get("content"))
                return [prompt(state, text, payload["turn_id"])] if text else []
        if record.get("type") != "response_item":
            return []
        kind = payload.get("type")
        if kind == "message" and payload.get("role") == "assistant":
            if payload.get("channel") not in {None, "commentary", "final"} or payload.get(
                "phase"
            ) not in {None, "commentary", "final_answer", "final"}:
                return []
            text = visible_text(payload.get("content"))
            return [{"kind": "assistant", "content": text}] if text else []
        if kind in {"function_call", "custom_tool_call"}:
            call = payload.get("call_id")
            internal = is_hook_tool(payload.get("name"), payload.get("namespace"))
            if isinstance(call, str):
                state.setdefault("calls", {})[call] = {
                    "selection": is_set_context(payload.get("name"), payload.get("namespace")),
                    "internal": internal,
                    "aggregate": is_code_mode_tool(payload.get("name"), payload.get("namespace")),
                }
            if internal:
                return []
            name = payload.get("name")
            return (
                [
                    tool_call(
                        name, call, payload, "arguments" if kind == "function_call" else "input"
                    )
                ]
                if isinstance(name, str)
                else []
            )
        if kind in {"function_call_output", "custom_tool_call_output"}:
            output = payload.get("output")
            text = visible_text(output)
            call = state.setdefault("calls", {}).pop(payload.get("call_id"), {})
            if call.get("internal"):
                return []
            if call.get("aggregate"):
                # One exec/wait result can combine calls made before and after
                # a selection, including concurrent or yielded work. Its text
                # has no single proven company; retain only an omission notice.
                return [
                    {
                        "kind": "tool_result",
                        "tool_call_id": payload.get("call_id"),
                        "content": "[Combined code-mode tool output omitted; original remains in the host conversation]",
                    }
                ]
            if call.get("selection"):
                marker = parse_marker(text, client, session, "selection")
                if marker:
                    result.append({"kind": "attribution", "marker": marker})
                else:
                    # A selection result with no valid receipt could change
                    # account/context; stop attributing later text to the old one.
                    result.append({"kind": "unknown_boundary"})
            if text:
                result.append(
                    {"kind": "tool_result", "content": text, "tool_call_id": payload.get("call_id")}
                )
            return result
        return []
    if (
        record.get("type") == "system"
        and record.get("subtype") == "stop_hook_summary"
        and record.get("stopReason") == ""
        and record.get("preventedContinuation") is False
        and record.get("hookErrors") == []
        and state.get("claude_end_turn")
    ):
        return [{"kind": "turn_end", "content": "", "completion": "completed"}]
    if (
        record.get("isMeta") is True
        or record.get("isCompactSummary") is True
        or record.get("isVisibleInTranscriptOnly") is True
        or not isinstance(payload, dict)
    ):
        return []
    kind = record.get("type")
    content = payload.get("content")
    if kind == "assistant" and payload.get("role") == "assistant":
        state["claude_end_turn"] = payload.get("stop_reason") == "end_turn"
        text = visible_text(content)
        if text:
            result.append({"kind": "assistant", "content": text})
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                call, name = item.get("id"), item.get("name")
                if isinstance(call, str) and isinstance(name, str):
                    internal = is_hook_tool(name)
                    state.setdefault("calls", {})[call] = {
                        "selection": is_set_context(name),
                        "internal": internal,
                    }
                    if not internal:
                        result.append(tool_call(name, call, item, "input"))
        return result
    if kind != "user" or payload.get("role") != "user":
        return []
    blocks = content if isinstance(content, list) else []
    tool_results = [
        item for item in blocks if isinstance(item, dict) and item.get("type") == "tool_result"
    ]
    if not tool_results:
        text = visible_text(content)
        if (
            "permissionMode" not in record
            and "promptSource" not in record
            and text.startswith(
                ("<command-name>", "<local-command-stdout>", "<local-command-stderr>")
            )
        ):
            # Claude's local /compact and /clear records are synthetic user
            # messages without prompt provenance, not fresh model turns.
            return []
        return [prompt(state, text, record.get("uuid"))] if text else []
    for item in tool_results:
        text = visible_text(item.get("content"))
        call = state.setdefault("calls", {}).pop(item.get("tool_use_id"), {})
        if call.get("internal"):
            continue
        if call.get("selection"):
            marker = (
                None if item.get("is_error") else parse_marker(text, client, session, "selection")
            )
            if marker:
                result.append({"kind": "attribution", "marker": marker})
            elif not item.get("is_error"):
                result.append({"kind": "unknown_boundary"})
        if text:
            result.append(
                {"kind": "tool_result", "content": text, "tool_call_id": item.get("tool_use_id")}
            )
    return result
