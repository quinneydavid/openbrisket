"""`smoke-test`: the check you run after lighting the fire and before the meat goes on.

Each check returns pass, fail or skip with a one-line reason. The cook should not start on a fail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from .notify import Notifier
from .recipes import Recipe
from .sources import Reading

PASS, FAIL, SKIP = "pass", "fail", "skip"


@dataclass
class Check:
    name: str
    result: str
    detail: str


def run(recipe: Recipe, reading: Reading, recent: list, notifier: Notifier | None, probe_no: int = 1,
        now: datetime | None = None) -> list[Check]:
    """`recent` is the engine's raw history: [iso time, pit, fan] rows, oldest first."""
    now = now or reading.at
    checks: list[Check] = []

    if reading.connected and reading.pit is not None:
        checks.append(Check("data-feed", PASS, f"readings arriving, pit at {reading.pit:.0f} C"))
    else:
        checks.append(Check("data-feed", FAIL, "no pit reading: check the reader and that the controller is on"))
        return checks + [Check(n, SKIP, "needs the data feed") for n in ("pit-stable", "fan", "probes")] + \
            [_notify(notifier)]

    rows = [(datetime.fromisoformat(a), pit, fan) for a, pit, fan in recent if pit is not None]
    window = [r for r in rows if now - r[0] <= timedelta(minutes=15)]
    span_min = (now - window[0][0]).total_seconds() / 60 if window else 0
    if span_min < 14:
        checks.append(Check("pit-stable", FAIL, f"only {span_min:.0f} min of history; needs 15 min at temperature "
                                                f"(keep `openbrisket run` going and try again)"))
    else:
        pits = [r[1] for r in window]
        lo, hi = min(pits), max(pits)
        if all(abs(p - recipe.pit_target_c) <= recipe.pit_tolerance_c for p in pits):
            checks.append(Check("pit-stable", PASS, f"held {lo:.0f}-{hi:.0f} C for 15 min (recipe "
                                                    f"{recipe.pit_target_c:.0f} +/- {recipe.pit_tolerance_c:.0f})"))
        else:
            checks.append(Check("pit-stable", FAIL, f"ranged {lo:.0f}-{hi:.0f} C over 15 min; recipe wants "
                                                    f"{recipe.pit_target_c:.0f} +/- {recipe.pit_tolerance_c:.0f}"))

    fans = [r[2] for r in window if r[2] is not None]
    if not fans:
        checks.append(Check("fan", SKIP, "no fan reading from this device"))
    elif median(fans) >= 95:
        checks.append(Check("fan", FAIL, f"fan pinned at {median(fans):.0f}%: weak fire, or air is leaking past it"))
    else:
        checks.append(Check("fan", PASS, f"fan averaging {median(fans):.0f}%, with headroom"))

    meat = reading.probe(probe_no)
    if meat is None:
        checks.append(Check("probes", FAIL, f"food probe {probe_no} is not reading: plug it in"))
    elif meat > 45:
        checks.append(Check("probes", FAIL, f"food probe {probe_no} reads {meat:.0f} C before the meat is on: is it "
                                            f"lying in the pit?"))
    else:
        checks.append(Check("probes", PASS, f"food probe {probe_no} reads {meat:.0f} C"))

    checks.append(Check("clean-smoke", SKIP, "photo check not available yet: look for thin blue smoke, not thick white"))
    checks.append(_notify(notifier))
    return checks


def _notify(notifier: Notifier | None) -> Check:
    if notifier is None:
        return Check("notifications", SKIP, "not tested")
    ok = notifier.send("OpenBrisket smoke test: if you can read this, notes will reach you.")
    return Check("notifications", PASS if ok else FAIL, "test note sent" if ok else "could not send a note")


def passed(checks: list[Check]) -> bool:
    return not any(c.result == FAIL for c in checks)


def render(checks: list[Check]) -> str:
    mark = {PASS: "ok  ", FAIL: "FAIL", SKIP: "skip"}
    lines = [f"  {mark[c.result]}  {c.name}: {c.detail}" for c in checks]
    lines.append("smoke test passed: put the meat on" if passed(checks) else "smoke test FAILED: do not start yet")
    return "\n".join(lines)
