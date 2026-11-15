import numpy as np
import pandas as pd
import pytest

from synthpop import model
from synthpop.model import Run
from synthpop.model.fit import fit_weights, integerize
from shapely.geometry import Point

from synthpop.model.place import assign_points
from synthpop.persons import family_ids, locate, person_table
from conftest import AREA


@pytest.fixture
def inputs(sources, catalog):
    return model.prepare(sources.load_all(), catalog, AREA)


def test_fit_hits_consistent_household_and_person_targets():
    # 3 household types; cells: households of type a, b; persons total.
    A = np.array([[1, 0, 0], [0, 1, 1], [1, 2, 3]], dtype=float)
    fit = fit_weights(A, np.ones(3), np.array([10, 20, 70]), se=np.full(3, 1e-3), min_se=1e-3)
    assert fit.converged
    np.testing.assert_allclose(A @ fit.weights, [10, 20, 70], rtol=1e-3)


def test_integerize_keeps_the_rounded_total():
    rng = np.random.default_rng(0)
    w = np.array([0.4, 1.7, 2.2, 0.7])
    assert integerize(w, rng).sum() == round(w.sum())


def test_every_household_lands_on_one_parcel_in_its_block_group(inputs):
    placed = model.run(inputs, Run.main())
    hh, cap = placed.households, placed.capacity.set_index("parcel_id")
    assert hh["household_id"].is_unique
    assert hh["parcel_id"].notna().all()
    assert (cap.loc[hh["parcel_id"], "block_group"].to_numpy() == hh["block_group"].to_numpy()).all()
    per_parcel = hh[hh["match"] != "overflow"].groupby("parcel_id").size()
    assert (per_parcel <= cap.loc[per_parcel.index, "units"]).all()


def test_people_match_their_households(inputs):
    placed = model.run(inputs, Run.main())
    in_hh = placed.persons[placed.persons["household_id"] >= 0]
    sizes = in_hh.groupby("household_id").size()
    expected = placed.households.set_index("household_id")["household_size"].map(
        {"p1": 1, "p2": 2, "p3": 3, "p4": 4}).dropna()
    assert (sizes.reindex(expected.index) == expected).all()
    # every person sits on the same parcel as their household
    merged = in_hh.merge(placed.households[["household_id", "parcel_id"]], on="household_id", suffixes=("", "_hh"))
    assert (merged["parcel_id"] == merged["parcel_id_hh"]).all()


def test_person_table_copies_exact_age_and_sex_from_the_seed(inputs, sources):
    placed = model.run(inputs, Run.main())
    table = person_table(placed.persons, sources.load("pums_persons"))
    assert list(table.columns) == ["person_id", "family_id", "parcel_id", "AGE", "SEX", "RACE", "EDUCATION",
                                   "POVERTY", "POVERTY_RATIO", "EMPLOYMENT", "household_id"]
    assert len(table) == len(placed.persons) and table["AGE"].notna().all()
    p = placed.persons.set_index("person_id").loc[table["person_id"]]
    assert (table["SEX"].to_numpy() == p["sex"].astype(str).to_numpy()).all()
    assert ((table["AGE"] >= 65).to_numpy() == (p["age65_plus"].astype(str) == "yes").to_numpy()).all()
    assert table.loc[p["household_id"].to_numpy() < 0, "household_id"].isna().all()
    # categories are the run's own; the exact ratio agrees with the poverty category
    for col, var in (("RACE", "race_eth"), ("EDUCATION", "education"), ("POVERTY", "poverty"), ("EMPLOYMENT", "employment")):
        assert table[col].fillna("").tolist() == p[var].astype("string").fillna("").tolist()
    assert (table["AGE"].lt(25) == table["EDUCATION"].isna()).all()
    assert (table["AGE"].lt(16) == table["EMPLOYMENT"].isna()).all()
    assert (table["POVERTY_RATIO"].isna() == table["POVERTY"].isna()).all()
    below = table.dropna(subset=["POVERTY"])
    assert ((below["POVERTY_RATIO"] < 100) == (below["POVERTY"] == "below_100")).all()


def test_family_is_the_householder_and_their_relatives():
    seed = pd.DataFrame({
        "SERIALNO": ["A", "A", "A", "B", "C", "C", "D", "D", "E"],
        "SPORDER": [1, 2, 3, 1, 1, 2, 1, 2, 1],
        # A: householder, spouse, roommate. B: alone. C: unmarried partners. D: parent and child. E: group quarters.
        "RELSHIPP": [20, 21, 34, 20, 20, 22, 20, 25, 37],
    })
    persons = pd.DataFrame({
        "person_id": range(9), "household_id": [0, 0, 0, 1, 2, 2, 3, 3, -1], "parcel_id": "P",
        "seed_serial": seed["SERIALNO"], "seed_person": seed["SPORDER"],
    })
    got = family_ids(persons, seed)
    assert got.fillna(-1).tolist() == [0, 0, -1, -1, -1, -1, 3, 3, -1]


def test_every_person_gets_a_point_on_their_parcel_shared_by_their_household(inputs, sources):
    placed = model.run(inputs, Run.main())
    e911, parcels = sources.load("e911"), sources.load("parcels")
    where = locate(placed.persons, e911, parcels, np.random.default_rng(0))
    p = placed.persons.merge(where, on="person_id")
    assert len(p) == len(placed.persons) and p[["lon", "lat"]].notna().all().all()
    in_hh = p[p["household_id"] >= 0]
    assert (in_hh.groupby("household_id")[["lon", "lat"]].nunique() == 1).all().all()
    # an address point belongs to the person's parcel; a fallback point lies inside it
    pts = e911.assign(parcel_id=assign_points(e911, parcels, set(p["parcel_id"])))
    key = pts.geometry.x.round(6).astype(str) + "," + pts.geometry.y.round(6).astype(str) + "," + pts["parcel_id"].astype(str)
    addr = p[p["location"] == "address"]
    assert (addr["lon"].astype(str) + "," + addr["lat"].astype(str) + "," + addr["parcel_id"]).isin(set(key)).all()
    shapes = parcels.set_index("parcel_id").geometry
    for _, r in p[p["location"] == "parcel"].iterrows():
        assert shapes[r["parcel_id"]].contains(Point(r["lon"], r["lat"]))


def test_runs_are_reproducible_and_replicates_differ(inputs):
    a = model.run(inputs, Run.main()).persons
    b = model.run(inputs, Run.main()).persons
    pd.testing.assert_frame_equal(a, b)
    r1 = model.run(inputs, Run.replicate(1)).persons
    r2 = model.run(inputs, Run.replicate(2)).persons
    assert not (len(r1) == len(r2) and (r1["parcel_id"].to_numpy() == r2["parcel_id"].to_numpy()).all())


def test_validation_run_never_reads_held_out_block_counts(inputs):
    held = model.holdout_blocks(inputs, 0.25, seed=1)
    assert held
    first = model.run(inputs, Run.validation(held)).capacity.set_index("parcel_id")["units"]
    inputs.blocks.loc[inputs.blocks["area_id"].isin(held), ["housing2020", "pop2020"]] = 999
    second = model.run(inputs, Run.validation(held)).capacity.set_index("parcel_id")["units"]
    pd.testing.assert_series_equal(first, second)
