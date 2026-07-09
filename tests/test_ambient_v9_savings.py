"""Hermetic tests — savings receipts.

The tool's core value proposition (10-40x cheaper than frontier) was invisible
in daily use: no dollar figure on the post-run receipt, no savings column in
`ambient usage`, and the agent lane silently under-counted. adds all
three, HONESTLY:

- per-run receipt: run cost from catalog pricing + a frontier-equivalent
  figure (AMBIENT_REFERENCE_PRICE, env > config > documented default) with a
  floored saved-% — unknown/assumed pricing NEVER fabricates a saving, and
  estimated token counts are labeled "(est.)";
- every log_usage record stores the run cost + the reference price in force,
  so historical savings stay accurate if the default changes later (old
  records without the fields still parse);
- `ambient usage` shows per-model + overall relative SAVINGS as a floored %
  (no dollar figures) — a pricier-than-reference model shows "costlier", never
  a fake saving; records lacking a stored ref fall back to the current one
  with an explicit approximation note; unpriced records claim NO saving;
- the agent lane (external opencode process) is genuinely unmeterable from
  here, so `ambient usage` DISCLOSES it instead of pretending totals are
  complete.

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
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v9sav", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v9sav", loader)
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
        dict(base, id="zero/model",
             pricing={"input": 0, "output": 0}),
    ]


REF = (3.0, 15.0)  # test reference: 3/Mtok in, 15/Mtok out


class TestReferencePriceParsing(unittest.TestCase):
    def test_in_out_pair(self):
        self.assertEqual(amb.parse_reference_price("3/15"), (3.0, 15.0))

    def test_single_blended(self):
        self.assertEqual(amb.parse_reference_price(" 10 "), (10.0, 10.0))

    def test_junk_returns_none(self):
        for raw in ("", "abc", "3/15/2", "-1", "0", "0/15", "3/-15",
                    "nan", "inf", "3/inf", None, 7):
            self.assertIsNone(amb.parse_reference_price(raw), raw)

    def test_default_is_a_sane_frontier_pair(self):
        d = amb.REFERENCE_PRICE_DEFAULT
        self.assertEqual(len(d), 2)
        self.assertTrue(0 < d[0] < d[1] < 1000)

    def test_env_beats_config(self):
        with env_var("AMBIENT_REFERENCE_PRICE", "4/20"):
            self.assertEqual(
                amb.resolve_reference_price({"AMBIENT_REFERENCE_PRICE": "9"}),
                (4.0, 20.0))

    def test_config_used_without_env(self):
        with env_var("AMBIENT_REFERENCE_PRICE", None):
            self.assertEqual(
                amb.resolve_reference_price({"AMBIENT_REFERENCE_PRICE": "9"}),
                (9.0, 9.0))

    def test_junk_falls_back_to_default(self):
        with env_var("AMBIENT_REFERENCE_PRICE", "total garbage"):
            self.assertEqual(amb.resolve_reference_price({}),
                             amb.REFERENCE_PRICE_DEFAULT)


class TestRunCostMath(unittest.TestCase):
    def test_cost_from_tokens_times_pricing(self):
        cost, assumed = amb.usage_cost(
            "cheap/model",
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            catalog=fake_catalog())
        self.assertFalse(assumed)
        self.assertAlmostEqual(cost, 1.0)  # 0.2 + 0.8

    def test_unknown_model_is_assumed_worst_case(self):
        cost, assumed = amb.usage_cost(
            "mystery/model",
            {"prompt_tokens": 1_000_000, "completion_tokens": 0},
            catalog=fake_catalog())
        self.assertTrue(assumed)
        self.assertAlmostEqual(cost, amb.ASSUMED_MAX_INPUT_PRICE)

    def test_zero_priced_model_is_assumed_not_free(self):
        _cost, assumed = amb.usage_cost(
            "zero/model", {"prompt_tokens": 10, "completion_tokens": 10},
            catalog=fake_catalog())
        self.assertTrue(assumed)

    def test_reference_cost(self):
        self.assertAlmostEqual(
            amb.reference_cost(
                {"prompt_tokens": 100_000, "completion_tokens": 10_000}, REF),
            0.45)


class TestSavingsNote(unittest.TestCase):
    """The stderr receipt suffix — the honesty rules live here."""

    def note(self, model, usage, catalog=None):
        # Savings receipts are opt-in (default OFF); this suite exercises the ON
        # path, so the conf must enable it. Clear any inherited AMBIENT_SAVINGS
        # so the conf value — not a polluted env — decides.
        with env_var("AMBIENT_SAVINGS", None):
            return amb.savings_note(
                model, usage,
                catalog=fake_catalog() if catalog is None else catalog,
                conf={"AMBIENT_SAVINGS": "on", "AMBIENT_REFERENCE_PRICE": "3/15"})

    def test_cost_frontier_and_floored_saved_pct(self):
        with env_var("AMBIENT_REFERENCE_PRICE", None):
            note = self.note(
                "cheap/model",
                {"prompt_tokens": 100_000, "completion_tokens": 10_000})
        # saved = 93.77% → floored to 93; no dollar figure is shown (billing
        # is plan-dependent), only the relative saving vs the frontier.
        self.assertIn("93% cheaper", note)
        self.assertIn("frontier", note)
        self.assertNotIn("$", note)

    def test_assumed_pricing_never_fabricates_a_saving(self):
        with env_var("AMBIENT_REFERENCE_PRICE", None):
            note = self.note(
                "mystery/model",
                {"prompt_tokens": 100_000, "completion_tokens": 0})
        # unknown real price → no comparison can be made, so no note at all.
        self.assertEqual(note, "")

    def test_estimated_usage_is_labeled(self):
        with env_var("AMBIENT_REFERENCE_PRICE", None):
            note = self.note(
                "cheap/model",
                {"prompt_tokens": 100_000, "completion_tokens": 10_000,
                 "_estimated": True})
        self.assertIn("(est.)", note)
        self.assertIn("93% cheaper", note)

    def test_pricier_than_reference_shows_costlier_not_a_saving(self):
        with env_var("AMBIENT_REFERENCE_PRICE", None):
            note = self.note(
                "pricey/model",
                {"prompt_tokens": 1_000_000, "completion_tokens": 0})
        self.assertIn("costlier", note)
        self.assertNotIn("saved", note)

    def test_zero_tokens_no_claim(self):
        with env_var("AMBIENT_REFERENCE_PRICE", None):
            self.assertEqual(
                self.note("cheap/model",
                          {"prompt_tokens": 0, "completion_tokens": 0}), "")

    def test_receipt_line_carries_the_note(self):
        args = argparse.Namespace(allow_partial=False)
        err = io.StringIO()
        # render_result resolves savings from config/env (it takes no conf),
        # so the opt-in has to come from the environment here.
        with env_var("AMBIENT_SAVINGS", "on"), \
                patched(amb, _PRICING_CATALOG=fake_catalog(),
                        _REF_CACHE=REF), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.render_result(
                "hi", False, None, args, "sk-test-key-x",
                usage={"prompt_tokens": 100_000, "completion_tokens": 10_000},
                model="cheap/model")
        receipt = err.getvalue()
        self.assertIn("in=100000", receipt)   # token counts kept
        self.assertIn("out=10000", receipt)
        self.assertIn("93% cheaper", receipt)
        self.assertNotIn("$", receipt)


class TestLedgerRecords(unittest.TestCase):
    def test_record_stores_cost_ref_and_est(self):
        d = tempfile.mkdtemp()
        up = os.path.join(d, "usage.jsonl")
        with patched(amb, USAGE_PATH=up, _PRICING_CATALOG=fake_catalog(),
                     _REF_CACHE=REF):
            amb.log_usage("cheap/model",
                          {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                           "_estimated": True})
        with open(up, encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertAlmostEqual(rec["cost"], 0.2)
        self.assertEqual(rec["ref"], [3.0, 15.0])
        self.assertIs(rec["est"], True)
        self.assertEqual(rec["in"], 1_000_000)

    def test_unpriced_model_stores_no_cost_but_keeps_ref(self):
        d = tempfile.mkdtemp()
        up = os.path.join(d, "usage.jsonl")
        with patched(amb, USAGE_PATH=up, _PRICING_CATALOG=[],
                     _REF_CACHE=REF):
            amb.log_usage("mystery/model",
                          {"prompt_tokens": 100, "completion_tokens": 5})
        with open(up, encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertNotIn("cost", rec)   # an assumed cost is not a real cost
        self.assertEqual(rec["ref"], [3.0, 15.0])
        self.assertNotIn("est", rec)

    def test_salvage_estimated_usage_is_metered_and_marked(self):
        """6d: the no-usage-object salvage path lands in the ledger AND the
        record carries the estimate marker."""
        d = tempfile.mkdtemp()
        up = os.path.join(d, "usage.jsonl")
        with patched(amb, USAGE_PATH=up, _PRICING_CATALOG=fake_catalog(),
                     _REF_CACHE=REF,
                     stream_completion=lambda *a, **k: (
                         200, {"content": "hello world answer",
                               "reasoning": "", "usage": None,
                               "finish_reason": "stop"})):
            args = argparse.Namespace(max_tokens=256, temperature=0.1,
                                      timeout=30, fallback=False)
            _c, usage, _b = amb.complete(
                "k", "https://x", "cheap/model",
                [{"role": "user", "content": "hi there"}], args)
        self.assertTrue(usage.get("_estimated"))
        with open(up, encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertIs(rec.get("est"), True)
        self.assertIn("ref", rec)


def usage_args(**kw):
    base = dict(days=30, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


@contextlib.contextmanager
def usage_env(records, catalog=None, offline=False, savings=True):
    """cmd_usage sandbox: seeded tempdir ledger, no network, no real config.
    Savings is opt-in (default OFF in the tool), so this sandbox forces
    AMBIENT_SAVINGS on for the savings-column tests; pass savings=False to
    assert the default-off behavior."""
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
            env_var("AMBIENT_SAVINGS", "on" if savings else None), \
            patched(amb, USAGE_PATH=up, read_config_file=lambda: {},
                    resolve_api_url=lambda conf: "https://api.ambient.xyz",
                    fetch_models=fetch, _REF_CACHE=None):
        yield


def run_usage(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        amb.cmd_usage(args)
    return out.getvalue()


class TestUsageSavings(unittest.TestCase):
    def test_stored_ref_beats_current_default(self):
        """Historical honesty: an old record keeps the reference it was
        billed against; only ref-less records use the current default. The
        RELATIVE % reflects both (no dollar figures — founder policy)."""
        now = int(time.time())
        records = [
            # new-style record with its own stored ref (1/1) + cost
            {"ts": now, "model": "m/x", "in": 1_000_000, "out": 0,
             "cost": 0.1, "ref": [1.0, 1.0]},
            # old-style record (pre-): no cost, no ref
            {"ts": now, "model": "m/x", "in": 1_000_000, "out": 0},
        ]
        cat = [dict(fake_catalog()[0], id="m/x",
                    pricing={"input": 0.1, "output": 0.4})]
        with usage_env(records, catalog=cat), \
                env_var("AMBIENT_REFERENCE_PRICE", "2/2"):
            out = run_usage(usage_args(json=True))
        data = json.loads(out)
        row = data["models"][0]
        # frontier = 1Mtok@1 (stored) + 1Mtok@2 (current default) = 3;
        # cost = stored 0.1 + recomputed 1Mtok@0.1 = 0.2 → saved 2.8 → 93%.
        self.assertEqual(row["saved_pct"], 93)
        self.assertEqual(data["approx_ref_records"], 1)
        # NO dollar fields, and NO per-token reference price, in --json.
        for gone in ("frontier_cost", "est_cost", "saved", "reference_price"):
            self.assertNotIn(gone, {**row, **data}, gone)

    def test_json_reports_percent_savings_not_dollars(self):
        now = int(time.time())
        records = [{"ts": now, "model": "cheap/model",
                    "in": 100_000, "out": 10_000,
                    "cost": 0.028, "ref": [3.0, 15.0]}]
        with usage_env(records, offline=True):
            out = run_usage(usage_args(json=True))
        data = json.loads(out)
        # KEEPS relative % + structural fields...
        for key in ("saved_pct", "unmetered_lanes", "all_priced"):
            self.assertIn(key, data, key)
        self.assertEqual(data["unmetered_lanes"], ["agent"])
        self.assertEqual(data["saved_pct"], 93)
        self.assertEqual(data["models"][0]["saved_pct"], 93)
        # ...and NEVER a dollar figure or per-token price (founder policy).
        # Match JSON key form ("saved": ...) so it doesn't collide with the
        # kept "saved_pct".
        blob = json.dumps(data)
        self.assertNotIn("$", blob)
        for gone in ("est_cost", "frontier_cost", "total_est_cost",
                     "reference_price", "saved"):
            self.assertNotIn(f'"{gone}":', blob, gone)

    def test_costlier_model_never_shows_a_fake_saving(self):
        now = int(time.time())
        records = [{"ts": now, "model": "pricey/model",
                    "in": 1_000_000, "out": 0,
                    "cost": 10.0, "ref": [3.0, 15.0]}]
        with usage_env(records, offline=True):
            text = run_usage(usage_args())
            out = run_usage(usage_args(json=True))
        self.assertIn("costlier", text)
        self.assertNotIn("$", text)
        # A pricier-than-frontier model claims NO saving: saved_pct is None,
        # never a fabricated positive.
        self.assertIsNone(json.loads(out)["models"][0]["saved_pct"])

    def test_unpriced_records_claim_no_saving(self):
        now = int(time.time())
        records = [{"ts": now, "model": "mystery/x", "in": 500, "out": 500}]
        with usage_env(records, offline=True):
            out = run_usage(usage_args(json=True))
        data = json.loads(out)
        row = data["models"][0]
        self.assertIsNone(row["saved_pct"])
        self.assertIs(row["cost_partial"], True)
        self.assertFalse(data["all_priced"])
        self.assertIsNone(data["saved_pct"])  # totals exclude unknowns

    def test_partial_model_counts_but_claims_no_saving(self):
        """A model with one priced + one unpriced record is flagged partial and
        claims NO saving; known spend still binds the gate internally (verified
        elsewhere), but no dollar figure is ever surfaced."""
        now = int(time.time())
        records = [
            {"ts": now, "model": "mystery/x", "in": 100_000, "out": 0,
             "cost": 0.20, "ref": [3.0, 15.0]},   # priced (stored cost)
            {"ts": now, "model": "mystery/x", "in": 500, "out": 500},
        ]
        with usage_env(records, offline=True):
            out = run_usage(usage_args(json=True))
            text = run_usage(usage_args())
        data = json.loads(out)
        row = data["models"][0]
        # Row: flagged partial, no savings math, no dollar fields.
        self.assertIs(row["cost_partial"], True)
        self.assertIsNone(row["saved_pct"])
        self.assertFalse(data["all_priced"])
        # Text: marked partial, no dollar figure anywhere.
        self.assertIn("partial", text)
        self.assertIn("partial — some records unpriced", text)
        overall = next(ln for ln in text.splitlines()
                       if ln.startswith("Overall"))
        self.assertIn("unpriced", overall)
        self.assertNotIn("$", text)

    def test_partial_model_does_not_pollute_fully_priced_savings(self):
        """The % saving must reflect ONLY fully-priced models; a partial model
        contributes nothing to the savings comparison."""
        now = int(time.time())
        records = [
            {"ts": now, "model": "cheap/model", "in": 100_000, "out": 10_000,
             "cost": 0.028, "ref": [3.0, 15.0]},   # fully priced → 93%
            {"ts": now, "model": "mystery/x", "in": 100_000, "out": 0,
             "cost": 0.20, "ref": [3.0, 15.0]},    # priced record...
            {"ts": now, "model": "mystery/x", "in": 500, "out": 500},
        ]
        with usage_env(records, offline=True):
            out = run_usage(usage_args(json=True))
        data = json.loads(out)
        rows = {r["model"]: r for r in data["models"]}
        self.assertIs(rows["cheap/model"]["cost_partial"], False)
        self.assertEqual(rows["cheap/model"]["saved_pct"], 93)
        self.assertIs(rows["mystery/x"]["cost_partial"], True)
        self.assertIsNone(rows["mystery/x"]["saved_pct"])
        # overall % over the fully-priced subset ONLY (0.422 saved / 0.45)
        self.assertEqual(data["saved_pct"], 93)
        self.assertFalse(data["all_priced"])

    def test_fully_priced_set_reports_percent(self):
        now = int(time.time())
        records = [{"ts": now, "model": "cheap/model",
                    "in": 100_000, "out": 10_000,
                    "cost": 0.028, "ref": [3.0, 15.0]}]
        with usage_env(records, offline=True):
            out = run_usage(usage_args(json=True))
        data = json.loads(out)
        row = data["models"][0]
        self.assertIs(row["cost_partial"], False)
        self.assertEqual(row["saved_pct"], 93)
        self.assertEqual(data["saved_pct"], 93)
        self.assertTrue(data["all_priced"])

    def test_text_output_has_savings_column_and_total(self):
        now = int(time.time())
        records = [{"ts": now, "model": "cheap/model",
                    "in": 100_000, "out": 10_000,
                    "cost": 0.028, "ref": [3.0, 15.0]}]
        with usage_env(records, offline=True):
            text = run_usage(usage_args())
        self.assertIn("cheaper", text)
        self.assertIn("frontier", text)
        self.assertIn("93%", text)
        self.assertNotIn("$", text)

    def test_agent_lane_unmetered_disclosure(self):
        now = int(time.time())
        records = [{"ts": now, "model": "cheap/model", "in": 10, "out": 10}]
        with usage_env(records, offline=True):
            text = run_usage(usage_args())
        self.assertIn("ambient agent", text)
        self.assertIn("not visible to local metering", text)

    def test_old_ledger_shape_still_parses(self):
        now = int(time.time())
        records = [{"ts": now, "model": "cheap/model", "in": 100, "out": 50}]
        with usage_env(records):  # online: live pricing covers old records
            out = run_usage(usage_args(json=True))
        data = json.loads(out)
        self.assertTrue(data["all_priced"])
        # old record priced online → a real relative saving, no dollar fields
        self.assertIsNotNone(data["models"][0]["saved_pct"])


class TestSavingsIsOptIn(unittest.TestCase):
    """The vs-frontier saving compares real spend against a list price WE chose,
    so it reads as a pitch and is OFF by default; it must stay silent until the
    user opts in. Every case pins BOTH the default (off) and the opt-in (on) for
    the SAME inputs, so a regression that flips the default cannot hide behind an
    input that happens to produce no saving."""

    FULLY_PRICED = {"prompt_tokens": 100_000, "completion_tokens": 10_000}

    def test_savings_note_silent_until_opted_in(self):
        # Identical fully-priced call that WOULD report a saving: silent by
        # default, "cheaper" once opted in.
        with env_var("AMBIENT_SAVINGS", None), \
                env_var("AMBIENT_REFERENCE_PRICE", None):
            off = amb.savings_note("cheap/model", self.FULLY_PRICED,
                                   catalog=fake_catalog(), conf={})
            on = amb.savings_note("cheap/model", self.FULLY_PRICED,
                                  catalog=fake_catalog(),
                                  conf={"AMBIENT_SAVINGS": "on"})
        self.assertEqual(off, "")
        self.assertIn("cheaper", on)

    def test_savings_note_by_served_silent_until_opted_in(self):
        by_model = {"cheap/model": self.FULLY_PRICED}
        with env_var("AMBIENT_SAVINGS", None), \
                env_var("AMBIENT_REFERENCE_PRICE", None):
            off = amb.savings_note_by_served(by_model, catalog=fake_catalog(),
                                             conf={})
            on = amb.savings_note_by_served(by_model, catalog=fake_catalog(),
                                            conf={"AMBIENT_SAVINGS": "on"})
        self.assertEqual(off, "")
        self.assertIn("cheaper", on)

    def test_usage_text_has_no_comparison_when_off(self):
        now = int(time.time())
        records = [{"ts": now, "model": "cheap/model",
                    "in": 100_000, "out": 10_000,
                    "cost": 0.028, "ref": [3.0, 15.0]}]
        with usage_env(records, offline=True, savings=False):
            text = run_usage(usage_args())
        # No relative-saving language at all, and the total line is plain.
        self.assertNotIn("cheaper", text)
        self.assertNotIn("costlier", text)
        self.assertNotIn("%", text)
        total = next(ln for ln in text.splitlines()
                     if ln.startswith("Overall"))
        self.assertEqual(total, "Overall usage summary")

    def test_usage_json_savings_flag_tracks_opt_in(self):
        now = int(time.time())
        # Ref-less record: approx_ref WOULD count it (1) when savings is on, so
        # 0-when-off proves the field is gated, not merely absent by input.
        records = [{"ts": now, "model": "cheap/model",
                    "in": 100_000, "out": 10_000, "cost": 0.028}]
        with usage_env(records, offline=True, savings=False):
            off = json.loads(run_usage(usage_args(json=True)))
        with usage_env(records, offline=True, savings=True):
            on = json.loads(run_usage(usage_args(json=True)))
        self.assertIs(off["savings"], False)
        self.assertIsNone(off["saved_pct"])
        self.assertEqual(off["approx_ref_records"], 0)
        self.assertIs(on["savings"], True)
        self.assertIsNotNone(on["saved_pct"])   # fully-priced ledger
        self.assertEqual(on["approx_ref_records"], 1)

    def test_config_settings_lists_savings_off_by_default(self):
        by_name = {s["name"]: s for s in amb.CONFIG_SETTINGS}
        self.assertIn("savings", by_name)
        self.assertEqual(by_name["savings"]["default"], "off")


if __name__ == "__main__":
    unittest.main()
