"""The cook as Prometheus metrics, served at /metrics in the text exposition format.

Written by hand so the app keeps PyYAML as its only dependency. Temperatures are in degrees Celsius and
times are Unix timestamps or seconds, as Prometheus expects. The numbers come from the same reading and
the same overview the page shows, so a Grafana panel and the Cook tab never disagree.

`monitoring/` in the repository has a Prometheus config, alert rules and a Grafana dashboard for these names.
"""
from __future__ import annotations

import time
from datetime import datetime

from . import __version__

PHASES = ("preheating", "cooking", "stall", "resting")
CHECK_VALUE = {"pass": 1, "pending": 0, "fail": -1}
FRESH_S = 120.0                 # a reading older than this is not reported (the page does the same)
COUNT_CACHE_S = 60.0            # the cook log is counted at most this often

# name: (type, help). The order here is the order on the page.
FAMILIES = {
    "openbrisket_info": ("gauge", "Always 1; the running version is in the label."),
    "openbrisket_feed_up": ("gauge", "1 when a fresh reading has arrived and the reader says it is connected."),
    "openbrisket_last_reading_timestamp_seconds": ("gauge", "When the last reading was taken."),
    "openbrisket_pit_temperature_celsius": ("gauge", "Pit temperature."),
    "openbrisket_pit_setpoint_celsius": ("gauge", "What the fan controller is set to hold."),
    "openbrisket_fan_output_percent": ("gauge", "Fan output, 0 to 100."),
    "openbrisket_fan_on": ("gauge", "1 when the fan is switched on."),
    "openbrisket_probe_temperature_celsius": ("gauge", "Food probe temperature, one series per probe that is reading."),
    "openbrisket_ambient_temperature_celsius": ("gauge", "Outdoor temperature, when a sensor is set."),
    "openbrisket_ambient_humidity_percent": ("gauge", "Outdoor relative humidity, when a sensor is set."),
    "openbrisket_rain": ("gauge", "The rain sensor's own number (mm, mm/h), or 1/0 for a wet/dry sensor."),
    "openbrisket_cooks": ("gauge", "Cooks in the log, by how they ended (open, done, abandoned)."),
    "openbrisket_cook_open": ("gauge", "1 while a cook is open."),
    "openbrisket_cook_info": ("gauge", "Always 1 while a cook is open; what is cooking is in the labels."),
    "openbrisket_cook_phase": ("gauge", "1 for the phase the cook is in, 0 for the others."),
    "openbrisket_cook_wrapped": ("gauge", "1 once the meat has been wrapped."),
    "openbrisket_cook_weight_kilograms": ("gauge", "Raw weight of the meat."),
    "openbrisket_cook_opened_timestamp_seconds": ("gauge", "When the cook was opened."),
    "openbrisket_cook_meat_on_timestamp_seconds": ("gauge", "When the meat went on."),
    "openbrisket_cook_serve_timestamp_seconds": ("gauge", "The serving time: the SLO."),
    "openbrisket_cook_estimated_off_timestamp_seconds": ("gauge", "When the meat is expected to come off."),
    "openbrisket_cook_error_budget_seconds": ("gauge", "Time between the estimated finish and serving. "
                                                       "The rest is paid out of this; negative means dinner is late."),
    "openbrisket_cook_meat_temperature_celsius": ("gauge", "The coldest probe still in the meat, which decides everything."),
    "openbrisket_cook_probe_in_meat": ("gauge", "1 for a probe of this cook that is in the meat, 0 once it is pulled."),
    "openbrisket_cook_pit_target_celsius": ("gauge", "Pit temperature the recipe declares."),
    "openbrisket_cook_pit_tolerance_celsius": ("gauge", "How far the pit may sit from the recipe's target before it counts as drift."),
    "openbrisket_cook_meat_target_celsius": ("gauge", "Meat temperature the recipe finishes at."),
    "openbrisket_cook_wrap_celsius": ("gauge", "Meat temperature at which the recipe wraps."),
    "openbrisket_cook_check_status": ("gauge", "Status checks on the open cook: 1 pass, 0 pending, -1 fail."),
    "openbrisket_cook_events_total": ("counter", "Events logged in the open cook, by kind. Starts again with each cook."),
    "openbrisket_hold_seconds": ("gauge", "How long the meat has been resting or held."),
    "openbrisket_hold_floor_celsius": ("gauge", "The temperature the meat must stay above during the hold."),
    "openbrisket_hold_rate_celsius_per_hour": ("gauge", "How fast the held meat is cooling (negative) or warming."),
    "openbrisket_hold_until_floor_seconds": ("gauge", "At the current rate, how long until the meat reaches the floor."),
}

_counts: dict = {}              # cook log folder -> (monotonic time, {status: count})


def _esc(value) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(value) -> str:
    if value is True or value is False:
        return "1" if value else "0"
    return repr(round(float(value), 3))


def _ts(text) -> float | None:
    """An ISO time from the cook's record as a Unix timestamp. A time with no zone is local time."""
    if not text:
        return None
    try:
        dt = text if isinstance(text, datetime) else datetime.fromisoformat(str(text).strip().replace(" ", "T", 1))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.astimezone()).timestamp()


