"""Live upstream to Ambient's CLEAN OpenAI `/v1/chat/completions` path.

We STREAM upstream (the OpenAI path streams reliably; only `/v1/messages` streaming is
broken) with `stream_options.include_usage` for correct usage, and ACCUMULATE the SSE
into a settled body. Streaming keeps a long reasoning turn's connection alive (turns can run many
minutes) instead of a single buffered request that a proxy would idle-kill.

`accumulate_stream` is the pure, testable core (no socket). `make_live_upstream` adds
the HTTP + 429 backoff + a concurrency gate; the key is injected here so the downstream
client never sees it.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Callable, Iterable, Iterator, Optional

from . import bridge_policy
from .orchestrator import UpstreamResult

_MAX_429_RETRIES = 3


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# Per-blocking-I/O no-progress timeout (NOT a total deadline): a stream that keeps
# producing tokens across a 12-minute reasoning turn stays alive; a truly stalled socket
# frees after this. A malformed env value falls back to the default, never crashes import.
_UPSTREAM_NOPROGRESS_S = _env_int("AMBIENT_BRIDGE_UPSTREAM_TIMEOUT_S", 300)


# ---- pure accumulation (OpenAI SSE chunks -> settled chat.completion) -----

def accumulate_stream(chunks: Iterable[dict]) -> dict:
    """Fold OpenAI streaming chunk dicts into a settled chat.completion body."""
    content_parts = []
    tool_calls = {}       # openai tool index -> {id,type,function:{name,arguments}}
    tool_order = []       # first-seen order of indices
    finish_reason = None
    usage = None
    resp_id = None
    model = None
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        resp_id = chunk.get("id", resp_id)
        model = chunk.get("model", model)
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            c = delta.get("content")
            if isinstance(c, str):
                content_parts.append(c)
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                slot = tool_calls.get(idx)
                if slot is None:
                    slot = {"id": None, "type": "function", "function": {"name": None, "arguments": ""}}
                    tool_calls[idx] = slot
                    tool_order.append(idx)
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if tc.get("type"):
                    slot["type"] = tc["type"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    slot["function"]["arguments"] += fn["arguments"]
    message = {"role": "assistant"}
    message["content"] = "".join(content_parts) if content_parts else None
    if tool_order:
        message["tool_calls"] = [tool_calls[i] for i in tool_order]
    body = {"id": resp_id or "chatcmpl-bridge",
            "object": "chat.completion",
            "model": model or "",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason or "stop"}]}
    if usage is not None:
        body["usage"] = usage
    return body


def iter_sse_data(readable) -> Iterator[dict]:
    """Yield parsed JSON objects from an OpenAI SSE byte stream; stop at `[DONE]`."""
    for raw in readable:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except (ValueError, TypeError):
            continue


# ---- live HTTP ------------------------------------------------------------

@contextmanager
def _nullslot():
    yield


def _redact(text: Optional[str], key: str) -> str:
    if not text:
        return text or ""
    return text.replace(key, "***") if key else text


def _upstream_payload(payload: dict) -> dict:
    up = dict(payload)
    up["stream"] = True
    up["stream_options"] = {"include_usage": True}
    return up


def make_live_upstream(api_key: str, api_url: str,
                       slot: Callable[[], "object"] = _nullslot,
                       timeout: Optional[int] = None) -> Callable[[dict], UpstreamResult]:
    """Return call_upstream(openai_payload) -> UpstreamResult, streaming + 429 backoff."""
    t = timeout or _UPSTREAM_NOPROGRESS_S
    url = "{0}/v1/chat/completions".format(api_url.rstrip("/"))

    def call(payload: dict) -> UpstreamResult:
        data = json.dumps(_upstream_payload(payload)).encode("utf-8")
        headers = {"Authorization": "Bearer {0}".format(api_key),
                   "Content-Type": "application/json", "Accept": "text/event-stream"}
        for attempt in range(_MAX_429_RETRIES + 1):
            retry_after = None
            with slot():
                req = urllib.request.Request(url, data=data, method="POST", headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=t) as r:
                        return UpstreamResult(200, accumulate_stream(iter_sse_data(r)))
                except urllib.error.HTTPError as e:
                    raw = _redact(e.read().decode("utf-8", "replace"), api_key)
                    if (e.code == 429 and attempt < _MAX_429_RETRIES
                            and bridge_policy.classify_429(raw) != "cold"):
                        retry_after = bridge_policy.backoff_seconds(attempt)
                    else:
                        return UpstreamResult(e.code, None, raw)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                    return UpstreamResult(502, None, _redact(str(e), api_key))
            if retry_after:
                time.sleep(retry_after)
        return UpstreamResult(429, None, "rate limited")

    return call
