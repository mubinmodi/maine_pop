import type { Detail, Schema, Stat, Values } from "./api";
import { levelLabel } from "./levels";

const Z = 1.645; // 90% margins, matching how ACS publishes MOEs
const BLUE = "#2a78d6";
const ORANGE = "#eb6834";

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const pct = (v: number) => `${(v * 100).toFixed(v > 0 && v < 0.1 ? 1 : 0)}%`;
const num = (v: number) => (v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1));
const chip = (c?: string) => (c ? `<span class="chip chip-${c}" title="Confidence: ${c}">${c}</span>` : "");

// Key shares shown side by side with the comparison areas.
const COMPARE_ROWS: [string, string, string][] = [
  ["People 65+", "age65_plus", "yes"],
  ["65+ without a diploma", "age65_no_hs", "yes"],
  ["Less than high school (25+)", "education", "lt_hs"],
  ["Bachelor's or higher (25+)", "education", "ba_plus"],
  ["White, non-Hispanic", "race_eth", "white"],
  ["Black, non-Hispanic", "race_eth", "black"],
  ["Asian, non-Hispanic", "race_eth", "asian"],
  ["Hispanic", "race_eth", "hispanic"],
  ["Renter households", "tenure", "renter"],
  ["One-person households", "household_size", "p1"],
];

const ATTR_LABELS: Record<string, string> = {
  address: "Address", land_use: "Land use (from E911 address types)", housing_units: "Housing units (model)",
  address_points: "E911 address points", units_by_fallback: "Units assigned by fallback",
  group_quarters_site: "Likely group-quarters site", housing_units_2020: "Housing units (2020 Census)",
  population_2020: "Population (2020 Census)",
};

function countRow(label: string, s: Stat | undefined): string {
  if (!s) return `<div class="count"><span>${label}</span><b>0</b></div>`;
  return `<div class="count"><span>${label}</span><b>${num(s.estimate)}</b>
    <span class="moe">± ${num(Z * s.se)}</span>${chip(s.confidence)}</div>`;
}

function pyramid(values: Values, schema: Schema): string {
  const ages = schema.variables.find((v) => v.id === "age_group")!.categories;
  const as = values.age_sex ?? {};
  const get = (sex: string, a: string) => as[`${sex}_${a}`] ?? { estimate: 0, se: 0 } as Stat;
  const max = Math.max(1e-9, ...ages.flatMap((a) => ["male", "female"].map((s) => get(s, a.id).estimate + Z * get(s, a.id).se)));
  const W = 340, mid = 44, half = (W - mid) / 2 - 6, row = 13, H = ages.length * row + 26;
  const x = (v: number) => (Math.min(v, max) / max) * half;
  const rows = [...ages].reverse().map((a, i) => {
    const y = 18 + i * row;
    const m = get("male", a.id), f = get("female", a.id);
    const ml = half - x(m.estimate), fr = half + mid + 12;
    const whisker = (cx: number, v: Stat, dir: number) => {
      const lo = cx + dir * x(Math.max(0, v.estimate - Z * v.se)), hi = cx + dir * x(v.estimate + Z * v.se);
      return `<line x1="${lo}" x2="${hi}" y1="${y + 5}" y2="${y + 5}" class="whisker"/>`;
    };
    return `
      <rect x="${ml}" y="${y}" width="${x(m.estimate)}" height="10" rx="2" fill="${BLUE}"><title>Male ${a.label}: ${num(m.estimate)} ± ${num(Z * m.se)}</title></rect>
      ${whisker(half, m, -1)}
      <text x="${half + 6 + mid / 2}" y="${y + 9}" text-anchor="middle" class="axis">${a.label}</text>
      <rect x="${fr}" y="${y}" width="${x(f.estimate)}" height="10" rx="2" fill="${ORANGE}"><title>Female ${a.label}: ${num(f.estimate)} ± ${num(Z * f.se)}</title></rect>
      ${whisker(fr, f, 1)}`;
  }).join("");
  return `<figure class="chart">
    <figcaption>Age and sex <span class="legend-inline"><i style="background:${BLUE}"></i>Male <i style="background:${ORANGE}"></i>Female</span>
    <span class="hint">lines show 90% margins</span></figcaption>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Age-sex pyramid">${rows}</svg></figure>`;
}

