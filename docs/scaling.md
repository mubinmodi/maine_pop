# Scaling plan: Portland → Maine → all states

Deliverable 5. Every estimate below is scaled from a **measured** Portland build (first table), using measured counts for Maine and the US. Estimates are marked "≈". Rounding is generous because seed sizes and parcel densities vary by place.

## Baseline: what Portland actually cost

Measured on an Apple M2 (8 cores, 16 GB), October 2026.

| | Portland |
|---|---|
| People / block groups / blocks / parcels | 68,711 / 61 / 1,065 / 31,079 (17,020 with households) |
| Seed (PUMA 23-01000) | 2,301 units (1,859 households + 442 group-quarters people), 62 constraint cells |
| Fit, one block group, one run | 3.4 ms |
| One full run (fit + integerize + place), 61 block groups | 0.63 s, so **10.3 ms per block group per run** |
| Runs per build | 103: main, 20 placement draws, 80 replicates, validation |
| `run` (all 103) | ≈ 70 s |
| `summarize` (101 runs → 1.31M synthetic rows) | 85 s |
| `fetch` | ≈ 2 min, network-bound |
| Disk: raw / interim / runs / out | 141 MB / 7.3 MB / 265 MB / 32 MB |
| Published files the app reads | `area_stats` 18 MB, `geo/` 8.3 MB (parcels 8.0 MB, 0.9 MB gzipped), `areas` 2 MB, `search` 0.8 MB |

## Units of work

