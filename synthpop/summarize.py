"""Area stats: the one long table every later step reads (ADR 0001).

    stats = summarize([main, *draws], replicates, catalog, city_id)   # synthetic rows
    pub = published(acs, catalog)                            # published ACS rows, same columns

Columns: level, area_id, variable, category, estimate, se, share, share_se,
source, confidence. `share` is the estimate over the variable's universe in that
area (all categories summed, so education shares are of people 25+).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .catalog import Catalog

LEVEL_COLUMNS = {"parcel": "parcel_id", "block": "block", "block_group": "block_group",
                 "tract": "tract", "city": "city"}
SYNTHETIC_LEVELS = list(LEVEL_COLUMNS)
TOTALS = {"population": "People", "households": "Households", "housing_units": "Housing units"}
KEY = ["level", "area_id", "variable", "category"]
COLUMNS = KEY + ["estimate", "se", "share", "share_se", "source", "confidence"]

# Coefficient-of-variation cut-offs commonly used to grade ACS reliability.
CV_HIGH, CV_MEDIUM = 0.12, 0.40


@dataclass
class RunData:
    persons: pd.DataFrame
    households: pd.DataFrame
    capacity: pd.DataFrame | None = None


def load_run(path: str | Path) -> RunData:
    d = Path(path)
    cap = d / "capacity.parquet"
    return RunData(pd.read_parquet(d / "persons.parquet"), pd.read_parquet(d / "households.parquet"),
                   pd.read_parquet(cap) if cap.exists() else None)


# ---------------------------------------------------------------------- synthetic

def counts(run: RunData, catalog: Catalog, city_id: str) -> pd.DataFrame:
    """estimate and universe for every (level, area, variable, category) in one run."""
    pp = run.persons.assign(tract=run.persons["block_group"].str[:11], city=city_id)
    hh = run.households.assign(tract=run.households["block_group"].str[:11], city=city_id)
    frames = []
    for level, col in LEVEL_COLUMNS.items():
        for df, unit, total in ((pp, "person", "population"), (hh, "household", "households")):
            n = df.groupby(col).size()
            frames.append(_rows(level, n, total, "total", n))
            for v in catalog.active:
                if v.unit != unit:                 # person variables over people, household ones over households
                    continue
                c = df.groupby([col, v.id], observed=True).size()
                universe = c.groupby(level=0).sum()
                frames.append(pd.DataFrame({
                    "level": level, "area_id": c.index.get_level_values(0).astype(str),
                    "variable": v.id, "category": c.index.get_level_values(1).astype(str),
                    "estimate": c.to_numpy(float),
                    "universe": universe.reindex(c.index.get_level_values(0)).to_numpy(float)}))
    if run.capacity is not None:
        cap = run.capacity.assign(tract=run.capacity["block_group"].str[:11], city=city_id)
        for level, col in LEVEL_COLUMNS.items():
            u = cap.groupby(col)["units"].sum()
            frames.append(_rows(level, u[u > 0], "housing_units", "total", u[u > 0]))
    out = pd.concat(frames, ignore_index=True)
    return out.set_index(KEY)


def _rows(level, values: pd.Series, variable, category, universe: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({"level": level, "area_id": values.index.astype(str), "variable": variable,
                         "category": category, "estimate": values.to_numpy(float),
                         "universe": universe.to_numpy(float)})


def summarize(draws: list[RunData], replicates: Iterable[RunData], catalog: Catalog, city_id: str) -> pd.DataFrame:
    """Area stats from placement draws of the main run (draws[0] is the main run
    itself) and from replicate runs.

    estimate  mean over the placement draws, so a single-family parcel shows its
              expected households (say 0.94), not one random 0 or 1. Means add up
              across levels just as counts do.
    se        sqrt(replicate variance + placement variance). Replicate variance is
              the successive-difference formula 4/80 · Σ (X_r − X_main)²; replicate
              runs share the main run's placement random numbers, so it measures
              survey uncertainty. Placement variance is the spread across draws.
    """
    first = counts(draws[0], catalog, city_id)
    x0 = first["estimate"]
    s0 = _share(first)

    sum_x, sum_xx = x0.copy(), x0 ** 2
    sum_s, sum_ss = s0.copy(), s0 ** 2
    for d in draws[1:]:
        c = counts(d, catalog, city_id)
        x, sh = c["estimate"], _share(c)
        sum_x, sum_xx = sum_x.add(x, fill_value=0), sum_xx.add(x ** 2, fill_value=0)
        sum_s, sum_ss = sum_s.add(sh, fill_value=0), sum_ss.add(sh ** 2, fill_value=0)
    k = len(draws)
    mean_x, mean_s = sum_x / k, sum_s / k
    var_place = ((sum_xx / k - mean_x ** 2) * k / (k - 1)).clip(lower=0) if k > 1 else mean_x * 0
    var_place_s = ((sum_ss / k - mean_s ** 2) * k / (k - 1)).clip(lower=0) if k > 1 else mean_s * 0

    ss_x = pd.Series(0.0, index=x0.index)
    ss_s = pd.Series(0.0, index=x0.index)
    n = 0
    for rep in replicates:
        r = counts(rep, catalog, city_id)
        ss_x = ss_x.add(r["estimate"].sub(x0, fill_value=0) ** 2, fill_value=0)
        ss_s = ss_s.add(_share(r).sub(s0, fill_value=0) ** 2, fill_value=0)
        n += 1
    factor = 4 / 80 * (80 / n) if n else 0.0      # rescale if fewer than 80 replicates were run

    idx = mean_x.index.union(ss_x.index)
    out = pd.DataFrame({
        "estimate": mean_x.reindex(idx, fill_value=0),
        "share": mean_s.reindex(idx, fill_value=0),
        "var_survey": factor * ss_x.reindex(idx, fill_value=0),
        "var_survey_s": factor * ss_s.reindex(idx, fill_value=0),
        "var_place": var_place.reindex(idx, fill_value=0),
        "var_place_s": var_place_s.reindex(idx, fill_value=0),
    }).reset_index()
    out = _survey_below_block_group(out, draws[0])
    out["se"] = np.sqrt(out.pop("var_survey") + out.pop("var_place"))
    out["share_se"] = np.sqrt(out.pop("var_survey_s") + out.pop("var_place_s"))
    out["source"] = "synthetic"
    out["confidence"] = confidence(out["estimate"], out["se"])
    if draws[0].capacity is not None:
        out = _parcel_quality(out, draws[0])
    return out[COLUMNS]


def _survey_below_block_group(out: pd.DataFrame, main: RunData) -> pd.DataFrame:
    """Survey uncertainty acts where the ACS constraints act, at block groups. Below
    them, replicate runs differ mostly by reshuffling households, which the 4/80
    formula would inflate. So blocks and parcels take their block group's survey
    variance in proportion to their share of it (squared), plus placement variance."""
    bg_of = pd.concat([
        main.households[["parcel_id", "block_group"]].drop_duplicates().rename(columns={"parcel_id": "area_id"})
        .assign(level="parcel"),
        main.households[["block", "block_group"]].drop_duplicates().rename(columns={"block": "area_id"})
        .assign(level="block"),
    ])
    if main.capacity is not None:
        bg_of = pd.concat([bg_of, main.capacity[["parcel_id", "block_group"]].rename(columns={"parcel_id": "area_id"})
                           .assign(level="parcel"),
                           main.capacity[["block", "block_group"]].rename(columns={"block": "area_id"}).assign(level="block")])
    bg_of = bg_of.drop_duplicates(["level", "area_id"])
    below = out["level"].isin(["parcel", "block"])
    sub = out[below].merge(bg_of, on=["level", "area_id"], how="left")
    bg = out[out["level"] == "block_group"].set_index(["area_id", "variable", "category"])
    key = pd.MultiIndex.from_arrays([sub["block_group"], sub["variable"], sub["category"]])
    bg_est = bg["estimate"].reindex(key).to_numpy()
    ratio = np.where(bg_est > 0, sub["estimate"].to_numpy() / np.where(bg_est > 0, bg_est, 1), 0)
    out.loc[below, "var_survey"] = ratio ** 2 * bg["var_survey"].reindex(key).fillna(0).to_numpy()
    out.loc[below, "var_survey_s"] = bg["var_survey_s"].reindex(key).fillna(0).to_numpy()
    return out


def _share(c: pd.DataFrame) -> pd.Series:
    return (c["estimate"] / c["universe"].replace(0, np.nan)).fillna(0)


def confidence(estimate: pd.Series, se: pd.Series) -> pd.Series:
    cv = se / estimate.where(estimate > 0)
    label = np.select([cv <= CV_HIGH, cv <= CV_MEDIUM], ["high", "medium"], "low")
    zero = estimate <= 0
    label = np.where(zero, np.where(se < 0.5, "high", "low"), label)
    return pd.Series(label, index=estimate.index)


def _parcel_quality(stats: pd.DataFrame, main: RunData) -> pd.DataFrame:
    """Parcels that only got units by fallback, or whose households overflowed
    their unit count, are never better than low / medium."""
    cap = main.capacity.set_index("parcel_id")
    overflow = main.households.groupby("parcel_id")["match"].apply(lambda m: (m == "overflow").any())
    fallback = set(cap.index[cap["fallback"]])
    over = set(overflow.index[overflow])
    is_parcel = stats["level"] == "parcel"
    low = is_parcel & stats["area_id"].isin(fallback)
    cap_medium = is_parcel & stats["area_id"].isin(over) & (stats["confidence"] == "high")
    stats.loc[low, "confidence"] = "low"
    stats.loc[cap_medium, "confidence"] = "medium"
    return stats


# ---------------------------------------------------------------------- published

def published(acs: pd.DataFrame, catalog: Catalog) -> pd.DataFrame:
    """Published ACS rows for every variable a constraint covers (including derived
    variables whose rules only read that constraint's variables), plus totals."""
    est = acs.pivot_table(index=["level", "area_id"], columns="cell", values="estimate", aggfunc="first")
    var = (acs.pivot_table(index=["level", "area_id"], columns="cell", values="moe", aggfunc="first") / 1.645) ** 2
    frames = []
    done = set()
    for c in catalog.constraints:
        refs = sorted({r.lstrip("-") for cells in c.cells.values() for r in cells})
        if not set(refs) <= set(est.columns):
            continue
        # Areas where the constraint's tables are published (tract-only tables have no block groups).
        has = est[refs].notna().any(axis=1)
        keys = pd.DataFrame([k.split("|") for k in c.cells], columns=list(c.by))
        e = pd.DataFrame({i: _cell_sum(est, refs) for i, refs in enumerate(c.cells.values())})[has]
        v = pd.DataFrame({i: _cell_sum(var, refs, squared=True) for i, refs in enumerate(c.cells.values())})[has]
        for vid in list(c.by) + [d.id for d in catalog.active if d.kind == "derived"]:
            if vid in done:
                continue
            if vid not in keys:
                try:
                    keys[vid] = catalog.recode(keys, vid).astype(object)
                except Exception:
                    continue                       # rule needs a variable this constraint lacks
                if keys[vid].isna().all():
                    keys.pop(vid)
                    continue
            done.add(vid)
            for cat in catalog.variable(vid).category_ids:
                idx = list(keys.index[keys[vid].astype(str) == cat])
                frames.append(_published_rows(e.index, vid, cat, e[idx].sum(axis=1), v[idx].sum(axis=1)))
    for vid, cell in (("population", "B01001_001"), ("households", "B25002_002"), ("housing_units", "B25002_001")):
        if cell in est:
            frames.append(_published_rows(est.index, vid, "total", est[cell], var[cell]))
    out = pd.concat(frames, ignore_index=True)
    universe = out.groupby(["level", "area_id", "variable"])["estimate"].transform("sum")
    universe_var = out.groupby(["level", "area_id", "variable"])["var"].transform("sum")
    totals = out["category"] == "total"
    p = (out["estimate"] / universe.replace(0, np.nan)).fillna(0)
    # Census formula for a proportion; fall back to the ratio formula when negative.
    inner = out["var"] - p ** 2 * universe_var
    inner = inner.where(inner >= 0, out["var"] + p ** 2 * universe_var)
    out["share"] = np.where(totals, 1.0, p)
    out["share_se"] = np.where(totals, 0.0, np.sqrt(inner) / universe.replace(0, np.nan)).astype(float)
    out["share_se"] = out["share_se"].fillna(0)
    out["se"] = np.sqrt(out.pop("var"))
    out["source"] = "published"
    out["confidence"] = confidence(out["estimate"], out["se"])
    return out[COLUMNS]


def _cell_sum(table: pd.DataFrame, refs, squared: bool = False) -> pd.Series:
    total = pd.Series(0.0, index=table.index)
    for ref in refs:
        sign = -1 if ref.startswith("-") and not squared else 1
        total = total + sign * table[ref.lstrip("-")].fillna(0)
    return total.clip(lower=0) if not squared else total


def _published_rows(index, variable, category, estimate, var) -> pd.DataFrame:
    return pd.DataFrame({"level": index.get_level_values(0), "area_id": index.get_level_values(1),
                         "variable": variable, "category": category,
                         "estimate": estimate.to_numpy(float), "var": var.to_numpy(float)})
