import contextlib
import csv
import io
import os
import tempfile
import unittest
from unittest import mock

import yaml

from openbrisket import cli, plan
from openbrisket.store import CURVE_FIELDS, Store
from tests.helpers import BRISKET, ROOT, brisket, cook_readings


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
            if isinstance(code, str):
                out.write(code)
                code = 1
    return code, out.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"OPENBRISKET_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_init_creates_private_settings_once(self):
        code, out = run("init")
        self.assertEqual(code, 0)
        path = os.path.join(self.tmp.name, "settings.yaml")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as f:
            self.assertEqual(yaml.safe_load(f)["source"]["type"], "homeassistant")
        self.assertIn("already exists", run("init")[1])

    def test_validate(self):
        self.assertEqual(run("validate", os.path.join(ROOT, "recipes"))[0], 0)
        bad = os.path.join(self.tmp.name, "r")
        os.makedirs(bad)
        with open(os.path.join(bad, "x.yaml"), "w") as f:
            f.write("apiVersion: openbrisket/v1\nkind: Recipe\nname: x\n")
        code, out = run("validate", bad)
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)

    def test_plan(self):
        code, out = run("plan", BRISKET, "--weight", "5.4", "--serve", "2026-11-14 18:00")
        self.assertEqual(code, 0)
        self.assertIn("18:00  serve", out)
        code, out = run("plan", BRISKET, "--serve", "2026-11-14 18:00")
        self.assertEqual(code, 2)
        self.assertIn("weight", out)

    def test_a_whole_cook_from_a_replayed_curve(self):
        code, out = run("apply", BRISKET, "--weight", "5.4", "--serve", "2026-11-14 18:00")
        self.assertEqual(code, 0, out)
        store = Store(os.path.join(self.tmp.name, "cooks"))
        cook_id = store.active()
        self.assertIn(cook_id, out)
        self.assertEqual(run("apply", BRISKET, "--weight", "5", "--serve", "2026-11-14 18:00")[0], 1)

        self.assertEqual(run("log", "meat-on")[0], 0)
        curve = os.path.join(self.tmp.name, "replay.csv")
        prior = plan.prior_params(brisket(), 5.4)
        with open(curve, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CURVE_FIELDS)
            w.writeheader()
            for r in cook_readings(prior, step_s=60, hours=7.0):
                w.writerow({"at": r.at.isoformat(), "pit": r.pit, "probe1": r.probes[0], "probe2": "", "probe3": "",
                            "fan": r.fan, "pit_target": r.pit_target})
        code, out = run("run", "--replay", curve)
        self.assertEqual(code, 0, out)
        self.assertIn("CHECK: stall detected", out)

        self.assertEqual(run("log", "wrap", "butcher", "paper,", "tight")[0], 0)
        self.assertEqual(run("log", "rest")[0], 0)
        code, out = run("history")
        self.assertIn("stall detected", out)
        self.assertEqual(run("close", "--score", "11")[0], 1)
        code, out = run("close", "--score", "8.5", "--cooked-weight", "3.1", "--notes", "flat a little dry")
        self.assertEqual(code, 0, out)
        meta = store.meta(cook_id)
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["result"]["yield"], round(3.1 / 5.4, 3))
        self.assertIsNone(store.active())
        self.assertEqual(run("status")[0], 1)


if __name__ == "__main__":
    unittest.main()
