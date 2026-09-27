import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";
import * as path from "node:path";
import * as fs from "node:fs";
import { fileURLToPath } from "node:url";

const require_ = createRequire(import.meta.url);

test("benchmark cards expose sources and explain unmapped recommendations", async () => {
  const opened: string[] = [];
  const Module = require_("node:module") as {
    _load: (request: string, parent: unknown, isMain: boolean) => unknown;
  };
  const originalLoad = Module._load;
  Module._load = (request, parent, isMain) => {
    if (request === "vscode") return {
      env: { openExternal: async (uri: { value: string }) => opened.push(uri.value) },
      Uri: { parse: (value: string) => ({ value }) },
      window: {}, commands: {}, workspace: {},
    };
    if (request === "./leaderboard") return { readLeaderboard: () => null };
    return originalLoad(request, parent, isMain);
  };
  try {
    const built = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
    const dashboardPath = path.join(built, "dashboard.js");
    delete require_.cache[require_.resolve(dashboardPath)];
    const { DashboardPanel } = require_(dashboardPath);
    const panel = Object.create(DashboardPanel.prototype) as any;
    panel.modelsData = { providers: { p: { models: { "p/known": {} } } } };
    panel.benchmarksData = { benchmarks: [{
      id: "screenspot_pro", name: "ScreenSpot Pro", description: "visual grounding",
      category: "gui_grounding", source: "source", source_auth: "", metric: "accuracy",
      url: "https://example.test/source", score_url: "https://example.test/receipt",
      methodology_url: "https://example.test/methodology",
      measured: {}, published: {
        source_url: "https://example.test/source", retrieved: "2026-09-06", freshness: "current",
        lib_recommendation: null,
        entries: [
          { model_name: "known", model_id: "p/known", score: 0.9, normalized_score: 0.9, status: "eligible" },
          { model_name: "missing", score: 0, status: "missing" },
          { model_name: "n/a", score: 0, status: "not_applicable" },
          { model_name: "approx", score: 0.8, normalized_score: 0.8, status: "approximate" },
          { model_name: "unmapped", score: 0.8, normalized_score: 0.8, status: "unmapped" },
        ],
      },
      recommendations: [
        { model_id: "p/known", score: 0.9, source: "published", rank: 1, cost_per_million: 1, quality_per_dollar: 0.9 },
        { model_id: "p/unknown", score: 0.8, source: "published", rank: 2, cost_per_million: null, quality_per_dollar: null },
      ],
    }, {
      id: "stale", name: "Stale board", description: "old data", category: "gui_grounding",
      source: "source", source_auth: "", metric: "accuracy", url: "https://example.test/stale",
      measured: {}, recommendations: [], published: {
        source_url: "https://example.test/stale", retrieved: "2020-01-01", freshness: "stale",
        lib_recommendation: null,
        entries: [{ model_name: "known", model_id: "p/known", score: 0.99,
          normalized_score: 0.99, status: "eligible" }],
      },
    }] };

    const benchmarks = panel.benchmarksCell();
    const source = benchmarks.content.flatMap((item: any) => item.actions ?? [])
      .find((action: any) => action.type === "openBenchmarkSource");
    assert.deepEqual(source.data, { benchmarkId: "screenspot_pro", sourceRole: "evaluation" });
    const explanation = [...benchmarks.content, ...panel.recommendationsCell().content]
      .flatMap((item: any) => item.kind === "table" ? item.rows.flat() : [item.value ?? ""])
      .join(" ");
    assert.match(explanation, /unmapped.*excluded/i);
    for (const state of ["missing", "not applicable", "approximate", "stale", "unmapped"]) {
      assert.match(explanation, new RegExp(state, "i"));
    }

    await panel.handleMessage({ type: "openBenchmarkSource", benchmarkId: source.data.benchmarkId,
      url: "https://attacker.test" });
    await panel.handleMessage({ type: "openBenchmarkSource", benchmarkId: source.data.benchmarkId,
      sourceRole: "methodology", url: "https://attacker.test" });
    await panel.handleMessage({ type: "openBenchmarkSource", benchmarkId: source.data.benchmarkId,
      sourceRole: "score", url: "https://attacker.test" });
    await panel.handleMessage({ type: "openBenchmarkSource", benchmarkId: "unknown" });
    panel.benchmarksData.benchmarks.push({
      id: "unsafe", url: "http://example.test/source", published: null,
    });
    await panel.handleMessage({ type: "openBenchmarkSource", benchmarkId: "unsafe" });
    assert.deepEqual(opened, [
      "https://example.test/source", "https://example.test/methodology", "https://example.test/receipt",
    ]);
  } finally {
    Module._load = originalLoad;
  }
});

