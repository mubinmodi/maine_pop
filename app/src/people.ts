// The people layer: every synthetic person of the main run as a dot on their E911
// address point, stacked upward where several share one (apartment buildings).
// Everything after the one download runs in the browser, so colouring, filtering and
// data points react instantly. A data point marks a person a case when their fixed
// draw u is below their risk, so moving a slider adds or removes cases at the margin
// instead of reshuffling who has the condition.
import { ScatterplotLayer } from "@deck.gl/layers";
import type { People, RiskModel, Schema } from "./api";

type RGB = [number, number, number];
const hex = (h: string): RGB => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) as RGB;

// Categorical slots in fixed order, checked with the dataviz palette validator on the
// light surface (contrast is below 3:1 for some, so the legend labels every colour).
const SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"].map(hex);
const BLANK: RGB = [175, 175, 170];
const CASE = SLOTS[1], NO_CASE = SLOTS[0];

export const MIN_ZOOM = 14;          // people show from block zoom down
const FLOOR_M = 2.5;                 // height between people stacked on one address point
const DRAFT = "__draft";

export type PeopleFilter = { by: string; values: string[] };
export type Focus = { level: string; id: string };
// focus: the area open in the detail panel; people outside it are faded.
export type PeopleState = { show: boolean; colorBy: string; filters: PeopleFilter[]; focus?: Focus | null };

type Cat = { id: string; label: string };
/** Something to colour or filter people by: a data point, an attribute or the age band. */
export type Dim = { id: string; label: string; kind: "datapoint" | "attribute"; cats: Cat[]; code: (i: number) => number };

const AGE_BANDS: [string, number][] = [["Under 18", 18], ["18–34", 35], ["35–64", 65], ["65+", Infinity]];
const DP_CATS: Cat[] = [{ id: "1", label: "Case" }, { id: "0", label: "Not a case" }, { id: "na", label: "Outside the model's universe" }];

export class PeopleLayer {
  readonly p: People;
  private schema: Schema;
  private positions: Float64Array;
  private outcomes = new Map<string, { model: RiskModel; key: string; v: Uint8Array }>();  // 0 case, 1 not, 2 outside
  private view: { key: string; idx: Uint32Array; pos: Float64Array; col: Uint8Array; counts: number[] } | null = null;

  constructor(p: People, schema: Schema) {
    this.p = p;
    this.schema = schema;
    this.positions = stack(p);
    this.setSchema(schema);
  }

  /** Saved data points become person variables automatically; unchanged ones are kept. */
  setSchema(schema: Schema) {
    this.schema = schema;
    const saved = schema.variables.filter((v) => v.kind === "datapoint" && v.model).map((v) => v.model!);
    for (const id of [...this.outcomes.keys()]) if (id !== DRAFT && !saved.some((m) => m.id === id)) this.outcomes.delete(id);
    for (const m of saved) this.apply(m.id, m);
    this.view = null;
  }

  /** The data point being edited, recomputed on every slider move; null removes it. */
  setDraft(m: RiskModel | null) {
    if (m) this.apply(DRAFT, m); else this.outcomes.delete(DRAFT);
    this.view = null;
  }

  private apply(id: string, m: RiskModel) {
    const key = JSON.stringify(m);
    if (this.outcomes.get(id)?.key === key) return;
    this.outcomes.set(id, { model: m, key, v: outcomes(this.p, this.schema, m) });
  }

  dims(): Dim[] {
    const dps: Dim[] = [...this.outcomes].map(([id, o]) => ({
      id: `dp:${id}`, label: id === DRAFT ? `${o.model.label} (draft)` : o.model.label, kind: "datapoint", cats: DP_CATS,
      code: (i: number) => o.v[i],
    }));
    const age: Dim = { id: "age_band", label: "Age", kind: "attribute", cats: AGE_BANDS.map(([l]) => ({ id: l, label: l })),
      code: (i) => { const a = this.p.age[i]; return a < 0 ? -1 : AGE_BANDS.findIndex(([, hi]) => a < hi); } };
    const attrs: Dim[] = this.p.attributes.flatMap((a) => {
      const v = this.schema.variables.find((x) => x.id === a);
      if (!v) return [];
      const codes = this.p.codes[a];
      return [{ id: a, label: v.label, kind: "attribute" as const, cats: v.categories, code: (i: number) => codes[i] }];
    });
    return [...dps, age, ...attrs];
  }

