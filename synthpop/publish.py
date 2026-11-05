"""Write side: everything the API and app read, under data/out/.

    publish(data, capacity, area, out_dir)

  areas.parquet         one row per area at every level: names, parent IDs, centroid, attributes (JSON)
  search.parquet        addresses and area names for search
  geo/<level>.geojson   simplified geometry with only `id`, fetched once per level by the app
  people.parquet        the main run's persons with a map point each (publish_people)
  area_stats.parquet    written earlier by `synthpop summarize`
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

from .config import StudyArea
from .model.place import GROUP_QUARTERS_TYPES, NON_RESIDENTIAL, assign_points

COMMERCIAL = {"Commercial", "Office", "Restaurant", "Financial Institution", "Retail - Strip Mall",
              "Retail - Enclosed Mall", "Retail - Free Standing", "Lodging", "Marina", "Golf Club", "Arena",
              "Sports Complex", "Attraction", "Airport"}
INSTITUTIONAL = {"School", "College - University", "Hospital", "Health Care", "Nursing Home", "Church",
                 "Government - Municipal", "Law Enforcement - County", "Law Enforcement -Municipal",
                 "Law Enforcement - Federal", "Fire Station", "EMS Station", "Library", "Post Office", "PSAP",
                 "Public Health Office", "Veterinarian"}
SIMPLIFY = {"parcel": 0.000005, "block": 0.00002, "block_group": 0.00005, "tract": 0.0001, "city": 0.0002}


def land_use(types: pd.Series) -> str:
    """A parcel's land use from the types of its E911 address points."""
    t = set(types.fillna("").str.strip())
    residential = any(x not in NON_RESIDENTIAL for x in t)
    commercial = bool(t & COMMERCIAL)
    if t & GROUP_QUARTERS_TYPES:
        return "Institutional (group quarters likely)"
    if residential and commercial:
        return "Mixed use"
    if residential:
        return "Residential"
    if commercial:
        return "Commercial"
    if t & INSTITUTIONAL:
        return "Institutional"
    return "Other"


def publish(data: dict, capacity: pd.DataFrame, area: StudyArea, out_dir: str | Path) -> dict[str, int]:
    out = Path(out_dir)
    (out / "geo").mkdir(parents=True, exist_ok=True)
    county_id, state_id, city_id = area.county_geoid, area.state_fips, area.place_geoid

    parcels = data["parcels"][data["parcels"]["parcel_id"].isin(set(capacity["parcel_id"]))].copy()
    e911 = data["e911"].copy()
    e911["parcel_id"] = assign_points(e911, data["parcels"], set(parcels["parcel_id"]))
    pts = e911[e911["parcel_id"].notna()]
    first_address = pts.groupby("parcel_id")["address"].first()
    n_points = pts.groupby("parcel_id").size()
    uses = pts.groupby("parcel_id")["place_type"].apply(land_use)

    cap = capacity.set_index("parcel_id")
    parcels = parcels.set_index("parcel_id").join(cap[["block", "block_group", "units", "fallback", "gq_site"]])
    parcels["name"] = first_address.reindex(parcels.index).fillna("Parcel " + parcels.index.to_series())
    parcels["attrs"] = [json.dumps({
        "address": first_address.get(pid), "land_use": uses.get(pid, "No address point"),
        "housing_units": int(r.units), "address_points": int(n_points.get(pid, 0)),
        "units_by_fallback": bool(r.fallback), "group_quarters_site": bool(r.gq_site)})
        for pid, r in parcels.iterrows()]
    parcels = parcels.reset_index().rename(columns={"parcel_id": "area_id"})

    blocks = data["blocks"].copy()
    blocks["name"] = "Block " + blocks["area_id"].str[-4:]
    blocks["block"] = blocks["area_id"]
    blocks["attrs"] = [json.dumps({"housing_units_2020": int(h), "population_2020": int(p)})
                       for h, p in zip(blocks["housing2020"], blocks["pop2020"])]

    bgs = data["block_groups"].copy()
    tract_name = data["tracts"].set_index("area_id")["name"]
    bgs["name"] = bgs["name"] + ", " + bgs["tract"].map(tract_name).fillna("Tract " + bgs["tract"].str[-6:])
    bgs["block_group"] = bgs["area_id"]
    tracts = data["tracts"].copy()
    tracts["tract"] = tracts["area_id"]
    city = data["place"].copy()
    city["name"] = area.place_name + " city"

    frames = []
    for level, gdf in (("parcel", parcels), ("block", blocks), ("block_group", bgs), ("tract", tracts), ("city", city)):
        g = gpd.GeoDataFrame(gdf, geometry="geometry", crs=4326)
        g["level"] = level
        for col in ("block", "block_group", "tract", "attrs"):
            if col not in g:
                g[col] = None
        if level in ("parcel", "block", "block_group"):
            g["tract"] = g["block_group"].str[:11]
        g["city"], g["county"], g["state"] = city_id, county_id, state_id
        rp = g.geometry.representative_point()
        g["lon"], g["lat"] = rp.x.round(6), rp.y.round(6)
        frames.append(g[["level", "area_id", "name", "block", "block_group", "tract", "city", "county", "state",
                         "lon", "lat", "attrs", "geometry"]])
        geo = g[["area_id", "geometry"]].rename(columns={"area_id": "id"})
        geo["geometry"] = shapely.set_precision(geo.geometry.simplify(SIMPLIFY[level], preserve_topology=True), 1e-6)
        (out / "geo" / f"{level}.geojson").write_text(geo.to_json(drop_id=True))

    extra = pd.DataFrame([
        {"level": "county", "area_id": county_id, "name": "Cumberland County", "county": county_id, "state": state_id},
        {"level": "state", "area_id": state_id, "name": "Maine", "state": state_id},
    ])
    areas = pd.concat([pd.DataFrame(f.drop(columns="geometry")) for f in frames] + [extra], ignore_index=True)
    areas.to_parquet(out / "areas.parquet", index=False)

    addr = pts[["address", "parcel_id", "geometry"]].dropna(subset=["address"]).drop_duplicates(["address", "parcel_id"])
    search = pd.concat([
        pd.DataFrame({"label": addr["address"] + ", Portland", "level": "parcel", "area_id": addr["parcel_id"],
                      "lon": addr.geometry.x.round(6), "lat": addr.geometry.y.round(6), "kind": "address"}),
        areas[areas["level"].isin(["block_group", "tract", "city", "county", "state"])]
        .assign(label=lambda d: d["name"], kind="area")[["label", "level", "area_id", "lon", "lat", "kind"]],
    ], ignore_index=True)
    search.to_parquet(out / "search.parquet", index=False)
    return areas.groupby("level").size().to_dict()


