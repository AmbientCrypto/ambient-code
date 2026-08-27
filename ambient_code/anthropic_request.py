"""Anthropic Messages request -> Ambient OpenAI chat.completions request.

The correctness core. The subtle part is the block-aware STATE
MACHINE for a user turn: one Anthropic user message may carry several `tool_result`
blocks, each of which becomes its OWN OpenAI `role:"tool"` message, followed by a
single `role:"user"` message for any trailing text/image blocks. Flattening a user
turn into one message loses the `tool_call_id` associations and the model sees tool
output as ordinary prose.

Every tool id is run through `tool_ids.decode_tool_id` UNIFORMLY (assistant
`tool_use.id` and the following `tool_result.tool_use_id`), so the replayed OpenAI
request is internally consistent regardless of the original id shape.

Stream flags are NOT set here — the orchestrator adds `stream`/`stream_options`.
"""
from __future__ import annotations

import json
from typing import List, Optional, Tuple

from . import tool_ids
from .errors import TranslationError, UnsupportedFeatureError

# Anthropic assistant-only blocks we intentionally drop (cannot be replayed to OpenAI;
# signed thinking must be returned unmodified or Anthropic 400s — so never forward it).
_DROP_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
_IMAGE_TYPES = frozenset({"image", "image_url", "input_image"})


def anthropic_to_openai(body: dict, model_id: str) -> dict:
    """Translate a full Anthropic Messages request into an OpenAI request for `model_id`."""
    if not isinstance(body, dict):
        raise TranslationError("request body must be a JSON object")
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise TranslationError("`messages` must be a list")

    out_messages = []  # type: List[dict]
    sys_text = _system_to_openai(body.get("system"))
    if sys_text:
        out_messages.append({"role": "system", "content": sys_text})

    for msg in messages:
        if not isinstance(msg, dict):
            raise TranslationError("each message must be an object")
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant"):
            raise TranslationError("unsupported message role: {0!r}".format(role))
        if isinstance(content, str):
            out_messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise TranslationError("message content must be a string or a list of blocks")
        if role == "assistant":
            out_messages.append(_assistant_message(content))
        else:
            out_messages.extend(_user_messages(content))

    out = {"model": model_id, "messages": out_messages}  # type: dict

    tools = body.get("tools")
    if tools is not None:
        translated = _translate_tools(tools)  # validates non-list; may be []
        if translated:
            out["tools"] = translated

    tool_choice, parallel = _translate_tool_choice(body.get("tool_choice"))
    if tool_choice is not None:
        out["tool_choice"] = tool_choice
    if parallel is not None and out.get("tools"):
        out["parallel_tool_calls"] = parallel

    mt = body.get("max_tokens")
    if isinstance(mt, int):
        out["max_tokens"] = mt
    stops = body.get("stop_sequences")
    if isinstance(stops, list) and stops:
        out["stop"] = stops
    for k in ("temperature", "top_p"):
        if k in body and isinstance(body[k], (int, float)):
            out[k] = body[k]
    return out


# ---- system --------------------------------------------------------------

def _system_to_openai(system) -> Optional[str]:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n\n".join(parts) if parts else None
    raise TranslationError("`system` must be a string or a list of text blocks")


# ---- assistant turn ------------------------------------------------------

def _assistant_message(blocks: list) -> dict:
    text_parts = []
    tool_calls = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if isinstance(t, str) and t:
                text_parts.append(t)
        elif btype == "tool_use":
            tool_calls.append({
                "id": tool_ids.decode_tool_id(block.get("id")),
                "type": "function",
                "function": {
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })
        elif btype in _DROP_BLOCK_TYPES:
            continue  # signed thinking is never replayed to OpenAI
        # unknown assistant block types are ignored (forward-compatible)
    msg = {"role": "assistant"}  # type: dict
    if text_parts:
        msg["content"] = "\n\n".join(text_parts)
    elif tool_calls:
        msg["content"] = None       # valid OpenAI: null content WITH tool_calls
    else:
        msg["content"] = ""         # never null-without-tool_calls (OpenAI-invalid)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# ---- user turn (the state machine) ---------------------------------------

def _user_messages(blocks: list) -> List[dict]:
    """One user turn -> zero or more OpenAI `tool` messages (one per tool_result,
    results FIRST) followed by at most one `user` message for trailing text/images."""
    tool_msgs = []  # type: List[dict]
    user_parts = []  # type: List[dict]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            tool_msgs.append({
                "role": "tool",
                "tool_call_id": tool_ids.decode_tool_id(block.get("tool_use_id")),
                "content": _tool_result_text(block),
            })
            # A tool that returns an image (screenshot / MCP): the OpenAI tool-role is
            # text-only, so forward the image into the trailing user message instead of
            # dropping it, so an image-capable model still sees it.
            user_parts.extend(_tool_result_images(block))
        elif btype == "text":
            t = block.get("text")
            if isinstance(t, str):
                user_parts.append({"type": "text", "text": t})
        elif btype in _IMAGE_TYPES:
            user_parts.append(_image_to_openai(block))
        # unknown user block types are ignored (forward-compatible)
    out = list(tool_msgs)
    if user_parts:
        out.append({"role": "user", "content": _pack_parts(user_parts)})
    return out


