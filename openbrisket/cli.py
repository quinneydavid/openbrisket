"""The `openbrisket` command."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import yaml

from . import __version__, notify, plan as planmod, recipes, settings, stacks, smoketest, sources
from .cook import Engine
from .store import Store, StoreError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _when(text: str) -> datetime:
    """'2026-11-14 18:00' in local time (or any ISO form) to an aware datetime."""
    dt = datetime.fromisoformat(text.strip().replace(" ", "T", 1))
    return dt if dt.tzinfo else dt.astimezone()


def _store(cfg: dict) -> Store:
    return Store(settings.cooks_path(cfg))


def _active(store: Store) -> str:
    cook_id = store.active()
    if not cook_id:
        sys.exit("no cook is open. Start one with: openbrisket apply <recipe> --serve '<date time>'")
    return cook_id


def _stack(path: str | None) -> dict | None:
    """A stack file given with --stack, checked."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    problems = stacks.validate_stack(doc)
    if problems:
        raise recipes.RecipeError(f"{path}: " + "; ".join(problems))
    return doc


def _engine(cfg: dict, store: Store, cook_id: str, notifier=None) -> Engine:
    meta = store.meta(cook_id)
    recipe = recipes.from_doc(meta["recipe"], meta.get("recipe_path", ""))
    return Engine(recipe, meta, store, notifier or notify.from_settings(cfg), cook_id)


# --- commands -------------------------------------------------------------------------------------
def cmd_init(args) -> int:
    path, created = settings.init()
    print(("created " if created else "already exists: ") + path)
    print("Edit it to set where readings come from and where notes are sent.")
    return 0


def cmd_validate(args) -> int:
    bad = recipes.validate_tree(args.path)
    for path, problems in bad.items():
        for p in problems:
            print(f"{path}: {p}")
    print("smoke-test: " + ("FAILED" if bad else "passed"))
    return 1 if bad else 0


def cmd_plan(args) -> int:
    recipe = recipes.load_recipe(args.recipe)
    stack = _stack(args.stack)
    print(planmod.render(planmod.make_plan(recipe, args.weight, _when(args.serve),
                                           stacks.fire_line(stack) if stack else None)))
    return 0


def cmd_apply(args) -> int:
    cfg = settings.load()
    recipe = recipes.load_recipe(args.recipe)
    serve = _when(args.serve)
    stack = _stack(args.stack)
    the_plan = planmod.make_plan(recipe, args.weight, serve, stacks.fire_line(stack) if stack else None)
    store = _store(cfg)
    meta = {
        "recipe_name": recipe.name, "recipe_version": recipe.version, "recipe_path": recipe.path,
        "weight_kg": args.weight, "serve_at": serve.isoformat(timespec="seconds"),
        "probes": [int(p) for p in str(args.probe).split(",") if p.strip()],
        "plan": [{"at": s.at.isoformat(timespec="minutes"), "step": s.what} for s in the_plan.steps],
        "recipe": recipe.raw,
    }
    if stack:
        meta["stack"] = stacks.snapshot(stack, os.path.splitext(os.path.basename(args.stack))[0])
    try:
        cook_id = store.open_cook(recipe.name, _now().astimezone(), meta)
    except StoreError as e:
        sys.exit(str(e))
    print(planmod.render(the_plan))
    print(f"\ncook opened: {cook_id}\nlog: {store.dir(cook_id)}")
    print("Next: light the fire, keep `openbrisket run` going, then `openbrisket smoke-test`.")
    return 0


def cmd_run(args) -> int:
    cfg = settings.load()
    store = _store(cfg)
    cook_id = _active(store)
    engine = _engine(cfg, store, cook_id)
    source = sources.ReplaySource(args.replay) if args.replay else sources.from_settings(cfg.get("source"))
    every = 0 if args.replay else float(cfg.get("poll_seconds", 15))
    print(f"running {cook_id} (ctrl-c to stop; the cook stays open)", flush=True)
    try:
        while True:
            if args.replay and source.exhausted():
                break
            try:
                reading = source.read()
            except sources.SourceError as e:
                print(f"source: {e}", file=sys.stderr, flush=True)
                reading = sources.Reading(at=_now(), connected=False)
            engine.tick(reading)
            if args.once:
                break
            if store.meta(cook_id).get("status") != "open":
                print("cook closed; stopping")
                break
            time.sleep(every)
    except KeyboardInterrupt:
        print("\nstopped; the cook is still open")
    return 0


def cmd_smoke_test(args) -> int:
    cfg = settings.load()
    store = _store(cfg)
    cook_id = _active(store)
    engine = _engine(cfg, store, cook_id)
    try:
        reading = sources.from_settings(cfg.get("source")).read()
    except sources.SourceError as e:
        print(f"source: {e}", file=sys.stderr)
        reading = sources.Reading(at=_now(), connected=False)
    checks = smoketest.run(engine.recipe, reading, engine.state.get("recent", []), engine.notifier,
                           engine.probe_nos)
    print(smoketest.render(checks))
    ok = smoketest.passed(checks)
    store.add_event(cook_id, reading.at, "smoke-test", "smoke test " + ("passed" if ok else "failed"),
                    {c.name: c.result for c in checks})
    return 0 if ok else 1


