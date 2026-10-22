import { search, type Filter, type Schema, type SearchHit } from "./api";
import type { ConfidenceMode } from "./mapLayer";

export type State = {
  variable: string;
  category: string;
  measure: "estimate" | "share";
  height: "none" | "population" | "households" | "housing_units" | "color";
  filters: Filter[];
  confidence: ConfidenceMode;
  extruded: boolean;
};

type Handlers = {
  onChange: () => void;
  onSearchPick: (hit: SearchHit) => void;
  onExport: (format: "csv" | "geojson") => void;
};

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);

/** Builds every control from /api/schema, so a new catalog variable shows up here with no code change. */
export function mountControls(el: HTMLElement, schema: Schema, state: State, h: Handlers) {
  const vars = schema.variables.filter((v) => v.available);
  const varOptions = (selected: string) =>
    vars.map((v) => `<option value="${v.id}" ${v.id === selected ? "selected" : ""}>${esc(v.label)}</option>`).join("");
  const catOptions = (variable: string, selected: string) =>
    (vars.find((v) => v.id === variable)?.categories ?? [])
      .map((c) => `<option value="${c.id}" ${c.id === selected ? "selected" : ""}>${esc(c.label)}</option>`).join("");

  el.innerHTML = `
    <h1>Portland, Maine<br><span>synthetic population</span></h1>
    <section class="search">
      <label for="q">Search an address, block group or tract</label>
      <input id="q" type="search" placeholder="e.g. 100 Congress St" autocomplete="off" />
      <ul id="hits" role="listbox"></ul>
    </section>
    <section>
      <h2>Colour by</h2>
      <label>Variable <select id="var">${varOptions(state.variable)}</select></label>
      <label>Category <select id="cat">${catOptions(state.variable, state.category)}</select></label>
      <div class="seg" role="radiogroup" aria-label="Measure">
        <label><input type="radio" name="measure" value="estimate" ${state.measure === "estimate" ? "checked" : ""}> Count</label>
        <label><input type="radio" name="measure" value="share" ${state.measure === "share" ? "checked" : ""}> Share</label>
      </div>
    </section>
    <section>
      <h2>3D</h2>
      <label class="check"><input id="extruded" type="checkbox" ${state.extruded ? "checked" : ""}> Extrude areas</label>
      <label>Height by <select id="height">
        <option value="population">People</option><option value="households">Households</option>
        <option value="housing_units">Housing units</option><option value="color">Selected category (count)</option>
        <option value="none">Flat</option></select></label>
    </section>
    <section>
      <h2>Filter</h2>
      <label>Show areas where <select id="fvar">${varOptions("age65_no_hs")}</select></label>
      <label>Category <select id="fcat">${catOptions("age65_no_hs", "yes")}</select></label>
      <div class="row">
        <select id="fmeasure" aria-label="Filter measure"><option value="share">share</option><option value="estimate">count</option></select>
        <select id="fop" aria-label="Comparison"><option>&gt;</option><option>&gt;=</option><option>&lt;</option><option>&lt;=</option></select>
        <input id="fval" type="number" value="10" step="any" aria-label="Threshold" /><span id="funit">%</span>
      </div>
      <div class="row"><button id="fadd">Add filter</button><button id="fclear" class="ghost">Clear</button></div>
      <ul id="filters" class="filters"></ul>
    </section>
    <section>
      <h2>Low-confidence areas</h2>
      <div class="seg" role="radiogroup" aria-label="Low confidence">
        ${(["show", "flag", "hide"] as const).map((m) => `<label><input type="radio" name="conf" value="${m}" ${state.confidence === m ? "checked" : ""}> ${m[0].toUpperCase() + m.slice(1)}</label>`).join("")}
      </div>
      <p class="hint">Confidence grades each value by its coefficient of variation: high ${schema.confidence.high}, medium ${schema.confidence.medium}, low ${schema.confidence.low}.</p>
    </section>
    <section>
      <h2>Export current level</h2>
      <div class="row"><button id="csv">CSV</button><button id="geojson">GeoJSON</button></div>
      <p class="hint">Exports the areas shown at this zoom, with filters applied.</p>
    </section>`;

  const $ = <T extends HTMLElement>(id: string) => el.querySelector<T>(`#${id}`)!;
  ($<HTMLSelectElement>("height")).value = state.height;

  const syncMeasure = () => {
    const isTotal = vars.find((v) => v.id === state.variable)?.kind === "total";
    el.querySelectorAll<HTMLInputElement>('input[name="measure"]').forEach((r) => {
      if (r.value === "share") r.disabled = isTotal;
      r.checked = r.value === (isTotal ? "estimate" : state.measure);
    });
    if (isTotal) state.measure = "estimate";
  };
  syncMeasure();

  $<HTMLSelectElement>("var").onchange = (e) => {
    state.variable = (e.target as HTMLSelectElement).value;
    state.category = vars.find((v) => v.id === state.variable)!.categories[0].id;
    $<HTMLSelectElement>("cat").innerHTML = catOptions(state.variable, state.category);
    if (vars.find((v) => v.id === state.variable)?.kind !== "total") state.measure = "share";
    syncMeasure();
    h.onChange();
  };
  $<HTMLSelectElement>("cat").onchange = (e) => { state.category = (e.target as HTMLSelectElement).value; h.onChange(); };
  el.querySelectorAll<HTMLInputElement>('input[name="measure"]').forEach((r) =>
    r.addEventListener("change", () => { state.measure = r.value as State["measure"]; h.onChange(); }));
  $<HTMLInputElement>("extruded").onchange = (e) => { state.extruded = (e.target as HTMLInputElement).checked; h.onChange(); };
  $<HTMLSelectElement>("height").onchange = (e) => { state.height = (e.target as HTMLSelectElement).value as State["height"]; h.onChange(); };
  el.querySelectorAll<HTMLInputElement>('input[name="conf"]').forEach((r) =>
    r.addEventListener("change", () => { state.confidence = r.value as ConfidenceMode; h.onChange(); }));

  // Filters
  $<HTMLSelectElement>("fvar").onchange = (e) => {
    const v = (e.target as HTMLSelectElement).value;
    $<HTMLSelectElement>("fcat").innerHTML = catOptions(v, vars.find((x) => x.id === v)!.categories[0].id);
  };
  $<HTMLSelectElement>("fmeasure").onchange = (e) => { $("funit").textContent = (e.target as HTMLSelectElement).value === "share" ? "%" : ""; };
  const renderFilters = () => {
    $("filters").innerHTML = state.filters.map((f, i) => {
      const v = vars.find((x) => x.id === f.variable);
      const c = v?.categories.find((x) => x.id === f.category);
      const val = f.measure === "share" ? `${+(f.value * 100).toFixed(2)}%` : `${f.value}`;
      return `<li>${esc(v?.label ?? f.variable)}: ${esc(c?.label ?? f.category)} ${f.measure} ${esc(f.op)} ${val}
        <button data-i="${i}" class="ghost" aria-label="Remove filter">×</button></li>`;
    }).join("");
    $("filters").querySelectorAll<HTMLButtonElement>("button").forEach((b) =>
      (b.onclick = () => { state.filters.splice(+b.dataset.i!, 1); renderFilters(); h.onChange(); }));
  };
  $("fadd").onclick = () => {
    const measure = $<HTMLSelectElement>("fmeasure").value as Filter["measure"];
    const raw = parseFloat($<HTMLInputElement>("fval").value);
    if (Number.isNaN(raw)) return;
    state.filters.push({
      variable: $<HTMLSelectElement>("fvar").value, category: $<HTMLSelectElement>("fcat").value,
      op: $<HTMLSelectElement>("fop").value as Filter["op"], value: measure === "share" ? raw / 100 : raw, measure,
    });
    renderFilters();
    h.onChange();
  };
  $("fclear").onclick = () => { state.filters = []; renderFilters(); h.onChange(); };

  // Search
  let timer = 0;
  const hits = $<HTMLUListElement>("hits");
  $<HTMLInputElement>("q").oninput = (e) => {
    clearTimeout(timer);
    const q = (e.target as HTMLInputElement).value;
    timer = window.setTimeout(async () => {
      if (q.trim().length < 2) { hits.innerHTML = ""; return; }
      const res = await search(q);
      hits.innerHTML = res.map((r, i) => `<li role="option" data-i="${i}" tabindex="0">${esc(r.label)}<span>${r.kind === "address" ? "address" : r.level.replace("_", " ")}</span></li>`).join("")
        || `<li class="none">No matches</li>`;
      hits.querySelectorAll<HTMLLIElement>("li[data-i]").forEach((li) => {
        const pick = () => { hits.innerHTML = ""; h.onSearchPick(res[+li.dataset.i!]); };
        li.onclick = pick;
        li.onkeydown = (ev) => { if (ev.key === "Enter") pick(); };
      });
    }, 200);
  };

  $("csv").onclick = () => h.onExport("csv");
  $("geojson").onclick = () => h.onExport("geojson");
}
