"""Write side: everything the API and app read, under data/out/.

    publish(data, capacity, area, out_dir)

  areas.parquet         one row per area at every level: names, parent IDs, centroid, attributes (JSON)
  search.parquet        addresses and area names for search
  geo/<level>.geojson   simplified geometry with only `id`, fetched once per level by the app
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
