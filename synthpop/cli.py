"""Command line entry point: `synthpop fetch`, later `synthpop build`."""

from __future__ import annotations

import argparse
import time

from .catalog import Catalog
from .config import ROOT, StudyArea
from .sources import NAMES, HttpFetcher, Sources


def _sources(args) -> Sources:
    return Sources(StudyArea.load(args.study_area), Catalog.load(args.variables), args.data, HttpFetcher())


def cmd_fetch(args) -> None:
    sources = _sources(args)
    for name in args.only or NAMES:
        t = time.time()
        df = sources.load(name)
        print(f"{name:16s} {len(df):>8,d} rows  {time.time() - t:6.1f}s")


def cmd_run(args) -> None:
    """Main run, validation run and 80 replicate runs, each saved under data/runs/<name>/."""
    from pathlib import Path

    from . import model

    sources = _sources(args)
    area, catalog = sources.area, sources.catalog
    inputs = model.prepare(sources.load_all(), catalog, area)
    holdout = model.holdout_blocks(inputs, area.holdout_fraction, area.random_seed)
    specs = [model.Run.main(), model.Run.validation(holdout)]
    specs += [model.Run.replicate(r) for r in range(1, (args.replicates or area.replicates) + 1)]
    out = Path(args.data) / "runs"
    for spec in specs:
        t = time.time()
        placed = model.run(inputs, spec)
        d = out / spec.name
        d.mkdir(parents=True, exist_ok=True)
        placed.persons.to_parquet(d / "persons.parquet", index=False)
        placed.households.to_parquet(d / "households.parquet", index=False)
        placed.fits.to_parquet(d / "fits.parquet", index=False)
        if spec.kind != "replicate":
            placed.capacity.to_parquet(d / "capacity.parquet", index=False)
        print(f"{spec.name:14s} {len(placed.households):>7,d} households {len(placed.persons):>7,d} persons"
              f"  {time.time() - t:5.1f}s")
    (out / "holdout_blocks.txt").write_text("\n".join(sorted(holdout)) + "\n")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="synthpop")
    p.add_argument("--data", default=str(ROOT / "data"))
    p.add_argument("--study-area", default=str(ROOT / "config" / "study_area.yaml"))
    p.add_argument("--variables", default=str(ROOT / "config" / "variables.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download, clip and cache every input")
    f.add_argument("--only", nargs="*", choices=NAMES)
    f.set_defaults(func=cmd_fetch)
    r = sub.add_parser("run", help="main, validation and replicate model runs")
    r.add_argument("--replicates", type=int, help="fewer replicate runs, for quick checks")
    r.set_defaults(func=cmd_run)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
