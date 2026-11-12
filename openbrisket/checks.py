"""Photo checks as data. A photo is looked at by a person or by an agent's vision model; what gets
stored is a small record in a fixed vocabulary, never the picture. The fixed words are what make one
cook's "mahogany, firm" comparable with the next."""
from __future__ import annotations

VOCAB: dict[str, dict] = {
    "raw": {
        "title": "Raw meat",
        "fields": {
            "fat_cap_mm": ("number", 0, 60),
            "thickness_cm": ("number", 0, 40),
            "marbling": ("choice", ["low", "medium", "high"]),
            "trim": ("choice", ["ok", "needed"]),
        },
        "verdicts": ["ok", "trim"],
    },
    "probe": {
        "title": "Probe placement",
        "fields": {"placement": ("choice", ["ok", "shallow", "in-fat", "near-bone", "through"])},
        "verdicts": ["ok", "move"],
    },
    "smoke": {
        "title": "Smoke",
        "fields": {"colour": ("choice", ["thin-blue", "clear", "white", "grey", "black"])},
        "verdicts": ["ok", "wait"],
    },
    "bark": {
        "title": "Bark",
        "fields": {
            "colour": ("choice", ["pale", "tan", "mahogany", "dark", "black"]),
            "set": ("choice", ["soft", "tacky", "firm"]),
            "coverage_pct": ("number", 0, 100),
            "cracking": ("choice", ["none", "light", "heavy"]),
        },
        "verdicts": ["wait", "spritz", "wrap", "ok"],
    },
    "slice": {
        "title": "Sliced",
        "fields": {
            "smoke_ring_mm": ("number", 0, 30),
            "moisture": ("choice", ["dry", "ok", "juicy"]),
            "bend": ("choice", ["stiff", "ok", "falls-apart"]),
            "fat_render": ("choice", ["unrendered", "partly", "rendered"]),
        },
        "verdicts": ["ok", "under", "over"],
    },
}


class CheckError(Exception):
    pass


def validate(kind: str, data: dict | None, verdict: str | None) -> dict:
    """Return the cleaned data for a check, or raise CheckError naming exactly what is wrong."""
    spec = VOCAB.get(kind)
    if spec is None:
        raise CheckError(f"unknown check '{kind}'; use one of: {', '.join(VOCAB)}")
    if not isinstance(data, dict) or not data:
        raise CheckError(f"a {kind} check needs data: {describe(kind)}")
    clean: dict = {}
    for key, value in data.items():
        field = spec["fields"].get(key)
        if field is None:
            raise CheckError(f"'{key}' is not a field of the {kind} check; fields: {describe(kind)}")
        if field[0] == "choice":
            if value not in field[1]:
                raise CheckError(f"{kind}.{key} must be one of: {', '.join(field[1])}")
            clean[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not field[1] <= value <= field[2]:
                raise CheckError(f"{kind}.{key} must be a number from {field[1]} to {field[2]}")
            clean[key] = round(float(value), 1)
    if verdict is not None and verdict not in spec["verdicts"]:
        raise CheckError(f"verdict for a {kind} check must be one of: {', '.join(spec['verdicts'])}")
    return clean


def describe(kind: str) -> str:
    parts = []
    for key, field in VOCAB[kind]["fields"].items():
        parts.append(f"{key} ({'/'.join(field[1])})" if field[0] == "choice" else f"{key} ({field[1]}-{field[2]})")
    return ", ".join(parts)


def summary(kind: str, data: dict, verdict: str | None) -> str:
    text = ", ".join(f"{k.replace('_', ' ')} {v:g}" if isinstance(v, float) else f"{k.replace('_', ' ')} {v}"
                     for k, v in data.items())
    return f"{VOCAB[kind]['title'].lower()} check: {text}" + (f" -> {verdict}" if verdict else "")
