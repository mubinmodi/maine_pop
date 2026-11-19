"""Data points: health or other measures attached to synthetic people by a risk model,
computed live and optionally saved.

    model = RiskModel.parse({...})
    engine = DataPointEngine(out_dir, catalog, city_id)
    frame = engine.compute(model, "block_group")   # area_id, estimate, se, share, share_se, confidence
    store = DataPointStore("config/datapoints.yaml", catalog)   # or "s3://bucket/key"

A risk model gives every synthetic person a probability:

    logit p = logit(baseline) + Σ log(odds ratio of the person's category, per factor) [+ δ_tract]

A model with `calibrate: places:<MEASURE>` gets one shift δ per tract, chosen so the
tract's expected cases equal its CDC PLACES prevalence times its adults (18+). The odds
ratios then only spread risk within a tract, and the tract's total follows PLACES.
PLACES' own uncertainty (its 95% interval) is added to the margins of such a model.

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
CALIBRATE_PATTERN = re.compile(r"^places:[A-Z0-9_]{2,40}$")
# PLACES rates are for adults 18+. Ages are in 5-year groups, so 18 and 19 are taken as
# two fifths of the 15–19 group.
ADULT_WEIGHT = {"a15_19": 0.4, **{a: 1.0 for a in ("a20_24", "a25_29", "a30_34", "a35_39", "a40_44", "a45_49",
                "a50_54", "a55_59", "a60_64", "a65_69", "a70_74", "a75_79", "a80_84", "a85_plus")}}


@dataclass
class RiskModel:
    id: str
    label: str
    baseline: float                                  # prevalence for the reference person, 0–1
    odds_ratios: dict[str, dict[str, float]] = field(default_factory=dict)
    universe: dict[str, list[str]] = field(default_factory=dict)   # factor -> categories counted
    description: str = ""
    source: str = ""
    calibrate: str = ""                              # "places:DIABETES": match CDC PLACES by tract

    @classmethod
    def parse(cls, raw: dict, catalog: Catalog) -> "RiskModel":
        factors = {f: set(catalog.variable(f).category_ids) for f in risk_factors(catalog)}
        m = cls(id=str(raw["id"]), label=str(raw.get("label") or raw["id"]), baseline=float(raw["baseline"]),
                odds_ratios={f: {c: float(v) for c, v in cats.items()} for f, cats in (raw.get("odds_ratios") or {}).items()},
                universe={f: [str(c) for c in cats] for f, cats in (raw.get("universe") or {}).items()},
                description=str(raw.get("description", "")), source=str(raw.get("source", "")),
                calibrate=str(raw.get("calibrate") or ""))
        if m.calibrate and not CALIBRATE_PATTERN.match(m.calibrate):
            raise ValueError("calibrate must be empty or places:<MEASURE>, e.g. places:DIABETES")
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

    def logits(self, cells: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Each cell's log-odds (before any tract shift) and whether it is in the universe."""
        logit = np.full(len(cells), math.log(self.baseline / (1 - self.baseline)))
        for f, cats in self.odds_ratios.items():
            logit += cells[f].map({c: math.log(v) for c, v in cats.items()}).fillna(0.0).to_numpy()
        inside = np.ones(len(cells), dtype=bool)
        for f, cats in self.universe.items():
            inside &= cells[f].isin(cats).to_numpy()
        return logit, inside

    def probabilities(self, cells: pd.DataFrame) -> pd.DataFrame:
        """`cells` has one column per factor; returns it with p and in_universe (0/1),
        without any tract shift."""
        logit, inside = self.logits(cells)
        return cells.assign(p=np.where(inside, _expit(logit), 0.0), in_universe=inside.astype(float))


def _expit(x):
    return 1 / (1 + np.exp(-np.clip(x, -40, 40)))


