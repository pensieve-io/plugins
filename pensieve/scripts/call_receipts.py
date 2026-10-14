"""Read exact-call receipts only from native completed tool results."""

import re

from context_receipt import json_object, transcript_tail

BOUND_TOOL_PREFIXES = (
    "mcp__plugin_pensieve_pensieve__",
    "mcp__claude_ai_Pensieve__",
    "mcp__pensieve__",
)
CALL_RECEIPT = re.compile(
    r"<!-- pensieve-call token=([a-f0-9]{64}) context_id=([1-9][0-9]{0,9}) -->"
)


def result_receipt(response):
    """Claude preserves MCP content blocks, but not result metadata."""
    if isinstance(response, dict):
        if response.get("isError") is True:
            return None
        response = response.get("content")
    if not isinstance(response, list) or not response:
        return None
    last = response[-1]
    if not isinstance(last, dict) or last.get("type") != "text":
        return None
    text = last.get("text")
    match = CALL_RECEIPT.fullmatch(text) if isinstance(text, str) else None
    return (match[1], int(match[2])) if match else None


def transcript_receipts(path, session):
    """Retry missed observations; preserve distinct companies and latest focus.

    PostToolUse is the primary path. This bounded recovery is not capture's
    exposure audit: capture still scans its full stream and subagents before upload.
    """
    tail = transcript_tail(path)
    calls = set()
    first = {}
    latest = None
    for line in tail[0] if tail else []:
        record = json_object(line)
        if not record or record.get("sessionId") != session:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        blocks = message.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if (
                record.get("type") == "assistant"
                and block.get("type") == "tool_use"
                and str(block.get("name", "")).startswith("mcp__")
            ):
                if isinstance(block.get("id"), str):
                    calls.add(block["id"])
            elif (
                record.get("type") == "user"
                and block.get("type") == "tool_result"
                and isinstance(block.get("tool_use_id"), str)
                and block["tool_use_id"] in calls
                and not block.get("is_error")
            ):
                receipt = result_receipt(block.get("content"))
                if receipt:
                    latest, context = receipt
                    if len(first) < 7:
                        first.setdefault(context, latest)
    tokens = list(first.values())
    if latest and latest not in tokens:
        tokens.append(latest)
    return tokens
