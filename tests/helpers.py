"""Synthetic cooks for the tests: readings generated from the model itself, with a known 'true' k."""
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from openbrisket import model, recipes
from openbrisket.sources import Reading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRISKET = os.path.join(ROOT, "recipes", "texas-brisket.yaml")
START = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)


def brisket():
    return recipes.load_recipe(BRISKET)


def cook_readings(params, pit=121.0, start_meat=8.0, target=95.0, wrap_at=74.0, step_s=15, hours=30.0,
                  start=START, pit_fn=None, fan_fn=None, stop_at_target=True):
    """Yield Readings every `step_s` seconds. `pit_fn(t_h)` and `fan_fn(t_h)` override pit and fan."""
    meat, t, wrapped = start_meat, 0.0, False
    dt_h = step_s / 3600
    while t < hours:
        p = pit_fn(t) if pit_fn else pit
        if wrap_at is not None and meat >= wrap_at:
            wrapped = True
        yield Reading(at=start + timedelta(hours=t), pit=p, probes=[round(meat, 2), None, None],
                      fan=fan_fn(t) if fan_fn else 35.0, fan_on=True, pit_target=pit, connected=True)
        if stop_at_target and meat >= target + 0.5:
            return
        meat += model.rate(meat, p, params, wrapped) * dt_h
        t += dt_h


def scaled(params, factor):
    return replace(params, k=params.k * factor)
