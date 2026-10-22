import type { Level } from "./api";

/** The geography level shown at a zoom: city → tract → block group → block → parcel. */
export function levelForZoom(levels: Level[], zoom: number): Level {
  return levels.find((l) => zoom >= l.min_zoom && zoom < l.max_zoom) ?? levels[levels.length - 1];
}

/** A comfortable zoom to show one area of a level (used after search). */
export function zoomForLevel(levels: Level[], level: string): number {
  const l = levels.find((x) => x.id === level);
  if (!l) return 13;
  return level === "parcel" ? 17.5 : Math.min(l.max_zoom - 0.25, (l.min_zoom + l.max_zoom) / 2 + 0.5);
}

export const levelLabel = (levels: Level[], id: string) =>
  levels.find((l) => l.id === id)?.label ?? { county: "County", state: "State" }[id] ?? id;
