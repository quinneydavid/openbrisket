import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from openbrisket import metrics, server
from tests.helpers import ROOT
from tests import test_service

SAMPLE = re.compile(r'^([a-z_]+)(\{[^}]*\})? (-?[0-9.e+-]+)$')


def parse(text):
    """{name: {labels-text: value}}, and every line checked against the exposition format."""
    out, typed = {}, set()
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            typed.add(line.split()[2])
            continue
        if line.startswith("# HELP "):
            continue
        m = SAMPLE.match(line)
        assert m, f"not a metrics line: {line!r}"
        assert m.group(1) in typed, f"{m.group(1)} has no TYPE line before it"
        out.setdefault(m.group(1), {})[m.group(2) or ""] = float(m.group(3))
    return out


class MetricsTests(test_service.Base):
    def test_no_cook_and_no_reading(self):
        m = parse(metrics.render(self.service))
        self.assertEqual(m["openbrisket_feed_up"][""], 0)
        self.assertEqual(m["openbrisket_cook_open"][""], 0)
        self.assertEqual(list(m["openbrisket_info"].values()), [1.0])
        self.assertNotIn("openbrisket_pit_temperature_celsius", m)
        self.assertNotIn("openbrisket_cook_info", m)

    def test_readings_without_a_cook(self):
        self.service.read_now()
        m = parse(metrics.render(self.service))
        self.assertEqual(m["openbrisket_feed_up"][""], 1)
        self.assertEqual(m["openbrisket_pit_temperature_celsius"][""], 120.0)
        self.assertEqual(m["openbrisket_pit_setpoint_celsius"][""], 121.0)
        self.assertEqual(m["openbrisket_fan_output_percent"][""], 40.0)
        self.assertEqual(m["openbrisket_fan_on"][""], 1)
        self.assertEqual(m["openbrisket_probe_temperature_celsius"], {'{probe="1",name="bbq reader probe 1"}': 24.0})

    def test_an_old_reading_is_not_reported(self):
        self.service.read_now()
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        m = parse(metrics.render(self.service, later))
        self.assertEqual(m["openbrisket_feed_up"][""], 0)
        self.assertNotIn("openbrisket_pit_temperature_celsius", m)
        self.assertIn("openbrisket_last_reading_timestamp_seconds", m)

    def test_an_open_cook(self):
        started = self.service.start("texas-brisket", 5.4, self.serve_at, probes=[1], probe_labels={"1": 'the "flat"'})
        self.service.read_now()
        m = parse(metrics.render(self.service))
        self.assertEqual(m["openbrisket_cook_open"][""], 1)
        (info,) = m["openbrisket_cook_info"]
        self.assertIn(f'cook="{started["cook"]}"', info)
        self.assertIn('recipe="texas-brisket"', info)
        self.assertEqual(m["openbrisket_cook_phase"]['{phase="preheating"}'], 1)
        self.assertEqual(sum(m["openbrisket_cook_phase"].values()), 1)
        self.assertNotIn("openbrisket_cook_meat_on_timestamp_seconds", m)
        self.assertEqual(m["openbrisket_cook_probe_in_meat"], {'{probe="1",label="the \\"flat\\""}': 1.0})
        self.assertEqual(m["openbrisket_cook_check_status"]['{check="data-feed"}'], 1)
        self.assertEqual(m["openbrisket_cook_check_status"]['{check="smoke-test"}'], 0)
        serve = datetime.strptime(self.serve_at, "%Y-%m-%d %H:%M").astimezone().timestamp()
        self.assertEqual(m["openbrisket_cook_serve_timestamp_seconds"][""], serve)
        self.assertEqual(m["openbrisket_cook_weight_kilograms"][""], 5.4)

        self.service.log_step("meat-on")
        self.service.log_step("hotfix", "half a chimney")
        self.service.log_step("hotfix", "and another")
        m = parse(metrics.render(self.service))
        self.assertEqual(m["openbrisket_cook_phase"]['{phase="cooking"}'], 1)
        self.assertEqual(m["openbrisket_cook_events_total"]['{event="hotfix"}'], 2)
        self.assertAlmostEqual(m["openbrisket_cook_meat_on_timestamp_seconds"][""], datetime.now().timestamp(), delta=5)
        self.assertEqual(m["openbrisket_cook_meat_temperature_celsius"][""], 24.0)
        self.assertEqual(m["openbrisket_cook_pit_target_celsius"][""], 121.0)
        self.assertEqual(m["openbrisket_cook_check_status"]['{check="wrapped"}'], 0)
        self.assertNotIn("openbrisket_hold_seconds", m)

        self.service.log_step("wrap")
        self.source.meat = 96.0
        self.service.read_now()
        self.service.log_step("rest")
        m = parse(metrics.render(self.service))
        self.assertEqual(m["openbrisket_cook_phase"]['{phase="resting"}'], 1)
        self.assertEqual(m["openbrisket_cook_wrapped"][""], 1)
        self.assertIn("openbrisket_hold_seconds", m)
        self.assertEqual(m["openbrisket_hold_floor_celsius"][""], 60.0)

        self.service.close(score=9)
        metrics._counts.clear()
        m = parse(metrics.render(self.service))
        self.assertEqual(m["openbrisket_cook_open"][""], 0)
        self.assertEqual(m["openbrisket_cooks"], {'{status="done"}': 1.0})
        self.assertNotIn("openbrisket_cook_info", m)

    def test_every_name_is_declared_once_and_documented(self):
        self.service.start("texas-brisket", 5.4, self.serve_at)
        self.service.read_now()
        self.service.log_step("meat-on")
        text = metrics.render(self.service)
        types = [line.split()[2] for line in text.splitlines() if line.startswith("# TYPE ")]
        self.assertEqual(len(types), len(set(types)))
        self.assertTrue(set(parse(text)) <= set(metrics.FAMILIES))
        with open(os.path.join(ROOT, "monitoring", "README.md"), encoding="utf-8") as f:
            doc = f.read()
        for name in metrics.FAMILIES:
            self.assertIn(f"`{name}`", doc, f"{name} is missing from monitoring/README.md")

    def test_the_dashboard_and_alerts_only_use_metrics_that_exist(self):
        used = set()
        for rel in ("monitoring/grafana/dashboards/openbrisket.json", "monitoring/prometheus/alerts.yml"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                used |= set(re.findall(r"openbrisket_[a-z_]+", f.read()))
        self.assertTrue(used, "the dashboard uses no OpenBrisket metric at all")
        self.assertEqual(used - set(metrics.FAMILIES), set())
        with open(os.path.join(ROOT, "monitoring/grafana/dashboards/openbrisket.json"), encoding="utf-8") as f:
            board = json.load(f)
        self.assertEqual(board["uid"], "openbrisket")


class MetricsHttpTests(test_service.Base):
    req = test_service.HttpTests.req

    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.service))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.token = self.service.access_token()

    def test_metrics_need_a_token_and_read_rights_are_enough(self):
        self.assertEqual(self.req("/metrics", token=None)[0], 401)
        self.assertEqual(self.req("/metrics", token="guess")[0], 401)
        minted = self.req("/api/tokens", {"name": "prometheus", "scope": "read"})[1]["token"]
        code, body, headers = self.req("/metrics", token=minted)
        self.assertEqual(code, 200)
        self.assertEqual(headers.get_content_type(), "text/plain")
        self.assertIn("version=0.0.4", headers["Content-Type"])
        self.assertIn("openbrisket_cook_open 0", body.decode())