test("dashboard renders the same refreshed PublishedTable bytes used by routing", () => {
  const fixturePath = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)), "../../../tests/fixtures/refreshed_published_table.json",
  );
  const published = JSON.parse(fs.readFileSync(fixturePath, "utf8")).after;
  const excluded = JSON.parse(fs.readFileSync(fixturePath, "utf8")).excluded;
  const built = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
  const { DashboardPanel } = require_(path.join(built, "dashboard.js"));
  const panel = Object.create(DashboardPanel.prototype) as any;
  panel.modelsData = { providers: { openai: { models: {
    "openai/visual-a": {}, "openai/visual-b": {},
  } } } };
  panel.benchmarksData = { benchmarks: [{
    id: "mmmu_pro", name: "MMMU Pro", description: "vision", category: "image",
    source: "source", source_auth: "", metric: "accuracy",
    url: published.source_url, measured: {}, recommendations: [], published,
  }, ...Object.entries(excluded).map(([id, table]) => ({
    id, name: id, description: "excluded state", category: "image", source: "source",
    source_auth: "", metric: "accuracy", url: (table as any).source_url,
    measured: {}, recommendations: [], published: table,
  }))] };

  const rendered = panel.benchmarksCell().content
    .flatMap((item: any) => item.kind === "table" ? item.rows.flat() : [item.value ?? ""])
    .join(" ");
  assert.match(rendered, /visual-b.*0\.950.*eligible/i);
  assert.match(rendered, /as of 2026-09-06/i);
  for (const state of ["missing", "stale", "approximate"]) {
    assert.match(rendered, new RegExp(`${state}.*excluded`, "i"));
  }
});

test("benchmark sources, missing-data guidance, and score semantics come from metadata", async () => {
  const opened: string[] = [];
  const Module = require_("node:module") as any;
  const originalLoad = Module._load;
  Module._load = (request: string, parent: unknown, isMain: boolean) => {
    if (request === "vscode") return {
      env: { openExternal: async (uri: { value: string }) => opened.push(uri.value) },
      Uri: { parse: (value: string) => ({ value }) }, window: {}, commands: {}, workspace: {},
    };
    if (request === "./leaderboard") return { readLeaderboard: () => null };
    return originalLoad(request, parent, isMain);
  };
  try {
    const built = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
    const { DashboardPanel } = require_(path.join(built, "dashboard.js"));
    const panel = Object.create(DashboardPanel.prototype) as any;
    panel.modelsData = { providers: {} };
    panel.benchmarksData = { benchmarks: [{
      id: "mmmu_pro", name: "MMMU Pro", description: "vision", category: "image",
      source: "Artificial Analysis", source_auth: "ARTIFICIAL_ANALYSIS_API_KEY",
      refresh_supported: true, metric: "accuracy", score_range: [0, 1], higher_is_better: true,
      url: "https://artificialanalysis.ai/evaluations/mmmu-pro",
      methodology_url: "https://artificialanalysis.ai/methodology/intelligence-benchmarking#mmmu-pro",
      measured: {}, recommendations: [], published: null,
    }, {
      id: "mmbench", name: "MMBench", description: "vision", category: "image",
      source: "OpenCompass", source_auth: "", refresh_supported: false,
      metric: "accuracy", score_range: [0, 1], higher_is_better: true,
      url: "https://github.com/open-compass/MMBench", measured: {}, recommendations: [], published: null,
    }, {
      id: "unknown-scale", name: "Unknown scale", description: "vision", category: "image",
      source: "source", source_auth: "", refresh_supported: false, metric: "score",
      url: "https://example.test", measured: {}, recommendations: [], published: null,
    }] };
    const cell = panel.benchmarksCell();
    const text = cell.content.flatMap((item: any) => [item.label ?? "", item.value ?? "", item.tooltip ?? ""]).join(" ");
    assert.match(text, /accuracy.*0(?:\.0)?–1(?:\.0)?.*higher is better/i);
    assert.match(text, /MMBench[\s\S]*current data unavailable[\s\S]*informational/i);
    assert.doesNotMatch(text, /MMBench[\s\S]*(Add key|Refresh)/i);
    assert.match(text, /Unknown scale[\s\S]*score semantics unavailable/i);
    const actions = cell.content.flatMap((item: any) => item.actions ?? []);
    const evaluation = actions.find((a: any) => a.data?.sourceRole === "evaluation");
    const methodology = actions.find((a: any) => a.data?.sourceRole === "methodology");
    assert.deepEqual(evaluation.data, { benchmarkId: "mmmu_pro", sourceRole: "evaluation" });
    assert.deepEqual(methodology.data, { benchmarkId: "mmmu_pro", sourceRole: "methodology" });
    assert.equal(methodology.label, "Methodology");
  } finally { Module._load = originalLoad; }
});

