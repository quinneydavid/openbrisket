"""Your own recipes and rubs: YAML files in a git repository, written from the page, the API or an agent.

    <data folder>/recipes/            your recipes, one file each
    <data folder>/recipes/rubs/       your rubs

Every save is a commit, so a recipe has a history and any version can be brought back with git.
The recipes shipped with the app are read-only; saving one under the same name makes your own copy,
which then takes its place.
"""
from __future__ import annotations

import os
import re
import subprocess

import yaml

from . import recipes

GIT_ID = ["-c", "user.name=OpenBrisket", "-c", "user.email=openbrisket@localhost"]


class LibraryError(Exception):
    pass


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _git(root: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", root, *GIT_ID, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise LibraryError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def ensure(root: str) -> None:
    os.makedirs(os.path.join(root, "rubs"), mode=0o700, exist_ok=True)
    if not os.path.isdir(os.path.join(root, ".git")):
        subprocess.run(["git", "init", "-q", "-b", "main", root], check=True, capture_output=True)
        with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
            f.write("# My recipes\n\nRecipes and rubs written with OpenBrisket. One file each, one commit per save.\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init recipe library")


def _commit(root: str, message: str) -> None:
    _git(root, "add", "-A")
    if _git(root, "status", "--porcelain").strip():
        _git(root, "commit", "-q", "-m", message)


def _num(value, name: str, required: bool = True):
    if value in (None, ""):
        if required:
            raise LibraryError(f"{name} is needed")
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise LibraryError(f"{name} must be a number") from None
    return int(v) if v == int(v) else v


def bump(version: str) -> str:
    parts = str(version).split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        return f"{version}.1"
    return ".".join(parts)


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _existing(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def rub_doc(form: dict) -> dict:
    """Turn a rub form into a rub document (not yet validated)."""
    name = slug(form.get("name") or "")
    if not name:
        raise LibraryError("the rub needs a name")
    ingredients = []
    for i, item in enumerate(form.get("ingredients") or []):
        if not isinstance(item, dict):
            raise LibraryError(f"ingredient {i + 1} is not readable")
        label = str(item.get("name") or "").strip()
        if not label and item.get("parts") in (None, ""):
            continue                      # an empty row in the form
        if not label:
            raise LibraryError(f"ingredient {i + 1} needs a name")
        entry = {"name": label, "parts": _num(item.get("parts"), f"parts of {label}")}
        if item.get("kind") in ("salt", "sugar"):
            entry["kind"] = item["kind"]
        ingredients.append(entry)
    doc = {"apiVersion": recipes.API_VERSION, "kind": "Rub", "name": name}
    if str(form.get("description") or "").strip():
        doc["description"] = str(form["description"]).strip()
    doc["ingredients"] = ingredients
    return doc


def recipe_doc(form: dict) -> dict:
    """Turn a recipe form into a recipe document (not yet validated)."""
    name = slug(form.get("name") or "")
    if not name:
        raise LibraryError("the recipe needs a name")
    doc: dict = {"apiVersion": recipes.API_VERSION, "kind": "Recipe", "name": name,
                 "version": str(form.get("version") or "1.0.0").strip(), "cut": slug(form.get("cut") or "")}
    if str(form.get("description") or "").strip():
        doc["description"] = str(form["description"]).strip()
    if form.get("rub"):
        doc["rub"] = f"rubs/{slug(form['rub'])}.yaml"
    doc["pit"] = {"target_c": _num(form.get("pit_target_c"), "pit temperature"),
                  "tolerance_c": _num(form.get("pit_tolerance_c"), "pit tolerance", False) or 10}
    doc["meat"] = {"target_c": _num(form.get("meat_target_c"), "meat target")}
    timing: dict = {}
    per_kg, fixed = _num(form.get("hours_per_kg"), "hours per kg", False), _num(form.get("hours_fixed"), "hours", False)
    if per_kg is not None:
        timing["hours_per_kg"] = per_kg
    if fixed is not None:
        timing["hours_fixed"] = fixed
    timing["fridge_out_min"] = _num(form.get("fridge_out_min"), "minutes out of the fridge", False) or 60
    brine = _num(form.get("dry_brine_hours"), "dry brine hours", False)
    if brine:
        timing["dry_brine_hours"] = brine
    doc["timing"] = timing
    doc["stall"] = {"from_c": _num(form.get("stall_from_c"), "stall start", False) or 65,
                    "to_c": _num(form.get("stall_to_c"), "stall end", False) or 77}
    wrap_at = _num(form.get("wrap_at_c"), "wrap temperature", False)
    if wrap_at is not None:
        doc["wrap"] = {"at_c": wrap_at}
        if str(form.get("wrap_material") or "").strip():
            doc["wrap"]["material"] = str(form["wrap_material"]).strip()
    doc["rest"] = {"min_minutes": _num(form.get("rest_min_minutes"), "minimum rest", False) or 30,
                   "ideal_minutes": _num(form.get("rest_ideal_minutes"), "ideal rest", False) or 60}
    hold = _num(form.get("hold_min_c"), "hold floor", False)
    if hold is not None and hold != 60:
        doc["rest"]["hold_min_c"] = hold
    return doc


def save_rub(root: str, form: dict) -> dict:
    doc = rub_doc(form)
    problems = recipes.validate_rub(doc)
    if problems:
        raise LibraryError("; ".join(problems))
    ensure(root)
    path = os.path.join(root, "rubs", doc["name"] + ".yaml")
    new = not os.path.exists(path)
    if _existing(path) == doc:
        return {"id": doc["name"], "saved": False, "message": "no change"}
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dump(doc))
    _commit(root, f"rub {doc['name']}: {'add' if new else 'update'}")
    return {"id": doc["name"], "saved": True, "message": "added" if new else "updated"}


def save_recipe(root: str, form: dict, rub_dirs: list[str]) -> dict:
    doc = recipe_doc(form)
    ensure(root)
    path = os.path.join(root, doc["name"] + ".yaml")
    old = _existing(path)
    if old is not None:
        same_body = {k: v for k, v in old.items() if k != "version"} == {k: v for k, v in doc.items() if k != "version"}
        if same_body and str(old.get("version")) == doc["version"]:
            return {"id": doc["name"], "version": doc["version"], "saved": False, "message": "no change"}
        if not same_body and str(old.get("version")) == doc["version"]:
            doc["version"] = bump(doc["version"])     # a changed recipe never keeps its old version number
    problems = recipes.validate_recipe(doc, root, rub_dirs)
    if problems:
        raise LibraryError("; ".join(problems))
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dump(doc))
    summary = str(form.get("change") or "").strip()
    _commit(root, f"recipe {doc['name']} {doc['version']}: " + (summary or ("add" if old is None else "update")))
    return {"id": doc["name"], "version": doc["version"], "saved": True,
            "message": "added" if old is None else f"updated to {doc['version']}"}


def delete(root: str, kind: str, item_id: str) -> None:
    name = slug(item_id)
    path = os.path.join(root, "rubs" if kind == "rub" else "", name + ".yaml")
    if not name or not os.path.isfile(path):
        raise LibraryError(f"there is no {kind} of yours called '{item_id}'")
    if kind == "rub":
        for f in os.listdir(root):
            if f.endswith((".yaml", ".yml")):
                d = _existing(os.path.join(root, f)) or {}
                if d.get("rub") == f"rubs/{name}.yaml":
                    raise LibraryError(f"recipe {d.get('name')} uses this rub; change or delete that recipe first")
    os.remove(path)
    _commit(root, f"{kind} {name}: delete")


def history(root: str, kind: str, item_id: str, limit: int = 20) -> list[str]:
    rel = os.path.join("rubs" if kind == "rub" else "", slug(item_id) + ".yaml")
    if not os.path.isdir(os.path.join(root, ".git")):
        return []
    return _git(root, "log", f"-{limit}", "--format=%ad %s", "--date=format:%d %b %Y %H:%M", "--", rel).splitlines()
