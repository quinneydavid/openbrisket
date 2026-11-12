"""Recipes for different kinds of food: what the plan and the cook engine do for each style."""
import copy
import os
import tempfile
import unittest
from datetime import timedelta

import yaml

from openbrisket import plan, recipes, smoketest
from openbrisket.cook import Engine
from openbrisket.notify import MemoryNotifier
from openbrisket.sources import Reading
from openbrisket.store import Store
from tests.helpers import ROOT, START, cook_readings


def load(name):
    return recipes.load_recipe(os.path.join(ROOT, "recipes", name + ".yaml"))


class StyleRecipeTests(unittest.TestCase):
    def test_defaults_come_from_the_style(self):
        expect = {"texas-brisket": ("low-and-slow", "feel", "needed", True, 120),
                  "pork-ribs": ("ribs", "bend", "optional", True, 20),        # its file asks for a stall explicitly
                  "ribs-3-2-1": ("ribs", "bend", "optional", False, 20),
                  "whole-chicken": ("poultry", "temperature", "needed", False, 15),
                  "chicken-wings": ("quick", "time", "none", False, 0),
                  "tri-tip": ("roast", "temperature", "needed", False, 15),
                  "smoked-salmon": ("fish", "temperature", "optional", False, 0),
                  "sausages": ("sausage", "temperature", "optional", False, 5)}
        for name, want in expect.items():
            r = load(name)
            self.assertEqual((r.style, r.done_by, r.probe, r.stall_enabled, r.rest_ideal_minutes), want, name)
        self.assertEqual(load("ribs-3-2-1").cook_hours(None), 6.0)
        self.assertTrue(load("chicken-wings").timed)
        self.assertFalse(load("tri-tip").timed)

    def doc(self, name):
        with open(os.path.join(ROOT, "recipes", name + ".yaml")) as f:
            return yaml.safe_load(f)

    def problems(self, name, **change):
        d = self.doc(name)
        for key, value in change.items():
            if value is None:
                d.pop(key, None)
            else:
                d[key] = value
        return recipes.validate_recipe(d)

    def test_validation_of_the_style_fields(self):
        self.assertTrue(any("style must be one of" in p for p in self.problems("sausages", style="stew")))
        self.assertTrue(any("needs a probe" in p for p in self.problems("chicken-wings", done_by="temperature")))
        # wings run by the clock but are chicken: the temperature to check at the end is still required and floored
        self.assertTrue(any("meat.target_c is missing" in p for p in self.problems("chicken-wings", meat=None)))
        self.assertTrue(any("food-safety floor of 74" in p for p in self.problems("chicken-wings", meat={"target_c": 70})))
        self.assertTrue(any("exactly one of" in p for p in self.problems("ribs-3-2-1", timing={"hours_fixed": 6})))
        self.assertTrue(any("stages[1].hours" in p for p in self.problems(
            "ribs-3-2-1", stages=[{"name": "smoke", "hours": 3}, {"name": "wrapped"}])))
        self.assertTrue(any("exactly one of at_c" in p for p in self.problems(
            "tri-tip", finish={"what": "sear", "minutes": 4})))
        self.assertTrue(any("finish.what" in p for p in self.problems("tri-tip", finish={"what": "boil", "minutes": 4})))
        self.assertTrue(any("pull_c" in p for p in self.problems("tri-tip", meat={"target_c": 54, "pull_c": 40})))
        self.assertEqual(self.problems("tri-tip", meat={"target_c": 54, "pull_c": 51}), [])
        veg = {"apiVersion": "openbrisket/v1", "kind": "Recipe", "name": "smoked-nuts", "version": "1.0.0",
               "cut": "nut-almonds", "style": "quick", "pit": {"target_c": 110}, "timing": {"hours_fixed": 1}}
        self.assertEqual(recipes.validate_recipe(veg), [])          # no meat, no floor, no target needed
        self.assertIsNone(recipes.from_doc(veg).meat_target_c)

    def test_poultry_skin_warning(self):
        d = self.doc("whole-chicken")
        self.assertEqual(recipes.warnings(d, None), [])
        d["pit"]["target_c"] = 110
        self.assertIn("rubbery skin", recipes.warnings(d, None)[0])


