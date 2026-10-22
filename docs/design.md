# Codebase design

How the code is split into modules, what each module's interface is, and where the seams are. Terms are defined in [GLOSSARY.md](../GLOSSARY.md). Architecture vocabulary (module, interface, seam, adapter, depth) follows the codebase-design skill.

## The flow

```
sources ──► model ──► summarize ──► publish ──► atlas ──► api ──► app
  (fetch)   (81 runs)  (area stats)  (files)    (queries)  (HTTP)  (map)
                ▲           ▲                      ▲               ▲
                └───────────┴──── catalog ─────────┴───────────────┘
                              (variables.yaml)
```

`validate` reads `summarize`'s output next to published Census figures and writes the validation report. `cli` runs the whole build.

## The one contract everything shares

Every number the app shows comes from one long table:

| column | meaning |
|---|---|
| `level` | `parcel`, `block`, `block_group`, `tract`, `city`, `county`, `state` |
| `area_id` | GEOID, or a parcel ID |
| `variable` | a variable ID from the catalog, e.g. `age_group` |
| `category` | a category ID, e.g. `85_plus`, or `total` |
| `estimate` | the value from the main run, or the published value |
| `se` | standard error (from the 80 replicate runs, or derived from the published MOE) |
| `source` | `synthetic` or `published` |
| `confidence` | `high`, `medium` or `low` |

Geometry sits in a second table, `areas(level, area_id, parent ids, name, attributes, geometry)`.

Because every variable is just more rows, adding a variable to `variables.yaml` and rebuilding makes it show up in the API and the app with no code changes. Comparison levels (county, state) are published rows in the same table, so the detail panel treats them the same way as everything else. See [ADR 0001](adr/0001-long-area-stats-table.md).

## Modules

### `catalog`: the variable schema

**Interface**
```python
catalog = Catalog.load("config/variables.yaml")
catalog.variables                                      # ordered list, for the API's /schema
people, households = catalog.apply(seed_persons, seed_households)
```
`apply` returns the seed tables with one categorical column per variable.

**Hides**
- Recoding PUMS codes into categories (e.g. SCHL 01–15 → `lt_hs`).
- Universes (education only for people aged 25+).
- Evaluating derived variables (e.g. `age65_no_hs`).
- Later, joining attached variables (diabetes, obesity).

There are three variable kinds: `seed`, `derived` and `attached`. `attached` is defined in the schema now but not implemented tomorrow.

**Seam**: none needed. It's pure in-process code, tested with small DataFrames.

### `sources`: fetching public data

**Interface**
```python
sources = Sources(cache_dir, fetcher=HttpFetcher(api_key))
gdf = sources.load("parcels")     # also: "pums_persons", "pums_households",
                                  # "acs_bg", "blocks_2020", "e911", "published"
```
Every load is already clipped to the study area and cached.

**Hides**
- URLs and the Census API key, with the bulk-download fallback.
- ArcGIS paging.
- Unzipping, reprojection to EPSG:4326, clipping, and caching.

**Seam**: the `fetcher` port. This is the only real external dependency (Census, Maine GeoLibrary), and it has two adapters:
- `HttpFetcher` for real builds.
- `FixtureFetcher`, which reads `tests/fixtures/`.

### `model`: synthesis and placement

**Interface**
```python
placed = model.run(inputs, run=Run.main())             # or Run.replicate(17), Run.main(holdout=blocks)
# placed.households: household_id, seed_serial, parcel_id, block, block_group
# placed.persons:    person_id, household_id, catalog category columns
```

**Hides**
1. **Penalized maximum-entropy fit** (P-MEDM style) per block group. It fits seed household and group-quarters weights (WGTP/PWGTP, or replicate weight r) to the ACS block-group constraints named in the catalog, using household-level and person-level totals.
2. **Integerization** of the weights into whole households (truncate, then sample the remainder).
3. **Block split**: households are spread across blocks using 2020 Census housing-unit and population counts. Held-out blocks don't get these counts.
4. **Placement** of households on parcels:
   - Each household goes onto a housing unit from the E911 unit count for its parcel.
   - The PUMS BLD code is matched to the parcel's unit count (single-family vs multi-unit).
   - Parcels without an addressed unit get a fallback placement, flagged `low`.
5. **Group quarters**: residents are placed on likely facility parcels.

Totals always add up, because every household lands on exactly one parcel.

