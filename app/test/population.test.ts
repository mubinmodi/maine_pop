import { describe, expect, it } from "vitest";
import { DATAPOINT, Population } from "../src/population";
import { people, schema, womenOnly } from "./fixture";

const pop = () => new Population(people(), "main", schema());

describe("Population", () => {
  it("lists a household's people householder first, then oldest first", () => {
    expect(pop().members(0)).toEqual([0, 2, 1]);
    expect(pop().members(2)).toEqual([5, 4]);
  });

  it("describes each person's place in their household", () => {
    const p = pop();
    expect([0, 1, 3, 7, 4].map((i) => p.person(i).role))
      .toEqual(["Householder", "Relative", "Householder", "Not related", "Group quarters resident"]);
    expect(p.person(4)).toMatchObject({ household: null, family: null, povertyRatio: null, parcel: "076 A039" });
    expect(p.person(6)).toMatchObject({ household: 12, fallback: true, povertyRatio: 501 });
  });

  it("describes a household and its circle", () => {
    expect(pop().household(2)).toMatchObject({ groupQuarters: true, household: null, members: [5, 4], parcel: "076 A039", r: 4.2 });
    expect(pop().household(0)).toMatchObject({ groupQuarters: false, household: 10, family: 10, lon: -70.2601 });
  });

  it("applies data points to every person: case, not a case, or outside the universe", () => {
    const d = pop().dim("dp:test_dp")!;
    expect(Array.from({ length: 8 }, (_, i) => d.code(i))).toEqual([2, 0, 1, 2, 0, 1, 2, 1]);
    expect(pop().dims().map((x) => x.id)).toEqual(["dp:test_dp", "age_band", "sex", "tenure"]);
  });

  it("keeps the draft data point apart from saved ones until it is removed", () => {
    const p = pop();
    p.setDraft({ ...womenOnly, label: "Draft", baseline: 0.95 });
    expect(p.draftId()).toBe("dp:__draft");
    expect(p.dim("dp:__draft")!.label).toBe("Draft (draft)");
    expect(p.dim("dp:__draft")!.code(5)).toBe(DATAPOINT.case);
    p.setDraft(null);
    expect(p.draftId()).toBeNull();
  });

  it("shifts a calibrated data point's risk by each person's tract, and keeps the shifts while the draft changes", () => {
    const p = pop();
    // Tract 23005000200 (block 1: people 4, 5, 6) is pushed to certain risk, the other tract to none.
    const offsets = { "23005000100": -30, "23005000200": 30 };
    p.setDraft({ ...womenOnly, universe: {}, baseline: 0.5, calibrate: "places:DIABETES", offsets });
    const d = () => Array.from({ length: 8 }, (_, i) => p.dim("dp:__draft")!.code(i));
    expect(d()).toEqual([1, 1, 1, 1, 0, 0, 0, 1]);
    p.setDraft({ ...womenOnly, universe: {}, baseline: 0.1, calibrate: "places:DIABETES" });   // no offsets yet
    expect(d()).toEqual([1, 1, 1, 1, 0, 0, 0, 1]);
    p.setDraft({ ...womenOnly, universe: {}, baseline: 0.5, calibrate: "", offsets });         // calibration off
    expect(d()).not.toEqual([1, 1, 1, 1, 0, 0, 0, 1]);
  });

  it("reapplies data points when another run's people come in, and says so through version", () => {
    const p = pop(), before = p.version;
    const next = people();
    next.u = next.u.map(() => 0.01);                          // everyone in the universe is a case
    p.setRun(next, "draw_01");
    expect(p.run).toBe("draw_01");
    expect(p.version).toBeGreaterThan(before);
    expect(p.dim("dp:test_dp")!.code(2)).toBe(DATAPOINT.case);
  });

  it("finds people by person, family or household number, or by parcel ID prefix", () => {
    const p = pop(), all = [0, 1, 2, 3, 4, 5, 6, 7];
    expect(all.filter(p.idSearch("11")!)).toEqual([3, 7]);
    expect(all.filter(p.idSearch("6")!)).toEqual([6]);
    expect(all.filter(p.idSearch(" 076a ")!)).toEqual([4, 5, 6]);
    expect(p.idSearch("  ")).toBeNull();
  });

  it("finds an area's people by block prefix or parcel", () => {
    expect(pop().inArea({ level: "block_group", id: "230050002001" })).toEqual([4, 5, 6]);
    expect(pop().inArea({ level: "parcel", id: "014 J001" })).toEqual([0, 1, 2, 3, 7]);
    expect(pop().inArea({ level: "city", id: "x" })).toHaveLength(8);
  });

  it("writes the person table's columns as CSV, with a column per data point", () => {
    const [header, row4] = pop().csv([4]).trim().split("\n");
    expect(header.split(",").slice(0, 13)).toEqual(["person_id", "family_id", "parcel_id", "AGE", "SEX", "RACE", "EDUCATION",
      "POVERTY", "POVERTY_RATIO", "EMPLOYMENT", "DISABILITY", "HEALTH_INSURANCE", "household_id"]);
    expect(header.endsWith(",test_dp")).toBe(true);
    expect(row4.split(",")).toEqual(["4", "", "076 A039", "85", "female", "", "", "", "", "", "", "", "", "230050002001000", "-70.27", "43.65", "address", "1"]);
  });

  it("summarises an area from its people", () => {
    expect(pop().summary([0, 1, 2, 3, 4, 5, 6, 7])).toEqual({
      people: 8, households: 3, families: 1, medianAge: 40, female: 5, groupQuarters: 2,
      dps: [{ label: "Test condition", cases: 2, universe: 5 }],
    });
  });
});
