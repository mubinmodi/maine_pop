import csv
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from synthpop import model
from synthpop.api import create_app
from synthpop.atlas import Atlas, Filter
from synthpop.datapoints import DataPointStore, RiskModel
from synthpop.model import Run
from synthpop.publish import publish, publish_people, publish_people_runs
from synthpop.summarize import RunData, published, summarize
from conftest import AREA, BG_IN, CATALOG_PATH


@pytest.fixture
def out_dir(sources, catalog, tmp_path):
    data = sources.load_all()
    inputs = model.prepare(data, catalog, AREA)
    runs = [model.run(inputs, s) for s in (Run.main(), Run.draw(1))]
    reps = [model.run(inputs, Run.replicate(r)) for r in (1, 2)]
    rd = lambda p: RunData(p.persons, p.households, p.capacity)
    stats = summarize([rd(p) for p in runs], [rd(p) for p in reps], catalog, AREA.place_geoid)
    out = tmp_path / "out"
    out.mkdir()
    pd.concat([stats, published(data["acs"], catalog)]).to_parquet(out / "area_stats.parquet", index=False)
    publish(data, runs[0].capacity, AREA, out)
    for name, placed in (("main", runs[0]), ("draw_01", runs[1]), ("replicate_01", reps[0])):
        (tmp_path / "runs" / name).mkdir(parents=True)
        placed.persons.to_parquet(tmp_path / "runs" / name / "persons.parquet", index=False)
    publish_people(tmp_path / "runs", data, catalog, AREA, out)
    publish_people_runs(tmp_path / "runs", data, catalog, AREA, out)
    return out


@pytest.fixture
def datapoints_path(tmp_path):
    return tmp_path / "datapoints.yaml"


@pytest.fixture
def atlas(out_dir, catalog, datapoints_path):
    return Atlas.open(out_dir, catalog, datapoints_path)


def test_schema_lists_totals_and_catalog_variables(atlas):
    ids = [v["id"] for v in atlas.schema()["variables"]]
    assert ids[:3] == ["population", "households", "housing_units"]
    assert {"age_group", "age65_no_hs"} <= set(ids)
    assert "synthetic" in atlas.schema()["disclosure"]


def test_layer_values_and_filters(atlas):
    all_bg = atlas.layer("block_group", "population")
    assert len(all_bg["values"]) == 2 and all_bg["filtered_count"] is None
    none = atlas.layer("block_group", "population", filters=[Filter("population", "total", ">", 1e9, "estimate")])
    assert none["filtered_count"] == 0
    assert not any(v[3] for v in none["values"].values())


def test_detail_compares_up_to_the_state(atlas):
    parcel = atlas.db.execute("SELECT area_id FROM stats WHERE level='parcel' LIMIT 1").fetchone()[0]
    d = atlas.detail("parcel", parcel)
    levels = {(c["level"], c["source"]) for c in d["comparisons"]}
    assert {("block_group", "synthetic"), ("block_group", "published"), ("county", "published"),
            ("state", "published")} <= levels
    assert "housing_units" in d["area"]["attrs"]
    assert d["source"] == "synthetic"


def test_search_finds_addresses_and_areas(atlas):
    assert atlas.search("Fixture St")[0]["level"] == "parcel"
    assert any(r["level"] == "county" for r in atlas.search("Cumberland"))


def test_csv_export_contains_the_selected_areas_population(atlas):
    area_id = BG_IN[0]
    population = atlas.layer("block_group", "population")["values"][area_id][0]

    body, media = atlas.export("block_group", "csv", area_ids=[area_id])
    disclosure, *lines = body.decode().splitlines()
    rows = list(csv.DictReader(lines))

    assert media == "text/csv"
    assert disclosure.startswith("# All values")
    assert [row["area_id"] for row in rows] == [area_id]
    assert rows[0]["source"] == "synthetic estimate"
    assert float(rows[0]["population.total"]) == pytest.approx(population, abs=0.0005)


