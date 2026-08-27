"""Reversible, stateless tool-call id codec — the fix that stops the brick.

Ambient/OpenAI models emit tool-call ids that are illegal as Anthropic
`tool_use.id`: `functions.Read:0` / `functions.save_note:0` (Kimi/DeepSeek) contain
`.` and `:`; Anthropic requires `^[A-Za-z0-9_-]+$`, so replaying such an id in the
next turn's `tool_result` 400s and BRICKS the session.

We translate to Ambient's OpenAI path, so WE own the id Claude Code sees. This codec
makes every id Anthropic-legal and **reversible**, statelessly (no session map — the
bridge is stateless, Claude Code can replay/resume history, and a restarted bridge
must still decode an id it emitted earlier):

  encode(openai_id) -> anthropic-legal id
  decode(anthropic_id) -> the exact original openai_id

Correctness comes from applying `decode` UNIFORMLY to both the assistant `tool_use.id`
and the following `tool_result.tool_use_id`, so the replayed OpenAI request is
internally consistent regardless of the original id shape. `decode(encode(x)) == x`
for every encodable non-empty x. Already-legal ids pass through unchanged
(length-optimal). Everything else is `amb1_` + base64url(MAGIC + utf8(id)); the MAGIC
marker gives provenance (a reserved-prefix id that does NOT carry MAGIC is not ours →
fail CLOSED, never a wrong guess, never hashed/truncated).

Not defended: a byte-mutation of one of OUR ids that still carries MAGIC and stays
canonical would decode to a different original. That does not occur here — the only
transport is Claude Code, which echoes `tool_use.id` VERBATIM in the next
`tool_result`; it never mutates ids. A MAC would close even that at a length cost we
don't need.
"""
from __future__ import annotations

import base64
import re

from .errors import TranslationError

PREFIX = "amb1_"
# Provenance marker embedded in every encoded payload (before the raw id bytes).
_MAGIC = b"\x00AMB1\x00"
# Anthropic's documented tool_use.id constraint. NOTE:
# use fullmatch, NOT a `$`-anchored search — `$` also matches before a trailing
# newline, which would let `foo\n` masquerade as legal and get passed through illegal.
_VALID = re.compile(r"[A-Za-z0-9_-]+")
_B64URL = re.compile(r"[A-Za-z0-9_-]*")
# Reject absurd inputs: real ids are < ~50 chars; this bounds allocation without
# rejecting any legitimate id (a DoS guard, fail-closed).
MAX_RAW_ID_LEN = 512


class ToolIdError(TranslationError):
    """An un-encodable id, or a reserved-prefix (`amb1_`) id that is not a canonical
    MAGIC-carrying encoding of ours. A `TranslationError` so the server renders it as a
    clean Anthropic `invalid_request_error` 400 identifying the bad id — never guessed.
    (The common case is a corrupt client-sent id on the request path.)"""


def is_anthropic_valid(tool_id: str) -> bool:
    """True if `tool_id` is already a legal Anthropic tool_use.id (fullmatch)."""
    return isinstance(tool_id, str) and _VALID.fullmatch(tool_id) is not None


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def encode_tool_id(openai_id: str) -> str:
    """OpenAI/Ambient tool-call id -> an Anthropic-legal, reversible id.

    Already-legal ids pass through unchanged (shortest, reversible) UNLESS they collide
    with our reserved prefix, in which case they are encoded so `decode` is unambiguous.
    Anything else is `amb1_` + base64url(MAGIC + utf8(id)), padding stripped. Fails
    CLOSED on a non-string, empty, over-long, or non-UTF-8-encodable id.
    """
    if not isinstance(openai_id, str) or openai_id == "":
        raise ToolIdError("tool call id must be a non-empty string")
    if len(openai_id) > MAX_RAW_ID_LEN:
        raise ToolIdError("tool call id too long ({0} chars)".format(len(openai_id)))
    if is_anthropic_valid(openai_id) and not openai_id.startswith(PREFIX):
        return openai_id
    try:
        raw = openai_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolIdError("un-encodable tool call id: {0}".format(exc))
    return PREFIX + _b64url_nopad(_MAGIC + raw)


def decode_tool_id(anthropic_id: str) -> str:
    """Inverse of `encode_tool_id`.

    A non-prefixed id was passed through on encode (or comes from an older/native
    transcript) and is returned unchanged. A `amb1_` id is strictly base64url-decoded,
    its MAGIC marker verified, UTF-8 decoded, then re-encoded — requiring byte-for-byte
    equality with the input. A reserved-prefix id lacking MAGIC, non-canonical, or
    undecodable fails CLOSED (ToolIdError), never a wrong guess.
    """
    if not isinstance(anthropic_id, str) or anthropic_id == "":
        raise ToolIdError("tool_use id must be a non-empty string")
    if not anthropic_id.startswith(PREFIX):
        return anthropic_id
    payload = anthropic_id[len(PREFIX):]
    if _B64URL.fullmatch(payload) is None:
        raise ToolIdError("malformed reserved-prefix tool id: {0!r}".format(anthropic_id))
    padded = payload + "=" * (-len(payload) % 4)
    try:
        blob = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise ToolIdError("undecodable reserved-prefix tool id {0!r}: {1}".format(anthropic_id, exc))
    if not blob.startswith(_MAGIC):
        raise ToolIdError("reserved-prefix tool id not ours (no marker): {0!r}".format(anthropic_id))
    try:
        original = blob[len(_MAGIC):].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolIdError("undecodable reserved-prefix tool id {0!r}: {1}".format(anthropic_id, exc))
    # canonical check: our encoder must reproduce EXACTLY this id, else reject.
    if encode_tool_id(original) != anthropic_id:
        raise ToolIdError("non-canonical reserved-prefix tool id: {0!r}".format(anthropic_id))
    return original
