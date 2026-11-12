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
| Open a cook | `ob /api/cooks '{"recipe":"texas-brisket","weight_kg":5.4,"serve_at":"2026-11-14 18:00","probes":[1,2],"probe_labels":{"1":"flat","2":"point"}}'` |
| Smoke test | `ob /api/smoke-test '{}'` |
| Log a step | `ob /api/log '{"step":"meat-on"}'` (steps: `meat-on`, `wrap`, `spritz`, `rest`, `note`) |
| Store a photo check | `ob /api/check '{"check":"bark","data":{"colour":"mahogany","set":"firm","coverage_pct":90},"verdict":"wrap","by":"<your model>"}'` |
| Check vocabulary | `ob /api/checks` |
| Close | `ob /api/close '{"score":8.5,"cooked_weight_kg":3.1,"notes":"flat a little dry"}'` |
| Past cooks | `ob /api/cooks`, one cook: `ob /api/cooks/<id>` |

Errors come back as `{"error": "..."}` with a plain sentence; pass it on.

## Reading the status

`cook` is null when nothing is open. Otherwise:

- `phase`: preheating, cooking, stall, resting.
- `meat_c`, and `reading.pit_c`, `reading.fan_pct`: temperatures in degrees Celsius. With several probes in
  the meat, `meat_c` is the coldest and `probes` lists each one by name; the coldest decides when it is done.
- `estimate.off_at`: when the meat should come off; `estimate.rest_available_min`: minutes between that
  and serving. `estimate.data_weight` near 0 means the estimate is still mostly the recipe's timing.
- `pipeline`: the stages with done / active / pending / failed.
- `asks`: photos OpenBrisket is waiting to be shown (see Photos). Mention them when you report on the cook.
- `checks`: each is pass, fail or pending with a one-line detail. Lead with any that fail.

## Photos

When the cook sends you a photo of the meat, the smoker or the smoke, look at it and store what you see with
`record_check` (MCP) or `/api/check`. Only the data points are stored; never upload or forward the photo.
`cook.asks` in the status lists the photos OpenBrisket is currently waiting for.

| Check | When | What to look for | Verdict |
|---|---|---|---|
| `raw` | Before the cook | Fat cap thickness in mm, marbling (low/medium/high), thickness of the thickest part in cm, whether it needs trimming | `ok` or `trim` |
| `smoke` | During the smoke test | Smoke colour: `thin-blue` or `clear` is right; `white`, `grey` or `black` means the fire is not burning cleanly | `ok` or `wait` |
| `probe` | Right after the meat goes on | Is the probe tip in the centre of the thickest part: `ok`, `shallow`, `in-fat`, `near-bone`, `through` | `ok` or `move` |
| `bark` | In the stall, and before wrapping | Colour (`pale` to `black`), whether it looks set (`soft`, `tacky`, `firm`), how much of the surface is covered, cracking | `wait`, `spritz`, `wrap` or `ok` |
| `slice` | After the rest | Smoke ring depth in mm, moisture (`dry`/`ok`/`juicy`), bend, how well the fat rendered | `ok`, `under` or `over` |

Rules for judging:

- Give only the fields you can actually see in the photo. Leave out anything you would be guessing.
- A bark verdict of `wrap` needs a dark colour AND a set surface. Colour alone is not enough: say so and ask the
  cook to rub it lightly with a finger if you cannot tell from the picture.
- If the photo is too dark, blurred or shows the wrong thing, ask for another one. Do not store a guess.
- After storing, tell the cook the verdict in one sentence and why.
- You cannot judge tenderness from a photo. That is always the probe and the hand.

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
