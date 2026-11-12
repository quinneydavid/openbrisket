"""Recipes as code: load and validate recipe and rub files.

A recipe is a YAML file. `validate_recipe` returns a list of problems (empty = valid); it is what the
`smoke-test` CI job runs on every pull request, including the food-safety floor per kind of meat.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

API_VERSION = "openbrisket/v1"

# Lowest finishing temperature (deg C) a recipe may declare, by the first word of `cut`.
# Whole-muscle values; anything minced must be declared as `<meat>-ground`.
MIN_SAFE_C = {
    "poultry": 74, "chicken": 74, "turkey": 74, "duck": 74,
    "pork": 63, "beef": 54, "lamb": 54, "fish": 52, "sausage": 71,
}
GROUND_MIN_SAFE_C = 71
PIT_RANGE_C = (80, 200)
FINISH_PIT_MAX_C = 350     # a sear at the end may run far hotter than the smoke

# What kind of cook a recipe is. The style only sets defaults: any of them can be overridden in the recipe.
#   stall    is there a stall to watch for
#   done_by  feel = probe tender (temperature is a guide) | temperature | bend (ribs) | time
#   probe    needed | optional | none
#   rest     default minimum and ideal rest, minutes
STYLES = {
    "low-and-slow": {"stall": True,  "done_by": "feel",        "probe": "needed",   "rest": (30, 60)},
    "ribs":         {"stall": False, "done_by": "bend",        "probe": "optional", "rest": (10, 20)},
    "poultry":      {"stall": False, "done_by": "temperature", "probe": "needed",   "rest": (10, 15)},
    "roast":        {"stall": False, "done_by": "temperature", "probe": "needed",   "rest": (10, 20)},
    "fish":         {"stall": False, "done_by": "temperature", "probe": "optional", "rest": (0, 0)},
    "sausage":      {"stall": False, "done_by": "temperature", "probe": "optional", "rest": (0, 5)},
    "quick":        {"stall": False, "done_by": "time",        "probe": "none",     "rest": (0, 0)},
}
DONE_BY = ("feel", "temperature", "bend", "time")
PROBE = ("needed", "optional", "none")
FINISHES = ("sear", "crisp", "glaze", "sauce")
NO_FLOOR = {"vegetable", "cheese", "nut", "fruit", "other"}      # cuts with no meat-safety floor
HOLD_FLOOR_C = 57          # hot-holding limit; recipes default to 60 for a margin


class RecipeError(Exception):
    pass


@dataclass
class Recipe:
    name: str
    version: str
    cut: str
    pit_target_c: float
    pit_tolerance_c: float
    meat_target_c: float | None
    hours_per_kg: float | None
    hours_fixed: float | None
    fridge_out_min: int
    dry_brine_hours: float
    stall_from_c: float
    stall_to_c: float
    wrap_at_c: float | None
    wrap_material: str | None
    rest_min_minutes: int
    rest_ideal_minutes: int
    hold_min_c: float = 60.0      # below this the meat should not be held; the hold is watched against it
    style: str = "low-and-slow"
    done_by: str = "feel"
    probe: str = "needed"
    stall_enabled: bool = True
    pull_c: float | None = None   # take it off at this temperature; it carries over to the target while resting
    stages: list = field(default_factory=list)     # timed stages: [{name, hours, note}]
    finish: dict | None = None    # a last step: {what, pit_c, minutes, at_c | minutes_before_end}
    rub: str | None = None
    description: str = ""
    path: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def timed(self) -> bool:
        """True when the clock decides the end, not a temperature."""
        return self.done_by in ("time", "bend")

    @property
    def off_c(self) -> float | None:
        """The meat temperature at which it comes off: the pull temperature if there is one, else the target."""
        return self.pull_c if self.pull_c is not None else self.meat_target_c

    def cook_hours(self, weight_kg: float | None) -> float:
        if self.stages:
            return float(sum(float(s["hours"]) for s in self.stages))
        if self.hours_fixed is not None:
            return float(self.hours_fixed)
        if weight_kg is None:
            raise RecipeError(f"recipe {self.name} is sized by weight: give the meat's weight")
        return float(self.hours_per_kg) * float(weight_kg)


def _num(d: dict, key: str, problems: list[str], where: str, required: bool = True):
    v = d.get(key)
    if v is None:
        if required:
            problems.append(f"{where}.{key} is missing")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        problems.append(f"{where}.{key} must be a number")
        return None
    return v


def min_safe_c(cut: str) -> float | None:
    words = str(cut).lower().split("-")
    floor = MIN_SAFE_C.get(words[0])
    if floor is not None and "ground" in words:
        floor = max(floor, GROUND_MIN_SAFE_C)
    return floor


def find_rub(rub: str, base_dir: str | None, extra_dirs: list[str] | None = None) -> str | None:
    """Where a recipe's rub file is: next to the recipe first, then in each library folder."""
    for d in [base_dir, *(extra_dirs or [])]:
        if d:
            path = os.path.normpath(os.path.join(d, rub))
            if os.path.isfile(path):
                return path
    return None


