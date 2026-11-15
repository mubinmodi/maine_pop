import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from synthpop.households import CRS, R1, place, slots, unit_counts


def _parcels():
    """Two parcels in metres (UTM 19N) near Portland: a 40 m square and a 60 x 20 m strip."""
    x0, y0 = 398000, 4835000
    g = gpd.GeoDataFrame({"parcel_id": ["A", "B"]}, geometry=[shapely.box(x0, y0, x0 + 40, y0 + 40),
                                                              shapely.box(x0 + 100, y0, x0 + 160, y0 + 20)], crs=CRS)
    return g.to_crs(4326)


def _persons():
    # Parcel A: households 1 (3 people), 2 (1), 3 (2) and two group-quarters residents.
    # Parcel B: household 4 (5 people).
    hh = [1, 1, 1, 2, 3, 3, -1, -1, 4, 4, 4, 4, 4]
    parcel = ["A"] * 8 + ["B"] * 5
    return pd.DataFrame({"person_id": range(len(hh)), "household_id": hh, "parcel_id": parcel,
                         "lon": -70.26, "lat": 43.66})


def test_slots_spread_units_evenly_inside_their_parcel():
    parcels = _parcels()
    table = slots(parcels, pd.DataFrame({"parcel_id": ["A", "A", "B"], "n": [1, 4, 2]}))
    assert sorted(table.groupby(["parcel_id", "n"]).size().items()) == [(("A", 1), 1), (("A", 4), 4), (("B", 2), 2)]
    shapes = parcels.set_index("parcel_id").to_crs(CRS).geometry
    pts = gpd.GeoSeries(gpd.points_from_xy(table["lon"], table["lat"]), crs=4326).to_crs(CRS)
    assert all(shapes[p].contains(pt) for p, pt in zip(table["parcel_id"], pts))
    one = pts[(table["parcel_id"] == "A").to_numpy() & (table["n"] == 1).to_numpy()].iloc[0]
    assert one.distance(shapes["A"].centroid) < 3                  # a lone household sits mid-parcel
    four = np.array([[p.x, p.y] for p in pts[((table["parcel_id"] == "A") & (table["n"] == 4)).to_numpy()]])
    d = np.linalg.norm(four[:, None] - four[None], axis=-1)[np.triu_indices(4, 1)]
    assert d.min() > 12                                           # four slots on a 40 m square, well apart
    assert (table["room"] > 0).all()


def test_units_are_households_and_each_parcels_group_quarters_sized_by_people():
    persons = _persons()
    assert unit_counts(persons).to_dict() == {"A": 4, "B": 1}
    table = slots(_parcels(), unit_counts(persons).rename("n").reset_index())
    unit, u = place(persons, table)
    # members of a household share a unit; the two group-quarters residents share one too
    assert len(set(unit[:3])) == 1 and unit[6] == unit[7] and len(set(unit)) == 5
    assert u["size"].tolist() == [3, 2, 2, 1, 5]                  # parcel A largest first, then parcel B
    # area proportional to size within a parcel, and no larger than R1·√size
    a = u[u["parcel_id"] == "A"]
    assert np.allclose(a["r"] / np.sqrt(a["size"]), a["r"].iloc[0] / np.sqrt(3), atol=0.02)
    assert (u["r"] <= R1 * np.sqrt(u["size"]) + 0.01).all()
    # circles on one parcel never overlap
    xy = gpd.GeoSeries(gpd.points_from_xy(a["lon"], a["lat"]), crs=4326).to_crs(CRS)
    xy = np.array([[p.x, p.y] for p in xy])
    r = a["r"].to_numpy()
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            assert np.linalg.norm(xy[i] - xy[j]) >= r[i] + r[j] - 0.05


def test_a_parcel_without_slots_keeps_its_units_on_the_address_point():
    persons = _persons()
    unit, u = place(persons, None)
    assert np.allclose(u["lon"], -70.26) and np.allclose(u["r"], R1 * np.sqrt(u["size"]), atol=0.01)
