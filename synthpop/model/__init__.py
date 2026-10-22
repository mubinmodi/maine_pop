"""Synthesis and placement.

    inputs = model.prepare(data, catalog, area)     # once
    placed = model.run(inputs, Run.main())          # or Run.replicate(r), Run.validation(...)

Every run fits seed units to each block group's ACS totals (IPU), turns the
weights into whole households and group-quarters residents, and places every
household on exactly one parcel. Totals at every level therefore add up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..catalog import Catalog
from ..config import StudyArea
from .fit import Fit, fit_weights, integerize
from .place import capacity, gq_block_weights, parcel_base, place_group_quarters, place_households

__all__ = ["Run", "Inputs", "Placed", "prepare", "run", "holdout_blocks"]


@dataclass(frozen=True)
class Run:
    kind: str                         # "main", "replicate" or "validation"
    r: int = 0                        # replicate weight set, 1..80 for replicate runs
    holdout: frozenset[str] = frozenset()

    @classmethod
    def main(cls) -> "Run":
        return cls("main")

    @classmethod
    def replicate(cls, r: int) -> "Run":
        if not 1 <= r <= 80:
            raise ValueError("replicate runs are numbered 1 to 80")
        return cls("replicate", r)

    @classmethod
    def validation(cls, holdout: frozenset[str]) -> "Run":
        return cls("validation", 0, frozenset(holdout))

    @property
    def name(self) -> str:
        return self.kind if self.kind != "replicate" else f"replicate_{self.r:02d}"


@dataclass
class Inputs:
    catalog: Catalog
    area: StudyArea
    units: pd.DataFrame               # one row per seed unit: unit kind, serial, structure
    members: pd.DataFrame             # seed persons with categories and their unit index
    A: np.ndarray                     # cells x units incidence
    cells: pd.DataFrame               # constraint, key, unit, acs cells
    weights: np.ndarray               # 81 x units: column 0 main weights, then replicates 1..80
    targets: pd.DataFrame             # block groups x cells, estimates
    target_se: pd.DataFrame           # block groups x cells, standard errors
    parcels: pd.DataFrame             # parcel_base(): block, block group, raw units, gq site
    blocks: pd.DataFrame              # 2020 blocks without geometry
    avg_household_pop: pd.Series      # people per housing unit, by block group (ACS)


@dataclass
class Placed:
    run: Run
    households: pd.DataFrame          # household_id, seed_serial, parcel_id, block, block_group, match, category columns
    persons: pd.DataFrame             # person_id, household_id, parcel_id, block, block_group, category columns
    capacity: pd.DataFrame            # units per parcel used by this run
    fits: pd.DataFrame                # per block group: iterations, converged, max error
    fitted: pd.DataFrame = field(default=None)   # block groups x cells, synthetic totals after integerizing


# ---------------------------------------------------------------------- prepare

def prepare(data: dict, catalog: Catalog, area: StudyArea) -> Inputs:
    persons, households = catalog.apply(data["pums_persons"], data["pums_households"])

    hh_units = households[(households["TYPEHUGQ"] == 1) & (households["NP"] > 0)].copy()
    hh_units["kind"] = "household"
    gq_people = persons[persons["living_quarters"] == "group_quarters"].copy()
    gq_units = pd.DataFrame({"SERIALNO": gq_people["SERIALNO"].to_numpy(), "kind": "group_quarters",
                             "SPORDER": gq_people["SPORDER"].to_numpy()})
    units = pd.concat([hh_units.assign(SPORDER=0), gq_units], ignore_index=True)
    units["unit"] = np.arange(len(units))

    hh_index = dict(zip(hh_units["SERIALNO"], units.loc[units["kind"] == "household", "unit"]))
    members = persons.copy()
    members["unit"] = members["SERIALNO"].map(hh_index)
    gq_mask = members["living_quarters"] == "group_quarters"
    members.loc[gq_mask, "unit"] = units.loc[units["kind"] == "group_quarters", "unit"].to_numpy()
    members = members[members["unit"].notna()].copy()
    members["unit"] = members["unit"].astype(int)

    cells = []
    for c in catalog.constraints:
        for key, acs in c.cells.items():
            cells.append({"constraint": c.id, "key": key, "unit_kind": c.unit, "by": c.by, "acs": acs})
    cells = pd.DataFrame(cells)

    A = np.zeros((len(cells), len(units)))
    unit_cats = units.merge(households, on="SERIALNO", how="left", suffixes=("", "_h"))
    for i, cell in cells.iterrows():
        values = cell["key"].split("|")
        if cell["unit_kind"] == "household":
            mask = np.ones(len(units), dtype=bool) & (units["kind"] == "household").to_numpy()
            for col, val in zip(cell["by"], values):
                mask &= (unit_cats[col].astype(str) == val).to_numpy()
            A[i] = mask
        else:
            m = np.ones(len(members), dtype=bool)
            for col, val in zip(cell["by"], values):
                m &= (members[col].astype(str) == val).to_numpy()
            A[i] = np.bincount(members.loc[m, "unit"], minlength=len(units))

    weights = np.zeros((81, len(units)))
    is_hh = (units["kind"] == "household").to_numpy()
    gq_w = gq_people.reset_index(drop=True)
    for r in range(81):
        hcol, pcol = ("WGTP", "PWGTP") if r == 0 else (f"WGTP{r}", f"PWGTP{r}")
        weights[r, is_hh] = hh_units[hcol].to_numpy()
        weights[r, ~is_hh] = gq_w[pcol].to_numpy()

    acs = data["acs"][data["acs"]["level"] == "block_group"]
    est = acs.pivot(index="area_id", columns="cell", values="estimate")
    moe = acs.pivot(index="area_id", columns="cell", values="moe")
    targets, se = cell_totals(cells, est, moe)

    blocks = pd.DataFrame(data["blocks"].drop(columns="geometry"))
    base = parcel_base(data["parcels"], data["e911"], data["blocks"])
    occupied = est["B25002_002"].replace(0, np.nan)
    avg_household_pop = (est["B25008_001"] / occupied).fillna(2.0)
    return Inputs(catalog, area, units, members, A, cells, weights, targets, se, base, blocks, avg_household_pop)


def cell_totals(cells: pd.DataFrame, est: pd.DataFrame, moe: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Sum (and subtract) ACS cells for every constraint cell. MOEs combine as the
    root sum of squares, the Census approximation for sums and differences."""
    t, s = {}, {}
    for i, cell in cells.iterrows():
        total = 0
        var = 0
        for ref in cell["acs"]:
            sign = -1 if ref.startswith("-") else 1
            ref = ref.lstrip("-")
            total = total + sign * est[ref].fillna(0)
            var = var + (moe[ref].fillna(0) / 1.645) ** 2
        t[i] = total.clip(lower=0)
        s[i] = np.sqrt(var)
    return pd.DataFrame(t), pd.DataFrame(s)


