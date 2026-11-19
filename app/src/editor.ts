// The data-point editor: a risk model with live sliders. Every change calls onChange;
// the map and the result charts redraw from the preview it returns.
import type { Preview, RiskModel, Schema } from "./api";

const Z = 1.645;
const OR_MIN = 0.1, OR_MAX = 20;           // odds-ratio slider range, on a log scale
const P_MIN = 0.001, P_MAX = 0.6;          // baseline prevalence slider range, on a log scale
const STEPS = 1000;

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const pct = (v: number) => `${(v * 100).toFixed(v > 0 && v < 0.1 ? 1 : 0)}%`;
const toSlider = (v: number, lo: number, hi: number) => Math.round((Math.log(v / lo) / Math.log(hi / lo)) * STEPS);
const fromSlider = (x: number, lo: number, hi: number) => lo * Math.pow(hi / lo, x / STEPS);
export const slug = (s: string) => {
  const id = s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);
  return /^[a-z]/.test(id) ? id : `dp_${id}`.slice(0, 40);
};

export type EditorHandlers = {
  onChange: (m: RiskModel) => void;
  onSave: (m: RiskModel) => Promise<void>;
  onClose: () => void;
};

export function openEditor(el: HTMLElement, schema: Schema, initial: RiskModel, isSaved: boolean, h: EditorHandlers) {
  const model: RiskModel = JSON.parse(JSON.stringify(initial));
  const factors = schema.datapoints.factors;
  const ages = factors.find((f) => f.id === "age_group")?.categories ?? [];
  const lq = factors.find((f) => f.id === "living_quarters");
  let idEdited = isSaved;
  const calibration = initial.calibrate ?? "";     // kept, so the checkbox can turn it back on

  const minAge = () => {
    const u = model.universe.age_group;
    return u?.length ? ages.findIndex((a) => a.id === u[0]) : 0;
  };
  const lqMode = () => (model.universe.living_quarters?.length === 1 ? model.universe.living_quarters[0] : "all");

  const referenceText = () => {
    const parts = factors.filter((f) => Object.keys(model.odds_ratios[f.id] ?? {}).length).map((f) => {
      const refs = f.categories.filter((c) => (model.odds_ratios[f.id]?.[c.id] ?? 1) === 1).map((c) => c.label);
      return `${f.label.toLowerCase()}: ${refs.join(", ") || "none"}`;
    });
    return parts.length ? parts.join("; ") : "everyone (no odds ratios yet)";
  };

  const factorBlock = (f: Schema["datapoints"]["factors"][number]) => {
    const rows = f.categories.map((c) => {
      const v = model.odds_ratios[f.id]?.[c.id] ?? 1;
      return `<div class="or-row">
        <span class="or-label">${esc(c.label)}</span>
        <input type="range" min="0" max="${STEPS}" value="${toSlider(v, OR_MIN, OR_MAX)}" data-f="${f.id}" data-c="${c.id}" aria-label="Odds ratio for ${esc(c.label)}">
        <span class="or-val" data-v="${f.id}|${c.id}">×${v.toFixed(2)}</span></div>`;
    }).join("");
    const active = Object.values(model.odds_ratios[f.id] ?? {}).some((v) => v !== 1);
    return `<details class="factor" ${active ? "open" : ""}><summary>${esc(f.label)}
      <button class="ghost tiny" data-reset="${f.id}" title="Set every odds ratio to 1">reset</button></summary>${rows}</details>`;
  };

  el.innerHTML = `
    <header>
      <button class="close" aria-label="Close editor">×</button>
      <div class="kicker">Data point · risk model · <span id="dp-state">${isSaved ? "saved" : "not saved"}</span></div>
      <input id="dp-label" class="title-input" value="${esc(model.label)}" aria-label="Name" />
      <div class="row small"><label for="dp-id">ID</label><input id="dp-id" value="${esc(model.id)}" ${isSaved ? "disabled" : ""} /></div>
    </header>
    <section class="dp-result" id="dp-result"><p class="hint">Calculating…</p></section>
    <section>
      <h3>Who is counted</h3>
      <label>Ages <select id="dp-age">${ages.map((a, i) => `<option value="${i}" ${i === minAge() ? "selected" : ""}>${esc(a.label)} and over</option>`).join("")}</select></label>
      ${lq ? `<label>Living in <select id="dp-lq">
        <option value="all" ${lqMode() === "all" ? "selected" : ""}>households and group quarters</option>
        ${lq.categories.map((c) => `<option value="${c.id}" ${lqMode() === c.id ? "selected" : ""}>${esc(c.label.toLowerCase())} only</option>`).join("")}
      </select></label>` : ""}
    </section>
    <section>
      <h3>Baseline</h3>
      <div class="or-row wide">
        <span class="or-label">Prevalence for the reference person</span>
        <input id="dp-base" type="range" min="0" max="${STEPS}" value="${toSlider(model.baseline, P_MIN, P_MAX)}" aria-label="Baseline prevalence">
        <span class="or-val" id="dp-base-val">${pct(model.baseline)}</span>
      </div>
      <p class="hint" id="dp-ref">Reference person: ${esc(referenceText())}.</p>
      ${calibration ? `<label class="check"><input type="checkbox" id="dp-cal" ${model.calibrate ? "checked" : ""}>
        Match the CDC PLACES rate in every tract</label>
      <p class="hint">When on, each tract's expected adults with the condition equal PLACES' rate for that tract
        (adults 18+), so the baseline has no effect: the odds ratios only spread risk among people within a tract.
        PLACES' own uncertainty is added to the margins.</p>` : ""}
    </section>
    <section>
      <h3>Odds ratios</h3>
      <p class="hint">Drag right to raise risk, left to lower it. 1 means the same as the reference.</p>
      ${factors.map(factorBlock).join("")}
    </section>
    <section>
      <h3>Notes</h3>
      <textarea id="dp-desc" rows="2" placeholder="What this measures">${esc(model.description)}</textarea>
      <input id="dp-src" placeholder="Source (paper, survey, year)" value="${esc(model.source)}" />
    </section>
    <footer class="dp-footer">
      <button id="dp-save">${isSaved ? "Save changes" : "Save as variable"}</button>
      <button id="dp-close" class="ghost">Close</button>
      <span id="dp-msg" class="hint"></span>
    </footer>`;
  el.hidden = false;

  const $ = <T extends HTMLElement>(sel: string) => el.querySelector<T>(sel)!;
  // No debounce: the caller keeps one preview in flight and sends the latest model when it lands.
  const changed = () => {
    $("#dp-state").textContent = "not saved";
    $("#dp-ref").textContent = `Reference person: ${referenceText()}.`;
    h.onChange(JSON.parse(JSON.stringify(model)));
  };

  $<HTMLInputElement>("#dp-label").oninput = (e) => {
    model.label = (e.target as HTMLInputElement).value || "Untitled data point";
    if (!idEdited) { model.id = slug(model.label); $<HTMLInputElement>("#dp-id").value = model.id; }
    changed();
  };
  $<HTMLInputElement>("#dp-id").oninput = (e) => { idEdited = true; model.id = slug((e.target as HTMLInputElement).value); changed(); };
  $<HTMLSelectElement>("#dp-age").onchange = (e) => {
    const i = +(e.target as HTMLSelectElement).value;
    if (i === 0) delete model.universe.age_group; else model.universe.age_group = ages.slice(i).map((a) => a.id);
    changed();
  };
  el.querySelector<HTMLSelectElement>("#dp-lq")?.addEventListener("change", (e) => {
    const v = (e.target as HTMLSelectElement).value;
    if (v === "all") delete model.universe.living_quarters; else model.universe.living_quarters = [v];
    changed();
  });
  const syncCal = () => { $<HTMLInputElement>("#dp-base").disabled = !!model.calibrate; };
  el.querySelector<HTMLInputElement>("#dp-cal")?.addEventListener("change", (e) => {
    model.calibrate = (e.target as HTMLInputElement).checked ? calibration : "";
    if (!model.calibrate) delete model.offsets;
    syncCal();
    changed();
  });
  syncCal();
  $<HTMLInputElement>("#dp-base").oninput = (e) => {
    model.baseline = +fromSlider(+(e.target as HTMLInputElement).value, P_MIN, P_MAX).toPrecision(3);
    $("#dp-base-val").textContent = pct(model.baseline);
    changed();
  };
  el.querySelectorAll<HTMLInputElement>('input[type="range"][data-f]').forEach((r) => {
    r.oninput = () => {
      let v = fromSlider(+r.value, OR_MIN, OR_MAX);
      if (Math.abs(Math.log(v)) < 0.03) v = 1;          // snap to "no effect"
      v = +v.toPrecision(3);
      const f = r.dataset.f!, c = r.dataset.c!;
      model.odds_ratios[f] = { ...(model.odds_ratios[f] ?? {}), [c]: v };
      if (v === 1) delete model.odds_ratios[f][c];
      if (!Object.keys(model.odds_ratios[f]).length) delete model.odds_ratios[f];
      el.querySelector(`[data-v="${f}|${c}"]`)!.textContent = `×${v.toFixed(2)}`;
      changed();
    };
  });
  el.querySelectorAll<HTMLButtonElement>("button[data-reset]").forEach((b) => {
    b.onclick = (e) => {
      e.preventDefault();
      const f = b.dataset.reset!;
      delete model.odds_ratios[f];
      el.querySelectorAll<HTMLInputElement>(`input[data-f="${f}"]`).forEach((r) => {
        r.value = String(toSlider(1, OR_MIN, OR_MAX));
        el.querySelector(`[data-v="${f}|${r.dataset.c}"]`)!.textContent = "×1.00";
      });
      changed();
    };
  });
  $<HTMLTextAreaElement>("#dp-desc").oninput = (e) => { model.description = (e.target as HTMLTextAreaElement).value; };
  $<HTMLInputElement>("#dp-src").oninput = (e) => { model.source = (e.target as HTMLInputElement).value; };
  $("#dp-save").onclick = async () => {
    $("#dp-msg").textContent = "Saving…";
    try {
      await h.onSave(JSON.parse(JSON.stringify(model)));
    } catch (err) {
      $("#dp-msg").textContent = (err as Error).message;
    }
  };
  $(".close").onclick = h.onClose;
  $("#dp-close").onclick = h.onClose;

  return {
    model: () => JSON.parse(JSON.stringify(model)) as RiskModel,
    // The panel may be showing an area's detail instead of the editor: then skip.
    showResult(p: Preview) {
      const box = el.querySelector<HTMLElement>("#dp-result");
      if (box) renderResult(box, p, schema, model);
    },
    showError(msg: string) {
      const box = el.querySelector<HTMLElement>("#dp-result");
      if (box) box.innerHTML = `<p class="error">${esc(msg)}</p>`;
    },
  };
}

function renderResult(el: HTMLElement, p: Preview, schema: Schema, model: RiskModel) {
  const c = p.city;
  // "65–69" → "people 65+": the youngest counted band's lower bound.
  const first = model.universe.age_group?.length
    ? schema.datapoints.factors.find((f) => f.id === "age_group")!.categories.find((a) => a.id === model.universe.age_group[0])?.label
    : undefined;
  const who = first ? `people ${first.split("–")[0]}+` : "people";
  const charts = schema.datapoints.factors.map((f) => {
    const rows = (p.breakdown[f.id] ?? []).filter((r) => r.universe > 0);
    if (!rows.length) return "";
    const max = Math.max(...rows.map((r) => r.rate), 1e-9);
    const W = 340, label = 130, bar = W - label - 62, row = 17;
    const body = rows.map((r, i) => {
      const lab = f.categories.find((x) => x.id === r.category)?.label ?? r.category;
      const y = i * row + 2;
      return `<text x="0" y="${y + 11}" class="lbl">${esc(lab)}</text>
        <rect x="${label}" y="${y + 2}" width="${bar}" height="10" rx="2" class="track"/>
        <rect x="${label}" y="${y + 2}" width="${(r.rate / max) * bar}" height="10" rx="2" fill="#1d87b4"><title>${esc(lab)}: ${pct(r.rate)} (${Math.round(r.cases).toLocaleString()} expected)</title></rect>
        <text x="${label + bar + 6}" y="${y + 11}" class="val">${pct(r.rate)}</text>`;
    }).join("");
    return `<figure class="chart small"><figcaption>Rate by ${esc(f.label.toLowerCase())}</figcaption>
      <svg viewBox="0 0 ${W} ${rows.length * row + 4}" role="img" aria-label="Rate by ${esc(f.label)}">${body}</svg></figure>`;
  }).join("");
  el.innerHTML = `
    <div class="dp-headline">
      <div><b>${Math.round(c.estimate).toLocaleString()}</b><span class="moe"> ± ${Math.round(Z * c.se).toLocaleString()}</span>
        <div class="hint">expected in Portland</div></div>
      <div><b>${pct(c.share)}</b><span class="moe"> ± ${pct(Z * c.share_se)}</span>
        <div class="hint">of ${esc(who)}</div></div>
    </div>
    <p class="hint">90% margins from the synthetic population's placement and survey uncertainty${p.model.calibrate ? ", and CDC PLACES' own interval for each tract" : ""}. They don't include uncertainty in the odds ratios themselves.</p>
    ${p.model.offsets_error ? `<p class="hint warn">${esc(p.model.offsets_error)}</p>` : ""}
    ${charts}`;
}
