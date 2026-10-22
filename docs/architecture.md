# System architecture

Deliverable 1. A parcel-level synthetic population for the City of Portland, Maine, from public data to an interactive 3D map. Module-level design is in [design.md](design.md). Terms are in [GLOSSARY.md](../GLOSSARY.md).

```
 PUBLIC DATA            BUILD (Python, run once per vintage)                       SERVE                    USE
┌──────────────┐   ┌─────────┐   ┌───────────────────┐   ┌───────────┐   ┌─────────┐   ┌────────┐   ┌──────────────────┐
│ PUMS 5-year  │   │         │   │ model             │   │           │   │         │   │        │   │ app              │
│ ACS 5-year   │──►│ sources │──►│  entropy fit / BG │──►│ summarize │──►│ publish │──►│ atlas  │──►│ deck.gl+MapLibre │
│ TIGER 2020   │   │ fetch,  │   │  × 81 runs        │   │ every     │   │ Parquet │   │ DuckDB │   │ 3D, zoom levels, │
│ ME parcels   │   │ clip,   │   │  place on parcels │   │ level,    │   │ GeoJSON │   │ api    │   │ panel, filters,  │
│ ME E911      │   │ cache   │   │  (E911 units)     │   │ SE        │   │ DuckDB  │   │FastAPI │   │ search, export   │
└──────────────┘   └─────────┘   └───────────────────┘   └─────┬─────┘   └─────────┘   └────────┘   └──────────────────┘
                        ▲                  ▲                   │
                        └──── catalog ─────┴───── validate ◄───┘
                        (config/variables.yaml)   (docs/validation.md)
```

## 1. Data pipeline (`synthpop fetch`)

Every input is public, needs no login and no API key. Each is clipped to the study area, reprojected to EPSG:4326 and cached in `data/interim/` as (Geo)Parquet.

| Source | What we take | Portland result |
|---|---|---|
| [ACS PUMS 2020–2024 5-year](https://www2.census.gov/programs-surveys/acs/data/pums/2024/5-Year/), Maine | Household and person records for PUMA 23-01000 (Portland, South Portland, Westbrook), with all 80 replicate weights | 2,453 housing records (1,859 occupied households), 4,185 persons (442 in group quarters) |
| [ACS 2020–2024 5-year table-based summary files](https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/) | Tables B01001, B03002, B11016, B15003, B25002, B25008, B25032, streamed and filtered without storing the national files | 61 block groups, 20 tracts, plus city, county and state rows (11,760 values) |
| [TIGER/Line 2024](https://www2.census.gov/geo/tiger/TIGER2024/) place, tracts, block groups | Boundaries. Areas count as Portland if at least half their area is inside the city. | 1 city, 20 tracts, 61 block groups |
| [TIGER/Line 2020 tabulation blocks](https://www2.census.gov/geo/tiger/TIGER2020/TABBLOCK20/) | Block boundaries with 2020 Census population and housing-unit counts | 1,065 blocks, 68,336 people, 35,591 housing units |
| [Maine GeoLibrary statewide parcels](https://services1.arcgis.com/RbMX0mRVOFNTdLzd/ArcGIS/rest/services/Parcels/FeatureServer/38) | Parcel boundaries and IDs (no assessor attributes) | 31,321 parcels |
| [Maine E911 address points](https://services1.arcgis.com/RbMX0mRVOFNTdLzd/ArcGIS/rest/services/Maine_E911_Addresses_Feature/FeatureServer/0) | Address, unit, place type, parcel key | 40,708 points. 76.6% match a parcel ID by key; the rest are joined by location. |

Checks on the fetched data: the 61 block groups sum to 68,761 people against 68,854 for the city (99.9%). The 2020 blocks sum to 68,336 against the city's 2020 count of 68,408.

The variables, constraint tables and category mappings all live in `config/variables.yaml`. Every ACS cell ID in it was checked against the official table shells (`ACS20245YR_Table_Shells.txt`).

## 2. Model (`synthpop build`, next)

For the main run, 20 more placement draws of it, 80 replicate runs (one per PUMS replicate weight set) and one validation run, in every block group:

1. **Fit.** A penalized maximum-entropy fit (P-MEDM style, [ADR 0002](adr/0002-penalized-entropy-fit.md)) reweights the PUMA's seed households and group-quarters persons to that block group's ACS totals: sex × age, race/ethnicity, education (25+), household vs group quarters, household size, and tenure × units in structure.
2. **Integerize.** Turn weights into whole synthetic households.
3. **Place.** Put each household on a housing unit on a parcel:
   - Unit counts come from E911 points per parcel, calibrated to 2020 block housing counts.
   - Structure type is matched: a household from a single-family home goes to a 1-unit parcel, one from a 20+ unit building to a 20+ unit parcel.
   - Group-quarters persons go to likely facility parcels.

A separate validation run hides 20% of blocks' 2020 counts. It is used only to score placement.

## 3. Storage (`publish`)

| File | Contents | Used by |
|---|---|---|
| `data/out/area_stats.parquet` | The shared contract: `level, area_id, variable, category, estimate, se, source, confidence` | atlas, validation, export |
| `data/out/areas.parquet` (GeoParquet) | Every area at every level, with parent IDs, name and attributes | atlas |
| `data/out/geo/<level>.geojson` | Simplified geometry per level, ID only | app (fetched once per level) |
| `data/out/atlas.duckdb` | Views over the two Parquet files, with indexes on `(level, area_id)` and `(level, variable, category)` | atlas |

Each run's households and people are kept under `data/runs/<run>/` (about 140 MB for Portland). Only the main run is needed after `summarize`. Portland's whole output is well under 1 GB.

## 4. API (FastAPI, `synthpop serve`)

| Route | Returns |
|---|---|
| `GET /schema` | Levels, variables and categories from the catalog. Drives every selector in the app. |
| `GET /layer?level=&variable=&category=&filter=` | `{area_id: estimate, se, confidence}` for one level |
| `GET /areas/{level}/{area_id}` | Detail panel: attributes, households and persons, distributions with SE, comparison to block group, tract, city, county and state |
| `GET /search?q=` | Addresses (E911), block groups, tracts, city, county |
| `GET /export?level=&ids=&format=csv\|geojson` | The current selection |
| `GET /geo/{level}.geojson`, `/` | Static geometry and the built app |

Every response labels values as `synthetic` or `published`.

## 5. App (Vite + TypeScript, deck.gl + MapLibre)

- **Zoom levels.** The zoom picks the level: city (z < 11), tract (11–12.5), block group (12.5–14), block (14–15.5), parcel (15.5+).
- **3D map.** Areas are extruded by a chosen count and coloured by any variable or category, with a legend.
- **Detail panel.** Clicking an area opens an age-sex pyramid, race/ethnicity and education bars, error bars always visible, and comparison rows.
- **Controls** are generated from `/schema`: variable and category, a threshold filter, search, a toggle to hide or flag low-confidence areas, and CSV/GeoJSON export.
- **Disclosure.** A banner and every value carry the "synthetic estimate" label.

## 6. Run order

```bash
uv sync
uv run synthpop fetch        # about 2 minutes, all inputs
uv run synthpop build        # model, summarize, validate, publish
uv run synthpop serve        # API and app on http://localhost:8000
```