def _tool_result_text(block: dict) -> str:
    """Flatten an Anthropic tool_result's content to an OpenAI tool-message string.
    (OpenAI tool-role content is text; tool-result images can't be conveyed to the
    text tool-role, so they are noted, not silently dropped.)"""
    content = block.get("content")
    prefix = "[tool error] " if block.get("is_error") else ""
    if isinstance(content, str):
        return prefix + content
    if isinstance(content, list):
        # Text parts here; images are forwarded separately (see _tool_result_images).
        parts = [b["text"] for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        return prefix + "\n".join(parts)
    if content is None:
        return prefix
    return prefix + json.dumps(content, ensure_ascii=False)


def _tool_result_images(block: dict) -> List[dict]:
    """Image parts inside a tool_result, as OpenAI image_url parts (forwarded to the
    trailing user message so image-capable models still receive them)."""
    content = block.get("content")
    if not isinstance(content, list):
        return []
    return [_image_to_openai(b) for b in content
            if isinstance(b, dict) and b.get("type") in _IMAGE_TYPES]


def _pack_parts(parts: List[dict]):
    """All-text -> a plain string (cheapest); any image -> an OpenAI content-parts list."""
    if all(p.get("type") == "text" for p in parts):
        return "\n\n".join(p.get("text", "") for p in parts)
    return parts


def _image_to_openai(block: dict) -> dict:
    src = block.get("source") or {}
    if isinstance(src, dict) and src.get("type") == "base64":
        url = "data:{0};base64,{1}".format(src.get("media_type", "image/png"), src.get("data", ""))
    elif isinstance(src, dict) and src.get("type") == "url":
        url = src.get("url", "")
    else:
        url = ""
    return {"type": "image_url", "image_url": {"url": url}}


# ---- tools + tool_choice -------------------------------------------------

def _translate_tools(tools) -> List[dict]:
    if not isinstance(tools, list):
        raise TranslationError("`tools` must be a list")
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise TranslationError("each tool must be an object")
        name = tool.get("name")
        schema = tool.get("input_schema")
        if isinstance(schema, dict) and isinstance(name, str):
            out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": schema,
                },
            })
        else:
            # An Anthropic server-side tool (web_search, computer-use, bash, text_editor,
            # ...): declared by `type` with no `input_schema`. Ambient/OpenAI cannot run
            # it — reject cleanly, never accept-then-ignore.
            raise UnsupportedFeatureError(
                "unsupported tool (Ambient cannot run Anthropic server tools): "
                "{0!r}".format(tool.get("type") or name))
    return out


def _translate_tool_choice(tc) -> Tuple[Optional[object], Optional[bool]]:
    if tc is None:
        return None, None
    if not isinstance(tc, dict):
        raise TranslationError("`tool_choice` must be an object")
    parallel = None
    if tc.get("disable_parallel_tool_use") is True:
        parallel = False
    ttype = tc.get("type")
    if ttype == "auto":
        return "auto", parallel
    if ttype == "any":
        return "required", parallel
    if ttype == "none":
        return "none", parallel
    if ttype == "tool":
        name = tc.get("name")
        if not isinstance(name, str):
            raise TranslationError("tool_choice type 'tool' requires a name")
        return {"type": "function", "function": {"name": name}}, parallel
    raise TranslationError("unsupported tool_choice type: {0!r}".format(ttype))
