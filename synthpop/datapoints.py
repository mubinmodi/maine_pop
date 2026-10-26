"""Data points: health or other measures attached to synthetic people by a risk model,
computed live and optionally saved.

    model = RiskModel.parse({...})
    engine = DataPointEngine(out_dir, catalog, city_id)
    frame = engine.compute(model, "block_group")   # area_id, estimate, se, share, share_se, confidence
    store = DataPointStore("config/datapoints.yaml")

A risk model gives every synthetic person a probability:

    logit p = logit(baseline) + Σ log(odds ratio of the person's category, per factor)

`baseline` is the prevalence for the reference person (every factor at a category
with no odds ratio). People outside the universe get p = 0 and are not counted
in shares. An area's estimate is its expected number of cases, so values add up
across levels exactly as the other variables do. Uncertainty combines placement
spread (21 draws) and survey spread (80 replicates), as in summarize.py. It does
not include uncertainty in the odds ratios themselves.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from .catalog import Catalog
from .publish import risk_factors
from .summarize import confidence

LEVEL_EXPR = {"parcel": "parcel_id", "block": "block", "block_group": "block_group",
              "tract": "substr(block_group, 1, 11)"}
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


@dataclass
class RiskModel:
    id: str
    label: str
    baseline: float                                  # prevalence for the reference person, 0–1
    odds_ratios: dict[str, dict[str, float]] = field(default_factory=dict)
    universe: dict[str, list[str]] = field(default_factory=dict)   # factor -> categories counted
    description: str = ""
    source: str = ""

    @classmethod
    def parse(cls, raw: dict, catalog: Catalog) -> "RiskModel":
        factors = {f: set(catalog.variable(f).category_ids) for f in risk_factors(catalog)}
        m = cls(id=str(raw["id"]), label=str(raw.get("label") or raw["id"]), baseline=float(raw["baseline"]),
                odds_ratios={f: {c: float(v) for c, v in cats.items()} for f, cats in (raw.get("odds_ratios") or {}).items()},
                universe={f: [str(c) for c in cats] for f, cats in (raw.get("universe") or {}).items()},
                description=str(raw.get("description", "")), source=str(raw.get("source", "")))
        if not ID_PATTERN.match(m.id):
            raise ValueError("id must be lower_snake_case, 2–41 characters, starting with a letter")
        if not 0 < m.baseline < 1:
            raise ValueError("baseline must be a prevalence between 0 and 1 (exclusive)")
        for f, cats in {**m.odds_ratios, **{k: dict.fromkeys(v, 1) for k, v in m.universe.items()}}.items():
            if f not in factors:
                raise ValueError(f"unknown factor {f!r}; use one of {sorted(factors)}")
            bad = set(cats) - factors[f]
            if bad:
                raise ValueError(f"unknown {f} categories {sorted(bad)}")
        for f, cats in m.odds_ratios.items():
            for c, v in cats.items():
                if not 0 < v < 1000:
                    raise ValueError(f"odds ratio for {f}={c} must be between 0 and 1000")
        return m

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def key(self) -> str:
        return hashlib.sha1(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()

    def probabilities(self, cells: pd.DataFrame) -> pd.DataFrame:
        """`cells` has one column per factor; returns it with p and in_universe (0/1)."""
        logit = np.full(len(cells), math.log(self.baseline / (1 - self.baseline)))
        for f, cats in self.odds_ratios.items():
            logit += cells[f].map({c: math.log(v) for c, v in cats.items()}).fillna(0.0).to_numpy()
        inside = np.ones(len(cells), dtype=bool)
        for f, cats in self.universe.items():
            inside &= cells[f].isin(cats).to_numpy()
        p = 1 / (1 + np.exp(-logit))
        return cells.assign(p=np.where(inside, p, 0.0), in_universe=inside.astype(float))


# Alzheimer's disease dementia, from Dhana et al. 2023 (Alzheimer's & Dementia 19:4388), Table 1:
# a quasibinomial model of the CHAP likelihood score. Intercept −3.455 is for a White man aged
# 65–69 at mean schooling (12.3 years). Education is per 3.5 years; our categories are mapped to
# about 10 / 12 / 14 / 16.5 years and taken relative to the high-school group, whose shift is
# folded into the baseline. Asian and other groups keep White-level risk, as in the paper.
def _dhana() -> dict:
    per_sd, mean_years = math.log(0.67), 12.3
    years = {"lt_hs": 10.0, "hs": 12.0, "some_college": 14.0, "ba_plus": 16.5}
    z = {c: (y - mean_years) / 3.5 for c, y in years.items()}
    baseline_logit = -3.455 + per_sd * z["hs"]
    return {
        "id": "alzheimers_dhana", "label": "Alzheimer's dementia (Dhana 2023 model)",
        "baseline": round(1 / (1 + math.exp(-baseline_logit)), 5),
        "odds_ratios": {
            "age_group": {"a70_74": 1.78, "a75_79": 3.08, "a80_84": 6.05, "a85_plus": 14.78},
            "sex": {"female": 1.13},
            "race_eth": {"black": 2.5, "hispanic": 1.73},
            "education": {c: round(math.exp(per_sd * (z[c] - z["hs"])), 3) for c in ("lt_hs", "some_college", "ba_plus")},
        },
        "universe": {"age_group": ["a65_69", "a70_74", "a75_79", "a80_84", "a85_plus"]},
        "description": "Expected people 65+ with Alzheimer's disease dementia. Reference person: White man aged "
                       "65–69 with a high school education.",
        "source": "Dhana K et al. Alzheimer's & Dementia 2023;19:4388–4395, Table 1. doi:10.1002/alz.13081",
    }


PRESETS = {"alzheimers_dhana": _dhana()}


class DataPointEngine:
    """Computes a risk model's expected cases for every area at a level, with margins."""

    def __init__(self, out_dir: str | Path, catalog: Catalog, city_id: str):
        out = Path(out_dir)
        self.catalog = catalog
        self.city_id = city_id
        self.factors = risk_factors(catalog)
        self.db = duckdb.connect()
        self.available = (out / "cells_draws.parquet").exists()
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}
        self._lock = threading.Lock()
        if not self.available:
            return
        self.db.execute(f"CREATE TABLE draws AS SELECT * FROM read_parquet('{out / 'cells_draws.parquet'}')")
        reps = out / "cells_reps.parquet"
        if reps.exists():
            self.db.execute(f"CREATE TABLE reps AS SELECT * FROM read_parquet('{reps}')")
        else:
            self.db.execute("CREATE TABLE reps AS SELECT * FROM draws WHERE false")
        cols = ", ".join(self.factors)
        self.cells = self.db.execute(f"SELECT DISTINCT {cols} FROM draws UNION SELECT DISTINCT {cols} FROM reps").fetchdf()
        self.n_draws = self.db.execute("SELECT count(DISTINCT run) FROM draws").fetchone()[0]
        self.n_reps = self.db.execute("SELECT count(DISTINCT run) FROM reps").fetchone()[0]

    def _cursor(self, model: RiskModel):
        cur = self.db.cursor()
        cur.register("probs", model.probabilities(self.cells))
        return cur

    def _by_run(self, cur, table: str, area_expr: str) -> pd.DataFrame:
        join = " AND ".join(f"t.{f} = probs.{f}" for f in self.factors)
        return cur.execute(
            f"SELECT run, {area_expr} AS area_id, SUM(n * p) AS cases, SUM(n * in_universe) AS uni "
            f"FROM {table} t JOIN probs ON {join} GROUP BY run, area_id").fetchdf()

    def _level(self, cur, level: str) -> pd.DataFrame:
        expr = LEVEL_EXPR.get(level, f"'{self.city_id}'")
        d = self._by_run(cur, "draws", expr)
        d["rate"] = np.where(d["uni"] > 0, d["cases"] / d["uni"].replace(0, np.nan), 0.0)
        k = self.n_draws
        sums = d.assign(cases2=d["cases"] ** 2, rate2=d["rate"] ** 2).groupby("area_id")[["cases", "rate", "cases2", "rate2"]].sum()
        out = pd.DataFrame({"estimate": sums["cases"] / k, "share": sums["rate"] / k})
        if k > 1:   # areas missing from a draw count as 0 in that draw
            out["var_place"] = ((sums["cases2"] / k - out["estimate"] ** 2) * k / (k - 1)).clip(lower=0)
            out["var_place_s"] = ((sums["rate2"] / k - out["share"] ** 2) * k / (k - 1)).clip(lower=0)
        else:
            out["var_place"] = out["var_place_s"] = 0.0
        base = d[d["run"] == 0].set_index("area_id")
        out["base"] = base["cases"].reindex(out.index).fillna(0)
        out["base_rate"] = base["rate"].reindex(out.index).fillna(0)
        return out

    def _survey(self, cur, level: str, base: pd.DataFrame) -> pd.DataFrame:
        """Replicate (survey) variance at block group and above."""
        if not self.n_reps:
            return pd.DataFrame({"var_survey": 0.0, "var_survey_s": 0.0}, index=base.index)
        expr = LEVEL_EXPR.get(level, f"'{self.city_id}'")
        r = self._by_run(cur, "reps", expr)
        r["rate"] = np.where(r["uni"] > 0, r["cases"] / r["uni"].replace(0, np.nan), 0.0)
        runs = sorted(r["run"].unique())
        cases = r.pivot_table(index="area_id", columns="run", values="cases", aggfunc="sum").reindex(columns=runs).fillna(0)
        rate = r.pivot_table(index="area_id", columns="run", values="rate", aggfunc="sum").reindex(columns=runs).fillna(0)
        cases, rate = cases.reindex(base.index).fillna(0), rate.reindex(base.index).fillna(0)
        factor = 4 / len(runs)
        return pd.DataFrame({
            "var_survey": factor * cases.sub(base["base"], axis=0).pow(2).sum(axis=1),
            "var_survey_s": factor * rate.sub(base["base_rate"], axis=0).pow(2).sum(axis=1),
        }, index=base.index)

    def compute(self, model: RiskModel, level: str) -> pd.DataFrame:
        if not self.available:
            raise ValueError("data points need cells_draws.parquet; run `synthpop publish`")
        key = (model.key, level)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        cur = self._cursor(model)
        out = self._level(cur, level)
        if level in ("parcel", "block"):
            bg = self._level(cur, "block_group")
            bg = bg.join(self._survey(cur, "block_group", bg))
            parent = self._parent_block_groups(cur, level, out.index)
            bg_est = bg["estimate"].reindex(parent).to_numpy()
            ratio = np.where(bg_est > 0, out["estimate"].to_numpy() / np.where(bg_est > 0, bg_est, 1), 0)
            out["var_survey"] = ratio ** 2 * bg["var_survey"].reindex(parent).fillna(0).to_numpy()
            out["var_survey_s"] = bg["var_survey_s"].reindex(parent).fillna(0).to_numpy()
        else:
            out = out.join(self._survey(cur, level, out))
        out["se"] = np.sqrt(out["var_place"] + out["var_survey"])
        out["share_se"] = np.sqrt(out["var_place_s"] + out["var_survey_s"])
        out["confidence"] = confidence(out["estimate"], out["se"])
        frame = out[["estimate", "se", "share", "share_se", "confidence"]].reset_index()
        with self._lock:
            if len(self._cache) > 200:
                self._cache.clear()
            self._cache[key] = frame
        return frame

    def _parent_block_groups(self, cur, level: str, ids: pd.Index) -> list[str]:
        if level == "block":
            return [i[:12] for i in ids]
        m = cur.execute("SELECT DISTINCT parcel_id, block_group FROM draws").fetchdf().set_index("parcel_id")["block_group"]
        return m.reindex(ids).tolist()

    def breakdown(self, model: RiskModel) -> dict[str, list[dict]]:
        """City-wide expected cases and rate by each factor's categories: how the
        distribution responds to the model, for the editor's charts."""
        cur = self._cursor(model)
        out = {}
        join = " AND ".join(f"t.{f} = probs.{f}" for f in self.factors)
        for f in self.factors:
            df = cur.execute(
                f"SELECT t.{f} AS category, SUM(n * p) / {self.n_draws} AS cases, SUM(n * in_universe) / {self.n_draws} AS uni "
                f"FROM draws t JOIN probs ON {join} GROUP BY t.{f}").fetchdf()
            order = {c: i for i, c in enumerate(self.catalog.variable(f).category_ids)}
            df = df[df["category"].isin(order)].assign(o=lambda x: x["category"].map(order)).sort_values("o")
            out[f] = [{"category": r.category, "cases": round(float(r.cases), 2),
                       "rate": round(float(r.cases / r.uni), 5) if r.uni > 0 else 0.0, "universe": round(float(r.uni), 1)}
                      for r in df.itertuples()]
        return out