  dim(id: string) { return this.dims().find((d) => d.id === id); }
  static colourable(d: Dim) { return d.kind === "datapoint" || d.cats.length <= SLOTS.length; }
  draftId() { return this.outcomes.has(DRAFT) ? `dp:${DRAFT}` : null; }

  private colours(d: Dim): RGB[] {
    return d.kind === "datapoint" ? [CASE, NO_CASE, BLANK] : d.cats.map((_, i) => SLOTS[i]);
  }

  /** Visible people (those passing every filter), their positions and colours, and counts per colour. */
  private compute(s: PeopleState) {
    const key = JSON.stringify(s);
    if (this.view?.key === key) return this.view;
    const n = this.p.count;
    const checks = s.filters.flatMap((f) => {
      const d = this.dim(f.by);
      if (!d) return [];
      const ok = new Set(f.values.map((v) => d.cats.findIndex((c) => c.id === v)));
      return [(i: number) => ok.has(d.code(i))];
    });
    const keep: number[] = [];
    for (let i = 0; i < n; i++) if (checks.every((c) => c(i))) keep.push(i);
    const idx = Uint32Array.from(keep);
    const d = this.dim(s.colorBy) ?? this.dims()[0];
    const pal = this.colours(d);
    const inFocus = s.focus ? this.matcher(s.focus) : null;
    const counts = new Array(d.cats.length + 1).fill(0);
    const pos = new Float64Array(idx.length * 3);
    const col = new Uint8Array(idx.length * 4);
    for (let j = 0; j < idx.length; j++) {
      const i = idx[j];
      pos.set(this.positions.subarray(i * 3, i * 3 + 3), j * 3);
      const c = d.code(i);
      const rgb = c >= 0 && c < pal.length ? pal[c] : BLANK;
      counts[c >= 0 && c < pal.length ? c : d.cats.length]++;
      // Outside a data point's universe (e.g. under 65 for dementia) stays faint, so cases stand out.
      const faint = (d.kind === "datapoint" && c === 2) || (inFocus !== null && !inFocus(i));
      col.set([...rgb, faint ? 45 : 225], j * 4);
    }
    this.view = { key, idx, pos, col, counts };
    return this.view;
  }

  /** onParcel: clicking a dot opens the parcel it lives on. */
  layer(zoom: number, s: PeopleState, onParcel?: (parcelId: string) => void): ScatterplotLayer | null {
    if (!s.show || zoom < MIN_ZOOM) return null;
    const v = this.compute(s);
    return new ScatterplotLayer({
      id: "people",
      data: { length: v.idx.length, attributes: {
        getPosition: { value: v.pos, size: 3 }, getFillColor: { value: v.col, size: 4 },
      } },
      billboard: true, radiusUnits: "pixels", getRadius: zoom >= 16 ? 3 : 2, stroked: false, pickable: true,
      onClick: (info) => { const i = this.person(info.index); if (i >= 0 && onParcel) onParcel(this.p.parcels[this.p.parcel[i]]); },
      updateTriggers: { getRadius: zoom >= 16 },
    });
  }

  /** Tests whether a person lives in an area. Block ids begin with their tract (11 digits)
   * and block group (12), so every level but parcel and city is a prefix of the block. */
  matcher(f: Focus): (i: number) => boolean {
    const p = this.p;
    if (f.level === "parcel") { const k = p.parcels.indexOf(f.id); return (i) => p.parcel[i] === k; }
    if (["block", "block_group", "tract"].includes(f.level)) {
      const ok = p.blocks.map((b) => b.startsWith(f.id));
      return (i) => ok[p.block[i]];
    }
    return () => f.level === "city";
  }

  inArea(f: Focus): number[] {
    const m = this.matcher(f), out: number[] = [];
    for (let i = 0; i < this.p.count; i++) if (m(i)) out.push(i);
    return out;
  }

  /** Columns of the people table and CSV: the person table, plus every data point (1 case, 0 not, blank outside). */
  private columns(): [string, (i: number) => string | number][] {
    const p = this.p, dims = this.dims();
    const cat = (id: string) => { const d = dims.find((x) => x.id === id); return (i: number) => { const c = d ? d.code(i) : -1; return c >= 0 ? d!.cats[c].id : ""; }; };
    return [
      ["person_id", (i) => i],
      ["household_id", (i) => (p.household[i] >= 0 ? p.household[i] : "")],
      ["parcel_id", (i) => p.parcels[p.parcel[i]]],
      ["block", (i) => p.blocks[p.block[i]]],
      ["AGE", (i) => p.age[i]],
      ["SEX", cat("sex")],
      ["race_eth", cat("race_eth")],
      ["education", cat("education")],
      ["lon", (i) => p.lon[i]],
      ["lat", (i) => p.lat[i]],
      ["location", (i) => (p.fallback[i] ? "parcel" : "address")],
      ...dims.filter((d) => d.kind === "datapoint").map((d) =>
        [d.id.slice(3), (i: number) => ["1", "0", ""][d.code(i)]] as [string, (i: number) => string]),
    ];
  }

