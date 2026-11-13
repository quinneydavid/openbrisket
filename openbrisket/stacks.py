"""Stacks as code: what the fire was built from.

A stack is a YAML file that says what you cook on and how you run it: the smoker, the fuel and how it is laid
and lit, the smoking wood, the water pan, the vents. A cook is opened with a stack, the cook keeps a copy of it,
and results are listed per stack version, so "did the bigger chunks help" has an answer.

    apiVersion: openbrisket/v1
    kind: Stack
    name: kamado-lump-oak
    version: 1.0.0
    smoker:  {type: kamado, model: ...}
    fuel:    {type: lump-charcoal, brand: ..., amount_kg: 4, layout: ..., lit_with: ...}
    wood:    [{species: post oak, form: chunks, pieces: 4, when: buried through the charcoal}]
    water_pan: none
    airflow: {controller: fan, deflector: true, top_vent: ..., bottom_vent: ...}
    notes: ...
"""
from __future__ import annotations

API_VERSION = "openbrisket/v1"

SMOKERS = ("kamado", "kettle", "offset", "drum", "bullet", "cabinet", "pellet", "electric", "gas", "other")
FUELS = ("lump-charcoal", "briquettes", "wood-splits", "pellets", "gas", "electric")
WOOD_FORMS = ("chunks", "chips", "splits", "pellets", "dust")
CONTROLLERS = ("fan", "manual", "built-in")
SMOKER_TEXT = ("model",)
FUEL_TEXT = ("brand", "layout", "lit_with")
AIRFLOW_TEXT = ("top_vent", "bottom_vent")


def _is_num(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float))


def _texts(d: dict, keys: tuple, where: str, p: list[str]) -> None:
    for key in keys:
        if key in d and not isinstance(d[key], str):
            p.append(f"{where}.{key} must be text")


def validate_stack(doc: dict) -> list[str]:
    """Return every problem found in a parsed stack document."""
    p: list[str] = []
    if not isinstance(doc, dict):
        return ["stack is not a mapping"]
    if doc.get("apiVersion") != API_VERSION:
        p.append(f"apiVersion must be {API_VERSION}")
    if doc.get("kind") != "Stack":
        p.append("kind must be Stack")
    if not isinstance(doc.get("name"), str) or not doc.get("name"):
        p.append("name is missing")
    if "version" in doc and not isinstance(doc["version"], (str, int, float)):
        p.append("version must be text such as 1.0.0")

    smoker = doc.get("smoker")
    if not isinstance(smoker, dict) or smoker.get("type") not in SMOKERS:
        p.append(f"smoker.type must be one of: {', '.join(SMOKERS)}")
        smoker = smoker if isinstance(smoker, dict) else {}
    _texts(smoker, SMOKER_TEXT, "smoker", p)

    fuel = doc.get("fuel")
    if not isinstance(fuel, dict) or fuel.get("type") not in FUELS:
        p.append(f"fuel.type must be one of: {', '.join(FUELS)}")
        fuel = fuel if isinstance(fuel, dict) else {}
    _texts(fuel, FUEL_TEXT, "fuel", p)
    amount = fuel.get("amount_kg")
    if amount is not None and (not _is_num(amount) or not 0 < amount <= 50):
        p.append("fuel.amount_kg must be a number of kilograms, up to 50")
    if smoker.get("type") == "pellet" and fuel.get("type") in FUELS and fuel.get("type") != "pellets":
        p.append("a pellet smoker burns pellets: set fuel.type to pellets")

    wood = doc.get("wood")
    if wood is not None:
        if not isinstance(wood, list):
            p.append("wood must be a list of {species, form}")
        else:
            for i, w in enumerate(wood):
                if not isinstance(w, dict) or not isinstance(w.get("species"), str) or not w.get("species"):
                    p.append(f"wood[{i}] needs a species, e.g. post oak")
                    continue
                if w.get("form") not in WOOD_FORMS:
                    p.append(f"wood[{i}].form must be one of: {', '.join(WOOD_FORMS)}")
                for key, top in (("pieces", 50), ("grams", 5000)):
                    if w.get(key) is not None and (not _is_num(w[key]) or not 0 < w[key] <= top):
                        p.append(f"wood[{i}].{key} must be a number above 0, up to {top}")
                _texts(w, ("when",), f"wood[{i}]", p)

    if "water_pan" in doc and not isinstance(doc["water_pan"], (str, bool)):
        p.append("water_pan is none, or what is in it (water, apple juice, sand ...)")

    air = doc.get("airflow")
    if air is not None:
        if not isinstance(air, dict):
            p.append("airflow must be a mapping")
        else:
            if "controller" in air and air["controller"] not in CONTROLLERS:
                p.append(f"airflow.controller must be one of: {', '.join(CONTROLLERS)}")
            if "deflector" in air and not isinstance(air["deflector"], bool):
                p.append("airflow.deflector must be true or false")
            _texts(air, AIRFLOW_TEXT, "airflow", p)
    for key in ("description", "notes"):
        if key in doc and not isinstance(doc[key], str):
            p.append(f"{key} must be text")
    return p


def water_pan(doc: dict) -> str | None:
    """What is in the water pan, or None when there is none."""
    v = doc.get("water_pan")
    if v in (None, False, "", "none", "no"):
        return None
    return "water" if v is True else str(v)


def wood_text(w: dict) -> str:
    amount = f"{w['pieces']:g} " if w.get("pieces") is not None else ""
    form = str(w.get("form", ""))
    if w.get("pieces") == 1 and form in ("chunks", "splits"):
        form = form[:-1]                               # 1 chunk, not 1 chunks
    text = f"{amount}{w['species']} {form}".strip()
    if w.get("grams") is not None:
        text += f" ({w['grams']:g} g)"
    if w.get("when"):
        text += f", {w['when']}"
    return text


def fuel_text(doc: dict) -> str:
    fuel = doc.get("fuel") or {}
    text = (f"{fuel['amount_kg']:g} kg " if fuel.get("amount_kg") is not None else "") + \
        str(fuel.get("type", "")).replace("-", " ")
    if fuel.get("brand"):
        text += f" ({fuel['brand']})"
    if fuel.get("layout"):
        text += f", {fuel['layout']}"
    if fuel.get("lit_with"):
        text += f", lit with {fuel['lit_with']}"
    return text


def fire_line(doc: dict) -> str:
    """One line for the plan's 'light the fire' step: what to load and light."""
    fuel = fuel_text(doc)
    parts = [fuel[:1].upper() + fuel[1:]]
    wood = [wood_text(w) for w in doc.get("wood") or []]
    if wood:
        parts.append("Wood: " + "; ".join(wood))
    pan = water_pan(doc)
    if pan:
        parts.append(f"Water pan with {pan}")
    return ". ".join(x for x in parts if x)


def snapshot(doc: dict, stack_id: str) -> dict:
    """The copy of a stack a cook keeps, so the cook still knows it after the stack changes."""
    out = {"id": stack_id, "name": doc.get("name"), "version": str(doc.get("version", "1.0.0"))}
    for key in ("smoker", "fuel", "wood", "water_pan", "airflow", "notes"):
        if key in doc:
            out[key] = doc[key]
    return out
