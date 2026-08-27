"""Synthesize the Anthropic Messages SSE event stream.

Ambient's own `/v1/messages` streaming drops the whole response ~50-75% of the time
(a known gateway streaming bug), so we NEVER proxy it. We stream the CLEAN OpenAI
`/v1/chat/completions` path, accumulate a settled body, and synthesize the Anthropic
event grammar here.

Long-turn survival: the server emits `message_start` + a
keepalive `ping` as soon as the upstream produces its first byte — before the body is
complete — so Claude Code's connection never idle-times-out during a long reasoning
turn. Overflow is caught PRE-call (count_tokens), so committing 200 early forfeits no
recoverable 400; a mid-stream upstream failure becomes an SSE `error` event.

Event order (Anthropic): message_start -> (per block: content_block_start,
content_block_delta*, content_block_stop) -> message_delta{stop_reason,usage} ->
message_stop. No OpenAI `[DONE]`.
"""
from __future__ import annotations

import json
from typing import Iterable, List, Tuple

Event = Tuple[str, dict]


def format_sse(event_name: str, data: dict) -> bytes:
    """One SSE frame: `event: <name>\\ndata: <json>\\n\\n` (UTF-8)."""
    return ("event: {0}\ndata: {1}\n\n".format(event_name, json.dumps(data, ensure_ascii=False))
            ).encode("utf-8")


def message_start_event(message_id: str, model: str, input_tokens: int = 0) -> Event:
    """Emittable EARLY (before the body is ready) for long-turn keepalive."""
    return ("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
            },
        },
    })


def ping_event() -> Event:
    return ("ping", {"type": "ping"})


def content_events(message: dict) -> List[Event]:
    """The content blocks + message_delta + message_stop, from a SETTLED Anthropic
    Message (the anthropic_response output). One delta per block carries the whole
    value — protocol-valid, and Claude Code reassembles it identically."""
    events = []  # type: List[Event]
    for index, block in enumerate(message.get("content") or []):
        btype = block.get("type")
        if btype == "text":
            events.append(("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "text", "text": ""}}))
            events.append(("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": block.get("text", "")}}))
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
        elif btype == "tool_use":
            events.append(("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "tool_use", "id": block.get("id"),
                                  "name": block.get("name"), "input": {}}}))
            events.append(("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta",
                          "partial_json": json.dumps(block.get("input") or {}, ensure_ascii=False)}}))
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
        # unknown block types are skipped (forward-compatible)
    usage = message.get("usage") or {}
    events.append(("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message.get("stop_reason"),
                  "stop_sequence": message.get("stop_sequence")},
        "usage": {"output_tokens": usage.get("output_tokens", 0)}}))
    events.append(("message_stop", {"type": "message_stop"}))
    return events


def error_event(error_type: str, message: str) -> Event:
    """A mid-stream failure after 200 is committed (Anthropic streaming `error`)."""
    return ("error", {"type": "error", "error": {"type": error_type, "message": message}})


def settled_message_to_sse(message: dict, input_tokens: int = None) -> bytes:
    """Full buffered stream for a settled Anthropic Message (no separate keepalive):
    message_start -> content -> message_delta -> message_stop, as one byte string."""
    usage = message.get("usage") or {}
    itoks = usage.get("input_tokens", 0) if input_tokens is None else input_tokens
    events = [message_start_event(message.get("id", "msg_bridge"), message.get("model", ""), itoks),
              ping_event()]
    events.extend(content_events(message))
    return b"".join(format_sse(name, data) for name, data in events)


def events_to_sse(events: Iterable[Event]) -> bytes:
    return b"".join(format_sse(name, data) for name, data in events)
