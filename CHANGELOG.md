# Changelog

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