| | Count | Source |
|---|---|---|
| Block groups, US + DC | 239,742 (242,297 with Puerto Rico) | counted in the ACS 2020–2024 summary file |
| Block groups, Maine | 1,184 | same |
| Tracts / counties, US + PR | 85,382 / 3,222 | same |
| 2020 blocks, Maine / US + territories | 47,138 / 8,180,866 | TIGER file / [Census figure via Wikipedia](https://en.wikipedia.org/wiki/Census_block) |
| PUMAs (2020) | ≈ 2,487 | [Wikipedia](https://en.wikipedia.org/wiki/Public_Use_Microdata_Area) |
| Parcels, Maine | 716,593 | Maine GeoLibrary service |
| Parcels, US | ≈ 149–151 million | [Regrid's published counts](https://app.regrid.com/parcels), commercial |
| People, Maine / US | 1.36M / 331M | 2020 Census |

## The job unit is the PUMA

Each block group is fitted against the seed of the PUMA that contains it. PUMAs are built from tracts, so every block group falls in exactly one. So a **PUMA is a self-contained job**: one seed, 100K–200K people, which is 1.5–3 times Portland. That gives about 2,487 independent jobs nationally, each needing under 4 GB of memory. Counties are worse units: Los Angeles County is about 150 times Portland.

Each PUMA job runs `fetch → run → summarize → publish` for its block groups. It **streams** each run into running sums instead of saving all 103 runs (Portland saved 265 MB of runs; only the main run, 3.1 MB, is needed afterwards). For levels larger than a PUMA (counties, states, and tracts in rare split cases), each job also writes its per-run totals: 103 numbers per area, variable and category. Higher-level estimates and standard errors are then computed across PUMAs, because the replicate formula needs per-run totals, not per-PUMA standard errors.

## Compute

| | Portland (measured) | Maine (≈) | United States (≈) |
|---|---|---|---|
| `run`, single core | 70 s | 1,184 BGs × 103 runs × 10.3 ms ≈ **21 min** | 239,742 × 103 × 10.3 ms ≈ **71 CPU-hours**, up to 2× for larger seeds |
| `summarize`, single core | 85 s | × 23 (parcels) ≈ **33 min** | × 4,830 ≈ **114 CPU-hours** |
| Wall clock | 2.5 min on the M2 | **≈ 10 min** on the M2 (10 PUMAs, 8 cores) | **≈ 3 hours** on one 96-vCPU machine, or under 1 hour as a fan-out batch |
| Compute cost | none | none (laptop) | 200–260 CPU-hours at about $0.04–0.05 per vCPU-hour on demand ≈ **$10–15 per national build**, less on spot instances |

Compute is not the constraint. Data acquisition and storage are.

## Storage

| | Portland (measured) | Maine (≈) | United States (≈) |
|---|---|---|---|
| Raw inputs | 141 MB | ≈ 1.5–2 GB: statewide parcels and E911 ≈ 1 GB, blocks 108 MB, PUMS 14 MB | ≈ 100–150 GB: PUMS 3.0 GB zipped, blocks ≈ 19 GB zipped (Maine's 2.3 KB per block × 8.2M), parcels 50–100 GB, address points 10–30 GB |
| Runs, if all saved | 265 MB | ≈ 5 GB | ≈ 1.3 TB, so **don't save them: stream** |
| Main run kept (households, persons) | 3.1 MB | ≈ 60 MB | ≈ 15 GB |
| `area_stats` (Parquet) | 18 MB | ≈ 420 MB | ≈ 90 GB, partitioned by `state/level` |
| Map geometry | 8.3 MB GeoJSON | parcels ≈ 185 MB as GeoJSON, too big for a browser, so **PMTiles** ≈ 100–200 MB | PMTiles ≈ 40–80 GB, all levels |
| **Total at build time** | 0.45 GB | **≈ 3–6 GB** (fits on the M2's 19 GB free) | **≈ 250–350 GB** |
| **Served** | 32 MB | ≈ 0.7 GB | ≈ 150 GB, ≈ $4 per month in object storage plus CDN egress |

## What has to change in the app

Portland's whole city fits in the browser as GeoJSON. Maine already doesn't: 716K parcels at 185 MB. These changes are needed for statewide Maine, in order:

1. **Geometry as PMTiles.** Run `tippecanoe` on each level's GeoJSON (installable with `brew install tippecanoe pmtiles`), keeping only `id` in the tiles. Host the files on object storage behind a CDN. MapLibre reads them through the `pmtiles://` protocol. Only `mapLayer.ts` changes, as the design planned.
2. **Values per viewport, not per level.** Add a `bbox` (or `z/x/y`) parameter to `/api/layer` so the app fetches only the areas in view, and cache responses at the CDN by `(level, variable, category, measure, tile)`. A statewide parcel layer is 716K values, which is fine as tiles but too much as one response.
3. **Partitioned stats.** Write `area_stats` as Parquet partitioned by `state` and `level`. DuckDB reads only the partitions a query touches. At Maine scale one DuckDB process is enough.
4. **Search.** Use DuckDB's full-text index for one state. Nationally, with about 150M addresses, move search to Postgres with `pg_trgm`, or to a search engine such as Meilisearch or Typesense.

**National serving.** Tiles and cached value tiles on a CDN handle the load. Click-through detail and export are key lookups, because the clicked feature's ID comes from the tile. They can stay on DuckDB over partitioned Parquet on stateless API replicas. Move to Postgres + PostGIS when one of these holds:
- concurrency is high enough that many replicas each hold a copy of the 90 GB stats table, or
- spatial queries on the server become necessary, for example "areas within this drawn polygon".

## What has to change in the model

| Change | Why | Needed for |
|---|---|---|
| Per-state adapters for parcels and address points, behind the existing `Fetcher` seam | There is no free national parcel layer. Coverage and schemas differ by state. National options are a Regrid licence (about $25K–65K a year per their pricing page), state GIS portals, or, as a fallback, building footprints (Overture or Microsoft) plus the USDOT National Address Database | Every new state |
| A full state postal-code table (`sources._state_abbr`) | Only Maine is listed today | Second state |
| PUMA job runner with streaming summarize, and per-run totals for higher levels | Memory, disk, and correct SEs above the PUMA | Maine (10 PUMAs) onward |
| 2020 Census DHC block tables (sex by age, race, group quarters) as block-level constraints | The biggest quality gain below block group: the 2020 Census published age, sex and race for every block, which we don't use yet. Needs a free Census API key | Recommended before Maine |
| Group-quarters sites from national facility lists (HIFLD nursing homes, colleges, prisons) | E911 place types are Maine-specific and sparse | Second state |
| Assessor attributes (year built, bedrooms, value) where available | Better structure and household matching on parcels | Wherever obtainable |
| Tract-level constraints next to block-group ones, as P-MEDM does | Tract ACS estimates are more precise | Optional |

## Suggested order

1. **Portland → Cumberland County.** Same PUMA family, more parcels. Add DHC block constraints and the API key.
2. **Statewide Maine.** 10 PUMA jobs on the laptop, PMTiles, viewport values, partitioned stats. This is the spec's performance target.
3. **A second, different state** (for example a large metro state). This tests the parcel and address adapters and the facility-list group-quarters method.
4. **National.** A batch fan-out over about 2,487 PUMAs on cloud spot capacity, a CDN for tiles, and Postgres or a search engine for address search.
