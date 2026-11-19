"""One run's synthetic persons as a flat table, one row per person.

    table = person_table(run_persons, seed_persons)          # the COLUMNS below
    where = locate(run_persons, e911, parcels, rng)          # person_id, lon, lat, location

Each synthetic person is a copy of one seed (PUMS) person, named by seed_serial and
seed_person. Columns in FIELDS are copied from that seed person, so exact age and the
exact poverty ratio come out here although the run itself keeps only categories.
Columns in CATEGORIES are the run's own categories, the ones the model fits to each
block group's ACS tables, so the table agrees with the map.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from .model.place import assign_points, is_residential

FIELDS = {"AGE": "AGEP", "SEX": "SEX", "POVERTY_RATIO": "POVPIP"}   # output column -> PUMS person column
LABELS = {"SEX": {1: "male", 2: "female"}}                           # PUMS codes shown as words
CATEGORIES = {"RACE": "race_eth", "EDUCATION": "education",          # output column -> run category column
              "POVERTY": "poverty", "EMPLOYMENT": "employment",
              "DISABILITY": "disability", "HEALTH_INSURANCE": "health_insurance"}
COLUMNS = ["person_id", "family_id", "parcel_id", "AGE", "SEX", "RACE", "EDUCATION",
           "POVERTY", "POVERTY_RATIO", "EMPLOYMENT", "DISABILITY", "HEALTH_INSURANCE", "household_id"]

# PUMS RELSHIPP codes for the householder (20) and the householder's relatives by birth,
# marriage or adoption: spouses (21, 23), children (25-27), siblings (28), parents (29),
# grandchildren (30), in-laws (31, 32) and other relatives (33). Unmarried partners (22, 24),
# roommates (34), foster children (35) and other nonrelatives (36) are not family members.
HOUSEHOLDER = 20
RELATIVES = {21, 23, *range(25, 34)}


def person_table(persons: pd.DataFrame, seed: pd.DataFrame, fields: dict[str, str] = FIELDS,
                 categories: dict[str, str] = CATEGORIES) -> pd.DataFrame:
    """Blank values are outside a column's universe:
    - household_id: group-quarters residents, who live in no household
    - family_id: everyone outside a Census family (see family_ids)
    - EDUCATION: under 25; EMPLOYMENT: under 16
    - DISABILITY, HEALTH_INSURANCE: people in institutions and active-duty military
    - POVERTY, POVERTY_RATIO: people whose poverty status is not determined (institutions,
      college dorms, military quarters, unrelated children under 15)
    """
    missing = [c for c in fields.values() if c not in seed.columns]
    if missing:
        raise ValueError(f"the cached PUMS persons lack {missing}; refetch with `synthpop fetch --only pums_persons`")
    joined = _join_seed(persons, seed, list(fields.values()))
    out = pd.DataFrame({
        "person_id": joined["person_id"],
        "family_id": family_ids(persons, seed).reset_index(drop=True),
        "parcel_id": joined["parcel_id"],
    })
    for name, col in fields.items():
        out[name] = joined[col].map(LABELS[name]) if name in LABELS else joined[col].astype("Int64")
    for name, col in categories.items():
        if col in persons.columns:
            out[name] = persons[col].astype("string").to_numpy()
    out["household_id"] = joined["household_id"].where(joined["household_id"] >= 0).astype("Int64")
    return out[[c for c in COLUMNS if c in out.columns] + [c for c in out.columns if c not in COLUMNS]]


def family_ids(persons: pd.DataFrame, seed: pd.DataFrame) -> pd.Series:
    """The Census family each person belongs to, aligned with `persons` (Int64, empty for none).

    A Census family is a householder plus everyone in the household related to them by
    birth, marriage or adoption. A household with no such relative (someone living alone,
    roommates, an unmarried couple with no children) holds no family, and nonrelatives in a
    family household (partners, roommates, foster children) are not in the family. Group-
    quarters residents have no family. Synthetic households copy one seed household whole,
    so the family is named by its household: family_id equals household_id.
    """
    rel = _join_seed(persons, seed, ["RELSHIPP"])
    in_hh = rel["household_id"].to_numpy() >= 0
    code = rel["RELSHIPP"].to_numpy()
    relative = in_hh & np.isin(code, list(RELATIVES))
    has_relative = pd.Series(relative).groupby(rel["household_id"].to_numpy()).transform("any").to_numpy()
    member = has_relative & (relative | (in_hh & (code == HOUSEHOLDER)))
    return rel["household_id"].where(member).astype("Int64").set_axis(persons.index).rename("family_id")


def _join_seed(persons: pd.DataFrame, seed: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """persons' ids with the named columns of the seed person each one copies, in persons' order."""
    if "seed_serial" not in persons.columns:
        raise ValueError("this run has no seed_serial column; re-run `synthpop run` to add it")
    keys = ["SERIALNO", "SPORDER"]
    return persons[["person_id", "household_id", "parcel_id", "seed_serial", "seed_person"]].merge(
        seed[keys + cols], left_on=["seed_serial", "seed_person"], right_on=keys,
        how="left", validate="many_to_one")


def locate(persons: pd.DataFrame, e911: gpd.GeoDataFrame, parcels: gpd.GeoDataFrame,
           rng: np.random.Generator) -> pd.DataFrame:
    """A point for every person: one of its parcel's E911 address points.

    A household's members share one point, and a group-quarters resident gets one of
    their own. A parcel's residential points are used when it has any, else any point
    on it (nursing homes and dorms are typed as non-residential). Points are dealt out
    in random order, so households share a point only once every point is taken.
    A parcel with no address point gets a uniform random point inside its boundary
    (location "parcel" rather than "address").
    """
    units = pd.DataFrame({
        "person_id": persons["person_id"].to_numpy(),
        "unit": np.where(persons["household_id"] >= 0, persons["household_id"], -1 - persons["person_id"]),
        "parcel_id": persons["parcel_id"].to_numpy(),
    })
    homes = units.drop_duplicates("unit").sample(frac=1, random_state=rng).reset_index(drop=True)
    homes["k"] = homes.groupby("parcel_id").cumcount()

    pts = e911.assign(parcel_id=assign_points(e911, parcels, set(homes["parcel_id"])), residential=is_residential(e911))
    pts = pts[pts["parcel_id"].notna()]
    any_res = pts.groupby("parcel_id")["residential"].transform("any")
    pts = pts[pts["residential"] | ~any_res].sample(frac=1, random_state=rng).reset_index(drop=True)
    pts = pd.DataFrame({"parcel_id": pts["parcel_id"], "lon": pts.geometry.x, "lat": pts.geometry.y,
                        "j": pts.groupby("parcel_id").cumcount()})
    n = pts.groupby("parcel_id").size().rename("n")

    homes = homes.merge(n, left_on="parcel_id", right_index=True, how="left")
    homes["j"] = homes["k"] % homes["n"].fillna(1).astype(int)
    homes = homes.merge(pts, on=["parcel_id", "j"], how="left")
    homes["location"] = np.where(homes["lon"].notna(), "address", "parcel")

    missing = homes["lon"].isna()
    if missing.any():
        shapes = parcels.set_index("parcel_id").geometry
        xy = [_inside(shapes[p], rng) for p in homes.loc[missing, "parcel_id"]]
        homes.loc[missing, ["lon", "lat"]] = xy

    out = units[["person_id", "unit"]].merge(homes[["unit", "lon", "lat", "location"]], on="unit")
    out[["lon", "lat"]] = out[["lon", "lat"]].round(6)
    return out.drop(columns="unit")


def _inside(shape, rng: np.random.Generator) -> tuple[float, float]:
    """Uniform random point inside a polygon, by rejection from its bounding box."""
    x0, y0, x1, y1 = shape.bounds
    for _ in range(1000):
        x, y = rng.uniform(x0, x1), rng.uniform(y0, y1)
        if shape.contains(Point(x, y)):
            return x, y
    p = shape.representative_point()
    return p.x, p.y
