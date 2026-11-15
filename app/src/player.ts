// The simulation player: steps the people layer through every saved run (main, 20
// placement draws, 80 replicates, validation) like a video. Runs load on demand and the
// next few are fetched ahead, so playback only waits when the network is slow.
import { getPeople, type People, type PeopleRun } from "./api";

const AHEAD = 3;          // runs fetched ahead of the playhead
const KEEP = 8;           // runs kept in memory

/** What a run is, in words. */
export function describeRun(r: PeopleRun, runs: PeopleRun[]): string {
  const n = Number(r.name.split("_")[1]);
  const of = (kind: string) => runs.filter((x) => x.kind === kind).length;
  switch (r.kind) {
    case "main": return "Main run: the one in the person table and on the map by default";
    case "draw": return `Placement draw ${n} of ${of("draw")}: same fit, households rounded and placed on parcels differently`;
    case "replicate": return `Replicate ${n} of ${of("replicate")}: refitted with Census survey weight set ${n} and its targets`;
    case "validation": return "Validation run: some blocks hidden from the model, to test placement";
    default: return r.name;
  }
}

export class Player {
  private el: HTMLElement;
  private runs: PeopleRun[];
  private onFrame: (p: People, run: PeopleRun, i: number) => void;
  private onClose: () => void;
  private cache = new Map<string, Promise<People>>();
  private i = 0;
  private playing = false;
  private timer: number | null = null;
  private fps = 1;
  private shown = -1;

  constructor(el: HTMLElement, runs: PeopleRun[], main: People,
              onFrame: (p: People, run: PeopleRun, i: number) => void, onClose: () => void) {
    this.el = el;
    this.runs = runs;
    this.onFrame = onFrame;
    this.onClose = onClose;
    this.cache.set("main", Promise.resolve(main));
  }

  get open() { return !this.el.hidden; }

  show() {
    this.el.hidden = false;
    this.mount();
    this.go(this.i);
  }

  hide() {
    this.pause();
    this.el.hidden = true;
    this.onClose();
  }

  private mount() {
    const max = this.runs.length - 1;
    this.el.innerHTML = `
      <div class="pl-row">
        <button class="pl-play" data-act="play" aria-label="Play">▶</button>
        <button class="ghost tiny" data-act="prev" aria-label="Previous run">‹</button>
        <button class="ghost tiny" data-act="next" aria-label="Next run">›</button>
        <input type="range" class="pl-scrub" min="0" max="${max}" step="1" value="${this.i}" aria-label="Run">
        <span class="pl-count"></span>
        <select class="pl-speed" aria-label="Speed">
          <option value="0.5">0.5 runs/s</option><option value="1" selected>1 run/s</option>
          <option value="2">2 runs/s</option><option value="4">4 runs/s</option>
        </select>
        <button class="ghost tiny" data-act="close" aria-label="Close player">×</button>
      </div>
      <div class="pl-label" aria-live="polite"></div>
      <div class="pl-ticks" aria-hidden="true">${this.runs.map((r) => `<i class="k-${r.kind}"></i>`).join("")}</div>`;
    const $ = <T extends HTMLElement>(sel: string) => this.el.querySelector<T>(sel)!;
    $<HTMLButtonElement>('[data-act="play"]').onclick = () => (this.playing ? this.pause() : this.play());
    $<HTMLButtonElement>('[data-act="prev"]').onclick = () => { this.pause(); this.go(this.i - 1); };
    $<HTMLButtonElement>('[data-act="next"]').onclick = () => { this.pause(); this.go(this.i + 1); };
    $<HTMLButtonElement>('[data-act="close"]').onclick = () => this.hide();
    $<HTMLInputElement>(".pl-scrub").oninput = (e) => { this.pause(); this.go(+(e.target as HTMLInputElement).value); };
    $<HTMLSelectElement>(".pl-speed").value = String(this.fps);
    $<HTMLSelectElement>(".pl-speed").onchange = (e) => {
      this.fps = +(e.target as HTMLSelectElement).value;
      if (this.playing) { this.pause(); this.play(); }
    };
  }

  private load(name: string): Promise<People> {
    let p = this.cache.get(name);
    if (!p) {
      p = getPeople(name);
      p.catch(() => this.cache.delete(name));
      this.cache.set(name, p);
      // Forget the oldest runs, but never the main run.
      for (const k of this.cache.keys()) {
        if (this.cache.size <= KEEP) break;
        if (k !== "main" && k !== name) this.cache.delete(k);
      }
    }
    return p;
  }

  /** Show run i (wrapping around), then fetch the next few. */
  private async go(i: number) {
    const n = this.runs.length;
    this.i = ((i % n) + n) % n;
    const run = this.runs[this.i];
    this.status(run, true);
    try {
      const p = await this.load(run.name);
      if (this.runs[this.i] !== run) return;          // the user moved on meanwhile
      this.shown = this.i;
      this.onFrame(p, run, this.i);
      this.status(run, false);
    } catch (e) {
      this.el.querySelector(".pl-label")!.textContent = `Could not load ${run.name}: ${(e as Error).message}`;
      this.pause();
    }
    for (let k = 1; k <= AHEAD; k++) this.load(this.runs[(this.i + k) % n].name);
  }

  private status(run: PeopleRun, loading: boolean) {
    const main = this.runs[0];
    const diff = run.people - main.people;
    this.el.querySelector<HTMLInputElement>(".pl-scrub")!.value = String(this.i);
    this.el.querySelector(".pl-count")!.textContent = `${this.i + 1} / ${this.runs.length}`;
    this.el.querySelector(".pl-label")!.innerHTML = `<b>${run.name}</b> · ${describeRun(run, this.runs)}<br>
      <span>${run.people.toLocaleString()} people${run.kind === "main" ? "" : ` (${diff >= 0 ? "+" : ""}${diff.toLocaleString()} vs main)`},
      ${run.households.toLocaleString()} households${loading ? " · loading…" : ""}</span>`;
    this.el.querySelectorAll(".pl-ticks i").forEach((t, k) => t.classList.toggle("on", k === this.i));
  }

  play() {
    this.playing = true;
    const b = this.el.querySelector<HTMLButtonElement>('[data-act="play"]')!;
    b.textContent = "❚❚"; b.setAttribute("aria-label", "Pause");
    const tick = async () => {
      if (!this.playing) return;
      const started = performance.now();
      await this.go(this.shown + 1);
      if (!this.playing) return;
      // Wait out the rest of the frame; a slow load just makes this frame longer.
      this.timer = window.setTimeout(tick, Math.max(0, 1000 / this.fps - (performance.now() - started)));
    };
    this.timer = window.setTimeout(tick, 1000 / this.fps);
  }

  pause() {
    this.playing = false;
    if (this.timer !== null) { clearTimeout(this.timer); this.timer = null; }
    const b = this.el.querySelector<HTMLButtonElement>('[data-act="play"]');
    if (b) { b.textContent = "▶"; b.setAttribute("aria-label", "Play"); }
  }
}
