# Changelog

## 0.8.1

- The tagline is written "BBQ as Code".
- A browser-tab icon.

## 0.8.0

- Recipes for every kind of food, not only big cuts. A recipe has a `style` (low-and-slow, ribs, poultry, roast,
  fish, sausage, quick) that sets what applies: whether there is a stall to watch, whether it is done by feel, by
  temperature, by the bend test or by the clock, whether a probe is needed, and how long it rests. Each can be
  overridden.
- Timed stages (`stages`), for example ribs 3-2-1, with a note at each change.
- A finish step (`finish`): sear, crisp, glaze or sauce, started by a meat temperature or timed from the end.
- A pull temperature (`meat.pull_c`) for roasts that carry over while they rest.
- Cooks with no probe in the food run by the clock: no probe alarms, a countdown, and a reminder to check the
  temperature before serving where the food has a safety floor.
- `rest: none` and `stall: none`. No false "stall detected" on a chicken.
- Six more built-in recipes: whole chicken, chicken wings, ribs 3-2-1, tri-tip (reverse sear), hot-smoked salmon,
  sausages. Cuts can now be vegetables, cheese or nuts, which have no meat target.
- The recipe form is arranged by these parts, and choosing a style fills in its defaults.
- Recipes written before this keep working unchanged: with no style they are low-and-slow.

## 0.7.1

- An API tab: every endpoint with its description, an editable example body and a Send button, drawn from an
  OpenAPI 3 document served at `/api/openapi.json` (which also opens in Swagger and similar tools).
- Probes say which physical sensor they are. Give each probe a name in Settings ("Fan controller 1",
  "Thermometer 3"); it is shown in the probe picker, beside each probe on the cook page, and kept with the cook.

## 0.7.0

- Change history you can read: History on a recipe or rub lists every version with the reason and exactly which
  fields changed, before and after ("Pit temperature: 135 -> 125", "brown sugar: 3 parts -> 1.5 parts").
- Results per recipe version: each recipe shows its finished cooks grouped by version, with the average and best
  score and links to the cooks, so you can see whether a change helped.
- Rubs have version numbers and a "what changed and why" line, like recipes. A cook records the exact rub it used.
- New MCP tool `recipe_history`.
- Fix: saving a recipe change without a version number (as an agent does) restarted the numbering at 1.0.0.
- The tagline is now "BBQ as code".

## 0.6.1

- Probes can come and go. A probe that falls 6 C or more inside three minutes is taken as pulled out and stops
  counting, with a note; if it goes back in and returns to about where it was, it counts again. The coldest probe
  still in the meat decides everything, so pulling all but one for the hold just works.
- An unplugged probe is simply not counted, and is no longer a failed check during the hold.
- "out" / "back in" buttons beside each probe on the cook page, and a `set_probe` MCP tool, for saying so by hand.

## 0.6.0

- The hold is followed. After "Off, resting / holding" the app keeps recording every probe, and shows how long the
  meat has been holding, its temperature, how fast it is cooling and how long until it reaches the holding floor.
- A holding floor per recipe (`rest.hold_min_c`, default 60 C, never below 57): one note when the meat gets within
  4 C of it, and an alert below it.
- It works with only the meat probes reading, so the fan controller can be switched off during the hold.
- The hold is drawn on the chart and summarised in the cook's analysis (length, start and end temperature, cooling
  rate, any time below the floor).

## 0.5.2

- Rain as a third optional weather sensor, logged with every reading. It takes a number (mm, mm/h) or a wet/dry
  sensor. The cook's analysis says how long it rained.

## 0.5.1

- More is logged with every cook: outdoor temperature and humidity (two optional sensors in Settings) and the
  finish estimate at every minute.
- When a cook closes, an analysis is written into its record: time on the pit against the recipe, stall start and
  length, when it was wrapped, how steady the pit was, how fast the meat really heated compared with the recipe,
  and how far out the estimate was a quarter, half and three quarters of the way through. Shown on the cook's page.

## 0.5.0

- Write your own recipes and rubs on the Recipes tab: forms for the numbers and the ingredients, saved as YAML
  files in your own library with a commit for every change. A changed recipe gets the next version number.
- "Customise" a built-in recipe or rub to make your own copy, which then takes its place. Built-ins stay read-only.
- Rubs are listed with their ingredients, shares, salt and sugar content, and which recipes use them.
- Warnings that do not block a save: a sugary rub on a pit above 135 C, and a salty rub on top of a long dry brine.
- New MCP tools `list_rubs`, `save_rub`, `save_recipe`, and API routes for the same.

## 0.4.0

- Photo checks as data: raw meat, probe placement, smoke, bark and slice, each in a fixed vocabulary. An agent
  looks at the photo and stores the record through the new `record_check` MCP tool or `/api/check`; the photo is
  never stored. The page lists what the cook is waiting to be shown and what it has been shown.
- A smoke check in the last 30 minutes now decides the `clean-smoke` item of the smoke test.
- Notes say what to photograph, and still say what to look for by eye.

## 0.3.0

- Several probes in the meat: tick them when opening a cook and name them (flat, point). The coldest one decides
  stall, wrap point, target and the estimate; every probe is drawn, listed and recorded.
- Up to nine food probe entities in Settings, so a second thermometer's probes can be used.
- Light, dark or auto theme, chosen in the header and remembered per browser. Light is the default.
- The page now uses GitHub's open-source Primer colours.
- Fixed: the Recipes tab showed "[object HTMLElement]" and the start page showed a stray "null".

## 0.2.0

- `openbrisket serve`: web page (cook, recipes, history, settings), JSON API and background follower in one process.
- MCP server at `/mcp` with eight tools.
- Agent skill for agents without MCP.
- Settings are edited on the page; secrets are never sent back to the browser.
- Access token for the page, the API and MCP.
- Dockerfile and compose file.

## 0.1.0

First version.

- Recipes and rubs as YAML, with validation and a food-safety floor per kind of meat.
- `plan`: the timeline worked back from the serving time.
- `apply`, `run`, `log`, `status`, `history`, `close`: follow a cook and keep its log in a local git repository.
- `smoke-test`: pre-cook check, and `validate` for CI.
- Notes for stall, wrap point, target, lid open, drift, dying fire, schedule, lost probe, rest.
- Finish-time estimate that starts from the recipe and learns from the cook's readings.
- Readings from Home Assistant; ESPHome reader for the Inkbird ISC-027BW and IBT-26S.
- Three recipes: Texas brisket, pulled pork, pork ribs.
