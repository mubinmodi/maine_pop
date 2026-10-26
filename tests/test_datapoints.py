import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from synthpop import model
from synthpop.api import create_app
from synthpop.atlas import Atlas, Filter
from synthpop.datapoints import PRESETS, DataPointEngine, RiskModel
from synthpop.model import Run
from synthpop.publish import publish, publish_cells
from synthpop.summarize import RunData, published, summarize
from conftest import AREA, CATALOG_PATH

DIABETES = {
    "id": "diabetes", "label": "Diabetes (test model)", "baseline": 0.05,
    "odds_ratios": {"age_group": {"a70_74": 3.0, "a85_plus": 4.0}, "sex": {"female": 0.8}},
    "universe": {},
}


@pytest.fixture
def built(sources, catalog, tmp_path):
    """A fixture town built like the real thing, with runs saved to disk."""
    data = sources.load_all()
    inputs = model.prepare(data, catalog, AREA)
    runs_dir, out = tmp_path / "runs", tmp_path / "out"
    placed = {}
    for spec in (Run.main(), Run.draw(1), Run.draw(2), Run.replicate(1), Run.replicate(2)):
        p = model.run(inputs, spec)
        d = runs_dir / spec.name
        d.mkdir(parents=True)
        p.persons.to_parquet(d / "persons.parquet", index=False)
        placed[spec.name] = p
    rd = lambda p: RunData(p.persons, p.households, p.capacity)
    stats = summarize([rd(placed[n]) for n in ("main", "draw_01", "draw_02")],
                      [rd(placed[n]) for n in ("replicate_01", "replicate_02")], catalog, AREA.place_geoid)
    out.mkdir()
    pd.concat([stats, published(data["acs"], catalog)]).to_parquet(out / "area_stats.parquet", index=False)
    publish(data, placed["main"].capacity, AREA, out)
    publish_cells(runs_dir, catalog, out)
    return out, placed


def test_model_rejects_bad_input(catalog):
    with pytest.raises(ValueError, match="unknown factor"):
        RiskModel.parse({**DIABETES, "odds_ratios": {"shoe_size": {"big": 2}}}, catalog)
    with pytest.raises(ValueError, match="categories"):
        RiskModel.parse({**DIABETES, "odds_ratios": {"sex": {"other": 2}}}, catalog)
    with pytest.raises(ValueError, match="baseline"):
        RiskModel.parse({**DIABETES, "baseline": 1.5}, catalog)
    with pytest.raises(ValueError, match="id"):
        RiskModel.parse({**DIABETES, "id": "Has Spaces"}, catalog)


def test_dhana_preset_parses(catalog):
    m = RiskModel.parse(PRESETS["alzheimers_dhana"], catalog)
    assert m.odds_ratios["age_group"]["a85_plus"] == 14.78
    assert 0.02 < m.baseline < 0.05


def test_expected_cases_match_a_brute_force_sum(built, catalog):
    out, placed = built
    m = RiskModel.parse(DIABETES, catalog)
    engine = DataPointEngine(out, catalog, AREA.place_geoid)

    def brute(persons):
        logit = math.log(0.05 / 0.95) + persons["age_group"].map({"a70_74": math.log(3), "a85_plus": math.log(4)}).fillna(0) \
            + persons["sex"].map({"female": math.log(0.8)}).fillna(0)
        return (1 / (1 + np.exp(-logit.astype(float)))).sum()

    expected = np.mean([brute(placed[n].persons) for n in ("main", "draw_01", "draw_02")])
    assert engine.compute(m, "city")["estimate"].iloc[0] == pytest.approx(expected, rel=1e-9)


def test_cases_add_up_across_levels_and_universe_limits_shares(built, catalog):
    out, placed = built
    engine = DataPointEngine(out, catalog, AREA.place_geoid)
    m = RiskModel.parse({**DIABETES, "universe": {"age_group": ["a70_74", "a85_plus"]}}, catalog)
    totals = [engine.compute(m, lvl)["estimate"].sum() for lvl in ("parcel", "block", "block_group", "tract", "city")]
    np.testing.assert_allclose(totals, totals[0])
    city = engine.compute(m, "city").iloc[0]
    in_universe = np.mean([placed[n].persons["age_group"].isin(["a70_74", "a85_plus"]).sum() for n in ("main", "draw_01", "draw_02")])
    assert city["share"] == pytest.approx(city["estimate"] / in_universe, rel=0.05)
    assert (engine.compute(m, "block_group")["se"] >= 0).all()


def test_preview_save_use_and_delete(built, catalog, tmp_path):
    out, _ = built
    store = tmp_path / "datapoints.yaml"
    atlas = Atlas.open(out, catalog, store)
    pv = atlas.preview(DIABETES, "block_group")
    assert len(pv["values"]) == 2 and pv["city"]["estimate"] > 0
    assert {r["category"] for r in pv["breakdown"]["sex"]} == {"male", "female"}

    atlas.save_datapoint(DIABETES)
    assert "diabetes" in store.read_text()
    assert "diabetes" in [v["id"] for v in atlas.schema()["variables"]]
    assert len(atlas.layer("parcel", "diabetes", "yes", "estimate")["values"]) > 0
    assert atlas.layer("block_group", "diabetes", "yes", filters=[Filter("diabetes", "yes", ">", 2, "share")])["filtered_count"] == 0
    parcel = atlas.db.execute("SELECT area_id FROM stats WHERE level='parcel' LIMIT 1").fetchone()[0]
    assert "diabetes" in atlas.detail("parcel", parcel)["values"]
    csv, _ = atlas.export("block_group", "csv")
    assert "diabetes.yes" in csv.decode()
    with pytest.raises(ValueError, match="already a variable"):
        atlas.save_datapoint({**DIABETES, "id": "sex"})

    reopened = Atlas.open(out, catalog, store)             # saved data points survive a restart
    assert "diabetes" in [v["id"] for v in reopened.schema()["variables"]]
    reopened.delete_datapoint("diabetes")
    assert "diabetes" not in [v["id"] for v in reopened.schema()["variables"]]


def test_api_routes(built, tmp_path):
    out, _ = built
    client = TestClient(create_app(out, CATALOG_PATH, app_dir=tmp_path / "no-app", datapoints_path=tmp_path / "dp.yaml"))
    r = client.post("/api/datapoints/preview", json={"model": DIABETES, "level": "tract"})
    assert r.status_code == 200 and "breakdown" in r.json()
    assert client.post("/api/datapoints/preview", json={"model": {**DIABETES, "baseline": 2}}).status_code == 400
    assert client.post("/api/datapoints", json=DIABETES).status_code == 200
    assert client.get("/api/layer", params={"level": "tract", "variable": "diabetes", "category": "yes"}).status_code == 200
    assert client.delete("/api/datapoints/diabetes").status_code == 200
    assert client.delete("/api/datapoints/diabetes").status_code == 404
