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


class RecipeError(Exception):
    pass


@dataclass
class Recipe:
    name: str
    version: str
    cut: str
    pit_target_c: float
    pit_tolerance_c: float
    meat_target_c: float
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
    rub: str | None = None
    description: str = ""
    path: str = ""
    raw: dict = field(default_factory=dict)

    def cook_hours(self, weight_kg: float | None) -> float:
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


def validate_recipe(doc: dict, base_dir: str | None = None) -> list[str]:
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

    pit = doc.get("pit") or {}
    meat = doc.get("meat") or {}
    timing = doc.get("timing") or {}
    stall = doc.get("stall") or {}
    wrap = doc.get("wrap")
    rest = doc.get("rest") or {}

    pit_t = _num(pit, "target_c", p, "pit")
    _num(pit, "tolerance_c", p, "pit", required=False)
    if pit_t is not None and not PIT_RANGE_C[0] <= pit_t <= PIT_RANGE_C[1]:
        p.append(f"pit.target_c {pit_t} is outside {PIT_RANGE_C[0]}-{PIT_RANGE_C[1]} C")

    meat_t = _num(meat, "target_c", p, "meat")
    floor = min_safe_c(doc.get("cut", ""))
    if isinstance(doc.get("cut"), str) and doc.get("cut") and floor is None:
        p.append(f"cut '{doc['cut']}' must start with one of: {', '.join(sorted(MIN_SAFE_C))}")
    if meat_t is not None and floor is not None and meat_t < floor:
        p.append(f"meat.target_c {meat_t} is below the food-safety floor of {floor} C for {doc.get('cut')}")
    if meat_t is not None and pit_t is not None and meat_t >= pit_t:
        p.append("meat.target_c must be below pit.target_c")

    per_kg = _num(timing, "hours_per_kg", p, "timing", required=False)
    fixed = _num(timing, "hours_fixed", p, "timing", required=False)
    if (per_kg is None) == (fixed is None):
        p.append("timing needs exactly one of hours_per_kg or hours_fixed")
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

    rub = doc.get("rub")
    if rub is not None:
        if not isinstance(rub, str):
            p.append("rub must be a path to a rub file")
        elif base_dir is not None:
            rub_path = os.path.normpath(os.path.join(base_dir, rub))
            if not os.path.isfile(rub_path):
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
    pit, meat, timing = doc["pit"], doc["meat"], doc["timing"]
    stall, wrap, rest = doc.get("stall") or {}, doc.get("wrap") or {}, doc.get("rest") or {}
    return Recipe(
        name=doc["name"], version=str(doc["version"]), cut=doc["cut"],
        pit_target_c=float(pit["target_c"]), pit_tolerance_c=float(pit.get("tolerance_c", 10)),
        meat_target_c=float(meat["target_c"]),
        hours_per_kg=timing.get("hours_per_kg"), hours_fixed=timing.get("hours_fixed"),
        fridge_out_min=int(timing.get("fridge_out_min", 60)),
        dry_brine_hours=float(timing.get("dry_brine_hours", 0)),
        stall_from_c=float(stall.get("from_c", 65)), stall_to_c=float(stall.get("to_c", 77)),
        wrap_at_c=float(wrap["at_c"]) if wrap.get("at_c") is not None else None,
        wrap_material=wrap.get("material"),
        rest_min_minutes=int(rest.get("min_minutes", 30)), rest_ideal_minutes=int(rest.get("ideal_minutes", 60)),
        rub=doc.get("rub"), description=doc.get("description", ""), path=path, raw=doc,
    )


def load_recipe(path: str) -> Recipe:
    doc = _read(path)
    problems = validate_recipe(doc, os.path.dirname(os.path.abspath(path)))
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