class _Page:
    def __init__(self):
        self.samples: dict[str, list[str]] = {}

    def add(self, name: str, value, /, **labels) -> None:
        if value is None:
            return
        try:
            number = _num(value)
        except (TypeError, ValueError):
            return
        text = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items() if v is not None)
        self.samples.setdefault(name, []).append(f"{name}{{{text}}} {number}" if text else f"{name} {number}")

    def text(self) -> str:
        out = []
        for name, (kind, help_) in FAMILIES.items():
            lines = self.samples.get(name)
            if not lines:
                continue
            out += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}", *lines]
        return "\n".join(out) + "\n"


def _cook_counts(store) -> dict:
    hit = _counts.get(store.root)
    if hit and time.monotonic() - hit[0] < COUNT_CACHE_S:
        return hit[1]
    counts: dict = {}
    for cook_id in store.cooks():
        try:
            status = str(store.meta(cook_id).get("status") or "unknown")
        except Exception:               # one unreadable record must not empty the page
            status = "unknown"
        counts[status] = counts.get(status, 0) + 1
    _counts[store.root] = (time.monotonic(), counts)
    return counts


def render(service, now: datetime | None = None) -> str:
    """Everything worth a graph, as one page of text. `now` is only passed by tests and the demo."""
    from .service import _now
    now = now or _now()
    page = _Page()
    page.add("openbrisket_info", 1, version=__version__)
    with service.lock:
        engine = service.engine()
        store = service.store()
        r = service.last_reading
        if r is not None:
            page.add("openbrisket_last_reading_timestamp_seconds", r.at.timestamp())
            if (now - r.at).total_seconds() > FRESH_S:
                r = None
        page.add("openbrisket_feed_up", bool(r is not None and r.connected))
        names = {s["no"]: s["name"] for s in service.probe_sources()}
        if r is not None:
            page.add("openbrisket_pit_temperature_celsius", r.pit)
            page.add("openbrisket_pit_setpoint_celsius", r.pit_target)
            page.add("openbrisket_fan_output_percent", r.fan)
            page.add("openbrisket_fan_on", r.fan_on)
            for i, value in enumerate(r.probes, start=1):
                page.add("openbrisket_probe_temperature_celsius", value, probe=i, name=names.get(i) or f"Probe {i}")
            page.add("openbrisket_ambient_temperature_celsius", r.ambient)
            page.add("openbrisket_ambient_humidity_percent", r.humidity)
            page.add("openbrisket_rain", r.rain)
        for status, count in sorted(_cook_counts(store).items()):
            page.add("openbrisket_cooks", count, status=status)
        page.add("openbrisket_cook_open", engine is not None)
        if engine is not None:
            _cook(page, engine, engine.overview(now, r))
    return page.text()


def _cook(page: _Page, engine, cook: dict) -> None:
    stack = engine.meta.get("stack") or {}
    page.add("openbrisket_cook_info", 1, cook=cook["id"], recipe=cook["recipe"], version=cook["recipe_version"],
             cut=cook["cut"], style=cook["style"], stack=stack.get("name") or "")
    for phase in PHASES:
        page.add("openbrisket_cook_phase", cook["phase"] == phase, phase=phase)
    page.add("openbrisket_cook_wrapped", cook["wrapped"])
    page.add("openbrisket_cook_weight_kilograms", cook["weight_kg"])
    page.add("openbrisket_cook_opened_timestamp_seconds", _ts(cook["opened_at"]))
    page.add("openbrisket_cook_meat_on_timestamp_seconds", _ts(cook["meat_on_at"]))
    serve = _ts(engine.serve_at)
    page.add("openbrisket_cook_serve_timestamp_seconds", serve)
    off = _ts((cook["estimate"] or {}).get("off_at"))
    page.add("openbrisket_cook_estimated_off_timestamp_seconds", off)
    if off is not None and serve is not None:
        page.add("openbrisket_cook_error_budget_seconds", serve - off)
    page.add("openbrisket_cook_meat_temperature_celsius", cook["meat_c"])
    for p in cook["probes"]:
        page.add("openbrisket_cook_probe_in_meat", p["in_meat"], probe=p["no"], label=p["label"])
    t = cook["targets"]
    page.add("openbrisket_cook_pit_target_celsius", t["pit_c"])
    page.add("openbrisket_cook_pit_tolerance_celsius", t["pit_tolerance_c"])
    page.add("openbrisket_cook_meat_target_celsius", t["meat_c"])
    page.add("openbrisket_cook_wrap_celsius", t["wrap_at_c"])
    for check in cook["checks"]:
        page.add("openbrisket_cook_check_status", CHECK_VALUE.get(check["state"], 0), check=check["name"])
    kinds: dict = {}
    try:
        for event in engine.store.events(engine.cook_id):
            kind = str(event.get("event") or "note")
            kinds[kind] = kinds.get(kind, 0) + 1
    except (OSError, ValueError):
        pass
    for kind, count in sorted(kinds.items()):
        page.add("openbrisket_cook_events_total", count, event=kind)
    hold = cook.get("hold") or {}
    if hold:
        page.add("openbrisket_hold_seconds", (hold.get("hours") or 0) * 3600)
        page.add("openbrisket_hold_floor_celsius", hold.get("floor_c"))
        page.add("openbrisket_hold_rate_celsius_per_hour", hold.get("rate_c_per_h"))
        if hold.get("hours_until_floor") is not None:
            page.add("openbrisket_hold_until_floor_seconds", hold["hours_until_floor"] * 3600)
