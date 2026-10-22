# Key risks and open questions

Deliverable 6. Numbers refer to the Portland build described in [validation.md](validation.md).

## Risks

Ordered by how much they could mislead someone using the map.

| # | Risk | Evidence today | Mitigation in place | Next step |
|---|---|---|---|---|
| 1 | **Crossed variables are not checked against any published figure.** "65+ without a diploma" (age × education) and the planned diabetes and obesity variables depend on PUMS joint distributions inside one PUMA, and the ACS publishes no block-group cross to check them against. | The validation report has no row for `age65_no_hs` | Standard errors include placement and replicate spread | Compare against ACS tract table B15001 (sex × age × education). Report crossed variables as "model-only" in the app. |
| 2 | **Parcel composition reads as fact.** A single-family parcel is one household drawn from a distribution, not the people who live there. | Median CV at parcel level: households 0.22, people 65+ 1.7 | Confidence grades, visible margins, banner and export labels, no person-level rows | Consider showing composition only from block level up (Q8) |
| 3 | **Housing units per parcel come from E911 addresses, not assessor records.** | 23% of address points have no usable parcel key (joined by location instead). 5.2% of households overflow their parcel's unit count. 1 parcel got units by fallback. | Calibrated to 2020 block housing counts; overflow parcels capped at medium confidence | Assessor data (units, year built, bedrooms) |
| 4 | **Group quarters are placed by heuristic.** Facility types come from E911 place types, and blocks are chosen by population above what their housing holds. This matters most for people 65+ in nursing homes and for students. | 1,959 synthetic vs 1,959 ACS in total, but which parcels they land on is untested | Living-quarters totals are fitted exactly | 2020 DHC block group-quarters counts; HIFLD facility lists; bed counts |
| 5 | **Time mismatch.** Blocks and housing units are from April 2020; ACS covers 2020–2024. | Synthetic housing units run 2.8% under ACS at city level | Validation compares shares within block groups, not counts | Calibrate units to ACS B25001 at block-group level, or use permit data |
| 6 | **One seed for three cities.** Portland, South Portland and Westbrook share PUMA 23-01000 (1,859 households). Household types rare in that PUMA are rare in every block group. | Not measured | Soft fit allows block groups to differ within their ACS margins | P-MEDM-style borrowing from neighbouring PUMAs for rare types |
| 7 | **Uncertainty is approximate.** The replicate method assumes target noise is half each cell's standard error, and gives blocks and parcels their block group's survey variance in proportion to their size. | City population ±506 synthetic vs ±52 ACS (90%), so conservative at the top. People 65+ ±749 vs ±772, which agrees. | Documented in the report; conservative rather than optimistic | Check against ACS tract margins systematically; per-run totals across PUMAs at scale |
| 8 | **Study-area edges.** The model covers the 61 block groups at least half inside Portland, which isn't the exact city line. TIGER tract boundaries include water. | City population 0.2% under the city's own ACS total (93 people) | Explained in the report | Cartographic boundary files for display; model the full county |
| 9 | **Source drift.** The ArcGIS services, Census FTP paths and the API's key requirement are outside our control. | The Census API began requiring a key during this build | All inputs cached; the fetcher seam has a test adapter | Pin vintages; add a source-schema check to `fetch` |
| 10 | **App not production-hardened.** | No authentication or rate limiting. The Dockerfile is unrun. GeoJSON delivery won't scale past one city. | Read-only API | Auth if not public; PMTiles and viewport values (scaling plan) |
| 11 | **National parcel data costs money or effort.** | No free national parcel layer; state portals vary | The `Fetcher` seam isolates per-state adapters | Decide between buying (Regrid) and building (state portals plus footprints) |

## Open questions

These need a decision from you or your team. My recommendation is first in each.

1. **Which use cases come first, and where does their data come from?** For diabetes and obesity, the options are CDC PLACES (model-based estimates per tract) or BRFSS microdata (state level, by age, sex, race and education). *Recommendation:* attach person-level probabilities from a BRFSS model by age, sex, race and education, then calibrate to PLACES tract rates. That makes it a model on top of a model, and its uncertainty has to be carried through.
2. **Is parcel-level composition needed, or is block level enough?** Block counts have a median CV of 0.09, while parcel composition is mostly low confidence. *Recommendation:* keep parcels for counts and land use, and show composition from blocks up.
3. **Can we get assessor data through work?** It's the biggest single improvement for parcels.
4. **Should we get a Census API key and add 2020 DHC block constraints?** *Recommendation:* yes. It's the cheapest large accuracy gain below block group.
5. **Who uses the app: internal staff only, or the public?** This decides authentication, hosting and how much detail parcels show.
6. **How often should it refresh?** ACS 5-year data comes out each December. *Recommendation:* one rebuild a year per vintage, with the old vintage kept for comparison.
7. **Should the study area grow to all of Cumberland County?** That would let county figures be synthetic instead of published only.
8. **Is there any local ground truth?** For example nursing-home bed counts, school enrollment or housing authority unit counts. These could check group quarters and age placement below block group, where nothing is checked today.
9. **For national scale: buy or build parcels?** The Regrid licence costs about $25K–65K a year. Building from state portals and footprints costs engineering time and gives uneven quality.
