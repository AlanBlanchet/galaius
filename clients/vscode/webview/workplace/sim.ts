/** The simulation: bodies that occupy tiles, a camera that watches them, and a floor that is
 *  never completely still.
 *
 *  Position belongs to the engine, not to the document. A body owns a tile coordinate; the DOM
 *  element is only where it happens to be drawn this frame. From that one inversion everything
 *  else follows:
 *
 *   - **Routes are computed, not guessed.** A breadth-first search over the walkability grid finds
 *     the way out of a room, along the hall and in through another room's door. It cannot cut
 *     through a wall or stand inside a desk, because those cells are simply not in the graph.
 *   - **Movement has a cause and it is usually INTERACTION.** One agent messaging another sends
 *     the sender across the building to say it and back again. Nobody teleports.
 *   - **Nothing is ever still.** Everyone has an errand loop of their own, staggered per person.
 *   - **Footfall is keyed to DISTANCE, not to a clock**, so a body that speeds up keeps its stride.
 *
 *  What is new here is the CAMERA and the two things it pays for.
 *
 *  The view this replaces scaled the whole building down until it fitted the panel, which in the
 *  side bar it actually ships in meant half scale: eighteen-pixel people and six-pixel capability
 *  marks. Fitting everything into frame is the one thing a game never does. So the frame is now
 *  bounded and the camera goes to the work — a weighted centroid of whoever is walking, talking or
 *  delivering, chased through a dead zone so it never jitters and never chases noise.
 *
 *  Because only eight or nine people are on screen at a time, two things become affordable that
 *  were not: the capability marks can be drawn at a size somebody can read, and everyone's line
 *  can be SHOWN rather than hidden behind a hover. The bubbles are placed here, on three levels,
 *  and any that would collide with another bubble or with somebody's nameplate is simply not
 *  shown — which is a guarantee, not a hope, because it is computed.
 */

