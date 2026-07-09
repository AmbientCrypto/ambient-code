"""Regression: the shared API opener must NOT follow HTTP redirects.

urllib's default opener follows 3xx redirects and re-sends the
`Authorization: Bearer <key>` header to the redirect target's host, which would
bypass host pinning and leak the API key. `_API_OPENER` installs
`_NoRedirectHandler`, which refuses redirects outright. This test proves a 302
raises (rather than forwarding) and that the redirect target receives nothing.

Localhost-only; no external network.
"""
import http.server
import importlib.machinery
import importlib.util
import os
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin", "ambient")


def _load():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_redir", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_redir", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = _load()


class _Recorder(http.server.BaseHTTPRequestHandler):
    received = []

    def do_GET(self):
        _Recorder.received.append(dict(self.headers))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class _Redirector(http.server.BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", _Redirector.target)
        self.end_headers()

    def log_message(self, *a):
        pass


class RedirectAuthzTest(unittest.TestCase):
    def test_handler_refuses_redirect(self):
        handler = amb._NoRedirectHandler()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                urllib.request.Request("https://api.example/x"),
                None, 302, "Found", {}, "https://evil.example/x")

    def test_opener_does_not_forward_key_on_redirect(self):
        _Recorder.received = []
        rec = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        _Redirector.target = f"http://127.0.0.1:{rec.server_address[1]}/x"
        red = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
        for srv in (rec, red):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{red.server_address[1]}/x",
                headers={"Authorization": "Bearer SECRET-KEY"})
            with self.assertRaises(urllib.error.HTTPError):
                amb._NO_REDIRECT_OPENER.open(req, timeout=5)
            # The redirect target must have received NOTHING — the key never left.
            self.assertEqual(_Recorder.received, [])
        finally:
            for srv in (rec, red):
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
