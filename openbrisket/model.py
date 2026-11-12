"""The cook model: how fast meat heats, and when it will be done.

Version 0 of the science, deliberately simple and explainable:

* Outside the stall the meat follows Newton heating: dT/dt = k * (pit - meat).
* Inside the stall band evaporation eats most of the heat, so the rate is multiplied by a factor
  (about 0.2 unwrapped, about 0.7 once wrapped).
* Before any data exists, k comes from the recipe's expected cook time (the prior).
* During the cook, k and the stall factor are re-fitted from the readings and blended with the prior,
  with more weight on the data the more of it there is.

Times are hours, temperatures deg C, rates deg C per hour.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from statistics import median

FRIDGE_C = 4.0
STEP_H = 1.0 / 60.0


@dataclass(frozen=True)
class Params:
    k: float
    stall_lo: float = 65.0
    stall_hi: float = 77.0
    stall_factor: float = 0.2
    wrapped_factor: float = 0.7


def rate(meat: float, pit: float, p: Params, wrapped: bool) -> float:
    r = p.k * (pit - meat)
    if p.stall_lo <= meat < p.stall_hi:
        r *= p.wrapped_factor if wrapped else p.stall_factor
    return r


@dataclass
class Projection:
    hours: float                      # to target; math.inf if it never gets there
    stall_start_h: float | None = None
    wrap_h: float | None = None
    stall_end_h: float | None = None


def simulate(meat: float, pit: float, target: float, p: Params, wrap_at: float | None = None,
             wrapped: bool = False, max_hours: float = 72.0) -> Projection:
    """Integrate forward from `meat` to `target` at a constant pit temperature."""
    out = Projection(hours=math.inf)
    t = 0.0
    if meat >= p.stall_lo:
        out.stall_start_h = 0.0
    while meat < target:
        if t >= max_hours:
            return out
        if not wrapped and wrap_at is not None and meat >= wrap_at:
            wrapped = True
            out.wrap_h = t
        r = rate(meat, pit, p, wrapped)
        if r <= 1e-6:
            return out
        before = meat
        meat += r * STEP_H
        t += STEP_H
        if out.stall_start_h is None and before < p.stall_lo <= meat:
            out.stall_start_h = t
        if out.stall_end_h is None and before < p.stall_hi <= meat:
            out.stall_end_h = t
    out.hours = t
    return out


def calibrate_k(total_hours: float, pit: float, target: float, base: Params, wrap_at: float | None = None,
                start: float = FRIDGE_C) -> float:
    """Find k so that a cook from `start` to `target` takes `total_hours` (bisection)."""
    lo, hi = 1e-4, 10.0
    for _ in range(60):
        mid = math.sqrt(lo * hi)
        h = simulate(start, pit, target, replace(base, k=mid), wrap_at).hours
        if h > total_hours:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


@dataclass
class Sample:
    t_h: float          # hours since the meat went on
    meat: float
    pit: float


@dataclass
class Fit:
    params: Params
    k_observed: float | None
    stall_factor_observed: float | None
    data_minutes: float
    weight: float        # 0 = all prior, 1 = all data


def _slopes(samples: list[Sample], gap_h: float = 1 / 3):
    """Yield (meat_mid, pit_mid, dT/dt) from pairs of samples about `gap_h` apart, to average out noise."""
    j = 0
    for i, a in enumerate(samples):
        while j < len(samples) and samples[j].t_h - a.t_h < gap_h:
            j += 1
        if j >= len(samples):
            return
        b = samples[j]
        dt = b.t_h - a.t_h
        if dt > gap_h * 2:
            continue
        window = samples[i:j + 1]
        yield (a.meat + b.meat) / 2, sum(s.pit for s in window) / len(window), (b.meat - a.meat) / dt


def fit(samples: list[Sample], prior: Params, wrapped_since_h: float | None = None) -> Fit:
    """Blend the prior with what the readings say about k and the stall factor."""
    k_vals, f_vals = [], []
    for meat, pit, slope in _slopes(samples):
        drive = pit - meat
        if drive < 5:
            continue
        if prior.stall_lo <= meat < prior.stall_hi:
            f_vals.append((meat, pit, slope))
        elif slope > 0:
            k_vals.append(slope / drive)
    minutes = 0.0
    if samples:
        minutes = (samples[-1].t_h - samples[0].t_h) * 60
    k_obs = median(k_vals) if len(k_vals) >= 5 else None
    weight = 0.0
    k = prior.k
    if k_obs is not None:
        k_obs = min(max(k_obs, prior.k / 4), prior.k * 4)
        weight = min(1.0, len(k_vals) / 90.0)
        k = math.exp((1 - weight) * math.log(prior.k) + weight * math.log(k_obs))
    params = replace(prior, k=k)

    f_obs = None
    if len(f_vals) >= 10:
        recent = f_vals[-60:]
        f_obs = median(max(s, 0.0) / (k * (pit - meat)) for meat, pit, s in recent)
        f_obs = min(max(f_obs, 0.03), 1.0)
        w = min(1.0, len(f_vals) / 60.0)
        if wrapped_since_h is not None:
            params = replace(params, wrapped_factor=(1 - w) * prior.wrapped_factor + w * f_obs)
        else:
            params = replace(params, stall_factor=(1 - w) * prior.stall_factor + w * f_obs)
    return Fit(params, k_obs, f_obs, minutes, weight)


def recent_rate(samples: list[Sample], window_h: float = 0.5) -> float | None:
    """Meat heating rate over the last `window_h` hours (deg C per hour), or None without enough data."""
    if len(samples) < 2:
        return None
    end = samples[-1]
    start = next((s for s in samples if end.t_h - s.t_h <= window_h), None)
    if start is None or end.t_h - start.t_h < window_h * 0.6:
        return None
    return (end.meat - start.meat) / (end.t_h - start.t_h)


def remaining(samples: list[Sample], prior: Params, target: float, wrap_at: float | None,
              wrapped: bool, wrapped_since_h: float | None = None) -> tuple[Projection, Fit]:
    """Projection from the latest sample to the target, plus the fit it used."""
    f = fit(samples, prior, wrapped_since_h if wrapped else None)
    last = samples[-1]
    recent = [s.pit for s in samples if last.t_h - s.t_h <= 1 / 3] or [last.pit]
    proj = simulate(last.meat, median(recent), target, f.params, wrap_at, wrapped)
    return proj, f
