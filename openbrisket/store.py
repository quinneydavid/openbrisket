"""The cook log: a private git repository where every cook is a folder and every event is a commit.

    cooks/2026-11-14-texas-brisket/
        cook.yaml     what was planned: recipe (and version), weight, serving time, probe
        events.yaml   everything that happened, in order
        curve.csv     the temperature and fan curve (committed when the cook closes)
        state.json    working state of the running cook (not committed)

Only text is stored. Photos are never written here: a photo is turned into data points and the data
points are what gets logged.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
from datetime import datetime

import yaml

MAX_PROBES = 9
# est_off_min = the estimate at that moment, in minutes until the meat comes off (blank before there is one)
CURVE_FIELDS = ["at", "pit", *[f"probe{n}" for n in range(1, MAX_PROBES + 1)], "fan", "pit_target",
                "ambient", "humidity", "est_off_min"]
GIT_ID = ["-c", "user.name=OpenBrisket", "-c", "user.email=openbrisket@localhost"]


class StoreError(Exception):
    pass


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class Store:
    def __init__(self, root: str):
        self.root = root

    # --- git ---------------------------------------------------------------------------------
    def _git(self, *args: str) -> str:
        r = subprocess.run(["git", "-C", self.root, *GIT_ID, *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise StoreError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def ensure(self) -> None:
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        if not os.path.isdir(os.path.join(self.root, ".git")):
            subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True, capture_output=True)
            with open(os.path.join(self.root, ".gitignore"), "w", encoding="utf-8") as f:
                f.write("state.json\n")
            with open(os.path.join(self.root, "README.md"), "w", encoding="utf-8") as f:
                f.write("# My cooks\n\nPrivate cook log written by OpenBrisket. One folder per cook, one commit per event.\n")
            self._git("add", "-A")
            self._git("commit", "-q", "-m", "init cook log")

    def commit(self, message: str) -> None:
        self._git("add", "-A")
        if self._git("status", "--porcelain").strip():
            self._git("commit", "-q", "-m", message)

    def log(self, cook_id: str, limit: int = 20) -> list[str]:
        out = self._git("log", f"-{limit}", "--format=%ad %s", "--date=format:%d %b %H:%M", "--", cook_id)
        return out.splitlines()

    # --- cooks -------------------------------------------------------------------------------
    def dir(self, cook_id: str) -> str:
        return os.path.join(self.root, cook_id)

    def _path(self, cook_id: str, name: str) -> str:
        return os.path.join(self.dir(cook_id), name)

    def open_cook(self, recipe_name: str, started: datetime, meta: dict) -> str:
        self.ensure()
        active = self.active()
        if active:
            raise StoreError(f"cook {active} is still open; close it first")
        base = f"{started.strftime('%Y-%m-%d')}-{slug(recipe_name)}"
        cook_id, n = base, 2
        while os.path.exists(self.dir(cook_id)):
            cook_id, n = f"{base}-{n}", n + 1
        os.makedirs(self.dir(cook_id))
        meta = dict(meta, id=cook_id, status="open", opened_at=started.isoformat(timespec="seconds"))
        self.write_meta(cook_id, meta)
        with open(self._path(cook_id, "events.yaml"), "w", encoding="utf-8") as f:
            f.write("# Everything that happened, in order.\n")
        self.commit(f"{cook_id}: open cook")
        return cook_id

    def meta(self, cook_id: str) -> dict:
        with open(self._path(cook_id, "cook.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def write_meta(self, cook_id: str, meta: dict) -> None:
        with open(self._path(cook_id, "cook.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(meta, f, sort_keys=False, allow_unicode=True)

    def cooks(self) -> list[str]:
        if not os.path.isdir(self.root):
            return []
        return sorted(d for d in os.listdir(self.root)
                      if os.path.isfile(os.path.join(self.root, d, "cook.yaml")))

    def active(self) -> str | None:
        for cook_id in reversed(self.cooks()):
            if self.meta(cook_id).get("status") == "open":
                return cook_id
        return None

    def add_event(self, cook_id: str, at: datetime, kind: str, message: str, data: dict | None = None,
                  commit: bool = True) -> None:
        entry = {"at": at.isoformat(timespec="seconds"), "event": kind, "message": message}
        if data:
            entry["data"] = data
        with open(self._path(cook_id, "events.yaml"), "a", encoding="utf-8") as f:
            f.write(yaml.safe_dump([entry], sort_keys=False, allow_unicode=True))
        if commit:
            self.commit(f"{cook_id}: {message}")

    def events(self, cook_id: str) -> list[dict]:
        with open(self._path(cook_id, "events.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f) or []

    def append_curve(self, cook_id: str, row: dict) -> None:
        p = self._path(cook_id, "curve.csv")
        fields = CURVE_FIELDS
        new = not os.path.exists(p)
        if not new:                       # a cook opened by an older version keeps the columns it started with
            with open(p, newline="", encoding="utf-8") as f:
                fields = next(csv.reader(f), None) or CURVE_FIELDS
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fields})

    def load_state(self, cook_id: str) -> dict:
        p = self._path(cook_id, "state.json")
        if not os.path.exists(p):
            return {}
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def save_state(self, cook_id: str, state: dict) -> None:
        p = self._path(cook_id, "state.json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, p)

    def close_cook(self, cook_id: str, at: datetime, status: str, result: dict) -> None:
        meta = self.meta(cook_id)
        meta["status"] = status
        meta["closed_at"] = at.isoformat(timespec="seconds")
        if result:
            meta["result"] = result
        self.write_meta(cook_id, meta)
        state = self._path(cook_id, "state.json")
        if os.path.exists(state):
            os.remove(state)
        self.commit(f"{cook_id}: {'merge' if status == 'done' else 'close'} cook ({status})")
