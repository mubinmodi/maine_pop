// A tiny run: three households and one group-quarters circle on two parcels.
//   unit 0  household 10 on parcel 014 J001: householder 40 (m), relatives aged 12 (f) and 70 (f)
//   unit 1  household 11 on parcel 014 J001: householder 30 (m) living with a roommate 28 (f)
//   unit 2  group quarters on parcel 076 A039: residents 85 (f) and 90 (f)
//   unit 3  household 12 on parcel 076 A039: householder 25 (m) living alone
import type { People, RiskModel, Schema } from "../src/api";

export const people = (): People => ({
  count: 8,
  lon: [-70.26, -70.26, -70.26, -70.261, -70.27, -70.27, -70.271, -70.261],
  lat: [43.66, 43.66, 43.66, 43.661, 43.65, 43.65, 43.651, 43.661],
  age: [40, 12, 70, 30, 85, 90, 25, 28],
  poverty_ratio: [250, 250, 250, 120, -1, -1, 501, 90],
  u: [0.9, 0.1, 0.9, 0.1, 0.2, 0.8, 0.3, 0.6],
  household: [10, 10, 10, 11, -1, -1, 12, 11],
  family: [10, 10, 10, -1, -1, -1, -1, -1],
  head: [1, 0, 0, 1, 0, 0, 1, 0],
  parcel: [0, 0, 0, 0, 1, 1, 1, 0], parcels: ["014 J001", "076 A039"],
  block: [0, 0, 0, 0, 1, 1, 1, 0], blocks: ["230050001001000", "230050002001000"],
  fallback: [0, 0, 0, 0, 0, 0, 1, 0],
  attributes: ["sex", "tenure"],
  codes: { sex: [0, 1, 1, 0, 1, 1, 0, 1], tenure: [0, 0, 0, 1, -1, -1, 1, 1] },
  unit: [0, 0, 0, 1, 2, 2, 3, 1],
  units: { lon: [-70.2601, -70.2602, -70.2701, -70.2702], lat: [43.6601, 43.6602, 43.6501, 43.6502], r: [5.2, 4.2, 4.2, 3] },
});

// A data point for women only: a case when u < 0.5, so persons 1 and 4 are cases.
export const womenOnly: RiskModel = {
  id: "test_dp", label: "Test condition", baseline: 0.5, odds_ratios: {}, universe: { sex: ["female"] }, description: "", source: "",
};

export const schema = (models: RiskModel[] = [womenOnly]): Schema => ({
  levels: [], compare_with: {}, confidence: {}, disclosure: "",
  datapoints: { available: true, factors: [], presets: [] },
  variables: [
    { id: "sex", label: "Sex", kind: "seed", unit: "person", universe: null, available: true,
      categories: [{ id: "male", label: "Male" }, { id: "female", label: "Female" }] },
    { id: "tenure", label: "Tenure", kind: "seed", unit: "household", universe: null, available: true,
      categories: [{ id: "owner", label: "Owner" }, { id: "renter", label: "Renter" }] },
    ...models.map((m) => ({ id: m.id, label: m.label, kind: "datapoint" as const, unit: "person" as const, universe: null,
      available: true, categories: [], model: m })),
  ],
});
