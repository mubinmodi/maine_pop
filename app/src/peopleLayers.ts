// The households on the map, as deck.gl layers: every household a circle on its parcel
// (circle area = people in it, layout from synthpop/households.py), and the open household
// drawn with its people inside as dots. Reads only the PeopleView.
import { ScatterplotLayer } from "@deck.gl/layers";
import { MIN_ZOOM, type PeopleView } from "./peopleView";
import { householdTooltip, personTooltip } from "./peoplePanels";

const LAYER = { households: "households", open: "household-open", members: "household-members" } as const;

/** The layers for this zoom (none when households are hidden). Clicking a circle opens its
 * household; clicking a person dot picks that person. */
export function peopleLayers(view: PeopleView, zoom: number): ScatterplotLayer[] {
  if (!view.visibleAt(zoom)) return [];
  const v = view.derived();
  const out: ScatterplotLayer[] = [new ScatterplotLayer({
    id: LAYER.households,
    data: { length: v.units.length, attributes: {
      getPosition: { value: v.upos, size: 2 }, getFillColor: { value: v.ucol, size: 4 }, getRadius: { value: v.urad, size: 1 },
    } },
    radiusUnits: "meters", radiusMinPixels: zoom >= MIN_ZOOM ? 1.5 : 1, billboard: false,
    stroked: zoom >= 15, getLineColor: [255, 255, 255, 230], lineWidthUnits: "pixels", getLineWidth: 0.75,
    pickable: true, autoHighlight: true, highlightColor: [20, 20, 20, 80],
    onClick: (info) => { if (info.index >= 0) view.open(v.units[info.index]); },
  })];
  const sel = view.selection;
  if (!sel) return out;
  const h = view.pop.household(sel.unit);
  out.push(new ScatterplotLayer({
    id: LAYER.open, data: [sel.unit], getPosition: [h.lon, h.lat], getRadius: h.r,
    radiusUnits: "meters", radiusMinPixels: 8, billboard: false,
    getFillColor: [255, 255, 255, 200], stroked: true, getLineColor: [24, 24, 24, 255], lineWidthUnits: "pixels", getLineWidth: 2,
  }));
  const lay = memberLayout(h.members.length, h.r);
  const cos = Math.cos((h.lat * Math.PI) / 180);
  const pos = lay.at.map(([dx, dy]) => [h.lon + dx / (111320 * cos), h.lat + dy / 111320] as [number, number]);
  out.push(new ScatterplotLayer<number>({
    id: LAYER.members, data: h.members,
    getPosition: (_i, { index }) => pos[index], getRadius: lay.dot,
    radiusUnits: "meters", radiusMinPixels: 2.5, billboard: false,
    getFillColor: (i) => view.memberFill(i), stroked: true, lineWidthUnits: "pixels",
    getLineColor: (i) => (i === sel.person ? [20, 20, 20, 255] : [255, 255, 255, 255]),
    getLineWidth: (i) => (i === sel.person ? 2.5 : 1),
    pickable: true, onClick: (info) => { if (typeof info.object === "number") view.pick(info.object); },
    updateTriggers: { getFillColor: view.key(), getLineColor: sel.person, getLineWidth: sel.person },
  }));
  return out;
}

/** The tooltip for a picked object on one of the people layers; undefined when it is not one of them. */
export function peopleTooltip(view: PeopleView, info: { layer?: { id: string } | null; index: number; object?: unknown }): string | null | undefined {
  switch (info.layer?.id) {
    case LAYER.households: return info.index >= 0 ? householdTooltip(view.pop, view.derived().units[info.index]) : null;
    case LAYER.members: return typeof info.object === "number" ? personTooltip(view.pop, info.object) : null;
    case LAYER.open: return null;
    default: return undefined;
  }
}

/** Where a household's people sit inside its circle (metres from the centre) and the dot
 * radius: one in the middle, up to seven in a ring (householder at the top), more in a
 * sunflower spiral. */
export function memberLayout(n: number, R: number): { at: [number, number][]; dot: number } {
  if (n <= 1) return { at: [[0, 0]], dot: R * 0.5 };
  if (n <= 7) {
    const rho = R * 0.56;
    return { dot: Math.min(R * 0.36, rho * Math.sin(Math.PI / n) * 0.85),
      at: Array.from({ length: n }, (_, k) => { const a = Math.PI / 2 - (2 * Math.PI * k) / n; return [rho * Math.cos(a), rho * Math.sin(a)]; }) };
  }
  // Scaled so the outermost dot (at spread·√((n-½)/n)) still ends inside the circle.
  const spread = (0.98 * R) / (Math.sqrt((n - 0.5) / n) + 0.42 * Math.sqrt(Math.PI / n));
  return { dot: 0.42 * spread * Math.sqrt(Math.PI / n),
    at: Array.from({ length: n }, (_, k) => { const rr = spread * Math.sqrt((k + 0.5) / n), a = Math.PI / 2 - k * 2.399963; return [rr * Math.cos(a), rr * Math.sin(a)]; }) };
}
