"""The reliability orchestrator — composes translation + policy over one turn.

Two phases so the server can commit the right HTTP status:

  prepare(body, ...)      translate + floor + PRE-CALL overflow check. No network.
                          Raises ContextOverflowError (-> 400 `prompt is too long`) or
                          TranslationError (-> 400) BEFORE anything is committed.
  run_upstream(prepared,  the upstream call (injected, so this is unit-tested with a
    call_upstream)        FakeUpstream — no socket, no secret), escalate-on-empty,
                          finish_reason fix, 400 classification from REQUEST STATE, then
                          OpenAI->Anthropic. Returns a settled Anthropic Message.

`call_upstream(openai_payload) -> UpstreamResult(status, body, error_text)` is where the
real streaming HTTP + 429 backoff + concurrency gate live (ambient_code.upstream); the
policy here stays pure.
"""
from __future__ import annotations

from typing import Callable, NamedTuple, Optional

from . import anthropic_request, anthropic_response, bridge_policy
from .errors import BridgeError, TranslationError, UpstreamContentError


class ModelProfile(NamedTuple):
    window: Optional[int]        # real context length, or None if unknown
    floor: int                   # per-model min output tokens
    ceiling: int                 # per-model max output tokens (escalation cap)
    supports_images: bool


class Prepared(NamedTuple):
    openai_payload: dict
    requested_model: str         # what the client asked for (echoed back)
    served_model: str            # the ambient model actually targeted
    profile: ModelProfile
    est_prompt_tokens: int
    has_image: bool


class UpstreamResult(NamedTuple):
    status: int
    body: Optional[dict]
    error_text: Optional[str] = None


class ContextOverflowError(TranslationError):
    """The prompt does not fit the model window. Rendered as the exact Anthropic
    `invalid_request_error` string Claude Code's auto-compaction recognizes."""


class UpstreamError(BridgeError):
    def __init__(self, status, message, anthropic_type="api_error"):
        super().__init__(message)
        self.http_status = status
        self.anthropic_type = anthropic_type


_MARGIN = bridge_policy._ESCALATE_MARGIN
# An opaque 400 (Ambient's 400s carry no machine-readable reason) whose prompt is already this
# fraction of the catalog window is treated as an overflow: the model's REAL window has
# likely shrunk below what the catalog claims, so compacting is the right recovery.
_OVERFLOW_400_FRACTION = 0.75


def prepare(body: dict,
            resolve_model: Callable[[Optional[str]], str],
            profile_of: Callable[[str], ModelProfile]) -> Prepared:
    """Translate + resolve model + PRE-CALL overflow check (no network)."""
    requested_model = body.get("model") if isinstance(body, dict) else None
    served_model = resolve_model(requested_model)
    profile = profile_of(served_model)
    payload = anthropic_request.anthropic_to_openai(body, served_model)  # may raise TranslationError
    est = bridge_policy.estimate_prompt_tokens(payload)
    has_image = bridge_policy.messages_have_image(payload.get("messages"))

    # Overflow is decided from REQUEST STATE (Ambient's 400 body is opaque). Skip the
    # pre-check for image requests (the char estimate is unreliable for them); the
    # post-400 gate in run_upstream still protects them.
    if profile.window is not None and not has_image:
        soft = _soft_window(profile.window, profile.floor)
        demand = est + profile.floor + _MARGIN
        if demand > soft:
            raise _overflow(demand, soft)
    return Prepared(payload, requested_model or served_model, served_model,
                    profile, est, has_image)


