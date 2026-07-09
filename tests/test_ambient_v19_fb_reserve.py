"""Hermetic tests: the fully-conservative fallback reserve.

The up-front batch gates reserve the PER-CALL SUM-OF-MAXIMA —
Sum_i max(cost_requested(call_i), cost_alt(call_i)) — which dominates EVERY
requested/fallback mixture across the batch. Three findings closed:

H1: cost_alt prices the ALT at the ALT's own resolved output budget (the
    exact alt_tokens re-derivation live complete() performs: auto-budget ->
    alt_profile.output_budget; explicit --max-tokens -> min(explicit, alt
    cap)) — never at the requested model's (possibly tiny) max_tokens.
H2: uneven lanes (map's per-item inputs) pick each ITEM's own fallback
    candidate from that item's REAL size — a big item live-switches to a
    large-context, pricier candidate an average-input pick would miss.
M:  max(sum_requested, sum_alt) does NOT dominate a mixture with uneven
    inputs + crossed price vectors; the per-call sum-of-maxima does.

PARITY: fallback off / SACRED _no_fallback / no pricier candidate ->
figures (and the printed gate line) stay byte-identical to estimate_cost.

No network, no live API, no writes outside tempdirs.
"""
import argparse
import contextlib
import io
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")

KEY = "sk-test-key-abcdef1234567890"


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v19", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v19", loader)
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


