# Privacy

`ambient-code` is local-first: it sends nothing anywhere except the inference
requests you make, to the Ambient API URL you configure.

**Ambient is a decentralized network.** `https://api.ambient.xyz` (the default)
is the entry point to the Ambient network, not a single first-party server — the
prompts and code in a request are served by independent model operators on that
network. Treat anything you send as disclosed to a third-party network, and see
Ambient's own data, retention, and training policy at
[ambient.xyz/privacy](https://ambient.xyz/privacy) for what the network does with
it.

## What leaves your machine
- **Only** the prompts/code you explicitly send in a request, and **only** to the
  Ambient API URL in your config (`https://api.ambient.xyz` by default), which
  routes it to the network's operators. Nothing else.
- No analytics, no telemetry, no phone-home, no crash reporting from this tool.
  The single outbound host is your configured `AMBIENT_API_URL`.
- **Delegate and takeover modes send more, automatically.** With delegate mode on
  (`/ambient on`) or an `ambient takeover` session, substantive turns are routed
  to the Ambient network without a per-request prompt until you turn it off — so
  more of your code and context leaves the machine than in one-off use.

## What stays on your machine
- Your API key: in the macOS Keychain (or a `chmod 600` file / OS secret store).
- Token/cost metering: a local `~/.config/ambient/usage.jsonl`, never uploaded.
- The chunk cache (on by default; `--no-cache` skips it): local only.

## Your responsibility
- Auditing code publishes it to the network you chose. The built-in secrets
  tripwire refuses obvious credentials and `.env` files, but review what you send.
- **Never** send secrets, credentials, or personal/health data.

## The `ambient agent` boundary
`ambient agent` launches [opencode](https://opencode.ai), a separate tool with
its own privacy behavior. This statement covers the `ambient` CLI itself, not
opencode. `ambient agent` passes your API key to opencode via the environment
(the standard credential model) — don't ask an agent to print its environment.
The outbound secrets tripwire does **not** apply to this lane: the agent reads
files from its working directory directly, so keep `.env` files, keys, and
credentials out of any tree you point it at.

## On-disk inventory (all local, all owner-only)

| Path | Contents | Purge |
|---|---|---|
| OS keychain item `ambient.xyz` | your API key | `ambient setup --remove` |
| `~/.config/ambient/env` (0600) | endpoint, defaults, curation — key only with `--file` | delete the file |
| `~/.config/ambient/cache/*.json` (0600, 7-day TTL) | model output quoting your code (map-reduce chunk results) | `ambient cache clear` |
| `~/.config/ambient/usage.jsonl` (0600) | timestamps, model ids, token counts — no content | delete the file |
| `<build dir>/.ambient-build.json` (0600) | resume state incl. generated file contents | delete after applying |
| `~/.config/opencode/opencode.json` | opencode's own config (references the API key via env, not the literal key) — written only when you run `ambient agent` | delete the file; `ambient uninstall` does not remove it |

`AMBIENT_TELEMETRY` controls only **local** self-calibration — it reads
`usage.jsonl` to tune model routing on your machine and transmits nothing.
Despite the name, it is not network telemetry.

Nothing is transmitted anywhere except your prompts/code to the Ambient API you
configured (which serves them over the decentralized network). No telemetry, no
analytics, no phoning home from this tool.
