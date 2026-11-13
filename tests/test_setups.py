import os
import tempfile
import unittest

import yaml

from openbrisket import cli, recipes, setups
from openbrisket.service import ServiceError
from tests.helpers import ROOT
from tests.test_service import Base

SETUP = {"name": "Drum Lump Cherry", "description": "Oil drum on lump.",
         "smoker": {"type": "drum", "model": "200 l"},
         "fuel": {"type": "lump-charcoal", "brand": "Big K", "amount_kg": 5, "layout": "minion", "lit_with": "half a chimney"},
         "wood": [{"species": "cherry", "form": "chunks", "pieces": 3, "when": "on the lit coals"},
                  {"species": "", "form": "chunks", "pieces": "", "grams": ""}],
         "water_pan": "", "airflow": {"controller": "fan", "deflector": True, "top_vent": "half open"}}


def doc(**changes):
    with open(os.path.join(ROOT, "recipes", "setups", "kamado-lump-oak.yaml")) as f:
        d = yaml.safe_load(f)
    d.update(changes)
    return d


class SchemaTests(unittest.TestCase):
    def test_shipped_setups_are_valid(self):
        self.assertEqual(setups.validate_setup(doc()), [])
        self.assertEqual(recipes.validate_tree(os.path.join(ROOT, "recipes")), {})

    def test_problems_are_named(self):
        cases = [({"smoker": {"type": "barrel"}}, "smoker.type must be one of"),
                 ({"fuel": {"type": "coal"}}, "fuel.type must be one of"),
                 ({"fuel": {"type": "briquettes", "amount_kg": 500}}, "fuel.amount_kg"),
                 ({"wood": [{"form": "chunks"}]}, "wood[0] needs a species"),
                 ({"wood": [{"species": "oak", "form": "logs"}]}, "wood[0].form must be one of"),
                 ({"wood": [{"species": "oak", "form": "chunks", "pieces": True}]}, "wood[0].pieces"),
                 ({"airflow": {"controller": "robot"}}, "airflow.controller"),
                 ({"airflow": {"deflector": "yes"}}, "airflow.deflector"),
                 ({"smoker": {"type": "pellet"}}, "a pellet smoker burns pellets"),
                 ({"water_pan": 3}, "water_pan"), ({"kind": "Rub"}, "kind must be Setup")]
        for change, want in cases:
            problems = setups.validate_setup(doc(**change))
            self.assertTrue(any(want in p for p in problems), f"{change}: {problems}")
        self.assertEqual(setups.validate_setup([]), ["setup is not a mapping"])

    def test_the_fire_in_one_line(self):
        line = setups.fire_line(doc(water_pan="apple juice"))
        self.assertIn("4 kg lump charcoal", line)
        self.assertIn("lit with one firelighter", line)
        self.assertIn("Wood: 4 post oak chunks", line)
        self.assertIn("Water pan with apple juice", line)
        self.assertEqual(setups.wood_text({"species": "cherry", "form": "chunks", "pieces": 1}), "1 cherry chunk")
        self.assertNotIn("Water pan", setups.fire_line(doc()))


