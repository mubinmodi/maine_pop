// The household card: shows while a household is open. It says what the household is and
// lists its people (householder first); clicking a person, here or on the map, shows
// everything about them. "Open parcel" opens the parcel in the detail panel.
import { describeHousehold, personTooltip } from "./peoplePanels";
import type { PeopleView } from "./peopleView";

export class HouseholdCard {
  private el: HTMLElement;
  private onParcel: (id: string) => void;
  private key = "";

  constructor(el: HTMLElement, onParcel: (id: string) => void) {
    this.el = el;
    this.onParcel = onParcel;
  }

  /** Redraw for the view's open household; hides when none is open or households are hidden. */
  render(view: PeopleView | null, shown: boolean) {
    const sel = view?.selection;
    if (!view || !sel || !shown) { this.el.hidden = true; this.key = ""; return; }
    this.el.hidden = false;
    const key = `${view.key()}|${sel.unit}|${sel.person}`;
    if (key === this.key) return;
    this.key = key;
    const pop = view.pop, h = describeHousehold(pop, sel.unit);
    const rows = h.members.map((i) => {
      const p = pop.person(i), sex = pop.label("sex", i)?.toLowerCase();
      return `<li><button data-i="${i}" class="${i === sel.person ? "on" : ""}" aria-pressed="${i === sel.person}">
        <i style="background:${view.colour(i)}"></i><span class="hc-role">${p.role}</span>
        <span>${p.age}${sex ? `, ${sex}` : ""}</span><span class="hc-id">#${i}</span></button></li>`;
    }).join("");
    this.el.innerHTML = `
      <header><b>${h.title}</b> <span class="src">${h.size}</span>
        <button class="ghost tiny hc-close" data-act="close" aria-label="Close household">×</button></header>
      <p class="hc-facts">${h.facts.map(esc).join(" · ")}</p>
      ${h.dps.length ? `<p class="hc-facts">${h.dps.join("<br>")}</p>` : ""}
      <ul class="hc-members">${rows}</ul>
      <div class="hc-person">${sel.person !== null ? personTooltip(pop, sel.person) : `<span class="hint">Click a person to see more about them.</span>`}</div>
      <div class="hc-foot"><button class="ghost tiny" data-act="parcel">Open parcel ${esc(h.parcel)}</button></div>`;
    this.el.querySelector<HTMLButtonElement>('[data-act="close"]')!.onclick = () => view.close();
    this.el.querySelector<HTMLButtonElement>('[data-act="parcel"]')!.onclick = () => this.onParcel(h.parcel);
    this.el.querySelectorAll<HTMLButtonElement>(".hc-members button").forEach((b) => (b.onclick = () => view.pick(+b.dataset.i!)));
  }
}

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
