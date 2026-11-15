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
| `RACE` | `white`, `black`, `asian`, `other` (other or multiracial) — all non-Hispanic — or `hispanic` (any race). The run's `race_eth`, fitted to ACS B03002 |
| `EDUCATION` | `lt_hs`, `hs`, `some_college` or `ba_plus`; empty under 25. The run's `education`, fitted to ACS B15003 |
| `POVERTY` | `below_100`, `p100_199` or `p200_plus` (income as % of the poverty line); empty where poverty status is not determined. The run's `poverty`, fitted to ACS C17002 |
| `POVERTY_RATIO` | Exact income-to-poverty ratio in percent, from the seed person's `POVPIP` (501 = over 500%); empty with `POVERTY` |
| `EMPLOYMENT` | `employed` (including the Armed Forces), `unemployed` or `not_in_lf` (not in the labor force); empty under 16. The run's `employment`, fitted to ACS B23025 |
| `household_id` | Synthetic household the person belongs to. Empty for the 1,959 group-quarters residents (dorms, nursing homes, shelters), who live in no household |
| `lon`, `lat` | Point to plot the person at (WGS 84) |
| `location` | `address` when the point is an E911 address point, `parcel` when it is a random point inside the parcel |

The main run gives 68,754 persons in 33,338 households and 14,748 families; 99.6% sit on an E911 address point.

In the app, **People → Person table (one run)** opens the same table over the bottom of the map: all 68,754 rows, 100 a page, following the sidebar's people filters. Search finds a person, family or household by number, or a parcel by ID. **Download CSV** saves the matching rows, and clicking a row flies to that person's household and opens it with them picked.

## Points

`synthpop.persons.locate` gives every person a point on their own parcel:

- A household gets one of its parcel's E911 address points, and all its members share it. A group-quarters resident gets a point of their own.
- Residential address points are used when the parcel has any. Otherwise any point on the parcel is used, because nursing homes and dorms are typed as non-residential in E911.
- Points are dealt out in random order, so two households share a point only once every point on the parcel is taken. Apartment buildings often have one address point for many units, so many people share it (up to 191 households on one point in the main run). The map spreads households over the parcel instead (see *Household circles*); for your own plots, use transparency or size by count.
- The 33 parcels with no address point (303 persons) get a uniform random point inside the parcel boundary.
- The draw uses its own random stream from the study area's `random_seed`, so the same run always gives the same points.

## Households and families

`household_id` is the Census household: everyone sharing one housing unit. `family_id` is the Census family: the householder plus everyone in the household related to them by birth, marriage or adoption, read from the seed person's `RELSHIPP`:

- In the family: householder (20), spouse (21, 23), biological, adopted or stepchild (25–27), sibling (28), parent (29), grandchild (30), parent-in-law or child-in-law (31, 32), other relative (33).
- Not in the family: unmarried partner (22, 24), roommate (34), foster child (35), other nonrelative (36), and group-quarters residents.

A household with no relative of the householder (someone living alone, roommates, an unmarried couple with no children) holds no family, so all its members have an empty `family_id`. Every synthetic household copies one seed household whole, so relationships stay intact and the family is named by its household. 41,019 of the 68,754 people are in a family.

## How exact values get in

A run keeps only the catalog categories, so age is stored as 5-year groups and poverty as three bands. Every synthetic person is a copy of one seed (PUMS) person, and each run now records which one in `seed_serial` and `seed_person` (PUMS `SERIALNO` and `SPORDER`). `synthpop.persons.person_table` joins any PUMS person column back through those two keys (`FIELDS`: exact age, sex and poverty ratio), and takes the run's own categories for the rest (`CATEGORIES`), so the table agrees with the map.

This is one realisation. Placement differs between draws, so a person's parcel changes from `main` to `draw_01`. Area totals should come from the 21-draw means in `area_stats.parquet`, not from one person table.

## People on the map

`synthpop publish` also writes `data/out/people.parquet`: the same main-run persons and points, plus each person's catalog categories, exact poverty ratio and a fixed uniform draw `u`. `/api/people` serves them as one columnar JSON document (about 1.2 MB gzipped). After that single download the app does everything in the browser:

- **Data points apply automatically.** Every saved data point, and the one open in the editor, gives each person a case (1) or not (0): a person is a case when `u` is below their risk from the model. Because `u` is fixed, moving a slider only adds or removes cases at the margin; it never reshuffles who has the condition. People outside the model's universe (under 65 for dementia) are shown faint.
- **Colour people by** any data point or attribute. People open coloured by the first saved data point.
- **Show only people where** stacks filters on data points (case / not a case) or attributes. The legend counts the people shown.
- **Households, not dots.** From block zoom (14) down, every household is a circle on its parcel, and all the group-quarters residents of one parcel (a dorm, nursing home or shelter) form one more. Circles are spread over the parcel rather than stacked on the address point, and a circle's area is proportional to the people in it (see *Household circles* below).
- **Colour.** A household takes its householder's colour (the Census convention: households by age, race or tenure of householder), or its first member's for group quarters. Coloured by a data point, a household is a case when anyone in it is. The legend still counts people. With filters on, a household shows when anyone in it passes, and takes its colour from the people who pass.
- **Open a household** by clicking its circle. The map zooms in until the circle is big enough, its people appear inside it as dots (householder at the top, then oldest first), and a card lists them: role (householder, relative, not related, group-quarters resident), age and sex, plus the household's tenure, building type, family and cases per data point. Click a person, on the map or in the card, to see everything about them. **Open parcel** opens the parcel's detail panel; × or Esc closes the household.
- While households are shown, 3D buildings (parcels, blocks) turn see-through, so the circles under them stay visible. The **3D** button under the map's zoom controls, or **3D buildings and areas** in the sidebar, turns extrusion on or off; with it off, areas lie flat and faint under the households.
- While a saved data point is being edited, filters on it follow the draft; closing without saving puts them back.
- **Clicking an area** (tract, block group, block or parcel) adds "Synthetic people here" to its detail panel: people, households, median age, share female, group-quarters residents and cases for each data point, counted from the people. Households outside the area fade. Clicking while zoomed out flies in to the area's households; "Show on map" does the same later.
- **People table** toggles a table of the area's people with the same columns as the CSV (plus block and one column per data point: 1 case, 0 not, blank outside the universe). It shows up to 500 rows; **Download CSV** gives all of them.

### Playing every simulation

The build saves 102 runs: the main run, 20 placement draws, 80 replicates and the validation run (see `synthpop run`). **People → ▶ Play all simulations** opens a player at the top of the map that steps the people layer through all of them like a video: play and pause, step back and forward, a scrubber, and a speed of 0.5 to 4 runs a second. A strip of ticks shows where you are (black main, blue draws, green replicates, orange validation), and the label says what the run is and how many people and households it has against the main run.

- While the player is open, households show at every zoom, so the whole city can be watched. Household ids differ between runs, so an open household closes when the run changes.
- Colours, filters, data points, the legend and the person table all follow the run on screen. Data points are reapplied to each run's people, so case counts change from run to run.
- Runs load on demand (about 1.2 MB gzipped each), three ahead of the playhead; the server keeps the last eight.
- Closing the player goes back to the main run.

`synthpop publish` writes the runs to `data/out/people_runs/` (about 25 MB for all 102). A person's attributes depend only on the seed person they copy, so they are stored once (`seeds.parquet`), as are the map points (`points.parquet`) and `u` (`u.parquet`, one stream shared by every run). Each `<run>.parquet` keeps only seed, household, a family flag and point, about 0.25 MB. `/api/people/runs` lists the runs and `/api/people?run=<name>` serves any of them in the same form as the main run.

Case counts on the map are one realisation, so they differ from the area estimates (the expected number of cases, averaged over 21 placement draws) by chance. For dementia, 1,124 people are cases against 1,076 ± 119 expected.

### Household circles

`synthpop/households.py` lays the circles out, in two steps:

1. **Slots, at publish time.** For every parcel and every number of units it holds in any of the 102 runs, `synthpop publish` lays out that many points spread evenly over the parcel: a fine hexagonal grid of candidate points inside the parcel boundary (about six per unit, at least 60), grouped into one cluster per unit by k-means, each slot the candidate nearest its cluster's centre. A lone household sits at the point deepest inside the parcel. Each slot gets the room a circle there can take without reaching the parcel edge or the next slot's circle. The result is `data/out/household_slots.parquet` (34,228 layouts, 3.4 MB, about a minute of the publish).
2. **Placing, per run.** When the app asks for a run's people, the parcel's units go into its slots, largest unit in the roomiest slot. A circle's radius is 3 m × √(people), so its area is proportional to household size; where the largest would not fit, every circle on that parcel shrinks by the same factor, so sizes stay comparable within a parcel and circles never overlap.

The people document gains `head` (1 for the householder, from PUMS `RELSHIPP` 20), `unit` (each person's circle) and `units` (each circle's centre and radius in metres). A person's `lon`/`lat` in the table and CSV stay their E911 address point; the circle is only where the map draws the household.

A circle's place on its parcel means nothing beyond the parcel: the model places households on parcels, not on units within a building.

## Race, education, poverty and employment

Every synthetic person is a whole copy of a seed person, so all four come with them. Each is also *calibrated*: a variable with a block-group constraint in `config/variables.yaml`, so the fit matches each block group's published ACS counts within their margins. The summary, API, map, filters, validation report and data-point risk factors pick them up from the YAML with no code change.

| Column | PUMS field | ACS block-group table | Universe |
|---|---|---|---|
| `RACE` | `RAC1P`, `HISP` | B03002 | Everyone |
| `EDUCATION` | `SCHL` | B15003 | 25 and over |
| `POVERTY`, `POVERTY_RATIO` | `POVPIP` | C17002 | Poverty status determined: not people in institutions, college dorms or military quarters, nor unrelated children under 15 (`POVPIP` is empty for them, matching the table) |
| `EMPLOYMENT` | `ESR` | B23025 | 16 and over |

### Why poverty and employment are calibrated

Carrying `POVPIP` and `ESR` through from the seed without a constraint was tested first. The local mix then follows only from what is calibrated (age, sex, race, education, household size, tenure, building type), and that misses a lot:

| Main run, 61 block groups | Carried through | Calibrated |
|---|---|---|
| Poverty cells inside the ACS 90% margin | 84% | 100% |
| Employment cells inside the ACS 90% margin | 87% | 100% |
| Block-group poverty rate, correlation with ACS | 0.74 | 0.99 |
| Block-group unemployment rate, correlation with ACS | 0.38 (one PUMA-wide rate does about as well) | 0.99 |
| Tract rates (never fitted), correlation with ACS: poverty, unemployment | 0.91, 0.42 | 1.00, 0.99 |
| Existing constraints inside the margin | 100% | 100%, median RAE unchanged |

The tract check is not fully independent: tract and block-group ACS estimates come from the same responses. Values stay coherent within each person, because whole seed people are copied: 19% of renters are below poverty against 3% of owners, and 45% of adults without a diploma against 5% with a degree.

### Limits

- **Thin seed for unemployment.** The PUMA seed has 96 unemployed people (and 4,185 people in all), so a block group's unemployed are copies of a small set of real profiles.
- **Wide ACS margins.** Block-group margins are about ±7 points for the poverty rate and ±4 for the unemployment rate, so the fit follows noisy targets. The 80 replicate runs carry that into every estimate's margin.
- **Race detail.** `other` holds American Indian, Pacific Islander, some other race and two or more races. Two or more races is most of it (4,062 people in the city; 211 seed people) and could become its own calibrated category, but that changes `race_eth`'s categories, which saved data points use. American Indian (6 seed people) and Pacific Islander (none) are too thin to calibrate.
- **Education under 25** is not in B15003, so it stays empty.
- Adding the two constraints changed which seed households are drawn: the main run now has 68,754 people in 33,338 households (68,769 in 33,287 before). City totals stay inside their ACS margins (docs/validation.md).