SUGAR_SCORCH_C = 135     # above this pit temperature a sugary rub darkens and turns bitter
SUGARY = 0.25


def warnings(doc: dict, rub_doc: dict | None) -> list[str]:
    """Things worth knowing that do not make a recipe invalid."""
    out: list[str] = []
    pit = (doc.get("pit") or {}).get("target_c")
    food = str(doc.get("cut", "")).split("-")[0]
    if food in ("chicken", "turkey", "duck", "poultry") and isinstance(pit, (int, float)) and pit < 135 \
            and not doc.get("finish"):
        out.append(f"poultry at a pit of {pit:g} C gives rubbery skin: run 135 C or hotter, or add a crisp finish")
    if rub_doc and isinstance(pit, (int, float)):
        fr = rub_fractions(rub_doc)
        if fr["sugar"] >= SUGARY and pit > SUGAR_SCORCH_C:
            out.append(f"the rub is {fr['sugar']:.0%} sugar and the pit runs at {pit:g} C: above {SUGAR_SCORCH_C} C "
                       f"sugar scorches and the bark turns bitter")
        brine = (doc.get("timing") or {}).get("dry_brine_hours") or 0
        if fr["salt"] >= 0.5 and brine >= 12:
            out.append(f"the rub is {fr['salt']:.0%} salt and the recipe also dry-brines for {brine:g} h: salt the "
                       f"meat once, not twice")
    return out


