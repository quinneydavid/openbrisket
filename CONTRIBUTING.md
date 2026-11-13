# Contributing

Recipes, rubs and stacks are the part of OpenBrisket meant for sharing, and they are the easiest thing to
contribute: one YAML file, one pull request.

## A recipe, rub or stack

1. Add the file under `recipes/`, `recipes/rubs/` or `recipes/stacks/`. Copy an existing one to start.
2. Check it: `python -m openbrisket validate recipes`. This is the same check the `smoke-test` job runs, and it
   includes the food-safety floor for each kind of meat.
3. In the pull request, say how many times you have cooked it and how it went. A recipe that has been cooked
   beats one that looks right.

A recipe must not depend on a particular smoker. What you burn and what you cook on belongs in a stack.

## Code

- Python 3.10 or newer. The only dependency is PyYAML; please keep it that way. The page has no build step
  and no framework.
- Run the tests: `python -m unittest discover -s tests -t .`
- Every route the server answers is listed in `openbrisket/apidoc.py`; a test fails if they drift apart.
- The page never inserts data as HTML. Use the `h()` helper.

## A new thermometer or controller

See [devices/README.md](devices/README.md). One ESPHome file that publishes the standard values is enough.
The firmware in this repository is read-only by design: it never writes to a device.

## Not accepted

- Anything that stores or uploads photos. Photos are turned into data points and only the data is kept.
- Anything that controls the fire. OpenBrisket reads and advises.
