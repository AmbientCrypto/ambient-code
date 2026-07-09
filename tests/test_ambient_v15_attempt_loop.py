"""tests: complete()'s bounded attempt-loop (formerly recursive
retry frames) and the frozen Session transport context.

Hermetic — every test fakes the transport (module-global stream_completion or
an injected Session transport); no network, no live API. Parity focus: the
loop must take the SAME retry/fallback/salvage branches the recursion did,
and Session must not change WHICH catalog is used (one memoized fetch)."""
import argparse
import contextlib
import dataclasses
import importlib.machinery
import importlib.util
import io
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v15", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v15", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()


def rich_catalog():
    return [
        {"id": "z-ai/glm-5.2", "context_length": 202752,
         "max_output_length": 202752, "is_ready": True,
         "supported_features": ["reasoning", "structured_outputs"],
         "output_modalities": ["text"],
         "pricing": {"input": 0.9, "output": 3.6}},
        {"id": "alt/cheap", "context_length": 300000,
         "max_output_length": 60000, "is_ready": True,
         "supported_features": ["reasoning", "json_mode"],
         "output_modalities": ["text"],
         "pricing": {"input": 0.1, "output": 0.4}},
    ]


def ns(**kw):
    base = dict(max_tokens=8000, temperature=0.1, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=True, yes=True,
                no_cache=True, cache_ttl=None, model=None,
                escalation_ceiling=30000, _auto_budget=True)
    base.update(kw)
    return argparse.Namespace(**base)


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


def stream_seq(*results):
    """Fake transport returning canned results per call; Exception instances
    raise. Returns (fake, payload-log, on_delta-log)."""
    calls, deltas = [], []

    def fake(api_url, api_key, payload, timeout, on_delta=None):
        calls.append(payload)
        deltas.append(on_delta)
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    return fake, calls, deltas


def ok_body(content="ok", finish="stop", reasoning=""):
    return (200, {"content": content, "reasoning": reasoning, "usage": None,
                  "finish_reason": finish})