class StylePlanTests(unittest.TestCase):
    SERVE = START + timedelta(hours=40)

    def steps(self, name, weight=None):
        p = plan.make_plan(load(name), weight, self.SERVE)
        self.assertEqual([s.at for s in p.steps], sorted(s.at for s in p.steps))
        return p, [s.what for s in p.steps]

    def test_wings_run_by_the_clock(self):
        p, names = self.steps("chicken-wings")
        self.assertEqual(names, ["light the fire", "out of the fridge", "meat on", "crisp", "time is up", "serve"])
        self.assertEqual(self.SERVE - p.meat_on, timedelta(hours=1.5))      # no rest, no slack
        self.assertIn("at least 80 C", p.steps[-2].detail)

    def test_ribs_stages(self):
        p, names = self.steps("ribs-3-2-1")
        self.assertEqual(names[3:6], ["stage: wrapped", "stage: glaze", "bend test"])
        self.assertNotIn("stall expected", names)
        at = {s.what: s.at for s in p.steps}
        self.assertEqual(at["stage: wrapped"] - p.meat_on, timedelta(hours=3))
        self.assertEqual(at["stage: glaze"] - p.meat_on, timedelta(hours=5))

    def test_roast_and_fish(self):
        _, names = self.steps("tri-tip", 1.2)
        self.assertIn("sear", names)
        self.assertNotIn("wrap", names)
        self.assertIn("at temperature (expected)", names)
        _, names = self.steps("smoked-salmon")
        self.assertNotIn("off and resting (latest)", names)
        self.assertEqual(names[-2:], ["at temperature (expected)", "serve"])

    def test_brisket_is_unchanged(self):
        _, names = self.steps("texas-brisket", 5.4)
        self.assertEqual(names, ["salt / dry brine", "out of the fridge", "light the fire", "meat on", "stall expected",
                                 "wrap", "probe tender (expected)", "off and resting (latest)", "serve"])


class StyleEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "cooks"))
        self.notifier = MemoryNotifier()

    def open(self, name, probes, weight=None, serve_in_h=30.0):
        recipe = load(name)
        meta = {"recipe_name": recipe.name, "weight_kg": weight, "probes": probes,
                "serve_at": (START + timedelta(hours=serve_in_h)).isoformat(), "recipe": copy.deepcopy(recipe.raw)}
        cook_id = self.store.open_cook(recipe.name, START, meta)
        engine = Engine(recipe, self.store.meta(cook_id), self.store, self.notifier, cook_id)
        engine.manual("meat-on", "", START)
        return cook_id, engine

    def kinds(self, cook_id):
        return [e["event"] for e in self.store.events(cook_id)]

    def test_wings_with_no_probe(self):
        cook_id, engine = self.open("chicken-wings", [])
        self.assertEqual(engine.probe_nos, [])
        last = None
        for minute in range(0, 100):
            last = Reading(at=START + timedelta(minutes=minute), pit=135, probes=[28, 28, 28], fan=40)
            engine.tick(last)
        kinds = self.kinds(cook_id)
        self.assertEqual([k for k in kinds if k in ("finish", "time-up")], ["finish", "time-up"])
        for never in ("probe-lost", "stall-start", "target", "near-target", "behind", "probe-out"):
            self.assertNotIn(never, kinds)
        events = {e["event"]: e["message"] for e in self.store.events(cook_id)}
        self.assertIn("time to crisp it up, 10 min with the pit at 200 C", events["finish"])
        self.assertIn("at least 80 C", events["time-up"])
        self.assertIn("serve, and close the cook", events["time-up"])
        o = engine.overview(last.at, last)
        self.assertEqual([p["title"] for p in o["pipeline"]], ["Smoke test", "Meat on", "Crisp", "Time up"])
        self.assertEqual(o["probes"], [])
        self.assertIsNone(o["meat_c"])
        checks = {c["name"]: c for c in o["checks"]}
        self.assertIn("clock", checks)
        self.assertNotIn("meat-probes", checks)
        early = Reading(at=START + timedelta(minutes=30), pit=135, probes=[], fan=40)
        off, _proj, fit = engine.projection(early.at)
        self.assertEqual(off, START + timedelta(hours=1.5))
        self.assertEqual(fit.weight, 0.0)
        smoke = {c.name: c.result for c in smoketest.run(engine.recipe, early, [], None, engine.probe_nos)}
        self.assertEqual(smoke["probes"], "skip")
        self.assertIn("estimate: off the pit about", engine.status(early.at, early))

    def test_ribs_stages_and_bend(self):
        cook_id, engine = self.open("ribs-3-2-1", [1])
        for minute in range(0, 6 * 60 + 5, 2):
            meat = min(20 + minute * 0.22, 94)
            engine.tick(Reading(at=START + timedelta(minutes=minute), pit=110, probes=[meat, None, None], fan=40))
        events = [(e["event"], e["message"]) for e in self.store.events(cook_id)]
        stages = [m for k, m in events if k == "stage"]
        self.assertEqual(len(stages), 2)
        self.assertIn("stage 2 of 3: wrapped, 2 h. wrap in foil", stages[0])
        self.assertIn("stage 3 of 3: glaze", stages[1])
        kinds = [k for k, _ in events]
        self.assertNotIn("stall-start", kinds)                      # ribs: no stall watch unless the recipe asks
        self.assertIn("time-up", kinds)
        self.assertIn("Do the bend test", dict(events)["time-up"])
        self.assertIn("bend test", dict(events)["target"])           # reaching 93 C is a guide, not the end

    def test_chicken_is_done_by_temperature_with_no_stall(self):
        cook_id, engine = self.open("whole-chicken", [1], weight=1.8)
        self.assertGreater(engine.prior.stall_lo, 1000)             # the model has no stall band for poultry
        for r in cook_readings(engine.prior, pit=150.0, target=74.0, wrap_at=None, step_s=60, hours=4):
            engine.tick(r)
        kinds = self.kinds(cook_id)
        self.assertNotIn("stall-start", kinds)
        self.assertNotIn("wrap-point", kinds)
        self.assertEqual(kinds.count("target"), 1)
        msg = next(e["message"] for e in self.store.events(cook_id) if e["event"] == "target")
        self.assertIn("take it off and log the rest", msg)
        self.assertNotIn("butter", msg)
        hours = engine.analysis(START + timedelta(hours=3))["hours_to_target"]
        self.assertAlmostEqual(hours, 0.9 * 1.8, delta=0.15)

    def test_tri_tip_sear_then_temperature(self):
        cook_id, engine = self.open("tri-tip", [1], weight=1.2)
        for r in cook_readings(engine.prior, pit=110.0, target=54.0, wrap_at=None, step_s=60, hours=3):
            engine.tick(r)
        kinds = [k for k in self.kinds(cook_id) if k in ("finish", "near-target", "target")]
        self.assertEqual(kinds, ["finish", "near-target", "target"])
        msg = next(e["message"] for e in self.store.events(cook_id) if e["event"] == "finish")
        self.assertIn("meat is at 46 C: time to sear it, 4 min with the pit at 260 C", msg)

    def test_pull_temperature_carries_over(self):
        recipe = load("tri-tip")
        doc = copy.deepcopy(recipe.raw)
        doc["meat"]["pull_c"] = 51
        doc.pop("finish")
        r2 = recipes.from_doc(doc)
        self.assertEqual((r2.off_c, r2.meat_target_c), (51.0, 54.0))
        meta = {"recipe_name": r2.name, "weight_kg": 1.2, "probes": [1], "recipe": doc,
                "serve_at": (START + timedelta(hours=30)).isoformat()}
        cook_id = self.store.open_cook(r2.name, START, meta)
        engine = Engine(r2, self.store.meta(cook_id), self.store, self.notifier, cook_id)
        engine.manual("meat-on", "", START)
        for minute, meat in enumerate([30, 40, 48, 50.5, 51.2, 52]):
            engine.tick(Reading(at=START + timedelta(minutes=minute * 10), pit=110, probes=[meat], fan=40))
        msg = next(e["message"] for e in self.store.events(cook_id) if e["event"] == "target")
        self.assertIn("meat has reached 51 C", msg)
        self.assertIn("carry over to about 54 C", msg)


if __name__ == "__main__":
    unittest.main()
