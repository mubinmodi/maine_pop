"""Household circles for the map: every household drawn as a circle on its parcel,
spread over the parcel instead of stacked on its E911 address point, with the circle's
area proportional to the number of people in it.

Layout happens in two steps:

- `slots` (at publish time) lays out, for every parcel and every number of units `n` it
  holds in any run, n points spread evenly over the parcel: a fine grid of candidate
  points inside the parcel, grouped into n clusters (k-means), each slot the candidate
  nearest its cluster's centre. Each slot gets `room`, the radius a circle there can
  have without reaching the parcel edge or the next slot's circle.
- `place` (per run, when the app asks for people) puts the parcel's units in those
  slots, the largest unit in the roomiest slot, and sizes them: radius R1·√size metres,
  shrunk by one factor per parcel when the largest would not fit.

A unit is a household, or all the group-quarters residents of one parcel (a dorm or
nursing home), who live in no household.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import shapely

R1 = 3.0                  # metres: radius of a one-person household's circle
CRS = 26919               # UTM 19N, metres
_ITER = 8                 # k-means iterations


def units(persons: pd.DataFrame) -> pd.Series:
    """Each person's unit: their household, or -1 - (parcel row) for group quarters."""
    gq = persons["household_id"] < 0
    parcel_code = pd.factorize(persons["parcel_id"])[0]
    return pd.Series(np.where(gq, -1 - parcel_code, persons["household_id"]), index=persons.index)


def unit_counts(persons: pd.DataFrame) -> pd.Series:
    """Number of units on each parcel (index parcel_id)."""
    u = pd.DataFrame({"parcel_id": persons["parcel_id"].to_numpy(), "unit": units(persons).to_numpy()})
    return u.drop_duplicates().groupby("parcel_id").size()


def slots(parcels, needs: pd.DataFrame) -> pd.DataFrame:
    """Slots for every (parcel_id, n) in `needs`: parcel_id, n, k, lon, lat, room (metres)."""
    import geopandas as gpd

    shapes = gpd.GeoSeries(parcels.set_index("parcel_id").geometry, crs=parcels.crs or 4326).to_crs(CRS)
    rows = []
    for pid, ns in needs.groupby("parcel_id")["n"]:
        if pid not in shapes.index:
            continue
        poly = shapes[pid]
        if poly is None or poly.is_empty:
            continue
        for n in sorted(set(ns)):
            xy, room = _layout(poly, int(n))
            rows.append(pd.DataFrame({"parcel_id": pid, "n": n, "k": np.arange(len(xy)), "x": xy[:, 0], "y": xy[:, 1], "room": room}))
    if not rows:
        return pd.DataFrame(columns=["parcel_id", "n", "k", "lon", "lat", "room"])
    out = pd.concat(rows, ignore_index=True)
    ll = gpd.GeoSeries(gpd.points_from_xy(out["x"], out["y"]), crs=CRS).to_crs(4326)
    out["lon"], out["lat"] = ll.x.round(7), ll.y.round(7)
    out["room"] = out["room"].round(2)
    return out[["parcel_id", "n", "k", "lon", "lat", "room"]].astype({"n": "int32", "k": "int32"})


