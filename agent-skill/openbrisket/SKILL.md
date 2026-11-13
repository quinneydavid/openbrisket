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
- `OPENBRISKET_TOKEN`, a token minted on the Settings page. `write` rights are right for an assistant that
  runs cooks; `read` is enough for one that only reports. With a read token the write tools are not offered,
  and a write call answers 403: tell the cook the token cannot do that rather than retrying.

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
| Recipes, rubs, stacks | `ob /api/recipes`, `ob /api/rubs`, `ob /api/stacks` |
| Save a stack | `ob /api/stacks '{"name":"kamado-lump-oak","smoker":{"type":"kamado"},"fuel":{"type":"lump-charcoal","amount_kg":4,"layout":"full basket, lit on top"},"wood":[{"species":"post oak","form":"chunks","pieces":4}],"change":"why it changed"}'` |
| Save a rub | `ob /api/rubs '{"name":"coffee-chilli","ingredients":[{"name":"ground coffee","parts":2},{"name":"kosher salt","parts":2,"kind":"salt"},{"name":"brown sugar","parts":3,"kind":"sugar"}]}'` |
| Save a recipe | `ob /api/recipes '{"name":"beef-short-ribs","cut":"beef-short-ribs","rub":"coffee-chilli","pit_target_c":135,"meat_target_c":96,"hours_fixed":8,"wrap_at_c":77,"change":"why it changed"}'` |
| Plan without starting | `ob /api/plan '{"recipe":"texas-brisket","weight_kg":5.4,"serve_at":"2026-11-14 18:00"}'` |
| Open a cook | `ob /api/cooks '{"recipe":"texas-brisket","weight_kg":5.4,"serve_at":"2026-11-14 18:00","probes":[1,2],"probe_labels":{"1":"flat","2":"point"},"stack":"kamado-lump-oak"}'` |
| Smoke test | `ob /api/smoke-test '{}'` |
| Log a step | `ob /api/log '{"step":"meat-on"}'` (steps: `meat-on`, `wrap`, `spritz`, `hotfix`, `wood`, `rest`, `note`) |
| Store a photo check | `ob /api/check '{"check":"bark","data":{"colour":"mahogany","set":"firm","coverage_pct":90},"verdict":"wrap","by":"<your model>"}'` |
| Check vocabulary | `ob /api/checks` |
| Close | `ob /api/close '{"score":8.5,"cooked_weight_kg":3.1,"fuel_used_kg":3.5,"notes":"flat a little dry"}'` |
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
- `hold`: once the meat is off and resting. How long it has been holding, the meat temperature, the cooling
  rate, and `hours_until_floor`, the time left before it reaches the holding floor (60 °C unless the recipe says
  otherwise). A long hold is normal for brisket; only worry the cook when it nears the floor.
- `asks`: photos OpenBrisket is waiting to be shown (see Photos). Mention them when you report on the cook.
- `checks`: each is pass, fail or pending with a one-line detail. Lead with any that fail.

## Recipes and rubs

The cook can dictate a recipe or a rub and you save it (`save_rub`, `save_recipe` over MCP). They are stored as
YAML files with a commit per save.

- Say what kind of cook it is with `style`: `low-and-slow` (brisket, shoulder), `ribs`, `poultry`, `roast`,
  `fish`, `sausage`, or `quick` (wings, vegetables; runs by the clock with no probe). The style sets the defaults
  for the stall, how it is judged done, whether a probe is needed and the rest; only pass `done_by`, `probe` or
  `stall` to differ from them. Use `stages` for timed steps such as ribs 3-2-1, `finish` for a sear, crisp or
  glaze at the end, and `pull_c` for a roast taken off a few degrees early.
- Meat, poultry and fish always need `meat_target_c`, even when the clock ends the cook: it is the check before
  serving. Vegetables, cheese and nuts do not.
- Save the rub before the recipe that uses it.
- Ingredients are in parts, as a ratio. Mark salt and sugar with `kind`, nothing else.
- A recipe needs a name, a cut that starts with the meat (`beef-`, `pork-`, `lamb-`, `chicken-`, `turkey-`,
  `duck-`, `fish-`, `sausage-`), a pit temperature, a meat target, and exactly one of `hours_per_kg` or
  `hours_fixed`. Ask for whatever is missing; do not invent timings.
- Before suggesting a change, look at what has been tried: `recipe_history` (or `ob /api/history/recipe/<id>`)
  lists every version with the reason and the fields that changed, and each recipe's `results` in the recipe list
  groups its finished cooks and scores by version. Say what the last change was and whether the scores moved.
- When changing an existing recipe, pass every field again (read it first with `list_recipes`) and put the
  reason in `change`. The version number moves on by itself.
- Pass on any warning in the reply, such as a sugary rub on a hot pit.
- A save that breaks the food-safety floor is refused. Do not work around it.

## Stacks

A stack is what the fire is built from: smoker, fuel (kind, brand, kg, how it is laid and lit), smoking wood,
water pan, vents. It is a file with versions, like a recipe (`list_stacks`, `save_stack`).

- When opening a cook, pass `stack`. If the cook does not name one, use the one marked `last_used` and say which
  it is; ask only if anything is different today (other charcoal, other wood). If something is different, save
  the stack with that change first, with the reason in `change`, then open the cook.
- When the cook says they added charcoal or wood, log it: `log_step` with step `hotfix` (fuel) or `wood` and how much in
  the note.
- At the end ask roughly how much fuel was burnt and pass it as `fuel_used_kg` to `close_cook`. Skip it if they
  do not know.
- Before suggesting a change to the fire, read the stack's `results`: finished cooks per stack version with
  score, how much the pit varied (`avg_pit_stdev_c`) the burn rate (`avg_burn_rate_kg_h`, kg of fuel an hour) and `hotfixes`. `recipe_history` with kind `stack` says
  what changed between versions and why. Suggest one change at a time.
- When changing a stack, pass every field again and put the reason in `change`.

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
- Probes come and go: `probes[].in_meat` is false for one that has been pulled out. OpenBrisket notices a pulled
  probe by its sudden fall; use `set_probe` only when the cook tells you they pulled or re-inserted one.
- Only log a step the cook says they have done. Never log `meat-on`, `wrap` or `rest` on a guess: they
  change how the cook is followed.
- The stall (meat sitting around 65 to 77 °C for hours) is normal for big cuts. Do not treat it as a problem.
- `cook.style` and `cook.done_by` say what kind of cook this is. A `quick` cook has no probe: report the time
  left, not a temperature. For `bend` and `time` the meat temperature is only a guide.
- Tenderness is judged by hand. When the target temperature is reached, remind the cook to probe the
  meat; the temperature alone does not mean it is done.
- A weight is needed for recipes sized by weight. Ask for it, and for the serving time, before planning.
- Food safety: do not suggest finishing temperatures below the recipe's target.
