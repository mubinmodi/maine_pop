# Person table

One row per synthetic person for one run.

```bash
uv run synthpop persons                 # main run -> data/out/persons_main.csv
uv run synthpop persons --run draw_07   # any other placement draw
```

| Column | Meaning |
|---|---|
| `person_id` | Synthetic person, unique within the run |
| `household_id` | Synthetic household the person belongs to. Empty for the 1,959 group-quarters residents (dorms, nursing homes, shelters), who live in no household |
| `parcel_id` | Maine parcel ID the person is placed on |
| `AGE` | Exact age in years, from the seed person's `AGEP` |
| `SEX` | `male` or `female`, from the seed person's `SEX` |
| `lon`, `lat` | Point to plot the person at (WGS 84) |
| `location` | `address` when the point is an E911 address point, `parcel` when it is a random point inside the parcel |

The main run gives 68,769 persons in 33,287 households; 99.5% sit on an E911 address point.

## Points

`synthpop.persons.locate` gives every person a point on their own parcel:

- A household gets one of its parcel's E911 address points, and all its members share it. A group-quarters resident gets a point of their own.
- Residential address points are used when the parcel has any. Otherwise any point on the parcel is used, because nursing homes and dorms are typed as non-residential in E911.
- Points are dealt out in random order, so two households share a point only once every point on the parcel is taken. Apartment buildings often have one address point for many units, so many people stack on it (up to 188 households on one point in the main run). Plot with transparency or size by count.
- The 31 parcels with no address point (331 persons) get a uniform random point inside the parcel boundary.
- The draw uses its own random stream from the study area's `random_seed`, so the same run always gives the same points.

`household_id` is the Census household (everyone sharing one housing unit), not the Census family (people related by birth, marriage or adoption). A household of roommates has one `household_id` but is not a family.

## How exact age gets in

A run keeps only the catalog categories, so age is stored as 5-year groups. Every synthetic person is a copy of one seed (PUMS) person, and each run now records which one in `seed_serial` and `seed_person` (PUMS `SERIALNO` and `SPORDER`). `synthpop.persons.person_table` joins any PUMS person column back through those two keys. Adding the keys changed no other value in any run.

This is one realisation. Placement differs between draws, so a person's parcel changes from `main` to `draw_01`. Area totals should come from the 21-draw means in `area_stats.parquet`, not from one person table.

## Adding race, poverty, employment and education

The seed person carries all four already, because the whole person is copied. The choice is whether each attribute is also *calibrated*, meaning fitted to that block group's ACS table.

| Attribute | PUMS field | ACS block-group table | Calibrated now? |
|---|---|---|---|
| Race and ethnicity | `RAC1P`, `HISP` | B03002 | Yes (`race_eth` constraint) |
| Education (25+) | `SCHL` | B15003 | Yes (`education` constraint) |
| Poverty | `POVPIP` (income-to-poverty ratio) | C17002 | No |
| Employment status (16+) | `ESR` | B23025 | No |

### Option A: carry through from the seed

Add the PUMS field to `FIELDS` in `synthpop/persons.py` (and to `PERSON_COLS` in `synthpop/sources/__init__.py` for `POVPIP` and `ESR`, which are not cached yet, then `synthpop fetch --only pums_persons`).

- **Cost:** a few lines, no model change.
- **What you get:** values that are consistent within each person and household. A 30-year-old renter with a degree gets an employment status and poverty ratio that such people actually have in the PUMA.
- **Limit:** for poverty and employment, the local mix only follows from what is calibrated (age, sex, race, education, household size, tenure, building type). A block group with unusually high unemployment for its profile is not reproduced.
- **Right for race and education today:** they are already calibrated, so carrying the raw codes through adds detail at no accuracy cost.

### Option B: add a constraint (recommended for poverty and employment)

Add a variable and a constraint to `config/variables.yaml`, in the same form as `race_eth` and `education`, mapping categories to C17002 or B23025 cells. The fit then matches each block group's published totals within their margins, and the summary, API, map and validation report pick the variable up with no code change.

- **Cost:** YAML plus the two PUMS fields; one full rebuild (about 3 minutes).
- **Risk:** block-group ACS margins for these tables are wide, and the seed has only 1,859 households, so the extra targets may pull against existing ones. Check with the validation report (share of cells inside the ACS 90% margin) before and after.
- **Universes differ:** poverty status excludes some group-quarters residents and unrelated children under 15 (`POVPIP` is empty for them); employment covers ages 16+ (`ESR` is empty below 16). The variable's `universe` must match the table's.

### Option C: a separate imputation model

Predict poverty or employment from the other columns, as data points do for health conditions. Not recommended: copying the whole seed person already gives coherent values, and a second model adds error without adding local information.

### Suggested order

1. Option A for all four, so the person table carries them now.
2. Option B for poverty, then employment, one at a time, checking the validation report after each.
