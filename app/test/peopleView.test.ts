import { describe, expect, it, vi } from "vitest";
import { memberLayout } from "../src/peopleLayers";
import { MIN_ZOOM, PeopleView, type PeopleState } from "../src/peopleView";
import { Population } from "../src/population";
import { people, schema } from "./fixture";

const make = (s: Partial<PeopleState> = {}) =>
  new PeopleView(new Population(people(), "main", schema()), { show: true, colorBy: "age_band", filters: [], ...s });
const rgba = (c: Uint8Array, k: number) => Array.from(c.subarray(k * 4, k * 4 + 4));

describe("PeopleView", () => {
  it("colours a household by its householder", () => {
    const v = make(), d = v.derived();
    expect(Array.from(d.units)).toEqual([0, 1, 2, 3]);
    expect(rgba(d.ucol, 0)).toEqual(rgba(d.col, 0));                 // householder aged 40, not the child
    expect(v.colour(0)).not.toBe(v.colour(1));
    expect(d.households).toBe(3);                                    // the group-quarters circle is not a household
    expect(d.counts).toEqual([1, 3, 1, 3, 0]);                       // people per age band, then blank
  });

  it("with a data point, a household is a case when anyone in it is", () => {
    const d = make({ colorBy: "dp:test_dp" }).derived();
    const caseColour = rgba(d.col, 1);                               // person 1 is a case
    expect(rgba(d.ucol, 0)).toEqual(caseColour);                     // householder outside, child a case
    expect(rgba(d.ucol, 2)).toEqual(rgba(d.col, 4));                 // group quarters: resident 4 is a case
    expect(rgba(d.ucol, 1)).toEqual(rgba(d.col, 7));                 // nobody a case: the roommate, not the outside householder
  });

  it("shows only households with someone passing the filters, coloured by who passes", () => {
    const d = make({ filters: [{ by: "age_band", values: ["Under 18"] }] }).derived();
    expect(Array.from(d.idx)).toEqual([1]);
    expect(Array.from(d.units)).toEqual([0]);
    expect(rgba(d.ucol, 0)).toEqual(rgba(d.col, 1));
  });

  it("fades people and households outside the area in focus", () => {
    const d = make({ focus: { level: "parcel", id: "076 A039" } }).derived();
    expect(d.col[0 * 4 + 3]).toBeLessThan(d.col[4 * 4 + 3]);
    expect(d.ucol[0 * 4 + 3]).toBeLessThan(d.ucol[2 * 4 + 3]);
  });

  it("rebuilds what it derives only when the choices or the people change", () => {
    const v = make(), first = v.derived();
    expect(v.derived()).toBe(first);
    v.state.filters.push({ by: "sex", values: ["female"] });
    const second = v.derived();
    expect(second).not.toBe(first);
    expect(Array.from(second.idx)).toEqual([1, 2, 4, 5, 7]);
    v.pop.setDraft(null);
    expect(v.derived()).not.toBe(second);
  });

  it("opens a household, picks a person and closes, telling onChange each time", () => {
    const v = make(), changed = vi.fn();
    v.onChange = changed;
    v.open(1);
    expect(v.selection).toEqual({ unit: 1, person: null });
    v.pick(5);                                                       // a person elsewhere opens their household
    expect(v.selection).toEqual({ unit: 2, person: 5 });
    v.close();
    expect(v.selection).toBeNull();
    v.close();                                                       // nothing open: no change
    v.open(99);                                                      // no such household
    expect(v.selection).toBeNull();
    expect(changed).toHaveBeenCalledTimes(3);
  });

  it("closes the open household when another run comes in", () => {
    const v = make();
    v.open(0);
    v.setRun(people(), "draw_01");
    expect(v.selection).toBeNull();
    expect(v.pop.run).toBe("draw_01");
  });

  it("shows households from block zoom, or at every zoom while the player is open", () => {
    expect(make().visibleAt(MIN_ZOOM - 1)).toBe(false);
    expect(make().visibleAt(MIN_ZOOM)).toBe(true);
    expect(make({ anyZoom: true }).visibleAt(10)).toBe(true);
    expect(make({ show: false, anyZoom: true }).visibleAt(18)).toBe(false);
  });

  it("filtered-out people in an open household are drawn grey", () => {
    const v = make({ filters: [{ by: "age_band", values: ["Under 18"] }] });
    expect(v.memberFill(0)).toEqual([175, 175, 170, 110]);
    expect(v.memberFill(1)[3]).toBeGreaterThan(110);
  });
});

describe("memberLayout", () => {
  it("puts every person inside the circle without overlapping", () => {
    for (const n of [1, 2, 3, 5, 7, 8, 12, 40, 222]) {
      const { at, dot } = memberLayout(n, 10);
      expect(at).toHaveLength(n);
      for (const [x, y] of at) expect(Math.hypot(x, y) + dot).toBeLessThanOrEqual(10.01);
      for (let a = 0; a < n; a++) for (let b = a + 1; b < n; b++)
        expect(Math.hypot(at[a][0] - at[b][0], at[a][1] - at[b][1])).toBeGreaterThanOrEqual(2 * dot - 1e-9);
    }
  });
});
