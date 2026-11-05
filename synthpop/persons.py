"""One run's synthetic persons as a flat table, one row per person.

    table = person_table(run_persons, seed_persons)          # person_id, household_id, parcel_id, AGE, SEX
    where = locate(run_persons, e911, parcels, rng)          # person_id, lon, lat, location

Each synthetic person is a copy of one seed (PUMS) person, named by seed_serial and
seed_person. Columns in FIELDS are copied from that seed person, so exact age comes
out here although the run itself only keeps 5-year age groups.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from .model.place import assign_points, is_residential

FIELDS = {"AGE": "AGEP", "SEX": "SEX"}            # output column -> PUMS person column
LABELS = {"SEX": {1: "male", 2: "female"}}        # PUMS codes shown as words


def person_table(persons: pd.DataFrame, seed: pd.DataFrame, fields: dict[str, str] = FIELDS) -> pd.DataFrame:
    """Group-quarters residents belong to no household, so their household_id is empty."""
    if "seed_serial" not in persons.columns:
        raise ValueError("this run has no seed_serial column; re-run `synthpop run` to add it")
    keys = ["SERIALNO", "SPORDER"]
    joined = persons[["person_id", "household_id", "parcel_id", "seed_serial", "seed_person"]].merge(
        seed[keys + list(fields.values())], left_on=["seed_serial", "seed_person"], right_on=keys,
        how="left", validate="many_to_one")
    out = pd.DataFrame({
        "person_id": joined["person_id"],
        "household_id": joined["household_id"].where(joined["household_id"] >= 0).astype("Int64"),
        "parcel_id": joined["parcel_id"],
    })
    for name, col in fields.items():
        out[name] = joined[col].map(LABELS[name]) if name in LABELS else joined[col]
    return out


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
