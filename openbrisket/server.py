"""`openbrisket serve`: the web page, the JSON API and the MCP endpoint, plus the background runner.

Everything under /api and /mcp needs the access token as `Authorization: Bearer <token>`. The page
itself is a static shell with no data in it; it asks for the token once and keeps it in the browser.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, apidoc, mcp
from .service import SCOPES, Service, ServiceError

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 256 * 1024
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}


WRONG_TOKEN_LIMIT = 10        # wrong tokens accepted per window before they are refused unread
WRONG_TOKEN_WINDOW_S = 60.0


class Throttle:
    """Slows down token guessing. Wrong tokens are counted over a rolling window; past the limit, a request
    with a wrong token is refused with 429. A request with a right token is never slowed or refused."""

    def __init__(self, limit: int = WRONG_TOKEN_LIMIT, window_s: float = WRONG_TOKEN_WINDOW_S):
        self.limit, self.window_s = limit, window_s
        self._failures: deque = deque()
        self._lock = threading.Lock()

    def wrong(self) -> bool:
        """Record a wrong token. Returns True when the limit has been passed."""
        now = time.monotonic()
        with self._lock:
            while self._failures and now - self._failures[0] > self.window_s:
                self._failures.popleft()
            self._failures.append(now)
            return len(self._failures) > self.limit


def make_handler(service: Service):
    throttle = Throttle()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"openbrisket/{__version__}"

        def log_message(self, fmt, *args):   # quiet: one line per request is noise during a 14 h cook
            pass

        # --- plumbing -----------------------------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _scope(self) -> str | None:
            """The rights of the token on this request, or None."""
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return None
            return service.scope_of(header[7:].strip())

        def _allowed(self, method: str, path: str) -> str | None:
            """Check the request's token against what the route needs. Sends the refusal itself and returns
            None; returns the token's scope when the request may go ahead."""
            scope = self._scope()
            if scope is None:
                if self.headers.get("Authorization", "")[7:].strip() and throttle.wrong():
                    self._json({"error": "too many wrong tokens; wait a minute and try again"}, 429)
                    return None
                self._json({"error": "access token required"}, 401)
                return None
            lookup = path
            if path.startswith("/api/cooks/"):
                lookup = "/api/cooks/{id}"
            elif path.startswith("/api/history/"):
                lookup = "/api/history/{kind}/{id}"
            need = apidoc.required_scope(method, lookup) or "read"
            if SCOPES.index(scope) < SCOPES.index(need):
                self._json({"error": f"this token has {scope} rights; this needs {need}"}, 403)
                return None
            return scope

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ServiceError("request too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw)
            except ValueError:
                raise ServiceError("body is not valid JSON") from None
            if not isinstance(data, dict):
                raise ServiceError("body must be a JSON object")
            return data

        # --- routes -------------------------------------------------------------------------------
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._json({"ok": True, "version": __version__})
            if path == "/api/openapi.json":
                return self._json(apidoc.spec())
            if path in STATIC:
                name, ctype = STATIC[path]
                with open(os.path.join(WEB_DIR, name), "rb") as f:
                    return self._send(200, f.read(), ctype, {
                        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"})
            if not path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            scope = self._allowed("GET", path)
            if scope is None:
                return None
            try:
                if path == "/api/tokens":
                    return self._json(service.list_tokens())
                if path == "/api/status":
                    return self._json(service.status())
                if path == "/api/recipes":
                    return self._json(service.list_recipes())
                if path == "/api/rubs":
                    return self._json(service.list_rubs())
                if path == "/api/stacks":
                    return self._json(service.list_stacks())
                if path == "/api/stacks/vocabulary":
                    return self._json(service.stack_vocabulary())
                if path.startswith("/api/history/"):
                    kind, _, item = path[len("/api/history/"):].partition("/")
                    return self._json(service.item_history(kind, item))
                if path == "/api/checks":
                    return self._json(service.check_vocabulary())
                if path == "/api/styles":
                    return self._json(service.styles())
                if path == "/api/cooks":
                    return self._json(service.cooks())
                if path.startswith("/api/cooks/"):
                    return self._json(service.cook(path[len("/api/cooks/"):]))
                if path == "/api/settings":
                    view = service.settings_view()
                    view["access_token"] = service.access_token()
                    view["tokens"] = service.list_tokens()
                    return self._json(view)
                return self._json({"error": "not found"}, 404)
            except ServiceError as e:
                return self._json({"error": str(e)}, 400)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path != "/mcp" and not path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            scope = self._allowed("POST", path)
            if scope is None:
                return None
            try:
                body = self._body()
                if path == "/mcp":
                    reply = mcp.handle(service, body, scope)
                    if reply is None:
                        return self._send(202, b"", "application/json")
                    return self._json(reply)
                if path == "/api/recipes":
                    return self._json(service.save_recipe(body))
                if path == "/api/rubs":
                    return self._json(service.save_rub(body))
                if path == "/api/stacks":
                    return self._json(service.save_stack(body))
                if path == "/api/yaml":
                    return self._json(service.save_yaml(body.get("kind", ""), body.get("yaml", ""), body.get("change", "")))
                if path == "/api/tokens":
                    return self._json(service.mint_token(body.get("name", ""), body.get("scope", "")))
                if path == "/api/tokens/revoke":
                    return self._json(service.revoke_token(body.get("id", "")))
                if path == "/api/delete":
                    return self._json(service.delete_item(body.get("kind", ""), body.get("id", "")))
                if path == "/api/plan":
                    return self._json(service.plan(body.get("recipe"), body.get("weight_kg"), body.get("serve_at"),
                                                   body.get("stack")))
                if path == "/api/cooks":
                    return self._json(service.start(body.get("recipe"), body.get("weight_kg"), body.get("serve_at"),
                                                    body.get("probes") or body.get("probe"), body.get("probe_labels"),
                                                    body.get("stack")))
                if path == "/api/log":
                    return self._json(service.log_step(body.get("step", ""), body.get("note", "")))
                if path == "/api/probe":
                    return self._json(service.set_probe(body.get("probe"), body.get("in_meat", False)))
                if path == "/api/check":
                    return self._json(service.record_check(body.get("check", ""), body.get("data"), body.get("verdict"),
                                                           body.get("by", ""), body.get("note", "")))
                if path == "/api/smoke-test":
                    return self._json(service.smoke_test())
                if path == "/api/close":
                    return self._json(service.close(body.get("score"), body.get("cooked_weight_kg"),
                                                    body.get("notes", ""), bool(body.get("abandon")),
                                                    body.get("fuel_used_kg")))
                if path == "/api/settings":
                    return self._json(service.save_settings(body))
                if path == "/api/backup":
                    return self._json(service.save_backup(body.get("url"), body.get("token") or ""))
                if path == "/api/backup/now":
                    return self._json(service.backup_now())
                if path == "/api/settings/new-token":
                    return self._json(service.new_access_token())
                if path == "/api/settings/test-source":
                    return self._json(service.test_source())
                if path == "/api/settings/test-notify":
                    return self._json(service.test_notify())
                return self._json({"error": "not found"}, 404)
            except ServiceError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:   # a bug must not take the server down mid-cook
                print(f"server: {path}: {e!r}", file=sys.stderr, flush=True)
                return self._json({"error": "internal error; see the log"}, 500)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8321, service: Service | None = None) -> None:
    service = service or Service()
    token = service.access_token()
    service.start_runner()
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    httpd.daemon_threads = True
    print(f"OpenBrisket {__version__} on http://{host}:{port}", flush=True)
    print(f"access token: {token}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        httpd.server_close()
