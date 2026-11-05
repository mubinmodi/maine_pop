# Person table

One row per synthetic person for one run.

```bash
uv run synthpop persons                 # main run -> data/out/persons_main.csv
uv run synthpop persons --run draw_07   # any other placement draw
```

| Column | Meaning |
|---|---|
| `person_id` | Synthetic person, unique within the run |
| `family_id` | Census family the person belongs to: the householder and their relatives by birth, marriage or adoption. Empty for people outside a family (see below). Equal to the family's `household_id` |
| `parcel_id` | Maine parcel ID the person is placed on |
| `AGE` | Exact age in years, from the seed person's `AGEP` |
| `SEX` | `male` or `female`, from the seed person's `SEX` |
| `household_id` | Synthetic household the person belongs to. Empty for the 1,959 group-quarters residents (dorms, nursing homes, shelters), who live in no household |
| `lon`, `lat` | Point to plot the person at (WGS 84) |
| `location` | `address` when the point is an E911 address point, `parcel` when it is a random point inside the parcel |

The main run gives 68,769 persons in 33,287 households and 14,771 families; 99.5% sit on an E911 address point.

In the app, **People → Person table (one run)** opens the same table over the bottom of the map: all 68,769 rows, 100 a page, following the sidebar's people filters. Search finds a person, family or household by number, or a parcel by ID. **Download CSV** saves the matching rows, and clicking a row flies to that person and opens their parcel.

## Points

`synthpop.persons.locate` gives every person a point on their own parcel:

- A household gets one of its parcel's E911 address points, and all its members share it. A group-quarters resident gets a point of their own.
- Residential address points are used when the parcel has any. Otherwise any point on the parcel is used, because nursing homes and dorms are typed as non-residential in E911.
- Points are dealt out in random order, so two households share a point only once every point on the parcel is taken. Apartment buildings often have one address point for many units, so many people stack on it (up to 188 households on one point in the main run). Plot with transparency or size by count.
- The 31 parcels with no address point (331 persons) get a uniform random point inside the parcel boundary.
- The draw uses its own random stream from the study area's `random_seed`, so the same run always gives the same points.

## Households and families

`household_id` is the Census household: everyone sharing one housing unit. `family_id` is the Census family: the householder plus everyone in the household related to them by birth, marriage or adoption, read from the seed person's `RELSHIPP`:

- In the family: householder (20), spouse (21, 23), biological, adopted or stepchild (25–27), sibling (28), parent (29), grandchild (30), parent-in-law or child-in-law (31, 32), other relative (33).
- Not in the family: unmarried partner (22, 24), roommate (34), foster child (35), other nonrelative (36), and group-quarters residents.

A household with no relative of the householder (someone living alone, roommates, an unmarried couple with no children) holds no family, so all its members have an empty `family_id`. Every synthetic household copies one seed household whole, so relationships stay intact and the family is named by its household. 40,962 of the 68,769 people are in a family.

## How exact age gets in

A run keeps only the catalog categories, so age is stored as 5-year groups. Every synthetic person is a copy of one seed (PUMS) person, and each run now records which one in `seed_serial` and `seed_person` (PUMS `SERIALNO` and `SPORDER`). `synthpop.persons.person_table` joins any PUMS person column back through those two keys. Adding the keys changed no other value in any run.

This is one realisation. Placement differs between draws, so a person's parcel changes from `main` to `draw_01`. Area totals should come from the 21-draw means in `area_stats.parquet`, not from one person table.

## People on the map

`synthpop publish` also writes `data/out/people.parquet`: the same main-run persons and points, plus each person's catalog categories and a fixed uniform draw `u`. `/api/people` serves them as one columnar JSON document (about 0.8 MB gzipped). After that single download the app does everything in the browser:

- **Data points apply automatically.** Every saved data point, and the one open in the editor, gives each person a case (1) or not (0): a person is a case when `u` is below their risk from the model. Because `u` is fixed, moving a slider only adds or removes cases at the margin; it never reshuffles who has the condition. People outside the model's universe (under 65 for dementia) are shown faint.
- **Colour people by** any data point or attribute. People open coloured by the first saved data point.
- **Show only people where** stacks filters on data points (case / not a case) or attributes. The legend counts the people shown.
- People appear from block zoom (14) down. People sharing an address point stack upward 2.5 m apart, so apartment buildings read as columns. While people are shown, the area layer goes flat and faint.
- While a saved data point is being edited, filters on it follow the draft; closing without saving puts them back.
- **Clicking an area** (tract, block group, block or parcel) adds "Synthetic people here" to its detail panel: people, households, median age, share female, group-quarters residents and cases for each data point, counted from the dots. People outside the area fade. Clicking while zoomed out flies in to the area's people; "Show on map" does the same later. Clicking a dot opens its parcel.
- **People table** toggles a table of the area's people with the same columns as the CSV (plus block, race and ethnicity, education and one column per data point: 1 case, 0 not, blank outside the universe). It shows up to 500 rows; **Download CSV** gives all of them.

Case counts on the map are one realisation, so they differ from the area estimates (the expected number of cases, averaged over 21 placement draws) by chance. For dementia, 1,124 people are cases against 1,077 ± 171 expected.

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
