# 2. IPF now, P-MEDM later

Population synthesis uses iterative proportional fitting (IPF) for each block group, rather than P-MEDM, the method UrbanPop uses (Tuccillo et al. 2023).

P-MEDM is better here: it fits within each ACS estimate's margin of error instead of forcing an exact match, and it returns allocation probabilities. But no maintained Python implementation exists, and the project was due the next day. IPF is quick to build and easy to check.

The two methods sit behind the same `model.run(inputs, run)` interface, so moving to P-MEDM changes only the inside of `model`. The trade-off is that IPF will over-fit noisy block-group values. Validation reports this per constraint, as relative allocation error.