class DataPointStore:
    """Saved data points, in a YAML file next to variables.yaml."""

    def __init__(self, path: str | Path, catalog: Catalog):
        self.path = Path(path)
        self.catalog = catalog
        self._lock = threading.Lock()
        raw = yaml.safe_load(self.path.read_text()) if self.path.exists() else None
        self.models: dict[str, RiskModel] = {}
        for item in (raw or {}).get("datapoints", []) or []:
            m = RiskModel.parse(item, catalog)
            self.models[m.id] = m

    def _write(self) -> None:
        header = ("# Saved data points (risk models) for the map. Edited by the app's Save button;\n"
                  "# safe to edit by hand. See synthpop/datapoints.py for the model.\n")
        body = yaml.safe_dump({"datapoints": [m.to_dict() for m in self.models.values()]}, sort_keys=False,
                              allow_unicode=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(header + body)
        tmp.replace(self.path)

    def save(self, model: RiskModel, taken: set[str]) -> RiskModel:
        if model.id in taken:
            raise ValueError(f"{model.id!r} is already a variable name")
        with self._lock:
            self.models[model.id] = model
            self._write()
        return model

    def delete(self, id: str) -> None:
        with self._lock:
            if id not in self.models:
                raise KeyError(f"no saved data point {id!r}")
            del self.models[id]
            self._write()
