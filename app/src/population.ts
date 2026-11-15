// One run's synthetic people, with every data point applied to them.
//
// The people arrive as one columnar document (synthpop/atlas.py). This module is the only
// code that reads it: codes into schema categories, -1 for blanks, parcel and block codes,
// household circles (units) and the householder flag all stay in here. Everything else asks
// for a person, a household, a dimension to colour or filter by, or an area's people.
//
// A data point marks a person a case when their fixed draw u is below their risk, so moving
// a slider adds or removes cases at the margin instead of reshuffling who has the condition.
import type { People, RiskModel, Schema } from "./api";

export type Cat = { id: string; label: string };
/** Something to colour or filter people by: a data point, an attribute or the age band.
 * code(i) is a category index (-1 blank). For a data point: 0 case, 1 not, 2 outside its universe. */
export type Dim = { id: string; label: string; kind: "datapoint" | "attribute"; cats: Cat[]; code: (i: number) => number };
export type Focus = { level: string; id: string };

export type Person = {
  id: number; age: number; unit: number; parcel: string; block: string; lon: number; lat: number;
  household: number | null;     // null in group quarters
  family: number | null;        // null outside a Census family
  povertyRatio: number | null;  // income as % of the poverty line (501 = over 500); null where not determined
  fallback: boolean;            // placed at a random point in the parcel, not on an address point
  role: "Householder" | "Relative" | "Not related" | "Group quarters resident";
};

export type Household = {
  unit: number; groupQuarters: boolean; household: number | null; family: number | null;
  members: number[];            // householder first, then oldest first
  parcel: string; lon: number; lat: number; r: number;   // the circle: centre and radius in metres
};

export type AreaSummary = {
  people: number; households: number; families: number; medianAge: number | null; female: number; groupQuarters: number;
  dps: { label: string; cases: number; universe: number }[];
};

export const DATAPOINT = { case: 0, notCase: 1, outside: 2 } as const;
const AGE_BANDS: [string, number][] = [["Under 18", 18], ["18–34", 35], ["35–64", 65], ["65+", Infinity]];
const DP_CATS: Cat[] = [{ id: "1", label: "Case" }, { id: "0", label: "Not a case" }, { id: "na", label: "Outside the model's universe" }];
const DRAFT = "__draft";

export class Population {
  private doc: People;
  private name: string;
  private schema: Schema;
  private outcomes = new Map<string, { model: RiskModel; key: string; v: Uint8Array }>();
  private groups: { start: Int32Array; list: Int32Array } | null = null;
  private dimCache: Dim[] | null = null;
  private v = 0;

  constructor(doc: People, run: string, schema: Schema) {
    this.doc = doc;
    this.name = run;
    this.schema = schema;
    this.setSchema(schema);
  }

  /** Which saved run these people are ("main", "draw_07", ...). */
  get run() { return this.name; }
  get count() { return this.doc.count; }
  get units() { return this.doc.units.r.length; }
  /** Goes up whenever any person's values or the data points change; for caches built on top. */
  get version() { return this.v; }

  // ---------------------------------------------------------------- changes

  /** Saved data points become person dimensions; unchanged ones are kept, not recomputed. */
  setSchema(schema: Schema) {
    this.schema = schema;
    const saved = schema.variables.filter((x) => x.kind === "datapoint" && x.model).map((x) => x.model!);
    for (const id of [...this.outcomes.keys()]) if (id !== DRAFT && !saved.some((m) => m.id === id)) this.outcomes.delete(id);
    for (const m of saved) this.apply(m.id, m);
    this.changed();
  }

  /** The data point being edited, recomputed on every slider move; null removes it. */
  setDraft(m: RiskModel | null) {
    if (m) this.apply(DRAFT, m); else this.outcomes.delete(DRAFT);
    this.changed();
  }

  /** The draft's dimension id, while a data point is being edited. */
  draftId() { return this.outcomes.has(DRAFT) ? `dp:${DRAFT}` : null; }

  /** Swap in another run's people. Data points are reapplied to them. */
  setRun(doc: People, run: string) {
    this.doc = doc;
    this.name = run;
    this.groups = null;
    for (const o of this.outcomes.values()) o.v = outcomes(doc, this.schema, o.model);
    this.changed();
  }

  private apply(id: string, m: RiskModel) {
    const key = JSON.stringify(m);
    if (this.outcomes.get(id)?.key === key) return;
    this.outcomes.set(id, { model: m, key, v: outcomes(this.doc, this.schema, m) });
  }

  private changed() { this.v++; this.dimCache = null; }

  // ---------------------------------------------------------------- dimensions

