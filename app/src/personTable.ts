// The person table: one row per synthetic person of the main run (one realisation),
// in a drawer over the bottom of the map. It follows the sidebar's "Show only people
// where" filters, pages 100 rows at a time, finds a person, family, household or parcel
// by ID, downloads every matching row as CSV, and clicking a row flies to that person.
import type { PeopleLayer, PeopleState } from "./people";

const PAGE = 100;
const KEY_COLS = 5;   // person_id, family_id, parcel_id, AGE, SEX

export class PersonTable {
  private el: HTMLElement;
  private layer: PeopleLayer | null = null;
  private s: PeopleState | null = null;
  private onPick: (i: number) => void;
  private page = 0;
  private query = "";
  private rows: number[] = [];

  constructor(el: HTMLElement, onPick: (i: number) => void) {
    this.el = el;
    this.onPick = onPick;
  }

  get open() { return !this.el.hidden; }

  show(layer: PeopleLayer, s: PeopleState) {
    this.layer = layer;
    this.s = s;
    this.el.hidden = false;
    this.page = 0;
    this.mount();
    this.update();
  }

  hide() { this.el.hidden = true; }

  /** Filters or data points changed: recount the rows, keeping the search. */
  refresh() {
    if (!this.open || !this.layer) return;
    this.page = 0;
    this.update();
  }

  private mount() {
    this.el.innerHTML = `
      <header>
        <div class="pt-title"><b>Person table</b> <span class="src">one realisation (main run)</span></div>
        <input type="search" class="pt-find" placeholder="Find person, family, household or parcel ID" aria-label="Find by ID" autocomplete="off" value="${esc(this.query)}">
        <span class="pt-count" aria-live="polite"></span>
        <div class="pt-pager">
          <button class="ghost tiny" data-act="prev" aria-label="Previous page">‹</button>
          <button class="ghost tiny" data-act="next" aria-label="Next page">›</button>
        </div>
        <button class="tiny" data-act="csv">Download CSV</button>
        <button class="ghost tiny" data-act="close" aria-label="Close person table">×</button>
      </header>
      <div class="people-table pt-body"></div>
      <p class="hint pt-note">Family = the householder and their relatives by birth, marriage or adoption (Census definition); empty for people living alone, with non-relatives, or in group quarters. Click a row to go to that person.</p>`;
    const q = this.el.querySelector<HTMLInputElement>(".pt-find")!;
    q.oninput = () => { this.query = q.value; this.page = 0; this.update(); };
    this.el.querySelector<HTMLButtonElement>('[data-act="prev"]')!.onclick = () => { this.page--; this.render(); };
    this.el.querySelector<HTMLButtonElement>('[data-act="next"]')!.onclick = () => { this.page++; this.render(); };
    this.el.querySelector<HTMLButtonElement>('[data-act="close"]')!.onclick = () => this.hide();
    this.el.querySelector<HTMLButtonElement>('[data-act="csv"]')!.onclick = () => this.download();
    this.el.querySelector<HTMLElement>(".pt-body")!.onclick = (e) => {
      const tr = (e.target as HTMLElement).closest<HTMLTableRowElement>("tr[data-i]");
      if (tr) this.onPick(+tr.dataset.i!);
    };
  }

  /** Rows passing the sidebar filters, then the ID search. */
  private update() {
    const layer = this.layer!, p = layer.p;
    const idx = Array.from(layer.visible(this.s!));
    const q = this.query.trim().replace(/\s+/g, "").toUpperCase();
    if (!q) this.rows = idx;
    else {
      // A number is an exact person, family or household ID; a parcel ID (e.g. "014 J001")
      // matches by prefix, ignoring spaces, once the search holds a letter.
      const n = /^\d+$/.test(q) ? Number(q) : NaN;
      const parcelHit = p.parcels.map((x) => { const k = x.replace(/\s+/g, "").toUpperCase(); return Number.isNaN(n) ? k.startsWith(q) : k === q; });
      this.rows = idx.filter((i) => i === n || p.family[i] === n || p.household[i] === n || parcelHit[p.parcel[i]]);
    }
    this.render();
  }

  private render() {
    const cols = this.layer!.columns();
    const pages = Math.max(1, Math.ceil(this.rows.length / PAGE));
    this.page = Math.min(Math.max(this.page, 0), pages - 1);
    const from = this.page * PAGE, to = Math.min(from + PAGE, this.rows.length);
    const body = this.rows.slice(from, to).map((i) =>
      `<tr data-i="${i}" tabindex="0">${cols.map(([, f], c) => `<td${c < KEY_COLS ? ' class="key"' : ""}>${esc(String(f(i)))}</td>`).join("")}</tr>`).join("");
    this.el.querySelector(".pt-body")!.innerHTML = `<table><thead><tr>${cols.map(([h], c) => `<th${c < KEY_COLS ? ' class="key"' : ""}>${esc(h)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`
      + (this.rows.length ? "" : `<p class="empty">No synthetic people match.</p>`);
    const total = this.layer!.p.count;
    const filtered = this.rows.length !== total ? ` (of ${total.toLocaleString()})` : "";
    this.el.querySelector(".pt-count")!.textContent = this.rows.length
      ? `${(from + 1).toLocaleString()}–${to.toLocaleString()} of ${this.rows.length.toLocaleString()} people${filtered}` : `0 people${filtered}`;
    this.el.querySelector<HTMLButtonElement>('[data-act="prev"]')!.disabled = this.page === 0;
    this.el.querySelector<HTMLButtonElement>('[data-act="next"]')!.disabled = this.page >= pages - 1;
  }

  private download() {
    const url = URL.createObjectURL(new Blob([this.layer!.csv(this.rows)], { type: "text/csv" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "persons_main.csv" });
    a.click();
    URL.revokeObjectURL(url);
  }
}

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
