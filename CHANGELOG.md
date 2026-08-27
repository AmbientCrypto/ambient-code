# Changelog

All notable changes to ambient-code. Format loosely follows [Keep a Changelog](https://keepachangelog.com).

## 1.1.0 — 2026-08-26

**Run Claude Code itself on Ambient** — a local, loopback-only reliability bridge.

`ambient claude` starts a bridge and launches Claude Code pointed at it (via
`ANTHROPIC_BASE_URL`), so a whole Claude Code session — subagents included — runs on
Ambient's open models. The bridge speaks the Anthropic Messages API to Claude Code and
translates every turn to Ambient's clean OpenAI path, routing around the known-broken
server-side `/v1/messages` gateway:

- **Reversible tool-call id sanitization** — Ambient/DeepSeek/Kimi ids like
  `functions.Read:0` are illegal as an Anthropic `tool_use.id`; the bridge encodes them so
  Claude Code only sees legal ids that round-trip. Fixes the session brick on the turn
  after the first tool call.
- **Streaming rebuilt** — streams the clean OpenAI path and synthesizes Anthropic SSE with
  early `message_start` + keepalive pings, so a long reasoning turn never idle-times-out.
- **Opaque overflow → real `prompt is too long`** so Claude Code auto-compacts and
  continues instead of 400-looping.
- **Per-model output floors + escalate-on-empty** (GLM/Kimi), **429 pacing + backoff**, and
  **cold-model substitution**.
- **Security** — the real Ambient key never reaches Claude Code; only a random local token
  authenticates to the loopback bridge, which injects the key upstream itself.

New commands: `ambient serve` (the bridge) and `ambient claude` (start Claude Code on it).
Verified live against `api.ambient.xyz` on Kimi, GLM-5.2, and DeepSeek-V4-flash.

Also: a plan-mode guardrail (defer mutating `ambient` actions), a bounded self-heal flow,
a `doctor` bridge health line, and a bundled stdlib-only `ambient_code/` package.

## 1.0.0 — 2026-07-09

Initial public release under the AmbientCrypto org.

`ambient-code` connects Claude Code to the [Ambient](https://ambient.xyz)
decentralized inference network over an OpenAI-compatible API:

- A `/ambient` control panel with first-run onboarding and a sticky, user-curated
  model picker.
- Second-opinion code audits that Claude cross-checks.
- Native `ambient build` file generation (writes only inside its target directory;
  never executes model output).
- Delegate mode — Ambient writes the token-heavy code while Claude plans and
  reviews — and an Ambient-powered agentic terminal.

Security and privacy posture: the API key is held in the OS secret store (or a
`chmod 600` file) and never printed; API requests refuse HTTP redirects so the key
cannot leave the pinned host; model output is treated as untrusted data. Ambient is
a decentralized network — prompts and code you send are processed by independent
operators; see [PRIVACY.md](PRIVACY.md).

Install: `/plugin marketplace add AmbientCrypto/ambient-code` then
`/plugin install ambient-code@ambient`.
