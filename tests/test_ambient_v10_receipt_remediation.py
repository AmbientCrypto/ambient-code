"""Hermetic REMEDIATION tests — savings-receipt honesty fixes.

Review findings on the savings receipts, each one an honesty
violation (over-stated saving or under-counted spend):

- H1: under --fallback the receipt priced the REQUESTED model, not the model
  that actually SERVED — a pricier fallback over-stated the saving;
- H2: the StallError salvage path returned EMPTY usage, so paid partial
  output was never billed/metered (spend under-count);
- H3: log_usage rounded cost to 6dp and cmd_usage rounded again before
  computing %, so many sub-microdollar calls aggregated as 0 spend and read
  as a fabricated ~100% saving;
- cmd_usage ignored the stored `est` flag, presenting estimated spend as
  exact;
- `ambient usage --json` lacked the contractual schema_version:1;
- locally-estimated usage never landed in body["usage"], so ask/code
  --json could emit usage:null while the ledger had an estimated record.

The savings receipt (the "~N% cheaper than a frontier model" tail) is now
OPT-IN and OFF by default, so the honesty tests that inspect that tail enable
it explicitly (AMBIENT_SAVINGS=on) — the properties they guard (served-model
pricing, sub-micro spend never reading as ~100% saved) only exist once the
comparison is shown. One test pins the default: OFF still prints the token
counts but no comparison tail.

Everything is offline: fake catalogs, tempdir ledgers, patched config/env.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v10rec", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v10rec", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()


@contextlib.contextmanager
def patched(obj, **attrs):
    old = {}
    missing = object()
    for k, v in attrs.items():
        old[k] = getattr(obj, k, missing)
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is missing:
                delattr(obj, k)
            else:
                setattr(obj, k, v)


@contextlib.contextmanager
def env_var(name, value):
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def fake_catalog():
    base = {"is_ready": True, "context_length": 128000,
            "max_output_length": 32000, "supported_features": [],
            "output_modalities": ["text"]}
    return [
        dict(base, id="cheap/model",
             pricing={"input": 0.2, "output": 0.8}),
        dict(base, id="pricey/model",
             pricing={"input": 10.0, "output": 50.0}),
    ]


REF = (3.0, 15.0)


def chat_args(**kw):
    base = dict(json=False, raw=False, allow_partial=False,
                max_tokens=256, temperature=0.1, timeout=30, fallback=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestH1ReceiptPricesServedModel(unittest.TestCase):
    """--fallback may switch to a PRICIER model mid-call: the receipt must
    price what actually served, never the cheap model that had no workers."""

    def test_chat_receipt_prices_served_model_not_requested(self):
        # Requested cheap/model (would read "saved 93%"); SERVED pricey/model
        # (costlier than the 3/15 reference → NO saving may be claimed).
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
        body = {"_served_model": "pricey/model", "finish_reason": "stop",
                "usage": usage}
        err = io.StringIO()
        # The saving tail is opt-in; enable it so the served-model pricing
        # property is actually rendered and can be asserted.
        with env_var("AMBIENT_SAVINGS", "on"), \
                patched(amb, _PRICING_CATALOG=fake_catalog(), _REF_CACHE=REF,
                        complete=lambda *a, **k: ("hi", usage, body)), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.chat("sk-test-key-x", "https://x", "cheap/model",
                     [{"role": "user", "content": "hi"}], chat_args())
        receipt = err.getvalue()
        self.assertIn("pricey/model", receipt)
        self.assertIn("costlier", receipt)
        self.assertNotIn("saved", receipt)

    def test_chat_receipt_unchanged_when_no_fallback(self):
        usage = {"prompt_tokens": 100_000, "completion_tokens": 10_000}
        body = {"_served_model": "cheap/model", "finish_reason": "stop",
                "usage": usage}
        err = io.StringIO()
        # Opt in to the saving tail so the honest claim is rendered.
        with env_var("AMBIENT_SAVINGS", "on"), \
                patched(amb, _PRICING_CATALOG=fake_catalog(), _REF_CACHE=REF,
                        complete=lambda *a, **k: ("hi", usage, body)), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.chat("sk-test-key-x", "https://x", "cheap/model",
                     [{"role": "user", "content": "hi"}], chat_args())
        receipt = err.getvalue()
        self.assertIn("cheap/model", receipt)
        self.assertIn("93% cheaper", receipt)

    def test_chat_receipt_off_by_default_has_no_saving_tail(self):
        """The saving comparison is opt-in: with AMBIENT_SAVINGS unset the
        receipt still reports the token counts, but carries NO
        '— cheaper/costlier than a frontier model' tail (which measures spend
        against a list price we picked, so it reads as a pitch, not a fact)."""
        usage = {"prompt_tokens": 100, "completion_tokens": 10}
        body = {"_served_model": "cheap/model", "finish_reason": "stop",
                "usage": usage}
        err = io.StringIO()
        # env cleared AND config pinned empty so the default (off) is
        # deterministic regardless of the developer's own machine.
        with env_var("AMBIENT_SAVINGS", None), \
                patched(amb, _PRICING_CATALOG=fake_catalog(), _REF_CACHE=REF,
                        read_config_file=lambda: {},
                        complete=lambda *a, **k: ("hi", usage, body)), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.chat("sk-test-key-x", "https://x", "cheap/model",
                     [{"role": "user", "content": "hi"}], chat_args())
        receipt = err.getvalue()
        # token counts still printed, tail closes right after "tokens"
        self.assertIn("[ambient cheap/model | in=100 out=10 tokens]", receipt)
        # no comparison tail of any kind
        self.assertNotIn(" — ", receipt)
        self.assertNotIn("cheaper", receipt)
        self.assertNotIn("costlier", receipt)

    def test_cmd_ask_receipt_gets_served_model(self):
        """cmd_ask's own render_result call must also pass the SERVED model."""
        usage = {"prompt_tokens": 1_000, "completion_tokens": 100}
        body = {"_served_model": "pricey/model", "finish_reason": "stop",
                "usage": usage}
        seen = {}

        def spy_render(text, partial, reason, args, api_key,
                       usage=None, model=None, already_streamed=False):
            seen["model"] = model

        args = chat_args(prompt=["hello", "there"], system=None,
                         model="cheap/model", allow_secrets=True)
        with patched(amb, safe_catalog=lambda *a: fake_catalog(),
                     complete=lambda *a, **k: ("hi", usage, body),
                     render_result=spy_render,
                     read_config_file=lambda: {}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_ask(args, "sk-test-key-x", "https://x", {})
        self.assertEqual(seen.get("model"), "pricey/model")


class TestH2SalvageIsMetered(unittest.TestCase):
    """Salvaged partial output was PAID FOR — it must land in the ledger as
    an estimated record, never silently under-count spend to 0."""

    def test_stall_salvage_meters_estimated_usage(self):
        d = tempfile.mkdtemp()
        up = os.path.join(d, "usage.jsonl")
        partial = "salvageable partial output " * 40  # > 400 chars

        def stall(*a, **k):
            raise amb.StallError("hit the wall", partial=partial,
                                 reasoning="", hard_wall=True)

        with patched(amb, USAGE_PATH=up, _PRICING_CATALOG=fake_catalog(),
                     _REF_CACHE=REF, stream_completion=stall), \
                contextlib.redirect_stderr(io.StringIO()):
            content, usage, body = amb.complete(
                "k", "https://x", "cheap/model",
                [{"role": "user", "content": "x" * 640}], chat_args())
        self.assertIn("PARTIAL", content)
        # returned usage is a real, marked estimate — not {}
        self.assertTrue(usage.get("_estimated"))
        self.assertGreater(usage.get("prompt_tokens", 0), 0)
        self.assertGreater(usage.get("completion_tokens", 0), 0)
        # the body reflects it too (--json consumers see the estimate)
        self.assertEqual(body.get("usage"), usage)
        self.assertTrue(body.get("salvaged_partial"))
        # a salvage in a fallback frame must still price the serving model
        self.assertEqual(body.get("_served_model"), "cheap/model")
        # and the ledger has the estimated record
        with open(up, encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["model"], "cheap/model")
        self.assertIs(rec.get("est"), True)
        self.assertGreater(rec["out"], 0)
        self.assertGreater(rec["in"], 0)


class TestH3SubMicroCostsNeverVanish(unittest.TestCase):
    """Sub-microdollar calls must aggregate to their TRUE nonzero total —
    rounding them to 0 fabricates a ~100% saving."""

    def test_log_usage_stores_full_precision_cost(self):
        d = tempfile.mkdtemp()
        up = os.path.join(d, "usage.jsonl")
        with patched(amb, USAGE_PATH=up, _PRICING_CATALOG=fake_catalog(),
                     _REF_CACHE=REF):
            # 1 input token on cheap/model = 2e-7 — round(…,6) flattens to 0
            amb.log_usage("cheap/model",
                          {"prompt_tokens": 1, "completion_tokens": 0})
        with open(up, encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertGreater(rec["cost"], 0)
        self.assertAlmostEqual(rec["cost"], 2e-7)

    def test_sub_micro_calls_sum_to_true_percent_not_100(self):
        now = int(time.time())
        n = 100
        records = [{"ts": now, "model": "cheap/model", "in": 1, "out": 0,
                    "cost": 2e-7, "ref": [3.0, 15.0]} for _ in range(n)]
        # The %-saving is opt-in; enable it so the sub-micro aggregation
        # (never rounding to a fabricated ~100%) is exercised.
        with env_var("AMBIENT_SAVINGS", "on"), usage_env(records, offline=True):
            out = run_usage(usage_args(json=True))
            text = run_usage(usage_args())
        data = json.loads(out)
        # The % is derived from RAW (never-rounded) totals, so sub-micro spend
        # can't fabricate a ~100% saving: 100×2e-7 vs frontier 100×3e-6 → 93%.
        self.assertEqual(data["saved_pct"], 93)
        self.assertNotIn("100%", text)
        self.assertNotIn("(99%)", text)
        self.assertIn("93%", text)
        self.assertNotIn("$", text)   # no dollar figures at all (founder policy)


class TestM1EstimatedRecordsSurfaced(unittest.TestCase):
    def test_estimated_counts_in_json_and_text(self):
        now = int(time.time())
        records = [
            {"ts": now, "model": "cheap/model", "in": 1000, "out": 100,
             "cost": 2.8e-4, "ref": [3.0, 15.0], "est": True},
            {"ts": now, "model": "cheap/model", "in": 1000, "out": 100,
             "cost": 2.8e-4, "ref": [3.0, 15.0]},
        ]
        with usage_env(records, offline=True):
            out = run_usage(usage_args(json=True))
            text = run_usage(usage_args())
        data = json.loads(out)
        self.assertEqual(data["est_records"], 1)
        self.assertEqual(data["models"][0]["est_records"], 1)
        self.assertIn("est.", text)
        self.assertIn("1 estimated", text)

    def test_no_estimated_records_no_marker(self):
        now = int(time.time())
        records = [{"ts": now, "model": "cheap/model", "in": 1000, "out": 100,
                    "cost": 2.8e-4, "ref": [3.0, 15.0]}]
        with usage_env(records, offline=True):
            out = run_usage(usage_args(json=True))
            text = run_usage(usage_args())
        data = json.loads(out)
        self.assertEqual(data["est_records"], 0)
        self.assertNotIn("estimated record", text)
        self.assertNotIn("est.]", text)


class TestM2UsageJsonSchemaVersion(unittest.TestCase):
    def test_schema_version_present_and_nothing_removed(self):
        now = int(time.time())
        records = [{"ts": now, "model": "cheap/model",
                    "in": 100_000, "out": 10_000,
                    "cost": 0.028, "ref": [3.0, 15.0]}]
        with usage_env(records, offline=True):
            out = run_usage(usage_args(json=True))
        data = json.loads(out)
        self.assertEqual(data["schema_version"], 1)
        for key in ("days", "models", "all_priced", "saved_pct",
                    "approx_ref_records", "unmetered_lanes", "note"):
            self.assertIn(key, data, key)
        # dollar + per-token-price fields are GONE (founder policy)
        for gone in ("total_est_cost", "reference_price", "frontier_cost",
                     "saved"):
            self.assertNotIn(gone, data, gone)


class TestM3EstimatedUsageInBody(unittest.TestCase):
    def test_body_usage_reflects_local_estimate(self):
        """A successful response missing its usage object gets a local
        estimate — body["usage"] must carry it so --json never says null
        while the ledger says otherwise."""
        d = tempfile.mkdtemp()
        up = os.path.join(d, "usage.jsonl")
        with patched(amb, USAGE_PATH=up, _PRICING_CATALOG=fake_catalog(),
                     _REF_CACHE=REF,
                     stream_completion=lambda *a, **k: (
                         200, {"content": "hello world answer",
                               "reasoning": "", "usage": None,
                               "finish_reason": "stop"})), \
                contextlib.redirect_stderr(io.StringIO()):
            _c, usage, body = amb.complete(
                "k", "https://x", "cheap/model",
                [{"role": "user", "content": "hi there"}], chat_args())
        self.assertTrue(usage.get("_estimated"))
        self.assertEqual(body.get("usage"), usage)


def usage_args(**kw):
    base = dict(days=30, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


@contextlib.contextmanager
def usage_env(records, catalog=None, offline=False):
    d = tempfile.mkdtemp()
    up = os.path.join(d, "usage.jsonl")
    with open(up, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")

    def fetch(_url, _key):
        if offline:
            raise amb.NetworkError("offline")
        return catalog if catalog is not None else fake_catalog()

    with env_var("AMBIENT_REFERENCE_PRICE", None), \
            patched(amb, USAGE_PATH=up, read_config_file=lambda: {},
                    resolve_api_url=lambda conf: "https://api.ambient.xyz",
                    fetch_models=fetch, _REF_CACHE=None):
        yield


def run_usage(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        amb.cmd_usage(args)
    return out.getvalue()


if __name__ == "__main__":
    unittest.main()
