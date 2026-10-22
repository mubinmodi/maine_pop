"""Read side: every query the API answers, over the published files in data/out/.

    atlas = Atlas.open(out_dir, catalog)
    atlas.schema()
    atlas.layer("block_group", "age65_plus", "yes", measure="share", filters=[...])
    atlas.detail("parcel", "162  D021")
    atlas.search("100 Congress")
    atlas.export("parcel", "geojson", filters=[...])

Every value carries `source` (synthetic or published). Synthetic values are
estimates from a synthetic population, never records of real people.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from .catalog import Catalog
from .summarize import TOTALS

MAP_LEVELS = [  # zoom ranges the app switches on
    {"id": "city", "label": "City", "min_zoom": 0, "max_zoom": 11},
    {"id": "tract", "label": "Census tract", "min_zoom": 11, "max_zoom": 12.5},
    {"id": "block_group", "label": "Block group", "min_zoom": 12.5, "max_zoom": 14},
    {"id": "block", "label": "Census block", "min_zoom": 14, "max_zoom": 15.5},
    {"id": "parcel", "label": "Parcel", "min_zoom": 15.5, "max_zoom": 24},
]
COMPARE_WITH = {  # where each level's detail panel looks for context
    "parcel": ["block", "block_group", "city", "county", "state"],
    "block": ["block_group", "city", "county", "state"],
    "block_group": ["city", "county", "state"],
    "tract": ["city", "county", "state"],
    "city": ["county", "state"],
}
OPS = {">": ">", ">=": ">=", "<": "<", "<=": "<="}
DISCLOSURE = ("All values inside Portland are synthetic estimates from a modelled population built from public "
              "Census and State of Maine data. They describe likely distributions, not real residents.")


@dataclass
class Filter:
    variable: str
    category: str
    op: str
    value: float
    measure: str = "share"          # "share" (0–1) or "estimate" (count)

    @classmethod
    def parse(cls, raw: dict) -> "Filter":
        f = cls(raw["variable"], str(raw.get("category", "total")), raw.get("op", ">"), float(raw["value"]),
                raw.get("measure", "share"))
        if f.op not in OPS or f.measure not in ("share", "estimate"):
            raise ValueError(f"bad filter {raw}")
        return f


class Atlas:
    def __init__(self, db: duckdb.DuckDBPyConnection, catalog: Catalog, out_dir: Path):
        self.db = db
        self.catalog = catalog
        self.out = out_dir
        self._variables = {v["id"] for v in self.schema()["variables"]}

    @classmethod
    def open(cls, out_dir: str | Path, catalog: Catalog) -> "Atlas":
        out = Path(out_dir)
        db = duckdb.connect()
        db.execute(f"CREATE TABLE stats AS SELECT * FROM read_parquet('{out / 'area_stats.parquet'}')")
        db.execute(f"CREATE TABLE areas AS SELECT * FROM read_parquet('{out / 'areas.parquet'}')")
        db.execute(f"CREATE TABLE search_index AS SELECT * FROM read_parquet('{out / 'search.parquet'}')")
        db.execute("CREATE INDEX stats_area ON stats(level, area_id)")
        db.execute("CREATE INDEX stats_var ON stats(level, variable, category)")
        return cls(db, catalog, out)

    def _q(self, sql: str, params: list | None = None):
        """Run a query on its own cursor: the API calls the atlas from several threads,
        and one DuckDB connection refuses concurrent calls."""
        return self.db.cursor().execute(sql, params or [])

    # ------------------------------------------------------------------ schema

    def schema(self) -> dict:
        base = self.catalog.schema()
        totals = [{"id": k, "label": v, "kind": "total", "unit": "household" if k != "population" else "person",
                   "universe": None, "available": True, "categories": [{"id": "total", "label": v}]}
                  for k, v in TOTALS.items()]
        return {
            "levels": MAP_LEVELS,
            "compare_with": COMPARE_WITH,
            "variables": totals + [v for v in base["variables"]],
            "confidence": {"high": "CV ≤ 12%", "medium": "CV 12–40%", "low": "CV > 40%"},
            "disclosure": DISCLOSURE,
        }

    def _check(self, level: str, variable: str) -> None:
        if level not in COMPARE_WITH:
            raise KeyError(f"unknown level {level!r}")
        if variable not in self._variables:
            raise KeyError(f"unknown variable {variable!r}")

    # ------------------------------------------------------------------ filters

    def _passing(self, level: str, filters: list[Filter]) -> set[str] | None:
        """Areas at `level` meeting every filter; None when there are no filters."""
        if not filters:
            return None
        keep = None
        for f in filters:
            self._check(level, f.variable)
            ids = self._q(
                f"SELECT area_id FROM stats WHERE level = ? AND source = 'synthetic' AND variable = ? "
                f"AND category = ? AND {f.measure} {OPS[f.op]} ?", [level, f.variable, f.category, f.value]).fetchall()
            ids = {r[0] for r in ids}
            if f.op in ("<", "<=") and f.value >= 0:      # areas with no row have 0, which passes
                all_ids = {r[0] for r in self._q("SELECT area_id FROM areas WHERE level = ?", [level]).fetchall()}
                present = {r[0] for r in self._q(
                    "SELECT area_id FROM stats WHERE level = ? AND source='synthetic' AND variable = ? AND category = ?",
                    [level, f.variable, f.category]).fetchall()}
                ids |= all_ids - present
            keep = ids if keep is None else keep & ids
        return keep

    # ------------------------------------------------------------------ layer

    def layer(self, level: str, variable: str, category: str = "total", measure: str = "estimate",
              filters: list[Filter] | None = None) -> dict:
        """Values for every area at one level, for colouring or extruding the map."""
        self._check(level, variable)
        rows = self._q(
            "SELECT area_id, estimate, se, share, share_se, confidence FROM stats "
            "WHERE level = ? AND source = 'synthetic' AND variable = ? AND category = ?",
            [level, variable, category]).fetchdf()
        value = rows["share"] if measure == "share" else rows["estimate"]
        error = rows["share_se"] if measure == "share" else rows["se"]
        passing = self._passing(level, filters or [])
        positive = value[value > 0]
        breaks = positive.quantile([0.2, 0.4, 0.6, 0.8]).round(6).tolist() if len(positive) else []
        return {
            "level": level, "variable": variable, "category": category, "measure": measure, "source": "synthetic",
            "values": {a: [round(float(v), 6), round(float(e), 6), c, (passing is None or a in passing)]
                       for a, v, e, c in zip(rows["area_id"], value, error, rows["confidence"])},
            "fields": ["value", "se", "confidence", "passes_filters"],
            "breaks": breaks, "max": float(value.max()) if len(value) else 0.0,
            "filtered_count": None if passing is None else len(passing),
        }

    # ------------------------------------------------------------------ detail

    def _values(self, level: str, area_id: str, source: str) -> dict:
        df = self._q(
            "SELECT variable, category, estimate, se, share, share_se, confidence FROM stats "
            "WHERE level = ? AND area_id = ? AND source = ?", [level, area_id, source]).fetchdf()
        out: dict = {}
        for r in df.itertuples(index=False):
            out.setdefault(r.variable, {})[r.category] = {
                "estimate": round(r.estimate, 3), "se": round(r.se, 3), "share": round(r.share, 5),
                "share_se": round(r.share_se, 5), "confidence": r.confidence}
        return out

    def area(self, level: str, area_id: str) -> dict:
        row = self._q("SELECT * FROM areas WHERE level = ? AND area_id = ?", [level, area_id]).fetchdf()
        if row.empty:
            raise KeyError(f"no {level} {area_id!r}")
        r = row.iloc[0].to_dict()
        r["attrs"] = json.loads(r["attrs"]) if isinstance(r.get("attrs"), str) else {}
        return {k: (None if pd.isna(v) else v) if not isinstance(v, dict) else v for k, v in r.items()}

    def detail(self, level: str, area_id: str) -> dict:
        """Everything the click panel shows for one area."""
        if level not in COMPARE_WITH:
            raise KeyError(f"unknown level {level!r}")
        a = self.area(level, area_id)
        comparisons = []
        for cl in COMPARE_WITH[level]:
            cid = a.get(cl)
            if not cid:
                continue
            try:
                name = self.area(cl, cid)["name"]
            except KeyError:
                name = cid
            for source in ("synthetic", "published"):
                vals = self._values(cl, cid, source)
                if vals:
                    comparisons.append({"level": cl, "area_id": cid, "name": name, "source": source, "values": vals})
        values = self._values(level, area_id, "synthetic")
        published = self._values(level, area_id, "published") if level in ("block_group", "tract", "city") else {}
        return {"area": a, "source": "synthetic", "values": values, "published": published,
                "comparisons": comparisons, "disclosure": DISCLOSURE}

    # ------------------------------------------------------------------ search

    def search(self, q: str, limit: int = 10) -> list[dict]:
        q = q.strip()
        if len(q) < 2:
            return []
        df = self._q(
            "SELECT label, level, area_id, lon, lat, kind FROM search_index WHERE label ILIKE ? "
            "ORDER BY CASE WHEN label ILIKE ? THEN 0 ELSE 1 END, kind DESC, length(label) LIMIT ?",
            [f"%{q}%", f"{q}%", limit]).fetchdf()
        return df.where(df.notna(), None).to_dict("records")

    # ------------------------------------------------------------------ export

    def export(self, level: str, fmt: str = "csv", area_ids: list[str] | None = None,
               filters: list[Filter] | None = None) -> tuple[bytes, str]:
        """The selected areas at one level, every variable, as CSV or GeoJSON."""
        if level not in COMPARE_WITH:
            raise KeyError(f"unknown level {level!r}")
        ids = self._passing(level, filters or [])
        if area_ids:
            ids = set(area_ids) if ids is None else ids & set(area_ids)
        areas = self._q("SELECT area_id, name, lon, lat FROM areas WHERE level = ?", [level]).fetchdf()
        if ids is not None:
            areas = areas[areas["area_id"].isin(ids)]
        stats = self._q(
            "SELECT area_id, variable || '.' || category AS col, estimate, se FROM stats "
            "WHERE level = ? AND source = 'synthetic'", [level]).fetchdf()
        stats = stats[stats["area_id"].isin(set(areas["area_id"]))]
        wide = stats.pivot_table(index="area_id", columns="col", values=["estimate", "se"], aggfunc="first")
        wide.columns = [c if m == "estimate" else f"{c}.se" for m, c in wide.columns]
        wide = wide[sorted(wide.columns)]
        table = areas.set_index("area_id").join(wide).reset_index()
        table.insert(1, "source", "synthetic estimate")
        if fmt == "csv":
            buf = io.StringIO()
            buf.write(f"# {DISCLOSURE}\n")
            table.to_csv(buf, index=False)
            return buf.getvalue().encode(), "text/csv"
        if fmt == "geojson":
            geo = json.loads((self.out / "geo" / f"{level}.geojson").read_text())
            props = table.set_index("area_id").where(table.set_index("area_id").notna(), None).to_dict("index")
            feats = [{"type": "Feature", "geometry": f["geometry"], "properties": {"area_id": f["properties"]["id"],
                      **props[f["properties"]["id"]]}} for f in geo["features"] if f["properties"]["id"] in props]
            body = {"type": "FeatureCollection", "disclosure": DISCLOSURE, "features": feats}
            return json.dumps(body).encode(), "application/geo+json"
        raise ValueError(f"unknown format {fmt!r}")
