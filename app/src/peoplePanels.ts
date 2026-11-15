// The people's HTML: tooltips for a person and a household, the people legend, the
// "Synthetic people here" section of the detail panel, and the People section of the sidebar.
import type { Dim, Focus, Population } from "./population";
import { BLANK, PeopleView } from "./peopleView";

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const realisation = (pop: Population) => `one realisation (${pop.run === "main" ? "main run" : `run ${pop.run}`})`;

/** A household in words: title, size, tenure and family, cases per data point (HTML), parcel. */
export function describeHousehold(pop: Population, u: number) {
  const h = pop.household(u), m = h.members, i0 = m[0];
  const dps = pop.dims().filter((d) => d.kind === "datapoint").map((d) => {
    const cases = m.filter((i) => d.code(i) === 0).length, inU = m.filter((i) => d.code(i) !== 2).length;
    return `${esc(d.label)}: <b>${cases} case${cases === 1 ? "" : "s"}</b>${inU < m.length ? ` (of ${inU} in the model's universe)` : ""}`;
  });
  const gq = h.groupQuarters;
  return {
    title: gq ? "Group quarters" : `Household ${h.household}`,
    size: `${m.length} ${gq ? (m.length === 1 ? "resident" : "residents") : m.length === 1 ? "person" : "people"}`,
    facts: gq ? ["Dorm, nursing home, shelter or similar: not a household"]
      : [pop.label("tenure", i0), pop.label("building_type", i0), h.family !== null ? `Family ${h.family}` : "No family (no relative of the householder)"].filter(Boolean) as string[],
    dps, parcel: h.parcel, members: m,
  };
}

export function householdTooltip(pop: Population, u: number): string {
  const h = describeHousehold(pop, u), head = pop.person(h.members[0]);
  const sex = pop.label("sex", head.id);
  const who = head.household === null ? "" : `Householder: age ${head.age}${sex ? `, ${sex.toLowerCase()}` : ""}`;
  return [`<b>${h.title}</b> · ${h.size}`, h.facts.map(esc).join(" · "), who, ...h.dps, `Parcel ${esc(h.parcel)}`,
    `<i>click to see who lives here · ${realisation(pop)}</i>`].filter(Boolean).join("<br>");
}

export function personTooltip(pop: Population, i: number): string {
  const p = pop.person(i), show = (id: string) => pop.label(id, i);
  const lines = [
    `<b>Synthetic person</b> · age ${p.age}${show("sex") ? ", " + show("sex")!.toLowerCase() : ""}`,
    show("race_eth"), show("education") && `Education: ${show("education")}`,
    show("employment") && `Employment: ${show("employment")!.toLowerCase()}`,
    show("poverty") && p.povertyRatio !== null && `Income: ${p.povertyRatio >= 501 ? "over 500" : p.povertyRatio}% of the poverty line (${show("poverty")!.toLowerCase()})`,
    p.household === null ? p.role : `${p.role} in household ${p.household} · ${(show("tenure") ?? "").toLowerCase()}`,
    ...pop.dims().filter((d) => d.kind === "datapoint").map((d) => `${esc(d.label)}: <b>${d.cats[d.code(i)].label.toLowerCase()}</b>`),
    `Person ${i} · ${p.family !== null ? `family ${p.family}` : "not in a family"} · parcel ${esc(p.parcel)}`,
    `<i>${realisation(pop)}</i>`,
  ];
  return lines.filter(Boolean).join("<br>");
}

export function renderPeopleLegend(el: HTMLElement, view: PeopleView, zoom: number) {
  const s = view.state, pop = view.pop;
  el.hidden = !s.show;
  if (!s.show) return;
  if (!view.visibleAt(zoom)) { el.innerHTML = `<div class="legend-title">Households</div><div class="legend-note">Zoom in to blocks to see households.</div>`; return; }
  const v = view.derived(), d = v.dim;
  const rows = d.cats.map((c, i) => v.counts[i] ? `<li><i style="background:rgb(${v.palette[i].join(",")})"></i>${esc(c.label)}<span>${v.counts[i].toLocaleString()}</span></li>` : "").join("")
    + (v.counts[d.cats.length] ? `<li><i style="background:rgb(${BLANK.join(",")})"></i>Not applicable<span>${v.counts[d.cats.length].toLocaleString()}</span></li>` : "");
  const how = d.kind === "datapoint" ? "a case when anyone in it is" : "by the householder";
  const gq = v.units.length - v.households;
  el.innerHTML = `<div class="legend-title">Households · ${esc(d.label)}</div>
    <div class="legend-note hh-key"><svg width="44" height="18" aria-hidden="true"><circle cx="7" cy="11" r="4"/><circle cx="29" cy="9" r="8"/></svg>Circle area = people in the household. Coloured ${how}; counts are people.</div>
    <ul class="ramp people-ramp">${rows}</ul>
    <div class="legend-note">${v.idx.length.toLocaleString()} of ${pop.count.toLocaleString()} synthetic people${s.filters.length ? " match filters" : ""}, in ${v.households.toLocaleString()} households${gq ? ` and ${gq.toLocaleString()} group quarters` : ""}${pop.run === "main" ? "" : ` · run ${pop.run}`}. Click a household to see its people.</div>`;
}

/** Up to `limit` people as an HTML table with the CSV's columns. */
export function peopleTableHtml(pop: Population, idx: number[], limit: number): string {
  const cols = pop.columns();
  const rows = idx.slice(0, limit).map((i) => `<tr>${cols.map(([, f]) => `<td>${esc(String(f(i)))}</td>`).join("")}</tr>`).join("");
  return `<div class="people-table"><table><thead><tr>${cols.map(([h]) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>
    ${idx.length > limit ? `<p class="hint">Showing ${limit.toLocaleString()} of ${idx.length.toLocaleString()} people. Download the CSV for all of them.</p>` : ""}`;
}

/** "Synthetic people here" in the detail panel: counts from the people in the clicked
 * area, a button to fly to them, and a toggle for the person table (with CSV). */
export function mountPeopleHere(el: HTMLElement, pop: Population, f: Focus, onShow: () => void) {
  const idx = pop.inArea(f);
  const s = pop.summary(idx);
  const pct = (n: number, d: number) => (d ? `${Math.round((n / d) * 100)}%` : "–");
  el.className = "people-here";
  el.innerHTML = `
    <h3>Synthetic people here <span class="src">one run</span></h3>
    ${s.people ? `<div class="counts">
      <div class="count"><span>People</span><b>${s.people.toLocaleString()}</b></div>
      <div class="count"><span>Households</span><b>${s.households.toLocaleString()}</b></div>
      <div class="count"><span>Families</span><b>${s.families.toLocaleString()}</b></div>
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
    if (on && !slot!.innerHTML) slot!.innerHTML = peopleTableHtml(pop, idx, 500);
  });
  el.querySelector<HTMLButtonElement>('[data-act="csv"]')?.addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([pop.csv(idx)], { type: "text/csv" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `people_${f.level}_${f.id.replace(/\W+/g, "_")}.csv` });
    a.click();
    URL.revokeObjectURL(url);
  });
}

