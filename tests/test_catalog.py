import pandas as pd
import pytest
import yaml

from synthpop.catalog import Catalog
from conftest import CATALOG_PATH


def _seed(sources):
    return sources.load("pums_persons"), sources.load("pums_households")


def test_apply_recodes_every_active_variable(sources, catalog):
    pp, hh = catalog.apply(*_seed(sources))
    row = pp.set_index(["SERIALNO", "SPORDER"])
    assert row.loc[("H1", 1), "age_group"] == "a70_74"
    assert row.loc[("H1", 1), "education"] == "lt_hs"
    assert row.loc[("H1", 1), "age65_no_hs"] == "yes"
    assert row.loc[("H2", 2), "race_eth"] == "hispanic"
    assert row.loc[("H3", 1), "race_eth"] == "asian"
    assert pd.isna(row.loc[("H3", 2), "education"])          # under 25: outside the universe
    assert row.loc[("GQ1", 1), "living_quarters"] == "group_quarters"
    hrow = hh.set_index("SERIALNO")
    assert hrow.loc["H2", "structure"] == "units_5_19" and hrow.loc["H2", "tenure"] == "renter"
    assert hrow.loc["H3", "household_size"] == "p3"


def test_a_new_variable_needs_no_code_change(sources, tmp_path):
    raw = yaml.safe_load(CATALOG_PATH.read_text())
    raw["variables"].append({
        "id": "seniors_renting", "label": "65+ and renting", "kind": "derived", "unit": "person",
        "rules": [{"category": "yes", "when": "age65_plus == 'yes' and tenure == 'renter'"},
                  {"category": "no", "when": "age_group == age_group"}],
        "categories": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
    })
    path = tmp_path / "variables.yaml"
    path.write_text(yaml.safe_dump(raw))
    cat = Catalog.load(path)
    assert "seniors_renting" in [v["id"] for v in cat.schema()["variables"]]
    pp, _ = cat.apply(*_seed(sources))
    assert set(pp["seniors_renting"].dropna()) <= {"yes", "no"}


def test_attached_variables_are_listed_but_not_built(tmp_path):
    raw = yaml.safe_load(CATALOG_PATH.read_text())
    raw["variables"].append({"id": "diabetes", "label": "Diabetes", "kind": "attached", "unit": "person",
                             "categories": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]})
    path = tmp_path / "variables.yaml"
    path.write_text(yaml.safe_dump(raw))
    cat = Catalog.load(path)
    entry = next(v for v in cat.schema()["variables"] if v["id"] == "diabetes")
    assert entry["available"] is False
    assert "diabetes" not in [v.id for v in cat.active]


def test_bad_schema_fails_loudly(tmp_path):
    raw = yaml.safe_load(CATALOG_PATH.read_text())
    raw["variables"][0]["rules"][0]["category"] = "nonsense"
    path = tmp_path / "variables.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="unknown categories"):
        Catalog.load(path)
