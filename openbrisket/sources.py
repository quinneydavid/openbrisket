"""Where readings come from. Every source returns the same `Reading`, so the rest of the app never
knows which thermometer or fan controller is behind it. To support a new device, publish the standard
values (see devices/README.md) or add a Source here."""
from __future__ import annotations

import csv
import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Reading:
    at: datetime
    pit: float | None = None
    probes: list[float | None] = field(default_factory=list)   # food probes, in order
    fan: float | None = None            # fan output, 0-100 %
    fan_on: bool | None = None
    pit_target: float | None = None     # what the controller is set to hold
    connected: bool = True

    def probe(self, number: int) -> float | None:
        """Food probe by its 1-based number."""
        if 1 <= number <= len(self.probes):
            return self.probes[number - 1]
        return None


class SourceError(Exception):
    pass


class Source:
    def read(self) -> Reading:
        raise NotImplementedError


def _float(state) -> float | None:
    try:
        v = float(state)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


class HomeAssistantSource(Source):
    """Reads the standard values from Home Assistant entities over its REST API."""

    def __init__(self, url: str, token: str, entities: dict, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.token = token
        self.entities = entities
        self.timeout = timeout

    def _state(self, entity_id: str | None):
        if not entity_id:
            return None
        req = urllib.request.Request(f"{self.url}/api/states/{entity_id}",
                                     headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r).get("state")
        except Exception as e:  # network, auth, unknown entity
            raise SourceError(f"Home Assistant: {entity_id}: {e}") from e

    def read(self) -> Reading:
        e = self.entities
        connected = self._state(e.get("connected")) if e.get("connected") else "on"
        fan_on = self._state(e.get("fan_on"))
        return Reading(
            at=datetime.now(timezone.utc),
            pit=_float(self._state(e.get("pit"))),
            probes=[_float(self._state(x)) for x in e.get("probes", [])],
            fan=_float(self._state(e.get("fan"))),
            fan_on={"on": True, "off": False}.get(fan_on),
            pit_target=_float(self._state(e.get("pit_target"))),
            connected=connected == "on",
        )


class ReplaySource(Source):
    """Plays back a recorded curve (the CSV a finished cook leaves behind). For tests and dry runs."""

    def __init__(self, path: str):
        with open(path, newline="", encoding="utf-8") as f:
            self.rows = list(csv.DictReader(f))
        self.i = 0

    def exhausted(self) -> bool:
        return self.i >= len(self.rows)

    def read(self) -> Reading:
        if self.exhausted():
            raise SourceError("replay finished")
        row = self.rows[self.i]
        self.i += 1
        probes = [_float(row[k]) for k in sorted(row) if k.startswith("probe")]
        return Reading(
            at=datetime.fromisoformat(row["at"]),
            pit=_float(row.get("pit")), probes=probes, fan=_float(row.get("fan")),
            fan_on=None, pit_target=_float(row.get("pit_target")), connected=True,
        )


def from_settings(cfg: dict) -> Source:
    kind = (cfg or {}).get("type")
    if kind == "homeassistant":
        return HomeAssistantSource(cfg["url"], cfg["token"], cfg.get("entities") or {})
    if kind == "replay":
        return ReplaySource(cfg["path"])
    raise SourceError(f"unknown source type: {kind!r} (set one with `openbrisket init`)")
