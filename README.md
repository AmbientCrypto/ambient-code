# ambient-code — Claude Code plugin for the Ambient network

> **Beta — early access.** `ambient-code` is open-source (MIT) and under active
> development: please
> [report issues](https://github.com/AmbientCrypto/ambient-code/issues). It
> connects Claude Code to [Ambient](https://ambient.xyz)'s public API.

Give Claude Code a second brain. This plugin connects Claude to
[Ambient](https://ambient.xyz), the decentralized AI inference network which serves
open-source models (Kimi, GLM, GPT-OSS, Qwen, Gemma…) on demand behind one
OpenAI-compatible API, paid per token.

**You stay in control the whole time**: you pick the model, you decide when Ambient
is used, and Claude reviews everything Ambient produces before it lands.

## 30-second start

```bash
# in Claude Code
/plugin marketplace add AmbientCrypto/ambient-code
/plugin install ambient-code@ambient
/ambient            # (completes to /ambient-code:ambient) — Claude walks you through the rest
```

The first time you use it, the CLI onboards you: it asks for an Ambient API key,
shows exactly how to get one (sign in at <https://app.ambient.xyz> → API Keys),
verifies the key with a real authenticated call, tells you *why* if it fails
(mistyped/revoked key vs out-of-funds vs model availability vs network — with an
Ambient support pointer), and finishes with a command showcase. Key entry happens in
**your** terminal with hidden input — the key never passes through the chat.

## The three moves

**1. Pre-commit second opinion, cheap enough to run on every commit**

```bash
git diff | ambient audit            # independent adversarial review of your changes
ambient audit src/auth.py --focus security --json
ambient audit --repo .              # the WHOLE repo — plan + cost shown before anything is sent
```

Independent eyes on every commit at a price where "always" is affordable — Claude
cross-checks the findings, so you get two reviewers for less than one.

**2. Plan with Claude, build with Kimi, review with Claude**

```bash
/ambient on                         # delegate mode
# You describe the feature. Claude writes a precise brief, then runs:
ambient build "FastAPI /users CRUD + pytest suite per the brief" --dir src/ --apply
# Claude reviews the manifest and every file, runs the tests, integrates.
```

Claude's judgment on every line, Kimi doing the token-heavy writing. `ambient build`
plans a file-set, generates it,
and writes only inside `--dir` after your confirmation. It **never executes
anything**.

Big builds are **truncation-safe**. `ambient build` streams one JSON record per
file, so a cut-off response never loses finished work — for instance when a
reasoning model spends its output budget before the batch is done. Files that came
through complete are kept; anything unfinished (and, to be safe, the one file
bordering the cut) is simply re-requested in smaller batches on the next pass. You
keep the model you chose — a large set just takes more passes. The only case that
still wants a bigger-output model or a raised budget is a *single* file too big to
emit in one response, which is reported after two attempts rather than dropped.

**3. Digest cheap, reason expensive**

```bash
cat src/**/*.py | ambient ask "Digest: per file — purpose, public API, gotchas." - > digest.md
```

Claude reads the thousand-token digest instead of the hundred-thousand-token repo,
and reasons over it at full quality.

## Models

Ambient scales models up and down with demand — `ambient models` shows what's
serving (READY) right now. A model that isn't serving at the moment is normal,
not broken: it spins up when demand arrives, and the CLI names serving
alternatives whenever you pick one that isn't serving yet. The default for every lane is **`moonshotai/kimi-k2.7-code`**. Any other model is fully selectable
(`ambient use z-ai/glm-5.2`, etc.). You
can curate which models your menus show: `ambient curate only <ids>` /
`hide <glob>` / `note <id> "label"` — explicit `-m` always works regardless.

**How the lanes work:** two sticky defaults, one per lane — **chat/audit**
(`ask`, `audit`, `chat`, `map`) and **code** (`code`, `build`, `agent`).
`ambient use <id>` sets both; `--chat` / `--code` scopes just one.

## Command reference

```text
ambient setup            store + verify your API key (--force rotate · --remove offboard)
ambient models           current model list (READY = serving now) · --all shows curated-out
ambient use [id]         sticky default model picker (--chat/--code scopes)
ambient curate …         choose which models the menus surface (hide/show/only/note)
ambient ask "q"          one-shot answer · pipe docs: cat doc.txt | ambient ask "sum" -
                         · --consensus A,B triangulates across models · --best-of K
ambient audit [files]    adversarial code review · git diff | ambient audit · --consensus A,B
                         · --repo [DIR] audits a whole repository (git-aware walker)
                         · --best-of K corroborates across K samples
                         · --install-hook installs a pre-commit/pre-push audit gate
ambient map "p" [files]  bulk lane: ONE prompt run independently over MANY items
                         (files, or one item per stdin line; --jsonl objects)
ambient chat             interactive REPL — streamed replies, per-turn token receipt,
                         /model /clear /help /exit
ambient code "task"      single-file code generation (-f context.py) · --best-of K
ambient build "task"     plan + generate a whole file-set (manifest-first, --apply writes)
ambient agent            interactive agentic terminal on Ambient (opencode)
ambient doctor           pinpoints key / funds / model-availability / network trouble
ambient usage            per-model calls and tokens (--days N)
ambient settings         every setting, its value, and how to change it
ambient mode on|off      delegate mode for Claude Code sessions
ambient link             put a stable `ambient` launcher on your PATH
ambient cache …          inspect / clear the local chunk cache
ambient trust-url <url>  explicitly trust a self-hosted gateway (advanced)
```

Every model is auto-tuned from the live catalog (context window, output cap,
reasoning behavior) — **no input is refused for size**: anything past a model's
single-pass budget is automatically split, processed in parallel by the *same*
model, and merged, with failed pieces reported as explicit coverage gaps. The
fan-out width defaults to 3 concurrent calls — raise or lower it per run with
`--parallel N` or persistently with `AMBIENT_MAX_PARALLEL` (clamped to 1-16); it
also sets how many `--consensus` models run at once. Spend is
gated: set `AMBIENT_MAX_SPEND` to cap it, and large jobs ask first (`--yes`
skips, `--allow-cost` overrides). The
ceiling is enforced **across every concurrently-running ambient process** — a
10-wide fan-out shares one budget, not ten: each gated call reserves its
estimate in `~/.config/ambient/reservations.jsonl` (released on exit; stale
entries from crashed processes are pruned by pid-liveness — a provably-alive
holder is never expired — and, where liveness is unknowable (Windows), by
`AMBIENT_RESERVATION_TTL`, default 3600s, so nothing ever wedges the budget).
Set `AMBIENT_FLEET_BUDGET=off` to restore per-invocation-only gating; the
mechanism itself is fail-open and never blocks a call on its own failure.

**Run receipts** — every run ends with the model and the tokens it used:

```
[ambient moonshotai/kimi-k2.7-code | in=48210 out=1834 tokens]
```

**Savings receipts are opt-in and off by default.** A `~N% cheaper than a frontier
model` line measures real spend against a list price the tool picked on your behalf,
so nothing surfaces it unless you ask: `ambient settings set savings on` (or
`AMBIENT_SAVINGS=on`). `AMBIENT_REFERENCE_PRICE` — an `in/out` per-million-token pair
or one blended figure — then sets the baseline it is measured against.

With savings on the figures stay conservative: a model whose catalog pricing is
missing shows an assumed worst-case cost with **no** savings claim, estimated token
counts are labeled `(est.)`, the saved-% is rounded down, and a model pricier than the
reference reads "costlier" rather than a fake saving. With savings off, `ambient usage`
reports calls and tokens per model and nothing it cannot measure. One honest gap:
`ambient agent` hands off to an external opencode process whose spend is billed by
Ambient but not visible to local metering — `ambient usage` discloses this instead of
pretending its totals are complete.

**Self-calibrating token math (smarter with use)** — cost estimates and
per-model budgets convert characters to tokens. Out of the box that uses a
fixed 3.2 chars/token heuristic; once real runs land in the local usage
ledger, ambient learns each model's *observed* chars-per-token (a
recent-weighted average of what the API actually reported) and uses it for
that model's budget sizing and cost estimates. No history → exactly the
static default, so nothing shifts on a fresh install; observed ratios are
clamped to a sane 1.0–8.0 range so a corrupt ledger can't skew budgets. Set
`AMBIENT_TELEMETRY=off` to keep the static constants.

**Advisory routing** (your explicit model choice is *never* silently swapped):
`-m auto` delegates the pick — the cheapest READY model that fits the input,
resolved against the live catalog on every call and printed to stderr
(`auto:cheapest` / `auto:largest` variants; `ambient use auto` makes it the
sticky default). If your *concrete* model isn't currently serving or the input
outgrows its window, a one-line stderr hint names READY alternatives with
prices — purely informational, nothing changes. `--reduce-model ID` runs the map-reduce
*synthesis* step on a different model (cheap map, strong reduce), and
`AMBIENT_MODEL_MAP` (env or config, alongside `AMBIENT_MAX_PARALLEL` /
`AMBIENT_MAX_SPEND`) routes phases persistently, e.g.
`AMBIENT_MODEL_MAP="map=z-ai/glm-5.2,reduce=moonshotai/kimi-k2.7-code"`
(phases: `chat`, `code`, `map`, `reduce`) — explicit `-m` / `--reduce-model`
always win. The opt-in `--fallback` now ranks alternates fit-then-cheapest
(the cheapest fitting model, not the biggest).

**Bulk per-item work is `ambient map`** — the "a thousand cheap things at once,
with a receipt" lane. One prompt is applied *independently* to each item (each
file, or each stdin line; `--jsonl` for `{"input": …, "id": …}` objects), one
single-shot call per item, fanned out `--parallel` wide. **One batch cost gate
up front** prices the whole run before the first call. Results stream per item
as they complete; a re-run serves already-finished items from the local cache
and re-bills only the missing ones (`--no-cache` disables). An item too large
for the model's single-shot window is refused *per item* (use `ambient audit`
for big files) — the rest of the batch still runs.

```bash
ambient map "summarize this file in one sentence" src/*.py
cat titles.txt | ambient map "classify: bug, feature, or question?"
cat questions.jsonl | ambient map "answer concisely" --jsonl --json > answers.jsonl
```

**Whole-repo audits are `ambient audit --repo [DIR]`** (default `.`). Inside a
git repo the walker uses `git ls-files` so `.gitignore` is respected; anywhere
else it walks the directory, pruning `.git`/`node_modules`/`dist`/`build`/
`vendor`/`__pycache__` and dot-directories and never following symlinks out of
`DIR`. Binaries (NUL-sniffed), lockfiles, empty files, and files over ~1 MB are
skipped with counts. **Before anything is sent** it reports the plan — file
count, total chars, chunk count, and the estimated cost (under `--format json`,
one compact `{"status": "plan", …}` object precedes the standard audit
envelope) — and a repo bigger than the 20M-char input ceiling is refused
cleanly unless `--allow-cost`/`--allow-partial` accepts auditing the files that
fit (the rest becomes an explicit coverage gap). The audit itself runs through
the same map-reduce as any oversized input, so `--parallel`, `--reduce-model`,
`--consensus`, `--focus`, the cost gate, and the fleet budget all apply
unchanged; every chunk carries a multi-language repo map (Python via ast;
JS/TS, Go, Rust, Ruby, Java/C#/C/C++ via top-level signature scans) sized to
the model's window, with an explicit `(+N files omitted)` marker when it can't
hold everything. After a chunked repo pass, ONE bounded **cross-file
confirmation pass** re-checks findings that span files reviewed in separate
chunks (at most one extra gated call over the suspect files; `--no-deep` skips
it, `--deep` enables it for non-repo audits). If the spend gate refuses that
optional extra call, the pass is skipped with a note and the pass-1 findings
are rendered unchanged — a refused deep pass never discards the paid result.
Under `--consensus` the deep pass never runs (multi-model corroboration
already cross-checks findings), so `--deep`/`--no-deep` are no-ops there; the
repo plan's `deep` field states what will actually happen.

```bash
ambient audit --repo . --focus security --format report
ambient audit --repo src/ --json --reduce-model z-ai/glm-5.2 > findings.json
```

**Quality from cheapness — `--best-of K`** (ask / code / audit). On a network
this cheap, the smart move is often *more samples, not a bigger model*:
`--best-of K` draws K **independent** samples (2-8) concurrently at
temperature > 0 (an explicit `--temperature 0` is raised to 0.7 with a note —
identical samples corroborate nothing) behind **one up-front cost gate that
prices all K calls** before the first one. Each sample lives in its own
salted cache lane, so an interrupted run resumes per sample and re-bills only
the missing ones. For `ask`/`code` the K candidates are printed along with a
deterministic, honestly-labeled selection — exact-majority vote for short
answers, otherwise the pairwise-similarity centroid (no hidden LLM judge; the
note states exactly which method picked what). For `audit`, findings are
**ranked by corroboration** — a bug flagged by 2 of 3 samples ranks above a
one-sample finding — with the vote count shown per finding (`[2/3 samples]`;
under `--json` an additive `corroboration: {count, of}` per finding plus
`best_of: K`). `--best-of` and `--consensus` are mutually exclusive (two
different corroboration lanes), and under `--best-of` the repo deep pass is
skipped for the same reason as under `--consensus`.

```bash
ambient ask "is this migration reversible?" --best-of 3
ambient audit src/pay.py --best-of 3 --json     # corroboration-ranked findings
```

**Triangulate an answer — `ask --consensus A,B`.** The same question runs on
several **explicitly-named** models (your set, never substituted)
concurrently, behind one summed up-front gate. You get every model's answer,
a per-model receipt, and an agreement note — a deterministic *textual*
similarity measure (it says so; it is not a semantic proof): `high` /
`medium` / `low`, with divergence surfaced loudly. Under `--json` the
envelope adds `consensus`, per-model `answers`, and an `agreement` object. A
funds/key/network failure aborts the whole set fail-fast; any other
per-model failure is reported and makes the result PARTIAL (exit 2), never
silently dropped.

**Live conversation is `ambient chat`** — a native readline REPL on the same
machinery as `ask`: replies stream as they generate, every turn ends with the
cost/savings receipt, and every turn is cost-gated and fleet-reserved like
any other call. Rolling in-memory history is trimmed oldest-first to the
model's window (the system prompt and your latest message always survive).
`/model ID` switches models mid-session (explicit and printed — `auto` specs
resolve via the live catalog), `/clear` forgets the conversation, `/exit` or
Ctrl-D quits, and Ctrl-C interrupts only the current turn. Chat requires a
real terminal; piped/scripted use gets a clean pointer to `ambient ask`.

**A standing audit gate is `ambient audit --install-hook [pre-commit|pre-push]`.**
It writes a **fixed, human-readable shell script** (never model-generated —
the script only *runs* `ambient audit --staged --json` / `--diff "@{u}...HEAD"
--json` and greps the verdict) to `.git/hooks/`, executable, no API key
needed to install. Threshold, documented in the script itself: it **blocks
only on verdict `FIX FIRST`** (CRITICAL/HIGH findings); `NEEDS WORK` and
clean passes never block, and everything unhealthy — ambient missing,
unconfigured, empty diff, network down — fails **open** (a review hook must
never brick commits). Overrides: `AMBIENT_HOOK_MODE=warn` reports without
blocking; `git commit --no-verify` / `git push --no-verify` bypasses once.
An existing hook that ambient didn't install is never clobbered without
`--force` (which backs it up to `<hook>.pre-ambient.bak`), and
`--uninstall-hook` removes only an ambient-installed hook.

```bash
ambient audit --install-hook              # pre-commit gate on the staged diff
ambient audit --install-hook pre-push     # gate outgoing commits instead
ambient audit --uninstall-hook            # removes only ambient's hook
```

## Agentic use (for scripts and orchestrators)

Exit codes: `0` ok · `1` diagnosed error (`ambient [category]: …`) · `2` **partial
result** (output delivered, coverage incomplete — never a clean pass) · `3` no key
configured · `64` usage error · `130` interrupted.

Every task-running `--json` surface emits one envelope: `{"schema_version": 1, "kind":
"ask|code|audit|consensus|build", "status": "ok|partial", "model", "partial",
"coverage_gap", "exit_code", …}` plus `content`, or `findings`+`verdict`, or
`files[]`+`failed[]`+`advisory_steps[]`. The shape never depends on how the result
was computed (single-shot vs map-reduce). A failed `--json` run is machine-readable
too: `{"schema_version": 1, "kind", "status": "error", "category", "diagnosis",
"exit_code": 1}` on stdout, exit 1 — never a bare stderr line.

`ambient map --json` streams **JSONL**: one envelope per line, per item, as each
completes (out of order — the `id` field names the item: its path, stdin item
index, or `--jsonl` id): `{"schema_version": 1, "kind": "map", "status":
"ok|partial|error", "id", "content", "exit_code"}` (+`category`/`diagnosis` on
errors, `"cached": true` on cache hits). Batch exit: `0` when every item is ok,
`2` when any item failed or was truncated (unless `--allow-partial`), with a
final `N ok / M failed / K cached` line on stderr; a fatal key/funds/network
failure aborts the queue immediately and exits `1` via the error envelope.

## When something fails

Errors are pre-diagnosed, so you're never left guessing whether "Ambient is down":

| You see | It means | Fix |
|---|---|---|
| `ambient [key]` | Key revoked/expired/mistyped | `ambient setup --force` with a fresh key |
| `ambient [funds]` | Account out of credit/quota | Top up at app.ambient.xyz |
| `ambient [model]` | That model isn't serving right now (normal — capacity follows demand) | `ambient models`, pick one that's serving |
| `ambient [rate]` | Rate limited | Wait and retry |
| `ambient [service]` | Ambient-side problem | Retry shortly — not your fault |
| `ambient [network]` | Can't reach the API at all | Check your own internet first |
| `ambient [setup]` | No API key configured yet | `ambient setup` |

`ambient doctor` runs the full checkup (config perms, key validity via a real
authenticated completion, model availability, launcher, agent lane, network) and
prints a one-line DIAGNOSIS.

## Security posture

- The key lives in the macOS Keychain / libsecret (0600 env-file fallback, written
  atomically, permissions self-healed); never in argv, never printed, redacted from
  every output path, verified with a real authenticated request before saving.
- Model output is treated as untrusted AND sanitized — ANSI/OSC terminal escapes
  are stripped so a malicious node can't forge verdicts or fire clipboard writes.
- A built-in tripwire refuses to send credential-looking content or
  credential-named files (`.env`, `.netrc`, `id_rsa`, `*.pem`, `credentials.json`,
  …) — locations only, never values; `--allow-secrets` for false positives.
- `ambient build` writes only inside the target directory through a path-traversal
  firewall (no `..`, no symlink escapes, no `.git`, no credential files, byte caps)
  and never executes anything — suggested commands are printed as text.
- The key is only ever sent to `*.ambient.xyz`; any other endpoint requires an
  explicit, typed `ambient trust-url` confirmation.
- **One exception, by design:** `ambient agent` passes the key to the opencode
  subprocess via its environment — the standard credential model of `aws`/`gh`/
  `docker`. That lane reads files itself, outside the tripwire; keep credentials
  out of its working tree.
- No telemetry. Usage metering is local (`~/.config/ambient/usage.jsonl`, 0600).

## Uninstall

```bash
ambient setup --remove      # delete the stored key (Keychain + env file)
ambient cache clear         # drop cached model output
ambient link --remove       # remove the PATH launcher
/plugin uninstall ambient-code@ambient
```

`~/.config/ambient/` keeps only your sticky settings after that — delete it to
reset everything.

## Notes

- Codex CLI support is staged but blocked upstream: Codex ≥0.142 requires the
  OpenAI Responses API and Ambient's `/v1/responses` endpoint currently rejects
  Codex's tool schema. `ambient codex` explains; `ambient agent` is the working
  agentic lane meanwhile.
- Single-file stdlib-only Python CLI (`bin/ambient`, Python 3.8+); hermetic tests
  (`python3 -m unittest discover -s tests`), live battery (`tools/stress_test.sh`),
  CI on macOS/Linux/Windows × Python 3.8-3.13. Releases: `docs/RELEASING.md`.

MIT licensed. Ambient and ambient.xyz are trademarks of their respective owner.
