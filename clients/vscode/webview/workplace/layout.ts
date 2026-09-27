/** Where everyone stands, worked out before a single rectangle is drawn.
 *
 *  The hard part of this view is not the art, it is that two rules fight each other: a worker
 *  stands in the ROOM their work is in, and a report stands WITH its lead. A researcher out at the
 *  mast is doing both — they are outside the building and they still answer to a manager on the
 *  first floor.
 *
 *  Resolved by making the pod a thing you can be AWAY from. A pod is anchored where its lead is;
 *  members in that same room stand on the lead's platform, and a member somewhere else stands in
 *  their own room on a small platform in the lead's colour, while the lead's platform keeps their
 *  empty footprint with an arrow to where they went. Nothing is drawn between rooms: at fifteen
 *  workers a wire per relationship is the node graph this view exists to not be.
 */
import { reportsOf } from "../../src/team";
import { assignAccents, atSeconds } from "./palette";
import { isHeld } from "./status";
import type { TeamState, Worker, ZoneId } from "../../src/team";

/** Floors, top to bottom. The ground floor is the one with the door, so the rooms a worker is
 *  sent to are upstairs and the way out is at the bottom — the building reads as a route. */
export const FLOORS: readonly (readonly ZoneId[])[] = [
  ["library", "studio", "lab"],
  ["code", "managers", "data"],
  ["idle", "entry"],
];

/** Outside the building. Kept out of FLOORS because it's not a room and must not inherit a
 *  room's walls. */
export const OUTDOOR: ZoneId = "web";

export interface Member {
  worker: Worker;
  /** 0 is the lead. Depth is drawn as size, so a chain of three reads without any indentation. */
  depth: number;
}

export interface Pod {
  lead: Worker;
  /** The lead first, then everyone under them, breadth-first — the order they were sent out. */
  members: Member[];
  /** var(--wp-hN), taken from the lead's id so a pod keeps its colour across snapshots. */
  accent: string;
}

/** Everyone, arranged into pods. A worker whose parent is not in the snapshot is its own lead —
 *  a sub-agent of a run that has already been forgotten is still a person standing in a room. */
export function buildPods(state: TeamState): Pod[] {
  const all = state.workers;
  const byId = new Map(all.map((w) => [w.run_id, w]));
  const isLead = (w: Worker) => !w.parent_run_id || !byId.has(w.parent_run_id);

  const heads = all.filter(isLead);
  const accents = assignAccents(heads.map((w) => w.run_id));
  const pods: Pod[] = [];
  for (const lead of heads) {
    const members: Member[] = [{ worker: lead, depth: 0 }];
    // Breadth-first so a lead's own reports come before their reports' reports, which is the
    // order a person would name them in.
    for (let i = 0; i < members.length; i++) {
      const { worker, depth } = members[i];
      if (depth >= 3) continue; // a fourth level has never happened; guard the cycle, not the case
      for (const child of reportsOf(all, worker.run_id)) {
        if (members.some((m) => m.worker.run_id === child.run_id)) continue;
        members.push({ worker: child, depth: depth + 1 });
      }
    }
    pods.push({ lead, members, accent: accents.get(lead.run_id) ?? "var(--wp-h1)" });
  }
  return pods;
}

export interface RoomPlan {
  zone: ZoneId;
  /** Pods whose LEAD is standing here — drawn in full, with away-markers for absent members. */
  anchored: Pod[];
  /** Members standing here whose lead is somewhere else — drawn small, in their lead's colour. */
  visiting: Member[];
  /** Everyone actually present, which is what sizes the room. */
  headcount: number;
}

export function planRooms(pods: Pod[]): Map<ZoneId, RoomPlan> {
  const plans = new Map<ZoneId, RoomPlan>();
  const room = (zone: ZoneId): RoomPlan => {
    let plan = plans.get(zone);
    if (!plan) {
      plan = { zone, anchored: [], visiting: [], headcount: 0 };
      plans.set(zone, plan);
    }
    return plan;
  };

  for (const pod of pods) {
    room(pod.lead.zone).anchored.push(pod);
    for (const member of pod.members) {
      room(member.worker.zone).headcount++;
      if (member.depth > 0 && member.worker.zone !== pod.lead.zone) {
        room(member.worker.zone).visiting.push(member);
      }
    }
  }
  return plans;
}

