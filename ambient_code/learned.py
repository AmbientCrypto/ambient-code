"""Learned per-model input ceilings — the bridge's transparent window self-heal.

Ambient is an inference provider: a model's REAL enforced context window can change (or
the live catalog can lag/lie) — e.g. DeepSeek drops 50k→40k while `/v1/models` still says
50k. When a model 400s on a request the catalog said should fit, we LEARN a ceiling at or
below that size and use `min(catalog, learned)` for every later request, so the bridge
compacts BEFORE re-hitting the real limit and the run just keeps going — invisibly.

It self-CORRECTS: each learned value carries a timestamp and expires after a TTL, so if a
model's window grows back the bridge forgets the old ceiling and returns to the catalog
value. Thread-safe; optionally persisted (0600) so it survives a bridge restart.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from typing import Optional


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)

_DEFAULT_TTL_S = 6 * 3600  # forget a learned ceiling after 6h -> recover if the window grows


class LearnedWindows:
    def __init__(self, path: Optional[str] = None, ttl_s: int = _DEFAULT_TTL_S):
        self._path = path
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._data = {}  # model -> [learned_window, recorded_at]
        if path:
            self._load()

    def record(self, model: str, failed_input_tokens: int, now: Optional[float] = None) -> None:
        """A model 400'd at ~`failed_input_tokens`; its real window is at or below that.
        Learn the SMALLEST such ceiling (monotone down within the TTL) so we never
        under-shoot the real limit."""
        if not isinstance(model, str) or not model or not isinstance(failed_input_tokens, int):
            return
        # A non-finite `now` (NaN/inf) would poison the TTL math forever (a NaN
        # timestamp never expires under `now - ts > ttl`), so fall back to the clock.
        now = time.time() if now is None or not _finite(now) else float(now)
        ceiling = max(1, failed_input_tokens)
        with self._lock:
            cur = self._data.get(model)
            if cur is None or now - cur[1] > self._ttl or ceiling < cur[0]:
                self._data[model] = [ceiling, now]
                self._save_locked()

    def effective(self, model: str, catalog_window: Optional[int],
                  now: Optional[float] = None) -> Optional[int]:
        """The window to plan against: `min(catalog, learned)`, or the catalog value once a
        learned ceiling has expired (window grew back)."""
        now = time.time() if now is None or not _finite(now) else float(now)
        with self._lock:
            cur = self._data.get(model)
            if cur is None:
                return catalog_window
            if now - cur[1] > self._ttl:
                del self._data[model]
                self._save_locked()
                return catalog_window
            learned = cur[0]
        if catalog_window is None:
            return learned
        return min(catalog_window, learned)

    # ---- persistence (best-effort; never crash the bridge) ----------------
    def _load(self):
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for k, v in data.items():
                    if not (isinstance(k, str) and isinstance(v, list) and len(v) == 2):
                        continue
                    win, ts = v
                    # Reject poisoned/garbage rows: a non-positive ceiling would force
                    # perpetual compaction, and a NaN/inf/overflowing timestamp would
                    # never expire (bricking self-correction). bool is an int subclass —
                    # exclude it so `is_ready: true`-style junk can't pose as a ceiling.
                    if isinstance(win, bool) or not (isinstance(win, int) and win > 0):
                        continue
                    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                        continue
                    try:
                        ts = float(ts)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if not math.isfinite(ts):
                        continue
                    self._data[k] = [win, ts]
        except (OSError, ValueError):
            pass

    def _save_locked(self):
        if not self._path:
            return
        try:
            d = os.path.dirname(self._path) or "."
            fd, tmp = tempfile.mkstemp(prefix=".learned.", dir=d)
            try:
                os.write(fd, json.dumps(self._data).encode("utf-8"))
                if hasattr(os, "fchmod"):   # POSIX only; Windows uses ACLs + mkstemp's 0600
                    os.fchmod(fd, 0o600)
                os.close(fd)
                fd = -1
                os.replace(tmp, self._path)
            finally:
                if fd != -1:
                    os.close(fd)
                if os.path.exists(tmp):
                    os.remove(tmp)
        except OSError:
            pass
