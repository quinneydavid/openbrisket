"""Settings live in one file in the data folder, written by `openbrisket init` and edited there.
Only the folder's location comes from the environment (OPENBRISKET_HOME)."""
from __future__ import annotations

import os

import yaml

TEMPLATE = """\
# OpenBrisket settings. This file holds secrets: keep it private (it is created readable by you only).

# Where readings come from.
source:
  type: homeassistant
  url: http://homeassistant.local:8123
  token: ""            # a Home Assistant long-lived access token
  entities:            # the standard values; see devices/README.md
    connected: binary_sensor.bbq_reader_fan_controller_connected
    pit: sensor.bbq_reader_pit_temperature
    probes:
      - sensor.bbq_reader_probe_1
      - sensor.bbq_reader_probe_2
      - sensor.bbq_reader_probe_3
    fan: sensor.bbq_reader_fan_output
    fan_on: binary_sensor.bbq_reader_fan_switched_on
    pit_target: sensor.bbq_reader_pit_target

# Where notes and photo requests are sent. With no bot token they are printed instead.
telegram:
  bot_token: ""
  chat_id: ""

# Your cooks. A private git repository on this machine; nothing is pushed unless you add a remote.
cooks:
  path: ""             # default: <data folder>/cooks

# Seconds between readings while a cook is running.
poll_seconds: 15
"""


def home() -> str:
    return os.environ.get("OPENBRISKET_HOME") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "openbrisket")


def path() -> str:
    return os.path.join(home(), "settings.yaml")


def init() -> tuple[str, bool]:
    """Create the settings file if missing. Returns (path, created)."""
    p = path()
    if os.path.exists(p):
        return p, False
    os.makedirs(home(), mode=0o700, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(TEMPLATE)
    return p, True


def load() -> dict:
    p = path()
    if not os.path.exists(p):
        return yaml.safe_load(TEMPLATE)
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    base = yaml.safe_load(TEMPLATE)
    for key, value in base.items():
        cfg.setdefault(key, value)
    return cfg


def cooks_path(cfg: dict) -> str:
    return os.path.expanduser((cfg.get("cooks") or {}).get("path") or "") or os.path.join(home(), "cooks")
