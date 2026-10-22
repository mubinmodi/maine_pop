"""The fetcher port: the one seam between this project and outside services.

`HttpFetcher` talks to the Census Bureau and the Maine GeoLibrary.
`FixtureFetcher` serves files from a folder, keyed by the same names, for tests.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Iterator, Protocol

import requests


class Fetcher(Protocol):
    def download(self, key: str, url: str, dest: Path) -> Path:
        """Save the resource at `url` to `dest` and return `dest`."""

    def json(self, key: str, url: str, params: dict) -> dict:
        """POST `params` to `url` (ArcGIS style) and return the parsed JSON."""

    def lines(self, key: str, url: str) -> Iterator[str]:
        """Stream a text resource line by line without storing it."""


class HttpFetcher:
    def __init__(self, timeout: int = 120, retries: int = 4):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "maine-synthpop/0.1"

    def _with_retries(self, fn):
        for attempt in range(self.retries):
            try:
                return fn()
            except (requests.RequestException, ValueError):
                if attempt == self.retries - 1:
                    raise
                time.sleep(2 ** attempt)

    def download(self, key: str, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")

        def go():
            with self.session.get(url, stream=True, timeout=self.timeout) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            tmp.replace(dest)
            return dest

        return self._with_retries(go)

    def json(self, key: str, url: str, params: dict) -> dict:
        def go():
            r = self.session.post(url, data=params, timeout=self.timeout)
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise ValueError(f"{key}: {body['error']}")
            return body

        return self._with_retries(go)

    def lines(self, key: str, url: str) -> Iterator[str]:
        with self.session.get(url, stream=True, timeout=self.timeout) as r:
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"      # census.gov sends no charset, so lines arrive as bytes otherwise
            for line in r.iter_lines(decode_unicode=True):
                if line:
                    yield line


class FixtureFetcher:
    """Serves `<root>/<key>.<ext>`. ArcGIS keys return the same page every call,
    with `exceededTransferLimit` false, so paging stops after one page."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _find(self, key: str) -> Path:
        hits = sorted(self.root.glob(f"{key}.*"))
        if not hits:
            raise FileNotFoundError(f"no fixture for {key} in {self.root}")
        return hits[0]

    def download(self, key: str, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._find(key), dest)
        return dest

    def json(self, key: str, url: str, params: dict) -> dict:
        if params.get("returnCountOnly") == "true":
            body = json.loads(self._find(key).read_text())
            return {"count": len(body["features"])}
        offset = int(params.get("resultOffset", 0))
        if offset:
            return {"type": "FeatureCollection", "features": []}
        return json.loads(self._find(key).read_text())

    def lines(self, key: str, url: str) -> Iterator[str]:
        with open(self._find(key)) as f:
            for line in f:
                line = line.rstrip("\n")
                if line:
                    yield line