/** Members of this pod standing in the pod's own room, and those who are not. */
export function splitPod(pod: Pod): { here: Member[]; away: Member[] } {
  const here: Member[] = [];
  const away: Member[] = [];
  for (const m of pod.members) {
    if (m.depth === 0) continue; // the lead is drawn separately, always first
    (m.worker.zone === pod.lead.zone ? here : away).push(m);
  }
  return { here, away };
}

export interface Tally {
  running: number;
  done: number;
  error: number;
  foreign: number;
  idle: number;
  cost: number;
  projects: string[];
}

/** The board by the door: how the shift is going.
 *
 *  idle is HELD — a run still open, not moved for two minutes. Used to be counted across every
 *  status, putting finished and foreign runs in the count: a DONE run's idle clock is only how
 *  long ago it ended, and the desk never counted those. One rule, in status.ts, so the two
 *  panels can't report different numbers for the same fact. */
export function tally(state: TeamState): Tally {
  const t: Tally = { running: 0, done: 0, error: 0, foreign: 0, idle: 0, cost: 0, projects: [] };
  const seen = new Set<string>();
  for (const w of state.workers) {
    if (w.status === "running") t.running++;
    else if (w.status === "done") t.done++;
    else if (w.status === "error") t.error++;
    else if (w.status === "foreign") t.foreign++;
    if (isHeld(w)) t.idle++;
    if (typeof w.cost_usd === "number") t.cost += w.cost_usd;
    if (w.project && !seen.has(w.project)) {
      seen.add(w.project);
      t.projects.push(w.project);
    }
  }
  return t;
}

/* ── who is talking to whom ── */

/** A message between two workers, as the view needs it: a stable key so the same exchange is not
 *  re-announced on every refresh, and both ends guaranteed present (the registry filters those). */
export interface Post {
  from: string;
  to: string;
  text: string;
  key: string;
  /** How long ago this was said, in seconds, or null when the exchange predates the stamp.
   *
   *  This is what lets a COLD OPEN say anything at all. Without it the only way to tell a new
   *  exchange from an old one was "did we already draw it", which is empty on a first load — so
   *  the room opened silent and stayed silent until somebody spoke again, which is half of "no
   *  agents talk to each other". With an age, a first load can carry the conversations that have
   *  genuinely JUST happened and stay quiet about the rest. Null is unknown, never zero. */
  age: number | null;
}

interface LinkLike {
  from_run_id: string;
  to_run_id: string;
  text: string;
  at?: number | null;
}

export function posts(state: TeamState): Post[] {
  const links = (state as { links?: LinkLike[] }).links;
  if (!Array.isArray(links)) return [];
  const here = new Set(state.workers.map((w) => w.run_id));
  const seen = new Set<string>();
  const out: Post[] = [];
  for (const l of links) {
    if (!l || !here.has(l.from_run_id) || !here.has(l.to_run_id)) continue;
    const key = `${l.from_run_id}>${l.to_run_id}:${l.text}`;
    if (seen.has(key)) continue;
    seen.add(key);
    // Both stamps normalised before subtracting: the snapshot and the message reach this from
    // different writers, and one of them being in the other's unit is a 50-year-old message.
    const age =
      typeof l.at === "number" && Number.isFinite(l.at)
        ? Math.max(0, atSeconds(state.at) - atSeconds(l.at))
        : null;
    out.push({ from: l.from_run_id, to: l.to_run_id, text: l.text ?? "", key, age });
  }
  return out;
}

/** Per worker: how much post they are in the middle of, and with whom — so a person in the middle
 *  of a conversation is marked even on a cold load, when there is no arrival to animate. */
export function traffic(list: Post[], workers: Worker[]): Map<string, { n: number; who: string[] }> {
  const name = new Map(workers.map((w) => [w.run_id, w.name]));
  const map = new Map<string, { n: number; who: string[] }>();
  const add = (id: string, other: string) => {
    const entry = map.get(id) ?? { n: 0, who: [] };
    entry.n++;
    const label = name.get(other);
    if (label && !entry.who.includes(label)) entry.who.push(label);
    map.set(id, entry);
  };
  for (const p of list) {
    add(p.from, p.to);
    add(p.to, p.from);
  }
  return map;
}
