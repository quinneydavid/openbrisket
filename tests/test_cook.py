import os
import subprocess
import tempfile
import unittest
from datetime import timedelta

from openbrisket import plan, smoketest
from openbrisket.cook import Engine
from openbrisket.notify import MemoryNotifier
from openbrisket.sources import Reading
from openbrisket.store import Store, StoreError
from tests.helpers import START, brisket, cook_readings, scaled


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "cooks"))
        self.recipe = brisket()
        self.notifier = MemoryNotifier()

    def open(self, serve_in_h=24.0, weight=5.4):
        meta = {"recipe_name": self.recipe.name, "weight_kg": weight, "probe": 1,
                "serve_at": (START + timedelta(hours=serve_in_h)).isoformat(), "recipe": self.recipe.raw}
        cook_id = self.store.open_cook(self.recipe.name, START, meta)
        return cook_id, Engine(self.recipe, self.store.meta(cook_id), self.store, self.notifier, cook_id)

    def kinds(self, cook_id):
        return [e["event"] for e in self.store.events(cook_id)]


class StoreTests(Base):
    def test_open_event_close(self):
        cook_id, _ = self.open()
        self.assertEqual(cook_id, "2026-11-13-texas-brisket")
        self.assertEqual(self.store.active(), cook_id)
        with self.assertRaises(StoreError):
            self.store.open_cook("texas-brisket", START, {})
        self.store.add_event(cook_id, START, "note", "spritz with cider vinegar")
        self.store.save_state(cook_id, {"x": 1})
        self.store.close_cook(cook_id, START + timedelta(hours=1), "done", {"score": 9})
        self.assertIsNone(self.store.active())
        self.assertEqual(self.store.meta(cook_id)["result"], {"score": 9})
        log = self.store.log(cook_id)
        self.assertEqual(len(log), 3)
        self.assertIn("merge cook", log[0])
        tracked = subprocess.run(["git", "-C", self.store.root, "ls-files"], capture_output=True, text=True).stdout
        self.assertNotIn("state.json", tracked)
        self.assertIn(f"{cook_id}/events.yaml", tracked)

    def test_a_cook_opened_by_an_older_version_keeps_its_columns(self):
        cook_id, _ = self.open()
        path = os.path.join(self.store.dir(cook_id), "curve.csv")
        with open(path, "w") as f:
            f.write("at,pit,probe1,fan\n")
        self.store.append_curve(cook_id, {"at": "t1", "pit": 120, "probe1": 30, "fan": 40, "ambient": 28, "est_off_min": 9})
        with open(path) as f:
            self.assertEqual(f.read().splitlines(), ["at,pit,probe1,fan", "t1,120,30,40"])

    def test_second_cook_same_day_gets_a_suffix(self):
        a, _ = self.open()
        self.store.close_cook(a, START, "abandoned", {})
        b, _ = self.open()
        self.assertEqual(b, a + "-2")


