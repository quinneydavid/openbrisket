"""The HTTP API, described once. `spec()` returns an OpenAPI 3 document (served at /api/openapi.json, usable
with Swagger and similar tools), and the API tab of the page is drawn from the same list. A test checks that
every route the server answers is listed here."""
from __future__ import annotations

from . import __version__

# (method, path, group, summary, example body or None)
ENDPOINTS = [
    ("GET", "/api/status", "Cook", "Where the open cook stands: phase, temperatures, pipeline, checks, estimate, hold.", None),
    ("POST", "/api/plan", "Cook", "The timeline for a cook, worked back from the serving time. Starts nothing.",
     {"recipe": "texas-brisket", "weight_kg": 5.4, "serve_at": "2026-11-14 18:00"}),
    ("POST", "/api/cooks", "Cook", "Open a cook from a recipe. Only one cook can be open at a time.",
     {"recipe": "texas-brisket", "weight_kg": 5.4, "serve_at": "2026-11-14 18:00", "probes": [1, 2],
      "probe_labels": {"1": "flat", "2": "point"}}),
    ("POST", "/api/smoke-test", "Cook", "Run the pre-cook check: feed, pit steady, fan, probes, smoke, notes.", {}),
    ("POST", "/api/log", "Cook", "Record a step done by hand: meat-on, wrap, spritz, rest, note.",
     {"step": "note", "note": "added two chunks of post oak"}),
    ("POST", "/api/probe", "Cook", "Say a probe has been pulled out of the meat, or put back.",
     {"probe": 2, "in_meat": False}),
    ("POST", "/api/check", "Cook", "Store what a photo showed, as data points in the fixed vocabulary.",
     {"check": "bark", "data": {"colour": "mahogany", "set": "firm", "coverage_pct": 90}, "verdict": "wrap"}),
    ("GET", "/api/checks", "Cook", "The vocabulary for photo checks: kinds, fields, allowed values, verdicts.", None),
    ("POST", "/api/close", "Cook", "Close the open cook with its result, or abandon it.",
     {"score": 8.5, "cooked_weight_kg": 3.1, "notes": "flat a little dry"}),
    ("GET", "/api/cooks", "History", "Every cook, newest first, with its result.", None),
    ("GET", "/api/cooks/{id}", "History", "One cook: its record, analysis, events and temperature curve.", None),
    ("GET", "/api/recipes", "Recipes", "Every recipe with its numbers, rub, warnings, source and results per version.", None),
    ("POST", "/api/recipes", "Recipes", "Add or change one of your own recipes. A changed recipe gets the next version.",
     {"name": "beef-short-ribs", "cut": "beef-short-ribs", "rub": "dalmatian", "pit_target_c": 125,
      "meat_target_c": 96, "hours_fixed": 8, "wrap_at_c": 77, "change": "why it changed"}),
    ("GET", "/api/styles", "Recipes", "The kinds of cook a recipe can be (low-and-slow, ribs, poultry, roast, fish, sausage, quick) and the defaults each sets.", None),
    ("GET", "/api/rubs", "Recipes", "Every rub with ingredients, salt and sugar share, and the recipes using it.", None),
    ("POST", "/api/rubs", "Recipes", "Add or change one of your own rubs.",
     {"name": "coffee-chilli", "ingredients": [{"name": "ground coffee", "parts": 2},
                                               {"name": "kosher salt", "parts": 2, "kind": "salt"}]}),
    ("GET", "/api/history/{kind}/{id}", "Recipes", "Every saved version of a recipe or rub with what changed. kind is recipe or rub.", None),
    ("POST", "/api/delete", "Recipes", "Delete one of your own recipes or rubs (it stays in the file history).",
     {"kind": "rub", "id": "coffee-chilli"}),
    ("GET", "/api/settings", "Settings", "Current settings. Secrets are never returned, only whether they are set.", None),
    ("POST", "/api/settings", "Settings", "Change settings. Leave a secret out to keep the saved one.",
     {"poll_seconds": 15}),
    ("POST", "/api/settings/test-source", "Settings", "Take one reading with the saved settings.", {}),
    ("POST", "/api/settings/test-notify", "Settings", "Send one test note.", {}),
    ("POST", "/mcp", "Agents", "Model Context Protocol endpoint (JSON-RPC 2.0): the same actions as tools for an agent.",
     {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    ("GET", "/api/openapi.json", "Agents", "This API as an OpenAPI 3 document. No token needed.", None),
    ("GET", "/healthz", "Agents", "Liveness check. No token needed.", None),
]
PUBLIC = {"/healthz", "/api/openapi.json"}


def spec() -> dict:
    paths: dict = {}
    for method, path, group, summary, example in ENDPOINTS:
        op: dict = {"summary": summary, "tags": [group],
                    "responses": {"200": {"description": "OK", "content": {"application/json": {}}},
                                  "400": {"description": "Refused, with {\"error\": \"why\"}"}}}
        if path not in PUBLIC:
            op["security"] = [{"token": []}]
            op["responses"]["401"] = {"description": "Access token missing or wrong"}
        params = [seg[1:-1] for seg in path.split("/") if seg.startswith("{")]
        if params:
            op["parameters"] = [{"name": p, "in": "path", "required": True, "schema": {"type": "string"}} for p in params]
        if method == "POST":
            op["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": {"type": "object"}, "example": example if example is not None else {}}}}
        paths.setdefault(path, {})[method.lower()] = op
    return {
        "openapi": "3.0.3",
        "info": {"title": "OpenBrisket", "version": __version__,
                 "description": "BBQ as code. Follow a barbecue cook, keep recipes and rubs as files with history. "
                                "Temperatures are in degrees Celsius."},
        "components": {"securitySchemes": {"token": {"type": "http", "scheme": "bearer",
                                                     "description": "The access token from the Settings page"}}},
        "paths": paths,
    }
