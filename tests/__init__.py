"""Test-suite hermeticity guard.

The telemetry-EWMA routing learns each model's observed chars-per-token from
the REAL user ledger (~/.config/ambient/usage.jsonl). Exact-value estimation
tests would silently start depending on whatever history the developer's
machine has accumulated — so the suite defaults telemetry OFF at import time
(before any test module loads bin/ambient). Telemetry tests that exercise the
feature opt back in explicitly by patching the environment.
"""
import os

os.environ.setdefault("AMBIENT_TELEMETRY", "off")

# makes EVERY lane (including plain ask/code/single-shot audit)
# fleet-reserve through ~/.config/ambient/reservations.jsonl. Cmd-level tests
# that aren't about the fleet must not write to (or get refused by) the REAL
# store on the developer's machine — default the fleet lane off at import
# time, exactly like the telemetry guard above. Fleet tests opt back in by
# clearing the variable inside their own tmpdir contexts.
os.environ.setdefault("AMBIENT_FLEET_BUDGET", "off")
