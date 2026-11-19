import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { MapboxOverlay } from "@deck.gl/mapbox";
import {
  deleteDatapoint, exportUrl, getDetail, getGeometry, getLayer, getPeople, getPeopleRuns, getSchema, previewDatapoint, previewDetail,
  saveDatapoint, type LayerValues, type People, type RiskModel, type Schema,
} from "./api";
import { mountControls, type State } from "./controls";
import { openEditor } from "./editor";
import { levelForZoom, levelLabel, zoomForLevel } from "./levels";
import { divergingBreaks, fmt, renderLegend, renderLevel } from "./mapLayer";
import { showDetail } from "./panel";
import { peopleLayers, peopleTooltip } from "./peopleLayers";
import { mountPeopleControls, mountPeopleHere, renderPeopleLegend } from "./peoplePanels";
import { MIN_ZOOM, PeopleView, type PeopleState } from "./peopleView";
import { Population } from "./population";
import { HouseholdCard } from "./householdCard";
import { Player } from "./player";
import { PersonTable } from "./personTable";

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
  // 3D on/off: extrudes areas (parcels as buildings) or lays them flat. Kept in step with
  // the sidebar's "3D buildings and areas" checkbox.
  const toggle3d = document.createElement("button");
  toggle3d.type = "button";
  toggle3d.className = "toggle-3d";
  toggle3d.textContent = "3D";
  const sync3d = () => {
    toggle3d.setAttribute("aria-pressed", String(state.extruded));
    toggle3d.title = state.extruded ? "Turn 3D buildings off" : "Turn 3D buildings on";
    const box = document.getElementById("extruded") as HTMLInputElement | null;
    if (box) box.checked = state.extruded;
  };
  toggle3d.onclick = () => { state.extruded = !state.extruded; sync3d(); refresh(); };
  map.addControl({
    onAdd: () => { const g = document.createElement("div"); g.className = "maplibregl-ctrl maplibregl-ctrl-group"; g.append(toggle3d); return g; },
    onRemove: () => {},
  }, "top-right");
  // Keep the canvas filling its box if the map was created while hidden or narrow.
  new ResizeObserver(() => map.resize()).observe(document.getElementById("map-wrap")!);
  const overlay = new MapboxOverlay({
    interleaved: false, layers: [],
    getTooltip: (info) => {
      const tip = view ? peopleTooltip(view, info) : undefined;
      if (tip !== undefined) return tip ? { html: tip, className: "tip" } : null;
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

  // People: every synthetic household as a circle on its parcel; clicking one shows its
  // people. Data points apply to them in the browser, so households recolour and filter as
  // soon as a data point is added or a slider moves.
  // colorBy starts empty, so people open coloured by the first saved data point (else age).
  const pstate: PeopleState = { show: true, colorBy: "", filters: [] };
  let view: PeopleView | null = null;
  const peopleSection = document.createElement("section");
  peopleSection.className = "people";
  const peopleLegend = document.getElementById("people-legend")!;
  let areaArgs: Parameters<typeof renderLevel> | null = null;
  let areaLayer: ReturnType<typeof renderLevel> | null = null;
  let areaFaded = false;
  const peopleOn = () => !!view && view.visibleAt(map.getZoom());
  // The simulation player swaps the people layer through every saved run; closing it goes back to main.
  let player: Player | null = null;
  let mainPeople: People | null = null;
  // The person table: clicking a row picks that person and flies to their household.
  let recentre = false;
  const personTable = new PersonTable(document.getElementById("person-table")!, (i) => {
    if (!view) return;
    pstate.show = true;
    recentre = true;
    view.pick(i);
  });
  // The household card: the open household's people.
  const card = new HouseholdCard(document.getElementById("household-card")!, (id) => openDetail("parcel", id));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") view?.close(); });

  /** When a household opens, zoom in until its circle is big enough to show its people
   * (and recentre on it after a pick from the person table). */
  let lastUnit = -1;
  function fitOpenHousehold() {
    const sel = view?.selection;
    const unit = sel?.unit ?? -1;
    if (!sel || (unit === lastUnit && !recentre)) { lastUnit = unit; recentre = false; return; }
    lastUnit = unit;
    const h = view!.pop.household(unit);
    const scale = 156543.03 * Math.cos((h.lat * Math.PI) / 180);       // metres per pixel at zoom 0
    if (recentre || h.r / (scale / 2 ** map.getZoom()) < 28) {
      const zoom = Math.min(20.5, Math.log2((scale * 60) / Math.max(h.r, 0.5)));
      map.flyTo({ center: [h.lon, h.lat], zoom: Math.max(map.getZoom(), zoom), speed: 1.4 });
    }
    recentre = false;
  }

  function draw() {
    const fade = peopleOn();
    if (areaArgs && (!areaLayer || fade !== areaFaded)) {
      const [level, geometry, color, extrude, opts] = areaArgs;
      areaLayer = renderLevel(level, geometry, color, extrude, { ...opts, fade });
      areaFaded = fade;
    }
    const pl = view ? peopleLayers(view, map.getZoom()) : [];
    overlay.setProps({ layers: [areaLayer, ...pl].filter(Boolean) });
    if (view) renderPeopleLegend(peopleLegend, view, map.getZoom());
    card.render(view, peopleOn());
    personTable.refresh();
  }

  function mountPeople() {
    const controls = document.getElementById("controls")!;
    const after = controls.querySelector(".datapoints") ?? controls.querySelector(".search");
    after?.after(peopleSection);
    mountPeopleControls(peopleSection, view, draw, () => view && personTable.show(view),
      player ? () => { pstate.show = true; pstate.anyZoom = true; player!.show(); draw(); } : undefined);
  }

  /** Colour people by a data point and drop filters on ones that no longer exist. */
  function syncPeople(colorBy?: string) {
    if (!view) return;
    const dims = view.pop.dims(), ids = new Set(dims.map((d) => d.id));
    pstate.filters = pstate.filters.filter((f) => ids.has(f.by));
    if (colorBy && ids.has(colorBy)) pstate.colorBy = colorBy;
    if (!ids.has(pstate.colorBy)) pstate.colorBy = dims.find((d) => d.kind === "datapoint")?.id ?? "age_band";
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
    if (editing) {
      const p = color as Awaited<ReturnType<typeof previewDatapoint>>;
      editing.editor.showResult(p);
      // A calibrated draft's tract offsets come from the server: give them to the people layer.
      if (p.model.offsets) { editing.model = { ...editing.model, offsets: p.model.offsets }; view?.pop.setDraft(editing.model); }
    }
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
    if (view && level !== "county" && level !== "state") {
      // The clicked area's own people: summary, table and CSV; people elsewhere fade on the map.
      const focus = { level, id };
      pstate.focus = level === "city" ? null : focus;
      const here = document.createElement("section");
      panel.querySelector("p.hint")?.after(here);
      const show = () => map.flyTo({ center: [d.area.lon, d.area.lat], zoom: Math.max(map.getZoom(), PEOPLE_ZOOM[level] ?? MIN_ZOOM), speed: 1.4 });
      mountPeopleHere(here, view.pop, focus, show);
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
        view?.pop.setDraft(m);
        draw();
        if (inFlight) again = true; else preview();
      },
      onSave: async (m) => {
        await saveDatapoint(m);
        schema = await getSchema();
        editing = null;
        panel.hidden = true;
        Object.assign(state, { variable: m.id, category: "yes", measure: "share" });
        for (const f of pstate.filters) if (f.by === view?.pop.draftId()) f.by = `dp:${m.id}`;
        view?.pop.setDraft(null);
        view?.pop.setSchema(schema);
        remountControls();
        syncPeople(`dp:${m.id}`);
        refresh();
      },
      onClose: () => {
        // Filters that followed the draft go back to the saved data point (or drop, if unsaved).
        const draft = view?.pop.draftId();
        if (saved) for (const f of pstate.filters) if (f.by === draft) f.by = `dp:${model.id}`;
        editing = null; panel.hidden = true; view?.pop.setDraft(null); syncPeople(); refresh();
      },
    });
    editing = { model, editor };
    view?.pop.setDraft(model);
    // While a saved data point is edited, filters on it follow the draft, so sliders change who shows.
    const draft = view?.pop.draftId();
    if (saved && draft) for (const f of pstate.filters) if (f.by === `dp:${model.id}`) f.by = draft;
    syncPeople(draft ?? undefined);
    refresh();
  }

  const blank = (): RiskModel => ({ id: "new_data_point", label: "New data point", baseline: 0.1, odds_ratios: {}, universe: {}, description: "", source: "" });

  const handlers = {
    onChange: () => { sync3d(); refresh(); },
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
      view?.pop.setSchema(schema);
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
  sync3d();
  getPeople().then((p) => {
    view = new PeopleView(new Population(p, "main", schema), pstate);
    view.onChange = () => { fitOpenHousehold(); draw(); };
    mainPeople = p;
    syncPeople();
    getPeopleRuns().then((runs) => {
      if (runs.length < 2) return;
      player = new Player(document.getElementById("player")!, runs, p,
        (frame, run) => view!.setRun(frame, run.name),
        () => { pstate.anyZoom = false; if (mainPeople) view!.setRun(mainPeople, "main"); else draw(); });
      mountPeople();
    }).catch(() => { /* no saved runs: the player stays hidden */ });
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
