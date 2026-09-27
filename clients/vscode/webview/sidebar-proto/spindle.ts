/** THE SPINDLE — the sidebar as the paperwork on your desk, not as a list of your agents.
 *
 *  The conventional answer to "show me my agents in a side bar" is a list of cards: an avatar, a
 *  name, a coloured status dot, a spinner, an elapsed time on the right. Every agent extension
 *  ships it, which is exactly the singularity check failing — it is what anyone would draw for ANY
 *  project, so it cannot be the answer for THIS one.
 *
 *  The second answer, once you know the workplace exists, is to shrink the workplace: a little
 *  cutaway building in the side bar. That one is worse than conventional — REDUNDANT: two views
 *  of the same picture, one of them strictly worse, and the honest question becomes why the big
 *  one exists at all. Coherence is not repetition.
 *
 *  So the two surfaces split the WORK instead of the picture. The workplace is spatial: it answers
 *  "where is everyone, what is the team doing" and it has no clock. This panel is the desk that
 *  building reports to — every job is a WORK ORDER, impaled on one steel spike, running the whole
 *  height of the panel and down through the composer at the bottom. That rod is the answer to "two
 *  products bolted together": everything in the side bar hangs on one piece of ironmongery, so
 *  there are no longer two halves to be incoherent between.
 *
 *  The one idea that makes it more than a nicer list: PAPER SINKS. A spike is physical, so depth in
 *  the pile means something. Live work sits on top at full height; work that is finished, held or
 *  somebody else's is pressed down into a sliver you can still read a name off. Vertical space is
 *  therefore allocated by DEMAND FOR ATTENTION rather than handed out equally per row — which is the
 *  behaviour a 220px column actually needs, and the one thing a list structurally cannot do.
 *
 *  Coherence with the workplace is STRUCTURAL, not cosmetic: the people, their deterministic faces,
 *  the pod-colour assignment, the status marks and the sleep mark are imported from
 *  `webview/workplace/` and rendered by the workplace's own pixel engine. The same rectangles make
 *  the same person in both views.
 */
import { draw, drawFrames } from "../workplace/pixels";
import { GHOST_PAL, POSE_MOVE, POSE_REST, SKIN_PAL, SNOOZE } from "../workplace/art";
import { STALL_SECONDS, isHeld, markOf, stampFor, stampHtml } from "../workplace/status";
import { assignAccents, faceOf, shortDuration } from "../workplace/palette";
import { clip, esc } from "../workplace/esc";
import { ZONES } from "../../src/team";
import type { ZoneId } from "../../src/team";
import { DESK_STAMP, DOG_EAR, OUT_FLAG, PAD_CORNER, SPINDLE_BASE, SPINDLE_TIP, STAPLE } from "./art";
import type { Board, Docket } from "./fixture";

const LABELS = new Map<ZoneId, string>(ZONES.map((z) => [z.id, z.label]));

/** How deep in the pile a slip sits. `full` is on top and readable; `slim` has been pressed down
 *  and shows only its top edge. This is the whole information model. */
type Fold = "full" | "slim";

function foldOf(d: Docket): Fold {
  if (d.status === "error") return "full"; // a death still needs a person, so it keeps its height
  if (d.status === "done" || d.status === "foreign") return "slim";
  return d.idle_seconds >= STALL_SECONDS ? "slim" : "full";
}

/** Time on the clock: counting up while it runs, frozen at its end once it has one. An end the
 *  registry never stamped renders open rather than pretending the run ended now. */
function elapsedOf(d: Docket, at: number): string {
  if (!d.started_at) return "";
  const end = d.finished_at ?? Math.floor(at / 1000);
  return shortDuration(Math.max(0, end - d.started_at));
}

/** API-equivalent usage value, separate from provider charge and account impact. `null` is unknown
 *  and renders an em dash rather than inventing zero. One fixed precision all the way down the
 *  column keeps the figures aligned. */
function costOf(cost: number | null | undefined): string {
  return cost == null ? "—" : `~$${cost.toFixed(4)}`;
}

function totalOf(ds: Docket[]): string {
  const known = ds.filter((d) => d.cost_usd != null);
  if (!known.length) return "—";
  return `~$${known.reduce((s, d) => s + (d.cost_usd ?? 0), 0).toFixed(4)}`;
}