def ns(**kw):
    base = dict(max_tokens=8000, temperature=0.1, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=False,
                yes=True, no_cache=True, cache_ttl=None, model=None,
                parallel=None, json=False, system=None,
                escalation_ceiling=30000, _auto_budget=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _mdl(mid, ctx, max_out, ready, price_in, price_out):
    return {"id": mid, "context_length": ctx, "max_output_length": max_out,
            "is_ready": ready, "supported_features": [],
            "output_modalities": ["text"],
            "pricing": {"input": price_in, "output": price_out}}


def smallcap_catalog():
    """Requested model with a TINY output cap (auto budget resolves ~2000)
    + a ready fallback candidate whose own profile budget is far bigger."""
    return [_mdl("tiny/cap", 120_000, 2000, False, 0.1, 0.4),
            _mdl("alt/big", 300_000, 60_000, True, 1.0, 40.0)]


def two_alt_catalog():
    """Uneven-lane fixture: a cheap requested model, a CHEAP small-context
    candidate (fits only small items) and a PRICIER large-context one that
    only a big item's own sizing selects."""
    return [_mdl("req/model", 200_000, 60_000, True, 0.1, 0.4),
            _mdl("small/alt", 3_000, 60_000, True, 0.2, 0.8),
            _mdl("big/alt", 400_000, 60_000, True, 2.0, 30.0)]


def crossed_catalog():
    """Crossed price vectors: requested is input-heavy (5.0/1.0), the alt
    is output-heavy (0.5/20.0) — the mixture edge case's shape."""
    return [_mdl("req/x", 200_000, 60_000, False, 5.0, 1.0),
            _mdl("alt/x", 400_000, 60_000, True, 0.5, 20.0)]


def cheaper_alt_catalog():
    return [_mdl("mid/model", 120_000, 60_000, True, 2.0, 10.0),
            _mdl("cheap/alt", 300_000, 60_000, True, 0.1, 0.4)]


# --------------------------------------------------------------------------
# (a) H1 — the alt is priced at ITS OWN resolved output budget
# --------------------------------------------------------------------------

class TestAltOwnOutputBudget(unittest.TestCase):
    def test_auto_budget_reserves_alt_at_its_bigger_own_budget(self):
        """Auto-budget on a small-cap requested model: live complete()
        resends alt_profile.output_budget — the reserve must cover that,
        not the requested model's 2000-token cap."""
        cat = smallcap_catalog()
        alt_budget = amb.model_profile(cat, "alt/big").output_budget
        self.assertGreater(alt_budget, 2000,
                           "fixture: the alt's own budget must exceed the "
                           "requested model's cap for this test to bite")
        n, chars, req_mt = 6, 12_000, 2000  # small-cap resolved auto budget
        with env_var("AMBIENT_FALLBACK", None):
            fb = amb.estimate_cost_fb(
                cat, "tiny/cap", chars, n, req_mt,
                ns(fallback=True, _auto_budget=True, max_tokens=req_mt), {})
        live = amb.estimate_cost(cat, "alt/big", chars, n, alt_budget)
        old = amb.estimate_cost(cat, "alt/big", chars, n, req_mt)
        # >= what an all-fallback live run is estimated to spend at the
        # ALT's own resend budget…
        self.assertGreaterEqual(fb[0], live[0])
        self.assertGreaterEqual(fb[1], live[1])
        # …which is strictly more than the R2 figure (alt at requested cap).
        self.assertGreater(fb[0], old[0])
        self.assertGreater(fb[1], old[1])

    def test_explicit_budget_prices_alt_at_the_explicit_value(self):
        """Explicit --max-tokens mirrors live: min(explicit, alt cap)."""
        cat = smallcap_catalog()
        n, chars, req_mt = 6, 12_000, 2000
        with env_var("AMBIENT_FALLBACK", None):
            fb_exp = amb.estimate_cost_fb(
                cat, "tiny/cap", chars, n, req_mt,
                ns(fallback=True, _auto_budget=False, max_tokens=req_mt), {})
            fb_auto = amb.estimate_cost_fb(
                cat, "tiny/cap", chars, n, req_mt,
                ns(fallback=True, _auto_budget=True, max_tokens=req_mt), {})
        want = amb.estimate_cost(cat, "alt/big", chars, n, req_mt)
        self.assertAlmostEqual(fb_exp[0], want[0], places=12)
        self.assertAlmostEqual(fb_exp[1], want[1], places=12)
        self.assertLess(fb_exp[1], fb_auto[1],
                        "auto-budget must reserve the alt's BIGGER budget")

    def test_map_reduce_auto_budget_reserves_alt_own_budget(self):
        """The mr gate inherits H1: both lanes price the alt's own budget."""
        cat = smallcap_catalog()
        with env_var("AMBIENT_FALLBACK", None):
            fb = amb.estimate_cost_mr_fb(
                cat, "tiny/cap", None, 60_000, 4, 2000,
                ns(fallback=True, _auto_budget=True, max_tokens=2000), {})
        old = amb.estimate_cost_mr(cat, "alt/big", None, 60_000, 4, 2000)
        self.assertGreater(fb[0], old[0])
        self.assertGreater(fb[1], old[1])


# --------------------------------------------------------------------------
# (b) H2 — uneven lanes pick each item's OWN candidate from its real size
# --------------------------------------------------------------------------

class TestPerItemCandidate(unittest.TestCase):
    SIZES = [20_000, 200, 200, 200]

    def test_batch_alt_pick_is_per_size(self):
        """Sanity on the fixture: the big item's sizing selects the pricier
        large-context candidate; small items select the cheap one; the
        AVERAGE input would select the cheap one for everything."""
        cat = two_alt_catalog()
        a = ns(fallback=True)
        with env_var("AMBIENT_FALLBACK", None):
            self.assertEqual(
                amb._batch_fallback_alt(cat, "req/model", a, {}, 20_000),
                "big/alt")
            self.assertEqual(
                amb._batch_fallback_alt(cat, "req/model", a, {}, 200),
                "small/alt")
            avg = sum(self.SIZES) / len(self.SIZES)
            self.assertEqual(
                amb._batch_fallback_alt(cat, "req/model", a, {}, avg),
                "small/alt")

    def test_uneven_batch_reserves_the_big_items_pricier_candidate(self):
        cat = two_alt_catalog()
        total, mt = sum(self.SIZES), 8000
        with env_var("AMBIENT_FALLBACK", None):
            fb = amb.estimate_cost_fb(
                cat, "req/model", total, len(self.SIZES), mt,
                ns(fallback=True), {}, per_call_chars=self.SIZES,
                per_call_tokens=[mt] * len(self.SIZES))
        base = amb.estimate_cost(cat, "req/model", total, len(self.SIZES),
                                 mt)
        r_big = amb._fb_call_cost(cat, "req/model", 20_000, mt, 1.3)
        a_big = amb._fb_call_cost(cat, "big/alt", 20_000, mt, 1.3)
        # the reserve must at least carry the big item's pricier candidate
        self.assertGreaterEqual(fb[0] + 1e-12,
                                base[0] + (a_big[0] - r_big[0]))
        self.assertGreaterEqual(fb[1] + 1e-12,
                                base[1] + (a_big[1] - r_big[1]))
        # …which is strictly more than an average-input pick could reserve
        # (every call at the cheap small-context candidate).
        avg_alt = amb.estimate_cost(cat, "small/alt", total,
                                    len(self.SIZES), mt)
        self.assertGreater(fb[0], max(base[0], avg_alt[0]))
        self.assertGreater(fb[1], max(base[1], avg_alt[1]))

    def _map_args(self, paths, **kw):
        base = dict(prompt="summarize", paths=paths, jsonl=False, json=True,
                    allow_secrets=False, model="req/model", system=None,
                    max_tokens=None, temperature=0.1, timeout=30, raw=False,
                    fallback=True, allow_partial=False, allow_cost=False,
                    yes=True, no_cache=True, cache_ttl=None, parallel=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def _run_map(self, ceiling, fallback):
        cat = two_alt_catalog()
        calls = []

        def spy_complete(api_key, api_url, model, messages, args,
                         session=None, **kw):
            calls.append(model)
            return "out", None, {"finish_reason": "stop"}

        raised = False
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i, size in enumerate(self.SIZES):
                p = os.path.join(d, f"item{i}.txt")
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("word " * (size // 5))
                paths.append(p)
            args = self._map_args(paths, fallback=fallback)
            with patched(amb, safe_catalog=lambda *a, **k: cat,
                         read_config_file=lambda: {},
                         complete=spy_complete), \
                    env_var("AMBIENT_MAX_SPEND", ceiling), \
                    env_var("AMBIENT_FLEET_BUDGET", "off"), \
                    env_var("AMBIENT_FALLBACK", None), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    amb.cmd_map(args, KEY, "https://x", {})
                except SystemExit:
                    raised = True
        return raised, calls

    def test_uneven_map_batch_refused_up_front_at_the_ceiling(self):
        """End-to-end: the huge item's pricier per-item candidate pushes the
        reserve over the ceiling — refused BEFORE any network call (the old
        average-input candidate priced ~0.02 and would have let it run)."""
        raised, calls = self._run_map("0.05", fallback=True)
        self.assertTrue(raised, "the fallback-aware reserve must refuse")
        self.assertEqual(calls, [], "refusal must precede ANY billed call")

    def test_same_uneven_map_batch_passes_without_fallback(self):
        """Control: without --fallback the same batch is pennies and runs."""
        raised, calls = self._run_map("0.05", fallback=False)
        self.assertFalse(raised)
        self.assertEqual(len(calls), len(self.SIZES))


# --------------------------------------------------------------------------
# (c) M — the per-call sum-of-maxima dominates EVERY mixture
# --------------------------------------------------------------------------

class TestMixtureDomination(unittest.TestCase):
    def test_reserve_dominates_all_mixtures_and_beats_max_of_totals(self):
        cat = crossed_catalog()
        sizes = [100_000, 1000, 1000, 1000]
        total, mt = sum(sizes), 8000
        a = ns(fallback=True)
        with env_var("AMBIENT_FALLBACK", None):
            fb = amb.estimate_cost_fb(
                cat, "req/x", total, len(sizes), mt, a, {},
                per_call_chars=sizes, per_call_tokens=[mt] * len(sizes))
            alts = [amb._batch_fallback_alt(cat, "req/x", a, {}, s)
                    for s in sizes]
        self.assertTrue(all(x == "alt/x" for x in alts))
        req = [amb._fb_call_cost(cat, "req/x", s, mt, 1.3) for s in sizes]
        alt = [amb._fb_call_cost(cat, "alt/x", s,
                                 amb._fb_alt_budget(cat, "alt/x", mt, False),
                                 1.3) for s in sizes]
        worst_exp = worst_bnd = 0.0
        for mask in range(2 ** len(sizes)):
            mix_exp = sum((alt if mask >> i & 1 else req)[i][0]
                          for i in range(len(sizes)))
            mix_bnd = sum((alt if mask >> i & 1 else req)[i][1]
                          for i in range(len(sizes)))
            self.assertGreaterEqual(
                fb[0] + 1e-9, mix_exp,
                f"mixture {mask:04b} exceeds the expected reserve")
            self.assertGreaterEqual(
                fb[1] + 1e-9, mix_bnd,
                f"mixture {mask:04b} exceeds the bound reserve")
            worst_exp = max(worst_exp, mix_exp)
            worst_bnd = max(worst_bnd, mix_bnd)
        # the edge case is REAL: the worst mixture exceeds
        # max(all-requested, all-alt) on these crossed price vectors…
        sum_req = amb.estimate_cost(cat, "req/x", total, len(sizes), mt)
        sum_alt = amb.estimate_cost(cat, "alt/x", total, len(sizes), mt)
        self.assertGreater(worst_exp, max(sum_req[0], sum_alt[0]))
        self.assertGreater(worst_bnd, max(sum_req[1], sum_alt[1]))
        # …and the new reserve covers it.
        self.assertGreaterEqual(fb[0] + 1e-9, worst_exp)
        self.assertGreaterEqual(fb[1] + 1e-9, worst_bnd)

    def test_uniform_reserve_still_covers_the_all_alt_total_exactly(self):
        """Uniform lane sanity: with a pricier alt the sum-of-maxima equals
        the all-alt total — never a hair under it (fleet-record exactness
        the v18 reservation test also pins)."""
        cat = crossed_catalog()
        with env_var("AMBIENT_FALLBACK", None):
            fb = amb.estimate_cost_fb(cat, "req/x", 8000, 8, 8000,
                                      ns(fallback=True), {})
        all_alt = amb.estimate_cost(cat, "alt/x", 8000, 8, 8000)
        sum_req = amb.estimate_cost(cat, "req/x", 8000, 8, 8000)
        self.assertGreaterEqual(fb[0], max(all_alt[0], sum_req[0]))
        self.assertGreaterEqual(fb[1], max(all_alt[1], sum_req[1]))


# --------------------------------------------------------------------------
# (d) PARITY — off / cheaper alt / SACRED are byte-identical
# --------------------------------------------------------------------------

class TestParity(unittest.TestCase):
    SIZES = [30_000, 500, 500]

    def test_estimates_byte_identical_off_cheaper_sacred(self):
        cat = cheaper_alt_catalog()
        total, mt = sum(self.SIZES), 8000
        base = amb.estimate_cost(cat, "mid/model", total, len(self.SIZES),
                                 mt)
        variants = [ns(fallback=False),
                    ns(fallback=True),  # candidate is CHEAPER — no uplift
                    ns(fallback=True, _no_fallback=True),
                    ns(fallback=True, _auto_budget=True)]
        with env_var("AMBIENT_FALLBACK", None):
            for a in variants:
                got = amb.estimate_cost_fb(
                    cat, "mid/model", total, len(self.SIZES), mt, a, {},
                    per_call_chars=self.SIZES,
                    per_call_tokens=[mt] * len(self.SIZES))
                self.assertEqual(got, base)
                uniform = amb.estimate_cost_fb(
                    cat, "mid/model", total, len(self.SIZES), mt, a, {})
                self.assertEqual(uniform, base)

    def test_mr_estimates_byte_identical_off_cheaper_sacred(self):
        cat = cheaper_alt_catalog()
        base = amb.estimate_cost_mr(cat, "mid/model", None, 60_000, 4, 8000,
                                    extra_calls=1)
        with env_var("AMBIENT_FALLBACK", None):
            for a in (ns(fallback=False), ns(fallback=True),
                      ns(fallback=True, _no_fallback=True)):
                got = amb.estimate_cost_mr_fb(
                    cat, "mid/model", None, 60_000, 4, 8000, a, {},
                    extra_calls=1)
                self.assertEqual(got, base)

    def test_gate_line_byte_identical_off_cheaper_sacred(self):
        cat = cheaper_alt_catalog()
        total, mt = sum(self.SIZES), 8000

        def gate_stderr(**kw):
            err = io.StringIO()
            with env_var("AMBIENT_FALLBACK", None), \
                    env_var("AMBIENT_MAX_SPEND", None), \
                    contextlib.redirect_stderr(err):
                amb.cost_gate(cat, "mid/model", total, len(self.SIZES),
                              ns(max_tokens=mt, **kw), {},
                              per_call_chars=self.SIZES,
                              per_call_tokens=[mt] * len(self.SIZES))
            return err.getvalue()

        off = gate_stderr(fallback=False)
        self.assertEqual(gate_stderr(fallback=True), off)
        self.assertEqual(gate_stderr(fallback=True, _no_fallback=True), off)


if __name__ == "__main__":
    unittest.main()
