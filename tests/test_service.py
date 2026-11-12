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
        self.library = os.path.join(self.tmp.name, "recipes")
        self.service = Service([os.path.join(ROOT, "recipes"), self.library])
        self.serve_at = (datetime.now() + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M")


class ServiceTests(Base):
    def test_recipes_and_plan(self):
        ids = [r["id"] for r in self.service.list_recipes()]
        self.assertEqual(ids, ["chicken-wings", "pork-ribs", "pulled-pork", "ribs-3-2-1", "sausages", "smoked-salmon",
                               "texas-brisket", "tri-tip", "whole-chicken"])
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
        self.assertEqual(detail["meta"]["analysis"]["events"], {})
        self.assertIn("meat_on_at", detail["meta"]["analysis"])
        self.assertNotIn("recipe", detail["meta"])
        self.assertIn("post oak, two chunks", json.dumps(detail["events"]))
        with self.assertRaises(ServiceError):
            self.service.cook("../etc")

    RUB = {"name": "Coffee Chilli", "description": "Dark and a little bitter.", "ingredients": [
        {"name": "ground coffee", "parts": 2}, {"name": "ancho chilli", "parts": 2},
        {"name": "kosher salt", "parts": 2, "kind": "salt"}, {"name": "brown sugar", "parts": 3, "kind": "sugar"},
        {"name": "", "parts": ""}]}
    RECIPE = {"name": "Beef Short Ribs", "cut": "beef-short-ribs", "rub": "coffee-chilli", "pit_target_c": 135,
              "meat_target_c": 96, "hours_fixed": 8, "wrap_at_c": 77, "wrap_material": "butcher paper",
              "rest_min_minutes": 30, "rest_ideal_minutes": 60, "description": "Plate ribs."}

    def test_own_rub_and_recipe_are_yaml_with_history(self):
        import subprocess
        import yaml
        with self.assertRaises(ServiceError) as cm:
            self.service.save_recipe(self.RECIPE)                       # the rub does not exist yet
        self.assertIn("save the rub first", str(cm.exception))
        out = self.service.save_rub(self.RUB)
        self.assertEqual((out["id"], out["message"]), ("coffee-chilli", "added"))
        self.assertEqual(self.service.save_rub(self.RUB)["message"], "no change")
        with open(os.path.join(self.library, "rubs", "coffee-chilli.yaml")) as f:
            doc = yaml.safe_load(f)
        self.assertEqual(doc["kind"], "Rub")
        self.assertEqual(len(doc["ingredients"]), 4)                    # the empty form row is dropped
        out = self.service.save_recipe(self.RECIPE)
        self.assertEqual((out["id"], out["version"], out["message"]), ("beef-short-ribs", "1.0.0", "added"))
        self.assertEqual(out["warnings"], [])
        hotter = dict(self.RECIPE, pit_target_c=150, change="hotter pit for a shorter cook")
        out = self.service.save_recipe(hotter)
        self.assertEqual(out["version"], "1.0.1")                       # changed -> new version number
        self.assertIn("sugar scorches", out["warnings"][0])
        self.assertEqual(self.service.save_recipe(dict(hotter, version="1.0.1"))["message"], "no change")
        mine = {r["id"]: r for r in self.service.list_recipes()}
        self.assertTrue(mine["beef-short-ribs"]["own"])
        self.assertFalse(mine["texas-brisket"]["own"])
        self.assertEqual(mine["beef-short-ribs"]["rub"]["id"], "coffee-chilli")
        rubs = {r["id"]: r for r in self.service.list_rubs()}
        self.assertEqual(rubs["coffee-chilli"]["used_by"], ["beef-short-ribs"])
        self.assertAlmostEqual(rubs["coffee-chilli"]["sugar"], 3 / 9)
        self.assertFalse(rubs["dalmatian"]["own"])
        log = subprocess.run(["git", "-C", self.library, "log", "--format=%s"], capture_output=True, text=True).stdout
        self.assertIn("recipe beef-short-ribs 1.0.1: hotter pit for a shorter cook", log)
        hist = self.service.item_history("recipe", "beef-short-ribs")
        self.assertEqual([(h["version"], h["created"]) for h in hist], [("1.0.1", False), ("1.0.0", True)])
        self.assertEqual(hist[0]["message"], "recipe beef-short-ribs 1.0.1: hotter pit for a shorter cook")
        self.assertEqual(hist[0]["changes"], [{"field": "Pit temperature", "before": "135", "after": "150"}])
        self.assertEqual(hist[1]["changes"], [])
        self.assertEqual(self.service.plan("beef-short-ribs", None, self.serve_at)["steps"][-1]["step"], "serve")
        with self.assertRaises(ServiceError) as cm:
            self.service.delete_item("rub", "coffee-chilli")
        self.assertIn("uses this rub", str(cm.exception))
        self.service.delete_item("recipe", "beef-short-ribs")
        self.service.delete_item("rub", "coffee-chilli")
        with self.assertRaises(ServiceError):
            self.service.delete_item("recipe", "texas-brisket")          # shipped recipes cannot be deleted

    def test_rub_ratios_have_versions_and_a_readable_history(self):
        first = self.service.save_rub(self.RUB)
        self.assertEqual(first["version"], "1.0.0")
        less_sugar = dict(self.RUB, ingredients=[
            {"name": "ground coffee", "parts": 2}, {"name": "ancho chilli", "parts": 2},
            {"name": "kosher salt", "parts": 2, "kind": "salt"}, {"name": "brown sugar", "parts": 1.5, "kind": "sugar"},
            {"name": "smoked paprika", "parts": 1}], change="bark burnt, halve the sugar")
        out = self.service.save_rub(less_sugar)
        self.assertEqual((out["version"], out["message"]), ("1.0.1", "updated to 1.0.1"))
        hist = self.service.item_history("rub", "coffee-chilli")
        self.assertEqual(hist[0]["message"], "rub coffee-chilli 1.0.1: bark burnt, halve the sugar")
        self.assertEqual(hist[0]["changes"], [
            {"field": "brown sugar", "before": "3 parts (sugar)", "after": "1.5 parts (sugar)"},
            {"field": "smoked paprika", "before": None, "after": "1 part"}])
        self.assertEqual({r["id"]: r["version"] for r in self.service.list_rubs()}["coffee-chilli"], "1.0.1")
        self.assertEqual({r["id"]: r["version"] for r in self.service.list_rubs()}["dalmatian"], "1.0.0")

    def test_results_are_grouped_by_recipe_version(self):
        self.service.save_rub(self.RUB)
        self.service.save_recipe(self.RECIPE)
        def cook(score):
            started = self.service.start("beef-short-ribs", None, self.serve_at)
            self.service.log_step("meat-on")
            self.service.close(score=score, notes="n")
            return started["cook"]
        a = cook(6)
        self.assertEqual(self.service.store().meta(a)["rub"]["version"], "1.0.0")
        self.assertEqual(len(self.service.store().meta(a)["rub"]["ingredients"]), 4)
        out = self.service.save_recipe(dict(self.RECIPE, meat_target_c=94, change="pull a little earlier"))
        self.assertEqual(out["version"], "1.0.1")
        b, c = cook(8), cook(9)
        # a later change sent without a version (as an agent does) carries on from 1.0.1, it never restarts at 1.0.0
        self.assertEqual(self.service.save_recipe(dict(self.RECIPE, meat_target_c=93))["version"], "1.0.2")
        self.assertEqual(self.service.save_recipe(dict(self.RECIPE, meat_target_c=93))["message"], "no change")
        self.service.save_recipe(dict(self.RECIPE, meat_target_c=94, version="1.0.2"))
        self.service.start("beef-short-ribs", None, self.serve_at)
        self.service.close(abandon=True)                         # an abandoned cook is not a result
        res = {r["id"]: r for r in self.service.list_recipes()}["beef-short-ribs"]["results"]
        self.assertEqual([(v["version"], v["count"], v["avg_score"], v["best_score"]) for v in res],
                         [("1.0.0", 1, 6.0, 6.0), ("1.0.1", 2, 8.5, 9.0)])
        self.assertEqual({r["id"]: r for r in self.service.list_recipes()}["beef-short-ribs"]["version"], "1.0.3")
        self.assertEqual({x["id"] for x in res[1]["cooks"]}, {b, c})
        self.assertEqual(res[0]["cooks"][0]["rub_version"], "1.0.0")
        self.assertEqual({r["id"]: r for r in self.service.list_recipes()}["texas-brisket"]["results"], [])

    def test_bad_recipes_are_refused_with_the_reason(self):
        self.service.save_rub(self.RUB)
        for change, wrong in (({"cut": "tofu-block"}, "must start with"),
                              ({"cut": "chicken-thighs", "meat_target_c": 65, "wrap_at_c": None}, "food-safety floor of 74"),
                              ({"hours_per_kg": 2}, "exactly one of"),
                              ({"pit_target_c": "hot"}, "must be a number"),
                              ({"name": " "}, "needs a name")):
            with self.assertRaises(ServiceError) as cm:
                self.service.save_recipe(dict(self.RECIPE, **change))
            self.assertIn(wrong, str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.library, "beef-short-ribs.yaml")))
        with self.assertRaises(ServiceError):
            self.service.save_rub({"name": "empty", "ingredients": []})

    def test_own_copy_replaces_a_shipped_recipe_and_keeps_its_rub(self):
        out = self.service.save_recipe({"name": "texas-brisket", "cut": "beef-brisket-packer", "rub": "dalmatian",
                                        "pit_target_c": 110, "meat_target_c": 95, "hours_per_kg": 3.3,
                                        "wrap_at_c": 74, "version": "1.0.0", "change": "lower and slower"})
        self.assertTrue(out["saved"])
        mine = {r["id"]: r for r in self.service.list_recipes()}["texas-brisket"]
        self.assertTrue(mine["own"])
        self.assertEqual(mine["pit_target_c"], 110)
        self.assertEqual(mine["rub"]["name"], "dalmatian")              # found in the shipped rubs
        self.service.delete_item("recipe", "texas-brisket")
        self.assertEqual({r["id"]: r for r in self.service.list_recipes()}["texas-brisket"]["pit_target_c"], 121)

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

    def test_a_probe_can_be_taken_out_and_put_back_by_hand(self):
        self.service.start("texas-brisket", 5.4, self.serve_at, [1, 2])
        self.service.log_step("meat-on")
        out = self.service.set_probe(2, False)
        self.assertEqual(out["in_meat"], [1])
        self.assertEqual(self.service.set_probe(2, False)["logged"], "no change")
        self.service.read_now()
        probes = {p["no"]: p["in_meat"] for p in self.service.status()["cook"]["probes"]}
        self.assertEqual(probes, {1: True, 2: False})
        self.assertEqual(self.service.set_probe("2", True)["in_meat"], [1, 2])
        with self.assertRaises(ServiceError):
            self.service.set_probe(7, False)

    def test_a_cook_with_no_probe_and_a_recipe_saved_with_a_style(self):
        started = self.service.start("chicken-wings", None, self.serve_at)       # quick: no probe by default
        self.assertEqual(self.service.store().meta(started["cook"])["probes"], [])
        self.service.log_step("meat-on")
        self.service.read_now()
        cook = self.service.status()["cook"]
        self.assertEqual((cook["style"], cook["done_by"], cook["rests"], cook["probes"]), ("quick", "time", False, []))
        self.assertEqual(cook["pipeline"][-1]["title"], "Time up")
        self.service.close(score=8)
        with self.assertRaises(ServiceError):
            self.service.start("texas-brisket", 5.4, self.serve_at, "x")
        self.assertEqual(self.service._probes([], "needed"), [1])                # a brisket always gets a probe
        self.assertEqual(self.service._probes([], "optional"), [])
        self.assertEqual(self.service._probes(None, "optional"), [1])
        out = self.service.save_recipe({
            "name": "Pork Belly Burnt Ends", "cut": "pork-belly", "style": "ribs", "pit_target_c": 120,
            "meat_target_c": 93, "stages": [{"name": "smoke", "hours": 3}, {"name": "sauced", "hours": 1.5,
                                                                              "note": "cube, sauce, back on in a tray"},
                                             {"name": "", "hours": ""}],
            "finish": {"what": "glaze", "minutes": 10, "minutes_before_end": 10}, "rest_min_minutes": 0,
            "rest_ideal_minutes": 0})
        self.assertTrue(out["saved"], out)
        with open(os.path.join(self.library, "pork-belly-burnt-ends.yaml")) as f:
            import yaml
            doc = yaml.safe_load(f)
        self.assertEqual((doc["style"], doc["rest"], len(doc["stages"]), doc["finish"]["what"]), ("ribs", "none", 2, "glaze"))
        self.assertNotIn("stall", doc)                                           # the style already says no stall
        self.assertNotIn("done_by", doc)
        saved = {r["id"]: r for r in self.service.list_recipes()}["pork-belly-burnt-ends"]
        self.assertEqual((saved["style"], saved["done_by"], saved["stall"], saved["rest_ideal_minutes"]),
                         ("ribs", "bend", False, 0))
        self.assertEqual(self.service.plan("pork-belly-burnt-ends", None, self.serve_at)["cook_hours"], 4.5)
        with self.assertRaises(ServiceError) as cm:
            self.service.save_recipe({"name": "wings2", "cut": "chicken-wings", "style": "quick", "pit_target_c": 135,
                                      "hours_fixed": 1.5})
        self.assertIn("meat.target_c is missing", str(cm.exception))            # chicken always needs its check
        hist = self.service.item_history("recipe", "pork-belly-burnt-ends")
        self.service.save_recipe({
            "name": "Pork Belly Burnt Ends", "cut": "pork-belly", "style": "ribs", "pit_target_c": 120,
            "meat_target_c": 93, "stages": [{"name": "smoke", "hours": 2.5}, {"name": "sauced", "hours": 1.5}],
            "rest_min_minutes": 0, "rest_ideal_minutes": 0})
        changes = {c["field"]: (c["before"], c["after"]) for c in self.service.item_history(
            "recipe", "pork-belly-burnt-ends")[0]["changes"]}
        self.assertEqual(changes["stage 1"], ("smoke, 3 h", "smoke, 2.5 h"))
        self.assertEqual(changes["Finish"], ("glaze", None))
        self.assertEqual(len(hist), 1)

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
        self.assertEqual(self.service.cook(started["cook"])["meta"]["probes"][0]["label"], "flat")

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
        self.assertEqual(view["source"]["entities"]["probe_names"], ["", ""])
        self.service.save_settings({"source": {"entities": {"probes": ["sensor.fan_probe_1", "", "sensor.thermo_probe_1"],
                                                            "probe_names": ["Fan controller 1", "skipped", ""]}}})
        self.assertEqual(self.service.probe_sources(), [
            {"no": 1, "entity": "sensor.fan_probe_1", "name": "Fan controller 1"},
            {"no": 2, "entity": "sensor.thermo_probe_1", "name": "thermo probe 1"}])    # unnamed: readable entity id
        started = self.service.start("texas-brisket", 5.4, self.serve_at, [1, 2], {"1": "flat"})
        self.service.read_now()
        st = self.service.status()
        self.assertEqual([p["source"] for p in st["cook"]["probes"]], ["Fan controller 1", "thermo probe 1"])
        self.assertEqual(st["probe_sources"][0]["name"], "Fan controller 1")
        self.assertEqual(self.service.cook(started["cook"])["meta"]["probes"][0],
                         {"no": 1, "label": "flat", "source": "Fan controller 1"})
        self.service.close(abandon=True)
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
        self.assertEqual(len(names), 14)
        for t in self.call("tools/list")["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")
        self.assertEqual(self.call("nope")["error"]["code"], -32601)
        self.assertEqual(self.call("tools/call", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(mcp.handle(self.service, {"id": 1})["error"]["code"], -32600)

    def test_a_cook_through_tools(self):
        err, text = self.tool("list_recipes")
        self.assertFalse(err)
        self.assertNotIn("apiVersion", text)                     # no raw YAML source in the tool result
        err, text = self.tool("save_rub", {"name": "sp", "ingredients": [{"name": "salt", "parts": 1, "kind": "salt"},
                                                                         {"name": "pepper", "parts": 1}]})
        self.assertFalse(err, text)
        err, text = self.tool("save_recipe", {"name": "lamb-shoulder", "cut": "lamb-shoulder", "rub": "sp",
                                              "pit_target_c": 130, "meat_target_c": 93, "hours_per_kg": 3})
        self.assertFalse(err, text)
        self.assertIn("lamb-shoulder", self.tool("list_recipes")[1])
        self.assertIn('"sp"', self.tool("list_rubs")[1])
        err, text = self.tool("save_recipe", {"name": "raw-chicken", "cut": "chicken-whole", "pit_target_c": 120,
                                              "meat_target_c": 60, "hours_fixed": 3})
        self.assertTrue(err)
        self.assertIn("food-safety floor", text)
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
        self.assertEqual(self.req("/favicon.svg", token=None)[0], 200)
        self.assertIn(b"BBQ as Code", body)
        self.assertIn(b'data-theme="light"', body)
        self.assertEqual(self.req("/healthz", token=None)[1]["ok"], True)
        self.assertEqual(self.req("/api/status", token=None)[0], 401)
        self.assertEqual(self.req("/api/status", token="wrong")[0], 401)
        self.assertEqual(self.req("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None)[0], 401)
        self.assertEqual(self.req("/api/settings", token="wrong")[0], 401)
        self.assertEqual(self.req("/../../etc/passwd", token=None)[0], 404)

    def test_every_route_is_in_the_api_description(self):
        import re
        from openbrisket import apidoc
        code, spec, _ = self.req("/api/openapi.json", token=None)          # public, like /healthz
        self.assertEqual((code, spec["openapi"][:3]), (200, "3.0"))
        with open(os.path.join(ROOT, "openbrisket", "server.py")) as f:
            src = f.read()
        served = set(re.findall(r'path == "(/[a-z/\-.]+)"', src))
        served |= {"/api/cooks/{id}", "/api/history/{kind}/{id}"}           # the two routes matched by prefix
        listed = {p for _m, p, *_ in apidoc.ENDPOINTS}
        self.assertEqual(served - listed, set(), "a route the server answers is missing from apidoc.ENDPOINTS")
        self.assertEqual(listed - served, set(), "apidoc.ENDPOINTS lists a route the server does not answer")
        self.assertNotIn("security", spec["paths"]["/healthz"]["get"])
        self.assertEqual(spec["paths"]["/api/status"]["get"]["security"], [{"token": []}])
        self.assertEqual(spec["paths"]["/api/cooks/{id}"]["get"]["parameters"][0]["name"], "id")
        self.assertIn("example", spec["paths"]["/api/plan"]["post"]["requestBody"]["content"]["application/json"])

    def test_api_and_mcp_over_http(self):
        self.assertIsNone(self.req("/api/status")[1]["cook"])
        self.assertEqual(len(self.req("/api/recipes")[1]), 9)
        self.assertEqual(self.req("/api/styles")[1]["quick"]["probe"], "none")
        self.assertEqual(len(self.req("/api/rubs")[1]), 2)
        code, body, _ = self.req("/api/rubs", {"name": "plain", "ingredients": [{"name": "salt", "parts": 1}]})
        self.assertEqual((code, body["id"]), (200, "plain"))
        self.assertEqual(self.req("/api/history/rub/plain")[1][0]["message"], "rub plain 1.0.0: add")
        self.assertEqual(self.req("/api/delete", {"kind": "rub", "id": "plain"})[1]["deleted"], "plain")
        self.assertEqual(self.req("/api/recipes", {"name": "x"})[0], 400)
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
