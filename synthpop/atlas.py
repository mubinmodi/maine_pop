"""Read side: every query the API answers, over the published files in data/out/.

    atlas = Atlas.open(out_dir, catalog)
    atlas.schema()
    atlas.layer("block_group", "age65_plus", "yes", measure="share", filters=[...])
    atlas.detail("parcel", "162  D021")
    atlas.search("100 Congress")
    atlas.export("parcel", "geojson", filters=[...])
    atlas.preview({...risk model...}, "block_group")     # a data point, live, unsaved
    atlas.save_datapoint({...}); atlas.delete_datapoint("diabetes")

Every value carries `source` (synthetic or published). Synthetic values are
estimates from a synthetic population, never records of real people.
"""

from __future__ import annotations

import io
import json
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from .catalog import Catalog
from .config import ROOT
from .datapoints import PRESETS, DataPointEngine, DataPointStore, RiskModel
from .households import place
from .publish import risk_factors
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
    def __init__(self, db: duckdb.DuckDBPyConnection, catalog: Catalog, out_dir: Path,
                 engine: DataPointEngine | None = None, store: DataPointStore | None = None):
        self.db = db
        self.catalog = catalog
        self.out = out_dir
        self.engine = engine
        self.store = store
        self._people: bytes | None = None
        self._runs: list[dict] | None = None
        self._shared: tuple | None = None
        self._run_cache: OrderedDict[str, bytes] = OrderedDict()
        self._slots: pd.DataFrame | None = None

    @classmethod
    def open(cls, out_dir: str | Path, catalog: Catalog,
             datapoints_path: str | Path = ROOT / "config" / "datapoints.yaml") -> "Atlas":
        out = Path(out_dir)
        db = duckdb.connect()
        db.execute(f"CREATE TABLE stats AS SELECT * FROM read_parquet('{out / 'area_stats.parquet'}')")
        db.execute(f"CREATE TABLE areas AS SELECT * FROM read_parquet('{out / 'areas.parquet'}')")
        db.execute(f"CREATE TABLE search_index AS SELECT * FROM read_parquet('{out / 'search.parquet'}')")
        db.execute("CREATE INDEX stats_area ON stats(level, area_id)")
        db.execute("CREATE INDEX stats_var ON stats(level, variable, category)")
        city = db.execute("SELECT area_id FROM areas WHERE level = 'city'").fetchone()
        engine = DataPointEngine(out, catalog, city[0]) if city else None
        return cls(db, catalog, out, engine, DataPointStore(datapoints_path, catalog))

    def _q(self, sql: str, params: list | None = None):
        """Run a query on its own cursor: the API calls the atlas from several threads,
        and one DuckDB connection refuses concurrent calls."""
        return self.db.cursor().execute(sql, params or [])

    @property
    def _saved(self) -> dict[str, RiskModel]:
        return self.store.models if self.store else {}

    @property
    def _variables(self) -> set[str]:
        return {v["id"] for v in self.schema()["variables"]}

    # ------------------------------------------------------------------ schema

    def schema(self) -> dict:
        base = self.catalog.schema()
        totals = [{"id": k, "label": v, "kind": "total", "unit": "household" if k != "population" else "person",
                   "universe": None, "available": True, "categories": [{"id": "total", "label": v}]}
                  for k, v in TOTALS.items()]
        saved = [{"id": m.id, "label": m.label, "kind": "datapoint", "unit": "person", "universe": m.description,
                  "available": True, "categories": [{"id": "yes", "label": m.label}], "model": m.to_dict()}
                 for m in self._saved.values()]
        factors = [{"id": f, "label": self.catalog.variable(f).label,
                    "categories": [{"id": c.id, "label": c.label} for c in self.catalog.variable(f).categories]}
                   for f in risk_factors(self.catalog)]
        return {
            "levels": MAP_LEVELS,
            "compare_with": COMPARE_WITH,
            "variables": totals + [v for v in base["variables"]] + saved,
            "datapoints": {"available": bool(self.engine and self.engine.available), "factors": factors,
                           "presets": list(PRESETS.values())},
            "confidence": {"high": "CV ≤ 12%", "medium": "CV 12–40%", "low": "CV > 40%"},
            "disclosure": DISCLOSURE,
        }

    # ------------------------------------------------------------------ values

    def _frame(self, level: str, variable: str, category: str) -> pd.DataFrame:
        """area_id, estimate, se, share, share_se, confidence for one variable at one level."""
        if variable in self._saved:
            return self._datapoint_frame(self._saved[variable], level)
        return self._q(
            "SELECT area_id, estimate, se, share, share_se, confidence FROM stats "
            "WHERE level = ? AND source = 'synthetic' AND variable = ? AND category = ?",
            [level, variable, category]).fetchdf()

    def _datapoint_frame(self, model: RiskModel, level: str) -> pd.DataFrame:
        if not (self.engine and self.engine.available):
            raise ValueError("data points need the per-run cells; run `synthpop publish`")
        return self.engine.compute(model, level)

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
        all_ids = None
        keep = None
        for f in filters:
            self._check(level, f.variable)
            frame = self._frame(level, f.variable, f.category)
            col = frame[f.measure]
            ids = set(frame.loc[col.map(lambda v: _compare(v, f.op, f.value)), "area_id"])
            if f.op in ("<", "<=") and f.value >= 0:      # areas with no row have 0, which passes
                if all_ids is None:
                    all_ids = {r[0] for r in self._q("SELECT area_id FROM areas WHERE level = ?", [level]).fetchall()}
                ids |= all_ids - set(frame["area_id"])
            keep = ids if keep is None else keep & ids
        return keep

    # ------------------------------------------------------------------ layer

    def layer(self, level: str, variable: str, category: str = "total", measure: str = "estimate",
              filters: list[Filter] | None = None) -> dict:
        """Values for every area at one level, for colouring or extruding the map."""
        self._check(level, variable)
        out = self._layer_from(self._frame(level, variable, category), measure, self._passing(level, filters or []))
        return {"level": level, "variable": variable, "category": category, **out}

    @staticmethod
    def _layer_from(rows: pd.DataFrame, measure: str, passing: set[str] | None) -> dict:
        value = rows["share"] if measure == "share" else rows["estimate"]
        error = rows["share_se"] if measure == "share" else rows["se"]
        positive = value[value > 0]
        breaks = positive.quantile([0.2, 0.4, 0.6, 0.8]).round(6).tolist() if len(positive) else []
        ids = rows["area_id"].tolist()
        ok = [True] * len(ids) if passing is None else [a in passing for a in ids]
        return {
            "measure": measure, "source": "synthetic",
            "values": {a: [v, e, c, k] for a, v, e, c, k in zip(
                ids, np.round(value.to_numpy(float), 6).tolist(), np.round(error.to_numpy(float), 6).tolist(),
                rows["confidence"].tolist(), ok)},
            "fields": ["value", "se", "confidence", "passes_filters"],
            "breaks": breaks, "max": float(value.max()) if len(value) else 0.0,
            "filtered_count": None if passing is None else len(passing),
        }

    # ------------------------------------------------------------------ people

    def people(self, run: str | None = None) -> bytes:
        """One run's synthetic persons as one columnar JSON document (the main run unless
        `run` names another). Each attribute is a list of codes into that variable's schema
        categories (-1 for blank), so the app can colour, filter and apply risk models with
        no more requests. The main run is built once; the last few other runs are cached."""
        if run in (None, "", "main"):
            if self._people is None:
                path = self.out / "people.parquet"
                if not path.exists():
                    raise KeyError("no people published; run `synthpop publish`")
                self._people = self._people_body(pd.read_parquet(path))
            return self._people
        if run not in {r["name"] for r in self.people_runs()}:
            raise KeyError(f"no run {run!r}")
        if run in self._run_cache:
            self._run_cache.move_to_end(run)
            return self._run_cache[run]
        body = self._people_body(self._run_frame(run))
        self._run_cache[run] = body
        while len(self._run_cache) > 8:
            self._run_cache.popitem(last=False)
        return body

    def _run_frame(self, run: str) -> pd.DataFrame:
        """A run's persons rebuilt from people_runs/: the run's own columns joined to the
        shared seed people, map points and u stream (see publish_people_runs)."""
        d = self.out / "people_runs"
        if self._shared is None:
            self._shared = (pd.read_parquet(d / "seeds.parquet").set_index("seed"),
                            pd.read_parquet(d / "points.parquet").set_index("point"),
                            pd.read_parquet(d / "u.parquet")["u"].to_numpy())
        seeds, points, u = self._shared
        f = pd.read_parquet(d / f"{run}.parquet")
        p = pd.concat([f[["household_id"]], seeds.loc[f["seed"]].reset_index(drop=True),
                       points.loc[f["point"]].reset_index(drop=True)], axis=1)
        p["family_id"] = p["household_id"].where(f["in_family"].to_numpy())
        p["u"] = u[:len(p)]
        return p

    def people_runs(self) -> list[dict]:
        """The runs the app can play, in order (main, placement draws, replicates,
        validation), each with its kind and how many people and households it has."""
        if self._runs is None:
            path = self.out / "people_runs" / "index.json"
            self._runs = json.loads(path.read_text()) if path.exists() else [{"name": "main", "kind": "main"}]
        return self._runs

    def household_slots(self) -> pd.DataFrame | None:
        if self._slots is None:
            path = self.out / "household_slots.parquet"
            self._slots = pd.read_parquet(path) if path.exists() else pd.DataFrame()
        return self._slots if len(self._slots) else None

    def _people_body(self, p: pd.DataFrame) -> bytes:
        attrs = [v.id for v in self.catalog.active if v.id in p.columns]
        codes = {}
        for a in attrs:
            index = {c: i for i, c in enumerate(self.catalog.variable(a).category_ids)}
            codes[a] = p[a].astype(str).map(index).fillna(-1).astype(int).tolist()
        parcels, parcel_code = np.unique(p["parcel_id"].to_numpy(dtype=str), return_inverse=True)
        blocks, block_code = np.unique(p["block"].to_numpy(dtype=str), return_inverse=True)
        unit, u = place(p, self.household_slots())
        body = {
            "count": len(p), "lon": p["lon"].tolist(), "lat": p["lat"].tolist(),
            "age": p["age"].fillna(-1).astype(int).tolist(),
            # Income as % of the poverty line (-1 where poverty status is not determined).
            "poverty_ratio": p["poverty_ratio"].fillna(-1).astype(int).tolist() if "poverty_ratio" in p else [-1] * len(p),
            "u": p["u"].astype(float).round(4).tolist(),
            "household": p["household_id"].astype(int).tolist(),
            "family": p["family_id"].fillna(-1).astype(int).tolist() if "family_id" in p else [-1] * len(p),
            "parcel": parcel_code.tolist(), "parcels": parcels.tolist(),
            # Block ids start with their block group (12 digits) and tract (11), so one code covers every level.
            "block": block_code.tolist(), "blocks": blocks.tolist(),
            "fallback": (p["location"] == "parcel").astype(int).tolist(),
            "attributes": attrs, "codes": codes,
            # 1 for the householder, who names the household's colour on the map.
            "head": p["head"].astype(int).tolist() if "head" in p else [0] * len(p),
            # Households (and each parcel's group-quarters residents) as circles spread over
            # their parcel: each person's unit, and each unit's centre and radius in metres.
            "unit": unit.tolist(),
            "units": {"lon": u["lon"].round(6).tolist(), "lat": u["lat"].round(6).tolist(), "r": np.maximum(np.floor(u["r"].to_numpy() * 10) / 10, 0.3).tolist()},
        }
        return json.dumps(body, separators=(",", ":")).encode()

    # ------------------------------------------------------------------ data points

    def preview(self, raw: dict, level: str, measure: str = "share", filters: list[Filter] | None = None) -> dict:
        """A data point computed live from an unsaved risk model: map values for one
        level, the Portland-wide result and the breakdown by each risk factor."""
        if level not in COMPARE_WITH:
            raise KeyError(f"unknown level {level!r}")
        model = RiskModel.parse(raw, self.catalog)
        layer = self._layer_from(self._datapoint_frame(model, level), measure, self._passing(level, filters or []))
        city = self._datapoint_frame(model, "city").iloc[0]
        return {"level": level, "variable": model.id, "category": "yes", **layer,
                "city": {"estimate": round(float(city.estimate), 2), "se": round(float(city.se), 2),
                         "share": round(float(city.share), 5), "share_se": round(float(city.share_se), 5),
                         "confidence": city.confidence},
                "breakdown": self.engine.breakdown(model), "model": model.to_dict()}

    def save_datapoint(self, raw: dict) -> dict:
        model = RiskModel.parse(raw, self.catalog)
        taken = {v.id for v in self.catalog.variables} | set(TOTALS)
        self.store.save(model, taken)
        return model.to_dict()

    def delete_datapoint(self, id: str) -> None:
        self.store.delete(id)

    def _datapoint_values(self, level: str, area_id: str, models) -> dict:
        out = {}
        if not (self.engine and self.engine.available) or level not in COMPARE_WITH:
            return out
        for m in models:
            f = self._datapoint_frame(m, level)
            row = f[f["area_id"] == area_id]
            if len(row):
                r = row.iloc[0]
                out[m.id] = {"yes": {"estimate": round(float(r.estimate), 3), "se": round(float(r.se), 3),
                                     "share": round(float(r.share), 5), "share_se": round(float(r.share_se), 5),
                                     "confidence": r.confidence}}
        return out

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

    def detail(self, level: str, area_id: str, extra: dict | None = None) -> dict:
        """Everything the click panel shows for one area. `extra` is an unsaved risk
        model to include, so the panel can show a data point while it is being edited."""
        if level not in COMPARE_WITH:
            raise KeyError(f"unknown level {level!r}")
        models = list(self._saved.values()) + ([RiskModel.parse(extra, self.catalog)] if extra else [])
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
                if source == "synthetic":
                    vals.update(self._datapoint_values(cl, cid, models))
                if vals:
                    comparisons.append({"level": cl, "area_id": cid, "name": name, "source": source, "values": vals})
        values = self._values(level, area_id, "synthetic")
        values.update(self._datapoint_values(level, area_id, models))
        published = self._values(level, area_id, "published") if level in ("block_group", "tract", "city") else {}
        return {"area": a, "source": "synthetic", "values": values, "published": published,
                "comparisons": comparisons, "datapoints": [{"id": m.id, "label": m.label} for m in models],
                "disclosure": DISCLOSURE}

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
        for m in self._saved.values():
            f = self._datapoint_frame(m, level)
            stats = pd.concat([stats, pd.DataFrame({"area_id": f["area_id"], "col": f"{m.id}.yes",
                                                    "estimate": f["estimate"], "se": f["se"]})], ignore_index=True)
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


def _compare(v: float, op: str, threshold: float) -> bool:
    return {">": v > threshold, ">=": v >= threshold, "<": v < threshold, "<=": v <= threshold}[op]
