"""The variable schema (config/variables.yaml): what variables exist, how they are
recoded from PUMS, and which ACS cells constrain and validate them."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

KINDS = ("seed", "derived", "attached")
UNITS = ("person", "household")


@dataclass(frozen=True)
class Category:
    id: str
    label: str


@dataclass(frozen=True)
class Variable:
    id: str
    label: str
    kind: str
    unit: str
    categories: tuple[Category, ...]
    rules: tuple[tuple[str, str], ...] = ()      # (category, pandas query)
    bins: dict | None = None                     # {"column": ..., "edges": [[lo, hi], ...]}
    universe: str | None = None
    source: dict | None = None                   # attached variables only

    @property
    def category_ids(self) -> list[str]:
        return [c.id for c in self.categories]


@dataclass(frozen=True)
class Constraint:
    id: str
    unit: str
    by: tuple[str, ...]
    cells: dict[str, tuple[str, ...]]            # "male|a0_4" -> ("B01001_003", "-B25008_001")
    exact: bool = False                          # fit as a hard total instead of within its SE

    @property
    def tables(self) -> set[str]:
        return {c.lstrip("-").split("_")[0] for cells in self.cells.values() for c in cells}


@dataclass
class Catalog:
    levels: list[str]
    variables: list[Variable]
    constraints: list[Constraint]
    model_tables: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: str | Path) -> "Catalog":
        raw = yaml.safe_load(Path(path).read_text())
        variables = [_parse_variable(v) for v in raw["variables"]]
        ids = [v.id for v in variables]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate variable ids in {path}")
        known = set(ids)
        constraints = []
        for c in raw.get("constraints", []):
            missing = [b for b in c["by"] if b not in known]
            if missing:
                raise ValueError(f"constraint {c['id']} uses unknown variables {missing}")
            constraints.append(Constraint(
                id=c["id"], unit=c["unit"], by=tuple(c["by"]),
                cells={k: tuple(v) for k, v in c["cells"].items()}, exact=bool(c.get("exact", False)),
            ))
        return cls(levels=raw["levels"], variables=variables, constraints=constraints,
                   model_tables=list(raw.get("model_tables", [])))

    # ---------------------------------------------------------------- lookups

    def variable(self, id: str) -> Variable:
        for v in self.variables:
            if v.id == id:
                return v
        raise KeyError(id)

    @property
    def active(self) -> list[Variable]:
        """Variables the build computes (attached ones wait for an attach step)."""
        return [v for v in self.variables if v.kind != "attached"]

    @property
    def acs_tables(self) -> list[str]:
        tables = set(self.model_tables)
        for c in self.constraints:
            tables |= c.tables
        return sorted(tables)

    def schema(self) -> dict:
        """The JSON shape the API serves at /schema."""
        return {
            "levels": self.levels,
            "variables": [
                {"id": v.id, "label": v.label, "kind": v.kind, "unit": v.unit,
                 "universe": v.universe, "available": v.kind != "attached",
                 "categories": [{"id": c.id, "label": c.label} for c in v.categories]}
                for v in self.variables
            ],
        }

    # ---------------------------------------------------------------- recoding

    def recode(self, df: pd.DataFrame, variable_id: str) -> pd.Series:
        """One variable's categories for `df`, which must hold the columns its rules read."""
        return _recode(df, self.variable(variable_id))

    def apply(self, persons: pd.DataFrame, households: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Add one category column per active variable.

        `persons` and `households` are PUMS-shaped and share SERIALNO. Person rules
        can read household columns. Returns copies; inputs are not modified.
        """
        hh = households.copy()
        for v in self.active:
            if v.kind == "seed" and v.unit == "household":
                hh[v.id] = _recode(hh, v)

        hh_cols = [c for c in hh.columns if c not in persons.columns or c == "SERIALNO"]
        pp = persons.merge(hh[hh_cols], on="SERIALNO", how="left", validate="many_to_one")
        for v in self.active:
            if v.kind == "seed" and v.unit == "person":
                pp[v.id] = _recode(pp, v)
        for v in self.active:
            if v.kind == "derived":
                pp[v.id] = _recode(pp, v)
        return pp, hh


def _parse_variable(v: dict) -> Variable:
    if v["kind"] not in KINDS:
        raise ValueError(f"{v['id']}: kind must be one of {KINDS}")
    if v["unit"] not in UNITS:
        raise ValueError(f"{v['id']}: unit must be one of {UNITS}")
    cats = tuple(Category(str(c["id"]), c["label"]) for c in v["categories"])
    rules = tuple((str(r["category"]), r["when"]) for r in v.get("rules", []))
    if v["kind"] != "attached" and not rules and "bins" not in v:
        raise ValueError(f"{v['id']}: needs rules or bins")
    if v.get("bins") and len(v["bins"]["edges"]) != len(cats):
        raise ValueError(f"{v['id']}: bins and categories differ in length")
    unknown = {r[0] for r in rules} - {c.id for c in cats}
    if unknown:
        raise ValueError(f"{v['id']}: rules use unknown categories {unknown}")
    return Variable(id=v["id"], label=v["label"], kind=v["kind"], unit=v["unit"], categories=cats,
                    rules=rules, bins=v.get("bins"), universe=v.get("universe"), source=v.get("source"))


def _recode(df: pd.DataFrame, v: Variable) -> pd.Series:
    if v.bins:
        x = pd.to_numeric(df[v.bins["column"]], errors="coerce")
        conds = [(x >= lo) & (x <= hi) for lo, hi in v.bins["edges"]]
        out = np.select(conds, v.category_ids, default=None)
    else:
        conds = [df.eval(when, engine="python").fillna(False).to_numpy(dtype=bool) for _, when in v.rules]
        out = np.select(conds, [c for c, _ in v.rules], default=None)
    if v.universe:
        out = np.where(df.eval(v.universe, engine="python").fillna(False).to_numpy(dtype=bool), out, None)
    return pd.Series(pd.Categorical(out, categories=v.category_ids), index=df.index)
