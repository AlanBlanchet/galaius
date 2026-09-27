import { strict as assert } from "node:assert";
import * as fs from "node:fs";
import { test } from "node:test";

import { mergeLiveTables, provenanceLabel } from "./benchmarkTables.ts";
import * as benchmarkTables from "./benchmarkTables.ts";

// The extension bundles benchmarks.json at BUILD time, so its scores could never change once
// packaged — a months-old model stayed on screen as best at MMMU, with its retrieved date buried
// in a tooltip nobody hovers. Python now caches live tables; this is the consumer that reads them.

const baked = {
  benchmarks: [
    { id: "mmmu", name: "MMMU", published: { retrieved: "2026-06-07", entries: [{ model_name: "OldModel", score: 0.94 }] } },
    { id: "screenspot", name: "ScreenSpot", published: { retrieved: "2026-05-21", entries: [] } },
  ],
};

test("a live table replaces the baked one for that benchmark", () => {
  const merged = mergeLiveTables(baked, {
    mmmu: { retrieved: "2026-08-18", entries: [{ model_name: "NewModel", score: 0.97 }] },
  });
  assert.equal(merged.benchmarks[0].published.entries[0].model_name, "NewModel");
});

test("a benchmark with no live table keeps its baked snapshot", () => {
  const merged = mergeLiveTables(baked, { mmmu: { retrieved: "x", entries: [] } });
  assert.equal(merged.benchmarks[1].published.retrieved, "2026-05-21");
});

test("no live data at all leaves everything as it was", () => {
  assert.deepEqual(mergeLiveTables(baked, {}), baked);
});

test("the baked file is not mutated — a stale render must not poison the next one", () => {
  mergeLiveTables(baked, { mmmu: { retrieved: "2026-08-18", entries: [{ model_name: "X", score: 1 }] } });
  assert.equal(baked.benchmarks[0].published.entries[0].model_name, "OldModel");
});

test("provenance is VISIBLE text, not a tooltip — a hidden date reads as current", () => {
  assert.match(provenanceLabel("2026-06-07"), /2026-06-07/);
});

test("a table with no date says so rather than looking authoritative", () => {
  assert.match(provenanceLabel(undefined), /unknown|undated/i);
});

test("an EMPTY live table never replaces a populated snapshot", () => {
  // Observed live: OpenVLM answered 200 with zero rows for MMMU. Letting that win would blank
  // the panel — trading stale data for no data.
  const merged = mergeLiveTables(baked, { mmmu: { retrieved: "2026-08-18", entries: [] } });
  assert.equal(merged.benchmarks[0].published.entries[0].model_name, "OldModel");
  assert.equal(merged.benchmarks[0].published.retrieved, "2026-06-07",
    "and it keeps the OLD date, so the row still admits how old it is");
});

test("a live table OLDER than the bundled snapshot does not replace it", () => {
  // OpenVLM's MMMU board last published 2025-09; the packaged snapshot is 2026-06. "Live" is not
  // the same as "current" — the freshest DATED source wins, whichever side it is on.
  const merged = mergeLiveTables(baked, {
    mmmu: { retrieved: "2025-09-17", entries: [{ model_name: "GPT-5-20250807", score: 0.71 }] },
  });
  assert.equal(merged.benchmarks[0].published.entries[0].model_name, "OldModel");
  assert.equal(merged.benchmarks[0].published.retrieved, "2026-06-07");
});

test("a live table NEWER than the snapshot does replace it", () => {
  const merged = mergeLiveTables(baked, {
    mmmu: { retrieved: "2026-08-18", entries: [{ model_name: "Fresh", score: 0.99 }] },
  });
  assert.equal(merged.benchmarks[0].published.entries[0].model_name, "Fresh");
});

test("the removed approximate MMMU snapshot cannot return through persisted live cache", () => {
  const bundled = { benchmarks: [{ id: "mmmu_pro", published: null }] };
  const merged = mergeLiveTables(bundled, { mmmu_pro: {
    source_url: "https://mmmu-benchmark.github.io/", retrieved: "2026-06-07",
    entries: [
      { model_name: "GPT-5.4", score: 0.94 },
      { model_name: "Claude Opus 4.7", score: 0.927 },
      { model_name: "Gemini 3.1 Pro", score: 0.84 },
      { model_name: "Qwen3.5", score: 0.77 },
    ],
  } });
  assert.equal(merged.benchmarks[0].published, null);
});

test("unverified scores remain visible but explicitly excluded", () => {
  assert.match(benchmarkTables.selectionExplanation("unverified"), /unverified.*excluded/i);
});

test("an undated live table is taken when the snapshot has no date either", () => {
  const undated = { benchmarks: [{ id: "x", published: { entries: [{ model_name: "B", score: 1 }] } }] };
  const merged = mergeLiveTables(undated, { x: { entries: [{ model_name: "A", score: 2 }] } });
  assert.equal(merged.benchmarks[0].published.entries[0].model_name, "A");
});

test("Models settings expose editable media thresholds and normalized weights", () => {
  const settingsData = JSON.parse(fs.readFileSync(new URL("./settings.json", import.meta.url), "utf8"));
  const settings = (settingsData as { settings: { key: string; description: string }[] }).settings;
  const policy = new Map(settings.map((setting) => [setting.key, setting.description]));
  assert.match(policy.get("media.criteria") ?? "", /threshold/i);
  assert.match(policy.get("media.criteriaWeights") ?? "", /normalized|unit/i);
});

test("VS Code registers both media selection settings", () => {
  const manifest = JSON.parse(
    fs.readFileSync(new URL("../package.json", import.meta.url), "utf8"),
  );
  const properties = manifest.contributes.configuration.properties;
  for (const [key, description] of [
    ["interact.media.criteria", /threshold/i],
    ["interact.media.criteriaWeights", /normalized|unit/i],
  ] as const) {
    assert.equal(properties[key]?.type, "string");
    assert.equal(properties[key]?.default, "");
    assert.match(properties[key]?.description ?? "", description);
  }
});

test("benchmark selection explains exclusions and exposes only trusted source links", () => {
  const helpers = benchmarkTables as unknown as Record<string, unknown>;
  assert.equal(typeof helpers.selectionExplanation, "function");
  assert.equal(typeof helpers.trustedBenchmarkSource, "function");

  const explain = helpers.selectionExplanation as (status: string) => string;
  for (const status of ["missing", "not_applicable", "stale", "unmapped"]) {
    assert.match(explain(status), new RegExp(status.replace("_", " "), "i"));
    assert.doesNotMatch(explain(status), /(?:^|\D)0(?:\.0+)?(?:\D|$)/);
  }
});