def validate_recipe(doc: dict, base_dir: str | None = None, rub_dirs: list[str] | None = None) -> list[str]:
    """Return every problem found in a parsed recipe document."""
    p: list[str] = []
    if not isinstance(doc, dict):
        return ["recipe is not a mapping"]
    if doc.get("apiVersion") != API_VERSION:
        p.append(f"apiVersion must be {API_VERSION}")
    if doc.get("kind") != "Recipe":
        p.append("kind must be Recipe")
    for key in ("name", "version", "cut"):
        if not isinstance(doc.get(key), str) or not doc.get(key):
            p.append(f"{key} is missing")

    style = doc.get("style", "low-and-slow")
    if style not in STYLES:
        p.append(f"style must be one of: {', '.join(STYLES)}")
        style = "low-and-slow"
    done_by = doc.get("done_by", STYLES[style]["done_by"])
    if done_by not in DONE_BY:
        p.append(f"done_by must be one of: {', '.join(DONE_BY)}")
        done_by = STYLES[style]["done_by"]
    probe = doc.get("probe", STYLES[style]["probe"])
    if probe not in PROBE:
        p.append(f"probe must be one of: {', '.join(PROBE)}")
        probe = STYLES[style]["probe"]
    if probe == "none" and done_by in ("feel", "temperature"):
        p.append(f"done_by {done_by} needs a probe: set probe to needed or optional, or done_by to time")

    pit = doc.get("pit") or {}
    meat = doc.get("meat") or {}
    timing = doc.get("timing") or {}
    stall_raw = doc.get("stall")
    stall = stall_raw if isinstance(stall_raw, dict) else {}
    if stall_raw is not None and not isinstance(stall_raw, dict) and stall_raw not in ("none", False):
        p.append("stall is either from_c and to_c, or none")
    wrap = doc.get("wrap")
    rest_raw = doc.get("rest")
    rest = rest_raw if isinstance(rest_raw, dict) else {}
    if rest_raw is not None and not isinstance(rest_raw, dict) and rest_raw not in ("none", False):
        p.append("rest is either minutes (min_minutes, ideal_minutes), or none")

    pit_t = _num(pit, "target_c", p, "pit")
    _num(pit, "tolerance_c", p, "pit", required=False)
    if pit_t is not None and not PIT_RANGE_C[0] <= pit_t <= PIT_RANGE_C[1]:
        p.append(f"pit.target_c {pit_t} is outside {PIT_RANGE_C[0]}-{PIT_RANGE_C[1]} C")

    floor = min_safe_c(doc.get("cut", ""))
    kind_of_food = str(doc.get("cut", "")).lower().split("-")[0]
    # a temperature target is needed whenever the meat has a safety floor, even if the clock ends the cook:
    # it is then the check to make with a thermometer before serving
    target_needed = done_by in ("feel", "temperature") or floor is not None
    meat_t = _num(meat, "target_c", p, "meat", required=target_needed)
    if isinstance(doc.get("cut"), str) and doc.get("cut") and floor is None and kind_of_food not in NO_FLOOR:
        p.append(f"cut '{doc['cut']}' must start with one of: {', '.join(sorted([*MIN_SAFE_C, *NO_FLOOR]))}")
    pull = _num(meat, "pull_c", p, "meat", required=False)
    if pull is not None and meat_t is not None and not meat_t - 10 <= pull <= meat_t:
        p.append("meat.pull_c must be at most 10 C below meat.target_c, and not above it")
    if meat_t is not None and floor is not None and meat_t < floor:
        p.append(f"meat.target_c {meat_t} is below the food-safety floor of {floor} C for {doc.get('cut')}")
    if meat_t is not None and pit_t is not None and meat_t >= pit_t:
        p.append("meat.target_c must be below pit.target_c")

    per_kg = _num(timing, "hours_per_kg", p, "timing", required=False)
    fixed = _num(timing, "hours_fixed", p, "timing", required=False)
    stages = doc.get("stages")
    if stages is not None:
        if not isinstance(stages, list) or not stages:
            p.append("stages must be a list of {name, hours}")
            stages = None
        else:
            for i, st in enumerate(stages):
                if not isinstance(st, dict) or not isinstance(st.get("name"), str) or not st.get("name"):
                    p.append(f"stages[{i}] needs a name")
                h = st.get("hours") if isinstance(st, dict) else None
                if isinstance(h, bool) or not isinstance(h, (int, float)) or not 0 < h <= 24:
                    p.append(f"stages[{i}].hours must be a number of hours")
    if sum(x is not None for x in (per_kg, fixed, stages)) != 1:
        p.append("timing needs exactly one of hours_per_kg, hours_fixed, or a list of stages")
    for v, name in ((per_kg, "hours_per_kg"), (fixed, "hours_fixed")):
        if v is not None and not 0 < v <= 48:
            p.append(f"timing.{name} {v} is not a sane number of hours")

    lo = _num(stall, "from_c", p, "stall", required=False)
    hi = _num(stall, "to_c", p, "stall", required=False)
    if (lo is None) != (hi is None):
        p.append("stall needs both from_c and to_c, or neither")
    if lo is not None and hi is not None and not lo < hi:
        p.append("stall.from_c must be below stall.to_c")

    if wrap is not None:
        if not isinstance(wrap, dict):
            p.append("wrap must be a mapping")
        else:
            at = _num(wrap, "at_c", p, "wrap")
            if at is not None and meat_t is not None and at >= meat_t:
                p.append("wrap.at_c must be below meat.target_c")

    rmin = _num(rest, "min_minutes", p, "rest", required=False)
    rideal = _num(rest, "ideal_minutes", p, "rest", required=False)
    if rmin is not None and rideal is not None and rideal < rmin:
        p.append("rest.ideal_minutes must be at least rest.min_minutes")
    hold = _num(rest, "hold_min_c", p, "rest", required=False)
    if hold is not None and not HOLD_FLOOR_C <= hold <= 90:
        p.append(f"rest.hold_min_c {hold} must be between {HOLD_FLOOR_C} and 90 C (food is not safe to hold below "
                 f"{HOLD_FLOOR_C} C)")

    finish = doc.get("finish")
    if finish is not None:
        if not isinstance(finish, dict) or finish.get("what") not in FINISHES:
            p.append(f"finish.what must be one of: {', '.join(FINISHES)}")
        else:
            fp = _num(finish, "pit_c", p, "finish", required=False)
            if fp is not None and not PIT_RANGE_C[0] <= fp <= FINISH_PIT_MAX_C:
                p.append(f"finish.pit_c {fp} is outside {PIT_RANGE_C[0]}-{FINISH_PIT_MAX_C} C")
            fm = _num(finish, "minutes", p, "finish")
            if fm is not None and not 0 < fm <= 120:
                p.append("finish.minutes must be between 1 and 120")
            at = _num(finish, "at_c", p, "finish", required=False)
            before = _num(finish, "minutes_before_end", p, "finish", required=False)
            if (at is None) == (before is None):
                p.append("finish needs exactly one of at_c (a meat temperature) or minutes_before_end")
            if at is not None and probe == "none":
                p.append("finish.at_c needs a probe; use minutes_before_end")

    rub = doc.get("rub")
    if rub is not None:
        if not isinstance(rub, str):
            p.append("rub must be a path to a rub file")
        elif base_dir is not None:
            rub_path = find_rub(rub, base_dir, rub_dirs)
            if rub_path is None:
                p.append(f"rub file not found: {rub}")
            else:
                p.extend(f"rub {rub}: {x}" for x in validate_rub(_read(rub_path)))
    return p