def test_geojson_export_uses_json_null_for_missing_area_values(atlas):
    # One area has no synthetic estimate for a category that exists elsewhere.
    atlas.db.execute(
        "DELETE FROM stats WHERE source = 'synthetic' AND level = 'block_group' "
        "AND area_id = ? AND variable = 'sex' AND category = 'female'", [BG_IN[0]],
    )

    def reject_non_json_number(value):
        raise ValueError(f"invalid JSON number: {value}")

    geo, _ = atlas.export("block_group", "geojson")
    body = json.loads(geo, parse_constant=reject_non_json_number)
    features = {f["properties"]["area_id"]: f for f in body["features"]}
    assert body["type"] == "FeatureCollection"
    assert set(features) == set(BG_IN)
    assert features[BG_IN[0]]["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    assert features[BG_IN[0]]["properties"]["source"] == "synthetic estimate"
    assert features[BG_IN[0]]["properties"]["sex.female"] is None
    assert features[BG_IN[1]]["properties"]["sex.female"] > 0


def test_csv_still_exports_demographics_when_saved_model_cells_are_missing(out_dir, catalog, datapoints_path):
    store = DataPointStore(datapoints_path, catalog)
    model = RiskModel.parse({"id": "example_risk", "baseline": 0.1}, catalog)
    store.save(model, set())
    client = TestClient(create_app(out_dir, CATALOG_PATH, datapoints_path=datapoints_path))

    response = client.get("/api/export", params={"level": "block_group", "format": "csv"})

    assert response.status_code == 200, response.text
    rows = list(csv.DictReader(response.text.splitlines()[1:]))
    assert {row["area_id"] for row in rows} == set(BG_IN)
    assert all(float(row["population.total"]) > 0 for row in rows)
    assert all("example_risk.yes" not in row for row in rows)


def test_api_routes(out_dir, tmp_path, datapoints_path):
    client = TestClient(create_app(out_dir, CATALOG_PATH, app_dir=tmp_path / "no-app", datapoints_path=datapoints_path))
    assert client.get("/api/schema").status_code == 200
    assert client.get("/api/layer", params={"level": "block", "variable": "population"}).status_code == 200
    assert client.get("/api/layer", params={"level": "block", "variable": "nope"}).status_code == 404
    bad = client.get("/api/layer", params={"level": "block", "variable": "population", "filters": "not json"})
    assert bad.status_code == 400
    assert client.get("/geo/block_group.geojson").status_code == 200
    assert client.get("/api/people").json()["count"] > 0
    assert [r["name"] for r in client.get("/api/people/runs").json()] == ["main", "draw_01", "replicate_01"]
    assert client.get("/api/people", params={"run": "draw_01"}).json()["count"] > 0
    assert client.get("/api/people", params={"run": "../people"}).status_code == 404
    r = client.get("/api/export", params={"level": "block_group", "format": "csv"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]


def test_people_are_one_columnar_document_coded_by_schema_category(atlas, catalog):
    body = json.loads(atlas.people())
    n = body["count"]
    assert n > 0 and all(len(body[k]) == n for k in ("lon", "lat", "age", "u", "household", "family", "poverty_ratio", "parcel", "block", "fallback"))
    assert all(0 <= u < 1 for u in body["u"])
    for a in body["attributes"]:
        cats = catalog.variable(a).category_ids
        assert len(body["codes"][a]) == n and all(-1 <= c < len(cats) for c in body["codes"][a])
    sex = catalog.variable("sex").category_ids
    p = pd.read_parquet(atlas.out / "people.parquet")
    assert [sex[c] for c in body["codes"]["sex"]] == p["sex"].astype(str).tolist()
    assert {body["parcels"][i] for i in body["parcel"]} <= set(p["parcel_id"])
    assert [body["blocks"][i] for i in body["block"]] == p["block"].tolist()
    assert sum(body["fallback"]) == int((p["location"] == "parcel").sum())
    # every household has one householder, and its people share one circle
    assert len(body["head"]) == n and len(body["unit"]) == n
    heads = pd.Series(body["head"]).groupby(pd.Series(body["household"])).sum()
    assert (heads.drop(-1, errors="ignore") == 1).all()
    by_unit = pd.DataFrame({"unit": body["unit"], "household": body["household"], "parcel": body["parcel"]})
    hh = by_unit[by_unit["household"] >= 0]
    assert (hh.groupby("household")["unit"].nunique() == 1).all() and (by_unit.groupby("unit")["parcel"].nunique() == 1).all()
    units = body["units"]
    assert len(units["lon"]) == len(units["lat"]) == len(units["r"]) == by_unit["unit"].nunique()
    assert all(r > 0 for r in units["r"])


def test_password_protects_everything_but_the_health_check(out_dir, tmp_path, datapoints_path):
    client = TestClient(create_app(out_dir, CATALOG_PATH, app_dir=tmp_path / "no-app",
                                   datapoints_path=datapoints_path, password="s3cret"))
    assert client.get("/healthz").status_code == 200
    r = client.get("/api/schema")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert client.get("/api/schema", auth=("synthpop", "wrong")).status_code == 401
    assert client.get("/api/schema", headers={"Authorization": "Basic not-base64!"}).status_code == 401
    assert client.get("/api/schema", auth=("synthpop", "s3cret")).status_code == 200
    assert client.get("/geo/block_group.geojson", auth=("synthpop", "s3cret")).status_code == 200


def test_every_run_plays_as_the_same_document_rebuilt_from_shared_seeds(atlas):
    runs = atlas.people_runs()
    assert [(r["name"], r["kind"]) for r in runs] == [("main", "main"), ("draw_01", "draw"), ("replicate_01", "replicate")]
    main = json.loads(atlas.people())
    # the main run rebuilt from people_runs/ matches people.parquet exactly
    assert json.loads(atlas._people_body(atlas._run_frame("main"))) == main
    for r in runs[1:]:
        body = json.loads(atlas.people(r["name"]))
        assert body["count"] == r["people"] and body["attributes"] == main["attributes"]
        assert len({h for h in body["household"] if h >= 0}) == r["households"]
    with pytest.raises(KeyError):
        atlas.people("draw_99")