class ServiceSetupTests(Base):
    def test_shipped_setups_are_listed(self):
        listed = self.service.list_setups()
        self.assertEqual([s["id"] for s in listed], ["kamado-lump-oak", "kettle-snake-hickory"])
        self.assertTrue(all(s["valid"] and not s["own"] and s["results"] == [] for s in listed))
        self.assertEqual(listed[0]["water_pan"], None)
        self.assertEqual(listed[1]["water_pan"], "water")
        self.assertIn("kamado", self.service.setup_vocabulary()["smokers"])

    def test_own_setup_is_yaml_with_history(self):
        out = self.service.save_setup(SETUP)
        self.assertEqual((out["id"], out["version"], out["saved"]), ("drum-lump-cherry", "1.0.0", True))
        path = os.path.join(self.library, "setups", "drum-lump-cherry.yaml")
        with open(path) as f:
            saved = yaml.safe_load(f)
        self.assertEqual(saved["kind"], "Setup")
        self.assertEqual(saved["water_pan"], "none")
        self.assertEqual(len(saved["wood"]), 1)                          # the empty form row is dropped
        self.assertEqual(self.service.save_setup(SETUP)["saved"], False)
        changed = {**SETUP, "wood": [{"species": "cherry", "form": "chunks", "pieces": 2}], "water_pan": "water",
                   "change": "too smoky, one chunk fewer"}
        self.assertEqual(self.service.save_setup(changed)["version"], "1.0.1")
        hist = self.service.item_history("setup", "drum-lump-cherry")
        self.assertEqual(hist[0]["message"], "setup drum-lump-cherry 1.0.1: too smoky, one chunk fewer")
        fields = {c["field"]: (c["before"], c["after"]) for c in hist[0]["changes"]}
        self.assertEqual(fields["Water pan"], ("none", "water"))
        self.assertEqual(fields["wood, cherry"], ("3 chunks, on the lit coals", "2 chunks"))
        mine = next(s for s in self.service.list_setups() if s["id"] == "drum-lump-cherry")
        self.assertTrue(mine["own"])
        with self.assertRaises(ServiceError):
            self.service.save_setup({**SETUP, "smoker": {"type": "barrel"}})
        with self.assertRaises(ServiceError):
            self.service.save_setup({"name": ""})
        self.assertEqual(self.service.delete_item("setup", "drum-lump-cherry")["kind"], "setup")
        self.assertFalse(os.path.exists(path))

    def test_setup_yaml_is_saved_as_typed(self):
        with open(os.path.join(ROOT, "recipes", "setups", "kamado-lump-oak.yaml")) as f:
            text = f.read()
        out = self.service.save_yaml("setup", text.replace("pieces: 4", "pieces: 6"), "more smoke")
        self.assertEqual(out["id"], "kamado-lump-oak")
        mine = next(s for s in self.service.list_setups() if s["id"] == "kamado-lump-oak")
        self.assertTrue(mine["own"])                                     # your copy takes the shipped one's place
        self.assertIn("# a full basket", mine["source"])                 # comments survive
        self.assertEqual(mine["wood"][0]["pieces"], 6)
        with self.assertRaises(ServiceError):
            self.service.save_yaml("setup", text.replace("type: kamado", "type: barrel"))
        with self.assertRaises(ServiceError):
            self.service.save_yaml("setup", text.replace("kind: Setup", "kind: Rub"))

    def test_a_cook_keeps_its_setup_and_results_are_per_version(self):
        self.service.save_setup(SETUP)
        self.assertIsNone(self.service.last_setup())
        plan = self.service.plan("texas-brisket", 5.4, self.serve_at, "drum-lump-cherry")
        fire = next(s for s in plan["steps"] if s["step"] == "light the fire")
        self.assertIn("5 kg lump charcoal (Big K), minion", fire["detail"])
        self.assertIn("Bring the pit to 121 C", fire["detail"])
        with self.assertRaises(ServiceError):
            self.service.start("texas-brisket", 5.4, self.serve_at, setup="nope")

        def cook(score, burnt):
            started = self.service.start("texas-brisket", 5.4, self.serve_at, setup="drum-lump-cherry")
            self.assertIn("5 kg", self.service.status()["cook"]["setup"]["fire"])
            self.service.log_step("fuel", "1 kg lump")
            self.service.log_step("meat-on")
            self.service.log_step("wood", "one chunk of cherry")
            self.service.close(score=score, fuel_used_kg=burnt)
            return started["cook"]
        a = cook(6, 4.0)
        meta = self.service.store().meta(a)
        self.assertEqual((meta["setup"]["id"], meta["setup"]["version"]), ("drum-lump-cherry", "1.0.0"))
        self.assertEqual(meta["setup"]["fuel"]["amount_kg"], 5)
        self.assertEqual(meta["result"]["fuel_used_kg"], 4.0)
        self.assertEqual((meta["analysis"]["events"]["fuel"], meta["analysis"]["events"]["wood"]), (1, 1))
        self.assertIn("fuel added: 1 kg lump", str(self.service.cook(a)["events"]))
        self.assertEqual(self.service.last_setup(), "drum-lump-cherry")
        self.service.save_setup({**SETUP, "fuel": {**SETUP["fuel"], "type": "briquettes"}, "change": "try briquettes"})
        cook(8, None)
        cook(9, None)
        mine = next(s for s in self.service.list_setups() if s["id"] == "drum-lump-cherry")
        self.assertTrue(mine["last_used"])
        self.assertEqual([(v["version"], v["count"], v["avg_score"]) for v in mine["results"]],
                         [("1.0.0", 1, 6.0), ("1.0.1", 2, 8.5)])
        self.assertEqual(mine["results"][0]["cooks"][0]["recipe"], "texas-brisket")
        self.assertEqual(self.service.cooks()[0]["setup"], "drum-lump-cherry")
        by_recipe = next(r for r in self.service.list_recipes() if r["id"] == "texas-brisket")["results"]
        self.assertEqual(by_recipe[0]["cooks"][0]["setup_version"], "1.0.0")
        with self.assertRaises(ServiceError):
            self.service.start("texas-brisket", 5.4, self.serve_at)
            self.service.close(fuel_used_kg="lots")

    def test_a_cook_without_a_setup_still_works(self):
        started = self.service.start("texas-brisket", 5.4, self.serve_at)
        self.assertIsNone(self.service.status()["cook"]["setup"])
        self.assertNotIn("setup", self.service.store().meta(started["cook"]))


class CliSetupTests(unittest.TestCase):
    def test_plan_with_a_setup_file(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["plan", os.path.join(ROOT, "recipes", "texas-brisket.yaml"), "--weight", "5.4",
                             "--serve", "2026-11-14 18:00",
                             "--setup", os.path.join(ROOT, "recipes", "setups", "kettle-snake-hickory.yaml")])
        self.assertEqual(code, 0)
        self.assertIn("2.5 kg briquettes, snake two wide", out.getvalue())
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f, contextlib.redirect_stderr(io.StringIO()):
            f.write("kind: Setup\n")
            f.flush()
            self.assertEqual(cli.main(["plan", os.path.join(ROOT, "recipes", "texas-brisket.yaml"), "--weight", "5",
                                       "--serve", "2026-11-14 18:00", "--setup", f.name]), 2)


if __name__ == "__main__":
    unittest.main()