def cmd_log(args) -> int:
    cfg = settings.load()
    store = _store(cfg)
    cook_id = _active(store)
    event = _engine(cfg, store, cook_id).manual(args.kind, " ".join(args.note), _now())
    print(f"{cook_id}: {event.message}")
    return 0


def cmd_status(args) -> int:
    cfg = settings.load()
    store = _store(cfg)
    cook_id = _active(store)
    engine = _engine(cfg, store, cook_id)
    try:
        reading = sources.from_settings(cfg.get("source")).read()
    except sources.SourceError as e:
        print(f"source: {e}", file=sys.stderr)
        reading = None
    print(engine.status(_now(), reading))
    return 0


def cmd_history(args) -> int:
    cfg = settings.load()
    store = _store(cfg)
    cook_id = args.cook or store.active() or (store.cooks() or [None])[-1]
    if not cook_id:
        sys.exit("no cooks yet")
    print("\n".join(store.log(cook_id, args.limit)))
    return 0


def cmd_close(args) -> int:
    cfg = settings.load()
    store = _store(cfg)
    cook_id = _active(store)
    result = {}
    if args.score is not None:
        if not 0 <= args.score <= 10:
            sys.exit("score is 0 to 10")
        result["score"] = args.score
    if args.cooked_weight is not None:
        result["cooked_weight_kg"] = args.cooked_weight
        raw = store.meta(cook_id).get("weight_kg")
        if raw:
            result["yield"] = round(args.cooked_weight / raw, 3)
    if args.fuel_used is not None:
        result["fuel_used_kg"] = args.fuel_used
    if args.notes:
        result["notes"] = args.notes
    status = "abandoned" if args.abandon else "done"
    store.close_cook(cook_id, _now().astimezone(), status, result)
    print(f"{cook_id}: closed ({status})")
    return 0


def cmd_serve(args) -> int:
    from . import server
    server.serve(args.host, args.port)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="openbrisket", description="BBQ as Code.")
    ap.add_argument("--version", action="version", version=f"openbrisket {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the settings file").set_defaults(fn=cmd_init)

    p = sub.add_parser("validate", help="check every recipe, rub and stack under a folder (the CI smoke test)")
    p.add_argument("path", nargs="?", default="recipes")
    p.set_defaults(fn=cmd_validate)

    for name, fn, text in (("plan", cmd_plan, "show the timeline for a cook without starting it"),
                           ("apply", cmd_apply, "open a cook from a recipe")):
        p = sub.add_parser(name, help=text)
        p.add_argument("recipe")
        p.add_argument("--weight", type=float, help="raw weight in kg")
        p.add_argument("--serve", required=True, help="serving time, e.g. '2026-11-14 18:00'")
        p.add_argument("--stack", metavar="FILE", help="stack file: smoker, fuel, wood (recipes/stacks/...)")
        if name == "apply":
            p.add_argument("--probe", default="1", help="food probes in the meat, e.g. 1 or 1,2,3 (default 1)")
        p.set_defaults(fn=fn)

    p = sub.add_parser("run", help="follow the open cook: read, detect, leave notes")
    p.add_argument("--once", action="store_true", help="take one reading and exit")
    p.add_argument("--replay", metavar="CSV", help="play back a recorded curve instead of live readings")
    p.set_defaults(fn=cmd_run)

    sub.add_parser("smoke-test", help="pre-cook check: feed, pit stable, fan, probes, notes").set_defaults(
        fn=cmd_smoke_test)

    p = sub.add_parser("log", help="record a step you did: meat-on, wrap, spritz, hotfix (fuel added), wood, rest, note")
    p.add_argument("kind")
    p.add_argument("note", nargs="*")
    p.set_defaults(fn=cmd_log)

    sub.add_parser("status", help="where the open cook stands and when it will be done").set_defaults(fn=cmd_status)

    p = sub.add_parser("history", help="the commit log of a cook")
    p.add_argument("cook", nargs="?")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("close", help="merge the cook: record the result and close it")
    p.add_argument("--score", type=float, help="0 to 10")
    p.add_argument("--cooked-weight", type=float, help="weight after cooking, kg")
    p.add_argument("--fuel-used", type=float, help="fuel burnt over the whole cook, kg (gives the burn rate)")
    p.add_argument("--notes")
    p.add_argument("--abandon", action="store_true", help="close without merging (a cook that went wrong)")
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("serve", help="run the web page, the API and the MCP endpoint, and follow cooks")
    p.add_argument("--host", default=os.environ.get("OPENBRISKET_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("OPENBRISKET_PORT", "8321")))
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (recipes.RecipeError, StoreError, sources.SourceError, OSError, yaml.YAMLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
