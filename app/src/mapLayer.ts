import { GeoJsonLayer } from "@deck.gl/layers";
import type { Confidence, LayerValues } from "./api";

// Sequential blue ramp (one hue, light → dark) for magnitude, five classes.
export const RAMP: [number, number, number][] = [
  [158, 197, 244], [109, 167, 236], [57, 135, 229], [37, 106, 191], [16, 66, 129],
];
const NO_DATA: [number, number, number] = [225, 224, 217];
const FILTERED_OUT: [number, number, number] = [195, 194, 183];

export type ConfidenceMode = "show" | "flag" | "hide";

// Highest extrusion per level, in metres, so towers stay readable at each zoom.
const HEIGHT: Record<string, number> = { city: 1200, tract: 900, block_group: 700, block: 350, parcel: 120 };

export type RenderOptions = {
  extruded: boolean;
  confidence: ConfidenceMode;
  onClick: (id: string) => void;
  selected: string | null;
};

export function classOf(v: number, breaks: number[]): number {
  let i = 0;
  while (i < breaks.length && v > breaks[i]) i++;
  return i;
}

export function renderLevel(
  level: string,
  geometry: GeoJSON.FeatureCollection,
  color: LayerValues,
  extrude: LayerValues | null,
  opts: RenderOptions,
): GeoJsonLayer {
  const vals = color.values;
  const lowHidden = opts.confidence === "hide";
  const features = geometry.features.filter((f) => {
    if (!lowHidden) return true;
    const v = vals[(f.properties as { id: string }).id];
    return !v || v[2] !== "low";
  });
  const maxExtrude = extrude ? Math.max(extrude.max, 1e-9) : 1;

  return new GeoJsonLayer({
    id: `areas-${level}`,
    data: { type: "FeatureCollection", features } as GeoJSON.FeatureCollection,
    pickable: true,
    stroked: true,
    filled: true,
    extruded: opts.extruded && extrude !== null,
    wireframe: false,
    lineWidthUnits: "pixels",
    getLineWidth: (f: GeoJSON.Feature) => ((f.properties as { id: string }).id === opts.selected ? 3 : level === "parcel" ? 0.4 : 1),
    getLineColor: (f: GeoJSON.Feature) =>
      (f.properties as { id: string }).id === opts.selected ? [11, 11, 11, 255] : [255, 255, 255, 180],
    getFillColor: (f: GeoJSON.Feature) => {
      const v = vals[(f.properties as { id: string }).id];
      if (!v || v[0] <= 0) return [...NO_DATA, 140];
      if (!v[3]) return [...FILTERED_OUT, 90];
      const base = RAMP[classOf(v[0], color.breaks)];
      const faded = opts.confidence === "flag" && (v[2] as Confidence) === "low";
      return [...base, faded ? 70 : 225];
    },
    getElevation: (f: GeoJSON.Feature) => {
      if (!extrude) return 0;
      const id = (f.properties as { id: string }).id;
      if (vals[id] && !vals[id][3]) return 0;     // areas outside the filters lie flat
      const v = extrude.values[id];
      return v ? (v[0] / maxExtrude) * HEIGHT[level] : 0;
    },
    material: { ambient: 0.6, diffuse: 0.5, shininess: 16 },
    onClick: (info) => {
      const id = (info.object as GeoJSON.Feature | undefined)?.properties?.id as string | undefined;
      if (id) opts.onClick(id);
    },
    updateTriggers: {
      getFillColor: [color, opts.confidence],
      getElevation: [extrude, color],
      getLineWidth: [opts.selected],
      getLineColor: [opts.selected],
    },
    transitions: { getElevation: 400 },
  });
}

export const fmt = (v: number, measure: string) =>
  measure === "share" ? `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%` : v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1);

export function renderLegend(el: HTMLElement, title: string, color: LayerValues, mode: ConfidenceMode, extrudeLabel: string | null) {
  const b = color.breaks;
  const edges = [0, ...b, color.max];
  const rows = RAMP.map((c, i) => {
    if (i > b.length) return "";
    const lo = fmt(edges[i], color.measure);
    const hi = fmt(edges[i + 1] ?? color.max, color.measure);
    return `<li><i style="background:rgb(${c.join(",")})"></i>${i === 0 ? "up to " + hi : lo + " – " + hi}</li>`;
  }).join("");
  el.innerHTML = `
    <div class="legend-title">${title}</div>
    <ul class="ramp">${rows}
      <li><i style="background:rgb(${NO_DATA.join(",")})"></i>None or no data</li>
      ${color.filtered_count !== null ? `<li><i style="background:rgba(${FILTERED_OUT.join(",")},.5)"></i>Doesn't match filters</li>` : ""}
      ${mode === "flag" ? `<li><i class="faded"></i>Low confidence (faded)</li>` : ""}
    </ul>
    ${extrudeLabel ? `<div class="legend-note">Height: ${extrudeLabel}</div>` : ""}
    <div class="legend-note">Synthetic estimates</div>`;
}