  csv(idx: number[]): string {
    const cols = this.columns();
    const q = (v: string | number) => (typeof v === "string" && /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : String(v));
    return [cols.map(([h]) => h).join(","), ...idx.map((i) => cols.map(([, f]) => q(f(i))).join(","))].join("\n") + "\n";
  }

  table(idx: number[], limit: number): string {
    const cols = this.columns();
    const rows = idx.slice(0, limit).map((i) => `<tr>${cols.map(([, f]) => `<td>${esc(String(f(i)))}</td>`).join("")}</tr>`).join("");
    return `<div class="people-table"><table><thead><tr>${cols.map(([h]) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>
      ${idx.length > limit ? `<p class="hint">Showing ${limit.toLocaleString()} of ${idx.length.toLocaleString()} people. Download the CSV for all of them.</p>` : ""}`;
  }

  /** Counts from the people themselves (one realisation), for the detail panel. */
  summary(idx: number[]) {
    const p = this.p;
    const households = new Set(idx.map((i) => p.household[i]).filter((h) => h >= 0)).size;
    const ages = idx.map((i) => p.age[i]).filter((a) => a >= 0).sort((a, b) => a - b);
    const sex = this.dim("sex");
    const female = sex ? idx.filter((i) => sex.cats[sex.code(i)]?.id === "female").length : 0;
    const dps = this.dims().filter((d) => d.kind === "datapoint").map((d) => {
      const inU = idx.filter((i) => d.code(i) !== 2).length;
      return { label: d.label, cases: idx.filter((i) => d.code(i) === 0).length, universe: inU };
    });
    return { people: idx.length, households, medianAge: ages.length ? ages[Math.floor(ages.length / 2)] : null,
      female, groupQuarters: idx.filter((i) => p.household[i] < 0).length, dps };
  }

  /** Index into the full people arrays of a picked dot. */
  person(pickedIndex: number) { return this.view ? this.view.idx[pickedIndex] : -1; }

  tooltip(i: number): string {
    const dims = this.dims();
    const show = (id: string) => {
      const d = dims.find((x) => x.id === id);
      const c = d ? d.code(i) : -1;
      return d && c >= 0 ? d.cats[c].label : null;
    };
    const lines = [
      `<b>Synthetic person</b> · age ${this.p.age[i]}${show("sex") ? ", " + show("sex")!.toLowerCase() : ""}`,
      show("race_eth"), show("education") && `Education: ${show("education")}`,
      this.p.household[i] < 0 ? "Group quarters" : `Household of ${show("household_size") ?? "?"} · ${(show("tenure") ?? "").toLowerCase()}`,
      ...dims.filter((d) => d.kind === "datapoint").map((d) => `${d.label}: <b>${d.cats[d.code(i)].label.toLowerCase()}</b>`),
      `<i>parcel ${this.p.parcels[this.p.parcel[i]]} · one realisation (main run)</i>`,
    ];
    return lines.filter(Boolean).join("<br>");
  }

  legend(el: HTMLElement, s: PeopleState, zoom: number) {
    el.hidden = !s.show;
    if (!s.show) return;
    if (zoom < MIN_ZOOM) { el.innerHTML = `<div class="legend-title">People</div><div class="legend-note">Zoom in to blocks to see people.</div>`; return; }
    const v = this.compute(s);
    const d = this.dim(s.colorBy) ?? this.dims()[0];
    const pal = this.colours(d);
    const rows = d.cats.map((c, i) => v.counts[i] ? `<li><i style="background:rgb(${pal[i].join(",")})"></i>${c.label}<span>${v.counts[i].toLocaleString()}</span></li>` : "").join("")
      + (v.counts[d.cats.length] ? `<li><i style="background:rgb(${BLANK.join(",")})"></i>Not applicable<span>${v.counts[d.cats.length].toLocaleString()}</span></li>` : "");
    el.innerHTML = `<div class="legend-title">People · ${d.label}</div><ul class="ramp people-ramp">${rows}</ul>
      <div class="legend-note">${v.idx.length.toLocaleString()} of ${this.p.count.toLocaleString()} synthetic people${s.filters.length ? " match filters" : ""}</div>`;
  }
}

