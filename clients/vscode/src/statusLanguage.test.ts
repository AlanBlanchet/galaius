import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { STATUS, voiceOf, HELD_AFTER_SECONDS, type Attention } from "./statusLanguage.ts";

const ALL: Attention[] = ["error", "asked", "held", "finished", "working", "not-ours"];

test("every state has a full voice", () => {
  for (const a of ALL) {
    const v = STATUS[a];
    assert.ok(v.mark && v.word && v.phrase && v.accent, `${a} is missing part of its voice`);
    assert.equal(v.word, v.word.toUpperCase(), `${a}'s word must be the stamped form`);
    assert.ok(v.accent.startsWith("--vscode-"), `${a} must tint from a theme token, not a hex`);
  }
});

test("marks are distinguishable without colour", () => {
  const marks = ALL.map((a) => STATUS[a].mark);
  assert.equal(new Set(marks).size, marks.length, "two states share a glyph — colour-blind readers see one state");
});

test("an unknown state reads as a person at a desk, not a hole", () => {
  assert.equal(voiceOf("something-new").word, "WORKING");
});

test("no surface re-invents the vocabulary locally", () => {
  /* The drift this file exists to stop: the rail kept its own glyph table while the world kept its
     own stamp words, and the two slid apart until a critic called them "two products". A comment
     asking future edits to use the shared table would not have caught it; reading the source does. */
  const rail = readFileSync(join(import.meta.dirname, "railHtml.ts"), "utf8");
  assert.ok(
    !/const\s+MARK\s*[:=]/.test(rail),
    "railHtml.ts declares its own status-glyph table again — pass voiceOf in instead",
  );
  for (const glyph of ["✕", "⏸", "✓", "○"]) {
    assert.ok(
      !rail.includes(`"${glyph}"`),
      `railHtml.ts hard-codes ${glyph}; the mark belongs to statusLanguage.ts`,
    );
  }
});

test("the held threshold means the same thing on every surface", () => {
  /* One rule, three declarations: HELD_AFTER_SECONDS here, HELD_SECONDS in rail.ts (which cannot
     import this module — node --test loads it directly and tsc will not emit the .ts specifier
     that loader needs), and STALL_SECONDS in the workplace. Duplication is tolerable; DIVERGENCE
     is not, because it would let the roster call someone stuck while the world still shows them
     working. So the equality is pinned here. */
  const read = (file: string, name: string) => {
    const src = readFileSync(join(import.meta.dirname, file), "utf8");
    const m = src.match(new RegExp(name + "\\s*(?::\\s*number\\s*)?=\\s*(\\d+)"));
    assert.ok(m, `${name} not found in ${file} — if it moved, move this guard with it`);
    return Number(m![1]);
  };
  assert.equal(read("rail.ts", "HELD_SECONDS"), HELD_AFTER_SECONDS);
  assert.equal(
    read("../webview/workplace/status.ts", "STALL_SECONDS"),
    HELD_AFTER_SECONDS,
    "the workplace and the rail disagree about when someone is stuck",
  );
});

test("only the states that earn colour are painted", () => {
  /* The world stamps HELD and NOT-OURS in neutral ink deliberately — a paused agent and someone
     else's project are not alarms. The rail tinted them anyway, so the same worker read cool grey
     in one surface and warm yellow in the other. One decision, one place. */
  assert.equal(STATUS.held.tinted, false, "a paused agent is not an alarm");
  assert.equal(STATUS["not-ours"].tinted, false, "another project is not an alarm");
  for (const a of ["error", "asked", "finished"] as const) {
    assert.equal(STATUS[a].tinted, true, `${a} needs the eye`);
  }
});

test("a finished agent says nothing — the checkmark is the whole message", () => {
  /* "Agents keep saying 'finished'. Instead, we should not have that." The states that keep their
     words are the two that need him; everything routine is a mark, not a caption. */
  assert.equal(STATUS.finished.quiet, true);
  assert.equal(STATUS.working.quiet, true);
  assert.ok(!STATUS.error.quiet, "an error must still SAY so");
  assert.ok(!STATUS.asked.quiet, "a question waiting on him must still say so");
});

test("a killed run never reads as success", () => {
  /* The professional sweep found tester×3 wearing ✓ while the agent card said "stopped" for the
     same runs, 200px apart. A run somebody killed did not finish its job; folding it into
     "finished" hides exactly the runs a person stopped for a reason. */
  assert.equal(STATUS.stopped.mark !== STATUS.finished.mark, true, "stopped must not wear the checkmark");
  assert.equal(STATUS.stopped.word, "STOPPED");
  assert.ok(!STATUS.stopped.quiet, "it is not an alarm, but it must SAY what it is");
  assert.equal(STATUS.stopped.tinted, false, "neutral ink: attention-worthy, not alarming");
});