  /** Data points first (the draft labelled as such), then age band, then every attribute. */
  dims(): Dim[] {
    if (this.dimCache) return this.dimCache;
    const p = this.doc;
    const dps: Dim[] = [...this.outcomes].map(([id, o]) => ({
      id: `dp:${id}`, label: id === DRAFT ? `${o.model.label} (draft)` : o.model.label, kind: "datapoint", cats: DP_CATS,
      code: (i: number) => o.v[i],
    }));
    const age: Dim = { id: "age_band", label: "Age", kind: "attribute", cats: AGE_BANDS.map(([l]) => ({ id: l, label: l })),
      code: (i) => { const a = p.age[i]; return a < 0 ? -1 : AGE_BANDS.findIndex(([, hi]) => a < hi); } };
    const attrs: Dim[] = p.attributes.flatMap((a) => {
      const v = this.schema.variables.find((x) => x.id === a);
      if (!v) return [];
      const codes = p.codes[a];
      return [{ id: a, label: v.label, kind: "attribute" as const, cats: v.categories, code: (i: number) => codes[i] }];
    });
    return (this.dimCache = [...dps, age, ...attrs]);
  }

  dim(id: string) { return this.dims().find((d) => d.id === id); }

  /** A person's category label on a dimension, or null where blank or unknown. */
  label(dimId: string, i: number): string | null {
    const d = this.dim(dimId), c = d ? d.code(i) : -1;
    return d && c >= 0 && c < d.cats.length ? d.cats[c].label : null;
  }

  // ---------------------------------------------------------------- people and households

  person(i: number): Person {
    const p = this.doc;
    const household = p.household[i] >= 0 ? p.household[i] : null, family = p.family[i] >= 0 ? p.family[i] : null;
    return {
      id: i, age: p.age[i], unit: p.unit[i], parcel: p.parcels[p.parcel[i]], block: p.blocks[p.block[i]],
      lon: p.lon[i], lat: p.lat[i], household, family,
      povertyRatio: p.poverty_ratio[i] >= 0 ? p.poverty_ratio[i] : null, fallback: !!p.fallback[i],
      role: household === null ? "Group quarters resident" : p.head[i] ? "Householder" : family !== null ? "Relative" : "Not related",
    };
  }

  /** Each unit's people, householder first, then oldest first. */
  members(u: number): number[] {
    const { start, list } = this.grouped();
    return Array.from(list.subarray(start[u], start[u + 1]));
  }

  /** Calls f(u, members) for every unit, in unit order, without copying member lists. */
  eachUnit(f: (u: number, members: Int32Array) => void) {
    const { start, list } = this.grouped();
    for (let u = 0; u < this.units; u++) f(u, list.subarray(start[u], start[u + 1]));
  }

  household(u: number): Household {
    const members = this.members(u), first = this.person(members[0]), c = this.doc.units;
    return { unit: u, groupQuarters: first.household === null, household: first.household, family: first.family, members,
      parcel: first.parcel, lon: c.lon[u], lat: c.lat[u], r: c.r[u] };
  }

  private grouped() {
    if (!this.groups) {
      const p = this.doc, nu = this.units;
      const start = new Int32Array(nu + 1);
      for (let i = 0; i < p.count; i++) start[p.unit[i] + 1]++;
      for (let k = 0; k < nu; k++) start[k + 1] += start[k];
      const fill = start.slice(0, nu), list = new Int32Array(p.count);
      for (let i = 0; i < p.count; i++) list[fill[p.unit[i]]++] = i;
      for (let k = 0; k < nu; k++) list.subarray(start[k], start[k + 1]).sort((a, b) => p.head[b] - p.head[a] || p.age[b] - p.age[a] || a - b);
      this.groups = { start, list };
    }
    return this.groups;
  }

  // ---------------------------------------------------------------- finding people

  /** Tests whether a person lives in an area. Block ids begin with their tract (11 digits)
   * and block group (12), so every level but parcel and city is a prefix of the block. */
  matcher(f: Focus): (i: number) => boolean {
    const p = this.doc;
    if (f.level === "parcel") { const k = p.parcels.indexOf(f.id); return (i) => p.parcel[i] === k; }
    if (["block", "block_group", "tract"].includes(f.level)) {
      const ok = p.blocks.map((b) => b.startsWith(f.id));
      return (i) => ok[p.block[i]];
    }
    return () => f.level === "city";
  }

  inArea(f: Focus): number[] {
    const m = this.matcher(f), out: number[] = [];
    for (let i = 0; i < this.count; i++) if (m(i)) out.push(i);
    return out;
  }

  /** A search for an ID: a number is an exact person, family or household ID; anything with a
   * letter is a parcel ID prefix (e.g. "014 J001"), ignoring spaces and case. null for an empty query. */
  idSearch(query: string): ((i: number) => boolean) | null {
    const q = query.trim().replace(/\s+/g, "").toUpperCase();
    if (!q) return null;
    const p = this.doc;
    const n = /^\d+$/.test(q) ? Number(q) : NaN;
    const parcelHit = p.parcels.map((x) => { const k = x.replace(/\s+/g, "").toUpperCase(); return Number.isNaN(n) ? k.startsWith(q) : k === q; });
    return (i) => i === n || p.family[i] === n || p.household[i] === n || parcelHit[p.parcel[i]];
  }

