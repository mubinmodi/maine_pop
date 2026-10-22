import numpy as np
import pandas as pd
import pytest

from synthpop import model
from synthpop.model import Run
from synthpop.model.fit import fit_weights, integerize
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
