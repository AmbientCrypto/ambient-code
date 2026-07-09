"""P3 — build-lane adaptation: a model that won't emit a structured plan gets a
downgrade retry, an actionable error naming the reliable model, and its failure
is LEARNED. A capable model is recorded ok. No silent model swap. See
docs/plans/2026-07-06-stress-test-remediation.md."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin", "ambient")


def _load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_build_adaptive", _BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_build_adaptive", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = _load_module()
KEY = "sk-test-key-abcdef1234567890"


@contextlib.contextmanager
def _patched(**attrs):
    old = {}
    missing = object()
    for k, v in attrs.items():
        old[k] = getattr(amb, k, missing)
        setattr(amb, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is missing:
                delattr(amb, k)
            else:
                setattr(amb, k, v)


def _catalog(mid):
    return lambda *a, **k: [
        {"id": mid, "context_length": 200000, "max_output_length": 200000,
         "is_ready": True, "supported_features": ["reasoning", "structured_outputs"],
         "output_modalities": ["text"], "pricing": {"input": 1.0, "output": 4.0}}]


def _build_args(model, d):
    return argparse.Namespace(
        model=model, task=["a", "tiny", "tool"], context=None, dir=d,
        apply=False, dry_run=False, plan_only=True, no_resume=True,
        max_files=20, max_file_bytes=200_000, max_tokens=None, temperature=0.1,
        timeout=30, raw=False, json=True, fallback=False, allow_partial=False,
        allow_secrets=False, allow_cost=True, yes=True, no_cache=True,
        cache_ttl=None, parallel=None, system=None, response_format=None)


def _run_build(model, complete_fn):
    d = tempfile.mkdtemp()
    args = _build_args(model, d)
    buf = io.StringIO()
    exit_code = None
    with _patched(safe_catalog=_catalog(model), cost_gate=lambda *a, **k: None,
                  complete=complete_fn), \
            contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            amb.cmd_build(args, KEY, "https://x", {})
        except SystemExit as e:
            exit_code = e.code
    return buf.getvalue(), exit_code


class TestV22BuildAdaptive(unittest.TestCase):
    def setUp(self):
        # was the autouse _isolate fixture: CAPABILITY_PATH -> temp file,
        # _CAP_CACHE reset before AND after, AMBIENT_TELEMETRY unset.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self._old_cap_path = amb.CAPABILITY_PATH
        amb.CAPABILITY_PATH = os.path.join(tmp, "caps.json")
        self.addCleanup(setattr, amb, "CAPABILITY_PATH", self._old_cap_path)
        amb._CAP_CACHE = None
        self.addCleanup(setattr, amb, "_CAP_CACHE", None)
        self._old_telemetry = os.environ.pop("AMBIENT_TELEMETRY", None)
        self.addCleanup(self._restore_telemetry)

    def _restore_telemetry(self):
        if self._old_telemetry is not None:
            os.environ["AMBIENT_TELEMETRY"] = self._old_telemetry
        else:
            os.environ.pop("AMBIENT_TELEMETRY", None)

    # --- downgrade helper --------------------------------------------------
    def test_downgrade_ladder_steps_down(self):
        class P:
            features = ["structured_outputs", "json_mode"]
        schema_rf = {"type": "json_schema", "json_schema": {"schema": {}}}
        step1 = amb.downgrade_response_format(schema_rf, P())
        assert step1 == {"type": "json_object"}          # schema -> json_object
        assert amb.downgrade_response_format(step1, P()) is None  # json_object -> None

        class P2:
            features = ["structured_outputs"]             # no json_mode
        assert amb.downgrade_response_format(schema_rf, P2()) is None  # straight to prompt-only

    # --- build fails on a model that won't emit a plan ----------------------
    def test_unparseable_plan_downgrades_then_errors_naming_reliable_model(self):
        calls = []

        def prose_only(api_key, api_url, model, messages, args, **kw):
            calls.append(getattr(args, "response_format", None))
            return ("I would create a file called tool.py that does the thing.", {}, {})

        out, code = _run_build("stubborn/model", prose_only)
        assert code == 1
        # it retried with a DIFFERENT (downgraded) response_format, not the same one
        assert len(calls) == 2 and calls[0] != calls[1]
        env = json.loads(out)
        assert env["kind"] == "build" and env["status"] == "error"
        assert env["category"] == "model"
        assert amb.DEFAULT_CODE_MODEL in env["diagnosis"]   # names the reliable model

    def test_build_failure_is_learned(self):
        def prose_only(api_key, api_url, model, messages, args, **kw):
            return ("nope, just prose here", {}, {})

        _run_build("stubborn/model", prose_only)
        assert amb.cap_state("stubborn/model", "build_plan") != "ok"
        _run_build("stubborn/model", prose_only)  # second failure => unreliable
        assert amb.cap_state("stubborn/model", "build_plan") == "unreliable"

    # --- Codex-found build bugs ---------------------------------------------
    def test_rf_ladder_is_full_and_unique_for_capable_model(self):
        class P:
            features = ["structured_outputs", "json_mode"]
        ladder = amb.build_plan_rf_ladder("cap/model", P())
        types = [None if r is None else r.get("type") for r in ladder]
        assert types == ["json_schema", "json_object", None]  # full ladder, no skip

    def test_rf_ladder_min_two_entries_for_unreliable(self):
        class P:
            features = ["structured_outputs", "json_mode"]
        for _ in range(amb.CAP_FAIL_THRESHOLD):
            amb.record_cap("bad/model", "structured_json", False)
        ladder = amb.build_plan_rf_ladder("bad/model", P())
        assert len(ladder) >= 2 and all(r is None for r in ladder)  # prompt-only ×2

    def test_terminal_chaterror_aborts_without_burning_the_ladder(self):
        calls = []

        def funds_error(api_key, api_url, model, messages, args, **kw):
            calls.append(1)
            raise amb.ChatError("funds", "no money near sk-x")

        out, code = _run_build("m", funds_error)
        assert code == 1 and len(calls) == 1          # aborted on first, no retry
        assert amb.cap_state("m", "build_plan") != "ok"

    def test_nonterminal_chaterror_downgrades_and_can_succeed(self):
        # Only a generic 400 ('unknown' — a structured-output rejection) downgrades.
        plan = json.dumps({"plan": [{"path": "tool.py", "purpose": "x"}]})
        state = {"n": 0}

        def flaky(api_key, api_url, model, messages, args, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise amb.ChatError("unknown", "HTTP 400: response_format rejected")
            return (plan, {}, {})

        out, code = _run_build("m", flaky)
        assert state["n"] == 2 and amb.cap_state("m", "build_plan") == "ok"

    def test_all_unsafe_paths_records_failure_not_success(self):
        evil = json.dumps({"plan": [{"path": "../escape.py", "purpose": "x"}]})

        def bad_paths(api_key, api_url, model, messages, args, **kw):
            return (evil, {}, {})

        out, code = _run_build("m", bad_paths)
        assert code == 1
        assert amb.cap_state("m", "build_plan") != "ok"   # NOT recorded capable

    def test_fallback_served_model_gets_the_credit_not_requested(self):
        plan = json.dumps({"plan": [{"path": "tool.py", "purpose": "x"}]})

        def served_by_other(api_key, api_url, model, messages, args, **kw):
            return (plan, {}, {"_served_model": "actual/server"})

        _run_build("requested/model", served_by_other)
        assert amb.cap_state("actual/server", "build_plan") == "ok"
        # the requested model that never produced it is not credited
        assert amb.cap_state("requested/model", "build_plan") != "ok"

    def test_rate_limit_aborts_immediately_not_downgrade(self):
        # Codex round 2: only a generic 400 ('unknown') is downgradeable; a rate
        # limit must abort on the first attempt, not burn the ladder + record False.
        calls = []

        def rate_limited(api_key, api_url, model, messages, args, **kw):
            calls.append(1)
            raise amb.ChatError("rate", "slow down")

        out, code = _run_build("m", rate_limited)
        assert code == 1 and len(calls) == 1
        env = json.loads(out)
        assert env["category"] == "rate"          # surfaces the REAL cause
        # Codex round 3: an infra failure must NOT poison the model's build_plan
        # capability (it isn't the model failing to plan).
        assert amb.cap_state("m", "build_plan") == "unknown"

    def test_malformed_completion_meta_does_not_crash(self):
        # Codex round 2: a non-dict _b (e.g. a string) must not crash served-model
        # attribution.
        plan = json.dumps({"plan": [{"path": "tool.py", "purpose": "x"}]})

        def weird_meta(api_key, api_url, model, messages, args, **kw):
            return (plan, {}, "not-a-dict")

        out, code = _run_build("m", weird_meta)
        assert amb.cap_state("m", "build_plan") == "ok"  # recorded under requested

    def test_valid_plan_is_recorded_ok(self):
        plan = json.dumps({"plan": [{"path": "tool.py", "purpose": "does the thing"}]})

        def good_plan(api_key, api_url, model, messages, args, **kw):
            return (plan, {}, {})

        _run_build("capable/model", good_plan)
        assert amb.cap_state("capable/model", "build_plan") == "ok"

    def test_learned_unreliable_model_announces_but_does_not_swap(self):
        def prose_only(api_key, api_url, model, messages, args, **kw):
            return ("prose, no json", {}, {})

        # teach it unreliable
        for _ in range(amb.CAP_FAIL_THRESHOLD):
            _run_build("stubborn/model", prose_only)
        assert amb.cap_state("stubborn/model", "build_plan") == "unreliable"

        seen_models = []

        def track_model(api_key, api_url, model, messages, args, **kw):
            seen_models.append(model)
            return ("prose, no json", {}, {})

        out, _ = _run_build("stubborn/model", track_model)
        # the user's model is still the one called — NO silent swap
        assert set(seen_models) == {"stubborn/model"}


if __name__ == "__main__":
    unittest.main()
