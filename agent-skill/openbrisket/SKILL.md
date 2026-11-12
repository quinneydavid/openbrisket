---
name: openbrisket
description: Follow and run a barbecue cook with OpenBrisket - plan a cook back from the serving time, open it, run the pre-cook smoke test, report how the cook is going and when it will be done, log steps (meat on, wrap, rest) and close it with a score. Use when the user asks about their smoker, brisket, ribs or pulled pork, a cook in progress, or planning a barbecue.
---

# OpenBrisket

OpenBrisket follows a barbecue cook against a recipe. It reads the pit temperature, the meat probes and
the fan, and works out where the cook stands. It does not control the fire: the cook or their fan
controller does that. Your job is to be the cook's assistant: answer "how is it going", plan ahead, and
record what they tell you they did.

## Connection

Two settings, both from the OpenBrisket Settings page ("Agents"):

- `OPENBRISKET_URL`, e.g. `http://openbrisket.local:8321`
- `OPENBRISKET_TOKEN`, the access token

If your agent supports MCP, connect to `$OPENBRISKET_URL/mcp` (streamable HTTP) with the header
`Authorization: Bearer $OPENBRISKET_TOKEN` and use the tools there; they mirror the calls below.
Otherwise use the JSON API with curl:

```bash
ob() { curl -sS -H "Authorization: Bearer $OPENBRISKET_TOKEN" -H "Content-Type: application/json" "$OPENBRISKET_URL$1" ${2:+-d "$2"}; }
```

## Calls

| What | Call |
|---|---|
| Where the cook stands | `ob /api/status` |
| Recipes | `ob /api/recipes` |
| Plan without starting | `ob /api/plan '{"recipe":"texas-brisket","weight_kg":5.4,"serve_at":"2026-11-14 18:00"}'` |
| Open a cook | `ob /api/cooks '{"recipe":"texas-brisket","weight_kg":5.4,"serve_at":"2026-11-14 18:00","probe":1}'` |
| Smoke test | `ob /api/smoke-test '{}'` |
| Log a step | `ob /api/log '{"step":"meat-on"}'` (steps: `meat-on`, `wrap`, `spritz`, `rest`, `note`) |
| Close | `ob /api/close '{"score":8.5,"cooked_weight_kg":3.1,"notes":"flat a little dry"}'` |
| Past cooks | `ob /api/cooks`, one cook: `ob /api/cooks/<id>` |

Errors come back as `{"error": "..."}` with a plain sentence; pass it on.

## Reading the status

`cook` is null when nothing is open. Otherwise:

- `phase`: preheating, cooking, stall, resting.
- `meat_c`, and `reading.pit_c`, `reading.fan_pct`: temperatures in degrees Celsius.
- `estimate.off_at`: when the meat should come off; `estimate.rest_available_min`: minutes between that
  and serving. `estimate.data_weight` near 0 means the estimate is still mostly the recipe's timing.
- `pipeline`: the stages with done / active / pending / failed.
- `checks`: each is pass, fail or pending with a one-line detail. Lead with any that fail.

## How to behave

- Answer "how's the brisket?" in two or three sentences: phase, meat temperature against target, when
  it comes off, and anything failing. Do not read out every field.
- Only log a step the cook says they have done. Never log `meat-on`, `wrap` or `rest` on a guess: they
  change how the cook is followed.
- The stall (meat sitting around 65 to 77 °C for hours) is normal. Do not treat it as a problem.
- Tenderness is judged by hand. When the target temperature is reached, remind the cook to probe the
  meat; the temperature alone does not mean it is done.
- A weight is needed for recipes sized by weight. Ask for it, and for the serving time, before planning.
- Food safety: do not suggest finishing temperatures below the recipe's target.
