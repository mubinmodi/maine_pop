"""Write side: everything the API and app read, under data/out/.

    publish(data, capacity, area, out_dir)

  areas.parquet         one row per area at every level: names, parent IDs, centroid, attributes (JSON)
  search.parquet        addresses and area names for search
  geo/<level>.geojson   simplified geometry with only `id`, fetched once per level by the app
  people.parquet        the main run's persons with a map point each (publish_people)
  people_runs/          every run's persons, compactly, for the app's simulation player
                        (publish_people_runs): index.json, seeds.parquet, <run>.parquet
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
    with an E911 address point, exact age and poverty ratio, catalog categories and a
    fixed uniform draw `u`.
    A data point marks a person a case when u < their risk, so changing a model only
    adds or removes cases at the margin instead of reshuffling who has it."""
    p = people_frame(pd.read_parquet(Path(runs_dir) / "main" / "persons.parquet"), data, catalog, area)
    p.drop(columns=["seed_serial", "seed_person"]).to_parquet(Path(out_dir) / "people.parquet", index=False)
    return len(p)


def people_frame(persons: pd.DataFrame, data: dict, catalog, area: StudyArea) -> pd.DataFrame:
    """One run's persons as the people layer shows them, in person_id order: IDs, parcel
    and block, a map point, exact age and poverty ratio, catalog categories and `u`.
    Every run uses the same random streams, so the main run matches `synthpop persons`."""
    import numpy as np

    from .persons import HOUSEHOLDER, family_ids, locate

    persons = persons.assign(family_id=family_ids(persons, data["pums_persons"]))
    where = locate(persons, data["e911"], data["parcels"], np.random.default_rng([area.random_seed, 2]))
    cols = [v.id for v in catalog.active]
    seed = data["pums_persons"][["SERIALNO", "SPORDER", "AGEP", "POVPIP", "RELSHIPP"]]
    p = (persons[["person_id", "family_id", "household_id", "parcel_id", "block", "seed_serial", "seed_person"] + cols]
         .merge(where, on="person_id")
         .merge(seed, left_on=["seed_serial", "seed_person"], right_on=["SERIALNO", "SPORDER"], how="left")
         .drop(columns=["SERIALNO", "SPORDER"])
         .rename(columns={"AGEP": "age", "POVPIP": "poverty_ratio"})
         .sort_values("person_id", ignore_index=True))
    # The householder (PUMS RELSHIPP 20), who names the household's colour on the map.
    p["head"] = (p.pop("RELSHIPP") == HOUSEHOLDER) & (p["household_id"] >= 0)
    p["u"] = np.random.default_rng([area.random_seed, 3]).random(len(p)).round(4)
    return p


RUN_KINDS = ("main", "draw", "replicate", "validation")


def run_names(runs_dir: str | Path) -> list[str]:
    """Saved runs in playback order: main, placement draws, replicates, validation."""
    runs = Path(runs_dir)
    names = ["main"] + sorted(d.name for d in runs.glob("draw_*")) + sorted(d.name for d in runs.glob("replicate_*")) + ["validation"]
    return [n for n in names if (runs / n / "persons.parquet").exists()]


def publish_people_runs(runs_dir: str | Path, data: dict, catalog, area: StudyArea, out_dir: str | Path) -> int:
    """people_runs/: every saved run's persons, for playing the simulations on the map.

    Only what differs between runs is stored per run, about 0.25 MB each:
      <run>.parquet   per person, in person_id order: seed (row in seeds), household_id,
                      in_family (family_id = household_id when true), point (row in points)
    and what is shared, once:
      seeds.parquet   one row per seed (PUMS) person any run uses: exact age and poverty
                      ratio and every catalog category, which depend only on the seed person
      points.parquet  every map point any run uses: lon, lat, parcel_id, block, location
      u.parquet       the fixed draw u by person_id; every run uses the same stream
      index.json      the runs in playback order, with kind, people and households
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    out = Path(out_dir) / "people_runs"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.parquet"):
        old.unlink()
    key, where = ["seed_serial", "seed_person"], ["lon", "lat", "parcel_id"]
    attrs = ["age", "poverty_ratio", "head"] + [v.id for v in catalog.active]
    frames = {name: people_frame(pd.read_parquet(Path(runs_dir) / name / "persons.parquet"), data, catalog, area)
              for name in run_names(runs_dir)}

    seeds = pd.concat([f[key + attrs] for f in frames.values()]).drop_duplicates(key, ignore_index=True)
    seeds.insert(0, "seed", np.arange(len(seeds), dtype="int16"))
    seeds.to_parquet(out / "seeds.parquet", index=False)
    points = pd.concat([f[where + ["block", "location"]] for f in frames.values()]).drop_duplicates(where, ignore_index=True)
    points.insert(0, "point", np.arange(len(points), dtype="int32"))
    points.to_parquet(out / "points.parquet", index=False, compression="zstd")
    longest = max(frames.values(), key=len)
    pd.DataFrame({"u": longest["u"].to_numpy()}).to_parquet(out / "u.parquet", index=False)

    index = []
    for name, f in frames.items():
        if not np.array_equal(f["u"].to_numpy(), longest["u"].to_numpy()[:len(f)]):
            raise ValueError(f"run {name} does not share the u stream")
        f = (f.merge(seeds[key + ["seed"]], on=key, how="left").merge(points[where + ["point"]], on=where, how="left")
             .sort_values("person_id", ignore_index=True))
        table = pa.table({"seed": f["seed"].astype("int16"), "household_id": f["household_id"].astype("int32"),
                          "in_family": f["family_id"].notna().to_numpy(), "point": f["point"].astype("int32")})
        pq.write_table(table, out / f"{name}.parquet", compression="zstd",
                       use_dictionary=["seed", "in_family", "point"], column_encoding={"household_id": "DELTA_BINARY_PACKED"})
        kind = next(k for k in RUN_KINDS if name.startswith(k))
        index.append({"name": name, "kind": kind, "people": len(f),
                      "households": int(f.loc[f["household_id"] >= 0, "household_id"].nunique())})
    (out / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    publish_household_slots(frames.values(), data["parcels"], out_dir)
    return len(index)


def publish_household_slots(frames, parcels, out_dir: str | Path) -> int:
    """household_slots.parquet: for every parcel and every number of households (and
    group-quarters groups) it holds in any run, where on the parcel each one's circle goes
    (see synthpop.households)."""
    from .households import slots, unit_counts

    needs = pd.concat([unit_counts(f).rename("n").reset_index() for f in frames]).drop_duplicates(ignore_index=True)
    table = slots(parcels[parcels["parcel_id"].isin(set(needs["parcel_id"]))], needs)
    table.to_parquet(Path(out_dir) / "household_slots.parquet", index=False, compression="zstd")
    return len(table)


def publish_places(data: dict, out_dir: str | Path) -> int:
    """places.parquet: CDC PLACES crude prevalence (percent of adults) by tract and city,
    for data points calibrated to it (datapoints.RiskModel.calibrate)."""
    p = data.get("places")
    if p is None or not len(p):
        return 0
    p = p[(p["value_type"] == "CrdPrv") & p["level"].isin(["tract", "city"])]
    p[["level", "area_id", "measure", "estimate", "low", "high", "year"]].to_parquet(Path(out_dir) / "places.parquet", index=False)
    return int((p["level"] == "tract").sum())


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
