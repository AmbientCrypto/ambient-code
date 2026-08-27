"""The loopback HTTP server presenting an ANTHROPIC face to Claude Code.

Claude Code points ANTHROPIC_BASE_URL at http://127.0.0.1:PORT and this server
translates every turn to Ambient's clean OpenAI path (orchestrator + upstream),
synthesizing the Anthropic response/SSE. Security: it authenticates a RANDOM LOCAL
token (never the Ambient key, which the bridge injects upstream itself), binds loopback,
and rejects cross-origin (DNS-rebind) requests.

Long turns: for a streaming request it emits message_start + periodic pings while the
upstream accumulates in a worker thread, so Claude Code's connection never idle-times-out
during a 12-minute reasoning turn.
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, List, Optional
from urllib.parse import urlparse

from . import anthropic_sse as sse
from . import catalog_map, orchestrator
from .errors import BridgeError
from .learned import LearnedWindows
from .orchestrator import ContextOverflowError

def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


_MAX_BODY_BYTES = 32 * 1024 * 1024
_CONN_TIMEOUT_S = 30.0
_MAX_HANDLER_THREADS = 64
_CATALOG_TTL_S = 60.0
_PING_INTERVAL_S = _env_float("AMBIENT_BRIDGE_PING_S", 10.0)
_ALLOWED_HOSTS = {"", "127.0.0.1", "localhost", "::1", "[::1]"} | {
    h.strip().lower() for h in os.environ.get("AMBIENT_BRIDGE_ALLOW_HOSTS", "").split(",") if h.strip()}
_STARTED_AT = int(time.time())


class Deps:
    """Everything the handler needs, injected by `bin/ambient` cmd_serve (kept out of the
    package so nothing here imports the CLI)."""
    def __init__(self, local_token: str, api_url: str, default_model: str,
                 upstream_call: Callable, fetch_catalog: Callable[[], List[dict]],
                 user_map: Optional[dict] = None, version: str = "bridge",
                 learned_path: Optional[str] = None):
        self.local_token = local_token
        self.api_url = api_url
        self.default_model = default_model
        self.upstream_call = upstream_call
        self.fetch_catalog = fetch_catalog
        self.user_map = user_map or {}
        self.version = version
        self.learned_path = learned_path  # persist learned per-model window ceilings here


class _Catalog:
    """TTL cache with single-flight refresh; keeps last-known-good on a failed fetch."""
    def __init__(self, fetch):
        self._fetch = fetch
        self._models = []  # type: List[dict]
        self._at = 0.0
        self._tried = False
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()

    def _fresh(self, now):
        # Back off for a full TTL after ANY attempt (success, empty, or failure) so an
        # empty/failed fetch does not cause a per-request fetch storm; keep last-known-good.
        return self._tried and now - self._at <= _CATALOG_TTL_S

    def get(self, now):
        with self._lock:
            if self._fresh(now):
                return self._models
        with self._fetch_lock:
            with self._lock:
                if self._fresh(now):
                    return self._models
            try:
                models = self._fetch() or []
            except Exception:  # noqa: BLE001  keep last-known-good
                with self._lock:
                    self._tried, self._at = True, now
                    return self._models
            with self._lock:
                self._tried, self._at = True, now
                if models:
                    self._models = models
                return self._models


class _NullLearned:
    """No-op stand-in so the handler never AttributeErrors when `learned` is unset
    (a partial/foreign construction). Plans against the catalog window; learns nothing."""
    def effective(self, model, catalog_window, now=None):
        return catalog_window

    def record(self, model, failed_input_tokens, now=None):
        pass


class AnthropicBridgeHandler(BaseHTTPRequestHandler):
    deps = None          # type: Deps
    catalog = None       # type: _Catalog
    learned = _NullLearned()   # type: LearnedWindows  (real one injected by serve())
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # never risk logging a body/secret
        pass

    # ---- guards --------------------------------------------------------
    def _cross_origin(self) -> bool:
        if self.headers.get("Origin") or self.headers.get("Referer"):
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        return host not in _ALLOWED_HOSTS

    def _authorized(self) -> bool:
        token = self.headers.get("x-api-key") or ""
        if not token:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                token = auth[7:]
        return bool(token) and hmac.compare_digest(token, self.deps.local_token)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_error(400, "invalid_request_error", "invalid Content-Length")
            return None
        if length < 0 or length > _MAX_BODY_BYTES:
            self._send_error(413, "invalid_request_error", "request body too large")
            return None
        try:
            obj = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as e:
            self._send_error(400, "invalid_request_error", "invalid JSON: {0}".format(e))
            return None
        if not isinstance(obj, dict):
            self._send_error(400, "invalid_request_error", "body must be a JSON object")
            return None
        return obj

    # ---- writers -------------------------------------------------------
    def _send_json(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def _send_error(self, status, err_type, message):
        self.close_connection = True  # avoid HTTP/1.1 desync from an undrained/rejected body
        self._send_json(status, {"type": "error", "error": {"type": err_type, "message": message}})

    def _write_event(self, event):
        self.wfile.write(sse.format_sse(event[0], event[1]))
        self.wfile.flush()

    # ---- routes --------------------------------------------------------
    def do_GET(self):
        if self._cross_origin():
            return self._send_error(403, "permission_error", "cross-origin not allowed")
        path = urlparse(self.path).path.rstrip("/")
        if path in ("/healthz", "/v1/healthz"):
            return self._send_json(200, self._health())
        if path == "/v1/models":
            if not self._authorized():
                return self._send_error(401, "authentication_error", "invalid local bridge token")
            return self._send_json(200, self._models_payload())
        self._send_error(404, "not_found_error", "no route for GET {0}".format(path))

    def do_POST(self):
        if self._cross_origin():
            return self._send_error(403, "permission_error", "cross-origin not allowed")
        path = urlparse(self.path).path.rstrip("/")
        if not self._authorized():
            return self._send_error(401, "authentication_error", "invalid local bridge token")
        if path == "/v1/messages":
            return self._handle_messages()
        if path == "/v1/messages/count_tokens":
            return self._handle_count_tokens()
        self._send_error(404, "not_found_error", "no route for POST {0}".format(path))

    # ---- handlers ------------------------------------------------------
    def _handle_count_tokens(self):
        body = self._read_json_body()
        if body is None:
            return
        self._send_json(200, {"input_tokens": catalog_map.count_input_tokens(body)})

    def _handle_messages(self):
        body = self._read_json_body()
        if body is None:
            return
        catalog = self.catalog.get(time.time())
        default = self.deps.default_model

        def resolve(req):
            return catalog_map.resolve_model(req, catalog, default, self.deps.user_map)

        def profile_of(mid):
            prof = catalog_map.profile_for(mid, catalog)
            # Plan against the LEARNED window (min(catalog, learned)) — self-heals a
            # model whose real window shrank below what the catalog still claims.
            return prof._replace(window=self.learned.effective(mid, prof.window))

        try:
            prepared = orchestrator.prepare(body, resolve, profile_of)
        except BridgeError as e:
            return self._send_error(getattr(e, "http_status", 400),
                                    getattr(e, "anthropic_type", "invalid_request_error"), str(e))

        if body.get("stream"):
            self._stream_response(prepared)
        else:
            try:
                message = orchestrator.run_upstream(prepared, self.deps.upstream_call)
            except BridgeError as e:
                if isinstance(e, ContextOverflowError):
                    self.learned.record(prepared.served_model,
                                        getattr(e, "observed_ceiling", prepared.est_prompt_tokens))
                return self._send_error(getattr(e, "http_status", 502),
                                        getattr(e, "anthropic_type", "api_error"), str(e))
            self._send_json(200, message)

    def _stream_response(self, prepared):
        mid = "msg_bridge_" + uuid.uuid4().hex
        # No Content-Length on an SSE stream, so close the connection at the end for the
        # client to see EOF (HTTP/1.1 would otherwise keep-alive and hang the reader).
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self._write_event(sse.message_start_event(mid, prepared.requested_model,
                                                      prepared.est_prompt_tokens))
            self._write_event(sse.ping_event())
        except (BrokenPipeError, ConnectionError, OSError):
            return

        holder = {}

        def _run():
            try:
                holder["msg"] = orchestrator.run_upstream(prepared, self.deps.upstream_call)
            except BaseException as e:  # noqa: BLE001
                holder["err"] = e

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        try:
            while th.is_alive():
                th.join(_PING_INTERVAL_S)
                if th.is_alive():
                    self._write_event(sse.ping_event())  # long-turn keepalive
            if "err" in holder:
                err = holder["err"]
                if isinstance(err, ContextOverflowError):
                    # Learn the ceiling so the NEXT turn's pre-call check compacts before
                    # committing the stream (this occurrence surfaces as an error the client
                    # retries; the retry pre-empts it — the run self-heals within one turn).
                    self.learned.record(prepared.served_model,
                                        getattr(err, "observed_ceiling", prepared.est_prompt_tokens))
                if isinstance(err, BridgeError):
                    self._write_event(sse.error_event(getattr(err, "anthropic_type", "api_error"), str(err)))
                else:
                    self._write_event(sse.error_event("api_error", "bridge error"))
                self._write_event(("message_stop", {"type": "message_stop"}))
            else:
                for event in sse.content_events(holder["msg"]):
                    self._write_event(event)
        except (BrokenPipeError, ConnectionError, OSError):
            pass  # client hung up mid-stream; fine

    # ---- payloads ------------------------------------------------------
    def _models_payload(self):
        catalog = self.catalog.get(time.time())
        data = []
        for e in catalog:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            data.append({"type": "model", "id": e["id"],
                         "display_name": e.get("display_name") or e["id"],
                         "created_at": "1970-01-01T00:00:00Z"})
        return {"data": data, "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None}

    def _health(self):
        catalog = self.catalog.get(time.time())
        return {"status": "ok", "bridge_version": self.deps.version,
                "models_cached": len(catalog),
                "upstream_host": urlparse(self.deps.api_url).hostname or "ambient"}


class _PooledHTTPServer(HTTPServer):
    """Bounded thread pool + per-connection timeout, swallowing connection errors."""
    def __init__(self, *args, **kwargs):
        # Create the pool BEFORE binding, so server_close() is safe even if the bind fails.
        self._pool = ThreadPoolExecutor(max_workers=_MAX_HANDLER_THREADS, thread_name_prefix="ambridge")
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        request.settimeout(_CONN_TIMEOUT_S)
        self._pool.submit(self._run, request, client_address)

    def _run(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except (socket.timeout, BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            self.shutdown_request(request)

    def handle_error(self, request, client_address):
        pass

    def server_close(self):
        super().server_close()
        pool = getattr(self, "_pool", None)
        if pool is not None:
            pool.shutdown(wait=False)


def serve(host: str, port: int, deps: Deps, on_bound=None) -> None:
    AnthropicBridgeHandler.deps = deps
    AnthropicBridgeHandler.catalog = _Catalog(deps.fetch_catalog)
    AnthropicBridgeHandler.learned = LearnedWindows(deps.learned_path)
    threading.Thread(target=lambda: AnthropicBridgeHandler.catalog.get(time.time()),
                     daemon=True).start()  # warm before first request
    httpd = _PooledHTTPServer((host, port), AnthropicBridgeHandler)
    if on_bound is not None:
        # Report the ACTUAL bound port (port 0 -> an OS-chosen ephemeral port), and only
        # AFTER the socket is bound — so state/health never advertise a port we don't hold.
        on_bound(httpd.server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