def _solve_shift(N: np.ndarray, logit: np.ndarray, target: np.ndarray, bound: float = 60.0) -> np.ndarray:
    """For each row t of N (people by cell), the δ with Σ_c N_tc·expit(logit_c + δ) = target_t.

    The sum rises with δ, so each root is bracketed in [−bound, bound] and the bracket
    shrinks every step. A Newton step is taken when it lands inside the bracket, else
    the midpoint: plain Newton from 0 can jump to where the slope is ~0 and stall there.
    """
    lo, hi = np.full(len(target), -bound), np.full(len(target), bound)
    delta = np.zeros(len(target))
    tol = 1e-9 * np.maximum(target, 1.0)
    for _ in range(200):
        q = _expit(logit[None, :] + delta[:, None])
        f = (N * q).sum(1) - target
        g = (N * q * (1 - q)).sum(1)
        done = np.abs(f) <= tol
        if done.all():
            break
        hi = np.where(f > 0, delta, hi)
        lo = np.where(f < 0, delta, lo)
        newton = delta - np.divide(f, g, out=np.full_like(f, np.inf), where=g > 0)
        inside = (newton > lo) & (newton < hi)
        delta = np.where(done, delta, np.where(inside, newton, (lo + hi) / 2))
    return delta


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


# Diagnosed diabetes. Published prevalences turned into odds ratios against a reference
# category; the tract shift (calibrate) then sets each tract's level from CDC PLACES.
# - Age and sex: NCHS Data Brief 516 (NHANES Aug 2021–Aug 2023), diagnosed diabetes in
#   adults 20+: 2.2% at 20–39, 12.1% at 40–59, 20.5% at 60+; men 12.9%, women 9.7%.
# - Race/ethnicity and education: National Diabetes Statistics Report, Appendix Table 3
#   (NHIS 2019–2021, age-adjusted): White 6.9%, Black 12.1%, Asian 9.1%, Hispanic 11.7%;
#   less than high school 13.1%, high school 9.1%, more than high school 6.9%.
# Marginal rates used as if they were adjusted for each other overstate the combined
# gradient somewhat; income is left out because education already carries most of it.
def _diabetes() -> dict:
    odds = lambda p: p / (1 - p)
    ratio = lambda p, ref: round(odds(p) / odds(ref), 3)
    ages = {"a40_44": 0.121, "a45_49": 0.121, "a50_54": 0.121, "a55_59": 0.121,
            **{a: 0.205 for a in ("a60_64", "a65_69", "a70_74", "a75_79", "a80_84", "a85_plus")}}
    return {
        "id": "diabetes_places", "label": "Diagnosed diabetes (CDC PLACES by tract)",
        "baseline": 0.022,
        "odds_ratios": {
            "age_group": {a: ratio(p, 0.022) for a, p in ages.items()},
            "sex": {"female": ratio(0.097, 0.129)},
            "race_eth": {"black": ratio(0.121, 0.069), "asian": ratio(0.091, 0.069), "hispanic": ratio(0.117, 0.069)},
            "education": {"lt_hs": ratio(0.131, 0.069), "hs": ratio(0.091, 0.069)},
        },
        "universe": {"age_group": list(ADULT_WEIGHT)[1:]},
        "description": "Expected adults 20+ with diagnosed diabetes. Each tract's total matches CDC PLACES "
                       "(2025 release, BRFSS 2023); age, sex, race/ethnicity and education spread it within the tract. "
                       "Reference person: White man aged 20–39 with more than a high school education.",
        "source": "CDC PLACES 2025 (tract crude prevalence); NCHS Data Brief 516 (2024), Figures 1–2; "
                  "CDC National Diabetes Statistics Report, Appendix Table 3 (2019–2021).",
        "calibrate": "places:DIABETES",
    }


