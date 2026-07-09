# Changelog

All notable changes to ambient-code. Format loosely follows [Keep a Changelog](https://keepachangelog.com).

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
