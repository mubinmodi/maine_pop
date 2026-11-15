"""Fetch, clip and cache every public input the build needs.

    sources = Sources(study_area, catalog, data_dir, HttpFetcher())
    parcels = sources.load("parcels")

Every load is clipped to the study area, in EPSG:4326, and cached under
`data_dir/interim/` so a second call reads the cache. Delete the cache file
to refetch.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

from ..catalog import Catalog
from ..config import StudyArea
from .fetchers import FixtureFetcher, HttpFetcher

__all__ = ["Sources", "HttpFetcher", "FixtureFetcher", "NAMES"]

NAMES = ("place", "tracts", "block_groups", "blocks", "parcels", "e911",
         "pums_households", "pums_persons", "acs")

CENSUS = "https://www2.census.gov"
GEOLIB = "https://services1.arcgis.com/RbMX0mRVOFNTdLzd/ArcGIS/rest/services"
PARCELS_URL = f"{GEOLIB}/Parcels/FeatureServer/38/query"
E911_URL = f"{GEOLIB}/Maine_E911_Addresses_Feature/FeatureServer/0/query"
E911_FIELDS = ["SITE_UID", "ADDRESS", "UNIT", "PLACE_TYPE", "MAP_BK_LOT", "ZIPCODE", "TOWN"]

HOUSING_COLS = ["SERIALNO", "PUMA", "WGTP", "NP", "TYPEHUGQ", "BLD", "TEN", "YRBLT", "BDSP", "VALP", "HINCP"]
PERSON_COLS = ["SERIALNO", "SPORDER", "PUMA", "PWGTP", "AGEP", "SEX", "RAC1P", "HISP", "SCHL", "RELSHIPP",
               "POVPIP", "ESR"]

# ACS jam values: -555555555 means "controlled, no sampling error"; other negatives are missing.
_CONTROLLED = -555555555


class Sources:
    def __init__(self, area: StudyArea, catalog: Catalog, data_dir: str | Path, fetcher=None):
        self.area = area
        self.catalog = catalog
        self.raw = Path(data_dir) / "raw"
        self.interim = Path(data_dir) / "interim"
        self.fetcher = fetcher or HttpFetcher()

    # ------------------------------------------------------------------ public

    def load(self, name: str):
        if name not in NAMES:
            raise KeyError(f"unknown source {name!r}; expected one of {NAMES}")
        cache = self.interim / f"{name}.parquet"
        is_geo = name not in ("pums_households", "pums_persons", "acs")
        if cache.exists():
            return gpd.read_parquet(cache) if is_geo else pd.read_parquet(cache)
        df = getattr(self, f"_load_{name}")()
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache, index=False)
        return df

    def load_all(self) -> dict:
        return {name: self.load(name) for name in NAMES}

    # ------------------------------------------------------------------ geography

    def _tiger(self, key: str, url: str) -> gpd.GeoDataFrame:
        path = self.fetcher.download(key, url, self.raw / Path(url).name)
        return gpd.read_file(f"zip://{path}").to_crs(4326)

    def _load_place(self) -> gpd.GeoDataFrame:
        a = self.area
        url = f"{CENSUS}/geo/tiger/TIGER{a.tiger_year}/PLACE/tl_{a.tiger_year}_{a.state_fips}_place.zip"
        gdf = self._tiger("tiger_place", url)
        gdf = gdf[gdf["PLACEFP"] == a.place_fips]
        if len(gdf) != 1 or gdf.iloc[0]["NAME"] != a.place_name:
            raise ValueError(f"place {a.place_fips} is not {a.place_name}: {gdf['NAME'].tolist()}")
        return gdf[["GEOID", "NAME", "geometry"]].rename(columns={"GEOID": "area_id", "NAME": "name"})

    def _within_place(self, gdf: gpd.GeoDataFrame, min_share: float = 0.5) -> gpd.GeoDataFrame:
        """Keep areas with at least `min_share` of their area inside the study area."""
        place = self.load("place").to_crs(26919).geometry.union_all()
        proj = gdf.to_crs(26919)
        share = proj.geometry.intersection(place).area / proj.geometry.area
        return gdf[share >= min_share]

    def _load_tracts(self) -> gpd.GeoDataFrame:
        a = self.area
        url = f"{CENSUS}/geo/tiger/TIGER{a.tiger_year}/TRACT/tl_{a.tiger_year}_{a.state_fips}_tract.zip"
        gdf = self._tiger("tiger_tract", url)
        gdf = self._within_place(gdf[gdf["COUNTYFP"] == a.county_fips])
        return gdf[["GEOID", "NAMELSAD", "geometry"]].rename(columns={"GEOID": "area_id", "NAMELSAD": "name"})

    def _load_block_groups(self) -> gpd.GeoDataFrame:
        a = self.area
        url = f"{CENSUS}/geo/tiger/TIGER{a.tiger_year}/BG/tl_{a.tiger_year}_{a.state_fips}_bg.zip"
        gdf = self._tiger("tiger_bg", url)
        gdf = self._within_place(gdf[gdf["COUNTYFP"] == a.county_fips])
        out = gdf[["GEOID", "NAMELSAD", "geometry"]].rename(columns={"GEOID": "area_id", "NAMELSAD": "name"})
        out["tract"] = out["area_id"].str[:11]
        return out

    def _load_blocks(self) -> gpd.GeoDataFrame:
        a = self.area
        y = a.blocks_year
        url = f"{CENSUS}/geo/tiger/TIGER{y}/TABBLOCK20/tl_{y}_{a.state_fips}_tabblock20.zip"
        gdf = self._tiger("tiger_blocks", url)
        gdf = gdf[gdf["COUNTYFP20"] == a.county_fips]
        bgs = set(self.load("block_groups")["area_id"])
        gdf = gdf[gdf["GEOID20"].str[:12].isin(bgs)]
        out = gdf[["GEOID20", "POP20", "HOUSING20", "ALAND20", "geometry"]].rename(columns={
            "GEOID20": "area_id", "POP20": "pop2020", "HOUSING20": "housing2020", "ALAND20": "aland"})
        out["block_group"] = out["area_id"].str[:12]
        out["pop2020"] = out["pop2020"].astype(int)
        out["housing2020"] = out["housing2020"].astype(int)
        return out

    # ------------------------------------------------------------------ ArcGIS

    def _arcgis(self, key: str, url: str, base: dict) -> gpd.GeoDataFrame:
        features, offset, page = [], 0, 2000
        while True:
            params = {**base, "f": "geojson", "outSR": "4326", "returnGeometry": "true",
                      "resultOffset": str(offset), "resultRecordCount": str(page)}
            body = self.fetcher.json(key, url, params)
            batch = body.get("features", [])
            features += batch
            more = body.get("exceededTransferLimit") or body.get("properties", {}).get("exceededTransferLimit")
            if not batch or not more:
                break
            offset += len(batch)
        if not features:
            raise ValueError(f"{key}: query returned no features")
        return gpd.GeoDataFrame.from_features(features, crs=4326)

    def _place_query_geometry(self) -> str:
        geom = self.load("place").geometry.iloc[0].simplify(0.0005).buffer(0.0005)
        rings = []
        for poly in getattr(geom, "geoms", [geom]):
            rings += [list(poly.exterior.coords)] + [list(r.coords) for r in poly.interiors]
        return json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}})

    def _load_parcels(self) -> gpd.GeoDataFrame:
        gdf = self._arcgis("parcels", PARCELS_URL, {
            "where": "1=1", "outFields": "Parcel_ID", "geometry": self._place_query_geometry(),
            "geometryType": "esriGeometryPolygon", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        })
        place = self.load("place").geometry.iloc[0]
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
        gdf = gdf[gdf.geometry.representative_point().within(place)]
        gdf = gdf.rename(columns={"Parcel_ID": "parcel_id"})[["parcel_id", "geometry"]]
        gdf["parcel_id"] = gdf["parcel_id"].astype(str)
        # The same ID can appear as several polygons (split lots): merge them.
        return gdf.dissolve(by="parcel_id", as_index=False)

    def _load_e911(self) -> gpd.GeoDataFrame:
        gdf = self._arcgis("e911", E911_URL, {
            "where": f"TOWN='{self.area.e911_town}'", "outFields": ",".join(E911_FIELDS),
        })
        gdf = gdf[[c for c in E911_FIELDS if c in gdf.columns] + ["geometry"]]
        gdf.columns = [c.lower() for c in gdf.columns[:-1]] + ["geometry"]
        return gdf.set_geometry("geometry")

    # ------------------------------------------------------------------ PUMS

    def _pums(self, kind: str) -> pd.DataFrame:
        a = self.area
        letter = "h" if kind == "households" else "p"
        url = f"{CENSUS}/programs-surveys/acs/data/pums/{a.pums_year}/5-Year/csv_{letter}{_state_abbr(a.state_fips)}.zip"
        path = self.fetcher.download(f"pums_{kind}", url, self.raw / Path(url).name)
        with zipfile.ZipFile(path) as z:
            member = next(n for n in z.namelist() if n.endswith(".csv"))
            header = pd.read_csv(z.open(member), nrows=0).columns
            base = HOUSING_COLS if kind == "households" else PERSON_COLS
            prefix = "WGTP" if kind == "households" else "PWGTP"
            reps = [c for c in header if c.startswith(prefix) and c[len(prefix):].isdigit()]
            cols = [c for c in base if c in header] + reps
            df = pd.read_csv(z.open(member), usecols=cols, dtype={"SERIALNO": str, "PUMA": str})
        df = df[df["PUMA"] == a.puma].reset_index(drop=True)
        if df.empty:
            raise ValueError(f"no PUMS {kind} records for PUMA {a.puma}")
        return df

    def _load_pums_households(self) -> pd.DataFrame:
        return self._pums("households")

    def _load_pums_persons(self) -> pd.DataFrame:
        return self._pums("persons")

    # ------------------------------------------------------------------ ACS

    def _load_acs(self) -> pd.DataFrame:
        """Long table: level, area_id, cell, estimate, moe for every catalog table,
        at block group and tract (study area), city, county and state."""
        a = self.area
        prefixes = {
            f"1500000US{a.county_geoid}": "block_group",
            f"1400000US{a.county_geoid}": "tract",
            f"1600000US{a.place_geoid}": "city",
            f"0500000US{a.county_geoid}": "county",
            f"0400000US{a.state_fips}": "state",
        }
        keep = {"block_group": set(self.load("block_groups")["area_id"]),
                "tract": set(self.load("tracts")["area_id"])}
        base = f"{CENSUS}/programs-surveys/acs/summary_file/{a.acs_year}/table-based-SF/data/5YRData"
        frames = []
        for table in self.catalog.acs_tables:
            url = f"{base}/acsdt5y{a.acs_year}-{table.lower()}.dat"
            lines = self.fetcher.lines(f"acs_{table.lower()}", url)
            header = next(lines).split("|")
            rows = []
            for line in lines:
                geo = line.split("|", 1)[0]
                for prefix, level in prefixes.items():
                    if geo.startswith(prefix) and (level not in ("state", "county", "city") or geo == prefix):
                        area_id = geo.split("US", 1)[1]
                        if level in keep and area_id not in keep[level]:
                            break
                        rows.append([level, area_id] + line.split("|")[1:])
                        break
            wide = pd.DataFrame(rows, columns=["level", "area_id"] + header[1:])
            frames.append(_acs_long(wide, table))
        out = pd.concat(frames, ignore_index=True)
        missing = keep["block_group"] - set(out.loc[out.level == "block_group", "area_id"])
        if missing:
            raise ValueError(f"ACS is missing {len(missing)} study-area block groups")
        return out


def _acs_long(wide: pd.DataFrame, table: str) -> pd.DataFrame:
    est = [c for c in wide.columns if "_E" in c]
    long = wide.melt(id_vars=["level", "area_id"], value_vars=est, var_name="col", value_name="estimate")
    moe = wide.melt(id_vars=["level", "area_id"], value_vars=[c.replace("_E", "_M") for c in est],
                    var_name="mcol", value_name="moe")
    long["moe"] = moe["moe"].to_numpy()
    long["cell"] = table + "_" + long["col"].str.split("_E").str[1]
    for c in ("estimate", "moe"):
        long[c] = pd.to_numeric(long[c], errors="coerce")
    long.loc[long["moe"] == _CONTROLLED, "moe"] = 0
    long.loc[long["estimate"] < 0, "estimate"] = None
    long.loc[long["moe"] < 0, "moe"] = None
    return long[["level", "area_id", "cell", "estimate", "moe"]]


def _state_abbr(fips: str) -> str:
    return {"23": "me"}.get(fips) or _raise(f"add the postal code for state {fips}")


def _raise(msg: str):
    raise ValueError(msg)
