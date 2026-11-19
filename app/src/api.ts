// Thin client for the FastAPI routes. Every shape mirrors synthpop/atlas.py.

export type Category = { id: string; label: string };
export type Variable = {
  id: string; label: string; kind: "total" | "seed" | "derived" | "attached" | "datapoint";
  unit: "person" | "household"; universe: string | null; available: boolean; categories: Category[];
  model?: RiskModel;
};
// A data point: a risk model applied to every synthetic person (synthpop/datapoints.py).
export type RiskModel = {
  id: string; label: string; baseline: number;
  odds_ratios: Record<string, Record<string, number>>;
  universe: Record<string, string[]>;
  description: string; source: string;
  // "places:DIABETES": each tract's total matches CDC PLACES; `offsets` are the server's
  // per-tract log-odds shifts that do that, so the people layer agrees with the area figures.
  calibrate?: string; offsets?: Record<string, number>; offsets_error?: string;
};
export type Factor = { id: string; label: string; categories: Category[] };
export type Level = { id: string; label: string; min_zoom: number; max_zoom: number };
export type Schema = {
  levels: Level[]; compare_with: Record<string, string[]>; variables: Variable[];
  datapoints: { available: boolean; factors: Factor[]; presets: RiskModel[] };
  confidence: Record<string, string>; disclosure: string;
};
export type Confidence = "high" | "medium" | "low";
export type Filter = { variable: string; category: string; op: ">" | ">=" | "<" | "<="; value: number; measure: "share" | "estimate" };
export type LayerValues = {
  level: string; variable: string; category: string; measure: string;
  values: Record<string, [number, number, Confidence, boolean]>;
  breaks: number[]; max: number; filtered_count: number | null;
};
export type Stat = { estimate: number; se: number; share: number; share_se: number; confidence: Confidence };
export type Values = Record<string, Record<string, Stat>>;
export type Detail = {
  area: { level: string; area_id: string; name: string; lon: number; lat: number; attrs: Record<string, unknown>; [k: string]: unknown };
  values: Values; published: Values; disclosure: string;
  comparisons: { level: string; area_id: string; name: string; source: "synthetic" | "published"; values: Values }[];
  datapoints?: { id: string; label: string }[];
};
export type Breakdown = Record<string, { category: string; cases: number; rate: number; universe: number }[]>;
export type Preview = LayerValues & { city: Stat; breakdown: Breakdown; model: RiskModel };
export type SearchHit = { label: string; level: string; area_id: string; lon: number; lat: number; kind: string };

async function json<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

const qs = (p: Record<string, string | undefined>) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined) as [string, string][]).toString();

export const getSchema = () => json<Schema>("/api/schema");

export const getLayer = (level: string, variable: string, category: string, measure: string, filters: Filter[]) =>
  json<LayerValues>(`/api/layer?${qs({ level, variable, category, measure, filters: filters.length ? JSON.stringify(filters) : undefined })}`);

export const getDetail = (level: string, id: string) =>
  json<Detail>(`/api/areas/${level}/${encodeURIComponent(id)}`);

export const search = (q: string) => json<SearchHit[]>(`/api/search?${qs({ q })}`);

export const exportUrl = (level: string, format: "csv" | "geojson", filters: Filter[]) =>
  `/api/export?${qs({ level, format, filters: filters.length ? JSON.stringify(filters) : undefined })}`;

async function send<T>(method: "POST" | "DELETE", url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const text = await r.text();
    let msg = text;
    try { msg = JSON.parse(text).detail ?? text; } catch { /* plain text */ }
    throw new Error(msg);
  }
  return r.json() as Promise<T>;
}

export const previewDatapoint = (model: RiskModel, level: string, measure: string, filters: Filter[]) =>
  send<Preview>("POST", "/api/datapoints/preview", { model, level, measure, filters });
export const previewDetail = (level: string, area_id: string, model: RiskModel | null) =>
  send<Detail>("POST", "/api/datapoints/detail", { level, area_id, model });
export const saveDatapoint = (model: RiskModel) => send<RiskModel>("POST", "/api/datapoints", model);
export const deleteDatapoint = (id: string) => send<{ deleted: string }>("DELETE", `/api/datapoints/${encodeURIComponent(id)}`);

// The main run's synthetic persons, columnar: codes index each variable's schema categories (-1 blank).
export type People = {
  count: number; lon: number[]; lat: number[]; age: number[]; poverty_ratio: number[]; u: number[]; household: number[]; family: number[];
  parcel: number[]; parcels: string[]; block: number[]; blocks: string[]; fallback: number[];
  attributes: string[]; codes: Record<string, number[]>;
  // head: 1 for the householder. unit: each person's household circle (a household, or one
  // parcel's group-quarters residents), an index into units (centre and radius in metres).
  head: number[]; unit: number[]; units: { lon: number[]; lat: number[]; r: number[] };
};
export const getPeople = (run?: string) => json<People>(run && run !== "main" ? `/api/people?run=${encodeURIComponent(run)}` : "/api/people");
// The saved runs, in playback order: main, placement draws, replicates, validation.
export type PeopleRun = { name: string; kind: "main" | "draw" | "replicate" | "validation"; people: number; households: number };
export const getPeopleRuns = () => json<PeopleRun[]>("/api/people/runs");

const geoCache = new Map<string, Promise<GeoJSON.FeatureCollection>>();
export const getGeometry = (level: string) => {
  if (!geoCache.has(level)) geoCache.set(level, json<GeoJSON.FeatureCollection>(`/geo/${level}.geojson`));
  return geoCache.get(level)!;
};
