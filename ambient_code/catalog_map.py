"""Model resolution, per-model profiles, and local token counting — all from the LIVE
Ambient catalog, so any model added tomorrow works with no code change.

Pure functions over a catalog (a list of dicts from Ambient's /v1/models). `bin/ambient`
injects the live catalog + default model; nothing here imports the CLI.
"""
from __future__ import annotations

import json
import math
from typing import List, Optional

from .bridge_policy import (NON_REASONING_MIN_OUTPUT_TOKENS, REASONING_MIN_OUTPUT_TOKENS,
                            SAFE_MAX_OUTPUT_TOKENS)
from .orchestrator import ModelProfile

# Measured per-model output floors: Kimi thinks less under a tight budget and
# still emits; GLM needs the full reasoning floor. Overrides the reasoning-flag default.
MEASURED_MIN_OUTPUT_TOKENS = {
    "moonshotai/kimi-k2.7-code": NON_REASONING_MIN_OUTPUT_TOKENS,
}
# Families known to accept images (e.g. Kimi). Used only when
# the catalog entry does not declare modalities.
_IMAGE_FAMILIES = ("moonshotai/",)

_CLAUDE_ALIASES = ("claude", "sonnet", "opus", "haiku")


def catalog_ids(catalog: List[dict]) -> set:
    return {e.get("id") for e in catalog if isinstance(e, dict) and e.get("id")}


def resolve_model(requested: Optional[str], catalog: List[dict], default_model: str,
                  user_map: Optional[dict] = None) -> str:
    """Deterministic resolution: exact live id -> itself; explicit user map -> mapped;
    a Claude family name (or nothing) -> the user's default; anything else -> the default.
    Then COLD/GONE substitution: if the chosen model has vanished from the live catalog or
    isn't serving, swap to a warm one (Ambient is an inference provider — models come and
    go / spin up on demand), so a session never dies on a vanished default."""
    ids = catalog_ids(catalog)
    chosen = default_model
    if isinstance(requested, str):
        if requested in ids:
            chosen = requested
        elif user_map and requested in user_map and user_map[requested] in ids:
            chosen = user_map[requested]
    return ready_substitute(chosen, catalog, default_model) or chosen


def _is_ready(entry: dict) -> bool:
    v = entry.get("is_ready")
    if v is None:
        return True  # no readiness info -> don't over-substitute
    if isinstance(v, str):
        # A JSON string "false"/"0"/"no" is truthy to bool() — decode it so a
        # stringly-typed catalog can't mark a cold model as warm (and vice-versa).
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


def ready_substitute(model_id: str, catalog: List[dict],
                     default_model: str) -> Optional[str]:
    """A serving substitute for a vanished/cold model (prefer same vendor, then the user's
    default, then any warm model), or None to serve `model_id` as-is (it's warm, its
    readiness is unknown, or nothing is warm — let it spin up / 429 honestly)."""
    entry = next((e for e in catalog if isinstance(e, dict) and e.get("id") == model_id), None)
    if entry is not None and _is_ready(entry):
        return None
    warm = [e["id"] for e in catalog
            if isinstance(e, dict) and e.get("id") and _is_ready(e)]
    if not warm or model_id in warm:
        return None
    vendor = (model_id or "").split("/", 1)[0]
    same = [w for w in warm if w.split("/", 1)[0] == vendor]
    if same:
        return same[0]
    if default_model in warm:
        return default_model
    return warm[0]


def _is_reasoning(entry: dict) -> bool:
    feats = entry.get("supported_features") or entry.get("features") or []
    if isinstance(feats, list) and feats:
        return "reasoning" in feats
    return True  # no capability info -> assume reasoning (the safe, higher floor)


