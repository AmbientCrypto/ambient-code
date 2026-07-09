# Contributing

Thanks for helping improve `ambient-code`.

## Ground rules

- The CLI is a **single stdlib-only file**: `bin/ambient`. No third-party runtime
  dependencies — keep it that way (it must run on a clean `python3` ≥ 3.8).
- Cross-platform: it must at least *start* on macOS, Linux, and Windows. Guard
  POSIX-only calls (`fcntl`, `os.getuid`, `os.fchmod`).
- Never log, print, or commit an API key. Route secrets through the OS secret
  store; pass them over stdin, never argv.

## Dev loop

```bash
python3 -m py_compile bin/ambient          # syntax
python3 -m unittest discover -s tests -v   # hermetic tests, no network
ruff check bin/ambient tests/              # style (advisory)
claude plugin validate .                   # plugin manifest
```

Tests are stdlib `unittest`, no network. Add a test for any behavior change to a
pure function (budgeting, chunking, JSON extraction, dedup, config, redaction).

## Releasing

Bump the version in **all three** places (tests + CI enforce all three stay in sync):
`bin/ambient` `__version__`, `.claude-plugin/plugin.json`, `pyproject.toml`, and
add a `CHANGELOG.md` entry.

## Pull requests

- One focused change per PR; explain the *why*.
- Keep the never-crash posture: a single bad input, chunk, or model must never
  abort the whole run.
- CI (GitHub Actions) runs compile + tests on macOS, Linux, and Windows across Python 3.8–3.13, plus a ruff lint gate and a version tri-sync check.
