import { GeoJsonLayer } from "@deck.gl/layers";
import type { Confidence, LayerValues } from "./api";

type RGB = [number, number, number];
const hex = (h: string): RGB => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) as RGB;

// Five-class ramps, light → dark, each checked with the dataviz palette validator
// (monotone lightness, visible steps, light end ≥ 2:1 against the basemap).
export type Scheme = "green_blue" | "heat" | "vs_city" | "spectrum";
export const SCHEMES: Record<Scheme, { label: string; colors: RGB[]; continuous?: boolean }> = {
  // Analogous green → teal → blue → indigo: the default for magnitude.
  green_blue: { label: "Green–blue", colors: ["#7cc495", "#36a9a0", "#1d87b4", "#2c56a3", "#2a2672"].map(hex) },
  // Amber → red → plum, for "heat" style reading of concentrations.
  heat: { label: "Heat", colors: ["#f2a03d", "#e8692d", "#d13a30", "#a01f3d", "#561241"].map(hex) },
  // Diverging around the Portland-wide value: blue below, neutral grey about the same, red above.
  vs_city: { label: "vs Portland", colors: ["#1c5cab", "#6da7ec", "#dcdbd4", "#ee8a80", "#a8282b"].map(hex) },
  // Full blue → cyan → green → yellow → orange → red spectrum, shaded continuously.
  // Lightness peaks at yellow, so order is read from the legend, not from light/dark,
  // and green/red neighbours are hard to tell apart for red-green colour blindness.
  spectrum: {
    label: "Spectrum",
    colors: ["#2c7bb6", "#00a6ca", "#00ccbc", "#90eb9d", "#f9d057", "#f29e2e", "#d7191c"].map(hex),
    continuous: true,
  },
};

/** Position 0–1 of a value along the scale: piecewise-linear through the quantile
 * breaks, so colours spread evenly over areas instead of bunching under outliers. */
export function scalePosition(v: number, breaks: number[], max: number): number {
  const knots = [0, ...breaks, Math.max(max, breaks[breaks.length - 1] ?? 0)];
  if (v <= knots[0]) return 0;
  for (let i = 1; i < knots.length; i++) {
    if (v <= knots[i]) {
      const span = knots[i] - knots[i - 1] || 1;
      return (i - 1 + (v - knots[i - 1]) / span) / (knots.length - 1);
    }
  }
  return 1;
}

export function interpolate(stops: RGB[], t: number): RGB {
  const x = Math.min(Math.max(t, 0), 1) * (stops.length - 1);
  const i = Math.min(Math.floor(x), stops.length - 2);
  const f = x - i;
  return stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f)) as RGB;
}

const gradientCss = (stops: RGB[]) =>
  `linear-gradient(to right, ${stops.map((c, i) => `rgb(${c.join(",")}) ${(i / (stops.length - 1)) * 100}%`).join(", ")})`;
const NO_DATA: RGB = [235, 234, 228];
const FILTERED_OUT: RGB = [195, 194, 183];

export type ConfidenceMode = "show" | "flag" | "hide";

/** Breaks for the diverging scheme: symmetric around zero difference from the city. */
export function divergingBreaks(diffs: number[]): number[] {
  const abs = diffs.map(Math.abs).sort((a, b) => a - b);
  const t = abs.length ? abs[Math.floor(abs.length * 0.8)] || abs[abs.length - 1] || 0.01 : 0.01;
  return [-t, -t / 3, t / 3, t];
}

// Highest extrusion per level, in metres, so towers stay readable at each zoom.
const HEIGHT: Record<string, number> = { city: 1200, tract: 900, block_group: 700, block: 350, parcel: 120 };

export type RenderOptions = {
  extruded: boolean;
  confidence: ConfidenceMode;
  onClick: (id: string) => void;
  selected: string | null;
  scheme: Scheme;
  reference: number | null;      // Portland-wide value, for the vs_city scheme
  breaks: number[];              // class edges (differences from the reference for vs_city)
  fade?: boolean;                // flat and faint, so the people layer reads on top
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
    extruded: opts.extruded && extrude !== null && !opts.fade,
    opacity: opts.fade ? 0.35 : 1,
    wireframe: false,
    lineWidthUnits: "pixels",
    getLineWidth: (f: GeoJSON.Feature) => ((f.properties as { id: string }).id === opts.selected ? 3 : level === "parcel" ? 0.4 : 1),
    getLineColor: (f: GeoJSON.Feature) =>
      (f.properties as { id: string }).id === opts.selected ? [11, 11, 11, 255] : [255, 255, 255, 180],
    getFillColor: (f: GeoJSON.Feature) => {
      const v = vals[(f.properties as { id: string }).id];
      if (!v || v[0] <= 0) return [...NO_DATA, 140];
      if (!v[3]) return [...FILTERED_OUT, 90];
      const x = opts.scheme === "vs_city" && opts.reference !== null ? v[0] - opts.reference : v[0];
      const sch = SCHEMES[opts.scheme];
      const base = sch.continuous ? interpolate(sch.colors, scalePosition(x, opts.breaks, color.max))
        : sch.colors[classOf(x, opts.breaks)];
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
      getFillColor: [color, opts.confidence, opts.scheme, opts.reference],
      getElevation: [extrude, color],
      getLineWidth: [opts.selected],
      getLineColor: [opts.selected],
    },
    transitions: { getElevation: 400 },
  });
}

export const fmt = (v: number, measure: string) =>
  measure === "share" ? `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%` : v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1);

const pts = (v: number) => `${(Math.abs(v) * 100).toFixed(Math.abs(v) < 0.1 ? 1 : 0)} pts`;

export function renderLegend(el: HTMLElement, title: string, color: LayerValues, mode: ConfidenceMode,
                             extrudeLabel: string | null, scheme: Scheme, breaks: number[], reference: number | null) {
  const colors = SCHEMES[scheme].colors;
  let rows: string;
  if (SCHEMES[scheme].continuous) {
    // Tick labels sit at the quantile knots the colours are stretched over.
    const knots = [0, ...breaks, color.max];
    rows = `<li class="gradient-row"><span class="gradient" style="background:${gradientCss(colors)}"></span></li>
      <li class="gradient-ticks">${knots.map((k) => `<span>${fmt(k, color.measure)}</span>`).join("")}</li>`;
  } else if (scheme === "vs_city" && reference !== null) {
    const [a, b] = [breaks[0], breaks[1]];
    const labels = [`${pts(a)}+ below Portland`, `${pts(b)}–${pts(a)} below`, `within ${pts(b)} of Portland (${fmt(reference, "share")})`,
                    `${pts(b)}–${pts(a)} above`, `${pts(a)}+ above Portland`];
    rows = colors.map((c, i) => `<li><i style="background:rgb(${c.join(",")})"></i>${labels[i]}</li>`).join("");
  } else {
    const edges = [0, ...breaks, color.max];
    rows = colors.map((c, i) => {
      if (i > breaks.length) return "";
      const lo = fmt(edges[i], color.measure);
      const hi = fmt(edges[i + 1] ?? color.max, color.measure);
      return `<li><i style="background:rgb(${c.join(",")})"></i>${i === 0 ? "up to " + hi : lo + " – " + hi}</li>`;
    }).join("");
  }
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