class EngineTests(Base):
    def run_cook(self, engine, readings, wrap_on_ask=True):
        for r in readings:
            for e in engine.tick(r):
                if e.kind == "wrap-point" and wrap_on_ask:
                    engine.manual("wrap", "", r.at)

    def test_full_cook_leaves_the_expected_notes(self):
        cook_id, engine = self.open()
        engine.manual("meat-on", "", START)
        self.run_cook(engine, cook_readings(engine.prior, step_s=30))
        kinds = self.kinds(cook_id)
        for expected in ("meat-on", "stall-start", "wrap-point", "wrap", "stall-end", "near-target", "target"):
            self.assertIn(expected, kinds)
        self.assertLess(kinds.index("stall-start"), kinds.index("stall-end"))
        self.assertEqual(kinds.count("target"), 1)
        for alarm in ("pit-drift", "fire-dying", "lid-open", "behind", "probe-lost", "no-data"):
            self.assertNotIn(alarm, kinds)
        self.assertTrue(any("CHECK: stall detected" in m for m in self.notifier.sent))
        self.assertIn("bark", [a["check"] for a in engine.state["asks"]])  # it is waiting to be shown the bark
        with open(os.path.join(self.store.dir(cook_id), "curve.csv")) as f:
            rows = f.read().splitlines()
        self.assertEqual(rows[0], "at,pit," + ",".join(f"probe{n}" for n in range(1, 10))
                         + ",fan,pit_target,ambient,humidity,est_off_min")
        mid = dict(zip(rows[0].split(","), rows[300].split(",")))
        self.assertTrue(0 < float(mid["est_off_min"]) < 15 * 60)      # the estimate is logged with every row
        a = engine.analysis(START + timedelta(hours=15))
        self.assertAlmostEqual(a["hours_to_target"], 14.6, delta=0.3)
        self.assertEqual(a["planned_hours"], round(2.7 * 5.4, 2))
        self.assertGreater(a["stall"]["hours"], 1)
        self.assertAlmostEqual(a["pit"]["mean_c"], 121, delta=0.5)
        self.assertAlmostEqual(a["model"]["k_ratio"], 1.0, delta=0.05)  # this cook ran exactly as the recipe assumes
        self.assertLess(abs(a["estimate"]["at_50pct"]["error_h"]), 0.5)
        self.assertEqual(sorted(k for k in a["estimate"] if k.startswith("at_")), ["at_25pct", "at_50pct", "at_75pct"])
        self.assertGreater(len(rows), 600)        # one a minute over a 14 h cook

    def test_state_survives_a_restart(self):
        cook_id, engine = self.open()
        engine.manual("meat-on", "", START)
        readings = list(cook_readings(engine.prior, step_s=60, hours=6.0))
        for r in readings[:200]:
            engine.tick(r)
        again = Engine(self.recipe, self.store.meta(cook_id), self.store, self.notifier, cook_id)
        self.assertEqual(len(again.state["samples"]), len(engine.state["samples"]))
        self.assertEqual(again.state["meat_on_at"], engine.state["meat_on_at"])

    def test_lid_open_is_noticed_and_not_called_drift(self):
        cook_id, engine = self.open()
        engine.manual("meat-on", "", START)

        def pit(t_h):
            return 95.0 if 2.0 <= t_h < 2.05 else 121.0
        self.run_cook(engine, cook_readings(engine.prior, hours=3.0, pit_fn=pit))
        kinds = self.kinds(cook_id)
        self.assertEqual(kinds.count("lid-open"), 1)
        self.assertNotIn("pit-drift", kinds)

    def test_pit_drift_and_dying_fire(self):
        cook_id, engine = self.open()
        engine.manual("meat-on", "", START)

        def pit(t_h):
            return 121.0 if t_h < 1 else max(80.0, 121.0 - (t_h - 1) * 40)

        def fan(t_h):
            return 35.0 if t_h < 1 else 100.0
        self.run_cook(engine, cook_readings(engine.prior, hours=2.5, pit_fn=pit, fan_fn=fan))
        kinds = self.kinds(cook_id)
        self.assertIn("pit-drift", kinds)
        self.assertIn("fire-dying", kinds)
        self.assertNotIn("lid-open", kinds)
        self.assertLessEqual(kinds.count("pit-drift"), 4)      # cooldown, not a flood

    def test_a_fast_cook_is_seen_in_the_analysis(self):
        cook_id, engine = self.open()
        engine.manual("meat-on", "", START)
        last = None
        for r in cook_readings(scaled(engine.prior, 1.3), step_s=60):
            r.ambient, r.humidity = 29.0, 80.0
            for e in engine.tick(r):
                if e.kind == "wrap-point":
                    engine.manual("wrap", "", r.at)
            last = r
        a = engine.analysis(last.at)
        self.assertAlmostEqual(a["model"]["k_ratio"], 1.3, delta=0.1)   # the meat heated 30% faster than the recipe says
        self.assertLess(a["hours_to_target"], a["planned_hours"] - 2)
        self.assertIsNotNone(a["wrapped_at_h"])
        with open(os.path.join(self.store.dir(cook_id), "curve.csv")) as f:
            header, row = f.readline().strip().split(","), f.readline().strip().split(",")
        self.assertEqual((row[header.index("ambient")], row[header.index("humidity")]), ("29.0", "80.0"))

    def test_slow_cook_fails_the_schedule_check(self):
        cook_id, engine = self.open(serve_in_h=16.5)             # plan needs about 14.6 h + rest
        engine.manual("meat-on", "", START)
        self.run_cook(engine, cook_readings(scaled(engine.prior, 0.6), step_s=60, hours=5.0))
        events = self.store.events(cook_id)
        behind = [e for e in events if e["event"] == "behind"]
        self.assertTrue(behind)
        self.assertIn("wrap now", behind[0]["message"])
        self.assertGreater(behind[0]["data"]["late_hours"], 1)

    def test_lost_probe_and_lost_feed(self):
        cook_id, engine = self.open()
        engine.manual("meat-on", "", START)
        for i in range(20):
            engine.tick(Reading(at=START + timedelta(seconds=15 * i), pit=121, probes=[None, None, None], fan=30))
        self.assertIn("probe-lost", self.kinds(cook_id))
        for i in range(20, 40):
            engine.tick(Reading(at=START + timedelta(seconds=15 * i), connected=False))
        self.assertEqual(self.kinds(cook_id).count("no-data"), 1)

    def test_preheat_and_rest_notes(self):
        cook_id, engine = self.open()
        for i in range(70):
            engine.tick(Reading(at=START + timedelta(seconds=15 * i), pit=120, probes=[24, None, None], fan=40))
        self.assertEqual(self.kinds(cook_id).count("pit-ready"), 1)
        engine.manual("meat-on", "", START + timedelta(minutes=20))
        engine.manual("rest", "", START + timedelta(hours=14))
        for minutes in (30, 61, 90, 121, 150):
            engine.tick(Reading(at=START + timedelta(hours=14, minutes=minutes), pit=60, probes=[80, None, None]))
        kinds = self.kinds(cook_id)
        self.assertEqual(kinds.count("rest-min"), 1)
        self.assertEqual(kinds.count("rest-ideal"), 1)

    def test_several_probes_the_coldest_one_governs(self):
        meta = {"recipe_name": self.recipe.name, "weight_kg": 5.4, "probes": [1, 2, 4],
                "probe_labels": {1: "flat", 2: "point"}, "serve_at": (START + timedelta(hours=24)).isoformat(),
                "recipe": self.recipe.raw}
        cook_id = self.store.open_cook(self.recipe.name, START, meta)
        engine = Engine(self.recipe, self.store.meta(cook_id), self.store, self.notifier, cook_id)
        self.assertEqual(engine.probe_nos, [1, 2, 4])
        engine.manual("meat-on", "", START)
        last = None
        for r in cook_readings(engine.prior, step_s=60, hours=13.0):
            m = r.probes[0]
            # probe 1 (flat) is the coldest; point runs 6 C ahead, probe 4 3 C ahead; probe 3 is not in the meat
            r.probes = [m, m + 6, 150.0, m + 3]
            engine.tick(r)
            last = r
        self.assertEqual(engine.coldest(last), last.probes[0])
        self.assertNotIn("target", self.kinds(cook_id))          # the point passed 95 C, the flat has not
        self.assertGreater(last.probes[1], 95)
        self.assertLess(last.probes[0], 95)
        o = engine.overview(last.at, last)
        self.assertEqual([p["label"] for p in o["probes"]], ["flat", "point", "probe 4"])
        self.assertEqual(o["meat_c"], last.probes[0])
        check = next(c for c in o["checks"] if c["name"] == "meat-probes")
        self.assertEqual(check["state"], "pass")
        self.assertIn("3 probes", check["detail"])
        self.assertIn("coldest of flat", engine.status(last.at, last))
        last.probes = [last.probes[0], None, 150.0, last.probes[3]]
        check = next(c for c in engine.live_checks(last.at, last) if c["name"] == "meat-probes")
        self.assertEqual((check["state"], check["detail"]), ("fail", "not reading: point"))
        self.assertEqual(engine.coldest(last), last.probes[0])   # the others still carry the cook
        with open(os.path.join(self.store.dir(cook_id), "curve.csv")) as f:
            header, row = f.readline().strip().split(","), f.readline().strip().split(",")
        self.assertEqual(row[header.index("probe3")], "150.0")   # every probe is recorded, in the meat or not

    def test_status_text(self):
        cook_id, engine = self.open()
        engine.manual("meat-on", "", START)
        last = None
        for r in cook_readings(engine.prior, step_s=60, hours=4.0):
            engine.tick(r)
            last = r
        text = engine.status(last.at, last)
        self.assertIn("since meat on", text)
        self.assertIn("estimate: off the pit about", text)
        self.assertIn("on schedule", text)


