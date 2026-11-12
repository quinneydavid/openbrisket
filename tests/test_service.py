import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

from openbrisket import mcp, server, settings, sources
from openbrisket.service import Service, ServiceError
from tests.helpers import ROOT


class FakeSource:
    def __init__(self):
        self.pit, self.meat, self.fan = 120.0, 24.0, 40.0

    def read(self):
        return sources.Reading(at=datetime.now(timezone.utc), pit=self.pit, probes=[self.meat, None, None],
                               fan=self.fan, fan_on=True, pit_target=121.0, connected=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"OPENBRISKET_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.source = FakeSource()
        src = mock.patch.object(sources, "from_settings", lambda cfg: self.source)
        src.start()
        self.addCleanup(src.stop)
        self.service = Service([os.path.join(ROOT, "recipes")])
        self.serve_at = (datetime.now() + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M")


class ServiceTests(Base):
    def test_recipes_and_plan(self):
        ids = [r["id"] for r in self.service.list_recipes()]
        self.assertEqual(ids, ["pork-ribs", "pulled-pork", "texas-brisket"])
        brisket = next(r for r in self.service.list_recipes() if r["id"] == "texas-brisket")
        self.assertEqual(brisket["rub"]["name"], "dalmatian")
        p = self.service.plan("texas-brisket", 5.4, self.serve_at)
        self.assertEqual(p["steps"][-1]["step"], "serve")
        with self.assertRaises(ServiceError):
            self.service.plan("texas-brisket", None, self.serve_at)
        with self.assertRaises(ServiceError):
            self.service.plan("nope", 1, self.serve_at)
        with self.assertRaises(ServiceError):
            self.service.plan("texas-brisket", 5, "teatime")

    def test_a_cook_through_the_service(self):
        self.assertIsNone(self.service.status()["cook"])
        with self.assertRaises(ServiceError):
            self.service.log_step("meat-on")
        started = self.service.start("texas-brisket", 5.4, self.serve_at)
        with self.assertRaises(ServiceError):
            self.service.start("texas-brisket", 5.4, self.serve_at)
        with self.assertRaises(ServiceError):
            self.service.log_step("wrap")                        # before meat-on
        smoke = self.service.smoke_test()
        self.assertFalse(smoke["passed"])                       # no 15 minutes of history yet
        self.service.read_now()
        st = self.service.status()["cook"]
        self.assertEqual(st["phase"], "preheating")
        self.assertEqual(st["pipeline"][0], {"key": "smoke-test", "title": "Smoke test", "state": "failed", "at": None})
        self.service.log_step("meat-on")
        with self.assertRaises(ServiceError):
            self.service.log_step("meat-on")
        with self.assertRaises(ServiceError):
            self.service.log_step("flip")
        with self.assertRaises(ServiceError):
            self.service.log_step("note", "  ")
        self.service.log_step("note", "post oak, two chunks")
        st = self.service.status()["cook"]
        self.assertEqual(st["phase"], "cooking")
        self.assertEqual([p["state"] for p in st["pipeline"]][:3], ["failed", "done", "active"])     # the failed smoke test stays on the record
        self.assertIn("meat-probes", [c["name"] for c in st["checks"]])
        self.assertIn("since meat on", self.service.status_text())
        with self.assertRaises(ServiceError):
            self.service.close(score=12)
        closed = self.service.close(score=9, cooked_weight_kg=3.2, notes="good bark")
        self.assertEqual(closed["result"]["yield"], round(3.2 / 5.4, 3))
        cooks = self.service.cooks()
        self.assertEqual(cooks[0]["id"], started["cook"])
        self.assertEqual(cooks[0]["status"], "done")
        detail = self.service.cook(started["cook"])
        self.assertNotIn("recipe", detail["meta"])
        self.assertIn("post oak, two chunks", json.dumps(detail["events"]))
        with self.assertRaises(ServiceError):
            self.service.cook("../etc")

    def test_photo_checks_are_stored_as_data(self):
        with self.assertRaises(ServiceError):
            self.service.record_check("bark", {"colour": "mahogany"})          # no cook open
        started = self.service.start("texas-brisket", 5.4, self.serve_at)
        for kind, data, verdict, wrong in (
                ("crust", {"colour": "dark"}, None, "unknown check"),
                ("bark", {}, None, "needs data"),
                ("bark", {"colour": "purple"}, None, "must be one of"),
                ("bark", {"shine": "high"}, None, "not a field"),
                ("bark", {"coverage_pct": 140}, None, "number from 0 to 100"),
                ("bark", {"colour": "dark"}, "eat", "verdict")):
            with self.assertRaises(ServiceError) as cm:
                self.service.record_check(kind, data, verdict)
            self.assertIn(wrong, str(cm.exception))
        self.service.log_step("meat-on")
        asks = self.service.status()["cook"]["asks"]
        self.assertEqual([a["check"] for a in asks], ["probe"])                # asked to show the probe placement
        self.service.record_check("probe", {"placement": "ok"}, "ok", by="test-model")
        self.assertEqual(self.service.status()["cook"]["asks"], [])
        out = self.service.record_check("bark", {"colour": "mahogany", "set": "firm", "coverage_pct": 90}, "wrap",
                                        note="edges darker than the middle")
        self.assertEqual(out["logged"], "bark check: colour mahogany, set firm, coverage pct 90 -> wrap "
                                        "(edges darker than the middle)")
        cook = self.service.status()["cook"]
        self.assertEqual(cook["photo_checks"]["bark"]["verdict"], "wrap")
        events = self.service.cook(started["cook"])["events"]
        bark = next(e for e in events if e["event"] == "check-bark")
        self.assertEqual(bark["data"], {"check": "bark", "colour": "mahogany", "set": "firm", "coverage_pct": 90.0,
                                        "verdict": "wrap"})
        self.assertIn("bark", self.service.check_vocabulary())

    def test_smoke_photo_completes_the_smoke_test(self):
        self.service.start("texas-brisket", 5.4, self.serve_at)
        before = {c["name"]: c["result"] for c in self.service.smoke_test()["checks"]}
        self.assertEqual(before["clean-smoke"], "skip")
        self.service.record_check("smoke", {"colour": "white"}, "wait")
        self.assertEqual({c["name"]: c["result"] for c in self.service.smoke_test()["checks"]}["clean-smoke"], "fail")
        self.service.record_check("smoke", {"colour": "thin-blue"}, "ok")
        self.assertEqual({c["name"]: c["result"] for c in self.service.smoke_test()["checks"]}["clean-smoke"], "pass")

    def test_probe_selection(self):
        p = self.service._probes
        self.assertEqual((p(None), p(2), p("1, 3,3"), p([4, "2", 4])), ([1], [2], [1, 3], [4, 2]))
        for bad in ("x", [0], [10], "1,two"):
            with self.assertRaises(ServiceError):
                p(bad)
        started = self.service.start("texas-brisket", 5.4, self.serve_at, [1, 2], {"1": " flat ", "3": "ignored"})
        meta = self.service.store().meta(started["cook"])
        self.assertEqual((meta["probes"], meta["probe_labels"]), ([1, 2], {1: "flat"}))
        self.service.read_now()
        probes = self.service.status()["cook"]["probes"]
        self.assertEqual([(x["no"], x["label"]) for x in probes], [(1, "flat"), (2, "probe 2")])
        self.assertEqual(self.service.cook(started["cook"])["meta"]["probes"][0], {"no": 1, "label": "flat"})

    def test_runner_ticks_the_open_cook(self):
        self.service.start("texas-brisket", 5.4, self.serve_at)
        settings.update({"poll_seconds": 3})
        t = self.service.start_runner()
        self.addCleanup(self.service.stop)
        for _ in range(50):
            if self.service.last_reading is not None and self.service.engine().state["recent"]:
                break
            threading.Event().wait(0.1)
        self.assertTrue(self.service.engine().state["recent"])
        self.assertTrue(t.is_alive())

    def test_settings_never_return_secrets(self):
        token = self.service.access_token()
        self.assertEqual(token, self.service.access_token())
        self.service.save_settings({"source": {"url": "http://ha:8123", "token": "s3cret",
                                               "entities": {"pit": "sensor.pit", "probes": ["sensor.a", "", "sensor.c"]}},
                                    "telegram": {"bot_token": "b0t", "chat_id": "42"}, "poll_seconds": 1})
        view = self.service.settings_view()
        self.assertNotIn("s3cret", json.dumps(view))
        self.assertNotIn("b0t", json.dumps(view))
        self.assertTrue(view["source"]["token_set"])
        self.assertEqual(view["source"]["entities"]["probes"], ["sensor.a", "sensor.c"])
        self.assertEqual(view["poll_seconds"], 3)
        self.service.save_settings({"source": {"url": "http://ha2:8123", "token": ""}})
        cfg = settings.load()
        self.assertEqual(cfg["source"]["token"], "s3cret")
        self.assertEqual(cfg["source"]["url"], "http://ha2:8123")
        self.assertEqual(cfg["access"]["token"], token)
        self.assertEqual(os.stat(settings.path()).st_mode & 0o777, 0o600)


class McpTests(Base):
    def call(self, method, params=None, msg_id=1):
        return mcp.handle(self.service, {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})

    def tool(self, name, args=None):
        res = self.call("tools/call", {"name": name, "arguments": args or {}})["result"]
        return res["isError"], res["content"][0]["text"]

    def test_handshake_and_tools(self):
        init = self.call("initialize", {"protocolVersion": "2025-03-26"})["result"]
        self.assertEqual(init["protocolVersion"], "2025-03-26")
        self.assertEqual(self.call("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"],
                         mcp.PROTOCOL_VERSIONS[0])
        self.assertIsNone(mcp.handle(self.service, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in self.call("tools/list")["result"]["tools"]]
        self.assertEqual(len(names), 9)
        for t in self.call("tools/list")["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")
        self.assertEqual(self.call("nope")["error"]["code"], -32601)
        self.assertEqual(self.call("tools/call", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(mcp.handle(self.service, {"id": 1})["error"]["code"], -32600)

    def test_a_cook_through_tools(self):
        err, text = self.tool("list_recipes")
        self.assertFalse(err)
        self.assertNotIn("apiVersion", text)                     # no raw YAML source in the tool result
        err, text = self.tool("plan_cook", {"recipe": "pork-ribs", "serve_at": self.serve_at})
        self.assertIn("serve", text)
        err, text = self.tool("log_step", {"step": "meat-on"})
        self.assertTrue(err)
        self.assertEqual(text, "no cook is open")
        err, text = self.tool("start_cook", {"recipe": "pork-ribs", "serve_at": self.serve_at, "probes": [0]})
        self.assertTrue(err)
        err, text = self.tool("start_cook", {"recipe": "pork-ribs", "serve_at": self.serve_at, "probes": [2, 3],
                                             "probe_labels": {"2": "thick end"}})
        self.assertFalse(err)
        self.assertFalse(self.tool("log_step", {"step": "meat-on"})[0])
        err, text = self.tool("cook_status")
        self.assertIn("since meat on", json.loads(text)["summary"])
        err, text = self.tool("record_check", {"check": "bark", "data": {"colour": "plaid"}})
        self.assertTrue(err)
        self.assertIn("must be one of: pale, tan, mahogany, dark, black", text)
        err, text = self.tool("record_check", {"check": "bark", "data": {"colour": "dark", "set": "tacky"},
                                               "verdict": "wait", "by": "qwen"})
        self.assertFalse(err)
        self.assertFalse(self.tool("close_cook", {"score": 8})[0])
        err, text = self.tool("cook_history")
        cook_id = json.loads(text)[0]["id"]
        err, text = self.tool("cook_history", {"cook": cook_id})
        self.assertIn("meat on", text)


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.service))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.token = self.service.access_token()

    def req(self, path, body=None, token="right"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + (self.token if token == "right" else token)
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=headers)) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else raw), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    def test_page_is_public_but_data_needs_the_token(self):
        code, body, headers = self.req("/", token=None)
        self.assertEqual(code, 200)
        self.assertIn(b"OpenBrisket", body)
        self.assertNotIn(self.token.encode(), body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.req("/app.js", token=None)[0], 200)
        self.assertEqual(self.req("/app.css", token=None)[0], 200)
        self.assertEqual(self.req("/theme.js", token=None)[0], 200)
        self.assertIn(b'data-theme="light"', body)
        self.assertEqual(self.req("/healthz", token=None)[1]["ok"], True)
        self.assertEqual(self.req("/api/status", token=None)[0], 401)
        self.assertEqual(self.req("/api/status", token="wrong")[0], 401)
        self.assertEqual(self.req("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None)[0], 401)
        self.assertEqual(self.req("/api/settings", token="wrong")[0], 401)
        self.assertEqual(self.req("/../../etc/passwd", token=None)[0], 404)

    def test_api_and_mcp_over_http(self):
        self.assertIsNone(self.req("/api/status")[1]["cook"])
        self.assertEqual(len(self.req("/api/recipes")[1]), 3)
        code, body, _ = self.req("/api/plan", {"recipe": "texas-brisket", "serve_at": self.serve_at})
        self.assertEqual(code, 400)
        self.assertIn("weight", body["error"])
        code, body, _ = self.req("/api/cooks", {"recipe": "texas-brisket", "weight_kg": 5.4, "serve_at": self.serve_at})
        self.assertEqual(code, 200)
        cook_id = body["cook"]
        self.assertEqual(self.req("/api/log", {"step": "meat-on"})[0], 200)
        self.assertEqual(self.req("/api/status")[1]["cook"]["id"], cook_id)
        self.assertEqual(self.req("/api/cooks/" + cook_id)[1]["meta"]["id"], cook_id)
        self.assertEqual(self.req("/api/cooks/nope")[0], 400)
        code, body, _ = self.req("/mcp", {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                         "params": {"name": "cook_status", "arguments": {}}})
        self.assertEqual(code, 200)
        self.assertEqual(body["id"], 7)
        self.assertEqual(self.req("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        self.assertEqual(self.req("/api/settings")[1]["access_token"], self.token)
        self.assertEqual(self.req("/api/close", {"score": 8})[1]["status"], "done")
        self.assertEqual(self.req("/api/nope", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