def validate_rub(doc: dict) -> list[str]:
    p: list[str] = []
    if not isinstance(doc, dict):
        return ["rub is not a mapping"]
    if doc.get("apiVersion") != API_VERSION:
        p.append(f"apiVersion must be {API_VERSION}")
    if doc.get("kind") != "Rub":
        p.append("kind must be Rub")
    if not isinstance(doc.get("name"), str) or not doc.get("name"):
        p.append("name is missing")
    if "version" in doc and not isinstance(doc["version"], (str, int, float)):
        p.append("version must be text such as 1.0.0")
    ing = doc.get("ingredients")
    if not isinstance(ing, list) or not ing:
        p.append("ingredients must be a non-empty list")
        return p
    total = 0.0
    for i, item in enumerate(ing):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            p.append(f"ingredients[{i}] needs a name")
            continue
        parts = item.get("parts")
        if isinstance(parts, bool) or not isinstance(parts, (int, float)) or parts <= 0:
            p.append(f"ingredients[{i}].parts must be a positive number")
        else:
            total += parts
    if total <= 0:
        p.append("ingredients have no parts")
    return p


def rub_fractions(doc: dict) -> dict[str, float]:
    """Share of salt and sugar in a rub (0-1), from ingredients tagged `kind: salt|sugar`."""
    ing = doc.get("ingredients") or []
    total = sum(float(i.get("parts", 0)) for i in ing) or 1.0
    out = {"salt": 0.0, "sugar": 0.0}
    for i in ing:
        if i.get("kind") in out:
            out[i["kind"]] += float(i.get("parts", 0)) / total
    return out


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def from_doc(doc: dict, path: str = "") -> Recipe:
    """Build a Recipe from an already validated document."""
    pit, meat, timing = doc["pit"], doc.get("meat") or {}, doc.get("timing") or {}
    style = doc.get("style", "low-and-slow")
    base = STYLES[style]
    stall_raw, rest_raw = doc.get("stall"), doc.get("rest")
    stall = stall_raw if isinstance(stall_raw, dict) else {}
    rest = rest_raw if isinstance(rest_raw, dict) else {}
    wrap = doc.get("wrap") or {}
    stall_enabled = bool(stall) if stall_raw is not None else base["stall"]
    no_rest = rest_raw in ("none", False)
    rest_min, rest_ideal = (0, 0) if no_rest else base["rest"]
    return Recipe(
        name=doc["name"], version=str(doc["version"]), cut=doc["cut"],
        pit_target_c=float(pit["target_c"]), pit_tolerance_c=float(pit.get("tolerance_c", 10)),
        meat_target_c=float(meat["target_c"]) if meat.get("target_c") is not None else None,
        style=style, done_by=doc.get("done_by", base["done_by"]), probe=doc.get("probe", base["probe"]),
        stall_enabled=stall_enabled,
        pull_c=float(meat["pull_c"]) if meat.get("pull_c") is not None else None,
        stages=[{"name": s["name"], "hours": float(s["hours"]), "note": s.get("note", "")}
                for s in (doc.get("stages") or [])],
        finish=dict(doc["finish"]) if doc.get("finish") else None,
        hours_per_kg=timing.get("hours_per_kg"), hours_fixed=timing.get("hours_fixed"),
        fridge_out_min=int(timing.get("fridge_out_min", 60)),
        dry_brine_hours=float(timing.get("dry_brine_hours", 0)),
        stall_from_c=float(stall.get("from_c", 65)), stall_to_c=float(stall.get("to_c", 77)),
        wrap_at_c=float(wrap["at_c"]) if wrap.get("at_c") is not None else None,
        wrap_material=wrap.get("material"),
        rest_min_minutes=int(rest.get("min_minutes", rest_min)),
        rest_ideal_minutes=int(rest.get("ideal_minutes", max(rest_ideal, int(rest.get("min_minutes", rest_min))))),
        hold_min_c=float(rest.get("hold_min_c", 60)),
        rub=doc.get("rub"), description=doc.get("description", ""), path=path, raw=doc,
    )


def load_recipe(path: str, rub_dirs: list[str] | None = None) -> Recipe:
    doc = _read(path)
    problems = validate_recipe(doc, os.path.dirname(os.path.abspath(path)), rub_dirs)
    if problems:
        raise RecipeError(f"{path}: " + "; ".join(problems))
    return from_doc(doc, os.path.abspath(path))


def validate_tree(root: str) -> dict[str, list[str]]:
    """Validate every recipe and rub under `root`. Returns {path: problems} for files with problems."""
    bad: dict[str, list[str]] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith((".yaml", ".yml")):
                continue
            path = os.path.join(dirpath, name)
            try:
                doc = _read(path)
            except yaml.YAMLError as e:
                bad[path] = [f"not valid YAML: {e}"]
                continue
            kind = doc.get("kind") if isinstance(doc, dict) else None
            if kind == "Recipe":
                problems = validate_recipe(doc, dirpath)
            elif kind == "Rub":
                problems = validate_rub(doc)
            else:
                problems = ["kind must be Recipe or Rub"]
            if problems:
                bad[path] = problems
    return bad
