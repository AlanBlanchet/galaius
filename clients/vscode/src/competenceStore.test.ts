/** "could not ASK" is not "not RANKED", and a failed ask must be retried.
 *
 *  Four surfaces each held their own copy of this fetch and each got both wrong: a machine whose
 *  installed CLI has no `agents models` command showed "no ranking carries this model" for models
 *  the board ranks, and one failure at startup silenced every surface for the life of the window.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { CompetenceStore, whyUnavailable } from "./competenceStore.ts";

/** No board on disk, so a failure to ask is a failure to answer. Tests that read the reader's own
 *  board would pass or fail on the machine they ran on. */
const NO_BOARD = () => new Map<string, number>();

const ROW = JSON.stringify([
  { id: "claude-sonnet-5", competence: "aa.intelligence 38.4 · 24th of 450 scored", covers: [] },
]);

test("a CLI that cannot answer is reported as such, never as an unranked model", async () => {
  const store = new CompetenceStore(async () => ({
    stdout: "", error: 'Unknown command "models".',
  }), NO_BOARD);
  await store.ensure(["claude-sonnet-5"]);
  assert.match(store.unavailable ?? "", /installed interact/,
    "the reader is told the binary is too old — the fix is a version, not a missing model");
  assert.notEqual(store.unavailable, "");
});

test("a failed ask is retried, not remembered as an answer", async () => {
  let calls = 0;
  const store = new CompetenceStore(async () => {
    calls += 1;
    return calls === 1
      ? { stdout: "", error: "interact: command not found" }
      : { stdout: ROW, error: null };
  }, NO_BOARD);
  await store.ensure(["claude-sonnet-5"]);
  assert.equal(calls, 1);
  await store.ensure(["claude-sonnet-5"]);
  assert.equal(calls, 2, "the same question is asked again after a failure");
  assert.match(store.competence("claude-sonnet-5") ?? "", /38\.4/);
  assert.equal(store.unavailable, null, "and the earlier failure is forgotten once it works");

  // Answered once, it does not ask again for the same set.
  await store.ensure(["claude-sonnet-5"]);
  assert.equal(calls, 2);
});

test("a lookup normalises, so no caller writes the two-step form again", async () => {
  const store = new CompetenceStore(async () => ({ stdout: ROW, error: null }), NO_BOARD);
  await store.ensure(["claude-sonnet-5"]);
  for (const spelling of ["claude-sonnet-5", "anthropic/claude-sonnet-5",
                          "claude-sonnet-5-20250101"]) {
    assert.match(store.competence(spelling) ?? "", /38\.4/, spelling);
  }
  assert.equal(store.competence(undefined), undefined);
});

test("what the CLI can reach and what the board measures are different questions", async () => {
  /* The board fallback added scores for models nobody here can run. One surface marks rows
     "routable" on exactly that distinction, so the two must not be one index. */
  const store = new CompetenceStore(async () => ({ stdout: ROW, error: null }),
    () => new Map([["gpt-6-astra", 52.8]]));
  await store.ensure(["claude-sonnet-5", "gpt-6-astra"]);
  assert.equal(store.routable("claude-sonnet-5"), true);
  assert.match(store.competence("gpt-6-astra") ?? "", /52\.8/,
    "the board still measures it — that is a different fact from being able to run it");
  assert.equal(store.routable("gpt-6-astra"), false);
});

test("the reason names the binary when the binary is the reason", () => {
  assert.match(whyUnavailable('Unknown command "models".'), /needs a newer version/);
  assert.equal(whyUnavailable("some other failure"), "interact could not be asked for scores");
});

test("a cache hit reports NOTHING CHANGED, so a repaint cannot ask its way into a loop", async () => {
  /* Round 47 pinned the pegged core to this line. `ensure()` returned `line.size > 0` on the
     cache-hit path — "known", not "changed" — and its one caller reads the result as a reason to
     repaint: roster() → ensureCompetence() → ensure() → true → refreshIfOpen() → render() →
     roster(). Measured live: 12.5 wholesale roster swaps/second, ~1.0 MB/s, extension host at
     104% with the tab CLOSED (retainContextWhenHidden keeps it rendering).

     The value means "is there something new to show". A cache hit is the case where there is
     provably nothing new. */
  let calls = 0;
  const store = new CompetenceStore(async () => {
    calls += 1;
    return { stdout: JSON.stringify([{ id: "m", competence: "aa.intelligence 40.0 · 1st of 1" }]), error: null };
  }, () => new Map());

  assert.equal(await store.ensure(["m"]), true, "the first answer IS new");
  assert.equal(calls, 1);
  assert.equal(await store.ensure(["m"]), false, "asking again for the same models changes nothing");
  assert.equal(await store.ensure(["m"]), false, "and still nothing, however many times a repaint asks");
  assert.equal(calls, 1, "and it does not re-ask the CLI either");
});

test("a CLI that cannot answer must not re-arm the repaint loop", async () => {
  /* Round 48, blocking: the `return false` guard only covered `asked === key`. On failure the code
     sets `asked = undefined` — deliberately, so the next surface retries rather than inheriting a
     permanent silence — so EVERY render is a cache miss, falls through to `return line.size > 0`,
     and `fillFromBoard()` has meanwhile filled `line` from the board on disk. True → repaint →
     ask → true. Measured with the CLI off PATH: 98.1% of a core with the tab CLOSED, 10.99
     swaps/s. The fix was conditional on the CLI answering, which is exactly when it does not. */
  let calls = 0;
  const board = new Map([["m", 40]]);
  const store = new CompetenceStore(async () => {
    calls += 1;
    return { stdout: "", error: "interact: command not found" };
  }, () => board);

  const first = await store.ensure(["m"]);
  const second = await store.ensure(["m"]);
  const third = await store.ensure(["m"]);
  assert.ok(calls >= 2, "it still retries a failure rather than caching silence");
  assert.equal(second, false, "but a retry that learns nothing new is not a reason to repaint");
  assert.equal(third, false, "and still not, however many renders ask");
  assert.ok(first === true || first === false, "the first answer may legitimately be either");
});
