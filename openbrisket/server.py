"""`openbrisket serve`: the web page, the JSON API and the MCP endpoint, plus the background runner.

Everything under /api and /mcp needs the access token as `Authorization: Bearer <token>`. The page
itself is a static shell with no data in it; it asks for the token once and keeps it in the browser.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, mcp
from .service import Service, ServiceError

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 256 * 1024
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}


def make_handler(service: Service):
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

        def _authed(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return False
            return hmac.compare_digest(header[7:].strip().encode(), service.access_token().encode())

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
            if path in STATIC:
                name, ctype = STATIC[path]
                with open(os.path.join(WEB_DIR, name), "rb") as f:
                    return self._send(200, f.read(), ctype, {
                        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"})
            if not path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            if not self._authed():
                return self._json({"error": "access token required"}, 401)
            try:
                if path == "/api/status":
                    return self._json(service.status())
                if path == "/api/recipes":
                    return self._json(service.list_recipes())
                if path == "/api/cooks":
                    return self._json(service.cooks())
                if path.startswith("/api/cooks/"):
                    return self._json(service.cook(path[len("/api/cooks/"):]))
                if path == "/api/settings":
                    view = service.settings_view()
                    view["access_token"] = service.access_token()
                    return self._json(view)
                return self._json({"error": "not found"}, 404)
            except ServiceError as e:
                return self._json({"error": str(e)}, 400)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path != "/mcp" and not path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            if not self._authed():
                return self._json({"error": "access token required"}, 401,)
            try:
                body = self._body()
                if path == "/mcp":
                    reply = mcp.handle(service, body)
                    if reply is None:
                        return self._send(202, b"", "application/json")
                    return self._json(reply)
                if path == "/api/plan":
                    return self._json(service.plan(body.get("recipe"), body.get("weight_kg"), body.get("serve_at")))
                if path == "/api/cooks":
                    return self._json(service.start(body.get("recipe"), body.get("weight_kg"), body.get("serve_at"),
                                                    body.get("probes") or body.get("probe"), body.get("probe_labels")))
                if path == "/api/log":
                    return self._json(service.log_step(body.get("step", ""), body.get("note", "")))
                if path == "/api/smoke-test":
                    return self._json(service.smoke_test())
                if path == "/api/close":
                    return self._json(service.close(body.get("score"), body.get("cooked_weight_kg"),
                                                    body.get("notes", ""), bool(body.get("abandon"))))
                if path == "/api/settings":
                    return self._json(service.save_settings(body))
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
