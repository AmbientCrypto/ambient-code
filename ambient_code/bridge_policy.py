"""Pure reliability decisions for the Ambient bridge — no sockets, no secrets.

Reliability decisions distilled from measured Ambient gateway behavior — the exact
ways the gateway can make an agent look broken. This module
operates on NON-streamed OpenAI `chat.completion` dicts and keys every decision off
per-model numbers the CALLER supplies (floor + real context window), so it stays
correct for any model Ambient serves now or later.

Every function is pure and immutable: none mutate their argument; rewrites return a
new dict. `ambient_code.server` composes these over a real socket.

Signatures are catalog-FREE (take plain ints) so this module has zero dependency on
either an external `catalog` or bin/ambient's `model_profile`; the caller derives
the numbers from `model_profile()` and passes them in.
"""
from __future__ import annotations

import copy
import json
import random
import re
from typing import Optional

# 429 carries no Retry-After header. Synthesize a bounded backoff.
MAX_BACKOFF_S = 30.0
_BACKOFF_BASE_S = 2.0

# Escalate-on-empty doubles the budget but must leave headroom in the real window,
# or the retry itself 400s.
_ESCALATE_MARGIN = 512

# The escalation must give a REAL reasoning budget, not just 2x a small floor.
# (A 512-token budget can be spent entirely on ~2000 chars of reasoning, with an
# empty answer.) Any escalation lands at least here so a reasoning tail can finish AND answer.
_ESCALATION_FLOOR = 2048

# One retry only. Floor(2048)->double(4096) covers a reasoning model's tail (a
# reasoning trace can run ~7400 chars). More than one doubling risks a loop.
MAX_ESCALATIONS = 1

# Per-model output floors.
# A reasoning model handed a tight max_tokens can spend it all on reasoning and
# return an empty 200, so an UNKNOWN model is assumed reasoning and
# gets the higher floor — the safe default. The caller derives the real floor from
# the live model profile (reasoning flag + a measured-override table) and passes it.
REASONING_MIN_OUTPUT_TOKENS = 2048
NON_REASONING_MIN_OUTPUT_TOKENS = 256
DEFAULT_MIN_OUTPUT_TOKENS = REASONING_MIN_OUTPUT_TOKENS  # no capability info -> assume reasoning
SAFE_MAX_OUTPUT_TOKENS = 65_536

# A base64 image part serializes to hundreds of KB but costs the model ~1k tokens.
IMAGE_TOKEN_ESTIMATE = 1024


# ---- response inspection -------------------------------------------------

def _first_choice(resp: dict) -> dict:
    try:
        return resp["choices"][0] or {}
    except (KeyError, IndexError, TypeError):
        return {}


def _message(resp: dict) -> dict:
    return _first_choice(resp).get("message") or {}


def _completion_tokens(resp: dict) -> Optional[int]:
    usage = resp.get("usage") or {}
    ct = usage.get("completion_tokens")
    return ct if isinstance(ct, int) else None


def is_empty_completion(resp: dict) -> bool:
    """True when the message carries neither usable content nor a tool call.

    A tool call legitimately carries content==null, so emptiness is (no content)
    AND (no tool_calls). Whitespace-only content counts as empty.
    """
    msg = _message(resp)
    content = msg.get("content")
    if content:
        if isinstance(content, str):
            if content.strip():
                return False
        else:  # non-string truthy content (e.g. blocks)
            return False
    if msg.get("tool_calls"):  # a non-empty list
        return False
    return True


def is_truncated(resp: dict, sent_max: int) -> bool:
    """Did the model hit the output budget (so a bigger budget could help)?"""
    if _first_choice(resp).get("finish_reason") == "length":
        return True
    ct = _completion_tokens(resp)
    return bool(ct is not None and sent_max and ct >= sent_max)


def should_escalate(resp: dict, sent_max: int) -> bool:
    """Retry with a bigger budget only when the response is empty BECAUSE it was
    truncated. Empty-but-not-truncated (the model stopped on its own) will not
    improve on a retry, and a missing `usage` cannot prove truncation — both fail
    safe to no escalation."""
    return is_empty_completion(resp) and is_truncated(resp, sent_max)