/** The People section of the sidebar. Rebuilt whenever data points change. */
export function mountPeopleControls(el: HTMLElement, view: PeopleView | null, onChange: () => void,
                                    onTable?: () => void, onPlayer?: () => void) {
  if (!view) { el.innerHTML = `<h2>People</h2><p class="hint">Loading synthetic people…</p>`; return; }
  const pop = view.pop, s = view.state;
  const dims = pop.dims();
  const dps = dims.filter((d) => d.kind === "datapoint");
  const attrs = dims.filter((d) => d.kind === "attribute");
  const opt = (d: Dim, sel: string) => `<option value="${d.id}" ${d.id === sel ? "selected" : ""}>${esc(d.label)}</option>`;
  const colourOpts = (sel: string) =>
    (dps.length ? `<optgroup label="Data points">${dps.map((d) => opt(d, sel)).join("")}</optgroup>` : "") +
    `<optgroup label="Attributes">${attrs.filter(PeopleView.colourable).map((d) => opt(d, sel)).join("")}</optgroup>`;
  const filterOpts = (sel: string) =>
    (dps.length ? `<optgroup label="Data points">${dps.map((d) => opt(d, sel)).join("")}</optgroup>` : "") +
    `<optgroup label="Attributes">${attrs.map((d) => opt(d, sel)).join("")}</optgroup>`;
  const catOpts = (id: string) => (pop.dim(id)?.cats ?? []).map((c) => `<option value="${c.id}">${esc(c.label)}</option>`).join("");
  const firstFilter = dps[0]?.id ?? "age_band";

  el.innerHTML = `
    <h2>People</h2>
    <label class="check"><input id="pp-show" type="checkbox" ${s.show ? "checked" : ""}> Show synthetic households</label>
    <p class="hint">${pop.count.toLocaleString()} people from one run, as household circles spread over their parcel (circle area = household size). Zoom in to blocks to see them, and click a household to see its people.</p>
    <div class="pp-buttons"><button id="pp-table" class="ghost">Person table (one run)</button>
      <button id="pp-player" class="ghost" ${onPlayer ? "" : "hidden"}>▶ Play all simulations</button></div>
    <label>Colour people by <select id="pp-color">${colourOpts(s.colorBy)}</select></label>
    ${dps.length ? "" : `<p class="hint">Add a data point above and every person gets it as case / not a case.</p>`}
    <label>Show only people where <select id="pp-fdim">${filterOpts(firstFilter)}</select></label>
    <div class="row"><select id="pp-fcat" aria-label="Value">${catOpts(firstFilter)}</select><button id="pp-fadd">Add</button></div>
    <ul id="pp-filters" class="filters"></ul>`;

  const $ = <T extends HTMLElement>(id: string) => el.querySelector<T>(`#${id}`)!;
  el.querySelectorAll("input, select").forEach((x) => x.setAttribute("autocomplete", "off"));
  const renderFilters = () => {
    $("pp-filters").innerHTML = s.filters.map((f, i) => {
      const d = pop.dim(f.by);
      const vals = f.values.map((v) => d?.cats.find((c) => c.id === v)?.label ?? v).join(" or ");
      return `<li>${esc(d?.label ?? f.by)}: ${esc(vals)}<button data-i="${i}" class="ghost" aria-label="Remove filter">×</button></li>`;
    }).join("");
    $("pp-filters").querySelectorAll<HTMLButtonElement>("button").forEach((b) =>
      (b.onclick = () => { s.filters.splice(+b.dataset.i!, 1); renderFilters(); onChange(); }));
  };
  renderFilters();
  $<HTMLButtonElement>("pp-table").onclick = () => onTable?.();
  $<HTMLButtonElement>("pp-player").onclick = () => onPlayer?.();
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
