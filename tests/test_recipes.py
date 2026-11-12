import copy
import os
import tempfile
import unittest

import yaml

from openbrisket import recipes
from tests.helpers import BRISKET, ROOT


class RecipeTests(unittest.TestCase):
    def doc(self):
        with open(BRISKET) as f:
            return yaml.safe_load(f)

    def test_shipped_recipes_are_valid(self):
        self.assertEqual(recipes.validate_tree(os.path.join(ROOT, "recipes")), {})

    def test_load(self):
        r = recipes.load_recipe(BRISKET)
        self.assertEqual(r.name, "texas-brisket")
        self.assertAlmostEqual(r.cook_hours(5.0), 13.5)
        self.assertEqual(r.wrap_at_c, 74)

    def test_weight_required_for_per_kg_recipe(self):
        with self.assertRaises(recipes.RecipeError):
            recipes.load_recipe(BRISKET).cook_hours(None)

    def test_food_safety_floor(self):
        d = self.doc()
        d["cut"] = "chicken-whole"
        d["meat"]["target_c"] = 65
        d["wrap"] = None
        problems = recipes.validate_recipe(d)
        self.assertTrue(any("food-safety floor of 74" in p for p in problems), problems)
        d["meat"]["target_c"] = 75
        self.assertEqual(recipes.validate_recipe(d), [])

    def test_hold_floor(self):
        d = self.doc()
        self.assertEqual(recipes.from_doc(d).hold_min_c, 60)
        d["rest"]["hold_min_c"] = 50
        self.assertTrue(any("not safe to hold below 57" in p for p in recipes.validate_recipe(d)))
        d["rest"]["hold_min_c"] = 63
        self.assertEqual(recipes.validate_recipe(d), [])
        self.assertEqual(recipes.from_doc(d).hold_min_c, 63)

    def test_ground_meat_floor(self):
        self.assertEqual(recipes.min_safe_c("beef-ground-burger"), 71)
        self.assertEqual(recipes.min_safe_c("beef-brisket-packer"), 54)
        self.assertIsNone(recipes.min_safe_c("tofu"))

    def test_unknown_cut_and_bad_numbers(self):
        d = self.doc()
        d["cut"] = "tofu-block"
        d["pit"]["target_c"] = 400
        d["timing"]["hours_fixed"] = 5
        problems = recipes.validate_recipe(d)
        self.assertTrue(any("must start with" in p for p in problems))
        self.assertTrue(any("outside" in p for p in problems))
        self.assertTrue(any("exactly one" in p for p in problems))

    def test_missing_rub_file(self):
        d = self.doc()
        d["rub"] = "rubs/nope.yaml"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(any("rub file not found" in p for p in recipes.validate_recipe(d, tmp)))

    def test_rub_fractions(self):
        with open(os.path.join(ROOT, "recipes", "rubs", "sweet-pork.yaml")) as f:
            fr = recipes.rub_fractions(yaml.safe_load(f))
        self.assertAlmostEqual(fr["sugar"], 4 / 12)
        self.assertAlmostEqual(fr["salt"], 2 / 12)

    def test_from_doc_roundtrip(self):
        r = recipes.load_recipe(BRISKET)
        again = recipes.from_doc(copy.deepcopy(r.raw))
        self.assertEqual(again.meat_target_c, r.meat_target_c)

    def test_validate_tree_reports_bad_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "bad.yaml"), "w") as f:
                f.write("kind: Recipe\nname: [unclosed\n")
            bad = recipes.validate_tree(tmp)
            self.assertEqual(len(bad), 1)


if __name__ == "__main__":
    unittest.main()