PRESETS = {"alzheimers_dhana": _dhana(), "diabetes_places": _diabetes()}


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

        # Each area's tract, and people by (tract, cell), for tract-calibrated models.
        tracts = self._levels["tract"]["areas"]
        for level, lv in self._levels.items():
            if level == "parcel":
                lv["tract"] = np.where(lv["parent"] >= 0, tracts.get_indexer(bg[np.maximum(lv["parent"], 0)].str[:11]), -1)
            elif level != "city":
                lv["tract"] = tracts.get_indexer(lv["areas"].str[:11])
        t = self._levels["tract"]["draws"]
        n_t = t["shape"][1]
        self._tract_cells = np.bincount((t["row"] % n_t) * len(uniq) + t["cell"], weights=t["n"],
                                        minlength=n_t * len(uniq)).reshape(n_t, len(uniq)) / self.n_draws
        adult = self.cells["age_group"].map(ADULT_WEIGHT).fillna(0.0).to_numpy() if "age_group" in self.cells else np.ones(len(uniq))
        self._tract_adults = self._tract_cells @ adult
        self.places: dict[str, pd.DataFrame] = {}
        if (out / "places.parquet").exists():
            pl = pd.read_parquet(out / "places.parquet")
            pl = pl[pl["level"] == "tract"]
            for m, g in pl.groupby("measure"):
                g = g.set_index("area_id")
                self.places[m] = pd.DataFrame({"prev": g["estimate"] / 100,
                                               "se": (g["high"] - g["low"]) / (2 * 1.96) / 100}).reindex(tracts)
        self._offsets: dict[str, np.ndarray] = {}

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

    def offsets(self, model: RiskModel) -> np.ndarray | None:
        """The model's log-odds shift for each tract (in tract-area order), or None when it
        is not calibrated. δ makes a tract's expected cases equal PLACES prevalence × adults."""
        if not model.calibrate:
            return None
        with self._lock:
            if model.key in self._offsets:
                return self._offsets[model.key]
        measure = model.calibrate.split(":", 1)[1]
        if measure not in self.places:
            raise ValueError(f"no CDC PLACES rates for {measure}; add it to PLACES_MEASURES and run "
                             "`synthpop fetch --only places` then `synthpop publish`")
        logit, inside = model.logits(self.cells)
        N = self._tract_cells * inside[None, :]
        adults = self._tract_adults
        if (N.sum(1) < 0.9 * adults).any():
            raise ValueError("a PLACES rate is for adults: the data point's ages must include every adult (20+)")
        prev = self.places[measure]["prev"].to_numpy()
        # A tract without a PLACES rate takes the rate of all the tracts that have one.
        known = ~np.isnan(prev)
        prev = np.where(known, prev, (prev[known] * adults[known]).sum() / max(adults[known].sum(), 1e-9))
        target = prev * adults
        delta = _solve_shift(N, logit, target)
        with self._lock:
            self._offsets[model.key] = delta
        return delta

    def _places_se(self, model: RiskModel) -> np.ndarray:
        """PLACES standard error of each tract's expected cases."""
        se = self.places[model.calibrate.split(":", 1)[1]]["se"].fillna(0.0).to_numpy()
        return se * self._tract_adults

    @staticmethod
    def _per_run(c: dict, p: np.ndarray, inside: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Expected cases and people in the universe for each run and area; `p` and
        `inside` are per entry of `c`."""
        size = c["shape"][0] * c["shape"][1]
        cases = np.bincount(c["row"], weights=c["n"] * p, minlength=size).reshape(c["shape"])
        uni = np.bincount(c["row"], weights=c["n"] * inside, minlength=size).reshape(c["shape"])
        return cases, uni

    def _entries(self, level: str, c: dict, logit, inside, delta) -> tuple[np.ndarray, np.ndarray]:
        x = logit[c["cell"]]
        if delta is not None:
            tract = self._levels[level]["tract"][c["row"] % c["shape"][1]]
            x = x + np.where(tract >= 0, delta[np.maximum(tract, 0)], 0.0)
        return np.where(inside[c["cell"]], _expit(x), 0.0), inside[c["cell"]].astype(float)

    def _runs(self, level: str, kind: str, logit, inside, delta) -> tuple[np.ndarray, np.ndarray] | None:
        """Cases and universe by (run, area). A calibrated city is the sum of its tracts."""
        if delta is not None and level == "city":
            r = self._runs("tract", kind, logit, inside, delta)
            return None if r is None else (r[0].sum(1, keepdims=True), r[1].sum(1, keepdims=True))
        lv = self._levels[level]
        if kind not in lv:
            return None
        return self._per_run(lv[kind], *self._entries(level, lv[kind], logit, inside, delta))

    def _level(self, level: str, logit, inside, delta=None) -> pd.DataFrame:
        lv = self._levels[level]
        rate = lambda cases, uni: np.divide(cases, uni, out=np.zeros_like(cases), where=uni > 0)
        cases, uni = self._runs(level, "draws", logit, inside, delta)
        share = rate(cases, uni)
        k = self.n_draws   # areas missing from a draw count as 0 in that draw
        out = pd.DataFrame({
            "estimate": cases.mean(0), "share": share.mean(0),
            "var_place": cases.var(0, ddof=1) if k > 1 else 0.0,
            "var_place_s": share.var(0, ddof=1) if k > 1 else 0.0,
        }, index=lv["areas"])
        reps = self._runs(level, "reps", logit, inside, delta) if self.n_reps else None
        if reps is not None:    # replicate (survey) variance at block group and above
            r_share = rate(*reps)
            factor = 4 / self.n_reps
            out["var_survey"] = factor * ((reps[0] - cases[self._main]) ** 2).sum(0)
            out["var_survey_s"] = factor * ((r_share - share[self._main]) ** 2).sum(0)
        else:
            out["var_survey"] = out["var_survey_s"] = 0.0
        return out

    def _places_var(self, model: RiskModel, level: str, out: pd.DataFrame, logit, inside, delta) -> tuple[np.ndarray, np.ndarray]:
        """PLACES variance of each area's cases and rate. Within a tract the error is shared
        in proportion to cases; tracts are taken as independent."""
        tr = self._level("tract", logit, inside, delta)
        v_t = self._places_se(model) ** 2
        uni_t = self._tract_cells[:, inside].sum(1)
        if level == "city":
            uni = uni_t.sum()
            return np.array([v_t.sum()]), np.array([v_t.sum() / max(uni, 1e-9) ** 2])
        t = self._levels[level]["tract"]
        ok = t >= 0
        ti = np.maximum(t, 0)
        ratio = np.divide(out["estimate"].to_numpy(), tr["estimate"].to_numpy()[ti], out=np.zeros(len(out)),
                          where=ok & (tr["estimate"].to_numpy()[ti] > 0))
        v_share = np.divide(v_t, np.maximum(uni_t, 1e-9) ** 2)
        return np.where(ok, ratio ** 2 * v_t[ti], 0.0), np.where(ok, v_share[ti], 0.0)

    def compute(self, model: RiskModel, level: str) -> pd.DataFrame:
        if not self.available:
            raise ValueError("data points need cells_draws.parquet; run `synthpop publish`")
        key = (model.key, level)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        logit, inside = model.logits(self.cells)
        delta = self.offsets(model)
        out = self._level(level, logit, inside, delta)
        if level in ("parcel", "block"):
            # Below block group, attribute the block group's survey variance by share of its cases.
            bg = self._level("block_group", logit, inside, delta)
            parent = self._levels[level]["parent"]
            ok = parent >= 0
            take = lambda col: np.where(ok, bg[col].to_numpy()[np.where(ok, parent, 0)], 0.0)
            bg_est = take("estimate")
            ratio = np.divide(out["estimate"].to_numpy(), bg_est, out=np.zeros(len(out)), where=bg_est > 0)
            out["var_survey"] = ratio ** 2 * take("var_survey")
            out["var_survey_s"] = take("var_survey_s")
        if delta is not None:
            v, v_s = self._places_var(model, level, out, logit, inside, delta)
            out["var_survey"] = out["var_survey"] + v
            out["var_survey_s"] = out["var_survey_s"] + v_s
        out["se"] = np.sqrt(out["var_place"] + out["var_survey"])
        out["share_se"] = np.sqrt(out["var_place_s"] + out["var_survey_s"])
        out["confidence"] = confidence(out["estimate"], out["se"])
        frame = out[["estimate", "se", "share", "share_se", "confidence"]].reset_index()
        with self._lock:
            if len(self._cache) > 200:
                self._cache.clear()
            self._cache[key] = frame
        return frame

    def offsets_by_tract(self, model: RiskModel) -> dict[str, float] | None:
        """The tract shifts keyed by tract id, for the people layer."""
        delta = self.offsets(model)
        if delta is None:
            return None
        return {t: round(float(d), 4) for t, d in zip(self._levels["tract"]["areas"], delta)}

    def breakdown(self, model: RiskModel) -> dict[str, list[dict]]:
        """City-wide expected cases and rate by each factor's categories: how the
        distribution responds to the model, for the editor's charts."""
        logit, inside = model.logits(self.cells)
        delta = self.offsets(model)
        if delta is None:
            people = self._cell_people
            cases_by_cell = people * np.where(inside, _expit(logit), 0.0)
        else:
            people = self._tract_cells.sum(0)
            cases_by_cell = (self._tract_cells * _expit(logit[None, :] + delta[:, None])).sum(0) * inside
        uni_by_cell = people * inside
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