export const SIM = String.raw`
/* ── the clock ─────────────────────────────────────────────────────────────────────────────── */
var BEAT = 2400;
var TICK = { t: 0, dt: 0, on: false };

/* Tiles per second. Slow enough to watch somebody cross a room, fast enough that a trip across
   the building is not a commute. */
var PACE = 3.6;
/* Half a tile per footfall: two frames per tile walked. */
var STRIDE = 0.5;
/* How long a body pauses once it gets where it was going. */
/* How long a settled body rests before it even CONSIDERS an ambient stroll. This was
   [900, 2600] — every stander re-rolled a walk roughly every two seconds, forty-five actors in
   perpetual motion, and the owner could not land a click ("the agents are moving too much").
   A minute-plus of stillness keeps the floor alive without making it a beehive; task walks
   (spawns, errands, couriers) are scheduled elsewhere and stay immediate. */
var DWELL = [45000, 110000];

function hash(s) {
  var h = 2166136261;
  for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = (h * 16777619) >>> 0; }
  return h >>> 0;
}
function phaseOf(id) { return (hash(id) % 997) / 997; }
function rnd(id, n) { return (hash(id + ":" + n) % 10000) / 10000; }

/* ── the world, as data ──────────────────────────────────────────────────────────────────────
   Handed over by the renderer rather than measured out of the DOM. A tile grid IS the geometry,
   so there is nothing to read off a bounding box and nothing that can go stale on a resize. */
var W = { cols: 0, rows: 0, tile: 32, solid: null, rooms: [], byId: {}, gate: [0, 0], ok: false };

function readWorld() {
  var el = document.querySelector(".wp-world");
  if (!el) { W.ok = false; return; }
  W.cols = Number(el.getAttribute("data-cols")) || 0;
  W.rows = Number(el.getAttribute("data-rows")) || 0;
  W.tile = Number(el.getAttribute("data-tile")) || 32;
  var bits = el.getAttribute("data-solid") || "";
  W.solid = new Uint8Array(W.cols * W.rows);
  for (var i = 0; i < W.solid.length; i++) W.solid[i] = bits.charCodeAt(i) === 49 ? 1 : 0;
  try { W.rooms = JSON.parse(el.getAttribute("data-rooms") || "[]"); } catch (e) { W.rooms = []; }
  W.byId = {};
  for (var r = 0; r < W.rooms.length; r++) W.byId[W.rooms[r].i] = W.rooms[r];
  var g = (el.getAttribute("data-gate") || "0,0").split(",");
  W.gate = [Number(g[0]), Number(g[1])];
  W.ok = W.cols > 0 && W.rows > 0;
}

function walkable(x, y) {
  if (x < 0 || y < 0 || x >= W.cols || y >= W.rows) return false;
  return !W.solid[y * W.cols + x];
}

/* ── routing ─────────────────────────────────────────────────────────────────────────────────
   Breadth-first over the four neighbours. The grid is a couple of thousand cells, so an exact
   search costs less than the heuristic would, and it is exact: if there is a way through the
   doors it is found, and if there is not, the body simply does not set off. */
function route(ax, ay, bx, by) {
  if (ax === bx && ay === by) return [];
  var n = W.cols * W.rows;
  var prev = new Int32Array(n).fill(-1);
  var start = ay * W.cols + ax, goal = by * W.cols + bx;
  if (!walkable(bx, by)) return null;
  var q = [start];
  prev[start] = start;
  var head = 0;
  while (head < q.length) {
    var cur = q[head++];
    if (cur === goal) break;
    var cx = cur % W.cols, cy = (cur / W.cols) | 0;
    for (var d = 0; d < 4; d++) {
      var nx = cx + (d === 0 ? 1 : d === 1 ? -1 : 0);
      var ny = cy + (d === 2 ? 1 : d === 3 ? -1 : 0);
      if (!walkable(nx, ny)) continue;
      var ni = ny * W.cols + nx;
      if (prev[ni] !== -1) continue;
      prev[ni] = cur;
      q.push(ni);
    }
  }
  if (prev[goal] === -1) return null;
  var out = [], at = goal;
  while (at !== start) { out.push({ x: at % W.cols, y: (at / W.cols) | 0 }); at = prev[at]; }
  out.reverse();
  return out;
}

/* A free cell inside a room: somewhere to wander to that is not a wall, a desk or the seat you
   just left. Deterministic per body and per attempt. */
function looseCell(room, id, n) {
  if (!room) return null;
  for (var tries = 0; tries < 14; tries++) {
    var dx = 1 + Math.floor(rnd(id, n * 13 + tries) * (room.w - 2));
    var dy = 1 + Math.floor(rnd(id, n * 29 + tries + 7) * (room.h - 2));
    var x = room.x + dx, y = room.y + dy;
    /* An open room is still a ROOM. This let an outdoor body pick any row in the entire world,
       so an agent working the web wandered the full height of the site and stood in a field — the
       outdoor half of "sprites are very weirdly placed". Its own rectangle, like everybody. */
    if (room.o) { y = room.y + 1 + Math.floor(rnd(id, n * 31 + tries) * Math.max(1, room.h - 2)); }
    if (walkable(x, y) && !claimed(id, x, y)) return { x: x, y: y };
  }
  return null;
}

/* Strolls must not converge: two loose cells are picked independently, so nothing stopped two
   strollers dwelling on the same spot, drawn through each other — the crowding rule enforced for
   every DECLARED place, lost for the wandering ones. A cell is CLAIMED when another body stands
   or will arrive within the same THREE-tile pitch every declared place honours: nameplates are
   sixty-six pixels wide over twenty-four-pixel tiles, so a two-tile gap still fuses the labels
   and two-at-two-tiles was exactly the crowding an independent verdict caught. */
function claimed(id, x, y) {
  for (var k in BODIES) {
    if (k === id) continue;
    var o = BODIES[k];
    if (!o) continue;
    var tx = o.path ? o.path[o.path.length - 1].x : o.x;
    var ty = o.path ? o.path[o.path.length - 1].y : o.y;
    if (Math.abs(tx - x) < 3 && Math.abs(ty - y) < 3) return true;
    /* THE SEAT IS CLAIMED WHILE ITS OWNER IS AWAY. Positions and destinations only cover the
       bodies that are THERE or on their way — a stroller could lawfully dwell on the exact cell
       of a seat whose owner was out walking, and the owner's return is unconditional (it is
       their seat), so the two settled at literally zero distance: one sprite hidden entirely
       under the other, two nameplates over one body. A seat is a standing promise to return;
       it is claimed whether or not anybody is currently on it. */
    if (Math.abs(o.seatX - x) < 3 && Math.abs(o.seatY - y) < 3) return true;
  }
  return false;
}

/* A cell beside somebody — where you stand when you have come over to say something.
   TWO TILES OUT FIRST, not one. A sprite is thirty-six pixels wide on a twenty-four pixel tile,
   so "adjacent" means the two of them overlap by twelve and one is drawn through the other —
   which is the same crowding complaint the seat pitch exists to prevent, arriving through the
   errand system instead of through the floor plan. Two tiles is clear of it and still plainly a
   conversation. The closer ring stays as a fallback for somebody cornered against furniture. */
function besideOf(x, y) {
  var around = [[2, 0], [-2, 0], [0, 2], [0, -2], [2, 1], [-2, 1], [2, -1], [-2, -1],
                [1, 2], [-1, 2], [1, -2], [-1, -2],
                [1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [-1, 1], [1, -1], [-1, -1]];
  /* First choice: a spot no OTHER seat lays claim to — a courier standing on the empty chair
     beside the recipient is in somebody's place the moment they walk back. The bare-walkable
     ring stays as the fallback for somebody genuinely cornered against furniture. */
  for (var i = 0; i < around.length; i++) {
    var nx = x + around[i][0], ny = y + around[i][1];
    if (walkable(nx, ny) && !seatAt(nx, ny)) return { x: nx, y: ny };
  }
  for (var j = 0; j < around.length; j++) {
    var fx = x + around[j][0], fy = y + around[j][1];
    if (walkable(fx, fy)) return { x: fx, y: fy };
  }
  return null;
}

/** Is this exact cell some body's seat? The courier ring only needs the literal cell — a ring
 *  spot NEAR a seat is a moment of standing close, which a conversation is. */
function seatAt(x, y) {
  for (var k in BODIES) {
    var o = BODIES[k];
    if (o && o.seatX === x && o.seatY === y) return true;
  }
  return false;
}

/* ── bodies ──────────────────────────────────────────────────────────────────────────────────
   One per worker. Position is in TILES and is a float while walking; the element is placed from
   it every frame and never the other way round. */
var BODIES = {};
var CAST = null;

function bodyFor(el) {
  var id = el.getAttribute("data-run-id");
  var b = BODIES[id];
  var seat = (el.getAttribute("data-seat") || "0,0").split(",");
  var sx = Number(seat[0]), sy = Number(seat[1]);
  if (!b) {
    b = BODIES[id] = {
      id: id, x: sx, y: sy, seatX: sx, seatY: sy,
      face: 1, wx: 1, wy: 0, path: null, step: 0, walked: 0, speed: 0, sx: 0, tx: 0, tagW: 0,
      mode: "settled", nextAt: 0, home: "", talkUntil: 0, errand: null,
      saidAt: 0, phase: phaseOf(id)
    };
    /* Staggered from the start, and seeded from the SAME dwell the settled loop uses — this
       was a flat 2-11s, so the whole cast took its first stroll inside the first ten seconds
       however calm the dwell was afterwards. Fifteen bodies deciding on one frame is the
       loudest tell that a scene is driven by a stylesheet rather than by people. */
    b.nextAt = DWELL[0] + b.phase * (DWELL[1] - DWELL[0]);
  }
  /* A REFRESH REPLACES THE ELEMENT AND KEEPS THE BODY, so every value memoised on the body about
     what is WRITTEN on the element is stale the instant the two are re-paired. The --sx custom
     property is an inline style on the bubble: the new bubble arrives without it while b.sx still
     remembers the old number, so the clamp compares equal to itself and writes nothing — a line
     correctly shifted before the swap comes back unshifted after it and hangs off the panel.
     Anything cached about the DOM is dropped when the DOM changes under it. */
  if (b.el !== el) {
    b.sx = 0; b.tx = 0; b.tagW = 0;
    b.paintTransform = null; b.paintDepth = null; b.edgeKey = null;
    b.paintLeft = null; b.paintLean = null;
  }
  b.el = el;
  b.home = el.getAttribute("data-home") || "";
  if (!b.path && Math.abs(b.x - sx) < 0.5 && Math.abs(b.y - sy) < 0.5) settleFace(b);
  /* A seat that moved means the roster moved them — a new department, or out to the web. They do
     not appear there; they get up and walk. */
  if (b.seatX !== sx || b.seatY !== sy) {
    b.seatX = sx; b.seatY = sy;
    if (b.mode !== "errand") sendTo(b, sx, sy, "commute");
  }
  return b;
}

function roomOf(b) { return W.byId[b.home] || null; }

/* Which way a body looks once it has stopped.
   Walking sets the facing from the direction of travel, which is right while the feet are moving
   and meaningless the moment they stop — so a body kept whichever way it happened to arrive, and
   two people sharing one bench ended up looking the same way like a queue. The SEAT declares it,
   because the seat is the thing that knows what is beside it. */
function settleFace(b) {
  if (!b.el) return;
  var want = Number(b.el.getAttribute("data-face"));
  if (want === 1 || want === -1) b.face = want;
}

/** Put a body on the road. Everything that moves anybody goes through here, so there is exactly
 *  one place where a journey can start and exactly one shape a journey has. */
function sendTo(b, x, y, mode) {
  if (!W.ok) return false;
  var from = { x: Math.round(b.x), y: Math.round(b.y) };
  var pts = route(from.x, from.y, x, y);
  if (!pts || !pts.length) return false;
  b.path = pts;
  b.step = 0;
  b.walked = 0;
  b.speed = 0;
  b.mode = mode || "walk";
  if (b.el) b.el.classList.add("is-walking");
  window.__wp.walks++;
  return true;
}

/* ── walking ─────────────────────────────────────────────────────────────────────────────────
   Constant pace with an acceleration at the start and a brake at the end, so a body leans into a
   journey and settles out of it. The stride is measured in DISTANCE walked, which is what keeps
   the feet under the person when the speed changes. */
function stepWalk(b, dt) {
  /* A body under the pointer HOLDS STILL, mid-journey included — "the agents are moving too
     much, I can't click on them". The walk resumes the moment the pointer leaves; only this
     body pauses, so the floor keeps living around it. */
  if (HAND.on && Math.abs(HAND.tx - b.x) <= GAZE && Math.abs(HAND.ty - b.y) <= GAZE) {
    b.speed = 0;
    return;
  }
  var want = PACE;
  if (b.walked < 0.35) want = PACE * (0.35 + (b.walked / 0.35) * 0.65);
  if (b.path.length - b.step <= 1) want = PACE * 0.55;
  b.speed += (want - b.speed) * Math.min(1, dt / 90);

  /* The frame's whole allowance, spent ACROSS waypoints rather than stopping at each one. A route
     through the doors is twenty-odd legs, and consuming only up to the next corner threw away a
     fraction of a frame at every one of them — a body that hesitates at every corner is being
     dragged along a path rather than walking. */
  var move = (b.speed * dt) / 1000;
  var guard = 0;
  while (move > 0 && b.path && guard++ < 64) {
    var target = b.path[b.step];
    var dx = target.x - b.x, dy = target.y - b.y;
    var dist = Math.sqrt(dx * dx + dy * dy);
    if (Math.abs(dx) > 0.02) b.face = dx > 0 ? 1 : -1;
    /* Which way this leg actually runs, for the facing: the sprite drawn is the side gait on a
       horizontal leg, the back on the way up, the front on the way down. */
    if (dist > 0.02) { b.wx = dx; b.wy = dy; }
    if (dist > move) {
      b.x += (dx / dist) * move;
      b.y += (dy / dist) * move;
      b.walked += move;
      return;
    }
    b.x = target.x;
    b.y = target.y;
    b.walked += dist;
    move -= dist;
    b.step++;
    if (b.step >= b.path.length) {
      b.path = null;
      if (b.el) b.el.classList.remove("is-walking");
      arrived(b);
      return;
    }
  }
}

function arrived(b) {
  if (b.mode === "errand" && b.errand) {
    /* Delivered. Both of them stop and turn to each other for a moment — a message that lands
       with nobody reacting is a note flying past a person rather than to one. */
    var them = BODIES[b.errand.to];
    b.talkUntil = TICK.t + 1500;
    if (b.el) b.el.classList.add("is-talking");
    if (them) {
      them.talkUntil = TICK.t + 1500;
      if (them.el) { them.el.classList.add("is-talking"); them.el.classList.add("has-post"); }
      them.face = them.x > b.x ? -1 : 1;
      b.face = b.x > them.x ? -1 : 1;
    }
    if (b.el && b.errand.text) {
      b.el.setAttribute("data-say", b.errand.text);
      var line = b.el.querySelector(".wp-say b");
      if (line) line.textContent = b.errand.text;
      b.saidAt = TICK.t;
    }
    b.mode = "delivering";
    b.nextAt = TICK.t + 1500;
    return;
  }
  b.mode = "settled";
  /* Home, and facing the way the place faces — but only at the place itself. Turning to face a
     bench you are merely walking past is worse than not turning at all. */
  if (Math.abs(b.x - b.seatX) < 0.5 && Math.abs(b.y - b.seatY) < 0.5) settleFace(b);
  b.nextAt = TICK.t + DWELL[0] + rnd(b.id, Math.floor(TICK.t / 1000)) * (DWELL[1] - DWELL[0]);
}

/* ── what a body decides to do next ──────────────────────────────────────────────────────────
   The part that makes the building alive whether or not anything happened. Nobody is told to
   move by the data here: they get up because it has been a while, cross their own room, look at
   what is on the wall, and come back. */
function decide(b, t) {
  if (t < b.nextAt) return;
  if (b.mode === "delivering") {
    if (b.el) b.el.classList.remove("is-talking");
    var them = b.errand && BODIES[b.errand.to];
    if (them && them.el) { them.el.classList.remove("is-talking"); them.el.classList.remove("has-post"); }
    b.errand = null;
    b.mode = "settled";
    if (!sendTo(b, b.seatX, b.seatY, "return")) b.nextAt = t + 3000;
    return;
  }
  if (b.el && b.el.getAttribute("data-status") === "error") { b.nextAt = t + 6000; return; }
  /* A worker the registry says has stopped stops. Absence of motion is the loudest possible way
     to say it, and much cheaper to read than a timestamp. */
  if (b.el && b.el.getAttribute("data-stalled") === "1") { b.nextAt = t + 8000; return; }

  var away = Math.abs(b.x - b.seatX) > 0.5 || Math.abs(b.y - b.seatY) > 0.5;
  var n = Math.floor(t / 1000);
  if (away && rnd(b.id, n) < 0.6) { if (sendTo(b, b.seatX, b.seatY, "return")) return; }
  /* SOMEBODY WHO HAS SAT DOWN STAYS SAT. "They could have a seat or rest in their room" is a
     statement about where a finished agent IS, and the errand loop was undoing it: everybody got
     up on their own schedule, so a run that had finished spent most of its life standing in the
     middle of the floor near a couch rather than on it. Seated postures still get up — a floor
     where nothing ever moves is the other failure — but rarely, and they always come back. */
  if (b.el && b.el.getAttribute("data-posture") !== "stand" && rnd(b.id, n * 7 + 3) > 0.18) {
    b.nextAt = t + 30000;
    return;
  }
  var cell = looseCell(roomOf(b), b.id, n);
  if (cell && sendTo(b, cell.x, cell.y, "roam")) return;
  b.nextAt = t + 20000;
}

/* ── nameplates that never sit on each other ─────────────────────────────────────────────────
   One pass over the whole cast per frame: walk them in reading order and give each plate the
   lowest row no already-placed NEIGHBOUR is using. Neighbourhood is horizontal, because that is
   the only direction a plate can collide in — and it is measured in TILES converted from the
   plate's SCREEN width, since pulled back the plates hold constant screen size while the floor
   shrinks (that mismatch is exactly what defeated the previous per-body formula).

   Formulas were tried twice and both had blind spots: DOM parity says nothing about who stands
   where, and every modulus over tile coordinates leaves some offset sharing a row. Only a pass
   that can SEE the neighbours gets this right, and 45 bodies is nothing to sort. */
var TAG_ROWS = 3;
var DECLUTTER_KEY = "";
function declutter() {
  var list = [];
  for (var id in BODIES) {
    var b = BODIES[id];
    if (b.el && b.el.isConnected) list.push(b);
  }
  var key = (VIEW.zoom || 1) + ":" + list.map(function (body) {
    return body.id + "@" + body.x.toFixed(2) + "," + body.y.toFixed(2);
  }).join("|");
  if (key === DECLUTTER_KEY) return;
  DECLUTTER_KEY = key;
  // Reading order: whoever is higher up the floor picks first, so the choice is stable frame to
  // frame and a plate does not flicker between rows while its owner walks.
  list.sort(function (p, q) { return p.y - q.y || p.x - q.x || (p.id < q.id ? -1 : 1); });
  // A plate is ~40px wide on screen pulled back, ~66 world px near. Convert to tiles at the
  // CURRENT zoom so the neighbourhood grows as the floor shrinks.
  var z = VIEW.zoom || 1;
  var reach = (z < 1 ? 44 / (W.tile * z) : 70 / W.tile) * 0.5 + 0.5;
  var placed = [];
  for (var i = 0; i < list.length; i++) {
    var b2 = list[i];
    var used = 0;
    for (var j = placed.length - 1; j >= 0; j--) {
      var o = placed[j];
      if (b2.y - o.y > 1.5) break;               // far enough up the floor to stop looking
      if (Math.abs(b2.x - o.x) < reach) used |= 1 << o.row;
    }
    var row = 0;
    while (row < TAG_ROWS - 1 && (used & (1 << row))) row++;
    b2.row = row;
    placed.push(b2);
    var ph = String(row);
    if (b2.el.dataset.ph !== ph) b2.el.dataset.ph = ph;
  }
}

/* ── drawing a body ──────────────────────────────────────────────────────────────────────────
   One transform per actor per frame, plus a z-index so somebody standing further down the room
   is in front of somebody standing further up it. Depth ordering is the cheapest thing that makes
   a flat grid read as a floor. */
function place(b, t) {
  var el = b.el;
  if (!el) return;
  var px = b.x * W.tile + W.tile / 2;
  var py = b.y * W.tile + W.tile;
  b.px = px;
  b.py = py;
  var bob = 0, lean = 0;
  if (b.path) {
    /* Footfall: a half tile per frame flip, so the gait belongs to the DISTANCE covered and the
       feet stay under the person when the speed changes. Four phases — contact, passing,
       contact, passing — and the BOB now lives in the passing frames' own pixels, so the element
       is not bounced on top of it. Which sprite plays is the leg's direction: the side gait for
       a horizontal leg, the back walking away, the front coming toward you. */
    var stepI = Math.floor(b.walked / STRIDE) % 4;
    var dir = Math.abs(b.wx) >= Math.abs(b.wy) ? "x" : b.wy < 0 ? "n" : "s";
    var stepS = String(stepI);
    if (el.getAttribute("data-step") !== stepS) el.setAttribute("data-step", stepS);
    if (el.getAttribute("data-dir") !== dir) el.setAttribute("data-dir", dir);
    /* Five degrees, not 1.5: the lean into the travel is what says HEADED somewhere rather than
       drifting, and at 1.5° it measured real and read as nothing — under the waddle's own ±7°
       swing it was noise. At 5° the swing sits asymmetric around the direction of travel
       (-2°..+12° eastbound), which is the lean-into-the-run every small-sprite gait carries. */
    lean = dir === "x" ? b.face * 5 : 0;
  } else {
    var status = el.getAttribute("data-status");
    if (status === "error") {
      bob = 1;
    }
    /* Noticed: a body watching the hand leans toward it. The mirror flip alone is nearly
       invisible on front-facing art; four degrees of lean is what makes a room full of resting
       people visibly TURN with the pointer. */
    if (b.gazing) lean = b.face * 4;
  }
  var transform = "translate3d(" + px.toFixed(1) + "px," + (py + bob).toFixed(1) + "px,0)";
  if (b.paintTransform !== transform) {
    el.style.transform = transform;
    b.paintTransform = transform;
  }
  var depth = String(100 + Math.round(b.y * 4));
  if (b.paintDepth !== depth) {
    el.style.zIndex = depth;
    b.paintDepth = depth;
  }
  // The nameplate row is assigned by declutter() once per frame across the WHOLE cast —
  // a per-body formula cannot see its neighbours. (Tile parity was tried: every modulus has
  // blind spots at some offset, and a calm floor never strolls out of the collision.)
  var edgeKey = [py, VIEW.x, VIEW.y, VIEW.zoom, CAM.vw, CAM.vh].join(":");
  if (b.edgeKey !== edgeKey) {
    sayEdge(b, py);
    b.edgeKey = edgeKey;
  }
  var left = b.face < 0;
  if (b.paintLeft !== left) {
    el.classList.toggle("face-left", left);
    b.paintLeft = left;
  }
  var leanValue = lean ? lean.toFixed(2) + "deg" : "";
  if (b.paintLean !== leanValue) {
    if (leanValue) el.style.setProperty("--lean", leanValue);
    else el.style.removeProperty("--lean");
    b.paintLean = leanValue;
  }
}

/** A BUBBLE MAY NOT FALL OFF THE FRAME.
 *
 *  A line is centred on its body and held at a constant SCREEN size, so a body within half a
 *  bubble of an edge has its sentence cut in half by the viewport — and in the width this
 *  actually ships in, a VS Code side bar, most of the floor is within half a bubble of an edge.
 *
 *  It has to be done HERE, per frame, and that is the whole lesson: the first attempt clamped in
 *  the speech LAYOUT, which runs every 420ms, so the shift was computed against a camera position
 *  up to a quarter of a second old while the body walked and the camera followed. It applied a
 *  real, measurable, wrong offset — a bubble still off the frame having visibly moved, which is
 *  worse than not moving at all. A correction to a moving quantity belongs on the same clock as
 *  the quantity.
 *
 *  Only for a body actually ON screen: dragging an off-frame character's line to the edge shows a
 *  sentence belonging to somebody nobody can see.
 */
/** Where a body is ON SCREEN, in screen pixels, against the camera that is actually PAINTED.
 *  ONE predicate, used by both the thing that decides who speaks and the thing that decides where
 *  the line goes — they disagreed by an eighteen-pixel pad, so the layout kept re-granting a
 *  bubble to a body the per-frame check had just taken it from, and the two of them handed it back
 *  and forth while the sentence hung eighty pixels outside the panel. */
function screenAt(b, py) {
  var seen = VIEW.ready ? VIEW : CAM;
  return { x: (b.px - seen.x) * seen.zoom, y: ((py === undefined ? b.py : py) - seen.y) * seen.zoom };
}

function onScreenAt(at) {
  return at.x >= 0 && at.x <= CAM.vw && at.y >= 0 && at.y <= CAM.vh;
}

function onScreen(b, py) {
  return onScreenAt(screenAt(b, py));
}

/** How far a label must move, in SCREEN pixels, to sit inside the frame. Zero when it already
 *  does. The half argument is half the label's own width, on screen. */
function edgeShift(sx, half) {
  if (sx - half < SAY_EDGE) return SAY_EDGE - (sx - half);
  if (sx + half > CAM.vw - SAY_EDGE) return CAM.vw - SAY_EDGE - (sx + half);
  return 0;
}

/** THE NAMEPLATE IS THE BUBBLE'S SIBLING AND NEEDS THE SAME CLAMP.
 *
 *  Reported by a critic immediately after the bubble clamp shipped, and it is the same defect
 *  with a different class name: a name is centred on its body, so a body near an edge has its
 *  name cut in half — measured at 26.5px of "code-reviewer" off the left of a side bar, for
 *  somebody whose body was still fully on screen. Fixing one instance of a class and leaving its
 *  sibling is the whole failure.
 *
 *  What differs is the SPACE. A bubble counter-scales (--inv) and is a constant screen object, so
 *  its shift is a screen distance; a nameplate scales WITH the world, so the same screen distance
 *  is that many world pixels divided by the zoom. Its width is measured off the rendered element
 *  once per element, because the cap is 66 world pixels and most names are shorter — shifting a
 *  short name by a long name's overhang moves it visibly too far in.
 */
function tagEdge(b, at, zoom) {
  var el = b.el;
  var tag = el.querySelector(".wp-tag");
  if (!tag) return;
  if (!b.tagW) b.tagW = tag.offsetWidth || 0;
  if (!b.tagW) return;
  /* ONLY FOR A BODY THAT IS ON SCREEN — and this guard is the whole reason to write the check
     once and share it. The bubble had it; the nameplate, copied from the bubble a minute later,
     did not, so every name in the building was clamped, INCLUDING the ones hundreds of pixels
     outside the frame: they were all dragged to the same edge and stacked into a pile of
     unreadable plates. Generalising a fix to its sibling means generalising its GUARD too. */
  var shift = onScreenAt(at) ? Math.round(edgeShift(at.x, (b.tagW / 2) * zoom) / zoom * 10) / 10 : 0;
  if (shift === b.tx) return;
  b.tx = shift;
  tag.style.left = shift + "px";
}

function sayEdge(b, py) {
  var el = b.el;
  var seenAt = screenAt(b, py);
  tagEdge(b, seenAt, VIEW.ready ? VIEW.zoom : CAM.zoom);
  if (!el.classList.contains("is-saying")) {
    if (b.sx) { b.sx = 0; var off = el.querySelector(".wp-say"); if (off) off.style.removeProperty("--sx"); }
    return;
  }
  var at = seenAt;
  var sx = at.x;
  /* WHO GETS A BUBBLE IS SETTLED HERE, EVERY FRAME — the layout pass only decides WHERE it goes.
     The layout runs every 420ms, and in 420ms a body walks a tile and a half and the camera can
     cross a room; a character that was on screen when the pass ran is off it long before the next
     one, still wearing the class, and what remains on the edge is the tail of a sentence with no
     owner. Measured over 220 frames at side-bar width, that was 173 pixels of a line hanging
     outside the frame. Cheap: two subtractions per speaking body per frame, and no DOM write
     unless the answer changed. */
  if (!onScreenAt(at)) {
    el.classList.remove("is-saying");
    return;
  }
  var shift = Math.round(edgeShift(sx, SAY_HALF));
  if (shift === b.sx) return;
  b.sx = shift;
  var say = el.querySelector(".wp-say");
  if (say) say.style.setProperty("--sx", shift + "px");
}

/* ── the camera ──────────────────────────────────────────────────────────────────────────────
   The building is bigger than the panel on purpose. Left alone the camera holds a weighted
   centroid of whoever is doing something — delivering a message outranks walking, walking
   outranks standing — and chases it through a DEAD ZONE, so it moves when the work moves and
   holds perfectly still when it does not.

   What is new here is that the SURVEY is the resting truth. The camera used to open FOLLOWING
   at a close rung, and following plus ambient walking is what ate aimed clicks: the floor
   panned under the pointer between the press and the release. Now the panel opens on the WHOLE
   FLOOR, following is opt-in, and the moment a pointer goes down the camera holds still.

   THE SCALE LADDER, which is the thing that makes pulling back possible at all. A tile is
   sixteen source pixels drawn at thirty-two, so one source pixel is two device pixels at zoom
   one. Every rung here is a HALF, which lands one source pixel on a whole number of device
   pixels at every rung — 1/2 draws it one wide, 1 draws it two, 3/2 three, up to six. A ladder
   of arbitrary fractions (0.75, 0.4) smears every hard edge this substrate is made of; halves
   are the ladder this art is exact on. The one licensed exception is the whole-floor FIT in a
   panel too narrow even for the bottom rung — seeing the company beats a perfect pixel there,
   and the far view is a signed plan, not something anybody reads sprites on. */
var CAM = {
  x: 0, y: 0, step: 2, zoom: 1, vw: 0, vh: 0, miniW: 0, miniH: 0,
  follow: false, pin: false, ready: false
};

/* ── THE CAMERA MOVES; IT DOES NOT CUT ───────────────────────────────────────────────────────
 *
 *  "The zoom could be smoother." It was not smooth in the strict sense: it was INSTANT. A rung is
 *  a third, so stepping one is a 33-50% change of scale applied in a single frame, and a mouse
 *  wheel emits one event per detent — so the whole ladder could be run in a flick, as nine cuts.
 *
 *  The fix is NOT a continuous scale. Every rung lands one source pixel on a whole number of
 *  device pixels, which is the only reason this scene stays crisp below 1x, and giving that up
 *  would smear every hard edge in the building to buy an animation. So the camera is split in
 *  two:
 *
 *    CAM   the LOGICAL camera. Rung-quantised, moved instantly by every handle there is — a
 *          wheel, a key, the rule, the plan, whole-floor. Every reading anything takes of the
 *          camera is this one, so the gauge lights the moment you touch it and the state the
 *          rest of the view derives (near or far, following or held) never flickers mid-move.
 *
 *    VIEW  what is actually PAINTED. It chases CAM every frame, and the chase is what you see.
 *
 *  Two details that matter more than the easing curve:
 *
 *   - SCALE IS CHASED IN LOG SPACE. Interpolating 1/3 to 3 linearly spends most of the move at
 *     the wide end and reads as a lurch; a constant ratio per unit time is what "zooming at an
 *     even rate" actually is.
 *   - IT LANDS EXACTLY, AND ROUNDS ONLY THERE. In flight the transform is left fractional,
 *     because snapping the translate to whole pixels while the scale is moving is a stutter you
 *     can see. Within a fifth of a percent of the target it snaps to the rung and rounds — so the
 *     picture is exact whenever it is still, which is when anybody is actually reading it.
 */
var VIEW = { x: 0, y: 0, zoom: 1, live: false, ready: false };
/* Time to close 99.9% of the gap, which for one rung works out at about 190ms of visible motion
   and about 300 for whole-floor — the classic UI transition band. Measured rather than guessed:
   the remaining gap is 0.001^(t/GLIDE), so a rung's 0.118 of log-scale reaches the 0.002 snap
   threshold at 0.59 x GLIDE. Short enough that a held key never feels laggy, long enough that a
   rung change is a MOVE and not a cut. */
var GLIDE = 320;
var STEP_MIN = 1;
var STEP_MAX = 6;
/* What the reader is told the scale is. A rung is a half, so every other one is a fraction and
   "0.5x" in a pixel-art building would be the only decimal on screen. */
var SCALE_WORDS = ["½", "1", "1½", "2", "2½", "3"];
/* What the panel is currently SHOWING, so the frame loop can skip a write it already made. */
var SHOWN = { far: "", follow: "", step: 0, read: "", live: "" };

function zoomOf(step) { return step / 2; }
function clampStep(s) { return Math.max(STEP_MIN, Math.min(STEP_MAX, Math.round(s) || STEP_MIN)); }
function worldPx() { return { w: W.cols * W.tile, h: W.rows * W.tile }; }

/** The BUILT extent, which is what "the whole company" actually means.
 *
 *  The world is deliberately bigger than the building — grounds on every side, so a tall narrow
 *  panel does not letterbox — and fitting the WORLD spends a whole rung of the ladder on
 *  grass. Measured against the rooms instead, the same 380px side bar frames 97% of the
 *  building where fitting the world framed 88% of a picture that is mostly lawn. Two tiles of
 *  verge, because a building drawn hard against the frame reads as cropped. */
var BUILT = null;
function builtPx() {
  if (BUILT) return BUILT;
  var rooms = W.rooms || [];
  if (!rooms.length) return (BUILT = worldPx());
  var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (var i = 0; i < rooms.length; i++) {
    var r = rooms[i];
    if (r.x < x0) x0 = r.x;
    if (r.y < y0) y0 = r.y;
    if (r.x + r.w > x1) x1 = r.x + r.w;
    if (r.y + r.h > y1) y1 = r.y + r.h;
  }
  x0 = Math.max(0, x0 - 2); y0 = Math.max(0, y0 - 2);
  x1 = Math.min(W.cols, x1 + 2); y1 = Math.min(W.rows, y1 + 2);
  BUILT = { x: x0 * W.tile, y: y0 * W.tile, w: (x1 - x0) * W.tile, h: (y1 - y0) * W.tile };
  return BUILT;
}

function camWeight(b) {
  if (!b.el || !b.el.isConnected) return 0;
  if (b.mode === "errand" || b.mode === "delivering") return 6;
  if (b.talkUntil > TICK.t) return 5;
  if (b.path) return 3;
  if (b.el.getAttribute("data-status") === "error") return 1.4;
  return 0.22;
}

/** The zoom at which the whole building is inside the frame. A pixel-exact rung when one fits;
 *  the TRUE fit — the one licensed fractional scale — when the panel is too narrow even for the
 *  bottom rung, because "see the whole company" is the survey's entire promise and the far view
 *  is a signed plan nobody reads sprites on. */
function camFitZoom() {
  var built = builtPx();
  if (!CAM.vw || !CAM.vh) return zoomOf(STEP_MIN);
  var fit = Math.min(CAM.vw / built.w, CAM.vh / built.h);
  var rung = Math.floor(fit * 2) / 2;
  if (rung >= zoomOf(STEP_MIN)) return Math.min(rung, zoomOf(STEP_MAX));
  return fit;
}

function camFitStep() {
  return clampStep(Math.round(camFitZoom() * 2));
}

/** Centre the built extent in the frame at the given zoom. The one place a non-rung zoom may be
 *  written, so every other handle stays quantised. */
function camFrameWhole() {
  var z = camFitZoom();
  CAM.zoom = z;
  CAM.step = clampStep(Math.round(z * 2));
  window.__wp.scale = z;
  var built = builtPx();
  var span = camSpan();
  CAM.x = built.x + (built.w - span.w) / 2;
  CAM.y = built.y + (built.h - span.h) / 2;
  camClamp();
}

function camFit() {
  var view = document.querySelector(".wp-view");
  if (!view) return;
  var box = view.getBoundingClientRect();
  if (!box.width || !box.height) return;
  CAM.vw = box.width;
  CAM.vh = box.height;
  /* This is the camera's one layout boundary: bind and resize arrive here before camApply writes
     the transformed stage. Reading the minimap after that write on every glide frame forced a
     synchronous layout — up to 23ms with 48 actors — although its size only changes here. */
  var mini = document.querySelector(".wp-mini");
  CAM.miniW = mini ? Math.max(0, mini.clientWidth - 4) : 0;
  CAM.miniH = mini ? Math.max(0, mini.clientHeight - 4) : 0;
  /* The hand's cached view origin moved with the layout; re-derive it on the next aim. */
  HAND.rok = false;
  if (!CAM.ready) {
    CAM.ready = true;
    /* Open on the occupied centre at a readable rung. The whole-floor survey remains one explicit
       button away; making it the default reduced people to coloured punctuation in a side bar. */
    CAM.step = Math.max(2, camFitStep());
    CAM.zoom = zoomOf(CAM.step);
    window.__wp.scale = CAM.zoom;
    if (!camSnap()) {
      var built = builtPx();
      var span = camSpan();
      CAM.x = built.x + (built.w - span.w) / 2;
      CAM.y = built.y + (built.h - span.h) / 2;
    }
  }
  camClamp();
  camApply();
}

function camSpan() {
  return { w: CAM.vw / CAM.zoom, h: CAM.vh / CAM.zoom };
}

function camClamp() {
  var world = worldPx();
  var span = camSpan();
  CAM.x = world.w <= span.w ? (world.w - span.w) / 2 : Math.max(0, Math.min(world.w - span.w, CAM.x));
  CAM.y = world.h <= span.h ? (world.h - span.h) / 2 : Math.max(0, Math.min(world.h - span.h, CAM.y));
}

/** Change scale while holding one point on the floor under the same pixel. Anchoring is what
 *  makes a wheel over the map read as a camera rather than as a jump: the desk you were looking
 *  at is still under the pointer afterwards. With no anchor the frame's own centre holds. */
function camSetStep(step, ax, ay) {
  var next = clampStep(step);
  if (next === CAM.step) return CAM.step;
  var view = document.querySelector(".wp-view");
  var box = view ? view.getBoundingClientRect() : null;
  var hx = box && typeof ax === "number" ? ax - box.left : CAM.vw / 2;
  var hy = box && typeof ay === "number" ? ay - box.top : CAM.vh / 2;
  /* Anchored on what is PAINTED, not on where the camera is headed. Mid-glide those are different
     places, and anchoring on the target makes a second wheel notch during the first one's move
     pull the floor out from under the pointer. */
  var seen = VIEW.ready ? VIEW : CAM;
  var wx = seen.x + hx / seen.zoom;
  var wy = seen.y + hy / seen.zoom;
  CAM.step = next;
  CAM.zoom = zoomOf(next);
  window.__wp.scale = CAM.zoom;
  CAM.x = wx - hx / CAM.zoom;
  CAM.y = wy - hy / CAM.zoom;
  camClamp();
  camApply();
  return CAM.step;
}

/** Hand the camera to the reader, or give it back. Taking it is implicit — drag, wheel, a rung,
 *  an arrow key — because asking somebody to press a button before they may move the map is the
 *  kind of thing that makes a view unusable. Giving it back is explicit and one click. */
function camHold() {
  if (!CAM.follow) return;
  CAM.follow = false;
  camApply();
}

function camFollow(on) {
  CAM.follow = !!on;
  camApply();
  return CAM.follow;
}

/** The whole company at once. This is the thing the view could not do at any setting: it is
 *  the bottom rung of the ladder plus a centred frame, and it is on a key, a button and a
 *  double-click because it is the first thing anybody wants from a map they are lost in. */
function camWhole() {
  camHold();
  camFrameWhole();
  camApply();
  return { step: CAM.step, zoom: CAM.zoom, span: camSpan(), built: builtPx() };
}

/** Move the frame by SCREEN pixels, so a key press and a drag of the same distance agree. */
function camPan(dx, dy) {
  camHold();
  CAM.x += dx / CAM.zoom;
  CAM.y += dy / CAM.zoom;
  camClamp();
  camApply();
}

function camAim() {
  var wx = 0, wy = 0, tw = 0;
  for (var id in BODIES) {
    var b = BODIES[id];
    var k = camWeight(b);
    if (!k) continue;
    wx += (b.px || b.x * W.tile) * k;
    wy += (b.py || b.y * W.tile) * k;
    tw += k;
  }
  if (!tw) return null;
  var span = camSpan();
  return { x: wx / tw - span.w / 2, y: wy / tw - span.h / 2 };
}

function camSnap() {
  var aim = camAim();
  if (!aim) return false;
  CAM.x = aim.x;
  CAM.y = aim.y;
  camClamp();
  return true;
}

function camStep(dt) {
  /* PINNED: a pointer is down somewhere on the glass, so the world holds still under it. The
     follow camera panning between press and release is what ate four aimed clicks in a row —
     the sprite was no longer where the finger came down. */
  if (!CAM.ready || !CAM.follow || CAM.pin) return;
  var aim = camAim();
  if (!aim) return;
  var span = camSpan();
  var dead = { x: span.w * 0.16, y: span.h * 0.16 };
  var k = Math.min(1, dt / 700);
  var gx = aim.x - CAM.x, gy = aim.y - CAM.y;
  if (Math.abs(gx) > dead.x) CAM.x += (gx - (gx > 0 ? dead.x : -dead.x)) * k;
  if (Math.abs(gy) > dead.y) CAM.y += (gy - (gy > 0 ? dead.y : -dead.y)) * k;
  camClamp();
  // No paint here. The frame loop's glide is what puts the picture where the camera is, and a
  // second write of the identical transform in the same frame is work nobody sees.
}

/** Chase the logical camera. Returns whether anything moved, so a still camera writes nothing. */
function camGlide(dt) {
  if (!CAM.ready) return false;
  if (!VIEW.ready) {
    VIEW.x = CAM.x; VIEW.y = CAM.y; VIEW.zoom = CAM.zoom; VIEW.ready = true; VIEW.live = false;
    return true;
  }
  var lz = Math.log(CAM.zoom) - Math.log(VIEW.zoom);
  var dx = CAM.x - VIEW.x;
  var dy = CAM.y - VIEW.y;
  var near = Math.abs(lz) < 0.002 && Math.abs(dx) < 0.4 && Math.abs(dy) < 0.4;
  if (near) {
    if (!VIEW.live) return false;
    VIEW.x = CAM.x; VIEW.y = CAM.y; VIEW.zoom = CAM.zoom; VIEW.live = false;
    return true;
  }
  /* Frame-rate independent: the same fraction of the REMAINING gap per unit time, whatever the
     frame took. A fixed per-frame lerp is a different curve at 30fps than at 60. */
  var k = 1 - Math.pow(0.001, Math.min(1, dt / GLIDE));
  VIEW.zoom = Math.exp(Math.log(VIEW.zoom) + lz * k);
  VIEW.x += dx * k;
  VIEW.y += dy * k;
  VIEW.live = true;
  return true;
}

/** Put the picture exactly where the camera is, this instant. For a DRAG, which has to be
 *  one-to-one with the pointer: easing a drag is what makes a map feel like it is on ice. */
function camSync() {
  VIEW.x = CAM.x; VIEW.y = CAM.y; VIEW.zoom = CAM.zoom; VIEW.ready = true; VIEW.live = false;
}

function camApply() {
  var stage = document.querySelector(".wp-stagebox");
  if (!stage) return;
  /* THE GLIDE IS AN ANIMATION, so it needs both an animator and a licence to animate — and it has
     neither here. Under prefers-reduced-motion the engine never starts its clock at all, so a
     camera that only ever paints what the glide has reached would have frozen the moment somebody
     zoomed: the rung would change, the gauge would light, and the picture would not move again.
     Same when the clock is simply paused. In both cases the camera CUTS, which is exactly what a
     reader who has asked for no motion wants. */
  if (!VIEW.ready || still || !TICK.on) camSync();
  /* PROMOTE THE STAGE ONLY WHILE THE GLIDE IS FLYING. Promoted at rest, Chromium keeps the
     layer's raster at whatever scale it was captured and the camera's scale() stretches that
     RASTER — the whole map measured bilinear-soft at zoom 2. Un-promoted during a glide, every
     frame re-rasters the visible tiles — measured p95 43.6ms across a zoom. So the layer is
     promoted for the ~300ms it is actually scaling (a soft frame mid-motion is invisible) and
     dropped the moment it settles, which re-rasters once, crisp, at the true scale. */
  var flying = VIEW.live ? "1" : "0";
  if (flying !== SHOWN.live) {
    stage.style.willChange = VIEW.live ? "transform" : "auto";
    SHOWN.live = flying;
  }
  var z = VIEW.zoom;
  var tx = -VIEW.x * z;
  var ty = -VIEW.y * z;
  // Crisp when still, smooth when moving. Rounding a translate mid-glide is a visible judder;
  // NOT rounding it at rest leaves the whole building on a half pixel.
  if (!VIEW.live) { tx = Math.round(tx); ty = Math.round(ty); }
  stage.style.setProperty("--inv", String(1 / z));
  stage.style.setProperty("--z", String(z));
  stage.style.transform = "translate3d(" + tx + "px," + ty + "px,0) scale(" + z + ")";
  /* WRITE ONLY ON CHANGE. This runs on every frame the camera is following, and an attribute
     written on .wp-view — the root every rule in the far-view block hangs off — invalidates
     the whole scene's style even when the value did not change. Measured against the same page
     without the guard: frame p95 33.3ms against 16.8ms, i.e. a dropped frame every twenty, for
     three writes that are almost always identical to what is already there. */
  var far = z < 1 ? "1" : "0";
  var fol = CAM.follow ? "1" : "0";
  if (far !== SHOWN.far || fol !== SHOWN.follow) {
    var view = document.querySelector(".wp-view");
    if (view) {
      /* Pulled back past one, a nameplate held at constant SCREEN size is wider than the room
         the person stands in, and the words out-mass the building — the complaint the
         constant-size trick exists to prevent, arriving from the other side. So below one the
         view sheds its words and becomes what it should be at that scale: a signed PLAN. */
      if (far !== SHOWN.far) view.setAttribute("data-far", far);
      if (fol !== SHOWN.follow) {
        view.setAttribute("data-follow", fol);
        /* The latch says two different words; the button carries one LABEL and a pressed state,
           so a reader who cannot see which word is showing is told the same thing. */
        var latch = document.querySelector(".wp-cam-follow");
        if (latch) latch.setAttribute("aria-pressed", CAM.follow ? "true" : "false");
      }
      SHOWN.far = far;
      SHOWN.follow = fol;
    }
  }
  /* The read says FIT when the whole-floor frame is running the one licensed off-rung scale,
     so the gauge never claims a rung the picture is not on. */
  var word = Math.abs(CAM.zoom - zoomOf(CAM.step)) > 0.01 ? "FIT" : SCALE_WORDS[CAM.step - 1] + "×";
  if (CAM.step !== SHOWN.step || word !== SHOWN.read) {
    var rule = document.querySelector(".wp-rule");
    var read = document.querySelector(".wp-read");
    if (rule) rule.setAttribute("data-step", String(CAM.step));
    if (read) read.textContent = word;
    if (rule || read) { SHOWN.step = CAM.step; SHOWN.read = word; }
  }
  var eye = document.querySelector(".wp-eye");
  if (eye && CAM.miniW && CAM.miniH) {
    var span = { w: CAM.vw / z, h: CAM.vh / z };
    var world = worldPx();
    var mw = CAM.miniW, mh = CAM.miniH;
    var sx = mw / world.w, sy = mh / world.h;
    /* Clamped INTO the plan. Pulled back far enough that the frame is larger than the world,
       the camera sits at a negative offset and the unclamped box was drawn floating above the
       panel as a bare rectangle over the floor — the tell that the plan and the frame had
       stopped agreeing. Clamped, it simply fills the plan, which is the truth. */
    var ex = Math.max(0, Math.min(mw, VIEW.x * sx));
    var ey = Math.max(0, Math.min(mh, VIEW.y * sy));
    eye.style.left = (2 + ex) + "px";
    eye.style.top = (2 + ey) + "px";
    eye.style.width = Math.max(4, Math.min(mw - ex, span.w * sx)) + "px";
    eye.style.height = Math.max(4, Math.min(mh - ey, span.h * sy)) + "px";
  }
}

/** Steer by pointing at the plan in the corner. The camera stays where it is put. */
function camLookAt(px, py) {
  camHold();
  var span = camSpan();
  CAM.x = px - span.w / 2;
  CAM.y = py - span.h / 2;
  camClamp();
  camApply();
}

/* ── speech, placed rather than hidden ───────────────────────────────────────────────────────
   Every line is on at rest. What makes that possible is that only a few people are on screen at
   once, so the bubbles can be laid out: candidates in priority order, three heights to choose
   from, and any bubble that would land on another bubble or on somebody's nameplate is dropped
   rather than drawn over them. The boxes are computed in world pixels, which is the same
   comparison the eye makes because everything scales together. */
var SAY_MAX = 7;
/* Half a bubble's own width, measured off the rendered element, and how close to the frame edge
   it may come. */
var SAY_HALF = 66;
var SAY_EDGE = 4;
var SAY_AT = 0;
var SAID = [];

/** How tall this body is drawn, in device pixels. Published by the renderer from ONE table. */
function headOf(b) {
  var n = b.el && Number(b.el.getAttribute("data-head"));
  return n > 0 ? n : 32;
}

function boxesOf(b, kind) {
  /* The furniture a character carries, as rectangles in world pixels around its feet.
     MEASURED off the rendered elements, not guessed: the boxes here had drifted from what the
     stylesheet actually draws — the stamp's reserve sat nine pixels below the stamp and missed its
     top edge entirely, and the tools' reserve still described the column at the shoulder they were
     moved off. A reserve that does not match the element it stands for refuses bubbles over empty
     air and allows them over a word. Generous on purpose, in the direction that costs a bubble
     rather than the one that draws words on top of a name. */
  var up = b.el && b.el.getAttribute("data-label") === "up";
  /* The SAME number the stylesheet places these with. A posture changes how tall a body is, and
     these rectangles are the only thing that promises two labels are never drawn over each other
     — so a reserve derived from a constant while the element is placed from a variable is a
     guarantee that silently stops being true. Read it off the element. */
  var head = headOf(b);
  var x = b.px, y = b.py;
  if (kind === "tag") return up ? [x - 34, y - head - 16, x + 34, y - head - 2] : [x - 34, y + 1, x + 34, y + 15];
  if (kind === "can") return up ? [x - 28, y, x + 28, y + 20] : [x - 28, y + 15, x + 28, y + 35];
  return up ? [x - 24, y - head - 41, x + 24, y - head - 28] : [x - 24, y - head - 11, x + 24, y - head + 2];
}

function sayBox(b, level) {
  var up = b.el && b.el.getAttribute("data-label") === "up";
  /* Clear of the stamp rather than three pixels off it, at the doll's own heights. */
  var base = (up ? 78 : 50) + level * 26 - (32 - headOf(b));
  /* The bubble holds a constant SCREEN size, so in world pixels it shrinks as the camera moves
     in. Reserving the unscaled box would refuse most of the lines at zoom two for a collision
     that is not there. */
  var hw = 70 / CAM.zoom, hh = 22 / CAM.zoom;
  return [b.px - hw, b.py - base - hh, b.px + hw, b.py - base + 2];
}

function hits(a, b) {
  return a[0] < b[2] - 1 && b[0] < a[2] - 1 && a[1] < b[3] - 1 && b[1] < a[3] - 1;
}

function sayRank(b, t) {
  if (b.talkUntil > t) return 100;
  if (b.mode === "errand" || b.mode === "delivering") return 90;
  if (t - b.saidAt < 7000) return 80;
  if (b.path) return 40;
  if (b.el && b.el.getAttribute("data-status") === "error") return 30;
  return 10 + b.phase;
}

function speechLayout(t) {
  if (t - SAY_AT < 420) return;
  SAY_AT = t;
  /* A LINE IS A LABEL FOR A CHARACTER, so a character nobody can see does not get one — judged by
     onScreen(), against the camera that is PAINTED and by the same test the per-frame edge clamp
     uses. It used to be a two-tile pad around the LOGICAL camera, which is wrong twice over: the
     logical camera is a room away from the painted one for the length of a glide, and the pad let
     a body a tile and a half outside the frame keep a bubble that is held at constant SCREEN size
     — so what showed was the last few letters of somebody else's sentence jammed against the edge
     with no owner attached. That is the "...face graph ..." a critic found in a side bar. */
  var live = [];
  var taken = SIGNS.slice();
  for (var id in BODIES) {
    var b = BODIES[id];
    if (!b.el || !b.el.isConnected || b.px === undefined) continue;
    if (!onScreen(b)) continue;
    /* Everything a character already wears is an obstacle, whether or not it gets a bubble. */
    taken.push(boxesOf(b, "tag"));
    taken.push(boxesOf(b, "can"));
    if (b.el.querySelector(".wp-mark")) taken.push(boxesOf(b, "mark"));
    if (b.el.getAttribute("data-say")) live.push(b);
  }
  live.sort(function (p, q) { return sayRank(q, t) - sayRank(p, t); });
  var next = [];
  for (var i = 0; i < live.length && next.length < SAY_MAX; i++) {
    for (var lv = 0; lv < 3; lv++) {
      var box = sayBox(live[i], lv);
      var clash = false;
      for (var j = 0; j < taken.length; j++) if (hits(box, taken[j])) { clash = true; break; }
      if (clash) continue;
      taken.push(box);
      live[i].el.style.bottom = "";
      live[i].el.querySelector(".wp-say").style.bottom =
        ((live[i].el.getAttribute("data-label") === "up" ? 74 : 44) + lv * 26 -
          (32 - headOf(live[i]))) + "px";
      next.push(live[i].el);
      break;
    }
  }
  /* Clear the class off EVERY actor that did not win a place, not merely off the ones this
     function last set it on: a swapped-in element that arrives already carrying it would
     otherwise keep a bubble open forever, outside the layout that is supposed to guarantee no two
     of them collide. */
  var all = document.querySelectorAll(".wp-actor.is-saying");
  for (var k = 0; k < all.length; k++) if (next.indexOf(all[k]) < 0) all[k].classList.remove("is-saying");
  for (var m = 0; m < next.length; m++) next[m].classList.add("is-saying");
  /* Clamp the ones just granted, HERE, rather than leaving it to the next frame. This function
     runs AFTER the bodies have been placed, so a bubble that appears on this frame is drawn
     unclamped for exactly one frame before the edge check catches it — one frame at sixty a
     second is invisible to a person and perfectly visible to a probe sampling every frame, and it
     was the whole of the last forty-seven pixels of overflow. A correction owed on the frame the
     thing appears is applied on that frame. */
  for (var q = 0; q < live.length; q++) if (next.indexOf(live[q].el) >= 0) sayEdge(live[q], live[q].py);
  SAID = next;
}

/* ── the hand at the glass ───────────────────────────────────────────────────────────────────
   The pointer, in TILE coordinates, so the world can notice the person looking at it. Tracked
   from the view's own pointer events; converted through the PAINTED camera because that is the
   picture the hand is actually over. */
var HAND = { on: false, tx: -1e9, ty: -1e9, cx: 0, cy: 0, rx: 0, ry: 0, rok: false };
/* How far a resting body's attention reaches, in tiles. */
var GAZE = 4.5;

/* The event stores CLIENT pixels and nothing else — a rect read per pointermove forces layout in
   the middle of a frame, and the conversion has to happen per FRAME anyway so a stationary
   pointer stays aimed at the same tile while the camera glides under it. The view's rect is
   cached and invalidated by camFit (resize and bind both land there); a transform never moves
   it, so the cache is exact. */
function handTrack(cx, cy) {
  HAND.cx = cx;
  HAND.cy = cy;
  HAND.on = true;
}

function handAim() {
  if (!HAND.on || !VIEW.zoom) return;
  if (!HAND.rok) {
    var view = document.querySelector(".wp-view");
    if (!view) return;
    var r = view.getBoundingClientRect();
    HAND.rx = r.left;
    HAND.ry = r.top;
    HAND.rok = true;
  }
  HAND.tx = ((HAND.cx - HAND.rx) / VIEW.zoom + VIEW.x) / W.tile;
  HAND.ty = ((HAND.cy - HAND.ry) / VIEW.zoom + VIEW.y) / W.tile;
}

/* How far a click may land from a body and still mean that body, in tiles. Generous on purpose:
   a sprite is one tile wide, ambient life moves it a little, and the professional sweep measured
   four aimed clicks in a row selecting nothing. Selection is by NEAREST BODY inside this ring,
   from the engine's own positions — never by whether the pointer happened to be inside a 32px
   element on the exact frame the button came up. */
var PICK_REACH = 2.2;

function actorNear(cx, cy) {
  if (!VIEW.ready || !VIEW.zoom) return null;
  var view = document.querySelector(".wp-view");
  if (!view) return null;
  var r = view.getBoundingClientRect();
  var tx = ((cx - r.left) / VIEW.zoom + VIEW.x) / W.tile;
  var ty = ((cy - r.top) / VIEW.zoom + VIEW.y) / W.tile;
  var best = null;
  var bd = PICK_REACH * PICK_REACH;
  for (var id in BODIES) {
    var b = BODIES[id];
    if (!b.el || !b.el.isConnected) continue;
    /* Aimed at the body's middle: the anchor is at the boots. */
    var dx = b.x - tx;
    var dy = b.y - 0.5 - ty;
    var d = dx * dx + dy * dy;
    if (d < bd) { bd = d; best = b; }
  }
  return best ? best.el : null;
}

/* Heads turn. A body at rest inside GAZE tiles of the pointer faces it and follows it — the
   cheapest possible proof that these are people in a place and not stickers on a picture. Two
   deliberate exclusions: a WORKING body keeps typing unless the pointer is directly on it (work
   is not interrupted by being watched), and a body mid-walk or mid-conversation already has
   somewhere better to look. When the hand moves on, everyone settles back the way their seat
   faces rather than staying frozen mid-stare. */
function presence() {
  handAim();
  var t = TICK.t;
  for (var id in BODIES) {
    var b = BODIES[id];
    if (!b.el || !b.el.isConnected) continue;
    if (b.path || (b.talkUntil && b.talkUntil > t)) { b.gazing = false; continue; }
    var met = b.el.classList.contains("is-met");
    var dx = HAND.tx - b.x;
    var near = HAND.on && Math.abs(dx) <= GAZE && Math.abs(HAND.ty - b.y) <= GAZE;
    var react = met || (near && b.el.getAttribute("data-attention") !== "working");
    if (react) {
      if (Math.abs(dx) > 0.2) b.face = dx > 0 ? 1 : -1;
      b.gazing = true;
    } else if (b.gazing) {
      b.gazing = false;
      settleFace(b);
    }
  }
}

/* ── rooms light up, doors open ──────────────────────────────────────────────────────────────
   Both driven off where bodies ACTUALLY are rather than off the snapshot, so a room goes dark and
   a door swings shut the moment the last person walks out of it. */
var LIT = {};
var LEAVES = [];

/* The room signs are world text too, and the speech layout was blind to them: it reserved every
   nameplate and every capability rail and then put a line straight through "Quality & Critics".
   Read once per bind, in untransformed layout pixels, which is the same space the bubbles use. */
var SIGNS = [];

function readSigns() {
  SIGNS = [];
  var list = document.querySelectorAll(".wp-plaque");
  for (var i = 0; i < list.length; i++) {
    var el = list[i];
    if (!el.offsetWidth) continue;
    SIGNS.push([el.offsetLeft - 2, el.offsetTop - 2, el.offsetLeft + el.offsetWidth + 2, el.offsetTop + el.offsetHeight + 2]);
  }
}

function readDoors() {
  LEAVES = [];
  var list = document.querySelectorAll(".wp-leaf");
  for (var i = 0; i < list.length; i++) {
    var el = list[i];
    LEAVES.push({
      el: el,
      x: Number(el.getAttribute("x") || 0) / 8,
      y: Number(el.getAttribute("y") || 0) / 8,
      on: false
    });
  }
}

function doors() {
  for (var i = 0; i < LEAVES.length; i++) {
    var d = LEAVES[i];
    var near = false;
    for (var id in BODIES) {
      var b = BODIES[id];
      if (!b.el || !b.el.isConnected) continue;
      if (Math.abs(b.x - d.x) <= 1.1 && Math.abs(b.y - d.y) <= 1.4) { near = true; break; }
    }
    /* The hand opens doors too. Sweeping the pointer along a corridor and watching each leaf
       swing for you is the world answering the hand the same way it answers a walker. */
    if (!near && HAND.on && Math.abs(HAND.tx - d.x) <= 1.2 && Math.abs(HAND.ty - d.y) <= 1.4) near = true;
    if (near === d.on) continue;
    d.on = near;
    d.el.classList.toggle("is-open", near);
  }
}

/* Worst first. A room holding one crashed agent and five happy ones is a room with a problem in
   it, so the light takes the loudest state in the room rather than an average or the last body
   the loop happened to see. Same order the rail sorts its own rows by. */

/* Which states TAKE a room's colour on their own, and which one it takes only unanimously.
   ERROR, ASKED and HELD are things that want a person, so one of them is enough — a department
   with a crashed agent in it burns red whoever else is in there. FINISHED is not like that: it is
   the absence of anything wanting, so a room is only finished when EVERY body in it is. Ranked
   ranked with the others it beat plain WORKING, so one done agent among five busy ones turned the
   whole department green — a light saying the opposite of the truth, which is worse than none. */
var LOUD = ["error", "asked", "held"];

function lighting() {
  var now = {};
  for (var id in BODIES) {
    var b = BODIES[id];
    if (!b.el || !b.el.isConnected) continue;
    for (var i = 0; i < W.rooms.length; i++) {
      var r = W.rooms[i];
      if (b.x >= r.x && b.x < r.x + r.w && b.y >= r.y && b.y < r.y + r.h) {
        var v = b.el.getAttribute("data-attention") || "working";
        var seat = now[r.i] || (now[r.i] = { rank: LOUD.length, voice: "working", done: true });
        var rank = LOUD.indexOf(v);
        if (rank >= 0 && rank < seat.rank) { seat.rank = rank; seat.voice = v; }
        if (v !== "finished" && v !== "not-ours") seat.done = false;
        break;
      }
    }
  }
  /* A ROOM WHERE EVERY RUN HAS FINISHED IS THE ONE THING THIS VIEW HAD NO WAY TO SAY AT A GLANCE.
     Posture says it up close — bodies on the couches instead of at the desks — but a posture is
     twelve device pixels of height at zoom one and nothing at all at the scale where the whole
     company is in frame, which is exactly the scale somebody asks "is anything still running?"
     at. The room's own light is the only mark big enough there, and it was already tinted by the
     taxonomy; it just had to be told what unanimous means. */
  for (var kd in now) {
    if (now[kd].rank === LOUD.length && now[kd].done) now[kd].voice = "finished";
  }
  for (var k in now) {
    if (LIT[k] && LIT[k].voice === now[k].voice) continue;
    setLit(k, "1", now[k].voice);
  }
  for (var k2 in LIT) {
    if (now[k2]) continue;
    setLit(k2, "0", "");
  }
  LIT = now;
}
function setLit(id, on, voice) {
  var q = '[data-room="' + cssq(id) + '"]';
  /* ALL of them: the building is drawn in layers now — grounds, light, structure — so one room is
     several groups carrying the same id, and a querySelector lit whichever came first. */
  var rooms = document.querySelectorAll(".wp-rm" + q);
  for (var i = 0; i < rooms.length; i++) {
    rooms[i].setAttribute("data-lit", on);
    if (voice) rooms[i].setAttribute("data-voice", voice);
    else rooms[i].removeAttribute("data-voice");
  }
  var dot = document.querySelector(".wp-mini " + q);
  if (dot) {
    dot.setAttribute("data-lit", on);
    if (voice) dot.setAttribute("data-voice", voice);
    else dot.removeAttribute("data-voice");
  }
}
function cssq(s) { return String(s).replace(/["\\]/g, "\\$&"); }

/* ── the plan in the corner ──────────────────────────────────────────────────────────────────
   One dot per person in their pod's colour. The camera takes the overview away and this is what
   gives it back, so it is updated often enough to be true and rarely enough to be free. */
var DOTS = {};
var DOT_AT = 0;

function minimap(t) {
  if (t - DOT_AT < 160) return;
  DOT_AT = t;
  var host = document.querySelector(".wp-dots");
  /* Minimap geometry is owned by camFit's bind/resize layout boundary. Reading it again here,
     after the active loop has written actor and camera styles, synchronously flushed the whole
     scene every 160ms. An absent or zero-sized plan remains inert until a later fit measures it. */
  if (!host || !CAM.miniW || !CAM.miniH) return;
  var sx = CAM.miniW / (W.cols * W.tile);
  var sy = CAM.miniH / (W.rows * W.tile);
  var seen = {};
  for (var id in BODIES) {
    var b = BODIES[id];
    if (!b.el || !b.el.isConnected || b.px === undefined) continue;
    seen[id] = 1;
    var dot = DOTS[id];
    if (!dot) {
      dot = DOTS[id] = document.createElement("i");
      host.appendChild(dot);
    }
    dot.style.setProperty("--accent", b.el.style.getPropertyValue("--accent"));
    dot.style.left = (b.px * sx) + "px";
    dot.style.top = (b.py * sy) + "px";
  }
  for (var k in DOTS) {
    if (seen[k]) continue;
    if (DOTS[k].parentNode) DOTS[k].parentNode.removeChild(DOTS[k]);
    delete DOTS[k];
  }
}

/* ── the post: an interaction is a WALK ──────────────────────────────────────────────────────
   A message is not a line drawn between two boxes and not a note flying over the roof: the sender
   gets up, walks the corridors to wherever the recipient is standing, says it, and walks back. */
var QUEUE = [];
var LIVE = 0;
var MAX_ERRANDS = 3;
var ENVIRONMENT_KEY = "";

function errand(fromId, toId, text) {
  QUEUE.push({ f: fromId, t: toId, x: text });
  if (QUEUE.length > 24) QUEUE.shift();
}

function pumpErrands(t) {
  while (LIVE < MAX_ERRANDS && QUEUE.length) {
    var job = QUEUE.shift();
    var a = BODIES[job.f], b = BODIES[job.t];
    if (!a || !b || !a.el || !b.el) continue;
    if (a.mode === "errand" || a.mode === "delivering") { QUEUE.push(job); break; }
    var spot = besideOf(Math.round(b.x), Math.round(b.y));
    if (!spot) continue;
    a.errand = { to: job.t, text: job.x };
    if (sendTo(a, spot.x, spot.y, "errand")) { LIVE++; window.__wp.notes++; }
    else { a.errand = null; }
  }
}

/* ── the loop ────────────────────────────────────────────────────────────────────────────────
   One rAF for the whole building. Cheap when nobody is looking: a panel left open on a second
   monitor is the intended way to use this, so a hidden document keeps its clock and skips the
   work. */
function frame(ts) {
  requestAnimationFrame(frame);
  /* Cheap when nobody is looking, and cheap when somebody has paused it — but the chain itself
     never stops, so TICK.on is a switch rather than a one-way door. */
  if (!TICK.on || document.hidden) { TICK.t = ts; return; }
  var dt = Math.min(64, ts - TICK.t || 16);
  TICK.t = ts;
  TICK.dt = dt;
  /* THE CAMERA MOVES FIRST, and everything is then placed against where it now IS.
     Aiming and gliding AFTER the bodies left placement one frame behind the transform that would
     actually paint it. Invisible for a sprite — it rides the stage, so a stale camera moves it
     along with everything else — and very much not invisible for anything corrected in SCREEN
     space, because that correction is computed against a camera which has since moved on. It
     showed as a few pixels of a speech bubble still outside the panel after the clamp: the right
     rule, one frame stale. Aim from last frame's positions (which is what following means),
     glide, paint, THEN place. */
  camStep(dt);
  if (camGlide(dt)) camApply();
  /* Before the bodies are placed, so a turned head is painted the same frame it turns. */
  presence();
  var busy = 0;
  for (var id in BODIES) {
    var b = BODIES[id];
    if (!b.el || !b.el.isConnected) continue;
    if (b.path) { stepWalk(b, dt); busy++; }
    else decide(b, ts);
    place(b, ts);
  }
  declutter();
  LIVE = 0;
  for (var id2 in BODIES) {
    var b2 = BODIES[id2];
    if (b2.mode === "errand" || b2.mode === "delivering") LIVE++;
  }
  pumpErrands(ts);
  var environmentKey = HAND.on + ":" + HAND.tx.toFixed(1) + "," + HAND.ty.toFixed(1);
  for (var id3 in BODIES) {
    var b3 = BODIES[id3];
    environmentKey += "|" + id3 + "@" + b3.x.toFixed(1) + "," + b3.y.toFixed(1) +
      ":" + (b3.el ? b3.el.getAttribute("data-attention") : "");
  }
  if (environmentKey !== ENVIRONMENT_KEY) {
    ENVIRONMENT_KEY = environmentKey;
    lighting();
    doors();
  }
  speechLayout(ts);
  minimap(ts);
  window.__wp.walking = busy;
}
`;
