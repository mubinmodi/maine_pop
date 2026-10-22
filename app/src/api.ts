// Thin client for the FastAPI routes. Every shape mirrors synthpop/atlas.py.

export type Category = { id: string; label: string };
export type Variable = {
  id: string; label: string; kind: "total" | "seed" | "derived" | "attached";
  unit: "person" | "household"; universe: string | null; available: boolean; categories: Category[];
};
export type Level = { id: string; label: string; min_zoom: number; max_zoom: number };
export type Schema = {
  levels: Level[]; compare_with: Record<string, string[]>; variables: Variable[];
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
};
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

const geoCache = new Map<string, Promise<GeoJSON.FeatureCollection>>();
export const getGeometry = (level: string) => {
  if (!geoCache.has(level)) geoCache.set(level, json<GeoJSON.FeatureCollection>(`/geo/${level}.geojson`));
  return geoCache.get(level)!;
};