def holdout_blocks(inputs: Inputs, fraction: float, seed: int) -> frozenset[str]:
    """A random share of blocks with housing, used only by the validation run."""
    eligible = inputs.blocks.loc[inputs.blocks["housing2020"] > 0, "area_id"].sort_values().to_numpy()
    rng = np.random.default_rng(seed)
    return frozenset(rng.choice(eligible, size=int(round(len(eligible) * fraction)), replace=False))


# ---------------------------------------------------------------------- run

def run(inputs: Inputs, spec: Run) -> Placed:
    rng = np.random.default_rng([inputs.area.random_seed, {"main": 0, "replicate": 1, "validation": 2}[spec.kind], spec.r])
    w0 = inputs.weights[spec.r]
    is_hh = (inputs.units["kind"] == "household").to_numpy()

    counts, fits, fitted = {}, [], {}
    for bg in inputs.targets.index:
        t = inputs.targets.loc[bg].to_numpy(float)
        if spec.kind == "replicate":
            # SDR replicates have half the target's SE each; perturbing targets this way
            # carries ACS sampling error into the replicate spread (4/80 · Σ = variance).
            t = np.clip(t + rng.normal(0, inputs.target_se.loc[bg].to_numpy(float) / 2), 0, None)
        fit: Fit = fit_weights(inputs.A, w0, t, inputs.target_se.loc[bg].to_numpy(float))
        n = np.zeros(len(w0), dtype=int)
        n[is_hh] = integerize(fit.weights[is_hh], rng)
        n[~is_hh] = integerize(fit.weights[~is_hh], rng)
        counts[bg] = n
        fitted[bg] = inputs.A @ n
        fits.append({"block_group": bg, "iterations": fit.iterations, "converged": fit.converged,
                     "max_rel_error": fit.max_rel_error})

    units = inputs.units
    rows = []
    for bg, n in counts.items():
        idx = np.repeat(np.flatnonzero(n), n[n > 0])
        rows.append(pd.DataFrame({"unit": idx, "block_group": bg}))
    drawn = pd.concat(rows, ignore_index=True)
    drawn["copy_id"] = np.arange(len(drawn))
    drawn = drawn.merge(units[["unit", "kind", "SERIALNO", "structure"]], on="unit")

    cap = capacity(inputs.parcels, inputs.blocks, spec.holdout)
    parcel_info = cap.set_index("parcel_id")[["block"]]

    hh = drawn[drawn["kind"] == "household"].rename(columns={"copy_id": "household_id", "SERIALNO": "seed_serial"})
    placed = place_households(hh[["household_id", "block_group", "structure"]], cap, rng)
    hh = hh.merge(placed, on="household_id")
    hh["block"] = parcel_info.loc[hh["parcel_id"], "block"].to_numpy()

    gq = drawn[drawn["kind"] == "group_quarters"].copy()
    weights = gq_block_weights(inputs.blocks, inputs.avg_household_pop, spec.holdout)
    gq["parcel_id"] = place_group_quarters(gq, cap, weights, rng).to_numpy() if len(gq) else []
    gq["block"] = parcel_info.loc[gq["parcel_id"], "block"].to_numpy() if len(gq) else []

    person_cols = [v.id for v in inputs.catalog.active]
    m = inputs.members[["unit"] + person_cols]
    hp = hh[["household_id", "unit", "parcel_id", "block", "block_group"]].merge(m, on="unit")
    gp = gq[["copy_id", "unit", "parcel_id", "block", "block_group"]].merge(m, on="unit").drop(columns="copy_id")
    gp["household_id"] = -1
    persons = pd.concat([hp, gp], ignore_index=True).drop(columns="unit")
    persons.insert(0, "person_id", np.arange(len(persons)))

    hh_cols = [v.id for v in inputs.catalog.active if v.unit == "household"]
    hh_cats = inputs.units[["unit"] + hh_cols]
    households = hh.drop(columns=["structure"]).merge(hh_cats, on="unit").drop(columns=["unit", "kind"])

    fitted_df = pd.DataFrame(fitted).T
    fitted_df.columns = inputs.targets.columns
    return Placed(spec, households, persons, cap, pd.DataFrame(fits), fitted_df)
