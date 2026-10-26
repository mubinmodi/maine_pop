"""HTTP routes: one per Atlas method, plus static geometry and the built app."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .atlas import Atlas, Filter
from .catalog import Catalog
from .config import ROOT


def _filters(raw: str | None) -> list[Filter]:
    if not raw:
        return []
    try:
        return [Filter.parse(f) for f in json.loads(raw)]
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"bad filters: {e}")


def create_app(out_dir: str | Path = ROOT / "data" / "out", catalog_path: str | Path = ROOT / "config" / "variables.yaml",
               app_dir: str | Path = ROOT / "app" / "dist",
               datapoints_path: str | Path = ROOT / "config" / "datapoints.yaml") -> FastAPI:
    out = Path(out_dir)
    atlas = Atlas.open(out, Catalog.load(catalog_path), datapoints_path)
    api = FastAPI(title="Maine synthetic population", description=atlas.schema()["disclosure"])
    api.add_middleware(GZipMiddleware, minimum_size=2000)

    def guard(fn):
        try:
            return fn()
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @api.get("/api/schema")
    def schema():
        return atlas.schema()

    @api.get("/api/layer")
    def layer(level: str, variable: str, category: str = "total", measure: str = "estimate",
              filters: str | None = None):
        return guard(lambda: atlas.layer(level, variable, category, measure, _filters(filters)))

    @api.get("/api/areas/{level}/{area_id:path}")
    def detail(level: str, area_id: str):
        return guard(lambda: atlas.detail(level, area_id))

    # Data points: live risk-model previews, and saving them as variables.
    @api.post("/api/datapoints/preview")
    def preview(body: dict = Body(...)):
        return guard(lambda: atlas.preview(body["model"], body.get("level", "block_group"), body.get("measure", "share"),
                                           [Filter.parse(f) for f in body.get("filters", [])]))

    @api.post("/api/datapoints/detail")
    def preview_detail(body: dict = Body(...)):
        return guard(lambda: atlas.detail(body["level"], body["area_id"], body.get("model")))

    @api.post("/api/datapoints")
    def save_datapoint(body: dict = Body(...)):
        return guard(lambda: atlas.save_datapoint(body))

    @api.delete("/api/datapoints/{id}")
    def delete_datapoint(id: str):
        guard(lambda: atlas.delete_datapoint(id))
        return {"deleted": id}

    @api.get("/api/search")
    def search(q: str = Query(..., min_length=1)):
        return atlas.search(q)

    @api.get("/api/export")
    def export(level: str, format: str = "csv", filters: str | None = None, ids: str | None = None):
        body, media = guard(lambda: atlas.export(level, format, ids.split(",") if ids else None, _filters(filters)))
        ext = "csv" if format == "csv" else "geojson"
        return Response(body, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="portland_{level}_synthetic.{ext}"'})

    @api.get("/geo/{level}.geojson")
    def geo(level: str):
        path = out / "geo" / f"{level}.geojson"
        if not path.exists():
            raise HTTPException(404, f"no geometry for {level}")
        return FileResponse(path, media_type="application/geo+json")

    app_dir = Path(app_dir)
    if app_dir.exists():
        api.mount("/", StaticFiles(directory=app_dir, html=True), name="app")
    return api
