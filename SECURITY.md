# Security Policy

## Reporting a vulnerability

Please report security issues privately — do **not** open a public issue for a
vulnerability.

- Open a [GitHub security advisory](https://docs.github.com/en/code-security/security-advisories)
  on this repository (preferred), or
- Open a private security advisory on GitHub (github.com/AmbientCrypto/ambient-code → Security → Report a vulnerability).

Please include repro steps and the affected version (`ambient --version`). We aim
to acknowledge within a few days.

## Scope / threat model

This is a thin, local-first CLI over an OpenAI-compatible API. The security
surface we care about:

- **API key handling** — stored in the OS secret store (macOS Keychain / Linux
  libsecret) or a `chmod 600` file; passed to the network over TLS and to the
  `security`/`secret-tool` backends over stdin (never argv); redacted from all
  output. The one documented exception is `ambient agent`, which exports the key
  into the opencode subprocess environment (standard env-credential model).
- **Untrusted model output** — treated as data, never executed. Callers should
  verify findings before acting.
- **The secrets tripwire** — refuses to transmit files that look like they hold
  credentials, and refuses `.env`-named files. It is best-effort, not a
  guarantee; review what you send. It covers the API lanes only, **not**
  `ambient agent`, which reads its working tree directly.
- **The SessionStart hook** (`hooks/session-start.sh`) — does no network I/O. It
  only self-heals the `~/.local/bin/ambient` launcher symlink it owns (repointing
  it after a plugin update) and reminds Claude when delegate mode is on; it never
  touches a file or symlink it did not create, and prints nothing in the normal
  case.

Out of scope: the security of the Ambient network/endpoint itself, and of
opencode (a separate project invoked by `ambient agent`).

## Good practice

- Keep `ambient` updated.
- Never pass secrets, credentials, or personal/health data in prompts.
- Review a diff before piping it to `ambient audit`.