def _layout(poly, n: int) -> tuple[np.ndarray, np.ndarray]:
    """n points spread evenly over a polygon (metres), and the room around each."""
    cand = _grid(poly, n)
    edge = shapely.distance(poly.boundary, shapely.points(cand))
    if n == 1:
        xy = cand[[int(np.argmax(edge))]]               # the point deepest inside
    else:
        # Farthest-point start from the deepest point, then k-means over the candidates.
        pick = [int(np.argmax(edge))]
        d = np.linalg.norm(cand - cand[pick[0]], axis=1)
        for _ in range(1, n):
            pick.append(int(np.argmax(d)))
            d = np.minimum(d, np.linalg.norm(cand - cand[pick[-1]], axis=1))
        centres = cand[pick].astype(float)
        for _ in range(_ITER):
            lab = np.argmin(((cand[:, None, :] - centres[None]) ** 2).sum(-1), axis=1)
            for k in range(n):
                m = lab == k
                if m.any():
                    centres[k] = cand[m].mean(axis=0)
        # Snap each centre to a distinct candidate inside the parcel.
        taken: set[int] = set()
        idx = []
        for c in centres:
            order = np.argsort(((cand - c) ** 2).sum(-1))
            j = next((int(o) for o in order if int(o) not in taken), int(order[0]))
            taken.add(j)
            idx.append(j)
        xy = cand[idx]
    edge_xy = shapely.distance(poly.boundary, shapely.points(xy))
    if len(xy) > 1:
        dist = np.linalg.norm(xy[:, None, :] - xy[None], axis=-1)
        np.fill_diagonal(dist, np.inf)
        room = np.minimum(edge_xy, dist.min(axis=1) / 2)
    else:
        room = edge_xy
    return xy, np.maximum(room, 0.3)


def _grid(poly, n: int) -> np.ndarray:
    """A hexagonal grid of points inside the polygon: about six per unit and at least 60
    (and never fewer than n)."""
    minx, miny, maxx, maxy = poly.bounds
    g = max(0.3, float(np.sqrt(poly.area / max(6 * n, 60))))
    for _ in range(6):
        ys = np.arange(miny + g / 2, maxy, g * np.sqrt(3) / 2)
        xs = np.arange(minx + g / 2, maxx, g)
        X, Y = np.meshgrid(xs, ys)
        X = X + (np.arange(len(ys)) % 2)[:, None] * g / 2
        x, y = X.ravel(), Y.ravel()
        ok = shapely.contains_xy(poly, x, y)
        if ok.sum() >= n:
            return np.column_stack([x[ok], y[ok]])
        g /= 2
    p = poly.representative_point()
    return np.array([[p.x, p.y]] * n)


def place(persons: pd.DataFrame, slot_table: pd.DataFrame | None) -> tuple[np.ndarray, pd.DataFrame]:
    """Each person's unit index and the units: lon, lat, r (metres), size, parcel_id.

    Units are ordered by parcel, then size (largest first), then household id. A parcel
    without slots (no geometry, or a run with more units than any slot set) keeps its
    units on their members' address point."""
    key = units(persons).to_numpy()
    f = pd.DataFrame({"unit": key, "parcel_id": persons["parcel_id"].to_numpy(),
                      "lon": persons["lon"].to_numpy(), "lat": persons["lat"].to_numpy()})
    u = f.groupby("unit", sort=False).agg(parcel_id=("parcel_id", "first"), size=("lon", "size"),
                                          alon=("lon", "first"), alat=("lat", "first")).reset_index()
    u = u.sort_values(["parcel_id", "size", "unit"], ascending=[True, False, True], ignore_index=True)
    u["k"] = u.groupby("parcel_id").cumcount().astype("int32")
    u["n"] = u.groupby("parcel_id")["unit"].transform("size").astype("int32")
    if slot_table is not None and len(slot_table):
        # Roomiest slot first, so the largest unit gets it.
        s = slot_table.sort_values(["parcel_id", "n", "room"], ascending=[True, True, False]).copy()
        s["k"] = s.groupby(["parcel_id", "n"]).cumcount().astype("int32")
        u = u.merge(s[["parcel_id", "n", "k", "lon", "lat", "room"]], on=["parcel_id", "n", "k"], how="left")
    else:
        u[["lon", "lat", "room"]] = np.nan
    base = R1 * np.sqrt(u["size"].to_numpy())
    fit = (u["room"] / base).fillna(1.0)
    shrink = fit.groupby(u["parcel_id"]).transform("min").clip(upper=1.0)
    u["r"] = (base * shrink).round(2).clip(lower=0.3)
    u["lon"] = u["lon"].fillna(u["alon"])
    u["lat"] = u["lat"].fillna(u["alat"])
    index = pd.Series(np.arange(len(u)), index=u["unit"])
    return index.loc[key].to_numpy(), u[["lon", "lat", "r", "size", "parcel_id"]]
