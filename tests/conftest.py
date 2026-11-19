"""A tiny fixture town that mirrors the shape of every real input.

Two block groups (one tract) inside the study area, one block group outside,
a handful of blocks, parcels and address points, and PUMS records in two PUMAs.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, box

from synthpop.catalog import Catalog
from synthpop.config import ROOT, StudyArea
from synthpop.sources import FixtureFetcher, Sources

X0, Y0 = -70.30, 43.65          # south-west corner of the fixture place
W = 0.02                        # place is a 0.02° square

AREA = replace(StudyArea.load(), name="Fixture", place_fips="99999", place_name="Fixtureville",
               puma="01000", e911_town="Fixtureville", replicates=4)
CATALOG_PATH = ROOT / "config" / "variables.yaml"

BG_IN = ["230050001001", "230050001002"]
BG_OUT = "230050002001"
TRACT = "23005000100"
TABLE_WIDTH = {"B01001": 49, "B03002": 21, "B11016": 16, "B15003": 25, "B23025": 7, "C17002": 8, "B25002": 3, "B25008": 3, "B25032": 23, "C18108": 13, "B27010": 66}


def _shapefile_zip(gdf: gpd.GeoDataFrame, path: Path) -> None:
    tmp = path.with_suffix("")
    tmp.mkdir(parents=True, exist_ok=True)
    gdf.to_file(tmp / "layer.shp")
    with zipfile.ZipFile(path, "w") as z:
        for f in tmp.iterdir():
            z.write(f, f.name)


def _geojson(gdf: gpd.GeoDataFrame, path: Path) -> None:
    path.write_text(json.dumps(json.loads(gdf.to_json()) | {"exceededTransferLimit": False}))


def build_fixtures(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    left = box(X0, Y0, X0 + W / 2, Y0 + W)
    right = box(X0 + W / 2, Y0, X0 + W, Y0 + W)
    outside = box(X0 + W, Y0, X0 + 2 * W, Y0 + W)

    _shapefile_zip(gpd.GeoDataFrame({"PLACEFP": ["99999", "11111"], "GEOID": ["2399999", "2311111"],
                                     "NAME": ["Fixtureville", "Elsewhere"]},
                                    geometry=[box(X0, Y0, X0 + W, Y0 + W), outside], crs=4326),
                   root / "tiger_place.zip")
    _shapefile_zip(gpd.GeoDataFrame({"COUNTYFP": ["005", "005"], "GEOID": [TRACT, "23005000200"],
                                     "NAMELSAD": ["Census Tract 1", "Census Tract 2"]},
                                    geometry=[box(X0, Y0, X0 + W, Y0 + W), outside], crs=4326),
                   root / "tiger_tract.zip")
    _shapefile_zip(gpd.GeoDataFrame({"COUNTYFP": ["005"] * 3, "GEOID": BG_IN + [BG_OUT],
                                     "NAMELSAD": ["Block Group 1", "Block Group 2", "Block Group 1"]},
                                    geometry=[left, right, outside], crs=4326),
                   root / "tiger_bg.zip")

    blocks = []
    for bg, cell in ((BG_IN[0], left), (BG_IN[1], right), (BG_OUT, outside)):
        x0, y0, x1, y1 = cell.bounds
        mid = (y0 + y1) / 2
        for i, b in enumerate((box(x0, y0, x1, mid), box(x0, mid, x1, y1))):
            blocks.append({"GEOID20": f"{bg[:11]}{bg[11]}{i:03d}", "COUNTYFP20": "005", "POP20": 20 + i,
                           "HOUSING20": 10 + i, "ALAND20": 1000, "geometry": b})
    _shapefile_zip(gpd.GeoDataFrame(blocks, crs=4326), root / "tiger_blocks.zip")

    parcels, points = [], []
    for j, blk in enumerate(blocks):
        x0, y0, x1, y1 = blk["geometry"].bounds
        for k in range(2):
            pid = f"P{j}{k}"
            parcels.append({"Parcel_ID": pid, "geometry": box(x0 + k * (x1 - x0) / 2, y0, x0 + (k + 1) * (x1 - x0) / 2, y1)})
            c = parcels[-1]["geometry"].centroid
            for u in range(1 + k * 2):          # left parcel: 1 unit, right parcel: 3 units
                points.append({"SITE_UID": f"S{j}{k}{u}", "ADDRESS": f"{j}{k} Fixture St", "UNIT": str(u) if k else None,
                               "PLACE_TYPE": "Apartment" if k else "Single Family", "MAP_BK_LOT": pid,
                               "ZIPCODE": "04101", "TOWN": "Fixtureville", "geometry": Point(c.x, c.y)})
    parcels.append({"Parcel_ID": "P00", "geometry": box(X0, Y0, X0 + 0.0001, Y0 + 0.0001)})  # split lot
    _geojson(gpd.GeoDataFrame(parcels, crs=4326), root / "parcels.json")
    _geojson(gpd.GeoDataFrame(points, crs=4326), root / "e911.json")

    h = pd.DataFrame({
        "SERIALNO": ["H1", "H2", "H3", "GQ1", "H9"], "PUMA": ["01000"] * 4 + ["00900"],
        "WGTP": [50, 60, 40, 0, 99], "NP": [1, 2, 3, 1, 1], "TYPEHUGQ": [1, 1, 1, 3, 1],
        "BLD": [2, 6, 4, None, 2], "TEN": [1, 3, 3, None, 1], "YRBLT": [1950, 1990, 1920, None, 2000],
        "BDSP": [3, 1, 2, None, 2], "VALP": [300000, None, None, None, 1], "HINCP": [50000, 30000, 40000, None, 1],
        **{f"WGTP{r}": [50 + r, 60 - r, 40, 0, 99] for r in range(1, 81)},
    })
    p = pd.DataFrame({
        "SERIALNO": ["H1", "H2", "H2", "H3", "H3", "H3", "GQ1", "H9"], "SPORDER": [1, 1, 2, 1, 2, 3, 1, 1],
        "PUMA": ["01000"] * 7 + ["00900"], "PWGTP": [50, 60, 60, 40, 40, 40, 30, 99],
        "AGEP": [72, 34, 31, 45, 12, 2, 88, 50], "SEX": [2, 1, 2, 1, 2, 1, 2, 1],
        "RAC1P": [1, 2, 1, 6, 6, 6, 1, 1], "HISP": [1, 1, 2, 1, 1, 1, 1, 1],
        "SCHL": [14, 21, 19, 16, 9, None, 12, 16], "RELSHIPP": [20, 20, 21, 20, 25, 25, 37, 20],
        "POVPIP": [250, 90, 90, 150, 150, 150, None, 300], "ESR": [6, 1, 3, 1, None, None, 6, 1],
        "DIS": [1, 2, 2, 2, 1, 2, 1, 2], "HICOV": [1, 1, 2, 1, 1, 1, 1, 1], "MIL": [4, 4, 4, 2, None, None, 4, 4],
        **{f"PWGTP{r}": [50, 60, 60, 40, 40, 40, 30, 99] for r in range(1, 81)},
    })
    for name, df, member in (("pums_households", h, "psam_h23.csv"), ("pums_persons", p, "psam_p23.csv")):
        with zipfile.ZipFile(root / f"{name}.zip", "w") as z:
            z.writestr(member, df.to_csv(index=False))

    geos = ([f"1500000US{bg}" for bg in BG_IN + [BG_OUT]] + [f"1400000US{TRACT}", "1600000US2399999",
            "0500000US23005", "0500000US23999", "0400000US23", "0400000US24"])
    for table, width in TABLE_WIDTH.items():
        cols = [f"{table}_{k}{i:03d}" for i in range(1, width + 1) for k in "EM"]
        rows = ["|".join(["GEO_ID"] + cols)]
        for g in geos:
            vals = []
            for i in range(1, width + 1):
                vals += [str(10 * i), "-555555555" if i == 1 else "5"]
            rows.append("|".join([g] + vals))
        (root / f"acs_{table.lower()}.dat").write_text("\n".join(rows) + "\n")

    head = "year,locationid,measureid,measure,datavaluetypeid,data_value,low_confidence_limit,high_confidence_limit,totalpop18plus"
    places = {"tract": [f"2023,{TRACT},DIABETES,Diagnosed diabetes among adults,CrdPrv,8.1,7.0,9.4,4939",
                        "2023,23005000200,DIABETES,Diagnosed diabetes among adults,CrdPrv,6.7,5.8,7.7,2113"],
              "city": ["2023,2399999,DIABETES,Diagnosed diabetes among adults,CrdPrv,8.3,7.3,9.3,",
                       "2023,2399999,DIABETES,Diagnosed diabetes among adults,AgeAdjPrv,8.1,7.1,9.0,"],
              "county": ["2023,23005,DIABETES,Diagnosed diabetes among adults,CrdPrv,8.9,7.9,10.0,"]}
    for level, rows in places.items():
        (root / f"places_{level}.csv").write_text("\n".join([head] + rows) + "\n")
    return root


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory) -> Path:
    return build_fixtures(tmp_path_factory.mktemp("fixtures"))


class CountingFetcher(FixtureFetcher):
    def __init__(self, root):
        super().__init__(root)
        self.calls = 0

    def download(self, *a):
        self.calls += 1
        return super().download(*a)

    def json(self, *a):
        self.calls += 1
        return super().json(*a)

    def lines(self, *a):
        self.calls += 1
        return super().lines(*a)


@pytest.fixture
def catalog() -> Catalog:
    return Catalog.load(CATALOG_PATH)


@pytest.fixture
def sources(fixture_dir, tmp_path, catalog) -> Sources:
    return Sources(AREA, catalog, tmp_path / "data", CountingFetcher(fixture_dir))
