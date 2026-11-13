"""One place that does things. The web page, the JSON API, the MCP server and the background runner
all call this, so they cannot disagree about how a cook works."""
from __future__ import annotations

import csv
import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timezone

import yaml

from . import checks as checkmod, library, notify, plan as planmod, recipes, settings, stacks, smoketest, sources
from .cook import Engine
from .store import MAX_PROBES, Store

STEPS = ("meat-on", "wrap", "spritz", "hotfix", "wood", "rest", "note")      # hotfix = fuel added mid-cook
KINDS = ("recipe", "rub", "stack")

# What a token may do. Each level includes the ones before it.
#   read   look: status, recipes, rubs, history, cooks, plans
#   write  run a cook and change recipes: open, log steps, probes, photo checks, smoke test, close, save
#   admin  everything: settings (which hold secrets), deleting, and tokens themselves
SCOPES = ("read", "write", "admin")


class ServiceError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def when(text: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(text).strip().replace(" ", "T", 1))
    except ValueError:
        raise ServiceError(f"cannot read the time '{text}'; use e.g. 2026-11-14 18:00") from None
    return dt if dt.tzinfo else dt.astimezone()


def _reading_dict(r: sources.Reading | None) -> dict | None:
    if r is None:
        return None
    return {"at": r.at.isoformat(timespec="seconds"), "connected": r.connected, "pit_c": r.pit,
            "probes_c": r.probes, "fan_pct": r.fan, "fan_on": r.fan_on, "pit_target_c": r.pit_target,
            "ambient_c": r.ambient, "humidity_pct": r.humidity, "rain": r.rain}


def burn_rate(fuel_kg, hours) -> float | None:
    """Fuel burnt per hour on the pit, kg/h. None when either is unknown or the cook was too short to say."""
    if not fuel_kg or not hours or hours < 0.25:
        return None
    return round(float(fuel_kg) / float(hours), 2)