/** Lon, lat and a height for each person: people sharing an address point stack upward. */
function stack(p: People): Float64Array {
  const pos = new Float64Array(p.count * 3);
  const seen = new Map<string, number>();
  for (let i = 0; i < p.count; i++) {
    const k = `${p.lon[i]},${p.lat[i]}`;
    const level = seen.get(k) ?? 0;
    seen.set(k, level + 1);
    pos[i * 3] = p.lon[i]; pos[i * 3 + 1] = p.lat[i]; pos[i * 3 + 2] = level * FLOOR_M;
  }
  return pos;
}

/** 0 case, 1 not a case, 2 outside the universe, for every person (as synthpop/datapoints.py). */
function outcomes(p: People, schema: Schema, m: RiskModel): Uint8Array {
  const cats = (f: string) => schema.variables.find((v) => v.id === f)?.categories.map((c) => c.id) ?? [];
  const logOr = Object.entries(m.odds_ratios).map(([f, ors]) => [p.codes[f], cats(f).map((c) => Math.log(ors[c] ?? 1))] as const);
  const inside = Object.entries(m.universe).map(([f, allowed]) => [p.codes[f], cats(f).map((c) => allowed.includes(c))] as const);
  const base = Math.log(m.baseline / (1 - m.baseline));
  const out = new Uint8Array(p.count);
  for (let i = 0; i < p.count; i++) {
    if (!inside.every(([codes, ok]) => codes && codes[i] >= 0 && ok[codes[i]])) { out[i] = 2; continue; }
    let l = base;
    for (const [codes, lo] of logOr) if (codes && codes[i] >= 0) l += lo[codes[i]];
    out[i] = p.u[i] < 1 / (1 + Math.exp(-l)) ? 0 : 1;
  }
  return out;
}

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);

/** "Synthetic people here" in the detail panel: counts from the people in the clicked
 * area, a button to fly to them, and a toggle for the person table (with CSV). */
export function mountPeopleHere(el: HTMLElement, layer: PeopleLayer, f: Focus, onShow: () => void) {
  const idx = layer.inArea(f);
  const s = layer.summary(idx);
  const pct = (n: number, d: number) => (d ? `${Math.round((n / d) * 100)}%` : "–");
  el.className = "people-here";
  el.innerHTML = `
    <h3>Synthetic people here <span class="src">one run</span></h3>
    ${s.people ? `<div class="counts">
      <div class="count"><span>People</span><b>${s.people.toLocaleString()}</b></div>
      <div class="count"><span>Households</span><b>${s.households.toLocaleString()}</b></div>
      <div class="count"><span>Median age</span><b>${s.medianAge ?? "–"}</b></div>
      <div class="count"><span>Female</span><b>${pct(s.female, s.people)}</b></div>
      ${s.groupQuarters ? `<div class="count"><span>In group quarters</span><b>${s.groupQuarters.toLocaleString()}</b></div>` : ""}
      ${s.dps.map((d) => `<div class="count"><span>${esc(d.label)}</span><b>${d.cases.toLocaleString()} case${d.cases === 1 ? "" : "s"}</b> <span class="moe">of ${d.universe.toLocaleString()} (${pct(d.cases, d.universe)})</span></div>`).join("")}
    </div>
    <div class="row">
      <button class="ghost tiny" data-act="show">Show on map</button>
      <label class="check"><input type="checkbox" data-act="table"> People table</label>
      <button class="ghost tiny" data-act="csv">Download CSV</button>
    </div>
    <div class="table-slot" hidden></div>
    <p class="hint">Counted from one placement of the synthetic people, so they differ a little from the averages above.</p>`
      : `<p class="empty">No synthetic people placed here.</p>`}`;
  const slot = el.querySelector<HTMLElement>(".table-slot");
  el.querySelector<HTMLButtonElement>('[data-act="show"]')?.addEventListener("click", onShow);
  el.querySelector<HTMLInputElement>('[data-act="table"]')?.addEventListener("change", (e) => {
    const on = (e.target as HTMLInputElement).checked;
    slot!.hidden = !on;
    if (on && !slot!.innerHTML) slot!.innerHTML = layer.table(idx, 500);
  });
  el.querySelector<HTMLButtonElement>('[data-act="csv"]')?.addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([layer.csv(idx)], { type: "text/csv" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `people_${f.level}_${f.id.replace(/\W+/g, "_")}.csv` });
    a.click();
    URL.revokeObjectURL(url);
  });
}