def next_max_tokens(sent_max: int, prompt_tokens: int, context_window: int,
                    hard_cap: int = SAFE_MAX_OUTPUT_TOKENS,
                    margin: int = _ESCALATE_MARGIN) -> Optional[int]:
    """The doubled budget for one escalation, capped to the output cap AND the real
    window minus the prompt. None when there is no room to grow — the caller must
    then treat it as an overflow, not keep retrying."""
    room = context_window - prompt_tokens - margin
    ceiling = min(hard_cap, room)
    nxt = min(max(sent_max * 2, _ESCALATION_FLOOR), ceiling)
    return nxt if nxt > sent_max else None


def rewrite_finish_reason(resp: dict, sent_max: int) -> dict:
    """Narrowly correct a mislabeled 'stop'->'length'. Returns a NEW
    dict; never mutates. Rewrites ONLY when all hold: content empty, no tool calls,
    and completion_tokens >= the budget WE sent. A blanket rewrite would corrupt a
    valid short 'stop'. Preserves usage untouched."""
    if _first_choice(resp).get("finish_reason") != "stop":
        return resp
    if not is_empty_completion(resp):
        return resp
    ct = _completion_tokens(resp)
    if not (ct is not None and sent_max and ct >= sent_max):
        return resp
    fixed = copy.deepcopy(resp)
    fixed["choices"][0]["finish_reason"] = "length"
    return fixed


# ---- 400 overflow classification + synthesis -----------------------------

def is_context_overflow(prompt_tokens: int, max_tokens: int, context_window: int,
                        margin: int = 0) -> bool:
    """Would this request's input+output exceed the model's real window? Decided from
    REQUEST STATE, never the opaque body — a vision-to-text 400 and a param 400 are
    byte-identical to an overflow 400, and synthesizing overflow for either would fire
    a destructive, useless compaction."""
    return prompt_tokens + max_tokens + margin > context_window


def synthesize_overflow_body(prompt_tokens: int, context_window: int) -> str:
    """The error string that fires Claude Code's PRIMARY in-loop auto-compaction.

    Claude Code keys on /prompt is too long/i and extracts 'N tokens > M maximum' to
    size the compaction (verified against Claude Code 2.1.219). N>=M so the compaction
    is not under-sized. This is the canonical Anthropic wire format for `invalid_request_error`.
    """
    return "prompt is too long: {0} tokens > {1} maximum".format(prompt_tokens, context_window)


# ---- request inspection --------------------------------------------------

def messages_have_image(messages) -> bool:
    """Does the request carry an image part? (A text-only model that 400s on an image
    gives the SAME opaque 400 as overflow; the bridge must pass it through, not
    synthesize overflow.)"""
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in (
                        "image_url", "image", "input_image"):
                    return True
    return False


def estimate_prompt_tokens(payload: dict) -> int:
    """Cheap char/~4 estimate of an OPENAI payload's prompt size, for the pre-call
    overflow math. Image parts are charged a flat constant, not their base64 length.
    UNDER-counts code (denser than 4 char/token); the proxy compensates on a real 400
    rather than trusting it as gospel. (A more thorough estimator backs count_tokens.)"""
    total = 0
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("image_url", "image", "input_image"):
                    total += IMAGE_TOKEN_ESTIMATE
                else:
                    total += len(json.dumps(part, ensure_ascii=False)) // 4
        total += 4  # per-message role/format overhead
    total += len(json.dumps(payload.get("tools") or [], ensure_ascii=False)) // 4
    return max(1, total)


_OVERFLOW_ERROR_MARKERS = (
    "too long", "context length", "maximum context", "maximum length",
    "request_too_large", "too many tokens", "token limit", "context window",
    "input is too long", "reduce the length")
# "exceeds"/"exceeded" alone is ambiguous (tool-call caps, rate/quota "exceeded"), so
# it only counts as overflow when a size word sits NEAR it (bounded distance, not
# anywhere in the string) AND — at the call site — the body names no specific cause.
_WEAK_OVERFLOW_RE = re.compile(
    r"exceed\w*[^.]{0,32}(?:token|context|window|prompt|length)"
    r"|(?:token|context|window|prompt|length)[^.]{0,32}exceed",
    re.IGNORECASE)


