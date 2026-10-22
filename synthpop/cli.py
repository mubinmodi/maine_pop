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


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="synthpop")
    p.add_argument("--data", default=str(ROOT / "data"))
    p.add_argument("--study-area", default=str(ROOT / "config" / "study_area.yaml"))
    p.add_argument("--variables", default=str(ROOT / "config" / "variables.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download, clip and cache every input")
    f.add_argument("--only", nargs="*", choices=NAMES)
    f.set_defaults(func=cmd_fetch)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
