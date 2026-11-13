"""The running cook: takes one reading at a time, keeps the state, and decides when to leave a note.

`Engine.tick(reading)` is the whole loop body. It is deterministic given the readings, so a recorded
curve can be replayed through it in tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from . import model
from .notify import Notifier
from .plan import prior_params
from .recipes import Recipe
from .sources import Reading
from .store import MAX_PROBES, Store

SAMPLE_EVERY_S = 60          # the model and the curve keep one point a minute
RECENT_KEEP_S = 20 * 60      # raw readings kept for the fast checks
STALL_RATE_C_PER_H = 2.0     # slower than this inside the stall band = stalled
LID_DROP_C = 15.0            # pit falling this much inside a few minutes = lid open
PULLED_DROP_C = 6.0          # a meat probe falling this much inside 3 minutes has been pulled out: meat cannot
PULLED_WINDOW_S = 180        # cool that fast, even uncovered


@dataclass
class Event:
    kind: str
    message: str
    severity: str = "info"      # info | ask | alert
    data: dict = field(default_factory=dict)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def fmt_dur(hours: float) -> str:
    minutes = int(round(hours * 60))
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


class Engine:
    def __init__(self, recipe: Recipe, meta: dict, store: Store, notifier: Notifier, cook_id: str):
        self.recipe = recipe
        self.meta = meta
        self.store = store
        self.notifier = notifier
        self.cook_id = cook_id
        self.state = store.load_state(cook_id) or {"samples": [], "recent": [], "said": {}, "last_sample": None}
        self.prior = prior_params(recipe, meta.get("weight_kg"))
        # Probes in the meat (1-based). Several are allowed; the coldest reading governs every decision,
        # because the meat is only done when its slowest part is.
        # an empty list is a cook with no probe in the food at all (wings, vegetables): the clock runs it
        chosen = meta["probes"] if "probes" in meta else [meta.get("probe", 1)]
        self.probe_nos = [int(n) for n in chosen]
        self.probe_labels = {int(k): str(v) for k, v in (meta.get("probe_labels") or {}).items()}
        self.probe_no = self.probe_nos[0] if self.probe_nos else 0
        self.serve_at = _dt(meta["serve_at"]) if meta.get("serve_at") else None

    # --- small helpers ----------------------------------------------------------------------------
    def _t(self, key: str) -> datetime | None:
        return _dt(self.state[key]) if self.state.get(key) else None

    def _cooled(self, kind: str, now: datetime, minutes: float) -> bool:
        """True if `kind` has not been said in the last `minutes` (and marks it as said now)."""
        last = self.state["said"].get(kind)
        if last and (now - _dt(last)) < timedelta(minutes=minutes):
            return False
        self.state["said"][kind] = _iso(now)
        return True

    def _once(self, kind: str, now: datetime) -> bool:
        if kind in self.state["said"]:
            return False
        self.state["said"][kind] = _iso(now)
        return True

    def probe_name(self, no: int) -> str:
        return self.probe_labels.get(no) or f"probe {no}"

    def probe_values(self, r: Reading | None) -> dict[int, float | None]:
        return {n: (r.probe(n) if r else None) for n in self.probe_nos}

    def probes_out(self) -> dict:
        """Probes that were ticked for this cook but are no longer in the meat (pulled, by hand or noticed)."""
        return {int(k): v for k, v in self.state.get("probes_out", {}).items()}

    def in_meat(self, r: Reading | None) -> dict[int, float]:
        """Readings of the probes that are in the meat right now: ticked, not pulled, and reading."""
        out = self.probes_out()
        return {n: v for n, v in self.probe_values(r).items() if v is not None and n not in out}

    def coldest(self, r: Reading | None) -> float | None:
        """The meat temperature that governs every decision: the coldest probe still in the meat. Probes come
        and go during a cook, so this is recomputed from whatever is in the meat at that moment."""
        values = list(self.in_meat(r).values())
        return min(values) if values else None

    def set_probe(self, no: int, in_meat: bool, now: datetime, why: str = "by hand") -> Event | None:
        st = self.state
        out = st.setdefault("probes_out", {})
        key = str(int(no))
        if int(no) not in self.probe_nos:
            return None
        if in_meat and key in out:
            del out[key]
            st.get("probe_track", {}).pop(key, None)
            e = Event("probe-in", f"{self.probe_name(int(no))} is counted again ({why})")
        elif not in_meat and key not in out:
            out[key] = {"at": _iso(now), "why": why}
            e = Event("probe-out", f"{self.probe_name(int(no))} is no longer counted ({why})")
        else:
            return None
        self.store.add_event(self.cook_id, now, e.kind, e.message)
        self.store.save_state(self.cook_id, st)
        return e

    def _watch_probes(self, now: datetime, r: Reading) -> list[Event]:
        """Notice a probe being pulled out of the meat (it suddenly reads far lower) and stop counting it, and
        notice it going back in (it returns to about where it was)."""
        st = self.state
        track = st.setdefault("probe_track", {})
        out = st.setdefault("probes_out", {})
        events: list[Event] = []
        for n, v in self.probe_values(r).items():
            key = str(n)
            if v is None:
                continue
            hist = track.setdefault(key, [])
            hist.append([_iso(now), v])
            cutoff = now - timedelta(seconds=PULLED_WINDOW_S)
            hist[:] = [h for h in hist if _dt(h[0]) >= cutoff]
            if key in out:
                before = out[key].get("before_c")
                if out[key].get("why", "").startswith("noticed") and before is not None and before - 5 <= v <= before + 8:
                    del out[key]
                    hist[:] = [[_iso(now), v]]
                    events.append(Event("probe-in", f"{self.probe_name(n)} is back at {v:.0f} C and is counted again"))
                continue
            peak = max(h[1] for h in hist)
            if peak - v >= PULLED_DROP_C:
                out[key] = {"at": _iso(now), "why": "noticed: fell fast", "before_c": peak}
                left = [self.probe_name(m) for m in self.probe_nos if str(m) not in out and r.probe(m) is not None]
                events.append(Event("probe-out", f"{self.probe_name(n)} fell from {peak:.0f} to {v:.0f} C in under "
                                                 f"{PULLED_WINDOW_S // 60} minutes: it looks pulled out, so it is no "
                                                 f"longer counted. " + (f"Still in the meat: {', '.join(left)}."
                                                                        if left else "No probe is left in the meat."),
                                    "info", {"probe": n, "from_c": round(peak, 1), "to_c": round(v, 1)}))
        return events

    def samples(self) -> list[model.Sample]:
        return [model.Sample(*s) for s in self.state["samples"]]

    def record_check(self, kind: str, data: dict, verdict: str | None, by: str, note: str, now: datetime) -> str:
        """Store what a photo (or a look) showed, as data points. Clears the matching open request."""
        from . import checks
        message = checks.summary(kind, data, verdict) + (f" ({note})" if note else "")
        payload = {"check": kind, **data}
        if verdict:
            payload["verdict"] = verdict
        if by:
            payload["by"] = by
        self.store.add_event(self.cook_id, now, f"check-{kind}", message, payload)
        st = self.state
        st.setdefault("checks", {})[kind] = {"at": _iso(now), "data": data, "verdict": verdict, "by": by or None}
        st["asks"] = [a for a in st.get("asks", []) if a["check"] != kind]
        self.store.save_state(self.cook_id, st)
        return message

    def _emit(self, events: list[Event], now: datetime) -> None:
        for e in events:
            want = (e.data or {}).get("check")
            if e.severity == "ask" and want in ("bark", "slice", "smoke", "probe", "raw"):
                asks = [a for a in self.state.setdefault("asks", []) if a["check"] != want]
                asks.append({"check": want, "since": _iso(now), "why": e.kind})
                self.state["asks"] = asks
            self.store.add_event(self.cook_id, now, e.kind, e.message, e.data or None)
            prefix = {"alert": "ALERT: ", "ask": "CHECK: "}.get(e.severity, "")
            self.notifier.send(f"[{self.recipe.name}] {prefix}{e.message}")

    # --- things the cook tells us -----------------------------------------------------------------
    def manual(self, kind: str, note: str, now: datetime) -> Event:
        """A step the cook did by hand: meat-on, wrap, spritz, rest, note."""
        r = self.recipe
        if kind == "meat-on":
            self.state["meat_on_at"] = _iso(now)
            self.state["samples"] = []
            asks = [a for a in self.state.setdefault("asks", []) if a["check"] != "probe"]
            asks.append({"check": "probe", "since": _iso(now), "why": "meat-on"})
            self.state["asks"] = asks
            msg = "meat on"
        elif kind == "wrap":
            self.state["wrapped_at"] = _iso(now)
            msg = "wrap" + (f" in {r.wrap_material}" if r.wrap_material else "")
        elif kind == "rest":
            self.state["rest_at"] = _iso(now)
            msg = f"off the pit, resting (minimum {r.rest_min_minutes} min, ideal {r.rest_ideal_minutes} min)"
        else:
            msg = {"hotfix": "hotfix, fuel added", "wood": "wood added"}.get(kind, kind)
        if note:
            msg += f": {note}"
        self.store.add_event(self.cook_id, now, kind, msg)
        self.store.save_state(self.cook_id, self.state)
        return Event(kind, msg)

    # --- the loop body ----------------------------------------------------------------------------
    def tick(self, r: Reading) -> list[Event]:
        now = r.at
        events: list[Event] = []
        st, rec = self.state, self.recipe
        meat = self.coldest(r)

        st["recent"].append([_iso(now), r.pit, r.fan])
        cutoff = now - timedelta(seconds=RECENT_KEEP_S)
        st["recent"] = [x for x in st["recent"] if _dt(x[0]) >= cutoff]

        if self._t("meat_on_at"):
            events += self._watch_probes(now, r)
            meat = self.coldest(r)
        if self._t("rest_at"):
            events += self._hold(now, self._t("rest_at"), meat, r)
            self._finish(events, now)
            return events

        if not r.connected or r.pit is None:
            st.setdefault("no_data_since", _iso(now))
            if now - _dt(st["no_data_since"]) > timedelta(minutes=3) and self._cooled("no-data", now, 30):
                events.append(Event("no-data", "no readings from the pit for 3 minutes; check the reader and the "
                                               "fan controller", "alert"))
            self._finish(events, now)
            return events
        st.pop("no_data_since", None)

        meat_on = self._t("meat_on_at")

        if meat_on is None:
            events += self._preheat_checks(now, r)
        else:
            self._sample(now, meat_on, meat, r)
            events += self._pit_checks(now, r)
            events += self._clock_checks(now, meat_on)
            if not self.probe_nos:
                pass                                  # nothing in the food to read: the clock checks carry the cook
            elif meat is None:
                st.setdefault("probe_lost_since", _iso(now))
                if now - _dt(st["probe_lost_since"]) > timedelta(minutes=2) and self._cooled("probe-lost", now, 30):
                    events.append(Event("probe-lost", "no meat probe is reading; check they are plugged in and "
                                                      "still in the meat", "alert"))
            else:
                st.pop("probe_lost_since", None)
                events += self._meat_checks(now, meat)
                events += self._schedule_checks(now)

        self._finish(events, now)
        return events

    def _finish(self, events: list[Event], now: datetime) -> None:
        self._emit(events, now)
        self.store.save_state(self.cook_id, self.state)

    def _sample(self, now: datetime, meat_on: datetime, meat: float | None, r: Reading) -> None:
        st = self.state
        last = self._t("last_sample")
        if last and (now - last).total_seconds() < SAMPLE_EVERY_S:
            return
        st["last_sample"] = _iso(now)
        if meat is not None:
            st["samples"].append([round((now - meat_on).total_seconds() / 3600, 4), meat, r.pit])
        row = {"at": _iso(now), "pit": r.pit, "fan": r.fan, "pit_target": r.pit_target, "ambient": r.ambient,
               "humidity": r.humidity, "rain": r.rain}
        if r.rain:
            st["rain_minutes"] = st.get("rain_minutes", 0) + 1
        p = self.projection(now) if meat is not None and "target" not in st["said"] else None
        if p and p[0] is not None:
            t_h = (now - meat_on).total_seconds() / 3600
            total_h = (p[0] - meat_on).total_seconds() / 3600
            row["est_off_min"] = round((p[0] - now).total_seconds() / 60)
            log = st.setdefault("est_log", [])
            if not log or t_h - log[-1][0] >= 0.25:           # one point every 15 minutes is plenty
                log.append([round(t_h, 3), round(total_h, 3), round(p[2].weight, 2)])
        row.update({f"probe{n}": r.probe(n) for n in range(1, MAX_PROBES + 1)})
        self.store.append_curve(self.cook_id, row)

    # --- checks -----------------------------------------------------------------------------------
    def _recent_pit(self, now: datetime, minutes: float) -> list[float]:
        cutoff = now - timedelta(minutes=minutes)
        return [x[1] for x in self.state["recent"] if _dt(x[0]) >= cutoff and x[1] is not None]

    def _preheat_checks(self, now: datetime, r: Reading) -> list[Event]:
        rec = self.recipe
        pits = self._recent_pit(now, 15)
        span = (now - _dt(self.state["recent"][0][0])).total_seconds() / 60 if self.state["recent"] else 0
        stable = span >= 14 and pits and all(abs(p - rec.pit_target_c) <= rec.pit_tolerance_c for p in pits)
        if stable and self._once("pit-ready", now):
            return [Event("pit-ready", f"pit has held {median(pits):.0f} C for 15 minutes. Run the smoke test, then "
                                       f"put the meat on and log it. A photo of the smoke completes the smoke test.",
                          "ask", {"check": "smoke"})]
        return []

    def _pit_checks(self, now: datetime, r: Reading) -> list[Event]:
        rec, st = self.recipe, self.state
        out: list[Event] = []
        earlier = [x[1] for x in st["recent"]
                   if x[1] is not None and timedelta(minutes=1) <= now - _dt(x[0]) <= timedelta(minutes=5)]
        if earlier and max(earlier) - r.pit >= LID_DROP_C:
            st["lid_at"] = _iso(now)
            if self._cooled("lid-open", now, 10):
                out.append(Event("lid-open", f"pit dropped {max(earlier) - r.pit:.0f} C in a few minutes: lid open? "
                                             f"Every look costs heat.", "info"))
        lid = self._t("lid_at")
        if lid and now - lid < timedelta(minutes=12):
            return out      # let the pit recover before judging it

        pits = self._recent_pit(now, 15)
        if len(pits) >= 10:
            mid = median(pits)
            if abs(mid - rec.pit_target_c) > rec.pit_tolerance_c and self._cooled("pit-drift", now, 30):
                way = "below" if mid < rec.pit_target_c else "above"
                out.append(Event("pit-drift", f"drift: pit has averaged {mid:.0f} C for 15 minutes, {way} the recipe's "
                                              f"{rec.pit_target_c:.0f} C (tolerance {rec.pit_tolerance_c:.0f}).",
                                 "alert", {"pit_c": round(mid, 1)}))
        fans = [x[2] for x in st["recent"] if x[2] is not None and now - _dt(x[0]) <= timedelta(minutes=10)]
        if (len(fans) >= 10 and min(fans) >= 90 and r.pit < rec.pit_target_c - rec.pit_tolerance_c
                and self._cooled("fire-dying", now, 30)):
            out.append(Event("fire-dying", f"fan has run at {min(fans):.0f}%+ for 10 minutes and the pit is still at "
                                           f"{r.pit:.0f} C: the fire is dying or out of fuel.", "alert"))
        return out

    def _meat_checks(self, now: datetime, meat: float) -> list[Event]:
        rec, st = self.recipe, self.state
        out: list[Event] = []
        samples = self.samples()
        wrapped = bool(st.get("wrapped_at"))
        rate_now = model.recent_rate(samples)

        if (rec.stall_enabled and not st.get("stall_at") and rec.stall_from_c <= meat < rec.stall_to_c
                and rate_now is not None and rate_now < STALL_RATE_C_PER_H):
            st["stall_at"] = _iso(now)
            out.append(Event("stall-start", f"stall detected at {meat:.0f} C (rising {max(rate_now, 0):.1f} C/h). "
                                            f"Bark check: send a photo of the bark, or judge it yourself: is it dark, "
                                            f"dry and set (does not rub off)? If yes, wrapping now shortens the "
                                            f"stall.", "ask",
                             {"meat_c": round(meat, 1), "rate_c_per_h": round(rate_now, 2), "check": "bark"}))
        if rec.wrap_at_c is not None and not wrapped and meat >= rec.wrap_at_c and self._once("wrap-point", now):
            out.append(Event("wrap-point", f"meat is at {meat:.0f} C, the recipe's wrap point. Check the bark, wrap"
                                           + (f" in {rec.wrap_material}" if rec.wrap_material else "")
                                           + " once the bark is set, and log the wrap. A photo of the bark first is useful.",
                             "ask", {"check": "bark"}))
        if st.get("stall_at") and meat >= rec.stall_to_c and self._once("stall-end", now):
            hours = (now - _dt(st["stall_at"])).total_seconds() / 3600
            out.append(Event("stall-end", f"through the stall after {fmt_dur(hours)}; meat at {meat:.0f} C and climbing.",
                             "info", {"stall_hours": round(hours, 2)}))

        fin = rec.finish or {}
        if fin.get("at_c") is not None and meat >= fin["at_c"] and self._once("finish", now):
            out.append(self._finish_event(f"meat is at {meat:.0f} C"))
        off = rec.off_c
        if off is None:
            return out
        after = "take it off and log the rest" if rec.rest_ideal_minutes else "take it off, serve, and close the cook"
        if off - 3 <= meat < off and self._once("near-target", now):
            out.append(Event("near-target", f"meat at {meat:.0f} C, {off - meat:.0f} C to go. Get ready to take it off.",
                             "info"))
        if meat >= off and self._once("target", now):
            carry = (f" It will carry over to about {rec.meat_target_c:.0f} C while it rests."
                     if rec.pull_c is not None else "")
            if rec.done_by == "feel":
                msg = (f"meat has reached {meat:.0f} C. Probe it in several places: it should slide in like butter. "
                       f"When it does, {after}.")
                check = "tenderness"
            elif rec.done_by == "temperature":
                msg, check = f"meat has reached {meat:.0f} C: {after}.{carry}", "temperature"
            else:                       # the clock or the bend test decides; the temperature is only a guide
                msg = (f"meat has reached {meat:.0f} C, the recipe's guide temperature. "
                       + ("Do the bend test: pick the rack up at one end, it should bend and the bark crack."
                          if rec.done_by == "bend" else "The clock decides this cook; this is just for information."))
                check = "guide"
            out.append(Event("target", msg, "ask" if rec.done_by != "time" else "info", {"check": check}))
        return out

    def _finish_event(self, why: str) -> Event:
        fin = self.recipe.finish
        how = {"sear": "sear it", "crisp": "crisp it up", "glaze": "glaze it", "sauce": "sauce it"}[fin["what"]]
        return Event("finish", f"{why}: time to {how}, {fin['minutes']:g} min"
                               + (f" with the pit at {fin['pit_c']:g} C" if fin.get("pit_c") else "") + ".",
                     "ask", {"finish": fin["what"]})

    def _clock_checks(self, now: datetime, meat_on: datetime) -> list[Event]:
        """Everything the clock decides: the change from one timed stage to the next, a finish step timed from
        the end, and the end itself when the recipe is done by time or by the bend test."""
        rec = self.recipe
        out: list[Event] = []
        elapsed_h = (now - meat_on).total_seconds() / 3600
        try:
            total_h = rec.cook_hours(self.meta.get("weight_kg"))
        except Exception:
            return out
        at_h = 0.0
        for i, stage in enumerate(rec.stages):
            if i and elapsed_h >= at_h and self._once(f"stage-{i}", now):
                out.append(Event("stage", f"stage {i + 1} of {len(rec.stages)}: {stage['name']}, {stage['hours']:g} h"
                                          + (f". {stage['note']}" if stage.get("note") else "."), "ask",
                                 {"stage": i + 1, "name": stage["name"]}))
            at_h += stage["hours"]
        fin = rec.finish or {}
        if fin.get("minutes_before_end") is not None and elapsed_h >= total_h - fin["minutes_before_end"] / 60 \
                and self._once("finish", now):
            out.append(self._finish_event(f"{fin['minutes_before_end']:g} min to go"))
        if rec.timed and elapsed_h >= total_h and self._once("time-up", now):
            after = "take it off and log the rest" if rec.rest_ideal_minutes else "take it off, serve, and close the cook"
            check = (" Check it with a thermometer first: it must be at least "
                     f"{rec.meat_target_c:.0f} C." if rec.meat_target_c is not None and not self.probe_nos else "")
            test = " Do the bend test first." if rec.done_by == "bend" else ""
            out.append(Event("time-up", f"{fmt_dur(total_h)} are up.{test}{check} Then {after}.", "ask"))
        return out

    def projection(self, now: datetime):
        """(off-the-pit time, Projection, Fit) from the current data, or None before the meat is on."""
        samples = self.samples()
        meat_on = self._t("meat_on_at")
        if meat_on is None:
            return None
        rec = self.recipe
        if rec.timed or rec.off_c is None:        # the clock decides: no model, the end is simply meat-on + time
            try:
                total = rec.cook_hours(self.meta.get("weight_kg"))
            except Exception:
                return None
            return meat_on + timedelta(hours=total), model.Projection(hours=total), model.Fit(self.prior, None, None, 0.0, 0.0)
        if not samples:
            return None
        wrapped_at = self._t("wrapped_at")
        since = (wrapped_at - meat_on).total_seconds() / 3600 if wrapped_at else None
        proj, fit = model.remaining(samples, self.prior, rec.off_c, rec.wrap_at_c, bool(wrapped_at), since)
        if proj.hours == float("inf"):
            return None, proj, fit
        last = meat_on + timedelta(hours=samples[-1].t_h)
        return last + timedelta(hours=proj.hours), proj, fit

    def _schedule_checks(self, now: datetime) -> list[Event]:
        rec = self.recipe
        if self.serve_at is None or self.recipe.timed or len(self.state["samples"]) < 45 \
                or "target" in self.state["said"]:
            return []
        if not self._cooled("schedule", now, 60):
            return []
        p = self.projection(now)
        if not p:
            return []
        off, _proj, _fit = p
        latest = self.serve_at - timedelta(minutes=rec.rest_min_minutes)
        if off is None:
            return [Event("behind", "at the current pit temperature the meat will not reach its target. "
                                    "Raise the pit or wrap.", "alert")]
        if off > latest:
            late = (off - latest).total_seconds() / 3600
            options = []
            if rec.wrap_at_c is not None and not self.state.get("wrapped_at"):
                options.append("wrap now")
            options += ["raise the pit by 10-15 C", "or serve later"]
            return [Event("behind", f"on-schedule check failing: done about {off.astimezone().strftime('%H:%M')}, which leaves less "
                                    f"than the minimum {rec.rest_min_minutes} min rest before serving at "
                                    f"{self.serve_at.astimezone().strftime('%H:%M')} ({fmt_dur(late)} short). Options: "
                                    + ", ".join(options) + ".", "alert",
                          {"projected_off": _iso(off), "late_hours": round(late, 2)})]
        return []

    # --- the rest and the hold ---------------------------------------------------------------------
    def _hold(self, now: datetime, resting: datetime, meat: float | None, r: Reading) -> list[Event]:
        """After the meat comes off it is still followed: a brisket can sit in a holding box for many hours, and
        what matters then is that it stays hot enough. Works with only the meat probes reading."""
        st, rec = self.state, self.recipe
        events = self._rest_checks(now, resting)
        last = self._t("last_sample")
        if not last or (now - last).total_seconds() >= SAMPLE_EVERY_S:
            st["last_sample"] = _iso(now)
            row = {"at": _iso(now), "pit": r.pit, "fan": r.fan, "pit_target": r.pit_target, "ambient": r.ambient,
                   "humidity": r.humidity, "rain": r.rain}
            row.update({f"probe{n}": r.probe(n) for n in range(1, MAX_PROBES + 1)})
            self.store.append_curve(self.cook_id, row)
            if meat is not None:
                t_h = round((now - resting).total_seconds() / 3600, 3)
                st.setdefault("hold_start_c", meat)
                st["hold_min_c"] = min(st.get("hold_min_c", meat), meat)
                st["hold_last_c"] = meat
                log = st.setdefault("hold", [])
                if not log or t_h - log[-1][0] >= 5 / 60:
                    log.append([t_h, meat])
                if meat < rec.hold_min_c:
                    st["hold_below_min"] = st.get("hold_below_min", 0) + 1
        if meat is None:
            return events
        stats = self.hold_stats(now, r)
        if meat < rec.hold_min_c:
            if self._cooled("hold-low", now, 30):
                events.append(Event("hold-low", f"meat has dropped to {meat:.0f} C, below the {rec.hold_min_c:.0f} C "
                                                f"holding floor. Serve it now or get it somewhere hotter: it should "
                                                f"not sit below this for long.", "alert", {"meat_c": round(meat, 1)}))
        elif meat < rec.hold_min_c + 4 and self._once("hold-near", now):
            left = stats.get("hours_until_floor")
            events.append(Event("hold-near", f"hold: meat is at {meat:.0f} C, {meat - rec.hold_min_c:.0f} C above the "
                                             f"{rec.hold_min_c:.0f} C floor"
                                             + (f", about {fmt_dur(left)} left at this rate." if left else "."), "info"))
        return events

    def hold_stats(self, now: datetime, r: Reading | None) -> dict:
        """Where the hold stands: how long, how hot, how fast it is cooling and how long until the floor."""
        st, rec = self.state, self.recipe
        resting = self._t("rest_at")
        if resting is None:
            return {}
        meat = self.coldest(r) if r is not None else None
        if meat is None:
            meat = st.get("hold_last_c")
        out = {"since": st["rest_at"], "hours": round((now - resting).total_seconds() / 3600, 2), "meat_c": meat,
               "start_c": st.get("hold_start_c"), "min_c": st.get("hold_min_c"), "floor_c": rec.hold_min_c,
               "box_c": r.pit if r is not None else None, "minutes_below_floor": st.get("hold_below_min", 0),
               "rate_c_per_h": None, "hours_until_floor": None}
        log = st.get("hold") or []
        if len(log) >= 2:
            recent = [p for p in log if log[-1][0] - p[0] <= 1.0]
            first = recent[0] if len(recent) >= 2 and recent[-1][0] - recent[0][0] >= 0.25 else log[0]
            span = log[-1][0] - first[0]
            if span >= 0.25:
                rate = (log[-1][1] - first[1]) / span
                out["rate_c_per_h"] = round(rate, 2)
                if meat is not None and rate < -0.05 and meat > rec.hold_min_c:
                    out["hours_until_floor"] = round((meat - rec.hold_min_c) / -rate, 2)
        return out

    def _rest_checks(self, now: datetime, resting: datetime) -> list[Event]:
        rec = self.recipe
        mins = (now - resting).total_seconds() / 60
        if mins >= rec.rest_ideal_minutes and self._once("rest-ideal", now):
            return [Event("rest-ideal", f"rested {rec.rest_ideal_minutes} min: slice whenever you are ready, or keep "
                                        f"holding it above {rec.hold_min_c:.0f} C. Close the cook with your score "
                                        f"after serving. A photo of a slice records the smoke ring and moisture.",
                          "ask", {"check": "slice"})]
        if mins >= rec.rest_min_minutes and self._once("rest-min", now):
            return [Event("rest-min", f"minimum rest of {rec.rest_min_minutes} min reached; it can be sliced from now. "
                                      f"Longer is better, and a long hold is fine while the meat stays above "
                                      f"{rec.hold_min_c:.0f} C.", "info")]
        return []

    # --- structured views for the web page, the API and agents ------------------------------------
    def phase(self) -> str:
        st = self.state
        if st.get("rest_at"):
            return "resting"
        if not st.get("meat_on_at"):
            return "preheating"
        if st.get("stall_at") and "stall-end" not in st["said"]:
            return "stall"
        return "cooking"

    def pipeline(self) -> list[dict]:
        """The cook as a row of stages: done, active, failed or pending, with the time each was reached."""
        st, said, rec = self.state, self.state["said"], self.recipe
        smoke = st.get("smoke")
        stages = [
            ("smoke-test", "Smoke test", (smoke or {}).get("at") if smoke and smoke.get("passed") else None),
            ("meat-on", "Meat on", st.get("meat_on_at")),
        ]
        if rec.stall_enabled:
            stages.append(("stall", "Stall", st.get("stall_at")))
        if rec.wrap_at_c is not None:
            stages.append(("wrap", "Wrap", st.get("wrapped_at")))
        if rec.stall_enabled:
            stages.append(("stall-end", "Through the stall", said.get("stall-end")))
        for i, stage in enumerate(rec.stages):
            if i:
                stages.append((f"stage-{i}", stage["name"].capitalize(), said.get(f"stage-{i}")))
        if rec.finish:
            stages.append(("finish", rec.finish["what"].capitalize(), said.get("finish")))
        done = {"feel": ("target", "Probe tender"), "temperature": ("target", "At temperature"),
                "bend": ("time-up", "Bend test"), "time": ("time-up", "Time up")}[rec.done_by]
        stages.append((done[0], done[1], said.get(done[0])))
        if rec.rest_ideal_minutes:
            stages.append(("rest", "Rest", st.get("rest_at")))
        out, active_set = [], False
        last_done = max((i for i, s in enumerate(stages) if s[2]), default=-1)
        for i, (key, title, at) in enumerate(stages):
            if at:
                state = "done"
            elif i < last_done:
                state = "skipped"
            elif not active_set:
                state, active_set = "active", True
            else:
                state = "pending"
            if key == "smoke-test" and smoke and not smoke.get("passed") and not at:
                state = "failed"
            out.append({"key": key, "title": title, "state": state, "at": at})
        return out

    def note_smoke_test(self, now: datetime, ok: bool) -> None:
        self.state["smoke"] = {"at": _iso(now), "passed": ok}
        self.store.save_state(self.cook_id, self.state)

    def live_checks(self, now: datetime, r: Reading | None) -> list[dict]:
        """Status checks on the running cook: pass, fail or pending, each with one line of detail."""
        rec, st = self.recipe, self.state
        out = []

        def add(name, state, detail):
            out.append({"name": name, "state": state, "detail": detail})

        if st.get("rest_at") and r is not None and self.coldest(r) is not None:
            pass                              # during the hold the meat probes are enough
        elif r is None or not r.connected or r.pit is None:
            add("data-feed", "fail", "no readings from the pit")
            return out
        add("data-feed", "pass", "readings arriving")
        if not st.get("meat_on_at"):
            smoke = st.get("smoke")
            if smoke:
                add("smoke-test", "pass" if smoke["passed"] else "fail",
                    "passed" if smoke["passed"] else "failed; fix and run it again")
            else:
                add("smoke-test", "pending", "run it once the pit is steady")
            return out
        if st.get("rest_at"):
            mins = (now - _dt(st["rest_at"])).total_seconds() / 60
            add("rest", "pass" if mins >= rec.rest_min_minutes else "pending",
                f"{mins:.0f} of {rec.rest_min_minutes} min minimum ({rec.rest_ideal_minutes} ideal)")
            h = self.hold_stats(now, r)
            if h.get("meat_c") is None:
                add("hold-temperature", "pending", "no meat probe is reading")
            elif h["meat_c"] < rec.hold_min_c:
                add("hold-temperature", "fail", f"{h['meat_c']:.1f} C, below the {rec.hold_min_c:.0f} C floor")
            else:
                left = h.get("hours_until_floor")
                using = self.in_meat(r)
                add("hold-temperature", "pass", f"{h['meat_c']:.1f} C, floor {rec.hold_min_c:.0f} C"
                    + (f", about {fmt_dur(left)} left at this rate" if left else "")
                    + (f" (from {', '.join(self.probe_name(n) for n in using)})" if using else ""))
            return out
        pits = self._recent_pit(now, 15) or [r.pit]
        mid = median(pits)
        ok = abs(mid - rec.pit_target_c) <= rec.pit_tolerance_c
        add("pit-stable", "pass" if ok else "fail",
            f"averaging {mid:.0f} C, recipe {rec.pit_target_c:.0f} +/- {rec.pit_tolerance_c:.0f}")
        if r.fan is not None:
            add("fan-headroom", "fail" if r.fan >= 95 else "pass", f"fan at {r.fan:.0f}%")
        pulled = self.probes_out()
        values = {n: v for n, v in self.probe_values(r).items() if n not in pulled}
        live = self.in_meat(r)
        meat = self.coldest(r)
        missing = [self.probe_name(n) for n, v in values.items() if v is None]
        note = f"; pulled: {', '.join(self.probe_name(n) for n in pulled)}" if pulled else ""
        if not self.probe_nos:
            meat_on = self._t("meat_on_at")
            total = rec.cook_hours(self.meta.get("weight_kg")) if rec.timed else None
            if meat_on and total:
                left = total - (now - meat_on).total_seconds() / 3600
                add("clock", "pass" if left > 0 else "pending",
                    f"{fmt_dur(left)} to go" if left > 0 else f"time is up ({fmt_dur(-left)} over)")
        elif meat is None:
            add("meat-probes", "fail", "no probe is reading in the meat" + note)
        elif missing:
            add("meat-probes", "fail", f"not reading: {', '.join(missing)}" + note)
        elif len(live) > 1:
            spread = max(live.values()) - meat
            add("meat-probes", "pass", f"{len(live)} probes, coldest {meat:.1f} C, spread {spread:.1f} C" + note)
        else:
            add("meat-probes", "pass", f"{meat:.1f} C" + note)
        if rec.wrap_at_c is not None:
            if st.get("wrapped_at"):
                add("wrapped", "pass", "wrapped")
            elif meat is not None and meat >= rec.wrap_at_c:
                add("wrapped", "fail", f"past the wrap point of {rec.wrap_at_c:.0f} C and not wrapped")
            else:
                add("wrapped", "pending", f"wrap at about {rec.wrap_at_c:.0f} C")
        if self.serve_at:
            p = self.projection(now)
            if not p or (not rec.timed and len(st["samples"]) < 20):
                add("on-schedule", "pending", "not enough readings yet")
            elif p[0] is None:
                add("on-schedule", "fail", "will not reach target at this pit temperature")
            else:
                slack = (self.serve_at - p[0]).total_seconds() / 60
                add("on-schedule", "pass" if slack >= rec.rest_min_minutes else "fail",
                    f"off about {p[0].astimezone().strftime('%H:%M')}, {slack:.0f} min before serving")
        return out

    def overview(self, now: datetime, r: Reading | None) -> dict:
        rec, st = self.recipe, self.state
        meat = self.coldest(r)
        est = None
        p = self.projection(now) if st.get("meat_on_at") and not st.get("rest_at") else None
        if p:
            off, _proj, f = p
            est = {"off_at": _iso(off) if off else None, "data_weight": round(f.weight, 2)}
            if off and self.serve_at:
                est["rest_available_min"] = round((self.serve_at - off).total_seconds() / 60)
        return {
            "id": self.cook_id, "recipe": rec.name, "recipe_version": rec.version, "cut": rec.cut,
            "weight_kg": self.meta.get("weight_kg"), "serve_at": self.meta.get("serve_at"),
            "style": rec.style, "done_by": rec.done_by, "rests": bool(rec.rest_ideal_minutes),
            "opened_at": self.meta.get("opened_at"), "phase": self.phase(), "wrapped": bool(st.get("wrapped_at")),
            "meat_on_at": st.get("meat_on_at"),
            "probes": [{"no": n, "label": self.probe_name(n), "temp_c": v, "in_meat": n not in self.probes_out()}
                       for n, v in self.probe_values(r).items()],
            "targets": {"pit_c": rec.pit_target_c, "pit_tolerance_c": rec.pit_tolerance_c,
                        "meat_c": rec.meat_target_c, "off_c": rec.off_c, "stall": rec.stall_enabled,
                        "stall_from_c": rec.stall_from_c, "stall_to_c": rec.stall_to_c,
                        "wrap_at_c": rec.wrap_at_c},
            "meat_c": meat, "estimate": est, "pipeline": self.pipeline(), "checks": self.live_checks(now, r),
            "asks": st.get("asks", []), "photo_checks": st.get("checks", {}),
            "hold": self.hold_stats(now, r) if st.get("rest_at") else None,
        }

    def analysis(self, now: datetime) -> dict:
        """What this cook teaches: how the meat actually behaved against what the recipe assumed, and how good
        the estimate was along the way. Written into cook.yaml when the cook closes."""
        from statistics import mean, pstdev
        st, rec = self.state, self.recipe
        meat_on = self._t("meat_on_at")
        if meat_on is None:
            return {}
        hours = lambda key, src=st: round((_dt(src[key]) - meat_on).total_seconds() / 3600, 2) if src.get(key) else None  # noqa: E731
        samples = self.samples()
        out: dict = {"meat_on_at": st["meat_on_at"], "readings": len(samples)}
        end = self._t("rest_at") or now
        out["hours_on_pit"] = round((end - meat_on).total_seconds() / 3600, 2)
        out["planned_hours"] = round(rec.cook_hours(self.meta.get("weight_kg")), 2) if (
            rec.hours_fixed is not None or self.meta.get("weight_kg")) else None
        out["hours_to_target"] = hours("target", st["said"])
        out["stall"] = {"start_h": hours("stall_at"), "end_h": hours("stall-end", st["said"])}
        if out["stall"]["start_h"] is not None and out["stall"]["end_h"] is not None:
            out["stall"]["hours"] = round(out["stall"]["end_h"] - out["stall"]["start_h"], 2)
        out["wrapped_at_h"] = hours("wrapped_at")
        if samples:
            pits = [s.pit for s in samples if s.pit is not None]
            out["pit"] = {"mean_c": round(mean(pits), 1), "min_c": round(min(pits), 1), "max_c": round(max(pits), 1),
                          "stdev_c": round(pstdev(pits), 1), "recipe_c": rec.pit_target_c}
            out["meat"] = {"start_c": samples[0].meat, "end_c": samples[-1].meat, "target_c": rec.meat_target_c}
            f = model.fit(samples, self.prior, hours("wrapped_at"))
            out["model"] = {"k_recipe": round(self.prior.k, 4), "k_fitted": round(f.params.k, 4),
                            "k_ratio": round(f.params.k / self.prior.k, 2), "stall_factor": round(f.params.stall_factor, 3),
                            "wrapped_factor": round(f.params.wrapped_factor, 3), "data_weight": round(f.weight, 2)}
        out["rain_minutes"] = st.get("rain_minutes", 0)
        if st.get("rest_at"):
            h = self.hold_stats(now, None)
            log = st.get("hold") or []
            out["hold"] = {"hours": h["hours"], "start_c": h["start_c"], "end_c": st.get("hold_last_c"),
                           "min_c": h["min_c"], "floor_c": h["floor_c"], "minutes_below_floor": h["minutes_below_floor"],
                           "mean_rate_c_per_h": round((log[-1][1] - log[0][1]) / (log[-1][0] - log[0][0]), 2)
                           if len(log) >= 2 and log[-1][0] > log[0][0] else None}
        total = out["hours_to_target"]
        if total and st.get("est_log"):
            marks = {}
            for share in (0.25, 0.5, 0.75):
                at = min(st["est_log"], key=lambda e: abs(e[0] - total * share))
                if abs(at[0] - total * share) <= 0.5:
                    marks[f"at_{int(share * 100)}pct"] = {"hours_in": at[0], "predicted_total_h": at[1],
                                                           "error_h": round(at[1] - total, 2)}
            out["estimate"] = {"actual_total_h": total, **marks}
        return out

    # --- status -----------------------------------------------------------------------------------
    def status(self, now: datetime, r: Reading | None = None) -> str:
        rec, st = self.recipe, self.state
        lines = [f"{self.cook_id}: {rec.name} v{rec.version}"]
        meat_on = self._t("meat_on_at")
        if self._t("rest_at"):
            h = self.hold_stats(now, r)
            phase = f"resting / holding for {fmt_dur(h['hours'])}"
            if h.get("meat_c") is not None:
                phase += f", meat at {h['meat_c']:.0f} C (floor {h['floor_c']:.0f})"
                if h.get("hours_until_floor"):
                    phase += f", about {fmt_dur(h['hours_until_floor'])} until the floor"
        elif meat_on is None:
            phase = "preheating (meat not on yet)"
        elif st.get("stall_at") and "stall-end" not in st["said"]:
            phase = "in the stall"
        else:
            phase = "cooking"
        if meat_on:
            phase += f", {fmt_dur((now - meat_on).total_seconds() / 3600)} since meat on"
        lines.append(f"phase: {phase}" + (", wrapped" if st.get("wrapped_at") else ""))
        if r is not None:
            meat = self.coldest(r)
            lines.append("now: pit " + (f"{r.pit:.0f} C" if r.pit is not None else "no reading")
                         + f" (recipe {rec.pit_target_c:.0f}), meat "
                         + (f"{meat:.1f} C" if meat is not None else "no reading")
                         + (" (coldest of " + ", ".join(f"{self.probe_name(n)} {v:.1f}" for n, v in
                                                        self.in_meat(r).items()) + ")"
                            if len(self.in_meat(r)) > 1 and meat is not None else "")
                         + (f" (target {rec.off_c:.0f}), fan " if rec.off_c is not None else ", fan ")
                         + (f"{r.fan:.0f}%" if r.fan is not None else "n/a"))
        p = self.projection(now) if meat_on and not self._t("rest_at") else None
        if p:
            off, _proj, fit = p
            if off is None:
                lines.append("estimate: will not reach target at this pit temperature")
            else:
                basis = "recipe timing only" if fit.weight == 0 else f"{fit.weight:.0%} from this cook's data"
                lines.append(f"estimate: off the pit about {off.astimezone().strftime('%a %H:%M')} ({basis})")
                if self.serve_at:
                    slack = (self.serve_at - off).total_seconds() / 60
                    verdict = "on schedule" if slack >= rec.rest_min_minutes else "BEHIND"
                    lines.append(f"serving {self.serve_at.astimezone().strftime('%a %H:%M')}: {slack:.0f} min of rest available "
                                 f"({verdict})")
        return "\n".join(lines)