def _soft_window(window: int, floor: int) -> int:
    """The real window minus HEADROOM. We trigger compaction against THIS (not the hard
    window), so after Claude Code compacts there is room for several more turns — this
    prevents compact-then-immediately-re-overflow thrashing (the failure mode a
    fire-at-the-limit trigger causes). Never drops below one floored turn."""
    headroom = min(max(2048, window // 8), 16384)
    return max(floor + _MARGIN, window - headroom)


def _overflow(demand: int, window: int) -> ContextOverflowError:
    """Report N > M so Claude Code's compaction sizes a REAL shrink (an N <= M reads as
    'already fits' and it never compacts). M is the SOFT window, so it compacts to fit
    with headroom to spare."""
    n = max(demand, window + _MARGIN)
    err = ContextOverflowError(bridge_policy.synthesize_overflow_body(n, window))
    # The input+output total that this overflow reflects. When it comes from an UPSTREAM
    # 400 (run_upstream), the server learns a window ceiling covering BOTH sides — not the
    # input estimate alone, which would under-shoot the true window and over-compact until
    # TTL. (The pre-call soft-window overflow also carries this, but the server does NOT
    # learn from it — no upstream rejection occurred, so there is no new window signal.)
    err.observed_ceiling = demand
    return err


def run_upstream(prepared: Prepared,
                 call_upstream: Callable[[dict], UpstreamResult],
                 max_escalations: int = bridge_policy.MAX_ESCALATIONS) -> dict:
    """Run the prepared request through the reliability layer; return an Anthropic Message."""
    p = prepared
    window = p.profile.window
    eff_window = window if window is not None else 200_000
    requested = p.openai_payload.get("max_tokens")
    sent_max = bridge_policy.floor_max_tokens(requested, p.profile.floor)
    # If the floored output would overrun the window, clamp it (room for >= floor exists
    # because prepare() already raised on a true overflow).
    if window is not None:
        room = window - p.est_prompt_tokens - _MARGIN
        if sent_max > room:
            sent_max = max(p.profile.floor, room)

    result = call_upstream(_with_max(p.openai_payload, sent_max))

    if result.status == 400:
        raise _classify_400(p, sent_max, result.error_text)
    if result.status != 200 or result.body is None:
        raise UpstreamError(result.status or 502, result.error_text or "upstream failure")

    body = result.body
    escalated = False
    if max_escalations and bridge_policy.should_escalate(body, sent_max):
        nxt = bridge_policy.next_max_tokens(sent_max, p.est_prompt_tokens, eff_window,
                                            hard_cap=p.profile.ceiling)
        if nxt:
            retry = call_upstream(_with_max(p.openai_payload, nxt))
            if retry.status == 200 and retry.body is not None:
                body, sent_max, escalated = retry.body, nxt, True
            elif retry.status == 400:
                raise _classify_400(p, nxt, retry.error_text)

    body = bridge_policy.rewrite_finish_reason(body, sent_max)
    try:
        return anthropic_response.openai_to_anthropic(body, p.requested_model)
    except UpstreamContentError:
        # A malformed tool call. Escalate once (a truncated call may complete), else fail
        # cleanly — never invent parameters. A retry 400 is reclassified; a still-malformed
        # retry is normalized to the clean 502 below (never leaks the raw content error).
        if not escalated and max_escalations:
            nxt = bridge_policy.next_max_tokens(sent_max, p.est_prompt_tokens, eff_window,
                                                hard_cap=p.profile.ceiling)
            if nxt:
                retry = call_upstream(_with_max(p.openai_payload, nxt))
                if retry.status == 400:
                    raise _classify_400(p, nxt, retry.error_text)
                if retry.status == 200 and retry.body is not None:
                    fixed = bridge_policy.rewrite_finish_reason(retry.body, nxt)
                    try:
                        return anthropic_response.openai_to_anthropic(fixed, p.requested_model)
                    except UpstreamContentError:
                        pass  # still malformed -> normalized clean error below
        raise UpstreamError(502, "upstream produced an unusable tool call")


def _classify_400(p: Prepared, sent_max: int, error_text) -> BridgeError:
    # A vision request to a text-only model 400s identically to an overflow — never
    # synthesize overflow for it (compaction can't fix a modality error).
    if p.has_image and not p.profile.supports_images:
        return UpstreamError(400, error_text or "model does not support images",
                             anthropic_type="invalid_request_error")
    window = p.profile.window
    est = p.est_prompt_tokens
    w = window if window is not None else 200_000
    # 1) Numeric overflow decided from REQUEST STATE (catalog window trusted).
    if window is not None and bridge_policy.is_context_overflow(est, sent_max, window):
        return _overflow(est + sent_max, _soft_window(window, p.profile.floor))
    # 2) An UNAMBIGUOUS overflow marker in the body — always overflow.
    if bridge_policy.error_strongly_overflow(error_text):
        return _overflow(est + sent_max, _soft_window(w, p.profile.floor))
    # 3) AMBIGUOUS signals — a weak "exceeds <size word>" hint, or the 75%-window
    #    fraction heuristic (the OPAQUE-400 self-heal for a silently-shrunk window) —
    #    fire ONLY when the body names no specific non-overflow cause (param/modality/
    #    schema), which compaction cannot fix. This keeps a real tool/param 400 from
    #    firing a useless compaction + poisoning the learned window, while still
    #    unbricking a genuinely-opaque overflow.
    if not bridge_policy.error_names_specific_cause(error_text):
        if bridge_policy.error_weakly_overflow(error_text):
            return _overflow(est + sent_max, _soft_window(w, p.profile.floor))
        if (window is not None and not p.has_image
                and est >= int(window * _OVERFLOW_400_FRACTION)):
            return _overflow(est + sent_max, _soft_window(window, p.profile.floor))
    return UpstreamError(400, error_text or "upstream rejected the request",
                         anthropic_type="invalid_request_error")


def _with_max(payload: dict, sent_max: int) -> dict:
    out = dict(payload)
    out["max_tokens"] = sent_max
    return out
