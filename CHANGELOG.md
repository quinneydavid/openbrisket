# Changelog

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
