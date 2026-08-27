#!/usr/bin/env bash
# SessionStart hook (startup|resume|clear|compact):
#  1. Self-heal the ~/.local/bin/ambient launcher — plugin updates move the
#     versioned install dir, and the old dir is garbage-collected later, which
#     would leave the user's terminal `ambient` dangling.
#  2. Remind Claude when Ambient delegate mode is ON.
# Prints nothing (adds no context) in the normal case.
set -eu

if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -x "${CLAUDE_PLUGIN_ROOT}/bin/ambient" ]; then
  link="$HOME/.local/bin/ambient"
  real="${CLAUDE_PLUGIN_ROOT}/bin/ambient"
  # Heal ONLY this well-known path, and ONLY when it is a SYMLINK we OWN:
  #  - dangling (a plugin update GC'd the old versioned dir it pointed at), or
  #  - a stale ambient-code launcher (target exists but is not the ACTIVE
  #    install).
  # OWNERSHIP is proven by an `ambient-code` path component in the stored
  # target (every real install — dev `.../skills/ambient-code/...` or
  # marketplace `.../ambient-code/<ver>/...` — has it; a DIFFERENT tool merely
  # named `ambient` does not). A real (non-symlink) file, or a symlink to any
  # non-ambient-code target, is NEVER touched — never clobber a foreign
  # `ambient` the user installed themselves. readlink still reports the stored
  # target of a broken (dangling) symlink, so the same guard covers both cases.
  if [ -L "$link" ]; then
    target="$(readlink "$link" 2>/dev/null || true)"
    case "$target" in
      */ambient-code/*)
        if [ ! -e "$link" ] || [ "$target" != "$real" ]; then
          "$real" link >/dev/null 2>&1 || true
        fi
        ;;
    esac
  fi
fi

# Bridge self-heal: if THIS session runs on our loopback bridge and the bridge has died,
# relaunch it on its recorded port + token so the session keeps working. It health-checks
# first (never spawns a duplicate) and only acts when the recorded state matches this port.
case "${ANTHROPIC_BASE_URL:-}" in
  http://127.0.0.1:*|http://localhost:*)
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && command -v python3 >/dev/null 2>&1; then
      python3 - <<'PY' >/dev/null 2>&1 || true
import json, os, re, subprocess, sys, urllib.request
base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
m = re.match(r"http://(?:127\.0\.0\.1|localhost):(\d+)$", base)
if not m:
    sys.exit(0)
port = int(m.group(1))
try:
    st = json.load(open(os.path.expanduser("~/.config/ambient/bridge.json")))
except Exception:
    sys.exit(0)
if not isinstance(st, dict) or st.get("port") != port or not st.get("token"):
    sys.exit(0)
# Is OUR bridge already up? Verify IDENTITY (an authenticated /v1/models with our token),
# not just /healthz, so a foreign listener on the port is never mistaken for ours.
req = urllib.request.Request(base + "/v1/models", headers={"x-api-key": st["token"]})
try:
    with urllib.request.urlopen(req, timeout=1) as r:
        if r.status == 200:
            sys.exit(0)
except Exception:
    pass
exe = os.path.join(os.environ.get("CLAUDE_PLUGIN_ROOT", ""), "bin", "ambient")
if not exe.strip("/ ") or not os.path.exists(exe):
    sys.exit(0)
# Relaunch on the recorded port with our token. A duplicate spawn is self-correcting:
# the loser fails to bind the port and exits.
try:
    subprocess.Popen([sys.executable, exe, "serve", "--port", str(port)],
                     env=dict(os.environ, AMBIENT_BRIDGE_LOCAL_TOKEN=st["token"]),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
except Exception:
    pass
PY
    fi
    ;;
esac

conf="$HOME/.config/ambient/env"
[ -f "$conf" ] || exit 0
# Last assignment wins AND key/value whitespace is trimmed, matching the CLI's
# config parser (key.strip()/val.strip()) — a duplicate OR hand-spaced
# AMBIENT_DELEGATE line must not make the hook disagree with `ambient mode`.
mode="$(sed -n 's/^[[:space:]]*AMBIENT_DELEGATE[[:space:]]*=[[:space:]]*//p' "$conf" 2>/dev/null \
        | sed 's/[[:space:]]*$//' | tail -1)"
if [ "$mode" = "takeover" ]; then
  cat <<'MSG'
Ambient TAKEOVER mode is ON (persisted setting). Per the ambient skill's takeover
contract, route EVERY substantive turn through Ambient so the user spends AMBIENT
tokens, not Claude's: conversation/questions/explanations via `ambient ask` (use
`ambient chat` only for a user-opened interactive terminal); code via `ambient
build`/`ambient code`; reviews via `ambient audit`. Claude stays the thin
router/safety/integration layer — it briefs Ambient, runs the outbound secret
tripwire, reviews + tests + integrates the result, and keeps its OWN token use
minimal. Keep these with Claude (never route to Ambient): trivial one-liners; any
safety-critical/destructive/irreversible turn (auth, crypto, secret handling, rm,
force-push, migrations, prod ops); and NEVER send secrets or user/health data.
Begin each substantive turn with exactly this one line:
🟢 Ambient Takeover ON — running on your Ambient tokens · /ambient off to stop
The user turns it off with /ambient off.
MSG
elif [ "$mode" = "on" ]; then
  cat <<'MSG'
Ambient delegate mode is ON (persisted setting). Per the ambient skill's delegate
contract: the user plans with Claude, Ambient (default Kimi) writes the code, Claude
reviews and integrates. Route substantive code writing through `ambient build`
(multi-file, manifest + --apply) or `ambient code` (single file); review every
generated diff and run the tests yourself. Trivial edits stay with Claude. The user
toggles this with /ambient off.
MSG
fi
exit 0
