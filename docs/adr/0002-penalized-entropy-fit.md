# 2. Penalized maximum-entropy fitting (P-MEDM style), not plain IPF

The plan was plain IPF first, with P-MEDM (the UrbanPop method) as a later upgrade. The first test showed that plain IPF and IPU can't fit person totals that cut across households. They scale every unit in a cell by the same factor, so they can't shift weight between households that differ only in size. On real Portland data the fit also missed totals by 20–25%.

So `model.fit.fit_weights` solves a penalized maximum-entropy problem, the same formulation as P-MEDM. It keeps weights as close as possible to the PUMS weights, in relative entropy weighted by n/N as P-MEDM does, while letting each ACS cell miss by about its standard error. It is solved in the dual with Newton's method: one small linear system per iteration (62 cells), about 10 iterations per block group.

On Portland's main run:
- 99.8% of block-group cells fall within 1 SE.
- 100% fall within the ACS 90% margin of error.
- City totals are within 0.7% for households and 2.2% for people.

A run takes under a second.

## Update: tract and block groups fitted together

`model.fit.fit_tract` now fits all block groups of a tract at once. Each block group is still fitted to its own cells. The tract's own ACS totals are added as extra cells on the block groups' sum, which is P-MEDM's multilevel form. The objective is the sum of the block groups' separate objectives plus the tract cells' misses, so with no tract cells it gives exactly the old per-block-group fit (a test checks this).

The Hessian has a block structure, one A·diag(w/γ)·Aᵀ per block group, so a tract of four block groups costs about four separate fits. A main run went from 0.6 s to 0.8 s.

Only tracts whose block groups are all in the study area get tract cells: 19 of Portland's 20. Tract 23005002400 has one of its three block groups outside the city, so its block groups are fitted on their own. `model.fit_tracts: false` in `config/study_area.yaml` turns the joint fit off.

On Portland, against the published ACS (the validation report):
- Median tract RAE fell for every variable. For example, age 0.05 → 0.03, race/ethnicity 0.05 → 0.03, structure 0.10 → 0.07 and tenure 0.11 → 0.09.
- Block-group fit stayed at 99.9% inside the margin.
- The city 65+ margin fell from ±857 to ±547.
- Block-level coefficients of variation fell slightly: 80% of blocks are high confidence, up from 77%.

The gain is modest because ACS block-group estimates already add up closely to their tracts. The bigger payoff is that the fit can now take tables published only for tracts, such as disability and health insurance.

**Caveat on margins.** A tract estimate and its block groups' estimates come from the same survey responses. Treating them as independent cells, as P-MEDM does, counts some of that information twice. The narrower replicate margins are therefore somewhat optimistic. Replicate runs also perturb tract and block-group targets independently.

## Update: tables published only for tracts

A constraint with `level: tract` in `config/variables.yaml` has no block-group cells. `fit_tract` fits it on the block groups' sum alone (`bg_rows` marks the rows each block group is fitted to). The first such table is disability (C18108, by under 65 / 65+). Health insurance (B27010) turned out to be published for block groups too, so it is an ordinary constraint.

Disability by tract, compared with ACS C18108:
- Copied from the PUMS seed without fitting, 85% of tracts were inside the 90% margin, with a correlation of 0.86.
- Fitted, 100% are inside the margin, with a correlation of 1.00.

Uninsured people (B27010) went from 75% inside the margin (correlation 0.80) to 100% (1.00). The other variables' fit did not change.

The one tract not wholly in the study area (23005002400) gets no tract cells, so its disability shares come from the seed alone.

Still missing compared with UrbanPop's P-MEDM: allocation probabilities as an output.
