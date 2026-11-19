"""Study-area settings (config/study_area.yaml)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class StudyArea:
    name: str
    state_fips: str
    county_fips: str
    place_fips: str
    place_name: str
    puma: str
    e911_town: str
    acs_year: int
    pums_year: int
    tiger_year: int
    blocks_year: int
    places_year: int
    replicates: int
    holdout_fraction: float
    placement_draws: int
    random_seed: int
    fit_tracts: bool = True

    @property
    def county_geoid(self) -> str:
        return self.state_fips + self.county_fips

    @property
    def place_geoid(self) -> str:
        return self.state_fips + self.place_fips

    @classmethod
    def load(cls, path: str | Path = ROOT / "config" / "study_area.yaml") -> "StudyArea":
        raw = yaml.safe_load(Path(path).read_text())
        v, m = raw["vintages"], raw["model"]
        return cls(
            name=raw["name"], state_fips=str(raw["state_fips"]), county_fips=str(raw["county_fips"]),
            place_fips=str(raw["place_fips"]), place_name=raw["place_name"], puma=str(raw["puma"]),
            e911_town=raw["e911_town"], acs_year=v["acs"], pums_year=v["pums"], tiger_year=v["tiger"],
            blocks_year=v["blocks"], places_year=v.get("places", 2025), replicates=m["replicates"], holdout_fraction=m["holdout_fraction"], placement_draws=m.get("placement_draws", 20),
            random_seed=m["random_seed"], fit_tracts=bool(m.get("fit_tracts", True)),
        )