class SmokeTestTests(Base):
    def recent(self, pit, minutes=16, fan=40.0):
        return [[(START + timedelta(seconds=15 * i)).isoformat(), pit, fan] for i in range(minutes * 4 + 1)]

    def test_pass(self):
        now = START + timedelta(minutes=16)
        r = Reading(at=now, pit=120, probes=[26, None, None], fan=40)
        checks = smoketest.run(self.recipe, r, self.recent(120), self.notifier, now=now)
        self.assertTrue(smoketest.passed(checks), smoketest.render(checks))
        self.assertEqual({c.name: c.result for c in checks}["clean-smoke"], "skip")
        self.assertIn("put the meat on", smoketest.render(checks))
        self.assertEqual(len(self.notifier.sent), 1)

    def test_failures(self):
        now = START + timedelta(minutes=16)
        r = Reading(at=now, pit=100, probes=[80, None, None], fan=100)
        got = {c.name: c.result for c in smoketest.run(self.recipe, r, self.recent(100, fan=100.0), None, now=now)}
        self.assertEqual(got["pit-stable"], "fail")
        self.assertEqual(got["fan"], "fail")
        self.assertEqual(got["probes"], "fail")
        short = smoketest.run(self.recipe, r, self.recent(120, minutes=3), None, now=START + timedelta(minutes=3))
        self.assertIn("history", {c.name: c.detail for c in short}["pit-stable"])

    def test_every_meat_probe_is_checked(self):
        now = START + timedelta(minutes=16)
        r = Reading(at=now, pit=120, probes=[26, None, 25, 90], fan=40)
        ok = {c.name: c for c in smoketest.run(self.recipe, r, self.recent(120), None, [1, 3], now=now)}
        self.assertEqual(ok["probes"].result, "pass")
        dead = {c.name: c for c in smoketest.run(self.recipe, r, self.recent(120), None, [1, 2], now=now)}
        self.assertEqual((dead["probes"].result, "2 not reading" in dead["probes"].detail), ("fail", True))
        hot = {c.name: c for c in smoketest.run(self.recipe, r, self.recent(120), None, [1, 4], now=now)}
        self.assertIn("4 (90 C) is hot", hot["probes"].detail)

    def test_no_feed(self):
        checks = smoketest.run(self.recipe, Reading(at=START, connected=False), [], None)
        self.assertFalse(smoketest.passed(checks))
        self.assertEqual(checks[0].name, "data-feed")


if __name__ == "__main__":
    unittest.main()
