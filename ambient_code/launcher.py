"""Pure launcher helpers for `ambient serve` / `ambient claude`.

Keeps bin/ambient's serve/claude subcommands thin: token minting, the bridge state file
(host/port/pid/token, 0600), a token-VERIFIED health probe, and free-port selection live
here (testable without a socket server). The actual process spawn + `execvpe("claude", ...)`
stay in bin/ambient, which owns the launcher path and the OS keychain.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import tempfile
import urllib.error
import urllib.request
from typing import Optional

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def generate_local_token() -> str:
    """A random bearer token given to Claude Code as ANTHROPIC_API_KEY. NEVER the Ambient
    key — the bridge injects the real key upstream itself, so the client never sees it."""
    return "ambr_" + secrets.token_urlsafe(32)


def bridge_state_path(config_dir: str) -> str:
    return os.path.join(config_dir, "bridge.json")


def read_bridge_state(path: str) -> Optional[dict]:
    """Return the recorded bridge state, but ONLY if it is loopback + well-formed (a
    non-loopback host or a missing token/port is rejected, so we never point Claude Code
    or its credentials at a routable or malformed target)."""
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    if not isinstance(state.get("port"), int) or not isinstance(state.get("token"), str):
        return None
    if str(state.get("host", "127.0.0.1")) not in _LOOPBACK:
        return None
    return state


def write_bridge_state(path: str, host: str, port: int, pid: int, token: str) -> None:
    """Atomic, exclusive 0600 write (mkstemp -> replace): never inherit a pre-existing
    file's laxer mode, never follow a symlink, never leave a world-readable token file."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".bridge.", dir=d)  # created 0600, O_EXCL
    try:
        os.write(fd, json.dumps({"host": host, "port": port, "pid": pid, "token": token}).encode())
        if hasattr(os, "fchmod"):   # POSIX only; Windows uses ACLs + mkstemp's 0600
            os.fchmod(fd, 0o600)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def find_free_port(host: str = "127.0.0.1") -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def health_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    """GET /healthz answers 200 with our bridge marker (liveness only, not identity)."""
    try:
        with urllib.request.urlopen("http://{0}:{1}/healthz".format(host, port), timeout=timeout) as r:
            if r.status != 200:
                return False
            body = json.loads(r.read().decode("utf-8"))
            return isinstance(body, dict) and body.get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


def bridge_is_ours(host: str, port: int, token: str, timeout: float = 2.0) -> bool:
    """IDENTITY check: an authenticated endpoint (/v1/models) accepts OUR token. A foreign
    listener that grabbed the port answers 401/404, so we never hand it Claude's traffic."""
    if str(host) not in _LOOPBACK or not token:
        return False
    req = urllib.request.Request("http://{0}:{1}/v1/models".format(host, port),
                                 headers={"x-api-key": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


def bridge_is_live(state: Optional[dict], timeout: float = 2.0) -> bool:
    """Is the recorded bridge OURS and answering on its recorded loopback port?"""
    if not state:
        return False
    return bridge_is_ours(state.get("host", "127.0.0.1"), state["port"],
                          state.get("token", ""), timeout=timeout)
