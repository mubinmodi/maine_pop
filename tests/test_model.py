from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from synthpop import model
from synthpop.model import Run
from synthpop.model.fit import fit_tract, fit_weights, integerize
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


def test_tract_fit_without_tract_cells_is_the_separate_block_group_fits():
    A = np.array([[1, 0, 0], [0, 1, 1], [1, 2, 3]], dtype=float)
    T = np.array([[10, 20, 70], [30, 5, 45]], dtype=float)
    S = np.array([[2, 3, 6], [4, 1, 5]], dtype=float)
    joint = fit_tract(A, np.ones(3), T, S)
    for b in range(2):
        np.testing.assert_allclose(joint[b].weights, fit_weights(A, np.ones(3), T[b], S[b]).weights, rtol=1e-6)


def test_a_precise_tract_total_moves_the_noisier_block_group():
    # One cell (people). Block groups say 100 ± 2 and 100 ± 20; the tract says 230 ± 1.
    A = np.array([[1.0, 2.0]])
    fits = fit_tract(A, np.ones(2), np.array([[100.0], [100.0]]), np.array([[2.0], [20.0]]),
                     np.array([230.0]), np.array([1.0]))
    a, b = (float(f.fitted[0]) for f in fits)
    assert abs(a + b - 230) < 2                       # the sum lands on the tract total
    assert b - 100 > 10 * (a - 100) > 0               # the imprecise block group takes most of the gap


def test_a_tract_only_cell_is_fitted_on_the_block_groups_sum():
    # Rows: people (both levels), people with a disability (tract only). Unit 1 has one.
    A = np.array([[1.0, 1.0], [0.0, 1.0]])
    fits = fit_tract(A, np.ones(2), np.array([[100.0], [100.0]]), np.array([[3.0], [3.0]]),
                     np.array([200.0, 60.0]), np.array([2.0, 1.0]), bg_rows=np.array([True, False]))
    assert abs(sum(f.weights[1] for f in fits) - 60) < 1.5    # tract-only total met
    assert all(f.fitted.shape == (1,) for f in fits)          # block groups report their own rows only
    assert all(abs(f.fitted[0] - 100) < 3 for f in fits)


def test_whole_tracts_skips_tracts_with_block_groups_outside():
    bg = pd.Series([50, 50, 40, 30], index=["230050001001", "230050001002", "230050002001", "230050002002"])
    tract = pd.Series([100, 100], index=["23005000100", "23005000200"])
    assert model.whole_tracts(bg, tract) == ["23005000100"]


def test_runs_fit_whole_tracts_together(sources, catalog):
    data = sources.load_all()
    acs = data["acs"].copy()
    # Make the fixture tract equal the sum of its two block groups, so it counts as whole.
    bg = acs[acs["level"] == "block_group"].assign(tract=lambda d: d["area_id"].str[:11])
    sums = bg.groupby(["tract", "cell"])["estimate"].sum()
    tr = acs["level"] == "tract"
    acs.loc[tr, "estimate"] = [sums.get((a, c), e) for a, c, e in acs.loc[tr, ["area_id", "cell", "estimate"]].itertuples(index=False)]
    inputs = model.prepare({**data, "acs": acs}, catalog, AREA)
    assert list(inputs.tract_targets.index) == ["23005000100"]
    placed = model.run(inputs, Run.main())
    assert placed.fits["tract_fit"].all() and placed.fits["converged"].all()
    off = model.prepare({**data, "acs": acs}, catalog, replace(AREA, fit_tracts=False))
    assert off.tract_targets.empty


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
                                   "POVERTY", "POVERTY_RATIO", "EMPLOYMENT", "DISABILITY", "HEALTH_INSURANCE",
                                   "household_id"]
    assert len(table) == len(placed.persons) and table["AGE"].notna().all()
    p = placed.persons.set_index("person_id").loc[table["person_id"]]
    assert (table["SEX"].to_numpy() == p["sex"].astype(str).to_numpy()).all()
    assert ((table["AGE"] >= 65).to_numpy() == (p["age65_plus"].astype(str) == "yes").to_numpy()).all()
    assert table.loc[p["household_id"].to_numpy() < 0, "household_id"].isna().all()
    # categories are the run's own; the exact ratio agrees with the poverty category
    for col, var in (("RACE", "race_eth"), ("EDUCATION", "education"), ("POVERTY", "poverty"), ("EMPLOYMENT", "employment"),
                     ("DISABILITY", "disability"), ("HEALTH_INSURANCE", "health_insurance")):
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
