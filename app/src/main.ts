import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { MapboxOverlay } from "@deck.gl/mapbox";
import {
  deleteDatapoint, exportUrl, getDetail, getGeometry, getLayer, getPeople, getSchema, previewDatapoint, previewDetail, saveDatapoint,
  type LayerValues, type RiskModel, type Schema,
} from "./api";
import { mountControls, type State } from "./controls";
import { openEditor } from "./editor";
import { levelForZoom, levelLabel, zoomForLevel } from "./levels";
import { divergingBreaks, fmt, renderLegend, renderLevel } from "./mapLayer";
import { showDetail } from "./panel";
import { MIN_ZOOM, mountPeopleControls, mountPeopleHere, PeopleLayer, type PeopleState } from "./people";

// Zoom that frames an area's people when it is clicked.
const PEOPLE_ZOOM: Record<string, number> = { tract: 14.5, block_group: 15, block: 16, parcel: 17.5 };

const BASEMAP = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";

async function start() {
  let schema: Schema = await getSchema();
  document.getElementById("disclosure-text")!.textContent = schema.disclosure;

  const state: State = {
    variable: "population", category: "total", measure: "estimate", scheme: "green_blue", height: "population",
    filters: [], confidence: "flag", extruded: true,
  };
  let selected: string | null = null;
  let current = { level: "", color: null as LayerValues | null };
  // While a data point is being edited, the map colours by its live preview.
  let editing: { model: RiskModel; editor: ReturnType<typeof openEditor> } | null = null;

  const map = new maplibregl.Map({
    container: "map", style: BASEMAP, center: [-70.27, 43.665], zoom: 12, pitch: 45, bearing: -15,
    maxBounds: [[-70.6, 43.5], [-69.9, 43.85]], attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
  // Keep the canvas filling its box if the map was created while hidden or narrow.
  new ResizeObserver(() => map.resize()).observe(document.getElementById("map-wrap")!);
  const overlay = new MapboxOverlay({
    interleaved: false, layers: [],
    getTooltip: (info) => {
      if (info.layer?.id === "people") {
        const i = people && info.index >= 0 ? people.person(info.index) : -1;
        return i >= 0 ? { html: people!.tooltip(i), className: "tip" } : null;
      }
      const object = info.object;
      const id = object?.properties?.id as string | undefined;
      const v = id && current.color?.values[id];
      if (!id) return null;
      const label = editing ? { label: editing.model.label, kind: "total" } : schema.variables.find((x) => x.id === state.variable);
      const cat = editing ? "" : label && "categories" in label ? label.categories.find((c) => c.id === state.category)?.label ?? "" : "";
      return {
        html: `<b>${levelLabel(schema.levels, current.level)}</b> ${id}<br>${label?.label}${label?.kind === "total" || label?.kind === "datapoint" ? "" : ": " + cat}<br>` +
          (v ? `${fmt(v[0], state.measure)} ± ${fmt(1.645 * v[1], state.measure)} · ${v[2]} confidence` : "none placed") +
          `<br><i>synthetic estimate · click for detail</i>`,
        className: "tip",
      };
    },
  });
  map.addControl(overlay as unknown as maplibregl.IControl);

  const status = document.getElementById("status")!;
  const legend = document.getElementById("legend")!;
  const panel = document.getElementById("panel")!;
  let seq = 0;
  let cityRate: number | null = null;

  // People: every synthetic person as a dot. Data points apply to them in the browser,
  // so the dots recolour and filter as soon as a data point is added or a slider moves.
  // colorBy starts empty, so people open coloured by the first saved data point (else age).
  const pstate: PeopleState = { show: true, colorBy: "", filters: [] };
  let people: PeopleLayer | null = null;
  const peopleSection = document.createElement("section");
  peopleSection.className = "people";
  const peopleLegend = document.getElementById("people-legend")!;
  let areaArgs: Parameters<typeof renderLevel> | null = null;
  let areaLayer: ReturnType<typeof renderLevel> | null = null;
  let areaFaded = false;
  const peopleOn = () => !!people && pstate.show && map.getZoom() >= MIN_ZOOM;

  function draw() {
    const fade = peopleOn();
    if (areaArgs && (!areaLayer || fade !== areaFaded)) {
      const [level, geometry, color, extrude, opts] = areaArgs;
      areaLayer = renderLevel(level, geometry, color, extrude, { ...opts, fade });
      areaFaded = fade;
    }
    const pl = people?.layer(map.getZoom(), pstate, (parcel) => openDetail("parcel", parcel));
    overlay.setProps({ layers: [areaLayer, pl].filter(Boolean) });
    people?.legend(peopleLegend, pstate, map.getZoom());
  }

  function mountPeople() {
    const controls = document.getElementById("controls")!;
    const after = controls.querySelector(".datapoints") ?? controls.querySelector(".search");
    after?.after(peopleSection);
    mountPeopleControls(peopleSection, people, pstate, draw);
  }

  /** Colour people by a data point and drop filters on ones that no longer exist. */
  function syncPeople(colorBy?: string) {
    if (!people) return;
    const ids = new Set(people.dims().map((d) => d.id));
    pstate.filters = pstate.filters.filter((f) => ids.has(f.by));
    if (colorBy && ids.has(colorBy)) pstate.colorBy = colorBy;
    if (!ids.has(pstate.colorBy)) pstate.colorBy = people.dims().find((d) => d.kind === "datapoint")?.id ?? "age_band";
    mountPeople();
    draw();
  }

  async function refresh() {
    const my = ++seq;
    const level = levelForZoom(schema.levels, map.getZoom()).id;
    status.textContent = `Loading ${levelLabel(schema.levels, level).toLowerCase()}s…`;
    const heightVar = state.height === "color" ? state.variable : state.height;
    const heightCat = state.height === "color" ? state.category : "total";
    const measure = editing ? "share" : state.measure;     // a draft data point is shown as its rate
    const vsCity = state.scheme === "vs_city" && measure === "share";
    const colorReq = editing
      ? previewDatapoint(editing.model, level, measure, state.filters)
      : getLayer(level, state.variable, state.category, state.measure, state.filters);
    const [geometry, color, extrude, city] = await Promise.all([
      getGeometry(level),
      colorReq,
      state.height === "none" ? Promise.resolve(null) : getLayer(level, heightVar, heightCat, "estimate", []),
      vsCity && !editing ? getLayer("city", state.variable, state.category, "share", []) : Promise.resolve(null),
    ]).catch((e) => { editing?.editor.showError(e.message); status.textContent = e.message; throw e; });
    if (vsCity && editing) {            // compare with the Portland-wide rate of the draft
      const p = color as Awaited<ReturnType<typeof previewDatapoint>>;
      cityRate = p.city.share;
    } else cityRate = null;
    if (my !== seq) return;                       // a newer refresh started meanwhile
    if (editing) editing.editor.showResult(color as Awaited<ReturnType<typeof previewDatapoint>>);
    current = { level, color };
    const scheme = vsCity ? "vs_city" : state.scheme === "vs_city" ? "green_blue" : state.scheme;
    const reference = city ? Object.values(city.values)[0]?.[0] ?? 0 : cityRate;
    const breaks = reference !== null
      ? divergingBreaks(Object.values(color.values).filter((v) => v[0] > 0).map((v) => v[0] - reference))
      : color.breaks;
    areaArgs = [level, geometry, color, extrude, {
      extruded: state.extruded, confidence: state.confidence, selected, scheme, reference, breaks,
      onClick: (id) => openDetail(level, id),
    }];
    areaLayer = null;
    draw();
    const v = schema.variables.find((x) => x.id === state.variable)!;
    const title = editing
      ? `${editing.model.label} (draft) ${measure === "share" ? "(rate)" : "(expected cases)"}`
      : `${v.label}${v.kind === "total" || v.kind === "datapoint" ? "" : " · " + v.categories.find((c) => c.id === state.category)?.label}` +
        (state.measure === "share" ? (v.kind === "datapoint" ? " (rate)" : " (share)") : v.kind === "datapoint" ? " (expected cases)" : " (count)");
    const heightLabel = state.height === "none" || !state.extruded ? null
      : state.height === "color" ? "selected category" : { population: "people", households: "households", housing_units: "housing units" }[state.height];
    renderLegend(legend, title, color as LayerValues, state.confidence, heightLabel, scheme, breaks, reference);
    const n = Object.keys(color.values).length;
    status.textContent = `${levelLabel(schema.levels, level)} level · ${n.toLocaleString()} areas` +
      (color.filtered_count !== null ? ` · ${color.filtered_count.toLocaleString()} match filters` : "") +
      ` · zoom ${map.getZoom().toFixed(1)}`;
  }

  async function openDetail(level: string, id: string) {
    selected = id;
    const draft = editing?.model ?? null;
    const d = draft ? await previewDetail(level, id, draft) : await getDetail(level, id);
    showDetail(panel, d, schema, () => {
      selected = null;
      pstate.focus = null;
      if (draft) startEditing(draft, isSavedId(draft.id)); else { panel.hidden = true; refresh(); }
    }, draft ? "Back to editor" : undefined);
    if (people && level !== "county" && level !== "state") {
      // The clicked area's own people: summary, table and CSV; people elsewhere fade on the map.
      const focus = { level, id };
      pstate.focus = level === "city" ? null : focus;
      const here = document.createElement("section");
      panel.querySelector("p.hint")?.after(here);
      const show = () => map.flyTo({ center: [d.area.lon, d.area.lat], zoom: Math.max(map.getZoom(), PEOPLE_ZOOM[level] ?? MIN_ZOOM), speed: 1.4 });
      mountPeopleHere(here, people, focus, show);
      // Clicking a structure while zoomed out goes straight to its people.
      if (pstate.show && map.getZoom() < MIN_ZOOM && level !== "city") show();
    }
    refresh();
  }

  const isSavedId = (id: string) => schema.variables.some((v) => v.kind === "datapoint" && v.id === id);

  function startEditing(model: RiskModel, saved: boolean) {
    // While a slider moves, keep one preview in flight; when it lands, send the latest model.
    let inFlight = false, again = false;
    const preview = () => {
      inFlight = true;
      refresh().catch(() => {}).then(() => {
        if (again) { again = false; preview(); } else inFlight = false;
      });
    };
    const editor = openEditor(panel, schema, model, saved, {
      onChange: (m) => {
        if (editing) editing.model = m;
        people?.setDraft(m);
        draw();
        if (inFlight) again = true; else preview();
      },
      onSave: async (m) => {
        await saveDatapoint(m);
        schema = await getSchema();
        editing = null;
        panel.hidden = true;
        Object.assign(state, { variable: m.id, category: "yes", measure: "share" });
        for (const f of pstate.filters) if (f.by === people?.draftId()) f.by = `dp:${m.id}`;
        people?.setDraft(null);
        people?.setSchema(schema);
        remountControls();
        syncPeople(`dp:${m.id}`);
        refresh();
      },
      onClose: () => {
        // Filters that followed the draft go back to the saved data point (or drop, if unsaved).
        const draft = people?.draftId();
        if (saved) for (const f of pstate.filters) if (f.by === draft) f.by = `dp:${model.id}`;
        editing = null; panel.hidden = true; people?.setDraft(null); syncPeople(); refresh();
      },
    });
    editing = { model, editor };
    people?.setDraft(model);
    // While a saved data point is edited, filters on it follow the draft, so sliders change who shows.
    const draft = people?.draftId();
    if (saved && draft) for (const f of pstate.filters) if (f.by === `dp:${model.id}`) f.by = draft;
    syncPeople(draft ?? undefined);
    refresh();
  }

  const blank = (): RiskModel => ({ id: "new_data_point", label: "New data point", baseline: 0.1, odds_ratios: {}, universe: {}, description: "", source: "" });

  const handlers = {
    onChange: refresh,
    onNewDatapoint: (preset: RiskModel | null) => {
      const m = preset ? { ...JSON.parse(JSON.stringify(preset)) } : blank();
      if (isSavedId(m.id)) m.id = `${m.id}_2`;
      startEditing(m, false);
    },
    onEditDatapoint: (id: string) => {
      const v = schema.variables.find((x) => x.id === id && x.kind === "datapoint");
      if (v?.model) startEditing(v.model, true);
    },
    onDeleteDatapoint: async (id: string) => {
      const v = schema.variables.find((x) => x.id === id);
      if (!window.confirm(`Remove the data point "${v?.label ?? id}"? It is deleted from config/datapoints.yaml.`)) return;
      await deleteDatapoint(id);
      schema = await getSchema();
      people?.setSchema(schema);
      if (state.variable === id) Object.assign(state, { variable: "population", category: "total", measure: "estimate" });
      remountControls();
      syncPeople();
      refresh();
    },
    onSearchPick: (hit: { lon: number; lat: number; level: string; area_id: string }) => {
      map.flyTo({ center: [hit.lon, hit.lat], zoom: zoomForLevel(schema.levels, hit.level), speed: 1.6 });
      if (["county", "state"].includes(hit.level)) return;
      map.once("moveend", () => openDetail(hit.level, hit.area_id));
    },
    onExport: (format: "csv" | "geojson") => { window.location.href = exportUrl(current.level, format, state.filters); },
  };
  const remountControls = () => { mountControls(document.getElementById("controls")!, schema, state, handlers); mountPeople(); };
  remountControls();
  getPeople().then((p) => {
    people = new PeopleLayer(p, schema);
    syncPeople();
  }).catch((e) => { peopleSection.innerHTML = `<h2>People</h2><p class="hint">People are unavailable: ${e.message}</p>`; });

  let lastLevel = "";
  map.on("load", refresh);
  map.on("zoomend", () => {
    const level = levelForZoom(schema.levels, map.getZoom()).id;
    if (level !== lastLevel) { lastLevel = level; refresh(); }
    else {
      status.textContent = status.textContent!.replace(/zoom [\d.]+/, `zoom ${map.getZoom().toFixed(1)}`);
      draw();
    }
  });
}

start().catch((e) => {
  document.getElementById("status")!.textContent = `Could not load: ${e.message}. Is the API running (uv run synthpop serve)?`;
});