function shareBars(title: string, variable: string, values: Values, schema: Schema): string {
  const v = schema.variables.find((x) => x.id === variable);
  const data = values[variable];
  if (!v || !data) return "";
  const W = 340, label = 150, bar = W - label - 70, row = 22;
  const rows = v.categories.map((c, i) => {
    const s = data[c.id] ?? ({ share: 0, share_se: 0 } as Stat);
    const y = i * row + 4;
    const lo = Math.max(0, s.share - Z * s.share_se), hi = Math.min(1, s.share + Z * s.share_se);
    return `<text x="0" y="${y + 12}" class="lbl">${esc(c.label)}</text>
      <rect x="${label}" y="${y + 2}" width="${bar}" height="12" rx="2" class="track"/>
      <rect x="${label}" y="${y + 2}" width="${Math.max(0, s.share * bar)}" height="12" rx="2" fill="${BLUE}"/>
      <line x1="${label + lo * bar}" x2="${label + hi * bar}" y1="${y + 8}" y2="${y + 8}" class="whisker"/>
      <text x="${label + bar + 6}" y="${y + 12}" class="val">${pct(s.share)} <tspan class="moe">±${pct(Z * s.share_se)}</tspan></text>`;
  }).join("");
  return `<figure class="chart"><figcaption>${title}</figcaption>
    <svg viewBox="0 0 ${W} ${v.categories.length * row + 6}" role="img" aria-label="${esc(title)}">${rows}</svg></figure>`;
}

function comparisonTable(d: Detail, schema: Schema): string {
  // One column per comparison level: published ACS where it exists, else synthetic.
  const byLevel = new Map<string, Detail["comparisons"][number]>();
  for (const c of d.comparisons) {
    const prev = byLevel.get(c.level);
    if (!prev || c.source === "published") byLevel.set(c.level, c);
  }
  const cols = [...byLevel.values()];
  // A variable with no row for a category means none were placed: 0%, not unknown.
  const cell = (vals: Values, v: string, cat: string) => {
    const s = vals[v]?.[cat];
    if (s) return `${pct(s.share)}<span class="moe"> ±${pct(Z * s.share_se)}</span>`;
    return vals[v] ? "0%" : "–";
  };
  const head = cols.map((c) => `<th>${esc(levelLabel(schema.levels, c.level))}<br><span class="src">${c.source === "published" ? "ACS" : "synthetic"}</span></th>`).join("");
  const rows = COMPARE_ROWS.map(([label, v, cat]) =>
    `<tr><th>${label}</th><td class="this">${cell(d.values, v, cat)}</td>${cols.map((c) => `<td>${cell(c.values, v, cat)}</td>`).join("")}</tr>`).join("");
  return `<h3>Compared with surrounding areas</h3>
    <div class="table-wrap"><table class="compare"><thead><tr><th></th><th>This area<br><span class="src">synthetic</span></th>${head}</tr></thead>
    <tbody>${rows}</tbody></table></div>
    <p class="hint">Shares with 90% margins. "ACS" columns are published Census estimates; county and state are not modelled.</p>`;
}

export function showDetail(el: HTMLElement, d: Detail, schema: Schema, onClose: () => void) {
  const a = d.area;
  const attrs = Object.entries(a.attrs ?? {})
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `<dt>${ATTR_LABELS[k] ?? k}</dt><dd>${typeof v === "boolean" ? (v ? "Yes" : "No") : esc(v)}</dd>`).join("");
  const people = d.values.population?.total;
  el.innerHTML = `
    <header>
      <button class="close" aria-label="Close">×</button>
      <div class="kicker">${esc(levelLabel(schema.levels, a.level))} · <code>${esc(a.area_id)}</code></div>
      <h2>${esc(a.name)}</h2>
      <div class="badge">Synthetic estimate</div>
      <div class="loc">${a.lat?.toFixed(5)}, ${a.lon?.toFixed(5)}</div>
    </header>
    ${attrs ? `<dl class="attrs">${attrs}</dl>` : ""}
    <h3>Synthetic households and people</h3>
    <div class="counts">
      ${countRow("Households", d.values.households?.total)}
      ${countRow("People", people)}
      ${d.values.population && d.values.living_quarters?.group_quarters ? countRow("In group quarters", d.values.living_quarters.group_quarters) : ""}
    </div>
    <p class="hint">Values are averages over 21 placements of the synthetic population, so a parcel can show 0.9 households. ± is the 90% margin.</p>
    ${people && people.estimate > 0 ? pyramid(d.values, schema) + shareBars("Race and ethnicity", "race_eth", d.values, schema)
      + shareBars("Educational attainment (25+)", "education", d.values, schema) : `<p class="empty">No synthetic residents placed here.</p>`}
    ${comparisonTable(d, schema)}
    <p class="disclosure-small">${esc(d.disclosure)}</p>`;
  el.hidden = false;
  el.querySelector<HTMLButtonElement>(".close")!.onclick = onClose;
}