/** The People section of the sidebar. Rebuilt whenever data points change. */
export function mountPeopleControls(el: HTMLElement, layer: PeopleLayer | null, s: PeopleState, onChange: () => void) {
  if (!layer) { el.innerHTML = `<h2>People</h2><p class="hint">Loading synthetic people…</p>`; return; }
  const dims = layer.dims();
  const dps = dims.filter((d) => d.kind === "datapoint");
  const attrs = dims.filter((d) => d.kind === "attribute");
  const opt = (d: Dim, sel: string) => `<option value="${d.id}" ${d.id === sel ? "selected" : ""}>${esc(d.label)}</option>`;
  const colourOpts = (sel: string) =>
    (dps.length ? `<optgroup label="Data points">${dps.map((d) => opt(d, sel)).join("")}</optgroup>` : "") +
    `<optgroup label="Attributes">${attrs.filter(PeopleLayer.colourable).map((d) => opt(d, sel)).join("")}</optgroup>`;
  const filterOpts = (sel: string) =>
    (dps.length ? `<optgroup label="Data points">${dps.map((d) => opt(d, sel)).join("")}</optgroup>` : "") +
    `<optgroup label="Attributes">${attrs.map((d) => opt(d, sel)).join("")}</optgroup>`;
  const catOpts = (id: string) => (layer.dim(id)?.cats ?? []).map((c) => `<option value="${c.id}">${esc(c.label)}</option>`).join("");
  const firstFilter = dps[0]?.id ?? "age_band";

  el.innerHTML = `
    <h2>People</h2>
    <label class="check"><input id="pp-show" type="checkbox" ${s.show ? "checked" : ""}> Show synthetic people</label>
    <p class="hint">${layer.p.count.toLocaleString()} people from one run, on their E911 address points. Zoom in to blocks to see them; apartment buildings stack upward.</p>
    <label>Colour people by <select id="pp-color">${colourOpts(s.colorBy)}</select></label>
    ${dps.length ? "" : `<p class="hint">Add a data point above and every person gets it as case / not a case.</p>`}
    <label>Show only people where <select id="pp-fdim">${filterOpts(firstFilter)}</select></label>
    <div class="row"><select id="pp-fcat" aria-label="Value">${catOpts(firstFilter)}</select><button id="pp-fadd">Add</button></div>
    <ul id="pp-filters" class="filters"></ul>`;

  const $ = <T extends HTMLElement>(id: string) => el.querySelector<T>(`#${id}`)!;
  el.querySelectorAll("input, select").forEach((x) => x.setAttribute("autocomplete", "off"));
  const renderFilters = () => {
    $("pp-filters").innerHTML = s.filters.map((f, i) => {
      const d = layer.dim(f.by);
      const vals = f.values.map((v) => d?.cats.find((c) => c.id === v)?.label ?? v).join(" or ");
      return `<li>${esc(d?.label ?? f.by)}: ${esc(vals)}<button data-i="${i}" class="ghost" aria-label="Remove filter">×</button></li>`;
    }).join("");
    $("pp-filters").querySelectorAll<HTMLButtonElement>("button").forEach((b) =>
      (b.onclick = () => { s.filters.splice(+b.dataset.i!, 1); renderFilters(); onChange(); }));
  };
  renderFilters();
  $<HTMLInputElement>("pp-show").onchange = (e) => { s.show = (e.target as HTMLInputElement).checked; onChange(); };
  $<HTMLSelectElement>("pp-color").onchange = (e) => { s.colorBy = (e.target as HTMLSelectElement).value; onChange(); };
  $<HTMLSelectElement>("pp-fdim").onchange = (e) => { $("pp-fcat").innerHTML = catOpts((e.target as HTMLSelectElement).value); };
  $("pp-fadd").onclick = () => {
    const by = $<HTMLSelectElement>("pp-fdim").value, value = $<HTMLSelectElement>("pp-fcat").value;
    const same = s.filters.find((f) => f.by === by);
    if (same) { if (!same.values.includes(value)) same.values.push(value); }
    else s.filters.push({ by, values: [value] });
    renderFilters();
    onChange();
  };
}