def risk_factors(catalog) -> list[str]:
    """Person-level seed variables a data point's risk model can use."""
    return [v.id for v in catalog.active if v.kind == "seed" and v.unit == "person"]


def publish_people(runs_dir: str | Path, data: dict, catalog, area: StudyArea, out_dir: str | Path) -> int:
    """people.parquet: the main run's synthetic persons for the map's people layer, each
    with an E911 address point, exact age, catalog categories and a fixed uniform draw `u`.
    A data point marks a person a case when u < their risk, so changing a model only
    adds or removes cases at the margin instead of reshuffling who has it."""
    import numpy as np

    from .persons import family_ids, locate

    persons = pd.read_parquet(Path(runs_dir) / "main" / "persons.parquet")
    persons["family_id"] = family_ids(persons, data["pums_persons"])
    # Same stream as `synthpop persons`, so the map and the CSV agree.
    where = locate(persons, data["e911"], data["parcels"], np.random.default_rng([area.random_seed, 2]))
    cols = [v.id for v in catalog.active]
    seed = data["pums_persons"][["SERIALNO", "SPORDER", "AGEP"]]
    p = (persons[["person_id", "family_id", "household_id", "parcel_id", "block", "seed_serial", "seed_person"] + cols]
         .merge(where, on="person_id")
         .merge(seed, left_on=["seed_serial", "seed_person"], right_on=["SERIALNO", "SPORDER"], how="left")
         .drop(columns=["seed_serial", "seed_person", "SERIALNO", "SPORDER"])
         .rename(columns={"AGEP": "age"})
         .sort_values("person_id", ignore_index=True))
    p["u"] = np.random.default_rng([area.random_seed, 3]).random(len(p)).round(4)
    p.to_parquet(Path(out_dir) / "people.parquet", index=False)
    return len(p)


def publish_cells(runs_dir: str | Path, catalog, out_dir: str | Path) -> dict[str, int]:
    """Per-run person counts by risk-factor cell, for computing data points live.
    Everyone is counted, including people with a blank factor (education under 25).

      cells_draws.parquet  main run (run 0) and placement draws, by parcel
      cells_reps.parquet   replicate runs, by block group (enough for survey variance)
    """
    runs, out = Path(runs_dir), Path(out_dir)
    factors = risk_factors(catalog)
    draws = [runs / "main"] + sorted(runs.glob("draw_*"))
    frames = []
    for i, d in enumerate(draws):
        p = pd.read_parquet(d / "persons.parquet", columns=["parcel_id", "block", "block_group"] + factors)
        c = p.astype({f: str for f in factors}).groupby(["parcel_id", "block", "block_group"] + factors,
                                                        observed=True, dropna=False).size().rename("n").reset_index()
        frames.append(c.assign(run=i))
    pd.concat(frames, ignore_index=True).to_parquet(out / "cells_draws.parquet", index=False)
    reps = sorted(runs.glob("replicate_*"))
    frames = []
    for d in reps:
        p = pd.read_parquet(d / "persons.parquet", columns=["block_group"] + factors)
        c = p.astype({f: str for f in factors}).groupby(["block_group"] + factors, observed=True, dropna=False).size()
        frames.append(c.rename("n").reset_index().assign(run=int(d.name.split("_")[1])))
    if frames:
        pd.concat(frames, ignore_index=True).to_parquet(out / "cells_reps.parquet", index=False)
    return {"draws": len(draws), "replicates": len(reps)}
