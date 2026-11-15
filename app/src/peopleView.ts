// What the map shows of the people: the sidebar's choices (colour by, filters, the area in
// focus), the household that is open, and everything derived from them: who passes the
// filters, each person's and household's colour, and the counts for the legend.
//
// Derived values are rebuilt only when the population or the choices change, and every
// change to the open household goes through open / pick / close, which call onChange.
import { DATAPOINT, type Dim, type Focus, type Population } from "./population";
import type { People } from "./api";

export type RGB = [number, number, number];
export type RGBA = [number, number, number, number];
export type PeopleFilter = { by: string; values: string[] };
// focus: the area open in the detail panel; households outside it are faded.
// anyZoom: show households at every zoom (while the simulation player is open), not only from block zoom.
export type PeopleState = { show: boolean; colorBy: string; filters: PeopleFilter[]; focus?: Focus | null; anyZoom?: boolean };
// The open household (a unit index) and, once one of its people is picked, that person.
export type Selection = { unit: number; person: number | null };

export const MIN_ZOOM = 14;          // households show from block zoom down

const hex = (h: string): RGB => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) as RGB;
// Categorical slots in fixed order, checked with the dataviz palette validator on the
// light surface (contrast is below 3:1 for some, so the legend labels every colour).
const SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"].map(hex);
export const BLANK: RGB = [175, 175, 170];
const FAINT = 45, SOLID = 215;

/** Everything drawn from the current choices. Per-person arrays are indexed by person id;
 * per-household arrays by position in `units` (the households holding anyone visible). */
export type Derived = {
  dim: Dim;                    // the dimension people are coloured by
  palette: RGB[];              // its colours, by category
  idx: Uint32Array;            // people passing every filter, in person_id order
  vis: Uint8Array;             // 1 where a person passes the filters
  col: Uint8Array;             // every person's colour (RGBA), faint outside a data point's universe or the focus
  counts: number[];            // visible people per category (last: blank)
  units: Uint32Array;
  upos: Float64Array; ucol: Uint8Array; urad: Float32Array;
  households: number;          // of units, how many are households (not group quarters)
};

export class PeopleView {
  readonly pop: Population;
  readonly state: PeopleState;
  onChange: () => void = () => {};
  private sel: Selection | null = null;
  private cache: { key: string; d: Derived } | null = null;

  constructor(pop: Population, state: PeopleState) {
    this.pop = pop;
    this.state = state;
  }

  static colourable(d: Dim) { return d.kind === "datapoint" || d.cats.length <= SLOTS.length; }

  /** Whether households show at this zoom. */
  visibleAt(zoom: number) { return this.state.show && (zoom >= MIN_ZOOM || !!this.state.anyZoom); }

  /** The dimension people are coloured by: the chosen one, else the first. */
  colourDim() { return this.pop.dim(this.state.colorBy) ?? this.pop.dims()[0]; }

  // ---------------------------------------------------------------- the open household

  get selection(): Readonly<Selection> | null { return this.sel; }

  /** Open a household, optionally with one of its people picked. */
  open(unit: number, person: number | null = null) {
    if (unit < 0 || unit >= this.pop.units) return;
    this.sel = { unit, person };
    this.onChange();
  }

  /** Pick a person: their household opens if it is not open already. */
  pick(person: number) {
    this.sel = { unit: this.pop.person(person).unit, person };
    this.onChange();
  }

  close() {
    if (!this.sel) return;
    this.sel = null;
    this.onChange();
  }

  /** Show another run's people. Household ids differ between runs, so the open household closes. */
  setRun(doc: People, run: string) {
    this.pop.setRun(doc, run);
    this.sel = null;
    this.onChange();
  }

  // ---------------------------------------------------------------- derived

  /** A key for everything the derived values depend on; changes whenever they would. */
  key() { return `${this.pop.version}|${JSON.stringify({ ...this.state, anyZoom: undefined })}`; }

  derived(): Derived {
    const key = this.key();
    if (this.cache?.key === key) return this.cache.d;
    const d = this.compute();
    this.cache = { key, d };
    return d;
  }

  /** People passing every filter in the sidebar, in person_id order. */
  visible(): Uint32Array { return this.derived().idx; }

  /** A person's colour, as CSS. */
  colour(i: number) {
    const c = this.derived().col;
    return `rgb(${c[i * 4]},${c[i * 4 + 1]},${c[i * 4 + 2]})`;
  }

  /** A person's dot colour inside an open household: their colour, or grey if filtered out. */
  memberFill(i: number): RGBA {
    const v = this.derived();
    return v.vis[i] ? [v.col[i * 4], v.col[i * 4 + 1], v.col[i * 4 + 2], Math.max(v.col[i * 4 + 3], 120)] : [...BLANK, 110];
  }

  /** A household takes its householder's colour (else its first visible member's); with a
   * data point it is a case when anyone visible in it is. Households with nobody visible are left out. */
  private compute(): Derived {
    const pop = this.pop, s = this.state, n = pop.count;
    const checks = s.filters.flatMap((f) => {
      const d = pop.dim(f.by);
      if (!d) return [];
      const ok = new Set(f.values.map((v) => d.cats.findIndex((c) => c.id === v)));
      return [(i: number) => ok.has(d.code(i))];
    });
    const dim = this.colourDim();
    const dp = dim.kind === "datapoint";
    const palette = dp ? [SLOTS[1], SLOTS[0], BLANK] : dim.cats.map((_, i) => SLOTS[i % SLOTS.length]);
    const inFocus = s.focus ? pop.matcher(s.focus) : null;
    const counts = new Array(dim.cats.length + 1).fill(0);
    const vis = new Uint8Array(n), col = new Uint8Array(n * 4), code = new Int16Array(n);
    const keep: number[] = [];
    for (let i = 0; i < n; i++) {
      const c = dim.code(i);
      code[i] = c;
      const known = c >= 0 && c < palette.length;
      // Outside a data point's universe (e.g. under 65 for dementia) stays faint, so cases stand out.
      const faint = (dp && c === DATAPOINT.outside) || (inFocus !== null && !inFocus(i));
      col.set([...(known ? palette[c] : BLANK), faint ? FAINT : SOLID], i * 4);
      if (checks.every((f) => f(i))) { vis[i] = 1; keep.push(i); counts[known ? c : dim.cats.length]++; }
    }
    const shown: number[] = [], reps: number[] = [];
    pop.eachUnit((u, members) => {
      let rep = -1;
      for (const i of members) {
        if (!vis[i]) continue;
        if (!dp) { rep = i; break; }
        if (rep < 0 || code[i] < code[rep]) rep = i;      // case before not a case before outside
      }
      if (rep >= 0) { shown.push(u); reps.push(rep); }
    });
    const units = Uint32Array.from(shown);
    const upos = new Float64Array(units.length * 2), ucol = new Uint8Array(units.length * 4), urad = new Float32Array(units.length);
    let households = 0;
    units.forEach((u, k) => {
      const h = pop.household(u);
      upos[k * 2] = h.lon; upos[k * 2 + 1] = h.lat; urad[k] = h.r;
      ucol.set(col.subarray(reps[k] * 4, reps[k] * 4 + 4), k * 4);
      if (!h.groupQuarters) households++;
    });
    return { dim, palette, idx: Uint32Array.from(keep), vis, col, counts, units, upos, ucol, urad, households };
  }
}