function spriteOf(d: Docket, scale: number): string {
  const pal = d.status === "foreign" ? GHOST_PAL : SKIN_PAL;
  const live = d.status === "running" && d.idle_seconds < STALL_SECONDS;
  return live
    ? drawFrames([POSE_REST, POSE_MOVE], pal, { scale, className: "sp-sprite" })
    : draw(POSE_REST, pal, { scale, className: "sp-sprite" });
}

/** A torn bottom edge, generated rather than hand-authored so the sawtooth stays even at any width.
 *  Only an errored slip gets one: the tear IS the status, read before any colour or any word.
 *
 *  The clipped element paints the PANEL colour, not the paper — these points describe where the
 *  sheet is MISSING, so what shows through the notches is whatever sits behind the work order. */
function tornEdge(steps = 13): string {
  const pts: string[] = [];
  for (let i = 0; i <= steps; i++) {
    pts.push(`${((i / steps) * 100).toFixed(2)}% ${i % 2 === 0 ? "100%" : "0%"}`);
  }
  pts.push("100% 100%", "0% 100%");
  return `clip-path:polygon(${pts.join(",")})`;
}

/** One job. The click target is this element and nothing inside it, so a click anywhere on the
 *  photo, the name or the words resolves to exactly one run. */
function slip(d: Docket, depth: number, at: number, leadName: string | null): string {
  const fold = foldOf(d);
  // Only WORK can stall. A finished run's idle clock is just how long ago it ended, and a session
  // interact did not start has no clock we own at all — sleeping either of them says the run is
  // stuck when it is simply over.
  const stalled = isHeld(d) ? 1 : 0;
  const stamp = stampFor(d);
  const zone = LABELS.get(d.zone) ?? d.zone;
  const out = d.zone === "web";

  // Buried slips do not show a face. You are looking at the edge of a piece of paper under a pile;
  // the mark is what is still visible, and it is the mark that carries the status anyway.
  const mug =
    fold === "full"
      ? `<span class="sp-mug">${spriteOf(d, 2)}</span>`
      : `<span class="sp-mug sp-mug-slim">${markOf(d.status, 2)}</span>`;

  // The stamp is not built here any more — it is the shared device, markup and all, so a change
  // to the word or the shape reaches both panels or neither. HELD used to be drawn with the open
  // square, which is the mark for NOT OURS; it has its own hourglass now.
  const badge = stamp
    ? stampHtml(stamp)
    : `<span class="sp-lamp" title="running">${markOf("running", 2)}</span>`;

  const flag = out
    ? `<span class="sp-out" title="out of the building">` +
      `${draw(OUT_FLAG.grid, OUT_FLAG.pal, { scale: 2, outline: false })}</span>`
    : "";

  const snooze = stalled
    ? `<span class="sp-snooze">${draw(SNOOZE.grid, SNOOZE.pal, { scale: 2, outline: false })}</span>`
    : "";

  const ear = stalled
    ? `<span class="sp-ear">${draw(DOG_EAR.grid, DOG_EAR.pal, { scale: 2, outline: false })}</span>`
    : "";

  const staple =
    depth > 0
      ? `<span class="sp-staple">${draw(STAPLE.grid, STAPLE.pal, { scale: 2, outline: false })}</span>`
      : "";

  const said = d.activity
    ? `<p class="sp-said">${esc(clip(d.activity, 96))}</p>`
    : `<p class="sp-said sp-said-none">no word yet</p>`;

  const role = [d.agent ? d.agent : "session lead", out ? "out at the web" : zone.toLowerCase()]
    .filter(Boolean)
    .join(" · ");

  const parent = leadName
    ? `<span class="sp-from" title="sent by ${esc(leadName)}">&#8627;${esc(clip(leadName, 12))}</span>`
    : "";

  const label =
    `${d.name} — ${d.status}, ${zone}` + (d.activity ? `: ${d.activity}` : "") ;

  return (
    `<article class="sp-slip" data-run-id="${esc(d.run_id)}" data-status="${esc(d.status)}" ` +
    `data-fold="${fold}" data-stalled="${stalled}" data-depth="${depth}" ` +
    `tabindex="0" role="button" aria-label="${esc(label)}" ` +
    `style="${faceOf(d.run_id)}"${d.status === "error" ? ` data-torn="1"` : ""}>` +
    staple +
    ear +
    `<div class="sp-row">` +
    mug +
    `<div class="sp-body">` +
    `<div class="sp-head"><h3 class="sp-name">${esc(clip(d.name, 22))}</h3>${flag}${snooze}${badge}</div>` +
    `<div class="sp-role">${esc(role)}</div>` +
    said +
    `<div class="sp-foot">` +
    `<span class="sp-t">${esc(elapsedOf(d, at))}</span>` +
    `<span class="sp-c">${esc(costOf(d.cost_usd))}</span>` +
    parent +
    `</div>` +
    `</div>` +
    `</div>` +
    (d.status === "error" ? `<span class="sp-tear" style="${tornEdge()}"></span>` : "") +
    `</article>`
  );
}

