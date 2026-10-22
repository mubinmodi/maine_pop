"""Parcel capacity (housing units per parcel) and placement of households and
group-quarters residents onto parcels."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd

NON_RESIDENTIAL = {
    "Commercial", "Office", "Post Office", "Fire Station", "School", "Parking", "Utility", "Health Care",
    "College - University", "EMS Station", "Church", "Restaurant", "Sports Complex", "Lodging", "Marina",
    "Vacant Lot", "Hospital", "Government - Municipal", "Park", "Library", "Tower", "Retail - Strip Mall",
    "Arena", "Attraction", "Financial Institution", "Nursing Home", "Law Enforcement - County",
    "Public Health Office", "Retail - Enclosed Mall", "Law Enforcement -Municipal", "Veterinarian", "Airport",
    "Law Enforcement - Federal", "PSAP", "Boat Landing", "Retail - Free Standing", "Cemetery", "Golf Club",
}
GROUP_QUARTERS_TYPES = {"Nursing Home", "College - University", "Health Care", "Hospital",
                        "Law Enforcement - County", "Lodging"}

CLASSES = ["single", "units_2_4", "units_5_19", "units_20_plus"]
FALLBACK = {
    "single": ["units_2_4", "units_5_19", "units_20_plus"],
    "units_2_4": ["single", "units_5_19", "units_20_plus"],
    "units_5_19": ["units_20_plus", "units_2_4", "single"],
    "units_20_plus": ["units_5_19", "units_2_4", "single"],
    "mobile_other": ["single", "units_2_4", "units_5_19", "units_20_plus"],
}


def unit_class(units: pd.Series) -> pd.Series:
    return pd.Series(np.select([units <= 1, units <= 4, units <= 19], CLASSES[:3], CLASSES[3]), index=units.index)


def parcel_base(parcels: gpd.GeoDataFrame, e911: gpd.GeoDataFrame, blocks: gpd.GeoDataFrame) -> pd.DataFrame:
    """One row per parcel inside a study block: block, block group, area, the raw
    E911 housing-unit count and whether it looks like a group-quarters site."""
    pts = parcels.copy()
    pts["geometry"] = parcels.geometry.representative_point()
    joined = gpd.sjoin(pts, blocks[["area_id", "block_group", "geometry"]], predicate="within", how="inner")
    base = pd.DataFrame({
        "parcel_id": joined["parcel_id"].to_numpy(),
        "block": joined["area_id"].to_numpy(),
        "block_group": joined["block_group"].to_numpy(),
        "area_m2": parcels.set_index("parcel_id").loc[joined["parcel_id"]].to_crs(26919).area.to_numpy(),
    }).drop_duplicates("parcel_id")

    pt = e911.copy()
    pt["parcel_id"] = assign_points(e911, parcels, set(base["parcel_id"]))
    pt = pt[pt["parcel_id"].notna()]

    ptype = pt["place_type"].fillna("").str.strip()
    unit = pt["unit"].fillna("").str.strip()
    pt["residential"] = ~ptype.isin(NON_RESIDENTIAL) & ~unit.str.upper().str.startswith("STE")
    pt["has_unit"] = unit != ""
    pt["gq_site"] = ptype.isin(GROUP_QUARTERS_TYPES)
    res = pt[pt["residential"]]
    agg = res.groupby("parcel_id").agg(points=("residential", "size"), unit_points=("has_unit", "sum"))
    agg["raw_units"] = np.where(agg["unit_points"] > 0, agg["unit_points"], agg["points"])
    base = base.merge(agg[["raw_units"]], left_on="parcel_id", right_index=True, how="left")
    base["raw_units"] = base["raw_units"].fillna(0).astype(int)
    base["gq_site"] = base["parcel_id"].isin(set(pt.loc[pt["gq_site"], "parcel_id"]))
    return base.reset_index(drop=True)


def assign_points(e911: gpd.GeoDataFrame, parcels: gpd.GeoDataFrame, parcel_ids: set[str]) -> pd.Series:
    """Parcel for each address point: its parcel key when that key exists, else the
    parcel it falls inside. None when neither lands on one of `parcel_ids`."""
    by_key = e911["map_bk_lot"].where(e911["map_bk_lot"].isin(parcel_ids))
    unkeyed = e911[by_key.isna()]
    if len(unkeyed):
        hit = gpd.sjoin(unkeyed[["geometry"]], parcels[["parcel_id", "geometry"]], predicate="within", how="left")
        hit = hit[~hit.index.duplicated()]
        by_key = by_key.fillna(hit["parcel_id"])
    return by_key.where(by_key.isin(parcel_ids))


def _largest_remainder(weights: np.ndarray, total: int) -> np.ndarray:
    if total <= 0 or weights.sum() <= 0:
        return np.zeros(len(weights), dtype=int)
    exact = weights / weights.sum() * total
    out = np.floor(exact).astype(int)
    short = total - out.sum()
    if short > 0:
        out[np.argsort(-(exact - out))[:short]] += 1
    return out


def capacity(base: pd.DataFrame, blocks: pd.DataFrame, holdout: frozenset[str] = frozenset()) -> pd.DataFrame:
    """Housing units per parcel, calibrated to 2020 block housing counts.

    Held-out blocks never see their own count: their parcels keep E911 units scaled
    by their block group's ratio from the blocks that are not held out.
    `fallback` marks parcels given units only because their block had housing but
    no addressed residential parcel.
    """
    cap = base.copy()
    cap["units"] = 0
    cap["fallback"] = False
    housing = blocks.set_index("area_id")["housing2020"]

    for block, idx in cap.groupby("block").groups.items():
        if block in holdout:
            continue
        rows = cap.loc[idx]
        target = int(housing.get(block, 0))
        if target == 0:
            continue
        if rows["raw_units"].sum() > 0:
            cap.loc[idx, "units"] = _largest_remainder(rows["raw_units"].to_numpy(float), target)
        else:                                   # housing but no addressed home: use the largest lot
            big = rows["area_m2"].idxmax()
            cap.loc[big, "units"] = target
            cap.loc[big, "fallback"] = True

    held = cap["block"].isin(holdout)
    if held.any():
        known = cap[~held].groupby("block_group").agg(units=("units", "sum"), raw=("raw_units", "sum"))
        ratio = (known["units"] / known["raw"].replace(0, np.nan)).fillna(1.0)
        for bg, idx in cap[held].groupby("block_group").groups.items():
            rows = cap.loc[idx]
            cap.loc[idx, "units"] = np.round(rows["raw_units"] * ratio.get(bg, 1.0)).astype(int)
    cap["cls"] = unit_class(cap["units"])
    return cap


def place_households(households: pd.DataFrame, cap: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Give each household (`household_id`, `block_group`, `structure`) a parcel.

    Same-structure units first, then the nearest structure class, then any parcel
    in the block group over capacity. Returns parcel_id, match ("exact",
    "nearest", "overflow") per household.
    """
    out = []
    for bg, hh in households.groupby("block_group", sort=False):
        parcels = cap[(cap["block_group"] == bg) & (cap["units"] > 0)]
        free = {c: list(rng.permutation(np.repeat(g["parcel_id"].to_numpy(), g["units"].to_numpy())))
                for c, g in parcels.groupby("cls")}
        hh = hh.sample(frac=1, random_state=int(rng.integers(1 << 31)))
        structure = hh["structure"].astype(str).to_numpy()
        parcel_of = np.empty(len(hh), dtype=object)
        match = np.empty(len(hh), dtype=object)
        # Exact matches in bulk, then a per-household pass over the few left over.
        for c in CLASSES:
            idx = np.flatnonzero(structure == c)
            k = min(len(idx), len(free.get(c, [])))
            if k:
                parcel_of[idx[:k]] = free[c][-k:]
                del free[c][-k:]
                match[idx[:k]] = "exact"
        for i in np.flatnonzero(pd.isna(parcel_of)):
            for c in FALLBACK.get(structure[i], CLASSES):
                if free.get(c):
                    parcel_of[i] = free[c].pop()
                    match[i] = "nearest"
                    break
            else:
                if len(parcels):
                    p = parcels["units"].to_numpy(float)
                    parcel_of[i] = rng.choice(parcels["parcel_id"].to_numpy(), p=p / p.sum())
                else:
                    in_bg = cap[cap["block_group"] == bg]
                    parcel_of[i] = in_bg.loc[in_bg["area_m2"].idxmax(), "parcel_id"]
                match[i] = "overflow"
        out.append(pd.DataFrame({"household_id": hh["household_id"].to_numpy(), "parcel_id": parcel_of, "match": match}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["household_id", "parcel_id", "match"])


def gq_block_weights(blocks: pd.DataFrame, avg_household_pop: pd.Series, holdout: frozenset[str]) -> pd.Series:
    """How likely each block is to hold group quarters: 2020 population above what
    its housing units would hold at the block group's people-per-unit rate."""
    b = blocks.set_index("area_id")
    expected = b["housing2020"] * b["block_group"].map(avg_household_pop).fillna(2.0)
    excess = (b["pop2020"] - expected).clip(lower=0)
    excess[excess.index.isin(holdout)] = 0
    return excess


def place_group_quarters(persons: pd.DataFrame, cap: pd.DataFrame, block_weight: pd.Series,
                         rng: np.random.Generator) -> pd.Series:
    """Parcel for each group-quarters resident (`block_group` column): pick a block
    by excess-population weight, then the block's group-quarters site or, failing
    that, its largest parcel."""
    site = (cap.sort_values(["gq_site", "area_m2"], ascending=False).drop_duplicates("block")
               .set_index("block")["parcel_id"])
    out = pd.Series(index=persons.index, dtype=object)
    for bg, grp in persons.groupby("block_group"):
        blocks_in = cap.loc[cap["block_group"] == bg, "block"].unique()
        w = block_weight.reindex(blocks_in).fillna(0).to_numpy(float)
        if w.sum() == 0:
            sites = cap[(cap["block_group"] == bg) & cap["gq_site"]]["block"].unique()
            blocks_in = sites if len(sites) else blocks_in
            w = np.ones(len(blocks_in))
        chosen = rng.choice(blocks_in, size=len(grp), p=w / w.sum())
        out.loc[grp.index] = site.reindex(chosen).to_numpy()
    return out
