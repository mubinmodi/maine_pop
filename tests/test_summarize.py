import numpy as np
import pandas as pd
import pytest
import yaml

from synthpop import model
from synthpop.catalog import Catalog
from synthpop.model import Run
from synthpop.summarize import RunData, published, summarize
from synthpop.validate import validate
from conftest import AREA, CATALOG_PATH

CITY = AREA.place_geoid


def _run_data(placed):
    return RunData(placed.persons, placed.households, placed.capacity)


@pytest.fixture
def setup(sources, catalog):
    data = sources.load_all()
    inputs = model.prepare(data, catalog, AREA)
    main = [_run_data(model.run(inputs, Run.main())), _run_data(model.run(inputs, Run.draw(1)))]
    reps = [_run_data(model.run(inputs, Run.replicate(r))) for r in (1, 2, 3)]
    return data, inputs, main, reps


def test_every_level_adds_up_from_parcels(setup, catalog):
    _, _, main, reps = setup
    stats = summarize(main, reps, catalog, CITY)
    by_level = stats.groupby(["level", "variable", "category"])["estimate"].sum().unstack("level")
    for level in ["block", "block_group", "tract", "city"]:
        np.testing.assert_allclose(by_level[level].fillna(0), by_level["parcel"].fillna(0))


def test_parcel_estimates_are_averages_over_placement_draws(setup, catalog):
    _, _, main, _ = setup
    one = summarize(main[:1], [], catalog, CITY)
    two = summarize(main, [], catalog, CITY)
    hh = lambda s: s[(s["level"] == "parcel") & (s["variable"] == "households")].set_index("area_id")["estimate"]
    assert (hh(one) % 1 == 0).all()                       # one draw: whole households
    assert hh(two).sum() == pytest.approx(hh(one).sum())  # averages keep the total


def test_standard_error_is_zero_when_replicates_match_the_main_run(setup, catalog):
    _, _, main, reps = setup
    same = summarize(main[:1], [main[0], main[0]], catalog, CITY)
    assert (same["se"] == 0).all() and (same["share_se"] == 0).all()
    varied = summarize(main, reps, catalog, CITY)
    assert (varied["se"] > 0).any()


def test_shares_sum_to_one_over_categories(setup, catalog):
    _, _, main, reps = setup
    stats = summarize(main, reps, catalog, CITY)
    edu = stats[(stats["variable"] == "education") & (stats["level"] == "block_group")]
    np.testing.assert_allclose(edu.groupby("area_id")["share"].sum(), 1.0)


def test_published_rows_cover_every_level_and_derived_variables(setup, catalog):
    data, *_ = setup
    pub = published(data["acs"], catalog)
    pop = pub[(pub["variable"] == "population")]
    assert set(pop["level"]) == {"block_group", "tract", "city", "county", "state"}
    assert "age65_plus" in set(pub["variable"])          # derived from the sex x age constraint
    assert "age65_no_hs" not in set(pub["variable"])     # needs age x education, which ACS here lacks
    assert (pub["source"] == "published").all()


def test_a_new_yaml_variable_reaches_area_stats(sources, tmp_path):
    raw = yaml.safe_load(CATALOG_PATH.read_text())
    raw["variables"].append({
        "id": "seniors_alone", "label": "65+ living alone", "kind": "derived", "unit": "person",
        "rules": [{"category": "yes", "when": "age65_plus == 'yes' and household_size == 'p1'"},
                  {"category": "no", "when": "age_group == age_group"}],
        "categories": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
    })
    path = tmp_path / "variables.yaml"
    path.write_text(yaml.safe_dump(raw))
    cat = Catalog.load(path)
    inputs = model.prepare(sources.load_all(), cat, AREA)
    stats = summarize([_run_data(model.run(inputs, Run.main()))], [], cat, CITY)
    assert {"yes", "no"} >= set(stats.loc[stats["variable"] == "seniors_alone", "category"])
    assert (stats["variable"] == "seniors_alone").any()


def test_validation_report_writes(setup, catalog, tmp_path):
    data, inputs, main, reps = setup
    held = model.holdout_blocks(inputs, 0.5, seed=1)
    val = summarize([_run_data(model.run(inputs, Run.validation(held)))], [], catalog, CITY)
    stats = summarize(main, reps, catalog, CITY)
    diagnostics = {"converged": 1.0, "iterations": 5, "exact": 0.8, "nearest": 0.15, "overflow": 0.05,
                   "parcels_with_units": 8, "fallback": 0, "gq_synthetic": 3, "gq_published": 4}
    report = validate(stats, val, published(data["acs"], catalog), inputs.blocks, held, diagnostics)
    report.write(tmp_path / "validation.md", tmp_path / "csv")
    text = (tmp_path / "validation.md").read_text()
    assert "Headline" in text and "Held-out blocks" in text
    assert (tmp_path / "csv" / "fit_by_area.csv").exists()
