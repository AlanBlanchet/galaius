import { strict as assert } from "node:assert";
import { test } from "node:test";

import { livenessOf } from "./runStatus.ts";

// Every DETACHED agent flashed "crashed" with a warning icon the moment it finished. `agents
// spawn` returns immediately, so nobody is left waiting to write the exit code — the record still
// says "running" while its process is already gone. Measured live on a teacher and a tester that
// both completed cleanly and reported their answers.

test("a run whose stream ENDED is finished, not crashed", () => {
  for (const active of ["starting", "running"] as const) {
    assert.equal(livenessOf(active, 999999, false, true), "done", active);
  }
});

test("a run that stopped with no ending recorded anywhere is a real crash", () => {
  for (const active of ["starting", "running"] as const) {
    assert.equal(livenessOf(active, 999999, false, false), "crashed", active);
  }
});

test("a live process is left alone", () => {
  for (const active of ["starting", "running"] as const) {
    assert.equal(livenessOf(active, 4242, true, false), active);
  }
});

test("a status the record already settled is never second-guessed", () => {
  for (const settled of ["waiting", "done", "failed", "cancelled", "stopped", "foreign"] as const) {
    assert.equal(livenessOf(settled, 999999, false, false), settled);
  }
});

test("a run with no pid is not called crashed on that basis", () => {
  // Nothing to probe: claiming a crash from an absent pid invents a failure.
  for (const active of ["starting", "running"] as const) {
    assert.equal(livenessOf(active, null, false, false), active);
  }
});

// visual-critic spawned a real agent from the panel; it answered "STREAM-TEST-OK" and exited
// cleanly, and the panel showed a red warning icon and the word "crashed". The record was stale
// (nothing had healed it yet), so the fallback below is what decided — and it defaulted to the
// alarming answer on no evidence at all.

test("a run that produced a transcript and then vanished is finished, not crashed", () => {
  for (const active of ["starting", "running"] as const) {
    assert.equal(livenessOf(active, 4242, false, false, true), "done", active);
  }
});

test("a run that vanished having said nothing is the shape a crash leaves", () => {
  for (const active of ["starting", "running"] as const) {
    assert.equal(livenessOf(active, 4242, false, false, false), "crashed", active);
  }
});

test("a settled stream still wins over both", () => {
  for (const active of ["starting", "running"] as const) {
    assert.equal(livenessOf(active, 4242, false, true, false), "done", active);
  }
});