interface Team {
  lead: Docket;
  reports: Docket[];
}

/** Leads, and who they sent. A run whose parent the registry has already forgotten is its own
 *  lead rather than vanishing — the panel's job is to show what exists. */
function teamsOf(dockets: Docket[]): Team[] {
  const ids = new Set(dockets.map((d) => d.run_id));
  const leads = dockets.filter((d) => !d.parent_run_id || !ids.has(d.parent_run_id));
  return leads.map((lead) => ({
    lead,
    reports: dockets.filter((d) => d.parent_run_id === lead.run_id),
  }));
}

/** Live work on top. A side bar you have to scroll to find the running agent in has failed at its
 *  one job; ties fall back to the lead's name so the order is stable between refreshes. */
function orderTeams(teams: Team[]): Team[] {
  const live = (t: Team) =>
    [t.lead, ...t.reports].filter((d) => d.status === "running" && d.idle_seconds < STALL_SECONDS)
      .length;
  const bad = (t: Team) => ([t.lead, ...t.reports].some((d) => d.status === "error") ? 1 : 0);
  return [...teams].sort(
    (a, b) => bad(b) - bad(a) || live(b) - live(a) || a.lead.name.localeCompare(b.lead.name),
  );
}

/** One team is ONE piece of paper on the spike — a multi-part work order, the lead printed at the
 *  top and each report stapled underneath it. That is why there is one punch hole per TEAM and not
 *  one per agent: the slips of a team are physically the same document, which is the same decision
 *  that made the workplace's rooms share walls instead of sitting in eight separate boxes.
 */
function team(t: Team, accent: string, at: number): string {
  const all = [t.lead, ...t.reports];
  const on = all.filter((d) => d.status === "running" && d.idle_seconds < STALL_SECONDS).length;
  return (
    `<section class="sp-order" style="--accent:${accent}" data-project="${esc(t.lead.project)}">` +
    `<span class="sp-hole" aria-hidden="true"></span>` +
    `<span class="sp-spine" aria-hidden="true"></span>` +
    slip(t.lead, 0, at, null) +
    t.reports.map((r) => slip(r, 1, at, t.lead.name)).join("") +
    // A lone worker is not a team, and a totals strip under it would repeat the one figure already
    // on its own foot — twice the height for none of the information.
    (all.length > 1
      ? `<div class="sp-total">` +
        `<span class="sp-proj">${esc(clip(t.lead.project, 18))}</span>` +
        `<span class="sp-on">${on} on</span>` +
        `<span class="sp-sum">${esc(totalOf(all))}</span>` +
        `</div>`
      : `<div class="sp-total sp-total-solo">` +
        `<span class="sp-proj">${esc(clip(t.lead.project, 18))}</span>` +
        `</div>`) +
    `</section>`
  );
}

