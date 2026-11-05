"""Data points: health or other measures attached to synthetic people by a risk model,
computed live and optionally saved.

    model = RiskModel.parse({...})
    engine = DataPointEngine(out_dir, catalog, city_id)
    frame = engine.compute(model, "block_group")   # area_id, estimate, se, share, share_se, confidence
    store = DataPointStore("config/datapoints.yaml", catalog)   # or "s3://bucket/key"

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

import numpy as np
import pandas as pd
import yaml

from .catalog import Catalog
from .publish import risk_factors
from .store import Document
from .summarize import confidence

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


def _prefix(s: pd.Series, n: int) -> pd.Series:
    """First n characters of each id, computed once per distinct id."""
    s = s.astype("category")
    return pd.Series(np.asarray(s.cat.categories.str[:n], dtype=object)[s.cat.codes], index=s.index)


def _factorize(s: pd.Series) -> tuple[np.ndarray, pd.Index]:
    """Codes for each row and the distinct values, sorted as text (a categorical's own order is arbitrary)."""
    s = s.astype("category").cat.remove_unused_categories()
    cats = np.asarray(s.cat.categories, dtype=object)
    order = np.argsort(cats)
    rank = np.empty(len(cats), dtype=np.int64)
    rank[order] = np.arange(len(cats))
    return rank[s.cat.codes.to_numpy()], pd.Index(cats[order], name="area_id")


class DataPointEngine:
    """Computes a risk model's expected cases for every area at a level, with margins.

    Every synthetic person falls in one cell: a combination of the risk factors (a few
    hundred occur). A model only sets each cell's probability, so at start-up the engine
    counts people by (run, area, cell) for every level, and a model's expected cases are
    those counts times the cell probabilities: one weighted sum, a few milliseconds.
    """

    def __init__(self, out_dir: str | Path, catalog: Catalog, city_id: str):
        out = Path(out_dir)
        self.catalog = catalog
        self.city_id = city_id
        self.factors = risk_factors(catalog)
        self.available = (out / "cells_draws.parquet").exists()
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}
        self._lock = threading.Lock()
        if not self.available:
            return
        # Text columns load as categoricals: a million rows of strings would briefly take ~1 GB.
        text = ["parcel_id", "block", "block_group", *self.factors]
        draws = pd.read_parquet(out / "cells_draws.parquet", read_dictionary=text)
        reps_path = out / "cells_reps.parquet"
        reps = (pd.read_parquet(reps_path, read_dictionary=text[2:]) if reps_path.exists()
                else draws.iloc[:0][["block_group", *self.factors, "n", "run"]])

        # Cells, including a missing category (education is blank under 25).
        codes_d, codes_r = self._cell_codes(draws), self._cell_codes(reps)
        uniq = np.unique(np.concatenate([codes_d, codes_r]))
        self.cells = self._decode(uniq)
        cell_d, cell_r = np.searchsorted(uniq, codes_d), np.searchsorted(uniq, codes_r)

        runs_d = np.sort(draws["run"].unique())
        self.n_draws, self.n_reps = len(runs_d), reps["run"].nunique()
        if 0 not in runs_d:
            raise ValueError("cells_draws.parquet has no main run (run 0)")
        self._main = int(np.searchsorted(runs_d, 0))
        run_d = np.searchsorted(runs_d, draws["run"].to_numpy())
        run_r = pd.factorize(reps["run"], sort=True)[0]
        n_d, n_r = draws["n"].to_numpy(float), reps["n"].to_numpy(float)

        ids = {"parcel": draws["parcel_id"], "block": draws["block"], "block_group": draws["block_group"],
               "tract": _prefix(draws["block_group"], 11), "city": pd.Series(city_id, index=draws.index)}
        rep_ids = {"block_group": reps["block_group"], "tract": _prefix(reps["block_group"], 11),
                   "city": pd.Series(city_id, index=reps.index)}
        self._levels: dict[str, dict] = {}
        for level, s in ids.items():
            area, areas = _factorize(s)
            lv = {"areas": pd.Index(areas, name="area_id"),
                  "draws": self._counts(run_d, area, cell_d, n_d, self.n_draws, len(areas))}
            if level in rep_ids and self.n_reps:
                r_area = areas.get_indexer(rep_ids[level])         # replicate areas outside the draws are dropped
                keep = r_area >= 0
                lv["reps"] = self._counts(run_r[keep], r_area[keep], cell_r[keep], n_r[keep], self.n_reps, len(areas))
            self._levels[level] = lv
        bg = self._levels["block_group"]["areas"]
        self._levels["block"]["parent"] = bg.get_indexer(self._levels["block"]["areas"].str[:12])
        parcel_bg = draws.drop_duplicates("parcel_id").set_index("parcel_id")["block_group"]
        self._levels["parcel"]["parent"] = bg.get_indexer(parcel_bg.reindex(self._levels["parcel"]["areas"]))
        self._cell_people = np.bincount(cell_d, weights=n_d, minlength=len(uniq)) / self.n_draws

    def _cell_codes(self, df: pd.DataFrame) -> np.ndarray:
        code = np.zeros(len(df), dtype=np.int64)
        for f in self.factors:
            cats = self.catalog.variable(f).category_ids
            c = pd.Categorical(df[f], categories=cats).codes.astype(np.int64)
            code = code * (len(cats) + 1) + np.where(c < 0, len(cats), c)
        return code

    def _decode(self, codes: np.ndarray) -> pd.DataFrame:
        cols, rest = {}, codes.copy()
        for f in reversed(self.factors):
            cats = self.catalog.variable(f).category_ids
            rest, c = np.divmod(rest, len(cats) + 1)
            cols[f] = np.array([*cats, None], dtype=object)[c]
        return pd.DataFrame({f: cols[f] for f in self.factors})

    @staticmethod
    def _counts(run, area, cell, n, n_runs, n_areas) -> dict:
        """People by (run × area, cell), with duplicate rows summed."""
        key = (run.astype(np.int64) * n_areas + area) * (cell.max(initial=0) + 1) + cell
        uniq, inv = np.unique(key, return_inverse=True)
        n_cells = cell.max(initial=0) + 1
        return {"row": uniq // n_cells, "cell": uniq % n_cells, "n": np.bincount(inv, weights=n),
                "shape": (n_runs, n_areas)}

    @staticmethod
    def _per_run(c: dict, p: np.ndarray, inside: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Expected cases, people in the universe, and the rate, for each run and area."""
        size = c["shape"][0] * c["shape"][1]
        cases = np.bincount(c["row"], weights=c["n"] * p[c["cell"]], minlength=size).reshape(c["shape"])
        uni = np.bincount(c["row"], weights=c["n"] * inside[c["cell"]], minlength=size).reshape(c["shape"])
        rate = np.divide(cases, uni, out=np.zeros_like(cases), where=uni > 0)
        return cases, rate

    def _level(self, level: str, p: np.ndarray, inside: np.ndarray) -> pd.DataFrame:
        lv = self._levels[level]
        cases, rate = self._per_run(lv["draws"], p, inside)
        k = self.n_draws   # areas missing from a draw count as 0 in that draw
        out = pd.DataFrame({
            "estimate": cases.mean(0), "share": rate.mean(0),
            "var_place": cases.var(0, ddof=1) if k > 1 else 0.0,
            "var_place_s": rate.var(0, ddof=1) if k > 1 else 0.0,
        }, index=lv["areas"])
        if "reps" in lv:    # replicate (survey) variance at block group and above
            r_cases, r_rate = self._per_run(lv["reps"], p, inside)
            factor = 4 / self.n_reps
            out["var_survey"] = factor * ((r_cases - cases[self._main]) ** 2).sum(0)
            out["var_survey_s"] = factor * ((r_rate - rate[self._main]) ** 2).sum(0)
        else:
            out["var_survey"] = out["var_survey_s"] = 0.0
        return out

    def compute(self, model: RiskModel, level: str) -> pd.DataFrame:
        if not self.available:
            raise ValueError("data points need cells_draws.parquet; run `synthpop publish`")
        key = (model.key, level)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        probs = model.probabilities(self.cells)
        p, inside = probs["p"].to_numpy(), probs["in_universe"].to_numpy()
        out = self._level(level, p, inside)
        if level in ("parcel", "block"):
            # Below block group, attribute the block group's survey variance by share of its cases.
            bg = self._level("block_group", p, inside)
            parent = self._levels[level]["parent"]
            ok = parent >= 0
            take = lambda col: np.where(ok, bg[col].to_numpy()[np.where(ok, parent, 0)], 0.0)
            bg_est = take("estimate")
            ratio = np.divide(out["estimate"].to_numpy(), bg_est, out=np.zeros(len(out)), where=bg_est > 0)
            out["var_survey"] = ratio ** 2 * take("var_survey")
            out["var_survey_s"] = take("var_survey_s")
        out["se"] = np.sqrt(out["var_place"] + out["var_survey"])
        out["share_se"] = np.sqrt(out["var_place_s"] + out["var_survey_s"])
        out["confidence"] = confidence(out["estimate"], out["se"])
        frame = out[["estimate", "se", "share", "share_se", "confidence"]].reset_index()
        with self._lock:
            if len(self._cache) > 200:
                self._cache.clear()
            self._cache[key] = frame
        return frame

    def breakdown(self, model: RiskModel) -> dict[str, list[dict]]:
        """City-wide expected cases and rate by each factor's categories: how the
        distribution responds to the model, for the editor's charts."""
        probs = model.probabilities(self.cells)
        people = self._cell_people
        cases_by_cell, uni_by_cell = people * probs["p"].to_numpy(), people * probs["in_universe"].to_numpy()
        out = {}
        for f in self.factors:
            cats = self.catalog.variable(f).category_ids
            code = pd.Categorical(self.cells[f], categories=cats).codes
            ok = code >= 0
            count = lambda w: np.bincount(code[ok], weights=w[ok], minlength=len(cats))
            n, cases, uni = count(people), count(cases_by_cell), count(uni_by_cell)
            out[f] = [{"category": c, "cases": round(float(cases[i]), 2),
                       "rate": round(float(cases[i] / uni[i]), 5) if uni[i] > 0 else 0.0, "universe": round(float(uni[i]), 1)}
                      for i, c in enumerate(cats) if n[i] > 0]
        return out


class DataPointStore:
    """Saved data points, in one YAML file: a local path (config/datapoints.yaml by default)
    or an S3 object, `s3://bucket/key`, so a container keeps them across deploys."""

    HEADER = ("# Saved data points (risk models) for the map. Edited by the app's Save button;\n"
              "# safe to edit by hand. See synthpop/datapoints.py for the model.\n")

    def __init__(self, path: str | Path, catalog: Catalog, s3=None):
        self.doc = Document(path, s3)
        self.catalog = catalog
        self._lock = threading.Lock()
        self.models = self._read()

    def _read(self) -> dict[str, RiskModel]:
        text = self.doc.read()
        raw = yaml.safe_load(text) if text else None
        models = {}
        for item in (raw or {}).get("datapoints", []) or []:
            m = RiskModel.parse(item, self.catalog)
            models[m.id] = m
        return models

    def _write(self) -> None:
        body = yaml.safe_dump({"datapoints": [m.to_dict() for m in self.models.values()]}, sort_keys=False,
                              allow_unicode=True)
        self.doc.write(self.HEADER + body)

    # Each change re-reads the file first, so a save from another server copy isn't overwritten.
    def save(self, model: RiskModel, taken: set[str]) -> RiskModel:
        if model.id in taken:
            raise ValueError(f"{model.id!r} is already a variable name")
        with self._lock:
            self.models = self._read()
            self.models[model.id] = model
            self._write()
        return model

    def delete(self, id: str) -> None:
        with self._lock:
            self.models = self._read()
            if id not in self.models:
                raise KeyError(f"no saved data point {id!r}")
            del self.models[id]
            self._write()
