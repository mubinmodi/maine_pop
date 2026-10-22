# Maine synthetic population

A parcel-level synthetic population for Portland, Maine, built only from public Census and State of Maine data, with an interactive 3D map. Every value is a **synthetic estimate**, not a record of real people.

- Architecture: [docs/architecture.md](docs/architecture.md)
- Module design: [docs/design.md](docs/design.md)
- Terms: [GLOSSARY.md](GLOSSARY.md)

## Setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.12+. No API keys are needed.

```bash
uv sync
uv run synthpop fetch                 # download, clip and cache every input into data/
uv run pytest                         # tests run on a built-in fixture town, offline
```

`synthpop fetch --only parcels e911` refetches selected inputs. Delete files in `data/interim/` to force a refetch.

## Adding a variable

Add an entry to [config/variables.yaml](config/variables.yaml):
- a **seed** variable recoded from a PUMS column, or
- a **derived** variable built from other variables' categories.

Then rebuild. The variable appears in the API and the map without code changes.