test("dashboard preserves refreshed source authority instead of promoting numeric scores", () => {
  const built = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
  const { DashboardPanel } = require_(path.join(built, "dashboard.js"));
  const panel = Object.create(DashboardPanel.prototype) as any;
  panel.modelsData = { providers: { openai: { models: { "openai/exact": {} } } } };
  panel.benchmarksData = { benchmarks: ["approximate", "unverified", "eligible"].map((status) => ({
    id: status, name: status, description: "authority", category: "image", source: "source",
    source_auth: "", metric: "accuracy", url: "https://example.test", measured: {}, recommendations: [],
    published: { source_url: "https://example.test", retrieved: "2026-09-06", freshness: "current",
      entries: [{ model_name: "exact", model_id: "openai/exact", score: 0.91,
        normalized_score: 0.91, status }] },
  })) };
  const text = panel.benchmarksCell().content
    .flatMap((item: any) => item.kind === "table" ? item.rows.flat() : [item.label ?? "", item.value ?? ""])
    .join(" ");
  assert.match(text, /approximate benchmark value; excluded/i);
  assert.match(text, /unverified benchmark value.*excluded/i);
  assert.match(text, /Eligible for routing/i);
});

test("dashboard renders entry authority independently from table freshness", () => {
  const built = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
  const { DashboardPanel } = require_(path.join(built, "dashboard.js"));
  const panel = Object.create(DashboardPanel.prototype) as any;
  panel.modelsData = { providers: {} };
  panel.benchmarksData = { benchmarks: [{
    id: "authority", name: "Authority", description: "test", category: "image", source: "source",
    source_auth: "", requires_auth: false, refresh_supported: true, metric: "accuracy",
    url: "https://example.test/evaluation", score_url: "https://example.test/receipt",
    methodology_url: "https://example.test/methodology", measured: {}, recommendations: [],
    published: { source_url: "https://example.test/receipt", retrieved: "2020-01-01", freshness: "stale",
      entries: [{ model_name: "model", score: 0.7, normalized_score: 0.7, status: "approximate" }] },
  }] };
  const cell = panel.benchmarksCell();
  const text = cell.content.flatMap((item: any) => item.kind === "table" ? item.rows.flat() : []).join(" ");
  assert.match(text, /approximate.*excluded/i);
  assert.match(text, /stale/i);
  const actions = cell.content.flatMap((item: any) => item.actions ?? []);
  assert.deepEqual(actions.map((action: any) => action.data), [
    { benchmarkId: "authority", sourceRole: "evaluation" },
    { benchmarkId: "authority", sourceRole: "score" },
    { benchmarkId: "authority", sourceRole: "methodology" },
  ]);
});

test("benchmark Configuration reflects implemented refresh and authentication capability", () => {
  const built = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
  const { DashboardPanel } = require_(path.join(built, "dashboard.js"));
  const panel = Object.create(DashboardPanel.prototype) as any;
  panel.keyManager = { get: () => undefined };
  panel.benchmarksData = { benchmarks: [{
    id: "unsupported", name: "Unsupported", category: "image", source: "source",
    source_auth: "KEY", requires_auth: true, refresh_supported: false, description: "none",
  }, {
    id: "keyed", name: "Keyed", category: "image", source: "source",
    source_auth: "REAL_KEY", requires_auth: true, refresh_supported: true, description: "keyed",
  }, {
    id: "keyless", name: "Keyless", category: "image", source: "source",
    source_auth: "", requires_auth: false, refresh_supported: true, description: "auto",
  }] };
  const cell = panel.benchmarkDataCell();
  const text = cell.content.flatMap((item: any) => [item.label ?? "", item.value ?? ""]).join(" ");
  assert.match(text, /Unsupported.*refresh unavailable.*informational/i);
  assert.match(text, /Keyed.*needs a key/i);
  assert.match(text, /Keyless.*automatic refresh.*no key needed/i);
  const actions = cell.content.flatMap((item: any) => item.actions ?? []);
  assert.deepEqual(actions.map((action: any) => action.data), [{ key: "REAL_KEY" }]);
  assert.doesNotMatch(text, /curated snapshots are used otherwise/i);
});

test("generated Configuration uses exact registered refresh capability", () => {
  const built = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
  const { DashboardPanel } = require_(path.join(built, "dashboard.js"));
  const panel = Object.create(DashboardPanel.prototype) as any;
  panel.keyManager = { get: () => undefined };
  panel.benchmarksData = JSON.parse(fs.readFileSync(
    path.resolve(path.dirname(fileURLToPath(import.meta.url)), "benchmarks.json"), "utf8",
  ));
  const text = panel.benchmarkDataCell().content
    .flatMap((item: any) => [item.label ?? "", item.value ?? ""]).join(" ");
  assert.match(text, /MMMU Pro.*refresh unavailable.*informational/i);
  assert.doesNotMatch(text, /MMMU Pro.*needs a key/i);
  assert.match(text, /MMBench.*automatic refresh.*no key needed/i);
});
