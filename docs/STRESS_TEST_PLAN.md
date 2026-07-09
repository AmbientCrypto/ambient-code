# ambient-code — QA batteries

Two repeatable acceptance harnesses. Run them after any change to `bin/ambient`
before shipping.

## How to run

```bash
# full battery (spends a small amount of Ambient credit; sandbox HOME, real key from
# the OS keychain — the operator's own config is never touched)
bash tools/stress_test.sh

# offline surface + robustness only (no spend, no key needed)
AMB_NO_LIVE=1 bash tools/stress_test.sh

# every-model × every-command matrix (READY models do real work; non-serving models
# must fail with the clean [model] diagnosis; plus command cycles the battery
# doesn't reach: use/mode/curate/cache/link/trust-url, git lanes, plan-only)
bash tools/model_matrix.sh
```

Exit code is non-zero if any check FAILS. Each check prints `PASS`/`FAIL`/`SKIP`,
and both harnesses end with a mechanical **key-leak tripwire** over every byte of
captured output.

## What `stress_test.sh` covers (~40 checks)

**Offline — surface + exit-code contract:** version surfaces, bare-command
banner, unknown command → exit 64 with did-you-mean, empty `ask` → 64,
unconfigured commands → exit 3 with the get-a-key pointer, `codex` explainer,
help completeness, corrupt-config degradation, API host pinning refusal,
key-in-argv refusal, secrets tripwire, binary/empty-file skipping, `--staged`
outside a repo, `link` create/remove, `cache` status, `curate` persist/reset,
headless `build --apply` consent gates.

**Live — correctness (the tool must FIND planted bugs):** single-shot `--json`
audit flags a division-by-zero; `--format report` renders a verdict; a
176k-char input map-reduces into 4 chunks (chunk evidence mandatory) and
returns a structured verdict; a re-run serves chunks from cache; 2-model
consensus; codegen emits valid Python; `build` plans, generates, and applies a
real multi-file package (every generated file parses; nothing executable).

**Live — contracts:** keyless `models` storefront, `models --json` envelope,
bogus key → clean `[key]` diagnosis (never a traceback), `ask --json`
envelope, piped output prints exactly once, TTY streaming (pty capture),
`doctor` healthy. JSON assertions parse the envelope AND check the process
exit code against the envelope's own `exit_code`.

### Stress inputs used
- ~5.4 MB random text (map-reduce + cost-gate path)
- a single 400 KB line (minified hard-split)
- binary blob, empty file (skip-not-crash)
- a planted fake-credential file (tripwire)

## What `model_matrix.sh` covers (~39 checks)

- **Every catalog model** (17 at last refresh): READY models must complete an
  ask, find a planted bug via `audit --json`, and return a working codegen
  envelope; non-serving models must fail with the classified `[model]` diagnosis —
  never a hang, traceback, or `[internal]`.
- **Command cycles:** `use` (persist, unique-substring resolution, ambiguous
  refusal), `mode on/status/off`, `curate only/note/reset`, `cache
  status/clear`, `link` idempotency, `trust-url` non-TTY refusal, `usage
  --json`, `setup` pre-validation + live bogus-key rejection, `audit --staged`
  and `--diff <bad-ref>` in a real git repo, `ask -s`, `code -f`,
  `build --plan-only` (returns a plan, writes nothing), `consensus --json`
  with corroboration annotations.

## Also verified separately (not in the shell batteries)
- **`tests/`** — 112 hermetic unit tests across `test_ambient.py`,
  `test_ambient_v2.py`, `test_ambient_features.py`: per-model profile
  invariants over the real catalog snapshot + fuzz, a fake-SSE harness for the
  stream parser, the complete() degradation ladder, the sacred-model
  invariant, hostile-catalog coercion, spend gates, output sanitization,
  onboarding/key validation, curation semantics, the `safe_relpath` corpus,
  build-mode continuation/resume/apply, and docs-drift guards.
  Run: `python3 -m unittest discover -s tests`.
- **CI** (`.github/workflows/ci.yml`) — compile + import + the unit suite on
  macOS, Linux, and Windows across Python 3.8-3.13, a version tri-sync +
  manifest check, and ruff as a gate. No live API in CI.
- **Adversarial review** on snapshot copies during development — every
  verified finding fixed before release.