class AttemptLoopTests(unittest.TestCase):
    """The bounded loop takes the SAME branches the old recursion did."""

    def setUp(self):
        self._logu = amb.log_usage
        amb.log_usage = lambda *a, **k: None

    def tearDown(self):
        amb.log_usage = self._logu

    def test_stall_retry_streams_only_on_the_first_attempt(self):
        fake, calls, deltas = stream_seq(amb.StallError("s", partial=""),
                                         ok_body("fine"))
        sink = []
        with patched(amb, stream_completion=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            content, _u, _b = amb.complete(
                "k", "u", "m", [{"role": "user", "content": "x"}], ns(),
                on_delta=sink.append)
        self.assertEqual(content, "fine")
        self.assertEqual(len(calls), 2)  # one stall retry, same model
        self.assertTrue(all(p["model"] == "m" for p in calls))
        self.assertIsNotNone(deltas[0])   # first attempt streams
        self.assertIsNone(deltas[1])      # the retry must NOT double-print

    def test_budget_escalation_never_mutates_the_callers_args(self):
        fake, calls, _d = stream_seq(ok_body(content=""), ok_body("done"))
        args = ns()
        with patched(amb, stream_completion=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            content, _u, _b = amb.complete(
                "k", "u", "m", [{"role": "user", "content": "x"}], args)
        self.assertEqual(content, "done")
        self.assertEqual(len(calls), 2)
        # same escalation math as the recursion: min(max(2x, x+16384), ceiling)
        self.assertEqual(calls[1]["max_tokens"], 24384)
        # immutability: the retry rode a REPLACED AttemptState with a fresh
        # Namespace — the caller's args object is untouched.
        self.assertEqual(args.max_tokens, 8000)

    def test_budget_400_shrinks_once_without_mutating_args(self):
        fake, calls, _d = stream_seq(
            (400, {"error": {"message": "max_tokens exceeds model limit"}}),
            ok_body("fine"))
        args = ns()
        with patched(amb, stream_completion=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            content, _u, _b = amb.complete("k", "u", "m", [], args)
        self.assertEqual(content, "fine")
        self.assertEqual(calls[1]["max_tokens"], 4000)
        self.assertEqual(args.max_tokens, 8000)

    def test_fallback_swap_discloses_the_served_model(self):
        fake, calls, _d = stream_seq(
            (429, {"error": {"message": "No workers available"}}),
            ok_body("swapped"))
        with patched(amb, stream_completion=fake,
                     fetch_models=lambda *a: rich_catalog(),
                     read_config_file=lambda: {}), \
                contextlib.redirect_stderr(io.StringIO()):
            content, _u, body = amb.complete(
                "k", "u", "z-ai/glm-5.2",
                [{"role": "user", "content": "x"}], ns(fallback=True))
        self.assertEqual(content, "swapped")
        self.assertEqual(calls[0]["model"], "z-ai/glm-5.2")
        self.assertNotEqual(calls[1]["model"], "z-ai/glm-5.2")
        # --json consumers must learn who ACTUALLY answered
        self.assertEqual(body["_served_model"], calls[1]["model"])

    def test_no_fallback_kill_switch_blocks_the_swap(self):
        # SACRED: _no_fallback wins over --fallback — the model fails as
        # itself instead of silently becoming another model.
        fake, calls, _d = stream_seq(
            (429, {"error": {"message": "No workers available"}}))
        args = ns(fallback=True)
        args._no_fallback = True
        with patched(amb, stream_completion=fake,
                     fetch_models=lambda *a: rich_catalog(),
                     read_config_file=lambda: {}), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(amb.ChatError) as cm:
                amb.complete("k", "u", "z-ai/glm-5.2",
                             [{"role": "user", "content": "x"}], args)
        self.assertEqual(cm.exception.category, "model")
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(p["model"] == "z-ai/glm-5.2" for p in calls))

    def test_full_ladder_fits_inside_the_hard_attempt_bound(self):
        # Deepest chain the old recursion could reach: stall retry → budget
        # shrink → model fallback → budget escalation → success = 5 attempts,
        # exactly the loop's hard bound.
        fake, calls, _d = stream_seq(
            amb.StallError("s", partial=""),
            (400, {"error": {"message": "max_tokens exceeds model limit"}}),
            (429, {"error": {"message": "No workers available"}}),
            ok_body(content=""),
            ok_body("done"))
        with patched(amb, stream_completion=fake,
                     fetch_models=lambda *a: rich_catalog(),
                     read_config_file=lambda: {}), \
                contextlib.redirect_stderr(io.StringIO()):
            content, _u, body = amb.complete(
                "k", "u", "z-ai/glm-5.2",
                [{"role": "user", "content": "x"}], ns(fallback=True))
        self.assertEqual(content, "done")
        self.assertEqual(len(calls), amb.MAX_COMPLETE_ATTEMPTS)
        self.assertEqual(body["_served_model"], calls[-1]["model"])

    def test_attempt_bound_is_a_hard_stop_not_a_spin(self):
        # The bound is unreachable through the guards (each retry flips one
        # False flag) — but it must exist and fail CLOSED, not loop forever.
        fake, _c, _d = stream_seq(ok_body("never-reached"))
        with patched(amb, stream_completion=fake, MAX_COMPLETE_ATTEMPTS=0):
            with self.assertRaises(amb.ChatError) as cm:
                amb.complete("k", "u", "m", [], ns())
        self.assertEqual(cm.exception.category, "internal")

    def test_attempt_state_is_frozen_and_replace_makes_a_new_one(self):
        st = amb.AttemptState(model="m", messages=[],
                              spec=amb.RequestSpec.from_args(ns()))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            st.model = "other"
        nxt = dataclasses.replace(st, stall_retried=True,
                                  attempt_no=st.attempt_no + 1)
        self.assertIsNot(nxt, st)
        self.assertFalse(st.stall_retried)   # original untouched
        self.assertTrue(nxt.stall_retried)
        self.assertEqual(st.attempt_no, 0)
        self.assertEqual(nxt.attempt_no, 1)


class SessionTests(unittest.TestCase):
    """Session: one frozen transport context, one memoized catalog fetch,
    and an injectable transport that needs no global monkeypatching."""

    def setUp(self):
        self._logu = amb.log_usage
        amb.log_usage = lambda *a, **k: None

    def tearDown(self):
        amb.log_usage = self._logu

    def test_session_is_frozen(self):
        sess = amb.Session(api_url="https://x", api_key="k")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            sess.api_key = "other"

    def test_session_is_hashable_despite_dict_conf(self):
        # eq=False → identity hash; a frozen eq=True dataclass would raise
        # TypeError on hash() because conf is an unhashable dict.
        sess = amb.Session(api_url="https://x", api_key="k", conf={"a": 1})
        self.assertEqual(hash(sess), hash(sess))
        self.assertIn(sess, {sess})  # usable as a set/dict key

    def test_catalog_is_fetched_at_most_once(self):
        count = [0]

        def fetch(url, key):
            count[0] += 1
            return rich_catalog()

        sess = amb.Session(api_url="https://x", api_key="k")
        with patched(amb, fetch_models=fetch):
            first = sess.catalog()
            second = sess.catalog()
        self.assertEqual(count[0], 1)       # ONE fetch across both calls
        self.assertIs(first, second)        # same object, no re-normalize
        self.assertEqual(first[0]["id"], "z-ai/glm-5.2")

    def test_degraded_catalog_is_memoized_not_retried(self):
        count = [0]

        def boom(url, key):
            count[0] += 1
            raise amb.NetworkError("down")

        sess = amb.Session(api_url="https://x", api_key="k")
        with patched(amb, fetch_models=boom):
            self.assertEqual(sess.catalog(), [])
            self.assertEqual(sess.catalog(), [])
        self.assertEqual(count[0], 1)  # a degraded [] never retries silently

    def test_injected_transport_is_used_without_global_patching(self):
        seen = []

        def transport(api_url, api_key, payload, timeout, on_delta=None):
            seen.append((api_url, api_key, payload["model"]))
            return ok_body("via-session")

        def bomb(*a, **k):  # the module global must NOT be touched
            raise AssertionError("global stream_completion was called")

        sess = amb.Session(api_url="https://x", api_key="k",
                           transport=transport)
        with patched(amb, stream_completion=bomb):
            content, _u, _b = amb.complete(
                "k", "https://x", "m", [{"role": "user", "content": "q"}],
                ns(), session=sess)
        self.assertEqual(content, "via-session")
        self.assertEqual(seen, [("https://x", "k", "m")])

    def test_default_transport_resolves_the_global_at_call_time(self):
        # A Session with transport=None must keep honoring test monkeypatches
        # of the module-level stream_completion (late binding — parity with
        # every pre-Session caller).
        fake, calls, _d = stream_seq(ok_body("global-lane"))
        sess = amb.Session(api_url="https://x", api_key="k")
        with patched(amb, stream_completion=fake):
            content, _u, _b = amb.complete(
                "k", "https://x", "m", [], ns(), session=sess)
        self.assertEqual(content, "global-lane")
        self.assertEqual(len(calls), 1)

    def test_session_transport_reaches_the_map_reduce_fan_out(self):
        seen = []

        def transport(api_url, api_key, payload, timeout, on_delta=None):
            seen.append(payload["messages"][-1]["content"])
            return ok_body("part")

        sess = amb.Session(api_url="https://x", api_key="k",
                           transport=transport)
        with contextlib.redirect_stderr(io.StringIO()):
            final, partial, _r = amb.run_map_reduce(
                "k", "https://x", "m", "map", ["chunk one", "chunk two"],
                ns(), "synth", 8000, reducer=lambda texts: "\n".join(texts),
                session=sess)
        self.assertFalse(partial)
        self.assertEqual(sorted(seen), ["chunk one", "chunk two"])

    def test_loose_args_and_session_forms_return_identical_results(self):
        fake_a, calls_a, _ = stream_seq(ok_body("same"))
        fake_b, calls_b, _ = stream_seq(ok_body("same"))
        msgs = [{"role": "user", "content": "q"}]
        with patched(amb, stream_completion=fake_a):
            res_loose = amb.complete("k", "https://x", "m", msgs, ns())
        sess = amb.Session(api_url="https://x", api_key="k",
                           transport=fake_b)
        res_sess = amb.complete("k", "https://x", "m", msgs, ns(),
                                session=sess)
        self.assertEqual(res_loose[0], res_sess[0])
        self.assertEqual(res_loose[2], res_sess[2])
        self.assertEqual(calls_a, calls_b)  # byte-identical payloads


if __name__ == "__main__":
    unittest.main()
