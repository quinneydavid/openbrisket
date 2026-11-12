# OpenBrisket

BBQ as code. Recipes and rubs are files in git, every cook is a log of commits, and you get a note whenever the
meat needs you. We call the method PitOps.

Smoking meat means changing things: how long it cooks, when it comes off, what goes in the rub. OpenBrisket keeps
every one of those changes with its reason, and puts the results of each version next to it, so the next change
is a decision and not a guess.

OpenBrisket reads your pit and meat temperatures and your fan, follows the cook against a recipe, and
tells you when something needs doing: the stall has started, it is time to wrap, the fire is dying, you
are running late for dinner. It does not control the fire. Your fan controller keeps doing that.

## How it maps

| Infrastructure | Barbecue |
|---|---|
| Declarative config | A recipe is a YAML file: cut, rub, pit temperature, wrap point, target, rest |
| Modules | Rubs are files that recipes reference |
| `plan` | The whole timeline, worked back from the serving time, before anything is lit |
| `apply` | Opens the cook |
| Smoke test | The check before the meat goes on. Also the CI job that validates recipes |
| Drift | The pit has left the range the recipe declares |
| Commit | Every event in a cook: meat on, stall detected, wrap, rest |
| Merge | Closing a finished cook with its score |

## A cook

```console
$ openbrisket plan recipes/texas-brisket.yaml --weight 5.4 --serve "2026-11-14 18:00"
Plan: texas-brisket v1.0.0 (beef-brisket-packer, 5.4 kg)
Pit 121 C, meat to 95 C, about 14.6 h on the pit

Fri 13 Nov
  11:57  salt / dry brine  - 12 h before the cook, uncovered in the fridge
  22:57  meat out of the fridge  - trim, apply the rub (rubs/dalmatian.yaml)
  23:12  light the fire  - bring the pit to 121 C, then run the smoke test
  23:57  meat on  - smoke test passed, probes in
Sat 14 Nov
  04:46  stall expected  - meat near 65 C; bark photo
  10:28  wrap  - at about 74 C, butcher paper
  14:32  probe tender (expected)  - meat at 95 C; feel test
  16:00  off and resting (latest)  - rest 120 min ideal, 60 min minimum
  18:00  serve

$ openbrisket apply recipes/texas-brisket.yaml --weight 5.4 --serve "2026-11-14 18:00"
$ openbrisket run            # leave this going for the whole cook
$ openbrisket smoke-test     # after lighting the fire
$ openbrisket log meat-on
$ openbrisket status
$ openbrisket log wrap
$ openbrisket log rest
$ openbrisket close --score 8.5 --cooked-weight 3.1 --notes "flat a little dry"
```

While `run` is going it leaves notes (on Telegram if you set a bot, in the terminal otherwise):

| Note | When |
|---|---|
| Pit ready | The pit has held the recipe's temperature for 15 minutes |
| Stall detected | Meat is in the stall band and rising slower than 2 °C an hour |
| Wrap point | Meat has reached the recipe's wrap temperature |
| Through the stall | Meat has left the stall band |
| Target reached | Time for the tenderness check |
| Lid open | The pit dropped 15 °C within a few minutes |
| Drift | The pit has averaged outside the recipe's range for 15 minutes |
| Fire dying | Fan at 90%+ for 10 minutes and the pit is still low |
| Behind schedule | The estimate leaves less than the minimum rest before serving |
| Probe or feed lost | No reading from the meat probe, or from the pit at all |
| Rest | Minimum and ideal rest reached |
| Hold | Meat within 4 °C of the holding floor, and again if it drops below it |

## Run it

With Docker:

```console
$ docker compose up -d
$ docker compose logs openbrisket | grep "access token"
```

Open http://localhost:8321, paste the access token once, and set where readings come from on the
Settings page. Settings and your cook log live in the `openbrisket-data` volume.

Without Docker (Python 3.10 or newer, and git):