  // ---------------------------------------------------------------- tables

  /** Columns of the person table and CSV. The first ten match `synthpop persons`: IDs, exact
   * age, sex, race, education, poverty and employment; then household, block, the address
   * point and one column per data point (1 case, 0 not, blank outside the universe). */
  columns(): [string, (i: number) => string | number][] {
    const p = this.doc, dims = this.dims();
    const cat = (id: string) => { const d = dims.find((x) => x.id === id); return (i: number) => { const c = d ? d.code(i) : -1; return c >= 0 ? d!.cats[c].id : ""; }; };
    return [
      ["person_id", (i) => i],
      ["family_id", (i) => (p.family[i] >= 0 ? p.family[i] : "")],
      ["parcel_id", (i) => p.parcels[p.parcel[i]]],
      ["AGE", (i) => p.age[i]],
      ["SEX", cat("sex")],
      ["RACE", cat("race_eth")],
      ["EDUCATION", cat("education")],
      ["POVERTY", cat("poverty")],
      ["POVERTY_RATIO", (i) => (p.poverty_ratio[i] >= 0 ? p.poverty_ratio[i] : "")],
      ["EMPLOYMENT", cat("employment")],
      ["household_id", (i) => (p.household[i] >= 0 ? p.household[i] : "")],
      ["block", (i) => p.blocks[p.block[i]]],
      ["lon", (i) => p.lon[i]],
      ["lat", (i) => p.lat[i]],
      ["location", (i) => (p.fallback[i] ? "parcel" : "address")],
      ...dims.filter((d) => d.kind === "datapoint").map((d) =>
        [d.id.slice(3), (i: number) => ["1", "0", ""][d.code(i)]] as [string, (i: number) => string]),
    ];
  }

  csv(idx: ArrayLike<number>): string {
    const cols = this.columns();
    const q = (v: string | number) => (typeof v === "string" && /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : String(v));
    return [cols.map(([h]) => h).join(","), ...Array.from(idx, (i) => cols.map(([, f]) => q(f(i))).join(","))].join("\n") + "\n";
  }

  /** Counts from the people themselves (one realisation), for the detail panel. */
  summary(idx: number[]): AreaSummary {
    const p = this.doc;
    const households = new Set(idx.map((i) => p.household[i]).filter((h) => h >= 0)).size;
    const families = new Set(idx.map((i) => p.family[i]).filter((h) => h >= 0)).size;
    const ages = idx.map((i) => p.age[i]).filter((a) => a >= 0).sort((a, b) => a - b);
    const sex = this.dim("sex");
    const female = sex ? idx.filter((i) => sex.cats[sex.code(i)]?.id === "female").length : 0;
    const dps = this.dims().filter((d) => d.kind === "datapoint").map((d) => ({
      label: d.label, cases: idx.filter((i) => d.code(i) === DATAPOINT.case).length,
      universe: idx.filter((i) => d.code(i) !== DATAPOINT.outside).length,
    }));
    return { people: idx.length, households, families, medianAge: ages.length ? ages[Math.floor(ages.length / 2)] : null,
      female, groupQuarters: idx.filter((i) => p.household[i] < 0).length, dps };
  }
}

/** 0 case, 1 not a case, 2 outside the universe, for every person (as synthpop/datapoints.py). */
function outcomes(p: People, schema: Schema, m: RiskModel): Uint8Array {
  const cats = (f: string) => schema.variables.find((v) => v.id === f)?.categories.map((c) => c.id) ?? [];
  const logOr = Object.entries(m.odds_ratios).map(([f, ors]) => [p.codes[f], cats(f).map((c) => Math.log(ors[c] ?? 1))] as const);
  const inside = Object.entries(m.universe).map(([f, allowed]) => [p.codes[f], cats(f).map((c) => allowed.includes(c))] as const);
  const base = Math.log(m.baseline / (1 - m.baseline));
  const out = new Uint8Array(p.count);
  for (let i = 0; i < p.count; i++) {
    if (!inside.every(([codes, ok]) => codes && codes[i] >= 0 && ok[codes[i]])) { out[i] = DATAPOINT.outside; continue; }
    let l = base;
    for (const [codes, lo] of logOr) if (codes && codes[i] >= 0) l += lo[codes[i]];
    out[i] = p.u[i] < 1 / (1 + Math.exp(-l)) ? DATAPOINT.case : DATAPOINT.notCase;
  }
  return out;
}
