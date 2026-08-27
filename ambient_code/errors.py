"""Shared bridge error types.

Each carries an Anthropic error `type` so the server can render a faithful
`{"type":"error","error":{"type":...,"message":...}}` envelope with the right HTTP
status, instead of a generic 500.
"""
from __future__ import annotations


class BridgeError(Exception):
    """Base for all bridge errors. `anthropic_type` + `http_status` drive the wire error."""
    anthropic_type = "api_error"
    http_status = 500


class TranslationError(BridgeError):
    """A request we cannot faithfully translate (bad shape, unsupported feature)."""
    anthropic_type = "invalid_request_error"
    http_status = 400


class UnsupportedFeatureError(TranslationError):
    """A well-formed request using a feature Ambient/OpenAI cannot fulfill
    (e.g. an Anthropic server-side tool). Rejected cleanly, never accepted-then-ignored."""


class UpstreamContentError(BridgeError):
    """The upstream returned a body we cannot faithfully render (e.g. a tool call whose
    `arguments` is not a JSON object). The orchestrator may escalate once; if it still
    fails, this becomes a redacted api_error — we NEVER invent tool parameters."""
    anthropic_type = "api_error"
    http_status = 502


class MalformedToolArgumentsError(UpstreamContentError):
    """A tool_call.arguments string that does not parse to a JSON object."""
