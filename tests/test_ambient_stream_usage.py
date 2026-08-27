"""stream_completion must capture OpenAI's trailing usage-only SSE chunk
(sent AFTER finish_reason, before [DONE], via stream_options.include_usage) instead
of breaking on finish_reason and falling back to a local estimate.

A real loopback HTTP server emits the exact OpenAI streaming order so the drain
logic is exercised end-to-end (no mock of the parser)."""
import http.server
import importlib.machinery
import importlib.util
import os
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_stream", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_stream", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()

# The canonical OpenAI include_usage order: content, then a finish_reason chunk,
# then a usage-only chunk (choices:[]), then [DONE].
_SSE = (
    b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
    b'data: [DONE]\n\n'
)


class _SSEHandler(http.server.BaseHTTPRequestHandler):
    body = _SSE

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # drain the request so the socket stays clean
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(self.body)
        self.wfile.flush()


class TestTrailingUsageCaptured(unittest.TestCase):
    def _serve(self, body):
        class H(_SSEHandler):
            pass
        H.body = body
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def test_usage_after_finish_is_captured(self):
        url = self._serve(_SSE)
        status, result = amb.stream_completion(
            url, "k", {"model": "m", "messages": []}, 10)
        self.assertEqual(status, 200)
        self.assertEqual(result["content"], "hello")
        self.assertEqual(result["finish_reason"], "stop")
        self.assertEqual(result["usage"],
                         {"prompt_tokens": 10, "completion_tokens": 5})

    def test_usage_after_keepalive_comments_is_captured(self):
        # SSE keepalive comments arriving AFTER finish_reason must not defeat the
        # drain — usage that follows them is still captured, not lost to an estimate.
        body = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                b': keepalive\n\n'
                b': keepalive\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
                b'data: [DONE]\n\n')
        url = self._serve(body)
        status, result = amb.stream_completion(
            url, "k", {"model": "m", "messages": []}, 10)
        self.assertEqual(status, 200)
        self.assertEqual(result["usage"], {"prompt_tokens": 7, "completion_tokens": 3})

    def test_finish_without_done_or_usage_still_completes(self):
        # A provider that sends finish_reason and closes WITHOUT [DONE]/usage must
        # still return cleanly (finished, not a stall) — usage just stays absent.
        body = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        url = self._serve(body)
        status, result = amb.stream_completion(
            url, "k", {"model": "m", "messages": []}, 10)
        self.assertEqual(status, 200)
        self.assertEqual(result["content"], "hi")
        self.assertEqual(result["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