**Seam**: none at the interface. It's in-process, tested on fixtures. There are internal seams (`fit`, `place`) with their own tests. Why the fit is P-MEDM style rather than plain IPF is in [ADR 0002](adr/0002-penalized-entropy-fit.md).

### `summarize`: area stats

**Interface**
```python
stats = summarize(runs, catalog, areas)
```
It takes the main run plus 80 replicate runs and returns a DataFrame in the shared contract.

**Hides**
- Counting categories at the parcel level, then rolling up through block → block group → tract → city.
- The standard-error formula: SE = sqrt(4/80 · Σ(X_r − X)²), from the PUMS handbook p.18.
- The confidence rules (coefficient of variation, fallback placement).
- Privacy: no person-level rows, only counts and distributions.

**Seam**: none. It's in-process.

### `validate`: the validation report

**Interface**
```python
report = validate(stats, published, holdout_blocks)
report.write("docs/validation.md")
```
Two inputs are compared, both part of the shared contract: the validation run's stats and the published figures.

**Hides**
- The relative allocation error, |synthetic − published| / (1.645 · σ), at every level (from UrbanPop).
- Scoring held-out blocks against 2020 Census counts.
- The summary tables.

### `publish` and `atlas`: write side and read side

**Write side**
```python
publish(stats, areas, out_dir)
```
It writes `area_stats.parquet`, `areas.parquet` (GeoParquet), one GeoJSON file per level for the app, and `atlas.duckdb`.

**Read side**
```python
atlas = Atlas.open(out_dir)
atlas.schema()                                   # catalog + levels + legend breaks
atlas.layer(level, variable, category, filters)  # {area_id: estimate, se, confidence}
atlas.detail(level, area_id)                     # attributes, distributions with SE, comparisons up the hierarchy
atlas.search(text)                               # town, county, block group, address
atlas.export(level, area_ids, fmt)               # CSV or GeoJSON bytes
```

**Hides**
- DuckDB SQL.
- Pivoting long rows for the panel.
- Walking the hierarchy (parcel → block group → county → state) for comparisons.
- Applying filters such as `age65_no_hs.share > 0.2`.
- Labelling every value synthetic or published.

**Seam**: none at the interface. DuckDB runs in-process and is tested against a fixture database built by `publish`.

### `api`: HTTP routes

Thin FastAPI routes, one per `Atlas` method, plus static serving of the GeoJSON files and the built app. Nothing to test beyond a smoke test, because the logic lives in `Atlas`.

### `app`: the map

A Vite + TypeScript front end with deck.gl and MapLibre. Its modules:

| module | interface | hides |
|---|---|---|
| `schema.ts` | `loadSchema()` | Fetching `/schema`. Every selector, legend and filter is built from it. |
| `levels.ts` | `levelForZoom(z)` | The zoom → level rules. |
| `mapLayer.ts` | `renderLevel(level, values, style)` | Joining values to cached geometry by `area_id`, extrusion height, colour scale, low-confidence hatching or hiding. |
| `panel.ts` | `showDetail(detail)` | Age-sex pyramid, bars with visible error bars, comparison rows. |
| `controls.ts` | `mountControls(schema, onChange)` | Variable, category and filter selectors, search, confidence toggle, export. |

Geometry is fetched once per level and values separately per variable, so changing the variable doesn't reload shapes. Switching to PMTiles for national scale changes only `mapLayer.ts`.

## Folder layout

```
maine-synthpop/
  GLOSSARY.md  README.md
  config/   study_area.yaml   variables.yaml
  synthpop/ catalog.py  sources/  model/  summarize.py  validate.py
            publish.py  atlas.py  api.py  cli.py
  tests/    fixtures/   test_catalog.py  test_model.py  test_summarize.py  test_atlas.py
  app/      index.html  src/{main,schema,levels,mapLayer,panel,controls}.ts
  docs/     design.md  architecture.md  model.md  validation.md  scaling.md  risks.md  adr/
  data/     raw/  interim/  out/          (git-ignored)
```

## Tests

Tests cross module interfaces only, and use one tiny fixture town: 2 block groups, 5 blocks, 12 parcels and 30 seed households.

- `model.run`: block-group totals match the constraints, and every household lands on exactly one parcel with free units.
- `summarize`: parcels add up to blocks, blocks to block groups, and so on. The standard error is zero when every replicate equals the main run.
- `catalog`: a new seed or derived variable in a fixture YAML appears in `summarize` output and in `atlas.schema()` with no code change. This test is the requirement itself.
- `atlas`: `detail` returns comparisons for every level up to the state, and filters and exports return the expected parcels.
