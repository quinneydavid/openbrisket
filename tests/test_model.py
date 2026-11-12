import math
import unittest
from datetime import datetime, timedelta, timezone

from openbrisket import model, plan
from tests.helpers import brisket, cook_readings, scaled


def samples_from(readings, every_s=60):
    out, last = [], None
    first = None
    for r in readings:
        first = first or r.at
        if last is None or (r.at - last).total_seconds() >= every_s:
            out.append(model.Sample((r.at - first).total_seconds() / 3600, r.probes[0], r.pit))
            last = r.at
    return out


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.recipe = brisket()
        self.prior = plan.prior_params(self.recipe, 5.4)

    def test_calibration_matches_recipe_hours(self):
        proj = model.simulate(model.FRIDGE_C, 121, 95, self.prior, 74)
        self.assertAlmostEqual(proj.hours, self.recipe.cook_hours(5.4), delta=0.05)
        self.assertLess(proj.stall_start_h, proj.wrap_h)
        self.assertLess(proj.wrap_h, proj.stall_end_h)

    def test_never_reaches_target_when_pit_is_too_cold(self):
        self.assertEqual(model.simulate(20, 90, 95, self.prior).hours, math.inf)

    def test_wrapping_shortens_the_cook(self):
        unwrapped = model.simulate(model.FRIDGE_C, 121, 95, self.prior, None).hours
        wrapped = model.simulate(model.FRIDGE_C, 121, 95, self.prior, 74).hours
        self.assertLess(wrapped, unwrapped)

    def test_fit_recovers_a_faster_cook(self):
        truth = scaled(self.prior, 1.4)
        s = [x for x in samples_from(cook_readings(truth)) if x.t_h <= 3.0]
        f = model.fit(s, self.prior)
        self.assertAlmostEqual(f.k_observed, truth.k, delta=truth.k * 0.05)
        self.assertGreater(f.weight, 0.9)
        self.assertAlmostEqual(f.params.k, truth.k, delta=truth.k * 0.08)

    def test_fit_without_data_is_the_prior(self):
        f = model.fit([], self.prior)
        self.assertEqual(f.params, self.prior)
        self.assertEqual(f.weight, 0.0)

    def test_estimate_improves_as_the_cook_goes(self):
        truth = scaled(self.prior, 0.75)          # a slow cook
        all_samples = samples_from(cook_readings(truth))
        true_total = all_samples[-1].t_h
        errors = []
        for at_h in (0.5, 3.0, 8.0):
            s = [x for x in all_samples if x.t_h <= at_h]
            proj, _ = model.remaining(s, self.prior, 95, 74, wrapped=s[-1].meat >= 74)
            errors.append(abs((at_h + proj.hours) - true_total))
        self.assertLess(errors[1], errors[0])
        self.assertLess(errors[2], 1.0)

    def test_recent_rate(self):
        s = [model.Sample(i / 60, 50 + i * 0.1, 121) for i in range(61)]
        self.assertAlmostEqual(model.recent_rate(s), 6.0, delta=0.01)
        self.assertIsNone(model.recent_rate(s[:5]))


class RainTests(unittest.TestCase):
    def test_numbers_and_words_both_become_numbers(self):
        from openbrisket.sources import _rain
        self.assertEqual([_rain(x) for x in ("0.0", "3.2", "on", "Raining", "off", "dry", "unavailable", None)],
                         [0.0, 3.2, 1.0, 1.0, 0.0, 0.0, None, None])


class PlanTests(unittest.TestCase):
    def test_timeline(self):
        recipe = brisket()
        serve = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
        p = plan.make_plan(recipe, 5.4, serve)
        self.assertEqual([s.at for s in p.steps], sorted(s.at for s in p.steps))
        self.assertEqual(p.steps[-1].what, "serve")
        expected_on = serve - timedelta(minutes=120) - timedelta(hours=recipe.cook_hours(5.4) * 1.1)
        self.assertEqual(p.meat_on, expected_on)
        names = [s.what for s in p.steps]
        for step in ("salt / dry brine", "light the fire", "meat on", "stall expected", "wrap"):
            self.assertIn(step, names)
        self.assertIn("texas-brisket", plan.render(p))


if __name__ == "__main__":
    unittest.main()
