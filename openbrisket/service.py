"""One place that does things. The web page, the JSON API, the MCP server and the background runner
all call this, so they cannot disagree about how a cook works."""
from __future__ import annotations

import csv
import os
import secrets
import threading
import time
from datetime import datetime, timezone

import yaml

from . import checks as checkmod, library, notify, plan as planmod, recipes, settings, smoketest, sources
from .cook import Engine
from .store import MAX_PROBES, Store

STEPS = ("meat-on", "wrap", "spritz", "rest", "note")


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
                    ent["probes"] = [str(p).strip() for p in probes if str(p).strip()]
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
        return {"name": doc.get("name"), "description": doc.get("description", ""),
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

    def list_recipes(self) -> list[dict]:
        out = []
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
                "rest_ideal_minutes": r.rest_ideal_minutes, "rub": rub,
                "warnings": recipes.warnings(r.raw, rub_doc), "source": source,
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

    def delete_item(self, kind: str, item_id: str) -> dict:
        if kind not in ("recipe", "rub"):
            raise ServiceError("kind is recipe or rub")
        with self.lock:
            try:
                library.delete(self.library_root(), kind, item_id)
            except library.LibraryError as e:
                raise ServiceError(str(e)) from None
            return {"deleted": library.slug(item_id), "kind": kind}

    def item_history(self, kind: str, item_id: str) -> list[str]:
        try:
            return library.history(self.library_root(), kind, item_id)
        except library.LibraryError as e:
            raise ServiceError(str(e)) from None

    def plan(self, recipe_id: str, weight_kg: float | None, serve_at: str) -> dict:
        recipe = self._recipe(recipe_id)
        try:
            p = planmod.make_plan(recipe, weight_kg, when(serve_at))
        except recipes.RecipeError as e:
            raise ServiceError(str(e)) from None
        return {"recipe": recipe.name, "version": recipe.version, "weight_kg": weight_kg,
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
    def _probes(probes) -> list[int]:
        """Probe numbers from 1, [1, 2], "1,2" or None (default probe 1), checked and de-duplicated."""
        if probes in (None, "", []):
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
              probe_labels: dict | None = None) -> dict:
        with self.lock:
            recipe = self._recipe(recipe_id)
            serve = when(serve_at)
            numbers = self._probes(probes)
            labels = {n: str(v).strip()[:24] for n, v in ((int(k), v) for k, v in (probe_labels or {}).items())
                      if n in numbers and str(v).strip()}
            try:
                p = planmod.make_plan(recipe, weight_kg, serve)
            except recipes.RecipeError as e:
                raise ServiceError(str(e)) from None
            meta = {
                "recipe_name": recipe.name, "recipe_version": recipe.version, "recipe_path": recipe.path,
                "weight_kg": weight_kg, "serve_at": serve.isoformat(timespec="seconds"), "probes": numbers,
                "probe_labels": labels,
                "plan": [{"at": s.at.isoformat(timespec="minutes"), "step": s.what} for s in p.steps],
                "recipe": recipe.raw,
            }
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
              abandon: bool = False) -> dict:
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
            if notes:
                result["notes"] = notes
            status = "abandoned" if abandon else "done"
            analysis = e.analysis(_now())
            if analysis:
                counts: dict = {}
                for ev in e.store.events(e.cook_id):
                    if ev["event"] in ("lid-open", "pit-drift", "fire-dying", "spritz", "probe-lost", "no-data", "behind"):
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
            return {"now": _now().isoformat(timespec="seconds"), "reading": _reading_dict(r),
                    "source_error": self.last_error,
                    "cook": e.overview(_now(), r) if e else None}

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
        meta["probes"] = [{"no": n, "label": labels.get(n) or f"probe {n}"} for n in numbers]
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
