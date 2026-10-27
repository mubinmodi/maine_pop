# Maine synthetic population

**Demo guide:** [how to use the map and the data-point sliders, and what the numbers mean](https://claude.ai/artifact/9SSRgA9MBvG7BqgxAqNtuf)

A parcel-level synthetic population for Portland, Maine, built only from public Census and State of Maine data, with an interactive 3D map. Every value is a **synthetic estimate**, not a record of real people.

- Architecture: [docs/architecture.md](docs/architecture.md)
- Module design: [docs/design.md](docs/design.md)
- Validation report: [docs/validation.md](docs/validation.md)
- Scaling plan: [docs/scaling.md](docs/scaling.md)
- Risks and open questions: [docs/risks.md](docs/risks.md)
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

The app is one process: FastAPI serves the API, the geometry files and the built front end, and reads only `data/out/` (about 35 MB for Portland). After building the data:

```bash
docker build --platform linux/arm64 -t maine-synthpop .
docker run -p 8000:8000 -e SYNTHPOP_PASSWORD=try-me maine-synthpop
```

Three environment variables configure a deployment:

- `SYNTHPOP_PASSWORD` turns on basic auth (user `SYNTHPOP_USER`, default `synthpop`). `/healthz` stays open for load-balancer health checks.
- `SYNTHPOP_DATAPOINTS` sets where saved data points live. Use `s3://bucket/key` in a container, or saves are lost on redeploy.

[docs/deploy-aws.md](docs/deploy-aws.md) has step-by-step commands for AWS ECS Express Mode (Fargate on Graviton, HTTPS load balancer, S3, Secrets Manager), at about $80 a month (about $2.70 a day for a short demo). Any other container host works too. Keep one server copy while saved data points are in a single file.

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

## Data points: health measures, live

In the app's **Data points** section, start a new data point, or start from a preset such as Alzheimer's dementia from Dhana et al. 2023. A data point is a risk model:
- a baseline prevalence for a reference person;
- odds ratios by age, sex, race/ethnicity, education and living quarters;
- the ages it counts.

Every synthetic person gets a probability, and each area shows its expected cases and rate with margins. Moving any slider redraws the map and the rate-by-group charts within a fraction of a second.

**Save as variable** writes the model to `config/datapoints.yaml`. It then works like any other variable (colour, filters, detail panel, export) with no rebuild. **×** removes it. The live maths uses per-run person counts that `synthpop publish` writes (`cells_draws.parquet`, `cells_reps.parquet`). The margins cover the synthetic population's uncertainty, not uncertainty in the odds ratios.

## Adding a variable

Add an entry to [config/variables.yaml](config/variables.yaml):
- a **seed** variable recoded from a PUMS column, or
- a **derived** variable built from other variables' categories.

Then rebuild. The variable appears in the API and the map without code changes.
