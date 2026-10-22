# Maine synthetic population

A parcel-level synthetic population for Portland, Maine, built only from public Census and State of Maine data, with an interactive 3D map. Every value is a **synthetic estimate**, not a record of real people.

- Architecture: [docs/architecture.md](docs/architecture.md)
- Module design: [docs/design.md](docs/design.md)
- Terms: [GLOSSARY.md](GLOSSARY.md)

## Setup

You need [uv](https://docs.astral.sh/uv/), Python 3.12+ and Node 20+. No API keys are needed.

```bash
uv sync
uv run synthpop fetch        # download, clip and cache every input into data/ (about 2 minutes)
uv run synthpop build        # model runs, area stats, docs/validation.md, published files (about 3 minutes)
(cd app && npm install && npm run build)
uv run synthpop serve        # API and map at http://localhost:8000
uv run pytest                # tests run on a built-in fixture town, offline
```

The build steps can also run one at a time: `synthpop run`, `summarize`, `validate`, `publish`. Delete files in `data/interim/` to refetch an input.

**Front-end development:** run `uv run synthpop serve`, then `cd app && npm run dev` and open http://localhost:5173. Vite forwards `/api` and `/geo` to port 8000.

## Deploy

The app is one process: FastAPI serves the API, the geometry files and the built front end, and reads only `data/out/` (about 30 MB for Portland). After building the data:

```bash
docker build -t maine-synthpop .
docker run -p 8000:8000 maine-synthpop
```

Any host that runs a container works (Fly.io, Render, Cloud Run, a small VM). Put it behind HTTPS. The API is read-only, so it can scale horizontally with no shared state. The Dockerfile has not been run yet, because Docker wasn't running on the build machine.

## What's where

| Path | What it is |
|---|---|
| `config/variables.yaml` | The variable schema: variables, categories, ACS constraints |
| `config/study_area.yaml` | Study area, data vintages, model settings |
| `synthpop/sources/` | Fetch, clip and cache public data |
| `synthpop/model/` | Fitting, placement and runs |
| `synthpop/summarize.py` | Area stats with standard errors |
| `synthpop/validate.py` | The validation report |
| `synthpop/publish.py`, `atlas.py`, `api.py` | Published files, queries, HTTP routes |
| `app/` | deck.gl + MapLibre front end |
| `docs/` | Architecture, design, validation report, ADRs |

## Adding a variable

Add an entry to [config/variables.yaml](config/variables.yaml):
- a **seed** variable recoded from a PUMS column, or
- a **derived** variable built from other variables' categories.

Then rebuild. The variable appears in the API and the map without code changes.