class Service:
    def __init__(self, recipe_dirs: list[str] | None = None):
        self.lock = threading.RLock()
        self._engine: Engine | None = None
        self.last_reading: sources.Reading | None = None
        self.last_error: str | None = None
        self.recipe_dirs = recipe_dirs or self._default_recipe_dirs()
        self._stop = threading.Event()

    # --- settings ---------------------------------------------------------------------------------
    @staticmethod
    def _default_recipe_dirs() -> list[str]:
        shipped = os.environ.get("OPENBRISKET_RECIPES") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recipes")
        return [shipped, os.path.join(settings.home(), "recipes")]

    def cfg(self) -> dict:
        return settings.load()

    def store(self) -> Store:
        return Store(settings.cooks_path(self.cfg()))

    def access_token(self) -> str:
        """The token agents and the web page present. Created on first use."""
        with self.lock:
            cfg = self.cfg()
            token = (cfg.get("access") or {}).get("token")
            if not token:
                token = secrets.token_urlsafe(24)
                settings.update({"access": {"token": token}})
            return token

    # --- tokens with limited rights ---------------------------------------------------------------
    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def scope_of(self, token: str) -> str | None:
        """The rights a presented token carries: admin for the main access token, the minted scope for a minted
        token, None for anything else."""
        if not token:
            return None
        if hmac.compare_digest(token.encode(), self.access_token().encode()):
            return "admin"
        digest = self._hash(token)
        for t in (self.cfg().get("access") or {}).get("tokens") or []:
            if hmac.compare_digest(str(t.get("hash", "")), digest):
                return t.get("scope") if t.get("scope") in SCOPES else None
        return None

    def list_tokens(self) -> list[dict]:
        """Minted tokens, without their values (only a hash is kept, so a value can never be shown again)."""
        return [{"id": t["id"], "name": t.get("name", ""), "scope": t.get("scope"), "created_at": t.get("created_at")}
                for t in (self.cfg().get("access") or {}).get("tokens") or []]

    def mint_token(self, name: str, scope: str) -> dict:
        name = str(name or "").strip()[:60]
        if not name:
            raise ServiceError("give the token a name, so you know later what it was for")
        if scope not in SCOPES:
            raise ServiceError(f"scope is one of: {', '.join(SCOPES)}")
        with self.lock:
            tokens = list((self.cfg().get("access") or {}).get("tokens") or [])
            if any(t.get("name") == name for t in tokens):
                raise ServiceError(f"there is already a token called '{name}'; revoke it first or pick another name")
            value = "ob_" + secrets.token_urlsafe(30)
            entry = {"id": secrets.token_hex(4), "name": name, "scope": scope, "hash": self._hash(value),
                     "created_at": _now().isoformat(timespec="seconds")}
            settings.update({"access": {"tokens": tokens + [entry]}})
            return {"id": entry["id"], "name": name, "scope": scope, "token": value}

    def revoke_token(self, token_id: str) -> dict:
        with self.lock:
            tokens = list((self.cfg().get("access") or {}).get("tokens") or [])
            keep = [t for t in tokens if t.get("id") != token_id]
            if len(keep) == len(tokens):
                raise ServiceError("no token with that id")
            settings.update({"access": {"tokens": keep}})
            return {"revoked": token_id}

    def settings_view(self) -> dict:
        """Settings for the web page. Secrets are never sent back, only whether they are set."""
        cfg = self.cfg()
        src, tg = cfg.get("source") or {}, cfg.get("telegram") or {}
        return {
            "source": {"type": src.get("type"), "url": src.get("url"), "token_set": bool(src.get("token")),
                       "entities": src.get("entities") or {}},
            "telegram": {"bot_token_set": bool(tg.get("bot_token")), "chat_id": tg.get("chat_id") or ""},
            "poll_seconds": cfg.get("poll_seconds", 15),
            "cooks_path": settings.cooks_path(cfg),
        }

    def save_settings(self, body: dict) -> dict:
        """Apply a settings form. Empty secret fields mean 'keep what is there'."""
        patch: dict = {}
        src = body.get("source") or {}
        if src:
            s: dict = {}
            for key in ("type", "url"):
                if src.get(key):
                    s[key] = str(src[key]).strip()
            if src.get("token"):
                s["token"] = str(src["token"]).strip()
            if isinstance(src.get("entities"), dict):
                ent = {k: v for k, v in src["entities"].items() if k != "probes" and isinstance(v, str)}  # incl. ambient
                ent = {k: v.strip() for k, v in ent.items()}
                probes = src["entities"].get("probes")
                if isinstance(probes, list):
                    names = src["entities"].get("probe_names")
                    names = names if isinstance(names, list) else []
                    kept = [(str(p).strip(), str(names[i]).strip() if i < len(names) and names[i] else "")
                            for i, p in enumerate(probes) if str(p).strip()][:MAX_PROBES]
                    ent["probes"] = [k[0] for k in kept]
                    ent["probe_names"] = [k[1] for k in kept]
                s["entities"] = ent
            patch["source"] = s
        tg = body.get("telegram") or {}
        if tg:
            t: dict = {}
            if tg.get("bot_token"):
                t["bot_token"] = str(tg["bot_token"]).strip()
            if "chat_id" in tg:
                t["chat_id"] = str(tg["chat_id"]).strip()
            patch["telegram"] = t
        if body.get("poll_seconds") is not None:
            try:
                patch["poll_seconds"] = min(300, max(3, int(body["poll_seconds"])))
            except (TypeError, ValueError):
                raise ServiceError("poll_seconds must be a number") from None
        with self.lock:
            settings.update(patch)
            self._engine = None          # pick up a new notifier
        return self.settings_view()

    def probe_sources(self) -> list[dict]:
        """Which physical sensor each probe number is: the entity behind it and the name given to it in Settings."""
        ent = (self.cfg().get("source") or {}).get("entities") or {}
        names = ent.get("probe_names") or []
        out = []
        for i, entity in enumerate(ent.get("probes") or []):
            name = names[i] if i < len(names) and names[i] else ""
            if not name:                           # no name given: make the entity id readable
                name = str(entity).split(".", 1)[-1].replace("_", " ")
            out.append({"no": i + 1, "entity": entity, "name": name})
        return out

    def test_source(self) -> dict:
        try:
            r = sources.from_settings(self.cfg().get("source")).read()
        except sources.SourceError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": r.pit is not None, "reading": _reading_dict(r),
                "error": None if r.pit is not None else "connected, but there is no pit reading"}

    def test_notify(self) -> dict:
        n = notify.from_settings(self.cfg())
        if isinstance(n, notify.ConsoleNotifier):
            return {"ok": False, "error": "no Telegram bot is set; notes are only printed to the log"}
        ok = n.send("OpenBrisket: test note. Notes from your cooks will arrive here.")
        return {"ok": ok, "error": None if ok else "Telegram refused the message; check the bot token and chat id"}

    # --- recipes and rubs -------------------------------------------------------------------------
    def library_root(self) -> str:
        """The folder your own recipes are written to: the last of the recipe folders."""
        return self.recipe_dirs[-1]

    def _is_own(self, path: str) -> bool:
        return os.path.abspath(path).startswith(os.path.abspath(self.library_root()) + os.sep)

    def _recipe_files(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for d in self.recipe_dirs:                 # later folders win: your copy replaces a shipped recipe
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.endswith((".yaml", ".yml")):
                    found[os.path.splitext(name)[0]] = os.path.join(d, name)
        return found

    def _rub_files(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for d in self.recipe_dirs:
            rubs = os.path.join(d, "rubs")
            if not os.path.isdir(rubs):
                continue
            for name in sorted(os.listdir(rubs)):
                if name.endswith((".yaml", ".yml")):
                    found[os.path.splitext(name)[0]] = os.path.join(rubs, name)
        return found

    def _rub_view(self, path: str) -> dict | None:
        try:
            with open(path, encoding="utf-8") as f:
                source = f.read()
            doc = yaml.safe_load(source)
            if recipes.validate_rub(doc):
                return None
        except (OSError, yaml.YAMLError):
            return None
        return {"name": doc.get("name"), "version": str(doc.get("version", "1.0.0")),
                "description": doc.get("description", ""),
                "ingredients": doc.get("ingredients"), **recipes.rub_fractions(doc), "source": source, "doc": doc}

    def list_rubs(self) -> list[dict]:
        used: dict[str, list[str]] = {}
        for r in self.list_recipes():
            if r.get("valid") and r.get("rub"):
                used.setdefault(r["rub"]["id"], []).append(r["id"])
        out = []
        for rid, path in sorted(self._rub_files().items()):
            v = self._rub_view(path)
            if v is None:
                out.append({"id": rid, "valid": False, "error": "not a valid rub file", "own": self._is_own(path)})
                continue
            v.pop("doc")
            out.append({"id": rid, "valid": True, "own": self._is_own(path), "used_by": used.get(rid, []), **v})
        return out

    def results(self) -> dict[str, list[dict]]:
        """Cooks grouped by recipe and then by recipe version: what each version of a recipe actually produced."""
        store = self.store()
        by_recipe: dict[str, dict[str, dict]] = {}
        for cook_id in store.cooks():
            m = store.meta(cook_id)
            if m.get("status") != "done":
                continue
            res, an = m.get("result") or {}, m.get("analysis") or {}
            v = by_recipe.setdefault(m.get("recipe_name") or "?", {}).setdefault(
                str(m.get("recipe_version") or "?"), {"version": str(m.get("recipe_version") or "?"), "cooks": []})
            v["cooks"].append({
                "id": cook_id, "opened_at": m.get("opened_at"), "weight_kg": m.get("weight_kg"),
                "score": res.get("score"), "yield": res.get("yield"), "notes": res.get("notes"),
                "hours_to_target": an.get("hours_to_target"), "hours_on_pit": an.get("hours_on_pit"),
                "k_ratio": (an.get("model") or {}).get("k_ratio"),
                "rub": (m.get("rub") or {}).get("name"), "rub_version": (m.get("rub") or {}).get("version"),
                "stack": (m.get("stack") or {}).get("name"), "stack_version": (m.get("stack") or {}).get("version")})
        out: dict[str, list[dict]] = {}
        for name, versions in by_recipe.items():
            rows = []
            for v in versions.values():
                scores = [c["score"] for c in v["cooks"] if c["score"] is not None]
                v["count"] = len(v["cooks"])
                v["avg_score"] = round(sum(scores) / len(scores), 1) if scores else None
                v["best_score"] = max(scores) if scores else None
                v["first_cook"] = min(c["opened_at"] or "" for c in v["cooks"])
                rows.append(v)
            out[name] = sorted(rows, key=lambda v: v["first_cook"])
        return out

    def list_recipes(self) -> list[dict]:
        out = []
        results = self.results()
        rub_files = self._rub_files()
        for rid, path in sorted(self._recipe_files().items()):
            own = self._is_own(path)
            try:
                r = recipes.load_recipe(path, self.recipe_dirs)
            except (recipes.RecipeError, yaml.YAMLError, OSError) as e:
                out.append({"id": rid, "valid": False, "error": str(e), "own": own})
                continue
            with open(path, encoding="utf-8") as f:
                source = f.read()
            rub, rub_doc = None, None
            if r.rub:
                rub_id = os.path.splitext(os.path.basename(r.rub))[0]
                found = recipes.find_rub(r.rub, os.path.dirname(path), self.recipe_dirs)
                view = self._rub_view(found or rub_files.get(rub_id, "")) if (found or rub_id in rub_files) else None
                if view:
                    rub_doc = view.pop("doc")
                    view.pop("source")
                    rub = {"id": rub_id, **view}
            out.append({
                "id": rid, "valid": True, "own": own, "name": r.name, "version": r.version, "cut": r.cut,
                "description": r.description, "pit_target_c": r.pit_target_c, "pit_tolerance_c": r.pit_tolerance_c,
                "meat_target_c": r.meat_target_c, "hours_per_kg": r.hours_per_kg, "hours_fixed": r.hours_fixed,
                "fridge_out_min": r.fridge_out_min, "dry_brine_hours": r.dry_brine_hours,
                "stall_from_c": r.stall_from_c, "stall_to_c": r.stall_to_c, "wrap_at_c": r.wrap_at_c,
                "wrap_material": r.wrap_material, "rest_min_minutes": r.rest_min_minutes,
                "rest_ideal_minutes": r.rest_ideal_minutes, "hold_min_c": r.hold_min_c, "rub": rub,
                "style": r.style, "done_by": r.done_by, "probe": r.probe, "stall": r.stall_enabled,
                "pull_c": r.pull_c, "stages": r.stages, "finish": r.finish,
                "warnings": recipes.warnings(r.raw, rub_doc), "source": source,
                "results": results.get(r.name, []),
            })
        return out

    def _recipe(self, recipe_id: str) -> recipes.Recipe:
        path = self._recipe_files().get(recipe_id)
        if not path:
            raise ServiceError(f"no recipe called '{recipe_id}'; there are: {', '.join(self._recipe_files()) or 'none'}")
        try:
            return recipes.load_recipe(path, self.recipe_dirs)
        except recipes.RecipeError as e:
            raise ServiceError(str(e)) from None

    # --- stacks -------------------------------------------------------------------------------
    def _stack_files(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for d in self.recipe_dirs:                 # later folders win: your copy replaces a shipped stack
            folder = os.path.join(d, "stacks")
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                if name.endswith((".yaml", ".yml")):
                    found[os.path.splitext(name)[0]] = os.path.join(folder, name)
        return found

    def _stack_doc(self, stack_id: str) -> dict:
        path = self._stack_files().get(library.slug(stack_id or ""))
        if not path:
            raise ServiceError(f"no stack called '{stack_id}'; there are: {', '.join(self._stack_files()) or 'none'}")
        try:
            with open(path, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            raise ServiceError(f"stack {stack_id}: {e}") from None
        problems = stacks.validate_stack(doc)
        if problems:
            raise ServiceError(f"stack {stack_id}: " + "; ".join(problems))
        return doc

    def stack_results(self) -> dict[str, list[dict]]:
        """Finished cooks grouped by stack and then by stack version: what each version of a stack produced
        (score, how steady the pit ran, the burn rate in kg of fuel an hour, hotfixes = fuel added mid-cook)."""
        store = self.store()
        by_stack: dict[str, dict[str, dict]] = {}
        for cook_id in store.cooks():
            m = store.meta(cook_id)
            used = m.get("stack") or {}
            if m.get("status") != "done" or not used.get("name"):
                continue
            res, an = m.get("result") or {}, m.get("analysis") or {}
            v = by_stack.setdefault(used["name"], {}).setdefault(
                str(used.get("version") or "?"), {"version": str(used.get("version") or "?"), "cooks": []})
            v["cooks"].append({
                "id": cook_id, "opened_at": m.get("opened_at"), "recipe": m.get("recipe_name"),
                "score": res.get("score"), "fuel_used_kg": res.get("fuel_used_kg"),
                "hours_on_pit": an.get("hours_on_pit"), "pit_stdev_c": (an.get("pit") or {}).get("stdev_c"),
                "burn_rate_kg_h": res.get("burn_rate_kg_h"),
                "hotfixes": (an.get("events") or {}).get("hotfix", 0),
                "wood_added": (an.get("events") or {}).get("wood", 0)})
        avg = lambda xs, digits=1: round(sum(xs) / len(xs), digits) if xs else None  # noqa: E731
        out: dict[str, list[dict]] = {}
        for name, versions in by_stack.items():
            rows = []
            for v in versions.values():
                cooks = v["cooks"]
                v["count"] = len(cooks)
                v["avg_score"] = avg([c["score"] for c in cooks if c["score"] is not None])
                v["avg_pit_stdev_c"] = avg([c["pit_stdev_c"] for c in cooks if c["pit_stdev_c"] is not None])
                v["avg_burn_rate_kg_h"] = avg([c["burn_rate_kg_h"] for c in cooks if c["burn_rate_kg_h"]], 2)
                v["hotfixes"] = sum(c["hotfixes"] for c in cooks)
                v["first_cook"] = min(c["opened_at"] or "" for c in cooks)
                rows.append(v)
            out[name] = sorted(rows, key=lambda v: v["first_cook"])
        return out

    def last_stack(self) -> str | None:
        """The stack the most recent cook was opened with, if it still exists: the default for the next cook."""
        store, files = self.store(), self._stack_files()
        for cook_id in reversed(store.cooks()):
            used = (store.meta(cook_id).get("stack") or {}).get("id")
            if used:
                return used if used in files else None
        return None

    def list_stacks(self) -> list[dict]:
        results, last = self.stack_results(), self.last_stack()
        out = []
        for sid, path in sorted(self._stack_files().items()):
            own = self._is_own(path)
            try:
                with open(path, encoding="utf-8") as f:
                    source = f.read()
                doc = yaml.safe_load(source)
                problems = stacks.validate_stack(doc)
            except (OSError, yaml.YAMLError) as e:
                problems = [str(e)]
            if problems:
                out.append({"id": sid, "valid": False, "error": "; ".join(problems), "own": own})
                continue
            out.append({
                "id": sid, "valid": True, "own": own, "name": doc["name"], "version": str(doc.get("version", "1.0.0")),
                "description": doc.get("description", ""), "smoker": doc.get("smoker") or {},
                "fuel": doc.get("fuel") or {}, "wood": doc.get("wood") or [], "water_pan": stacks.water_pan(doc),
                "airflow": doc.get("airflow") or {}, "notes": doc.get("notes", ""),
                "fire": stacks.fire_line(doc), "last_used": sid == last, "source": source,
                "results": results.get(doc["name"], []),
            })
        return out

    def stack_vocabulary(self) -> dict:
        return {"smokers": list(stacks.SMOKERS), "fuels": list(stacks.FUELS), "wood_forms": list(stacks.WOOD_FORMS),
                "controllers": list(stacks.CONTROLLERS)}

    def save_stack(self, form: dict) -> dict:
        with self.lock:
            try:
                return library.save_stack(self.library_root(), form or {})
            except library.LibraryError as e:
                raise ServiceError(str(e)) from None

    def save_rub(self, form: dict) -> dict:
        with self.lock:
            try:
                return library.save_rub(self.library_root(), form or {})
            except library.LibraryError as e:
                raise ServiceError(str(e)) from None

    def save_recipe(self, form: dict) -> dict:
        with self.lock:
            form = dict(form or {})
            if form.get("rub") and library.slug(form["rub"]) not in self._rub_files():
                raise ServiceError(f"no rub called '{form['rub']}'; save the rub first, or choose one of: "
                                   f"{', '.join(self._rub_files()) or 'none'}")
            try:
                out = library.save_recipe(self.library_root(), form, self.recipe_dirs)
            except library.LibraryError as e:
                raise ServiceError(str(e)) from None
            saved = next((r for r in self.list_recipes() if r["id"] == out["id"]), None)
            out["warnings"] = (saved or {}).get("warnings", [])
            return out

    def save_yaml(self, kind: str, text: str, change: str = "") -> dict:
        """Save a recipe, rub or stack from its YAML text, exactly as typed (comments and layout are kept)."""
        if kind not in KINDS:
            raise ServiceError("kind is recipe, rub or stack")
        with self.lock:
            try:
                out = library.save_yaml(self.library_root(), kind, str(text or ""), self.recipe_dirs, str(change or ""))
            except library.LibraryError as e:
                raise ServiceError(str(e)) from None
            if kind == "recipe":
                saved = next((r for r in self.list_recipes() if r["id"] == out["id"]), None)
                out["warnings"] = (saved or {}).get("warnings", [])
            return out

    def delete_item(self, kind: str, item_id: str) -> dict:
        if kind not in KINDS:
            raise ServiceError("kind is recipe, rub or stack")
        with self.lock:
            try:
                library.delete(self.library_root(), kind, item_id)
            except library.LibraryError as e:
                raise ServiceError(str(e)) from None
            return {"deleted": library.slug(item_id), "kind": kind}

    def item_history(self, kind: str, item_id: str) -> list[dict]:
        if kind not in KINDS:
            raise ServiceError("kind is recipe, rub or stack")
        try:
            return library.history(self.library_root(), kind, item_id)
        except library.LibraryError as e:
            raise ServiceError(str(e)) from None

    def plan(self, recipe_id: str, weight_kg: float | None, serve_at: str, stack: str | None = None) -> dict:
        recipe = self._recipe(recipe_id)
        fire = stacks.fire_line(self._stack_doc(stack)) if stack else None
        try:
            p = planmod.make_plan(recipe, weight_kg, when(serve_at), fire)
        except recipes.RecipeError as e:
            raise ServiceError(str(e)) from None
        return {"recipe": recipe.name, "version": recipe.version, "weight_kg": weight_kg, "stack": stack or None,
                "cook_hours": round(p.cook_hours, 2), "text": planmod.render(p),
                "steps": [{"at": s.at.isoformat(timespec="minutes"), "step": s.what, "detail": s.detail}
                          for s in p.steps]}

    # --- the cook ---------------------------------------------------------------------------------
    def engine(self) -> Engine | None:
        with self.lock:
            store = self.store()
            cook_id = store.active()
            if not cook_id:
                self._engine = None
                return None
            if self._engine is None or self._engine.cook_id != cook_id:
                meta = store.meta(cook_id)
                recipe = recipes.from_doc(meta["recipe"], meta.get("recipe_path", ""))
                self._engine = Engine(recipe, meta, store, notify.from_settings(self.cfg()), cook_id)
            return self._engine

    def _need_engine(self) -> Engine:
        e = self.engine()
        if e is None:
            raise ServiceError("no cook is open")
        return e

    @staticmethod
    def _probes(probes, need: str = "needed") -> list[int]:
        """Probe numbers from 1, [1, 2], "1,2" or None, checked and de-duplicated. `need` is the recipe's:
        needed = at least one (default probe 1), optional = default probe 1 but none is allowed,
        none = default no probe."""
        if probes == [] and need != "needed":
            return []
        if probes in (None, "", []):
            if need == "none":
                return []
            probes = [1]
        if isinstance(probes, str):
            probes = [p for p in probes.replace(" ", "").split(",") if p]
        if not isinstance(probes, (list, tuple)):
            probes = [probes]
        out: list[int] = []
        for p in probes:
            try:
                n = int(p)
            except (TypeError, ValueError):
                raise ServiceError(f"'{p}' is not a probe number") from None
            if not 1 <= n <= MAX_PROBES:
                raise ServiceError(f"probe numbers run from 1 to {MAX_PROBES}")
            if n not in out:
                out.append(n)
        return out

    def start(self, recipe_id: str, weight_kg: float | None, serve_at: str, probes=None,
              probe_labels: dict | None = None, stack: str | None = None) -> dict:
        with self.lock:
            recipe = self._recipe(recipe_id)
            stack_doc = self._stack_doc(stack) if stack else None
            serve = when(serve_at)
            numbers = self._probes(probes, recipe.probe)
            labels = {n: str(v).strip()[:24] for n, v in ((int(k), v) for k, v in (probe_labels or {}).items())
                      if n in numbers and str(v).strip()}
            try:
                p = planmod.make_plan(recipe, weight_kg, serve, stacks.fire_line(stack_doc) if stack_doc else None)
            except recipes.RecipeError as e:
                raise ServiceError(str(e)) from None
            meta = {
                "recipe_name": recipe.name, "recipe_version": recipe.version, "recipe_path": recipe.path,
                "weight_kg": weight_kg, "serve_at": serve.isoformat(timespec="seconds"), "probes": numbers,
                "probe_labels": labels,
                "plan": [{"at": s.at.isoformat(timespec="minutes"), "step": s.what} for s in p.steps],
                "recipe": recipe.raw,
            }
            by_no = {s["no"]: s["name"] for s in self.probe_sources()}
            meta["probe_sources"] = {n: by_no[n] for n in numbers if n in by_no}    # which sensor each probe was
            if recipe.rub:                       # the exact rub used, so the cook still knows it after the rub changes
                found = recipes.find_rub(recipe.rub, os.path.dirname(recipe.path), self.recipe_dirs)
                view = self._rub_view(found) if found else None
                if view:
                    meta["rub"] = {"name": view["name"], "version": view["version"],
                                   "ingredients": view["ingredients"]}
            if stack_doc:                        # the exact stack used: smoker, fuel, wood, vents
                meta["stack"] = stacks.snapshot(stack_doc, library.slug(stack))
            store = self.store()
            if store.active():
                raise ServiceError(f"cook {store.active()} is still open; close it first")
            cook_id = store.open_cook(recipe.name, _now().astimezone(), meta)
            self._engine = None
            return {"cook": cook_id, "plan": planmod.render(p)}

    def log_step(self, kind: str, note: str = "") -> dict:
        kind = str(kind).strip().lower()
        if kind not in STEPS:
            raise ServiceError(f"unknown step '{kind}'; use one of: {', '.join(STEPS)}")
        if kind == "note" and not note.strip():
            raise ServiceError("a note needs some text")
        with self.lock:
            e = self._need_engine()
            if kind == "meat-on" and e.state.get("meat_on_at"):
                raise ServiceError("the meat is already logged as on")
            if kind in ("wrap", "rest") and not e.state.get("meat_on_at"):
                raise ServiceError("log meat-on first")
            ev = e.manual(kind, note.strip(), _now())
            return {"cook": e.cook_id, "logged": ev.message}

    def set_probe(self, probe, in_meat: bool) -> dict:
        """Say that a probe has been pulled out of the meat, or put back in."""
        with self.lock:
            e = self._need_engine()
            try:
                no = int(probe)
            except (TypeError, ValueError):
                raise ServiceError("probe is a number") from None
            if no not in e.probe_nos:
                raise ServiceError(f"probe {no} is not one of this cook's probes ({', '.join(map(str, e.probe_nos))})")
            ev = e.set_probe(no, bool(in_meat), _now())
            return {"cook": e.cook_id, "logged": ev.message if ev else "no change",
                    "in_meat": [n for n in e.probe_nos if n not in e.probes_out()]}

    def record_check(self, kind: str, data: dict | None, verdict: str | None = None, by: str = "",
                     note: str = "") -> dict:
        """Store what a photo showed as data points (the photo itself is never stored)."""
        with self.lock:
            e = self._need_engine()
            try:
                clean = checkmod.validate(str(kind), data, verdict)
            except checkmod.CheckError as err:
                raise ServiceError(str(err)) from None
            message = e.record_check(str(kind), clean, verdict, str(by)[:60], str(note).strip()[:200], _now())
            return {"cook": e.cook_id, "logged": message}

    def styles(self) -> dict:
        """The kinds of cook a recipe can be, with the defaults each one sets."""
        return {name: {"stall": d["stall"], "done_by": d["done_by"], "probe": d["probe"],
                       "rest_min_minutes": d["rest"][0], "rest_ideal_minutes": d["rest"][1]}
                for name, d in recipes.STYLES.items()}

    def check_vocabulary(self) -> dict:
        return {kind: {"title": spec["title"], "verdicts": spec["verdicts"],
                       "fields": {k: ({"choices": f[1]} if f[0] == "choice" else {"min": f[1], "max": f[2]})
                                  for k, f in spec["fields"].items()}}
                for kind, spec in checkmod.VOCAB.items()}

    def smoke_test(self) -> dict:
        with self.lock:
            e = self._need_engine()
            r = self.read_now()
            if r is None:
                r = sources.Reading(at=_now(), connected=False)
            checks = smoketest.run(e.recipe, r, e.state.get("recent", []), e.notifier, e.probe_nos,
                                   smoke_check=(e.state.get("checks") or {}).get("smoke"))
            ok = smoketest.passed(checks)
            e.store.add_event(e.cook_id, r.at, "smoke-test", "smoke test " + ("passed" if ok else "failed"),
                              {c.name: c.result for c in checks})
            e.note_smoke_test(r.at, ok)
            return {"passed": ok, "checks": [{"name": c.name, "result": c.result, "detail": c.detail} for c in checks],
                    "text": smoketest.render(checks)}

    def close(self, score: float | None = None, cooked_weight_kg: float | None = None, notes: str = "",
              abandon: bool = False, fuel_used_kg: float | None = None) -> dict:
        with self.lock:
            e = self._need_engine()
            result: dict = {}
            if score is not None:
                if not 0 <= float(score) <= 10:
                    raise ServiceError("score is 0 to 10")
                result["score"] = float(score)
            if cooked_weight_kg is not None:
                result["cooked_weight_kg"] = float(cooked_weight_kg)
                raw = e.meta.get("weight_kg")
                if raw:
                    result["yield"] = round(float(cooked_weight_kg) / float(raw), 3)
            if fuel_used_kg is not None:
                try:
                    used = float(fuel_used_kg)
                except (TypeError, ValueError):
                    raise ServiceError("fuel_used_kg is a number of kilograms") from None
                if not 0 <= used <= 100:
                    raise ServiceError("fuel_used_kg is a number of kilograms")
                result["fuel_used_kg"] = used
            if notes:
                result["notes"] = notes
            status = "abandoned" if abandon else "done"
            analysis = e.analysis(_now())
            rate = burn_rate(result.get("fuel_used_kg"), (analysis or {}).get("hours_on_pit"))
            if rate is not None:
                result["burn_rate_kg_h"] = rate
            if analysis:
                counts: dict = {}
                for ev in e.store.events(e.cook_id):
                    if ev["event"] in ("lid-open", "pit-drift", "fire-dying", "spritz", "hotfix", "wood", "probe-lost", "no-data",
                                           "behind"):
                        counts[ev["event"]] = counts.get(ev["event"], 0) + 1
                analysis["events"] = counts
                meta = e.store.meta(e.cook_id)
                meta["analysis"] = analysis
                e.store.write_meta(e.cook_id, meta)
            e.store.close_cook(e.cook_id, _now().astimezone(), status, result)
            cook_id, self._engine = e.cook_id, None
            return {"cook": cook_id, "status": status, "result": result}

    # --- reading ----------------------------------------------------------------------------------
    def read_now(self) -> sources.Reading | None:
        try:
            r = sources.from_settings(self.cfg().get("source")).read()
            self.last_reading, self.last_error = r, None
            return r
        except (sources.SourceError, KeyError) as e:
            self.last_error = str(e)
            return None

    def status(self) -> dict:
        with self.lock:
            e = self.engine()
            r = self.last_reading
            if r is not None and (_now() - r.at).total_seconds() > 120:
                r = None
            sources_ = self.probe_sources()
            cook = e.overview(_now(), r) if e else None
            if cook:
                used = {int(k): v for k, v in (e.meta.get("probe_sources") or {}).items()}
                now_names = {s["no"]: s["name"] for s in sources_}
                for p in cook["probes"]:
                    p["source"] = used.get(p["no"]) or now_names.get(p["no"])
                cook["stack"] = e.meta.get("stack")
                if cook["stack"]:
                    cook["stack"] = {**cook["stack"], "fire": stacks.fire_line(cook["stack"])}
            return {"now": _now().isoformat(timespec="seconds"), "reading": _reading_dict(r),
                    "source_error": self.last_error, "probe_sources": sources_, "cook": cook}

    def status_text(self) -> str:
        with self.lock:
            e = self.engine()
            r = self.last_reading or self.read_now()
            if e is None:
                if r is None or r.pit is None:
                    return "No cook is open, and there is no pit reading right now."
                return f"No cook is open. Pit is at {r.pit:.0f} C."
            return e.status(_now(), r)

    # --- history ----------------------------------------------------------------------------------
    def cooks(self) -> list[dict]:
        store = self.store()
        out = []
        for cook_id in reversed(store.cooks()):
            m = store.meta(cook_id)
            out.append({"id": cook_id, "recipe": m.get("recipe_name"), "version": m.get("recipe_version"),
                        "status": m.get("status"), "weight_kg": m.get("weight_kg"), "opened_at": m.get("opened_at"),
                        "stack": (m.get("stack") or {}).get("name"),
                        "closed_at": m.get("closed_at"), "result": m.get("result") or {}})
        return out

    def cook(self, cook_id: str, max_points: int = 600) -> dict:
        store = self.store()
        if cook_id not in store.cooks():
            raise ServiceError(f"no cook called '{cook_id}'")
        m = store.meta(cook_id)
        curve: list[dict] = []
        path = os.path.join(store.dir(cook_id), "curve.csv")
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            stride = max(1, len(rows) // max_points)
            numbers = [int(n) for n in (m.get("probes") or [m.get("probe", 1)])]
            for row in rows[::stride] + (rows[-1:] if stride > 1 else []):
                def num(key):
                    try:
                        return float(row.get(key) or "")
                    except ValueError:
                        return None
                probes = {str(n): num(f"probe{n}") for n in numbers}
                present = [v for v in probes.values() if v is not None]
                curve.append({"at": row["at"], "pit_c": num("pit"), "meat_c": min(present) if present else None,
                              "probes_c": probes, "fan_pct": num("fan"), "ambient_c": num("ambient"),
                              "humidity_pct": num("humidity"), "rain": num("rain"), "est_off_min": num("est_off_min")})
        meta = {k: v for k, v in m.items() if k != "recipe"}
        numbers = [int(n) for n in (m.get("probes") or [m.get("probe", 1)])]
        labels = {int(k): v for k, v in (m.get("probe_labels") or {}).items()}
        srcs = {int(k): v for k, v in (m.get("probe_sources") or {}).items()}
        meta["probes"] = [{"no": n, "label": labels.get(n) or f"probe {n}", "source": srcs.get(n)} for n in numbers]
        return {"meta": meta, "events": store.events(cook_id), "curve": curve}

    # --- background runner ------------------------------------------------------------------------
    def run_forever(self) -> None:
        while not self._stop.is_set():
            every = 15.0
            try:
                every = float(self.cfg().get("poll_seconds", 15))
                r = self.read_now()
                with self.lock:
                    e = self.engine()
                    if e is not None:
                        e.tick(r if r is not None else sources.Reading(at=_now(), connected=False))
            except Exception as ex:  # keep the loop alive whatever one tick does
                self.last_error = f"runner: {ex}"
                print(f"runner: {ex!r}", flush=True)
            self._stop.wait(max(3.0, every))

    def start_runner(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="openbrisket-runner", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
        time.sleep(0)