/** The board. */
export function renderBoard(board: Board): string {
  const teams = orderTeams(teamsOf(board.dockets));
  // Pod hues come from the workplace's own greedy allocator, fed EXACTLY what the building feeds
  // it: every lead in the snapshot, foreign ones included.
  //
  // This panel used to hold a foreign session out of the allocation — nobody's team, so why burn
  // one of seven colours on it. Right about the COLOUR, wrong about the ALLOCATOR: it's greedy, so
  // a lead whose preferred hue is taken steps to the next free one, and which hues are taken
  // depends on the whole SET of leads — drop one and another lead can move. Simulated over random
  // run ids, a real team's hue comes out different between the two panels in 26% of four-lead
  // snapshots, 38% of five-lead ones — and the pod hue is the ONLY thing tying a report three
  // rooms away back to its lead, so a disagreement there is the two surfaces disagreeing about who
  // is on whose team.
  // One input, one assignment. The foreign slip still draws no colour; it just no longer changes
  // anyone else's.
  const accents = assignAccents(teams.map((t) => t.lead.run_id));

  const live = board.dockets.filter(
    (d) => d.status === "running" && d.idle_seconds < STALL_SECONDS,
  ).length;
  const out = board.dockets.filter((d) => d.zone === "web").length;
  const done = board.dockets.filter((d) => d.status === "done").length;

  const tally =
    `<span class="sp-chip">${markOf("running", 2)}<b>${live}</b> on</span>` +
    (out ? `<span class="sp-chip"><b>${out}</b> out</span>` : "") +
    (done ? `<span class="sp-chip">${markOf("done", 2)}<b>${done}</b> done</span>` : "") +
    `<span class="sp-chip sp-chip-sum"><b>${esc(totalOf(board.dockets))}</b></span>`;

  // A foreign session is allocated a hue like everyone else and then does not wear it: it is
  // nobody's team, and a coloured spine would claim it is. Holding it out of the ALLOCATION is
  // what moved everyone else's colour; holding it out of the PAINT costs nothing.
  const body = teams.length
    ? teams
        .map((t) =>
          team(
            t,
            t.lead.status === "foreign"
              ? "var(--sp-dim)"
              : (accents.get(t.lead.run_id) ?? "var(--sp-dim)"),
            board.at,
          ),
        )
        .join("")
    : `<p class="sp-empty">nothing on the spike</p>`;

  return (
    `<div class="sp-panel">` +
    `<div class="sp">` +
    `<div class="sp-rail" aria-hidden="true">` +
    `<span class="sp-tip">${draw(SPINDLE_TIP.grid, SPINDLE_TIP.pal, { scale: 2, outline: false })}</span>` +
    `<span class="sp-rod"></span>` +
    `<span class="sp-base">${draw(SPINDLE_BASE.grid, SPINDLE_BASE.pal, { scale: 2, outline: false })}</span>` +
    `</div>` +
    `<header class="sp-plate"><span class="sp-plate-name">Agents</span>` +
    `<span class="sp-tally">${tally}</span></header>` +
    `<div class="sp-stack">${body}</div>` +
    // The desk under the spike. Everything below the last order used to be free space in a flex
    // column — the rod hanging in unlit black, which is what a small team gets for most of the
    // panel most of the time. It is a surface now, with the die-cut of the next slip printed on it
    // and the stamp standing where you left it. Same answer the building gives an empty room: keep
    // the floor, keep the props, keep the label, take the people away.
    `<div class="sp-clear" aria-hidden="true">` +
    `<div class="sp-blotter">` +
    ["tl", "tr", "br", "bl"]
      .map(
        (at) =>
          `<span class="sp-corner" data-at="${at}">` +
          `${draw(PAD_CORNER.grid, PAD_CORNER.pal, { scale: 2, outline: false })}</span>`,
      )
      .join("") +
    `<div class="sp-dieline">` +
    `<span class="sp-dieline-hole"></span>` +
    `<span class="sp-dieline-label">next slip</span>` +
    `</div>` +
    `<span class="sp-props">` +
    `${draw(DESK_STAMP.grid, DESK_STAMP.pal, { scale: 2 })}</span>` +
    `</div>` +
    `</div>` +
    // The composer is STUBBED — the real chat is its own webview. It is here because the rod
    // running THROUGH it is the whole argument that this panel is one object and not two.
    `<footer class="sp-compose">` +
    `<span class="sp-hole" aria-hidden="true"></span>` +
    `<div class="sp-compose-body">` +
    `<div class="sp-compose-tag">to the team</div>` +
    `<div class="sp-compose-line"><span class="sp-caret">&gt;</span>` +
    `<span class="sp-compose-ph">say something…</span></div>` +
    `</div>` +
    `</footer>` +
    `</div>` +
    `</div>`
  );
}
