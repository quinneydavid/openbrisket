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
    doc = {"apiVersion": recipes.API_VERSION, "kind": "Rub", "name": name,
           "version": str(form.get("version") or "1.0.0").strip()}
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
                 "version": str(form.get("version") or "").strip(), "cut": slug(form.get("cut") or "")}
    if str(form.get("description") or "").strip():
        doc["description"] = str(form["description"]).strip()
    if form.get("rub"):
        doc["rub"] = f"rubs/{slug(form['rub'])}.yaml"
    style = str(form.get("style") or "low-and-slow")
    base = recipes.STYLES.get(style, recipes.STYLES["low-and-slow"])
    if style != "low-and-slow":
        doc["style"] = style
    for key in ("done_by", "probe"):          # written only when they differ from what the style already says
        if form.get(key) and form[key] != base[key]:
            doc[key] = str(form[key])
    doc["pit"] = {"target_c": _num(form.get("pit_target_c"), "pit temperature"),
                  "tolerance_c": _num(form.get("pit_tolerance_c"), "pit tolerance", False) or 10}
    target = _num(form.get("meat_target_c"), "meat target", False)
    if target is not None:
        doc["meat"] = {"target_c": target}
        pull = _num(form.get("pull_c"), "pull temperature", False)
        if pull is not None:
            doc["meat"]["pull_c"] = pull
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
    stages = []
    for i, st in enumerate(form.get("stages") or []):
        if not isinstance(st, dict):
            raise LibraryError(f"stage {i + 1} is not readable")
        label = str(st.get("name") or "").strip()
        if not label and st.get("hours") in (None, ""):
            continue
        entry = {"name": label, "hours": _num(st.get("hours"), f"hours of stage {label or i + 1}")}
        if str(st.get("note") or "").strip():
            entry["note"] = str(st["note"]).strip()
        stages.append(entry)
    if stages:
        doc["stages"] = stages
    wants_stall = form.get("stall") if form.get("stall") is not None else base["stall"]
    if wants_stall in (True, "true", "yes", "on", 1):
        doc["stall"] = {"from_c": _num(form.get("stall_from_c"), "stall start", False) or 65,
                        "to_c": _num(form.get("stall_to_c"), "stall end", False) or 77}
    elif base["stall"]:
        doc["stall"] = "none"
    fin = form.get("finish") or {}
    if isinstance(fin, dict) and fin.get("what"):
        f = {"what": str(fin["what"]), "minutes": _num(fin.get("minutes"), "finish minutes")}
        for key, label in (("pit_c", "finish pit temperature"), ("at_c", "finish meat temperature"),
                           ("minutes_before_end", "finish minutes before the end")):
            v = _num(fin.get(key), label, False)
            if v is not None:
                f[key] = v
        doc["finish"] = f
    wrap_at = _num(form.get("wrap_at_c"), "wrap temperature", False)
    if wrap_at is not None:
        doc["wrap"] = {"at_c": wrap_at}
        if str(form.get("wrap_material") or "").strip():
            doc["wrap"]["material"] = str(form["wrap_material"]).strip()
    rmin = _num(form.get("rest_min_minutes"), "minimum rest", False)
    rideal = _num(form.get("rest_ideal_minutes"), "ideal rest", False)
    rmin = base["rest"][0] if rmin is None else rmin
    rideal = max(base["rest"][1], rmin) if rideal is None else rideal
    if rmin == 0 and rideal == 0:
        doc["rest"] = "none"
    else:
        doc["rest"] = {"min_minutes": rmin, "ideal_minutes": rideal}
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
    old = _existing(path)
    if old is not None:
        if "version" not in form or not form.get("version"):
            doc["version"] = str(old.get("version", "1.0.0"))
        body = lambda d: {k: v for k, v in d.items() if k != "version"}  # noqa: E731
        if body(old) == body(doc) and str(old.get("version", "1.0.0")) == doc["version"]:
            return {"id": doc["name"], "version": doc["version"], "saved": False, "message": "no change"}
        if body(old) != body(doc) and str(old.get("version", "1.0.0")) == doc["version"]:
            doc["version"] = bump(doc["version"])     # a changed rub never keeps its old version number
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dump(doc))
    summary = str(form.get("change") or "").strip()
    _commit(root, f"rub {doc['name']} {doc['version']}: " + (summary or ("add" if old is None else "update")))
    return {"id": doc["name"], "version": doc["version"], "saved": True,
            "message": "added" if old is None else f"updated to {doc['version']}"}


def save_recipe(root: str, form: dict, rub_dirs: list[str]) -> dict:
    doc = recipe_doc(form)
    ensure(root)
    path = os.path.join(root, doc["name"] + ".yaml")
    old = _existing(path)
    if not doc["version"]:               # no version given: carry on from the saved one, never restart at 1.0.0
        doc["version"] = str(old.get("version", "1.0.0")) if old is not None else "1.0.0"
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