def _supports_images(entry: dict) -> bool:
    # An EXPLICIT modality declaration wins (a text-only model must not be overridden by
    # the family heuristic). Otherwise a vision feature flag; otherwise the family
    # heuristic (the catalog's `supported_features` is a reasoning/feature list, not a
    # modality list, and some models accept images without declaring the modality).
    mods = entry.get("input_modalities") or entry.get("modalities")
    if isinstance(mods, list):
        return any("image" in str(m).lower() for m in mods)
    feats = entry.get("supported_features") or entry.get("features") or []
    if isinstance(feats, list) and any(isinstance(f, str) and ("vision" in f or "image" in f)
                                       for f in feats):
        return True
    mid = entry.get("id", "")
    return any(mid.startswith(fam) for fam in _IMAGE_FAMILIES)


def _pos_int(v) -> Optional[int]:
    return v if isinstance(v, int) and v > 0 else None


def profile_from_entry(entry: dict) -> ModelProfile:
    window = _pos_int(entry.get("context_length")) or _pos_int(entry.get("context_window"))
    ceiling = _pos_int(entry.get("max_output_length")) or _pos_int(entry.get("max_output_tokens")) \
        or SAFE_MAX_OUTPUT_TOKENS
    mid = entry.get("id", "")
    if mid in MEASURED_MIN_OUTPUT_TOKENS:
        floor = MEASURED_MIN_OUTPUT_TOKENS[mid]
    else:
        floor = REASONING_MIN_OUTPUT_TOKENS if _is_reasoning(entry) else NON_REASONING_MIN_OUTPUT_TOKENS
    floor = min(floor, ceiling)  # never floor above the model's own output cap
    return ModelProfile(window=window, floor=floor, ceiling=ceiling,
                        supports_images=_supports_images(entry))


def profile_for(model_id: str, catalog: List[dict]) -> ModelProfile:
    for entry in catalog:
        if isinstance(entry, dict) and entry.get("id") == model_id:
            return profile_from_entry(entry)
    # Unknown/offline model: conservative reasoning profile, window unknown.
    return ModelProfile(window=None, floor=REASONING_MIN_OUTPUT_TOKENS,
                        ceiling=SAFE_MAX_OUTPUT_TOKENS, supports_images=False)


# ---- local count_tokens (no network) -------------------------------------

_IMAGE_TOKENS = 1024
_PER_MESSAGE = 4
_PER_TOOL = 8


def _text_tokens(text: str) -> int:
    # ceil(UTF-8 bytes / 3): counts BYTES not code points, so CJK/emoji (3-4 bytes each)
    # are not undercounted. Denser than prose's ~4 char/token, so /3 is a safe over-estimate
    # for an agent transcript (over-counting compacts a little early; the synthesized-overflow
    # path is the backstop). Never clamp to the window.
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return int(math.ceil(len(text.encode("utf-8")) / 3.0))


def count_input_tokens(body: dict) -> int:
    """Anthropic count_tokens: system + messages + tools, images at a flat cost."""
    total = 0
    system = body.get("system")
    if isinstance(system, str):
        total += _text_tokens(system)
    elif isinstance(system, list):
        for b in system:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                total += _text_tokens(b["text"])
    for msg in body.get("messages") or []:
        total += _PER_MESSAGE
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            total += _text_tokens(content)
        elif isinstance(content, list):
            for block in content:
                total += _count_block(block)
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            total += _PER_TOOL + _text_tokens(json.dumps(tool, ensure_ascii=False))
    return max(1, total)


def _count_block(block) -> int:
    if not isinstance(block, dict):
        return 0
    btype = block.get("type")
    if btype == "text" and isinstance(block.get("text"), str):
        return _text_tokens(block["text"])
    if btype in ("image", "image_url", "input_image"):
        return _IMAGE_TOKENS
    if btype == "tool_use":
        return _text_tokens(json.dumps(block.get("input") or {}, ensure_ascii=False)) + 4
    if btype == "tool_result":
        c = block.get("content")
        if isinstance(c, str):
            return _text_tokens(c)
        if isinstance(c, list):
            return sum(_count_block(b) for b in c)
    return _text_tokens(json.dumps(block, ensure_ascii=False))
