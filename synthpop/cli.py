"""Command line entry point: `synthpop fetch`, later `synthpop build`."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

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
    specs += [model.Run.draw(d) for d in range(1, (args.draws if args.draws is not None else area.placement_draws) + 1)]
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


def cmd_persons(args) -> None:
    """One row per synthetic person in one run, with an E911 address point each:
    data/out/persons_<run>.csv by default."""
    import numpy as np
    import pandas as pd

    from .persons import locate, person_table

    sources = _sources(args)
    run_dir = Path(args.data) / "runs" / args.run
    persons = pd.read_parquet(run_dir / "persons.parquet")
    table = person_table(persons, sources.load("pums_persons"))
    # Its own random stream (model runs use [seed, 0, d] and [seed, 1, r]).
    rng = np.random.default_rng([sources.area.random_seed, 2])
    table = table.merge(locate(persons, sources.load("e911"), sources.load("parcels"), rng), on="person_id")
    out = Path(args.out) if args.out else Path(args.data) / "out" / f"persons_{args.run}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out, index=False)
    on_address = (table["location"] == "address").mean()
    print(f"{out}: {len(table):,} persons in {table['household_id'].nunique():,} households "
          f"and {table['family_id'].nunique():,} families, "
          f"{on_address:.1%} on an E911 address point")


def cmd_summarize(args) -> None:
    """Area stats from the saved runs: data/out/area_stats.parquet (main run plus
    published ACS rows) and data/out/validation_stats.parquet."""
    from pathlib import Path

    import pandas as pd

    from . import summarize as S

    sources = _sources(args)
    area, catalog = sources.area, sources.catalog
    runs, out = Path(args.data) / "runs", Path(args.data) / "out"
    out.mkdir(parents=True, exist_ok=True)
    reps = sorted(runs.glob("replicate_*"))
    draws = [runs / "main"] + sorted(runs.glob("draw_*"))
    t = time.time()
    stats = S.summarize([S.load_run(d) for d in draws], (S.load_run(d) for d in reps), catalog, area.place_geoid)
    pub = S.published(sources.load("acs"), catalog)
    pd.concat([stats, pub], ignore_index=True).to_parquet(out / "area_stats.parquet", index=False)
    val = S.summarize([S.load_run(runs / "validation")], [], catalog, area.place_geoid)
    val.to_parquet(out / "validation_stats.parquet", index=False)
    print(f"area_stats: {len(stats):,} synthetic + {len(pub):,} published rows from {len(reps)} replicates "
          f"and {len(draws)} placement draws ({time.time() - t:.1f}s)")


def cmd_validate(args) -> None:
    from pathlib import Path

    import pandas as pd

    from .validate import validate

    sources = _sources(args)
    runs, out = Path(args.data) / "runs", Path(args.data) / "out"
    stats = pd.read_parquet(out / "area_stats.parquet")
    pub = stats[stats["source"] == "published"]
    hh = pd.read_parquet(runs / "main" / "households.parquet")
    cap = pd.read_parquet(runs / "main" / "capacity.parquet")
    fits = pd.read_parquet(runs / "main" / "fits.parquet")
    persons = pd.read_parquet(runs / "main" / "persons.parquet", columns=["household_id"])
    city = pub[(pub["level"] == "city")].set_index(["variable", "category"])["estimate"]
    match = hh["match"].value_counts(normalize=True)
    diagnostics = {
        "converged": fits["converged"].mean(), "iterations": fits["iterations"].median(),
        "exact": match.get("exact", 0), "nearest": match.get("nearest", 0), "overflow": match.get("overflow", 0),
        "parcels_with_units": int((cap["units"] > 0).sum()), "fallback": int(cap["fallback"].sum()),
        "gq_synthetic": float((persons["household_id"] < 0).sum()),
        "gq_published": float(city.get(("living_quarters", "group_quarters"), float("nan"))),
    }
    holdout = frozenset((runs / "holdout_blocks.txt").read_text().split())
    blocks = pd.DataFrame(sources.load("blocks").drop(columns="geometry"))
    report = validate(stats[stats["source"] == "synthetic"], pd.read_parquet(out / "validation_stats.parquet"),
                      pub, blocks, holdout, diagnostics)
    md = Path(args.docs) / "validation.md"
    report.write(md, out / "validation")
    print(f"wrote {md}")
    for k, v in report.headline.items():
        print(f"  {k}: {v}")


def cmd_publish(args) -> None:
    from pathlib import Path

    import pandas as pd

    from .publish import publish, publish_cells, publish_people, publish_people_runs, publish_places

    sources = _sources(args)
    data = sources.load_all()
    cap = pd.read_parquet(Path(args.data) / "runs" / "main" / "capacity.parquet")
    counts = publish(data, cap, sources.area, Path(args.data) / "out")
    print("published areas:", ", ".join(f"{k} {v:,}" for k, v in counts.items()))
    n = publish_people(Path(args.data) / "runs", data, sources.catalog, sources.area, Path(args.data) / "out")
    print(f"published {n:,} people for the map")
    n = publish_people_runs(Path(args.data) / "runs", data, sources.catalog, sources.area, Path(args.data) / "out")
    print(f"published people from {n} runs for the simulation player")
    cells = publish_cells(Path(args.data) / "runs", sources.catalog, Path(args.data) / "out")
    print(f"published risk-factor cells from {cells['draws']} draws and {cells['replicates']} replicates")
    print(f"published CDC PLACES rates for {publish_places(data, Path(args.data) / 'out')} tract-measures")


def cmd_build(args) -> None:
    """The whole build after fetch: run, summarize, validate, publish."""
    for step in (cmd_run, cmd_summarize, cmd_validate, cmd_publish):
        print(f"== {step.__name__[4:]}")
        step(args)


def cmd_serve(args) -> None:
    import uvicorn

    from .api import create_app

    # Deployment settings come from the environment (see docs/deploy-aws.md).
    app = create_app(Path(args.data) / "out", args.variables,
                     datapoints_path=os.environ.get("SYNTHPOP_DATAPOINTS", ROOT / "config" / "datapoints.yaml"),
                     password=os.environ.get("SYNTHPOP_PASSWORD") or None,
                     user=os.environ.get("SYNTHPOP_USER", "synthpop"))
    # Behind a load balancer, trust its X-Forwarded-Proto so redirects stay on https.
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")


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
    r.add_argument("--draws", type=int, help="placement draws of the main run")
    r.set_defaults(func=cmd_run)
    pr = sub.add_parser("persons", help="one row per synthetic person in one run, as CSV")
    pr.add_argument("--run", default="main", help="run folder under data/runs, e.g. main or draw_01")
    pr.add_argument("--out", help="output path (default data/out/persons_<run>.csv)")
    pr.set_defaults(func=cmd_persons)
    sub.add_parser("summarize", help="area stats with replicate standard errors").set_defaults(func=cmd_summarize)
    v = sub.add_parser("validate", help="write docs/validation.md")
    v.add_argument("--docs", default=str(ROOT / "docs"))
    v.set_defaults(func=cmd_validate)
    sub.add_parser("publish", help="areas, geometry and search index for the app").set_defaults(func=cmd_publish)
    b = sub.add_parser("build", help="run, summarize, validate and publish")
    b.add_argument("--replicates", type=int)
    b.add_argument("--draws", type=int)
    b.add_argument("--docs", default=str(ROOT / "docs"))
    b.set_defaults(func=cmd_build)
    s = sub.add_parser("serve", help="API and app on http://localhost:8000")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
