import assert from "node:assert/strict";
import { test } from "node:test";
import { presentMediaStatus } from "./mediaStatus.ts";

test("media status presenter distinguishes policy from actual visual API routing", () => {
  assert.match(presentMediaStatus("session_only", false, "auto", false), /blocked/i);
  assert.match(presentMediaStatus("session_only", true, "auto", false), /unknown/i);
  assert.match(presentMediaStatus("api_allowed", true, "auto", true), /metered.*fallback/i);
  assert.match(presentMediaStatus("api_allowed", true, "session", false), /fallback disabled/i);
  assert.match(presentMediaStatus("api_allowed", false, "session", false), /fallback disabled/i);
  assert.match(presentMediaStatus("api_allowed", false, "api", true), /metered.*api/i);
});