```console
$ pip install .
$ openbrisket serve
```

The same commands the page uses are available in the terminal: `plan`, `apply`, `run`, `smoke-test`,
`log`, `status`, `history`, `close`. `openbrisket serve` follows the open cook by itself; `run` is for
using the terminal alone.

## The page

- **Cook**: the open cook as a row of stages, live temperatures, the curve with the estimate drawn
  ahead, status checks, buttons for the steps you do by hand, and the commit log. Put as many probes
  in the meat as you like: the coldest one decides when it is done.
- **Recipes**: every recipe and rub with its numbers and source file, the cooks and scores each recipe version
  produced, a History that shows exactly what changed between versions and why, and forms to write your own. What you
  save is a YAML file in your own library (`recipes/` in the data folder, a git repository), with a commit for
  every change. The built-in ones are read-only; "Customise" makes your copy.
- **History**: a year of cooks at a glance, and each past cook with its curve, events and score.
- **Settings**: readings, Telegram, and the address and token for agents.

The header has a light / dark / auto switch. The colours are GitHub's open-source Primer palette;
OpenBrisket is not affiliated with GitHub.

Everything under `/api` and `/mcp` needs the access token. Put the page behind HTTPS (a reverse proxy)
before reaching it from outside your home network.

## Agents

- **MCP**: `POST /mcp` is a Model Context Protocol server (streamable HTTP) with tools to check a cook,
  plan, open, smoke-test, log steps and close. Send the access token as a bearer token.
- **Agent skill**: [agent-skill/openbrisket](agent-skill/openbrisket/SKILL.md) teaches an agent the
  same calls over the JSON API, and how to talk about a cook.

## Readings

OpenBrisket reads a standard set of values: pit, food probes, fan output, fan on/off, pit target. In
this version they come from Home Assistant entities. [devices/](devices/README.md) has an ESP32
firmware config that reads Inkbird's ISC-027BW fan controller and IBT-26S thermometer over Bluetooth
and publishes those values, and explains how to add another device.

## Your data

- Cooks are written to a git repository on your own machine. Nothing is uploaded. If you want an
  off-site copy, add a remote to that repository yourself, and make it a private one.
- Only text is stored: events, temperatures, scores. No photos.
- Recipes are the part meant for sharing. The built-in ones live in this repository and changes come as pull
  requests. Your own stay in your library until you choose to share one.

## The estimate

The finish time comes from a small model, described in [openbrisket/model.py](openbrisket/model.py):
the meat heats in proportion to the gap between pit and meat, and much more slowly inside the stall.
It starts from the recipe's timing and shifts towards your cook's own readings as they come in. It is
a first version. It has been tested against simulated cooks, not yet against many real ones.

## Photo checks

At certain moments OpenBrisket asks to see something: the smoke before the meat goes on, where the
probe sits, the bark in the stall, a slice at the end. You send the photo to your agent (see Agents).
The agent looks at it and stores a small record in a fixed vocabulary, for example
`bark: colour mahogany, set firm, coverage 90 -> wrap`. The photo is never stored or uploaded by
OpenBrisket. Without an agent you can post the same record to `/api/check` yourself, or just follow
the note, which always says what to look for by eye.

## Not here yet

- Opening each cook as a GitHub pull request, and recipe suggestions as pull requests after a cook.
- Learning from finished cooks.

## Food safety

Recipes are checked against a minimum finishing temperature for each kind of meat, and a recipe below
it fails the smoke test. That check is a floor, not advice. You are responsible for what you serve.

## Prior art

People have been wiring computers to smokers for a long time. Thanks in particular to
[PitmasterPi](https://github.com/justindean/PitmasterPi) and
[inkbird-ble-ha](https://github.com/777Timo/inkbird-ble-ha), whose notes on the ISC-027BW protocol
the reader firmware here follows. OpenBrisket is not affiliated with Inkbird.

## Licence

MIT
