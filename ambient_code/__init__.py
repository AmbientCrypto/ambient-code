"""ambient_code — the Anthropic→Ambient reliability bridge package.

Bundled with the ambient-code plugin and imported by `bin/ambient` (via a
realpath sys.path shim) for the `ambient serve` / `ambient claude` subcommands.
Stdlib-only, Python 3.8+, so the plugin keeps its zero-dependency property.

Modules:
  bridge_policy  pure reliability decisions (floors, escalate, overflow, 429)
  anthropic      Anthropic<->OpenAI request/response/SSE translation + tool-id codec (later phases)
  server         loopback HTTP lifecycle, pacing, local-token auth (later phases)
  catalog_map    live catalog + Anthropic model mapping (later phases)
"""
from __future__ import annotations

__all__ = ["bridge_policy"]
