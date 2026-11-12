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
        with open(os.path.join(self.store.dir(cook_id), "curve.csv")) as f:
            rows = f.read().splitlines()
        self.assertEqual(rows[0], "at,pit,probe1,probe2,probe3,fan,pit_target")
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

    def test_no_feed(self):
        checks = smoketest.run(self.recipe, Reading(at=START, connected=False), [], None)
        self.assertFalse(smoketest.passed(checks))
        self.assertEqual(checks[0].name, "data-feed")


if __name__ == "__main__":
    unittest.main()
