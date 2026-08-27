"""Settled OpenAI chat.completion -> Anthropic Message (non-streaming shape).

Inverse of anthropic_request. Encodes every tool-call id so Claude Code only ever sees
Anthropic-legal `tool_use.id` (the round-trip the codec guarantees). Maps finish_reason
-> stop_reason, forcing `tool_use` whenever tool blocks exist (else Claude Code may not
run them). Drops Ambient `reasoning` (it is unsigned; a fabricated Anthropic `thinking`
block would 400 on replay — its tokens are already in completion_tokens). Validates that
every tool_call.arguments parses to a JSON object; never invents parameters.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from . import tool_ids
from .errors import MalformedToolArgumentsError

_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def openai_to_anthropic(resp: dict, requested_model: str,
                        message_id: Optional[str] = None) -> dict:
    """Translate a settled OpenAI chat.completion into an Anthropic Message."""
    choice = _first_choice(resp)
    message = choice.get("message") or {}

    content_blocks = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})
    elif isinstance(text, list):
        # Some providers already return content parts; keep text parts only.
        for part in text:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                content_blocks.append({"type": "text", "text": part["text"]})

    tool_calls = message.get("tool_calls") or []
    emitted_tools = 0
    for ordinal, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args_str = fn.get("arguments")
        content_blocks.append({
            "type": "tool_use",
            "id": tool_ids.encode_tool_id(tc.get("id") or _synth_call_id(resp, tc, ordinal)),
            "name": fn.get("name"),
            "input": _parse_arguments(args_str, fn.get("name")),
        })
        emitted_tools += 1

    stop_reason = _stop_reason(choice.get("finish_reason"), emitted_tools)
    usage = resp.get("usage") or {}
    return {
        "id": message_id or ("msg_bridge_" + uuid.uuid4().hex),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": _as_int(usage.get("prompt_tokens")),
            "output_tokens": _as_int(usage.get("completion_tokens")),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _first_choice(resp: dict) -> dict:
    try:
        return (resp.get("choices") or [{}])[0] or {}
    except (IndexError, TypeError):
        return {}


def _stop_reason(finish_reason, emitted_tools: int) -> str:
    # A refused/filtered response must NEVER be turned into tool execution, even if it
    # also carried tool_calls — map it to end_turn so Claude Code does not run them.
    if finish_reason == "content_filter":
        return "end_turn"
    # A response with ACTUAL emitted tool_use blocks MUST report tool_use, or Claude
    # Code won't execute the calls (based on emitted blocks, not raw/malformed entries).
    if emitted_tools > 0:
        return "tool_use"
    return _STOP_MAP.get(finish_reason, "end_turn")


def _parse_arguments(args_str, name):
    """Tool-call arguments MUST be a JSON object. On anything else, fail (the caller
    escalates once, then returns an api_error) — never invent parameters."""
    if args_str is None or args_str == "":
        return {}
    if isinstance(args_str, dict):  # some providers already give an object
        return args_str
    if not isinstance(args_str, str):
        raise MalformedToolArgumentsError(
            "tool call {0!r} arguments not a string/object".format(name))
    try:
        # Reject NaN/Infinity: Python's json accepts them, but they are not valid JSON
        # and would break Claude Code when it re-serializes the tool input.
        parsed = json.loads(args_str, parse_constant=_reject_json_constant)
    except (ValueError, TypeError):
        raise MalformedToolArgumentsError(
            "tool call {0!r} arguments are not valid JSON".format(name))
    if not isinstance(parsed, dict):
        raise MalformedToolArgumentsError(
            "tool call {0!r} arguments are not a JSON object".format(name))
    return parsed


def _reject_json_constant(token):
    raise ValueError("non-JSON constant in tool arguments: {0}".format(token))


def _synth_call_id(resp: dict, tc: dict, ordinal: int) -> str:
    """A deterministic UNIQUE id when the upstream omits one, so parallel tool calls
    stay distinct and still round-trip. Prefers the provider `index`, else the loop
    ordinal (never a constant, which would collide across parallel calls)."""
    idx = tc.get("index")
    if not isinstance(idx, int):
        idx = ordinal
    return "bridge:{0}:{1}".format(resp.get("id", "resp"), idx)


def _as_int(v) -> int:
    return v if isinstance(v, int) else 0
