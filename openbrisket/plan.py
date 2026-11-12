"""`plan`: work the whole timeline back from the serving time, before anything is lit."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import model
from .recipes import Recipe

PREHEAT_MIN = 45        # light the fire this long before the meat goes on (covers the smoke test)
BUFFER_FRACTION = 0.10  # slack on the cook time; a long rest absorbs it if unused


@dataclass
class Step:
    at: datetime
    what: str
    detail: str = ""


@dataclass
class Plan:
    recipe: Recipe
    weight_kg: float | None
    serve_at: datetime
    cook_hours: float
    params: model.Params
    steps: list[Step]

    @property
    def meat_on(self) -> datetime:
        return next(s.at for s in self.steps if s.what == "meat on")


NO_STALL = 10_000.0     # a stall band no meat ever reaches: how "this food does not stall" is told to the model


def prior_params(recipe: Recipe, weight_kg: float | None) -> model.Params:
    lo, hi = (recipe.stall_from_c, recipe.stall_to_c) if recipe.stall_enabled else (NO_STALL, NO_STALL + 1)
    base = model.Params(k=1.0, stall_lo=lo, stall_hi=hi)
    if recipe.off_c is None:               # no temperature to aim at: the clock runs this cook, k is never used
        return base
    k = model.calibrate_k(recipe.cook_hours(weight_kg), recipe.pit_target_c, recipe.off_c, base, recipe.wrap_at_c)
    return model.Params(k=k, stall_lo=lo, stall_hi=hi)


DONE_LABEL = {"feel": "probe tender (expected)", "temperature": "at temperature (expected)",
              "bend": "bend test", "time": "time is up"}


def make_plan(recipe: Recipe, weight_kg: float | None, serve_at: datetime) -> Plan:
    hours = recipe.cook_hours(weight_kg)
    params = prior_params(recipe, weight_kg)
    by_temp = not recipe.timed and recipe.off_c is not None
    proj = model.simulate(model.FRIDGE_C, recipe.pit_target_c, recipe.off_c, params, recipe.wrap_at_c) if by_temp \
        else model.Projection(hours=hours)
    finish = recipe.finish or {}
    finish_h = float(finish.get("minutes", 0)) / 60 if finish.get("at_c") is not None else 0.0   # a sear comes on top

    rest = timedelta(minutes=recipe.rest_ideal_minutes)
    off = serve_at - rest
    # a clock-run cook needs no slack; one that waits for the meat gets some, which a longer rest absorbs
    slack = 1.0 if recipe.timed else 1 + BUFFER_FRACTION
    meat_on = off - timedelta(hours=hours * slack + finish_h)
    steps: list[Step] = []
    if recipe.dry_brine_hours:
        steps.append(Step(meat_on - timedelta(hours=recipe.dry_brine_hours), "salt / dry brine",
                          f"{recipe.dry_brine_hours:g} h before the cook, uncovered in the fridge"))
    if recipe.fridge_out_min or recipe.rub:
        steps.append(Step(meat_on - timedelta(minutes=recipe.fridge_out_min), "out of the fridge",
                          f"trim, apply the rub ({recipe.rub})" if recipe.rub else "trim and pat dry"))
    steps.append(Step(meat_on - timedelta(minutes=PREHEAT_MIN), "light the fire",
                      f"bring the pit to {recipe.pit_target_c:g} C, then run the smoke test"))
    steps.append(Step(meat_on, "meat on", "smoke test passed" + (", probes in" if recipe.probe != "none" else "")))
    if recipe.stall_enabled and proj.stall_start_h is not None:
        steps.append(Step(meat_on + timedelta(hours=proj.stall_start_h), "stall expected",
                          f"meat near {recipe.stall_from_c:g} C; bark photo"))
    if by_temp and proj.wrap_h is not None:
        steps.append(Step(meat_on + timedelta(hours=proj.wrap_h), "wrap",
                          f"at about {recipe.wrap_at_c:g} C" + (f", {recipe.wrap_material}" if recipe.wrap_material else "")))
    at_h = 0.0
    for i, stage in enumerate(recipe.stages):
        if i:
            steps.append(Step(meat_on + timedelta(hours=at_h), f"stage: {stage['name']}",
                              f"{stage['hours']:g} h" + (f"; {stage['note']}" if stage.get("note") else "")))
        at_h += stage["hours"]
    if finish:
        when = meat_on + timedelta(hours=hours) if finish.get("at_c") is not None \
            else meat_on + timedelta(hours=hours) - timedelta(minutes=float(finish["minutes_before_end"]))
        steps.append(Step(when, finish["what"], f"{finish['minutes']:g} min"
                          + (f" at {finish['pit_c']:g} C" if finish.get("pit_c") else "")
                          + (f", once the meat is at {finish['at_c']:g} C" if finish.get("at_c") is not None else "")))
    if recipe.off_c is None:
        target = "by the clock"
    elif recipe.done_by == "time":
        target = f"check with a thermometer: at least {recipe.off_c:g} C"
    elif recipe.done_by == "bend":
        target = f"the rack bends and the bark cracks; about {recipe.off_c:g} C between the bones"
    else:
        target = f"meat at {recipe.off_c:g} C"
    if recipe.pull_c is not None:
        target += f" (it carries over to {recipe.meat_target_c:g} C)"
    steps.append(Step(meat_on + timedelta(hours=hours + finish_h), DONE_LABEL[recipe.done_by], target))
    if recipe.rest_ideal_minutes:
        steps.append(Step(off, "off and resting (latest)",
                          f"rest {recipe.rest_ideal_minutes} min ideal, {recipe.rest_min_minutes} min minimum"))
    steps.append(Step(serve_at, "serve"))
    steps.sort(key=lambda s: s.at)
    return Plan(recipe, weight_kg, serve_at, hours, params, steps)


def render(plan: Plan) -> str:
    r = plan.recipe
    size = f"{plan.weight_kg:g} kg" if plan.weight_kg else "fixed time"
    lines = [f"Plan: {r.name} v{r.version} ({r.cut}, {size})",
             f"Pit {r.pit_target_c:g} C, " + (f"meat to {r.meat_target_c:g} C, " if r.meat_target_c is not None else "")
             + f"about {plan.cook_hours:.1f} h on the pit ({r.style}, done by {r.done_by})",
             ""]
    day = None
    for s in plan.steps:
        d = s.at.strftime("%a %d %b")
        if d != day:
            lines.append(d)
            day = d
        lines.append(f"  {s.at.strftime('%H:%M')}  {s.what}" + (f"  - {s.detail}" if s.detail else ""))
    return "\n".join(lines)