def save_yaml(root: str, kind: str, text: str, rub_dirs: list[str], change: str = "") -> dict:
    """Save a recipe or rub from YAML text. The text is written as typed, so comments survive; only the version
    line is touched, and only when the content changed without the version being moved on."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise LibraryError(f"not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise LibraryError("the YAML must be a mapping with apiVersion, kind, name ...")
    want = "Rub" if kind == "rub" else "Recipe"
    if doc.get("kind") != want:
        raise LibraryError(f"kind must be {want}")
    name = slug(doc.get("name") or "")
    if not name or name != doc.get("name"):
        raise LibraryError("name must be lower-case words joined by dashes, e.g. " + (name or "beef-short-ribs"))
    ensure(root)
    path = os.path.join(root, "rubs" if kind == "rub" else "", name + ".yaml")
    old = _existing(path)
    body = lambda d: {k: v for k, v in d.items() if k != "version"}  # noqa: E731
    version = str(doc.get("version", "1.0.0"))
    if old is not None:
        old_version = str(old.get("version", "1.0.0"))
        if body(old) == body(doc) and old_version == version:
            return {"id": name, "version": version, "saved": False, "message": "no change"}
        if body(old) != body(doc) and old_version == version:
            new_version = bump(version)             # a changed file never keeps its old version number
            pattern = re.compile(r"^(version:\s*)(['\"]?)" + re.escape(version) + r"\2\s*$", re.M)
            if pattern.search(text):
                text = pattern.sub(lambda m: f"{m.group(1)}{new_version}", text, count=1)
            else:                                   # no version line to move on: add one after the name
                text = re.sub(r"^(name:.*)$", lambda m: f"{m.group(1)}\nversion: {new_version}", text, count=1, flags=re.M)
            doc["version"], version = new_version, new_version
    problems = recipes.validate_rub(doc) if kind == "rub" else recipes.validate_recipe(doc, root, rub_dirs)
    if problems:
        raise LibraryError("; ".join(problems))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    _commit(root, f"{kind} {name} {version}: " + (change.strip() or ("add" if old is None else "update")))
    return {"id": name, "version": version, "saved": True, "message": "added" if old is None else f"updated to {version}"}


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


LABELS = {
    "version": "Version", "cut": "Cut", "description": "Description", "rub": "Rub", "style": "Style",
    "done_by": "Done by", "probe": "Probe", "stall": "Stall", "rest": "Rest", "meat.pull_c": "Pull temperature",
    "finish.what": "Finish", "finish.pit_c": "Finish pit temperature", "finish.minutes": "Finish minutes",
    "finish.at_c": "Finish when the meat is at", "finish.minutes_before_end": "Finish, minutes before the end",
    "pit.target_c": "Pit temperature", "pit.tolerance_c": "Pit tolerance", "meat.target_c": "Meat target",
    "timing.hours_per_kg": "Hours per kg", "timing.hours_fixed": "Hours, whatever the weight",
    "timing.fridge_out_min": "Out of the fridge, minutes before", "timing.dry_brine_hours": "Dry brine, hours before",
    "stall.from_c": "Stall from", "stall.to_c": "Stall to", "wrap.at_c": "Wrap at", "wrap.material": "Wrap in",
    "rest.min_minutes": "Rest, minimum minutes", "rest.ideal_minutes": "Rest, ideal minutes",
    "rest.hold_min_c": "Hold floor",
}


def _flat(doc: dict | None) -> dict[str, str]:
    """A recipe or rub as flat field -> text, so two versions can be compared line by line."""
    out: dict[str, str] = {}
    if not isinstance(doc, dict):
        return out
    for key, value in doc.items():
        if key in ("apiVersion", "kind", "name"):
            continue
        if key == "stages" and isinstance(value, list):
            for n, item in enumerate(value, 1):
                if isinstance(item, dict):
                    out[f"ingredient:stage {n}"] = f"{item.get('name')}, {item.get('hours'):g} h" + (
                        f" ({item['note']})" if item.get("note") else "")
        elif key == "ingredients" and isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("name"):
                    parts = item.get("parts")
                    text = (f"{parts:g} part" + ("" if parts == 1 else "s")) if isinstance(parts, (int, float)) else str(parts)
                    out[f"ingredient:{item['name']}"] = text + (f" ({item['kind']})" if item.get("kind") else "")
        elif isinstance(value, dict):
            for k2, v2 in value.items():
                out[f"{key}.{k2}"] = f"{v2:g}" if isinstance(v2, float) else str(v2)
        else:
            out[key] = str(value)
    return out


def _changes(before: dict | None, after: dict | None) -> list[dict]:
    a, b = _flat(before), _flat(after)
    out = []
    for key in list(dict.fromkeys([*a, *b])):
        if a.get(key) == b.get(key) or key == "version":
            continue
        label = key[len("ingredient:"):] if key.startswith("ingredient:") else LABELS.get(key, key)
        out.append({"field": label, "before": a.get(key), "after": b.get(key)})
    return out


def history(root: str, kind: str, item_id: str, limit: int = 50) -> list[dict]:
    """Every saved version of a recipe or rub, newest first, each with what changed from the one before."""
    rel = os.path.join("rubs" if kind == "rub" else "", slug(item_id) + ".yaml")
    if not os.path.isdir(os.path.join(root, ".git")):
        return []
    lines = _git(root, "log", f"-{limit}", "--format=%H|%aI|%s", "--", rel).splitlines()
    entries, previous = [], None
    for line in reversed(lines):                      # oldest first, to compare each with the one before
        sha, at, message = line.split("|", 2)
        try:
            doc = yaml.safe_load(_git(root, "show", f"{sha}:{rel}"))
        except (LibraryError, yaml.YAMLError):
            doc = None                                # the commit that deleted the file
        entries.append({"at": at, "message": message, "version": str(doc.get("version", "")) if doc else None,
                        "deleted": doc is None, "created": previous is None and doc is not None,
                        "changes": _changes(previous, doc) if previous is not None and doc is not None else []})
        previous = doc
    return list(reversed(entries))
