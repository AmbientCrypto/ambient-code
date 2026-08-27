"""ambient_code.server: the Anthropic-face HTTP server (fake upstream, real socket)."""
import http.client
import json
import os
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ambient_code import server as srv  # noqa: E402
from ambient_code.orchestrator import UpstreamResult  # noqa: E402

TOKEN = "local-bridge-token-xyz"
BIG_CATALOG = [{"id": "moonshotai/kimi-k2.7-code", "context_length": 100000,
                "max_output_length": 8192, "supported_features": ["reasoning"],
                "display_name": "Kimi K2.7 Code"}]
TINY_CATALOG = [{"id": "moonshotai/kimi-k2.7-code", "context_length": 100,
                 "max_output_length": 64, "supported_features": ["reasoning"]}]


def clean_upstream(payload):
    return UpstreamResult(200, {"choices": [{"message": {"role": "assistant", "content": "hi there"},
                          "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})


def tool_upstream(payload):
    return UpstreamResult(200, {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": "functions.Read:0", "type": "function",
             "function": {"name": "Read", "arguments": '{"path":"a.py"}'}}]}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4}})


class Harness:
    def __init__(self, catalog=BIG_CATALOG, upstream=clean_upstream):
        self.catalog = catalog
        self.upstream = upstream

    def __enter__(self):
        deps = srv.Deps(local_token=TOKEN, api_url="https://api.ambient.xyz",
                        default_model="moonshotai/kimi-k2.7-code", upstream_call=self.upstream,
                        fetch_catalog=lambda: self.catalog, version="test")
        srv.AnthropicBridgeHandler.deps = deps
        srv.AnthropicBridgeHandler.catalog = srv._Catalog(deps.fetch_catalog)
        srv.AnthropicBridgeHandler.learned = srv.LearnedWindows()
        self.httpd = srv._PooledHTTPServer(("127.0.0.1", 0), srv.AnthropicBridgeHandler)
        self.port = self.httpd.server_address[1]
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def req(self, method, path, body=None, headers=None, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Content-Type": "application/json"}
        if auth:
            h["x-api-key"] = TOKEN
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = r.read().decode("utf-8")
        status = r.status
        conn.close()
        return status, data


class TestRoutesAndAuth(unittest.TestCase):
    def test_healthz_no_auth(self):
        with Harness() as h:
            status, data = h.req("GET", "/healthz", auth=False)
            self.assertEqual(status, 200)
            body = json.loads(data)
            self.assertEqual(body["status"], "ok")
            # never leak the full upstream URL/scheme/credentials — hostname only
            self.assertEqual(body.get("upstream_host"), "api.ambient.xyz")
            self.assertNotIn("https://", json.dumps(body))

    def test_models_requires_auth_and_is_anthropic_shaped(self):
        with Harness() as h:
            self.assertEqual(h.req("GET", "/v1/models", auth=False)[0], 401)
            status, data = h.req("GET", "/v1/models")
            self.assertEqual(status, 200)
            payload = json.loads(data)
            self.assertEqual(payload["data"][0]["type"], "model")
            self.assertIn("has_more", payload)

    def test_bad_token_401(self):
        with Harness() as h:
            self.assertEqual(h.req("POST", "/v1/messages", body={"messages": []},
                                   headers={"x-api-key": "wrong"})[0], 401)

    def test_bridge_identity_via_token(self):
        # launcher.bridge_is_ours must accept OUR token and reject a foreign one, so a
        # stale/foreign listener on the port is never handed Claude's traffic.
        import sys as _sys
        _sys.path.insert(0, ROOT)
        from ambient_code import launcher as lm
        with Harness() as h:
            self.assertTrue(lm.bridge_is_ours("127.0.0.1", h.port, TOKEN, timeout=2.0))
            self.assertFalse(lm.bridge_is_ours("127.0.0.1", h.port, "wrong-token", timeout=2.0))

    def test_cross_origin_403(self):
        with Harness() as h:
            self.assertEqual(h.req("GET", "/healthz", headers={"Origin": "http://evil"})[0], 403)

    def test_beta_query_param_is_routed(self):
        with Harness() as h:
            status, data = h.req("POST", "/v1/messages?beta=true",
                                 body={"model": "claude-x", "max_tokens": 100,
                                       "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(status, 200, data)


class TestMessages(unittest.TestCase):
    def test_non_stream_text(self):
        with Harness() as h:
            status, data = h.req("POST", "/v1/messages",
                                 body={"model": "claude-x", "max_tokens": 100,
                                       "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(status, 200, data)
            msg = json.loads(data)
            self.assertEqual(msg["type"], "message")
            self.assertEqual(msg["content"], [{"type": "text", "text": "hi there"}])
            self.assertEqual(msg["stop_reason"], "end_turn")

    def test_non_stream_tool_use(self):
        with Harness(upstream=tool_upstream) as h:
            status, data = h.req("POST", "/v1/messages",
                                 body={"model": "claude-x", "max_tokens": 100,
                                       "messages": [{"role": "user", "content": "read a.py"}]})
            msg = json.loads(data)
            self.assertEqual(msg["stop_reason"], "tool_use")
            self.assertEqual(msg["content"][0]["type"], "tool_use")

    def test_stream(self):
        with Harness() as h:
            status, data = h.req("POST", "/v1/messages",
                                 body={"model": "claude-x", "max_tokens": 100, "stream": True,
                                       "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(status, 200)
            self.assertIn("event: message_start", data)
            self.assertIn("event: content_block_start", data)
            self.assertIn("event: message_stop", data)
            self.assertNotIn("[DONE]", data)

    def test_count_tokens(self):
        with Harness() as h:
            status, data = h.req("POST", "/v1/messages/count_tokens",
                                 body={"messages": [{"role": "user", "content": "hello world"}]})
            self.assertEqual(status, 200)
            self.assertIsInstance(json.loads(data)["input_tokens"], int)

    def test_window_self_heal_learns_on_overflow_400(self):
        # A large opaque 400 -> compaction (fraction heuristic) AND the bridge LEARNS a
        # ceiling below the catalog window, so the next turn pre-empts it (the DeepSeek
        # 50k->40k case, transparent).
        def overflow_upstream(payload):
            return UpstreamResult(400, None, "Upstream request failed")
        with Harness(upstream=overflow_upstream) as h:
            big = "x " * 156000  # ~78k est; catalog window 100k
            status, data = h.req("POST", "/v1/messages",
                                 body={"model": "claude-x", "max_tokens": 100,
                                       "messages": [{"role": "user", "content": big}]})
            self.assertEqual(status, 400)
            self.assertIn("prompt is too long", data)
            eff = srv.AnthropicBridgeHandler.learned.effective("moonshotai/kimi-k2.7-code", 100000)
            self.assertLess(eff, 100000, "bridge must learn a smaller ceiling")

    def test_overflow_400_unbrick(self):
        with Harness(catalog=TINY_CATALOG) as h:
            status, data = h.req("POST", "/v1/messages",
                                 body={"model": "claude-x", "max_tokens": 100,
                                       "messages": [{"role": "user", "content": "hello"}]})
            self.assertEqual(status, 400)
            err = json.loads(data)
            self.assertEqual(err["error"]["type"], "invalid_request_error")
            self.assertIn("prompt is too long", err["error"]["message"])


if __name__ == "__main__":
    unittest.main()
