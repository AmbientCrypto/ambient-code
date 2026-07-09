"""Hermetic tests: fan-out x --fallback spend safety.

FIX 1 (HIGH): the UP-FRONT batch gates (cost_gate / cost_gate_mr /
    _consensus_estimate for best-of) are FALLBACK-AWARE — with --fallback
    live, every call is priced at max(requested, fallback-candidate), so N
    concurrent workers swapping to a pricier alt are covered by ONE
    aggregate reservation. The old per-worker re-gate inside complete()
    only max-refreshed the same-process fleet record to a single alt's
    cost — an N-fold under-reserve. PARITY: with fallback off, or a
    cheaper-or-equal candidate, every figure is byte-identical.
FIX 2 (MED): complete() re-gates a live fallback swap ONLY in single-call
    lanes (RequestSpec.gate_fallback, default True). Fan-out workers
    (map/consensus/best-of) run with gate_fallback=False — no SystemExit
    is ever raised inside a pool thread by the fallback gate.
FIX 3 (MED, defense in depth): the fan-out supervisors treat a
    BaseException (incl. SystemExit) from a worker as a fatal fail-fast —
    cancel_event trips, queued work is dropped, siblings stop billing.

No network, no live API, no writes outside tempdirs.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")

KEY = "sk-test-key-abcdef1234567890"


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v18", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v18", loader)
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


@contextlib.contextmanager
def fleet_dir():
    """Point the fleet machinery at a tmpdir with the fleet lane ON (the
    suite defaults it off for hermeticity) and no ceiling set."""
    with tempfile.TemporaryDirectory() as d:
        with patched(amb, USAGE_PATH=os.path.join(d, "usage.jsonl"),
                     _FLEET_RES_IDS=[]), \
                env_var("AMBIENT_FLEET_BUDGET", None), \
                env_var("AMBIENT_MAX_SPEND", None), \
                env_var("AMBIENT_FALLBACK", None), \
                env_var("AMBIENT_RESERVATION_TTL", None):
            yield d


def seed(d, records):
    with open(os.path.join(d, "reservations.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def store(d):
    path = os.path.join(d, "reservations.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def rec(amount, pid=None, age=0.0, rid=None):
    return {"pid": pid if pid is not None else os.getpid(),
            "ts": time.time() - age, "amount": amount,
            "id": rid or f"seed-{amount}-{age}"}


def fb_catalog(alt_in=1.0, alt_out=40.0):
    """Requested model 'cheap/asker' (cheap, workerless) + ONE ready
    fallback candidate 'alt/other' whose pricing the test controls."""
    return [
        {"id": "cheap/asker", "context_length": 120000,
         "max_output_length": 60000, "is_ready": False,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 0.1, "output": 0.4}},
        {"id": "alt/other", "context_length": 300000,
         "max_output_length": 60000, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": alt_in, "output": alt_out}},
    ]


def mid_catalog():
    return [
        {"id": "mid/model", "context_length": 120000,
         "max_output_length": 8000, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 2.0, "output": 10.0}},
    ]


def ns(**kw):
    """Engine-level namespace (complete/run_map_reduce/gate callers)."""
    base = dict(max_tokens=8000, temperature=0.1, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=False,
                yes=True, no_cache=True, cache_ttl=None, model=None,
                parallel=None, json=False, system=None,
                escalation_ceiling=30000, _auto_budget=False)
    base.update(kw)
    return argparse.Namespace(**base)


def stream_seq(*results):
    calls = []

    def fake(api_url, api_key, payload, timeout, on_delta=None):
        calls.append(payload)
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    return fake, calls


def ok_body(content="ok", finish="stop"):
    return (200, {"content": content, "reasoning": "", "usage": None,
                  "finish_reason": finish})


def no_workers():
    return (429, {"error": {"message": "No workers available"}})


# --------------------------------------------------------------------------
# FIX 1 — the up-front batch gate reserves the fallback exposure
# --------------------------------------------------------------------------

class TestFix1UpfrontFallbackReserve(unittest.TestCase):
    def test_pricier_fallback_batch_refused_up_front_at_fleet_ceiling(self):
        """8 calls under --fallback with a 40/M candidate: the BATCH gate
        must price the swap and refuse against a near-ceiling sibling —
        before any network call could exist."""
        cat = fb_catalog(alt_out=40.0)
        with fleet_dir() as d:
            seed(d, [rec(4.90)])
            with env_var("AMBIENT_MAX_SPEND", "5"), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as cm:
                amb.cost_gate(cat, "cheap/asker", 4000, 8,
                              ns(fallback=True), {})
            msg = str(cm.exception)
            self.assertIn("already reserved", msg)
            self.assertNotIn("$", msg)                   # zero dollar figures

    def test_same_batch_passes_without_fallback(self):
        """Control for the refusal above: the requested model alone is
        pennies — without --fallback the identical batch must pass."""
        cat = fb_catalog(alt_out=40.0)
        with fleet_dir() as d:
            seed(d, [rec(4.90)])
            with env_var("AMBIENT_MAX_SPEND", "5"), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cost_gate(cat, "cheap/asker", 4000, 8,
                              ns(fallback=False), {})
            amb._fleet_release_all()

    def test_reservation_covers_all_n_alt_calls_not_just_one(self):
        """The HIGH itself: the fleet record must hold the FULL N-call alt
        exposure (aggregate), not a max-refresh of one alt call."""
        cat = fb_catalog(alt_out=40.0)
        n_calls = 8
        with fleet_dir() as d:
            with contextlib.redirect_stderr(io.StringIO()):
                amb.cost_gate(cat, "cheap/asker", 4000, n_calls,
                              ns(fallback=True), {})
            recs = store(d)
            self.assertEqual(len(recs), 1)
            exp_alt = amb.estimate_cost(cat, "alt/other", 4000, n_calls,
                                        8000)[0]
            one_alt = amb.estimate_cost(cat, "alt/other", 4000, 1, 8000)[0]
            self.assertAlmostEqual(recs[0]["amount"], exp_alt, places=9)
            self.assertGreater(recs[0]["amount"], one_alt * (n_calls - 1))
            amb._fleet_release_all()

    def test_parity_cheaper_candidate_is_byte_identical(self):
        """A cheaper-or-equal candidate (the usual fit-then-cheapest pick)
        must leave the estimate EXACTLY unchanged — and so the gate line."""
        cat = [dict(mid_catalog()[0]),
               {"id": "cheap/alt", "context_length": 300000,
                "max_output_length": 60000, "is_ready": True,
                "supported_features": [], "output_modalities": ["text"],
                "pricing": {"input": 0.1, "output": 0.4}}]
        base = amb.estimate_cost(cat, "mid/model", 50_000, 6, 8000)
        with env_var("AMBIENT_FALLBACK", None):
            with_fb = amb.estimate_cost_fb(cat, "mid/model", 50_000, 6,
                                           8000, ns(fallback=True), {})
            without = amb.estimate_cost_fb(cat, "mid/model", 50_000, 6,
                                           8000, ns(fallback=False), {})
        self.assertEqual(with_fb, base)
        self.assertEqual(without, base)

        def gate_stderr(fallback):
            err = io.StringIO()
            with env_var("AMBIENT_FALLBACK", None), \
                    env_var("AMBIENT_MAX_SPEND", None), \
                    contextlib.redirect_stderr(err):
                amb.cost_gate(cat, "mid/model", 50_000, 6,
                              ns(fallback=fallback), {})
            return err.getvalue()

        self.assertEqual(gate_stderr(True), gate_stderr(False),
                         "the printed gate estimate must be byte-identical "
                         "when the candidate is cheaper")

    def test_sacred_no_fallback_keeps_the_estimate_unchanged(self):
        """_no_fallback lanes (consensus members) can never swap — a pricier
        candidate must not inflate their estimate."""
        cat = fb_catalog(alt_out=40.0)
        base = amb.estimate_cost(cat, "cheap/asker", 4000, 8, 8000)
        got = amb.estimate_cost_fb(cat, "cheap/asker", 4000, 8, 8000,
                                   ns(fallback=True, _no_fallback=True), {})
        self.assertEqual(got, base)

    def test_map_reduce_gate_is_fallback_aware_with_parity(self):
        cat = fb_catalog(alt_out=40.0)
        base = amb.estimate_cost_mr(cat, "cheap/asker", None, 60_000, 4,
                                    8000)
        with env_var("AMBIENT_FALLBACK", None):
            pricier = amb.estimate_cost_mr_fb(
                cat, "cheap/asker", None, 60_000, 4, 8000,
                ns(fallback=True), {})
            off = amb.estimate_cost_mr_fb(
                cat, "cheap/asker", None, 60_000, 4, 8000,
                ns(fallback=False), {})
        self.assertEqual(off, base)
        self.assertGreater(pricier[0], base[0])
        self.assertGreater(pricier[1], base[1])
        # never under-reserve: at least the all-alt figure
        all_alt = amb.estimate_cost_mr(cat, "alt/other", None, 60_000, 4,
                                       8000)
        self.assertGreaterEqual(pricier[0], all_alt[0])
        self.assertGreaterEqual(pricier[1], all_alt[1])

    def test_best_of_consensus_estimate_prices_fallback_only_when_asked(self):
        """_consensus_estimate: fb_args (best-of lane) raises the figure for
        a pricier candidate; without fb_args (consensus lane, SACRED
        _no_fallback workers) it stays byte-identical."""
        cat = fb_catalog(alt_out=40.0)
        labeled = [("a.py", "x = 1\n" * 50)]
        total = sum(len(t) for _, t in labeled)
        models = ["cheap/asker"] * 2
        plain = amb._consensus_estimate(cat, models, labeled, total)
        with env_var("AMBIENT_FALLBACK", None):
            again = amb._consensus_estimate(cat, models, labeled, total)
            fb = amb._consensus_estimate(cat, models, labeled, total,
                                         fb_args=ns(fallback=True),
                                         fb_conf={})
        self.assertEqual(again[:2], plain[:2])
        self.assertGreater(fb[0], plain[0])

    def test_best_of_chat_batch_refused_before_any_network_call(self):
        """End-to-end fan-out lane: a --fallback best-of batch against a
        near-ceiling fleet must be refused by the UP-FRONT gate — zero
        payloads ever reach the wire."""
        cat = fb_catalog(alt_out=40.0)
        fake, calls = stream_seq(ok_body("never"))
        messages = [{"role": "user", "content": "q" * 2000}]
        with fleet_dir() as d:
            seed(d, [rec(4.90)])
            with env_var("AMBIENT_MAX_SPEND", "5"), \
                    patched(amb, stream_completion=fake,
                            read_config_file=lambda: {}), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                amb._best_of_chat("k", "u", "cheap/asker", messages,
                                  ns(fallback=True, temperature=0.7), 2,
                                  cat, {}, kind="ask")
            self.assertEqual(calls, [],
                             "the batch must be refused BEFORE any spend")


# --------------------------------------------------------------------------
# FIX 2 — no per-worker fallback gate inside fan-out lanes
# --------------------------------------------------------------------------

class TestFix2WorkerGateRouting(unittest.TestCase):
    def setUp(self):
        self._logu = amb.log_usage
        amb.log_usage = lambda *a, **k: None

    def tearDown(self):
        amb.log_usage = self._logu

    def _swap(self, gate_calls, **spec_kw):
        cat = fb_catalog(alt_out=40.0)
        fake, calls = stream_seq(no_workers(), ok_body("j"))

        def spy_gate(expected, args, conf, bound=None):
            gate_calls.append(expected)

        with patched(amb, stream_completion=fake,
                     fetch_models=lambda *a: cat,
                     pick_fallback_model=lambda *a, **k: "alt/other",
                     _gate_amount=spy_gate,
                     read_config_file=lambda: {}), \
                env_var("AMBIENT_FALLBACK", None), \
                contextlib.redirect_stderr(io.StringIO()):
            content = amb.complete(
                "k", "u", "cheap/asker",
                [{"role": "user", "content": "x"}],
                ns(fallback=True, **spec_kw))[0]
        return content, calls

    def test_fanout_worker_spec_skips_the_per_call_fallback_gate(self):
        gate_calls = []
        content, calls = self._swap(gate_calls, gate_fallback=False)
        self.assertEqual(content, "j")
        self.assertEqual(calls[1]["model"], "alt/other")
        self.assertEqual(gate_calls, [],
                         "a fan-out worker must NEVER re-gate the swap — "
                         "the batch reserved it up front")

    def test_single_call_lane_still_gates_the_fallback_per_call(self):
        gate_calls = []
        content, calls = self._swap(gate_calls)  # default gate_fallback
        self.assertEqual(content, "j")
        self.assertEqual(calls[1]["model"], "alt/other")
        self.assertEqual(len(gate_calls), 1,
                         "single-call lanes keep the re-gate")
        self.assertGreater(gate_calls[0], 0.0)

    def test_fanout_worker_swap_never_systemexits_at_the_ceiling(self):
        """The MED scenario: a near-ceiling fleet + a pricier swap inside a
        worker context must NOT SystemExit — the batch already reserved."""
        cat = fb_catalog(alt_out=40.0)
        fake, calls = stream_seq(no_workers(), ok_body("j"))
        with fleet_dir() as d:
            seed(d, [rec(4.90)])
            with env_var("AMBIENT_MAX_SPEND", "5"), \
                    patched(amb, stream_completion=fake,
                            fetch_models=lambda *a: cat,
                            pick_fallback_model=lambda *a, **k: "alt/other",
                            read_config_file=lambda: {}), \
                    contextlib.redirect_stderr(io.StringIO()):
                content = amb.complete(
                    "k", "u", "cheap/asker",
                    [{"role": "user", "content": "x"}],
                    ns(fallback=True, gate_fallback=False))[0]
            self.assertEqual(content, "j")
            self.assertEqual(len(store(d)), 1,
                             "no per-worker fleet write — only the sibling")

    def test_run_map_reduce_threads_gate_fallback_false_to_every_call(self):
        specs = []

        def spy_complete(api_key, api_url, model, messages, args,
                         session=None, **kw):
            specs.append(amb.RequestSpec.from_args(args))
            return "part", None, {"finish_reason": "stop"}

        with patched(amb, complete=spy_complete), \
                contextlib.redirect_stderr(io.StringIO()):
            final, partial, _r = amb.run_map_reduce(
                "k", "u", "m/x", "SYS", ["aaa", "bbb"], ns(parallel=2),
                "SYNTH", 100_000)
        self.assertFalse(partial)
        self.assertEqual(len(specs), 3)  # 2 map + 1 synthesis
        for s in specs:
            self.assertFalse(s.gate_fallback,
                             "map-reduce workers AND synthesis must not "
                             "per-call gate a fallback swap")

    def test_best_of_chat_workers_carry_gate_fallback_false(self):
        specs = []

        def spy_complete(api_key, api_url, model, messages, args,
                         session=None, **kw):
            specs.append(amb.RequestSpec.from_args(args))
            return "sample", None, {"finish_reason": "stop"}

        with patched(amb, complete=spy_complete,
                     read_config_file=lambda: {}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb._best_of_chat("k", "u", "mid/model",
                              [{"role": "user", "content": "q"}],
                              ns(temperature=0.7, json=True), 2,
                              mid_catalog(), {}, kind="ask")
        self.assertEqual(len(specs), 2)
        for s in specs:
            self.assertFalse(s.gate_fallback)

    def test_map_items_workers_carry_gate_fallback_false(self):
        specs = []

        def spy_complete(api_key, api_url, model, messages, args,
                         session=None, **kw):
            specs.append(amb.RequestSpec.from_args(args))
            return "out", None, {"finish_reason": "stop"}

        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i in range(2):
                p = os.path.join(d, f"item{i}.txt")
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(f"text {i}\n")
                paths.append(p)
            args = argparse.Namespace(
                prompt="summarize", paths=paths, jsonl=False, json=True,
                allow_secrets=False, model="mid/model", system=None,
                max_tokens=None, temperature=0.1, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=True,
                yes=True, no_cache=True, cache_ttl=None, parallel=None)
            with patched(amb, safe_catalog=lambda *a, **k: mid_catalog(),
                         read_config_file=lambda: {},
                         complete=spy_complete), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cmd_map(args, KEY, "https://x", {})
        self.assertEqual(len(specs), 2)
        for s in specs:
            self.assertFalse(s.gate_fallback)


# --------------------------------------------------------------------------
# FIX 3 — a worker-side BaseException fails the fan-out fast
# --------------------------------------------------------------------------

class TestFix3WorkerFatalFailFast(unittest.TestCase):
    def tearDown(self):
        # Worker-fatal / Ctrl-C tests use a MOCKED os._exit, so pool workers
        # survive the test (production's real os._exit kills them). Drain them so
        # a leaked worker can't pollute a later test by resolving a module-global
        # it re-patched (the cross-test thread-leak flake).
        deadline = time.monotonic() + 10.0
        for t in list(threading.enumerate()):
            if (t is threading.main_thread() or not t.is_alive()
                    or not t.name.startswith("ThreadPoolExecutor")):
                continue
            t.join(timeout=max(0.0, deadline - time.monotonic()))

    def test_map_reduce_worker_fatal_trips_cancel_and_stops_siblings(self):
        cancel = threading.Event()
        calls = []

        def fatal_complete(api_key, api_url, model, messages, args,
                           session=None, **kw):
            i = len(calls)
            calls.append(model)
            if i == 0:
                raise SystemExit(3)
            # A sibling that raced in before the abort: it must observe the
            # fail-fast promptly instead of billing to completion.
            if not cancel.wait(5):
                return "BILLED", None, {"finish_reason": "stop"}
            raise amb.ChatError("cancelled", "sibling observed the abort")

        with patched(amb, complete=fatal_complete), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            amb.run_map_reduce("k", "u", "m/x", "SYS",
                               ["aaa", "bbb", "ccc"], ns(parallel=1),
                               "SYNTH", 100_000,
                               reducer=lambda texts: "merged",
                               cancel_event=cancel)
        self.assertTrue(cancel.is_set(),
                        "the supervisor must trip cancel_event on a "
                        "worker-side fatal")
        self.assertLessEqual(len(calls), 2,
                             "queued siblings must never start billing "
                             "after the fatal")
        self.assertNotIn("BILLED", calls)

    def test_map_reduce_local_event_when_caller_passed_none(self):
        """Standalone map-reduce (no caller cancel_event) still fails fast:
        the supervisor provides its own event."""
        calls = []

        def fatal_complete(api_key, api_url, model, messages, args,
                           session=None, **kw):
            calls.append(model)
            raise SystemExit(3)

        with patched(amb, complete=fatal_complete), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            amb.run_map_reduce("k", "u", "m/x", "SYS",
                               ["aaa", "bbb", "ccc"], ns(parallel=1),
                               "SYNTH", 100_000,
                               reducer=lambda texts: "merged")
        self.assertLessEqual(len(calls), 2)

    def test_map_reduce_keyboard_interrupt_exits_promptly(self):
        """Ordinary map-reduce Ctrl-C must os._exit(130) + trip cancel_event,
        NOT stall on the finally's blocking pool.shutdown(wait=True) while an
        in-flight worker drains to the timeout (mirrors the consensus/best-of
        Ctrl-C fix). On the old code this hung ~5s and never os._exit'd."""
        exit_codes = []
        cancel = threading.Event()

        def fake_exit(code):            # halt where the real os._exit would
            exit_codes.append(code)
            raise SystemExit(code)

        def in_flight(api_key, api_url, model, messages, args,
                      session=None, **kw):
            cancel.wait(5)              # returns only once cancel is tripped
            return "x", None, {"finish_reason": "stop"}

        def boom(*a, **k):              # Ctrl-C in the main thread
            raise KeyboardInterrupt

        start = time.monotonic()
        with patched(amb, complete=in_flight), \
                patched(amb.concurrent.futures, as_completed=boom), \
                patched(amb.os, _exit=fake_exit), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            amb.run_map_reduce("k", "u", "m/x", "SYS",
                               ["aaa", "bbb", "ccc"], ns(parallel=1),
                               "SYNTH", 100_000,
                               reducer=lambda texts: "merged",
                               cancel_event=cancel)
        self.assertEqual(exit_codes, [130])
        self.assertEqual(cm.exception.code, 130)
        self.assertTrue(cancel.is_set(),
                        "Ctrl-C must trip cancel_event so in-flight workers bail")
        self.assertLess(time.monotonic() - start, 3)

    def test_best_of_chat_worker_fatal_aborts_the_batch(self):
        release = threading.Event()
        calls = []

        def fatal_complete(api_key, api_url, model, messages, args,
                           session=None, **kw):
            i = len(calls)
            calls.append(model)
            if i == 0:
                raise SystemExit(3)
            release.wait(5)
            raise amb.ChatError("cancelled", "aborted")

        try:
            with patched(amb, complete=fatal_complete,
                         read_config_file=lambda: {}), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                amb._best_of_chat("k", "u", "mid/model",
                                  [{"role": "user", "content": "q"}],
                                  ns(temperature=0.7, parallel=1, json=True),
                                  3, mid_catalog(), {}, kind="ask")
        finally:
            release.set()
        self.assertLessEqual(len(calls), 2,
                             "the third sample must never start after the "
                             "fatal (fail-fast, not drain-and-bill)")

    def test_map_items_worker_fatal_aborts_the_batch(self):
        release = threading.Event()
        calls = []

        def fatal_complete(api_key, api_url, model, messages, args,
                           session=None, **kw):
            i = len(calls)
            calls.append(model)
            if i == 0:
                raise SystemExit(3)
            release.wait(5)
            raise amb.ChatError("cancelled", "aborted")

        try:
            with tempfile.TemporaryDirectory() as d:
                paths = []
                for i in range(3):
                    p = os.path.join(d, f"item{i}.txt")
                    with open(p, "w", encoding="utf-8") as fh:
                        fh.write(f"text {i}\n")
                    paths.append(p)
                args = argparse.Namespace(
                    prompt="summarize", paths=paths, jsonl=False, json=True,
                    allow_secrets=False, model="mid/model", system=None,
                    max_tokens=None, temperature=0.1, timeout=30, raw=False,
                    fallback=False, allow_partial=False, allow_cost=True,
                    yes=True, no_cache=True, cache_ttl=None, parallel=1)
                with patched(amb, safe_catalog=lambda *a, **k: mid_catalog(),
                             read_config_file=lambda: {},
                             complete=fatal_complete), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit):
                    amb.cmd_map(args, KEY, "https://x", {})
        finally:
            release.set()
        self.assertLessEqual(len(calls), 2,
                             "the third item must never start after the "
                             "fatal (fail-fast, not drain-and-bill)")


if __name__ == "__main__":
    unittest.main()
