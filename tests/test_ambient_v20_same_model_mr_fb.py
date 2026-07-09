"""Hermetic tests: SAME-MODEL map-reduce fallback under-reserve.

Final spend-safety fixture: with a live
--fallback, cost_gate_mr's same-model delegation to cost_gate priced the swap
exposure on a UNIFORM input/(n_chunks*2+extra) pseudo-call. The live map
workers each carry their REAL chunk (gate_fallback=False — no later per-call
gate), and a big real chunk swaps to a pricier LARGE-context candidate the
small averaged pseudo-call never priced. The edge case: 4 chunks,
input_chars=80_000, max_tokens=8000, requested model unready, a cheap
small-context alt fits the 10k pseudo-call while the pricier big-context alt
fits each real 20k map call — reserve ~0.045 exp / ~0.058 bound vs ~0.77
exp / ~1.01 bound of the 4 real map fallbacks.

The fix: the map-reduce fallback reserve prices each MAP call at its REAL
chunk size + that chunk's OWN fallback candidate (exactly like cmd_map's
uneven items), never a uniform average — even on the same-model delegated
path. PARITY: fallback off / SACRED _no_fallback / no pricier candidate stays
BYTE-identical to the classic delegated cost_gate figure (and its stderr).

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
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v20", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v20", loader)
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


def edge_case_catalog():
    """The an unready cheap requested model, a
    CHEAP small-context candidate that fits the 10k-char averaged pseudo-call
    (10_000/3.2 = 3125 tok <= 4000) but NOT a real 20k-char map chunk
    (6250 tok > 4000), and a PRICIER large-context candidate that each real
    map call must fall back to."""
    return [_mdl("req/pro", 200_000, 60_000, False, 0.1, 0.4),
            _mdl("small/alt", 4_000, 60_000, True, 0.2, 0.8),
            _mdl("big/alt", 400_000, 60_000, True, 2.0, 30.0)]


def cheaper_alt_catalog():
    return [_mdl("mid/model", 120_000, 60_000, True, 2.0, 10.0),
            _mdl("cheap/alt", 300_000, 60_000, True, 0.1, 0.4)]


CHUNKS = [20_000] * 4
INPUT, N, MT = 80_000, 4, 8000


def _fb_ctx():
    return contextlib.ExitStack()


# --------------------------------------------------------------------------
# (a) the the reserve must cover the REAL map fallbacks
# --------------------------------------------------------------------------

class TestSpendEdgeCase(unittest.TestCase):
    def test_fixture_candidates_split_by_size(self):
        """Sanity: the averaged pseudo-call picks the cheap alt; each REAL
        20k map chunk can only fall back to the pricier big-context one."""
        cat = edge_case_catalog()
        a = ns(fallback=True)
        pseudo = INPUT / (N * 2)  # the old delegated per-call average
        with env_var("AMBIENT_FALLBACK", None):
            self.assertEqual(
                amb._batch_fallback_alt(cat, "req/pro", a, {}, pseudo),
                "small/alt")
            self.assertEqual(
                amb._batch_fallback_alt(cat, "req/pro", a, {}, 20_000),
                "big/alt")

    def test_reserve_covers_the_real_map_fallback_spend(self):
        """The per-chunk reserve must be >= the 4 real map-call fallbacks
        (~0.77 exp / ~1.01 bound) — not the ~0.045 averaged figure."""
        cat = edge_case_catalog()
        with env_var("AMBIENT_FALLBACK", None):
            fb = amb.estimate_cost_mr_fb(
                cat, "req/pro", None, INPUT, N, MT, ns(fallback=True), {},
                per_call_chars=CHUNKS)
            # the pre-fix delegated figure: uniform pseudo-calls over 2N
            old = amb.estimate_cost_fb(cat, "req/pro", INPUT, N * 2, MT,
                                       ns(fallback=True), {})
        alt_mt = amb._fb_alt_budget(cat, "big/alt", MT, False)
        live_maps_exp = sum(amb._fb_call_cost(cat, "big/alt", c, alt_mt,
                                              1.0)[0] for c in CHUNKS)
        live_maps_bnd = sum(amb._fb_call_cost(cat, "big/alt", c, alt_mt,
                                              1.0)[1] for c in CHUNKS)
        # the numbers are real on this fixture
        self.assertAlmostEqual(live_maps_exp, 0.77, places=2)
        self.assertAlmostEqual(live_maps_bnd, 1.01, places=2)
        self.assertLess(old[0], 0.10, "the bug: the averaged reserve is a "
                                      "fraction of the real fallback spend")
        # the fixed reserve covers the live map fallbacks (plus the
        # not-swapped synthesis calls the base already prices)
        synth_req_exp = sum(amb._fb_call_cost(cat, "req/pro", c, MT, 0.3,
                                              cpt_model="req/pro")[0]
                            for c in CHUNKS)
        synth_req_bnd = sum(amb._fb_call_cost(cat, "req/pro", c, MT, 0.3,
                                              cpt_model="req/pro")[1]
                            for c in CHUNKS)
        self.assertGreaterEqual(fb[0] + 1e-9, live_maps_exp + synth_req_exp)
        self.assertGreaterEqual(fb[1] + 1e-9, live_maps_bnd + synth_req_bnd)
        self.assertGreater(fb[0], old[0])

    def test_gate_refuses_up_front_below_the_true_cost(self):
        """cost_gate_mr must refuse at a 0.30 ceiling: the old averaged
        figure (~0.045) sailed under it while the live run could legally
        spend ~0.77+ on map fallbacks alone."""
        cat = edge_case_catalog()

        def gate(fallback):
            with env_var("AMBIENT_FALLBACK", None), \
                    env_var("AMBIENT_MAX_SPEND", "0.30"), \
                    env_var("AMBIENT_FLEET_BUDGET", "off"), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cost_gate_mr(cat, "req/pro", None, INPUT, N,
                                 ns(fallback=fallback, max_tokens=MT), {},
                                 per_call_chars=CHUNKS)

        with self.assertRaises(SystemExit):
            gate(fallback=True)
        gate(fallback=False)  # control: no fallback exposure — pennies, runs

    def test_gate_allows_with_a_ceiling_above_the_true_reserve(self):
        cat = edge_case_catalog()
        with env_var("AMBIENT_FALLBACK", None), \
                env_var("AMBIENT_MAX_SPEND", "50"), \
                env_var("AMBIENT_FLEET_BUDGET", "off"), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cost_gate_mr(cat, "req/pro", None, INPUT, N,
                             ns(fallback=True, max_tokens=MT), {},
                             per_call_chars=CHUNKS)


# --------------------------------------------------------------------------
# (b) domination: NO mixture of map/synthesis fallbacks exceeds the reserve
# --------------------------------------------------------------------------

class TestMixtureDomination(unittest.TestCase):
    SIZES = [40_000, 20_000, 10_000, 10_000]  # uneven — MIXED candidates

    def test_every_map_and_synthesis_mixture_is_covered(self):
        """Each chunk's own candidate (big chunks -> big/alt, small ->
        small/alt), every 2^4 x 2^4 map/synthesis fallback mixture priced
        per the estimate's own lane decomposition — none may exceed the
        reserve."""
        cat = edge_case_catalog()
        total = sum(self.SIZES)
        a = ns(fallback=True)
        with env_var("AMBIENT_FALLBACK", None):
            fb = amb.estimate_cost_mr_fb(
                cat, "req/pro", None, total, len(self.SIZES), MT, a, {},
                per_call_chars=self.SIZES)
            alts = {c: amb._batch_fallback_alt(cat, "req/pro", a, {}, c)
                    for c in set(self.SIZES)}
        self.assertEqual({alts[40_000], alts[10_000]},
                         {"big/alt", "small/alt"},
                         "fixture must exercise MIXED per-chunk candidates")

        def call(mdl, chars, in_factor):
            mt = (MT if mdl == "req/pro"
                  else amb._fb_alt_budget(cat, mdl, MT, False))
            return amb._fb_call_cost(cat, mdl, chars, mt, in_factor,
                                     cpt_model="req/pro"
                                     if mdl == "req/pro" else None)

        n = len(self.SIZES)
        for map_mask in range(2 ** n):
            for syn_mask in range(2 ** n):
                exp = bnd = 0.0
                for i, c in enumerate(self.SIZES):
                    m_mdl = alts[c] if map_mask >> i & 1 else "req/pro"
                    s_mdl = alts[c] if syn_mask >> i & 1 else "req/pro"
                    me, mb, _ = call(m_mdl, c, 1.0)
                    se, sb, _ = call(s_mdl, c, 0.3)
                    exp += me + se
                    bnd += mb + sb
                self.assertGreaterEqual(
                    fb[0] + 1e-9, exp,
                    f"mixture map={map_mask:04b} syn={syn_mask:04b} "
                    "exceeds the expected reserve")
                self.assertGreaterEqual(
                    fb[1] + 1e-9, bnd,
                    f"mixture map={map_mask:04b} syn={syn_mask:04b} "
                    "exceeds the bound reserve")

    def test_uneven_chunks_beat_the_uniform_average_reserve(self):
        """Threading the REAL chunk list must reserve strictly more than the
        uniform input/n average when only the big chunk's own sizing selects
        the pricier large-context candidate."""
        cat = edge_case_catalog()
        sizes = [20_000, 800, 800, 800]
        total = sum(sizes)
        with env_var("AMBIENT_FALLBACK", None):
            per_chunk = amb.estimate_cost_mr_fb(
                cat, "req/pro", None, total, len(sizes), MT,
                ns(fallback=True), {}, per_call_chars=sizes)
            uniform = amb.estimate_cost_mr_fb(
                cat, "req/pro", None, total, len(sizes), MT,
                ns(fallback=True), {})
            self.assertEqual(
                amb._batch_fallback_alt(cat, "req/pro", ns(fallback=True),
                                        {}, total / len(sizes)),
                "small/alt")
        self.assertGreater(per_chunk[0], uniform[0])
        self.assertGreater(per_chunk[1], uniform[1])
        # and it carries the big chunk's big/alt swap at full map size
        base = amb.estimate_cost_mr(cat, "req/pro", None, total, len(sizes),
                                    MT)
        alt_mt = amb._fb_alt_budget(cat, "big/alt", MT, False)
        big_alt = amb._fb_call_cost(cat, "big/alt", 20_000, alt_mt, 1.0)
        big_req = amb._fb_call_cost(cat, "req/pro", 20_000, MT, 1.0,
                                    cpt_model="req/pro")
        self.assertGreaterEqual(per_chunk[0] + 1e-9,
                                base[0] + (big_alt[0] - big_req[0]))
        self.assertGreaterEqual(per_chunk[1] + 1e-9,
                                base[1] + (big_alt[1] - big_req[1]))


# --------------------------------------------------------------------------
# (c) PARITY — off / SACRED / cheaper alt: byte-identical to the classic gate
# --------------------------------------------------------------------------

class TestSameModelParity(unittest.TestCase):
    SIZES = [30_000, 500, 500, 29_000]

    def test_mr_fb_same_model_byte_identical_off_cheaper_sacred(self):
        cat = cheaper_alt_catalog()
        total = sum(self.SIZES)
        base = amb.estimate_cost_mr(cat, "mid/model", None, total,
                                    len(self.SIZES), MT)
        classic = amb.estimate_cost(cat, "mid/model", total,
                                    len(self.SIZES) * 2, MT)
        self.assertEqual(base, classic)  # the same-model delegation contract
        variants = [ns(fallback=False),
                    ns(fallback=True),  # candidate is CHEAPER — no uplift
                    ns(fallback=True, _no_fallback=True),
                    ns(fallback=True, _auto_budget=True)]
        with env_var("AMBIENT_FALLBACK", None):
            for a in variants:
                got = amb.estimate_cost_mr_fb(
                    cat, "mid/model", None, total, len(self.SIZES), MT,
                    a, {}, per_call_chars=self.SIZES)
                self.assertEqual(got, base)
                uniform = amb.estimate_cost_mr_fb(
                    cat, "mid/model", None, total, len(self.SIZES), MT,
                    a, {})
                self.assertEqual(uniform, base)

    def test_gate_stderr_byte_identical_off_cheaper_sacred(self):
        """The same-model cost_gate_mr must keep delegating to cost_gate —
        same figures, same printed line — whenever fallback is off / SACRED
        / the candidate is no costlier."""
        cat = cheaper_alt_catalog()
        total = sum(self.SIZES)

        def mr_stderr(**kw):
            err = io.StringIO()
            with env_var("AMBIENT_FALLBACK", None), \
                    env_var("AMBIENT_MAX_SPEND", None), \
                    env_var("AMBIENT_FLEET_BUDGET", "off"), \
                    contextlib.redirect_stderr(err):
                amb.cost_gate_mr(cat, "mid/model", None, total,
                                 len(self.SIZES), ns(max_tokens=MT, **kw),
                                 {}, per_call_chars=self.SIZES)
            return err.getvalue()

        classic = io.StringIO()
        with env_var("AMBIENT_FALLBACK", None), \
                env_var("AMBIENT_MAX_SPEND", None), \
                env_var("AMBIENT_FLEET_BUDGET", "off"), \
                contextlib.redirect_stderr(classic):
            amb.cost_gate(cat, "mid/model", total, len(self.SIZES) * 2,
                          ns(max_tokens=MT, fallback=False), {})

        off = mr_stderr(fallback=False)
        self.assertEqual(off, classic.getvalue())
        self.assertEqual(mr_stderr(fallback=True), off)
        self.assertEqual(mr_stderr(fallback=True, _no_fallback=True), off)

    def test_deterministic_merge_lane_parity_is_preserved(self):
        """synthesis=False (map-only consensus/structured pricing) stays
        byte-identical when fallback is off or the candidate is cheaper."""
        cat = cheaper_alt_catalog()
        total = sum(self.SIZES)
        base = amb.estimate_cost_mr(cat, "mid/model", None, total,
                                    len(self.SIZES), MT, synthesis=False)
        with env_var("AMBIENT_FALLBACK", None):
            for a in (ns(fallback=False), ns(fallback=True),
                      ns(fallback=True, _no_fallback=True)):
                got = amb.estimate_cost_mr_fb(
                    cat, "mid/model", None, total, len(self.SIZES), MT,
                    a, {}, synthesis=False, per_call_chars=self.SIZES)
                self.assertEqual(got, base)


# --------------------------------------------------------------------------
# (d) wiring — the callers thread the REAL packed chunk sizes into the gate
# --------------------------------------------------------------------------

class TestCallSiteWiring(unittest.TestCase):
    def _spy(self, record):
        def spy(catalog, model, reduce_model, input_chars, n_chunks, args,
                conf, extra_calls=0, synthesis=True, per_call_chars=None):
            record.append({"input_chars": input_chars, "n_chunks": n_chunks,
                           "per_call_chars": per_call_chars,
                           "synthesis": synthesis})
            raise SystemExit(0)  # stop before any (fake) network call
        return spy

    def test_ask_split_threads_real_chunk_sizes(self):
        record = []
        args = argparse.Namespace(
            prompt=["y" * 700_000], system=None, model="map/big",
            max_tokens=None, temperature=0.1, timeout=30, raw=True,
            fallback=False, allow_partial=True, allow_cost=True, yes=True,
            no_cache=True, cache_ttl=None, parallel=None,
            allow_secrets=False, json=False, reduce_model=None)
        cat = [_mdl("map/big", 262_144, 65_536, True, 0.2, 0.8)]
        with patched(amb, safe_catalog=lambda *a, **k: cat,
                     cost_gate_mr=self._spy(record),
                     log_usage=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                amb.cmd_ask(args, KEY, "https://x", {})
        self.assertEqual(len(record), 1)
        got = record[0]
        sizes = got["per_call_chars"]
        self.assertIsInstance(sizes, list)
        self.assertEqual(len(sizes), got["n_chunks"])
        self.assertTrue(all(s > 0 for s in sizes))
        # the REAL chunks jointly carry the whole doc (small header slack)
        self.assertGreaterEqual(sum(sizes), got["input_chars"] * 0.95)
        self.assertLessEqual(sum(sizes), got["input_chars"] * 1.2)

    def test_audit_chunked_threads_chunk_plus_code_map_sizes(self):
        record = []
        cat = [_mdl("reduce/tiny", 16_000, 8_000, True, 0.1, 0.4)]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "big.py")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("def f():\n    pass\n" * 12_000)
            args = argparse.Namespace(
                paths=[p], staged=False, diff=None, focus=None,
                allow_secrets=False, format="prose", dry_run=False,
                consensus=None, model="reduce/tiny", max_tokens=None,
                temperature=0.1, timeout=30, raw=False, fallback=False,
                allow_partial=True, allow_cost=True, yes=True,
                no_cache=True, cache_ttl=None, parallel=None,
                reduce_model=None, json=False, repo=None, deep=None,
                best_of=None, install_hook=None, uninstall_hook=None,
                force=False)
            with patched(amb, safe_catalog=lambda *a, **k: cat,
                         cost_gate_mr=self._spy(record),
                         log_usage=lambda *a, **k: None), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(len(record), 1)
        got = record[0]
        sizes = got["per_call_chars"]
        self.assertIsInstance(sizes, list)
        self.assertEqual(len(sizes), got["n_chunks"])
        # each size carries its chunk + the repo map the gate's input counts
        self.assertGreaterEqual(sum(sizes), got["input_chars"] * 0.95)
        self.assertLessEqual(sum(sizes), got["input_chars"] * 1.2)

    def test_best_of_miss_plans_carry_per_call_sizes(self):
        """_best_of_audit_misses must expose each billed chunk's REAL size
        so the best-of gate prices per-chunk candidates, not an average."""
        cat = [_mdl("reduce/tiny", 16_000, 8_000, True, 0.1, 0.4)]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "big.py")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("def f():\n    pass\n" * 12_000)
            with open(p, encoding="utf-8") as fh:
                labeled = [(p, fh.read())]
            args = ns(fallback=False, max_tokens=None, format="json")
            plans = amb._best_of_audit_misses(
                cat, "reduce/tiny", labeled, "audit this", args, 2, False, None)
        self.assertEqual(len(plans), 2)
        for plan in plans:
            self.assertEqual(len(plan), 4)
            miss_calls, miss_input, _mt, sizes = plan
            self.assertEqual(len(sizes), miss_calls)
            self.assertEqual(sum(sizes), miss_input)
            self.assertGreater(miss_calls, 1, "fixture must chunk")


if __name__ == "__main__":
    unittest.main()