def error_strongly_overflow(text: Optional[str]) -> bool:
    """UNAMBIGUOUS overflow phrasings — these always mean a context overflow."""
    low = (text or "").lower()
    if any(marker in low for marker in _OVERFLOW_ERROR_MARKERS):
        return True
    # "<n> tokens must be <= <m>" is an overflow phrasing (total/input/request tokens),
    # EXCEPT the OUTPUT-budget param "max_tokens must be <= N" — a real overflow never
    # names the max_tokens param.
    return "tokens must be" in low and "max_tokens" not in low


def error_weakly_overflow(text: Optional[str]) -> bool:
    """An AMBIGUOUS 'exceeds … <size word>' hint (proximity-checked). The caller MUST
    also confirm the body names no specific non-overflow cause before trusting it."""
    return bool(_WEAK_OVERFLOW_RE.search(text or ""))


def error_looks_like_overflow(text: Optional[str]) -> bool:
    """Belt: strong OR weak. (Ambient's opaque 'Upstream request failed' matches
    neither — so this is a belt for other upstreams, not the primary
    signal.) Call sites that must not misfire on tool/param 400s use the split
    strong/weak forms and gate the weak one by error_names_specific_cause."""
    return error_strongly_overflow(text) or error_weakly_overflow(text)


# Words that identify a SPECIFIC, non-overflow 400 cause. When an opaque-400 body
# names one of these, the request-state fraction heuristic must NOT synthesize an
# overflow (compaction can't fix a param/modality/schema error), so the honest 400
# is surfaced instead. Deliberately UNAMBIGUOUS ones only — generic 400 words like
# "invalid", "must be", "required" are EXCLUDED because real overflow bodies contain
# them too ("invalid request: total tokens must be <= 80000"); suppressing on those
# would defeat the self-heal. When a 400 is genuinely ambiguous, the heuristic fires
# (compaction is recoverable; a persistent non-overflow 400 re-surfaces honestly next
# turn once the prompt is smaller). Checked only AFTER the overflow markers miss.
_SPECIFIC_400_MARKERS = (
    "image", "vision", "modality", "unsupported", "not supported", "parameter",
    "schema", "tool", "role", "malformed", "decode", "not allowed",
    # "required" names a concrete missing-field param error; a real overflow that also
    # says "…required" is still caught by the STRONG markers checked first.
    # NOT the param name "max_tokens": it appears in overflow-ish bodies ("max_tokens
    # exceeds the room left by the prompt"), and "max_tokens must be <= N" is already
    # excluded from the strong markers, so it needs no specific-cause entry.
    "forbidden field", "required")


def error_names_specific_cause(text: Optional[str]) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _SPECIFIC_400_MARKERS)


# ---- 429 handling --------------------------------------------------------

def classify_429(body: str) -> str:
    """cold  -> the model has no warm workers; a retry will not warm it, fail over.
       rate_limit -> we sent too fast; a bounded backoff can recover."""
    if "no workers are currently available" in (body or "").lower():
        return "cold"
    return "rate_limit"


def backoff_seconds(attempt: int) -> float:
    """Exponential, bounded, WITH JITTER. No Retry-After exists, so this is a
    synthesized guess. The jitter is essential: without it, a burst of subagents that
    all rate-limit at once retry in lockstep and re-collide. Equal jitter keeps a floor
    of half the backoff and randomizes the rest."""
    ceiling = min(MAX_BACKOFF_S, _BACKOFF_BASE_S * (2 ** max(0, attempt)))
    half = ceiling / 2
    return half + random.uniform(0, half)


# ---- effective budget for the FIRST call ---------------------------------

def floor_max_tokens(requested: Optional[int], floor: int = DEFAULT_MIN_OUTPUT_TOKENS) -> int:
    """Raise a too-small (or absent/junk) max_tokens to the model's floor. The caller
    derives `floor` from the live model profile, so it is correct for every model."""
    if not isinstance(requested, int) or requested < floor:
        return floor
    return requested
