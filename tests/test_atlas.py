import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from synthpop import model
from synthpop.api import create_app
from synthpop.atlas import Atlas, Filter
from synthpop.model import Run
from synthpop.publish import publish, publish_people
from synthpop.summarize import RunData, published, summarize
from conftest import AREA, CATALOG_PATH


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
    (tmp_path / "runs" / "main").mkdir(parents=True)
    runs[0].persons.to_parquet(tmp_path / "runs" / "main" / "persons.parquet", index=False)
    publish_people(tmp_path / "runs", data, catalog, AREA, out)
    return out


@pytest.fixture
def atlas(out_dir, catalog):
    return Atlas.open(out_dir, catalog)


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


def test_export_csv_and_geojson_label_values_synthetic(atlas):
    body, media = atlas.export("block_group", "csv")
    text = body.decode()
    assert text.startswith("# All values") and "synthetic estimate" in text
    geo, _ = atlas.export("block_group", "geojson")
    feats = json.loads(geo)["features"]
    assert len(feats) == 2 and feats[0]["properties"]["source"] == "synthetic estimate"


def test_api_routes(out_dir, tmp_path):
    client = TestClient(create_app(out_dir, CATALOG_PATH, app_dir=tmp_path / "no-app"))
    assert client.get("/api/schema").status_code == 200
    assert client.get("/api/layer", params={"level": "block", "variable": "population"}).status_code == 200
    assert client.get("/api/layer", params={"level": "block", "variable": "nope"}).status_code == 404
    bad = client.get("/api/layer", params={"level": "block", "variable": "population", "filters": "not json"})
    assert bad.status_code == 400
    assert client.get("/geo/block_group.geojson").status_code == 200
    assert client.get("/api/people").json()["count"] > 0
    r = client.get("/api/export", params={"level": "block_group", "format": "csv"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]


def test_people_are_one_columnar_document_coded_by_schema_category(atlas, catalog):
    body = json.loads(atlas.people())
    n = body["count"]
    assert n > 0 and all(len(body[k]) == n for k in ("lon", "lat", "age", "u", "household", "family", "parcel", "block", "fallback"))
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


def test_password_protects_everything_but_the_health_check(out_dir, tmp_path):
    client = TestClient(create_app(out_dir, CATALOG_PATH, app_dir=tmp_path / "no-app", password="s3cret"))
    assert client.get("/healthz").status_code == 200
    r = client.get("/api/schema")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert client.get("/api/schema", auth=("synthpop", "wrong")).status_code == 401
    assert client.get("/api/schema", headers={"Authorization": "Basic not-base64!"}).status_code == 401
    assert client.get("/api/schema", auth=("synthpop", "s3cret")).status_code == 200
    assert client.get("/geo/block_group.geojson", auth=("synthpop", "s3cret")).status_code == 200
