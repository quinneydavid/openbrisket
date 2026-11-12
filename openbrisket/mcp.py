"""A small MCP server (Model Context Protocol, streamable HTTP, JSON responses) so an agent can follow
and run a cook. One POST to /mcp carries one JSON-RPC message; there are no server-initiated streams.

The tools are thin wrappers over `Service`, the same calls the web page makes.
"""
from __future__ import annotations

import json

from . import __version__
from .service import STEPS, Service, ServiceError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "OpenBrisket follows a barbecue cook against a recipe. It reads the pit and meat temperatures; it does "
    "not control the fire. Typical order: list_recipes, plan_cook, start_cook, smoke_test once the pit is "
    "steady, log_step meat-on, cook_status whenever asked, log_step wrap / rest when the cook does them, "
    "close_cook with the score. Temperatures are in degrees Celsius. Times are local to the server unless "
    "they carry an offset.")


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


RECIPE = {"type": "string", "description": "Recipe id from list_recipes, e.g. texas-brisket"}
WEIGHT = {"type": "number", "description": "Raw weight in kg. Needed for recipes sized by weight."}
SERVE = {"type": "string", "description": "Serving time, e.g. 2026-11-14 18:00"}

TOOLS = [
    {"name": "cook_status",
     "description": "Where the open cook stands: phase, temperatures, pipeline stages, status checks and the "
                    "estimated time it comes off the pit. Also works with no cook open (current pit reading).",
     "inputSchema": _obj({})},
    {"name": "list_recipes",
     "description": "The recipes available, with pit and meat targets, timing, wrap point and rub.",
     "inputSchema": _obj({})},
    {"name": "plan_cook",
     "description": "The full timeline for a cook, worked back from the serving time, without starting anything.",
     "inputSchema": _obj({"recipe": RECIPE, "weight_kg": WEIGHT, "serve_at": SERVE}, ["recipe", "serve_at"])},
    {"name": "start_cook",
     "description": "Open a cook from a recipe. Only one cook can be open at a time.",
     "inputSchema": _obj({"recipe": RECIPE, "weight_kg": WEIGHT, "serve_at": SERVE,
                          "probes": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 9},
                                     "description": "Which food probes are in the meat (default [1]). With several, "
                                                    "the coldest one decides when the meat is done."},
                          "probe_labels": {"type": "object", "additionalProperties": {"type": "string"},
                                           "description": "Optional names by probe number, e.g. {\"1\": \"flat\", "
                                                          "\"2\": \"point\"}"}},
                         ["recipe", "serve_at"])},
    {"name": "smoke_test",
     "description": "The pre-cook check: data feed, pit steady at the recipe's temperature for 15 minutes, fan "
                    "headroom, food probe, notifications. Run it after the fire is lit and before the meat goes on.",
     "inputSchema": _obj({})},
    {"name": "log_step",
     "description": "Record something the cook did by hand. meat-on starts the clock; wrap and rest change how the "
                    "cook is followed; spritz and note are just logged.",
     "inputSchema": _obj({"step": {"type": "string", "enum": list(STEPS)},
                          "note": {"type": "string", "description": "Optional detail; required for step=note"}},
                         ["step"])},
    {"name": "close_cook",
     "description": "Close the open cook with its result. abandon=true closes a cook that went wrong without a score.",
     "inputSchema": _obj({"score": {"type": "number", "description": "0 to 10"},
                          "cooked_weight_kg": {"type": "number"}, "notes": {"type": "string"},
                          "abandon": {"type": "boolean"}})},
    {"name": "cook_history",
     "description": "Past cooks with recipe, weight and result. Give a cook id to get that cook's events instead.",
     "inputSchema": _obj({"cook": {"type": "string", "description": "A cook id, e.g. 2026-11-14-texas-brisket"}})},
]


def _call(service: Service, name: str, a: dict):
    if name == "cook_status":
        s = service.status()
        return {"summary": service.status_text(), **s}
    if name == "list_recipes":
        return [{k: v for k, v in r.items() if k != "source"} for r in service.list_recipes()]
    if name == "plan_cook":
        return service.plan(a.get("recipe"), a.get("weight_kg"), a.get("serve_at"))
    if name == "start_cook":
        return service.start(a.get("recipe"), a.get("weight_kg"), a.get("serve_at"), a.get("probes") or a.get("probe"),
                             a.get("probe_labels"))
    if name == "smoke_test":
        return service.smoke_test()
    if name == "log_step":
        return service.log_step(a.get("step", ""), a.get("note", ""))
    if name == "close_cook":
        return service.close(a.get("score"), a.get("cooked_weight_kg"), a.get("notes", ""), bool(a.get("abandon")))
    if name == "cook_history":
        if a.get("cook"):
            c = service.cook(a["cook"])
            return {"meta": c["meta"], "events": c["events"]}
        return service.cooks()
    raise KeyError(name)


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(service: Service, msg: dict) -> dict | None:
    """Answer one JSON-RPC message. Returns None for notifications (the HTTP layer replies 202)."""
    method, msg_id = msg.get("method"), msg.get("id")
    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(msg_id, -32600, "not a JSON-RPC 2.0 request")
    if "id" not in msg:
        return None
    params = msg.get("params") or {}

    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version, "capabilities": {"tools": {}},
            "serverInfo": {"name": "openbrisket", "version": __version__}, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS}:
            return _error(msg_id, -32602, f"unknown tool: {name}")
        try:
            result = _call(service, name, args)
            text, is_error = json.dumps(result, indent=1, default=str), False
        except ServiceError as e:
            text, is_error = str(e), True
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}
    return _error(msg_id, -32601, f"method not found: {method}")
