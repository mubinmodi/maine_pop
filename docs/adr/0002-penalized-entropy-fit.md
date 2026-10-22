# 2. Penalized maximum-entropy fitting (P-MEDM style), not plain IPF

The plan was plain IPF first, with P-MEDM (the UrbanPop method) as a later upgrade. The first test showed that plain IPF and IPU can't fit person totals that cut across households. They scale every unit in a cell by the same factor, so they can't shift weight between households that differ only in size. On real Portland data the fit also missed totals by 20–25%.

So `model.fit.fit_weights` solves a penalized maximum-entropy problem, the same formulation as P-MEDM. It keeps weights as close as possible to the PUMS weights, in relative entropy weighted by n/N as P-MEDM does, while letting each ACS cell miss by about its standard error. It is solved in the dual with Newton's method: one small linear system per iteration (62 cells), about 10 iterations per block group.

On Portland's main run:
- 99.8% of block-group cells fall within 1 SE.
- 100% fall within the ACS 90% margin of error.
- City totals are within 0.7% for households and 2.2% for people.

A run takes under a second.

What is still missing compared with UrbanPop's P-MEDM: tract-level constraints alongside block-group ones, and allocation probabilities as an output. Both fit behind the same `model.run(inputs, run)` interface.
