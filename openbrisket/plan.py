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


def prior_params(recipe: Recipe, weight_kg: float | None) -> model.Params:
    base = model.Params(k=1.0, stall_lo=recipe.stall_from_c, stall_hi=recipe.stall_to_c)
    k = model.calibrate_k(recipe.cook_hours(weight_kg), recipe.pit_target_c, recipe.meat_target_c, base,
                          recipe.wrap_at_c)
    return model.Params(k=k, stall_lo=recipe.stall_from_c, stall_hi=recipe.stall_to_c)


def make_plan(recipe: Recipe, weight_kg: float | None, serve_at: datetime) -> Plan:
    hours = recipe.cook_hours(weight_kg)
    params = prior_params(recipe, weight_kg)
    proj = model.simulate(model.FRIDGE_C, recipe.pit_target_c, recipe.meat_target_c, params, recipe.wrap_at_c)

    rest = timedelta(minutes=recipe.rest_ideal_minutes)
    off = serve_at - rest
    meat_on = off - timedelta(hours=hours * (1 + BUFFER_FRACTION))
    steps: list[Step] = []
    if recipe.dry_brine_hours:
        steps.append(Step(meat_on - timedelta(hours=recipe.dry_brine_hours), "salt / dry brine",
                          f"{recipe.dry_brine_hours:g} h before the cook, uncovered in the fridge"))
    steps.append(Step(meat_on - timedelta(minutes=recipe.fridge_out_min), "meat out of the fridge",
                      "trim, apply the rub" + (f" ({recipe.rub})" if recipe.rub else "")))
    steps.append(Step(meat_on - timedelta(minutes=PREHEAT_MIN), "light the fire",
                      f"bring the pit to {recipe.pit_target_c:g} C, then run the smoke test"))
    steps.append(Step(meat_on, "meat on", "smoke test passed, probes in"))
    if proj.stall_start_h is not None:
        steps.append(Step(meat_on + timedelta(hours=proj.stall_start_h), "stall expected",
                          f"meat near {recipe.stall_from_c:g} C; bark photo"))
    if proj.wrap_h is not None:
        steps.append(Step(meat_on + timedelta(hours=proj.wrap_h), "wrap",
                          f"at about {recipe.wrap_at_c:g} C" + (f", {recipe.wrap_material}" if recipe.wrap_material else "")))
    steps.append(Step(meat_on + timedelta(hours=hours), "probe tender (expected)",
                      f"meat at {recipe.meat_target_c:g} C; feel test"))
    steps.append(Step(off, "off and resting (latest)",
                      f"rest {recipe.rest_ideal_minutes} min ideal, {recipe.rest_min_minutes} min minimum"))
    steps.append(Step(serve_at, "serve"))
    steps.sort(key=lambda s: s.at)
    return Plan(recipe, weight_kg, serve_at, hours, params, steps)


def render(plan: Plan) -> str:
    r = plan.recipe
    size = f"{plan.weight_kg:g} kg" if plan.weight_kg else "fixed time"
    lines = [f"Plan: {r.name} v{r.version} ({r.cut}, {size})",
             f"Pit {r.pit_target_c:g} C, meat to {r.meat_target_c:g} C, about {plan.cook_hours:.1f} h on the pit",
             ""]
    day = None
    for s in plan.steps:
        d = s.at.strftime("%a %d %b")
        if d != day:
            lines.append(d)
            day = d
        lines.append(f"  {s.at.strftime('%H:%M')}  {s.what}" + (f"  - {s.detail}" if s.detail else ""))
    return "\n".join(lines)
