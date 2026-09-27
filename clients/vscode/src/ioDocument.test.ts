/** The full-payload tab: ids in, ids out. */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { IO_INLINE, IO_SCHEME, ioFromPath, ioPath, ioTarget } from "./ioDocument.ts";

test("the path names the tab and carries the ids back out", () => {
  const p = ioPath("run-1", "toolu_42", "out", "Bash");
  assert.equal(p, "/run-1/toolu_42/Bash.out.txt");
  assert.deepEqual(ioFromPath(p), { runId: "run-1", toolId: "toolu_42", side: "out" });
  assert.equal(IO_SCHEME, "interact-agent-io");
});

test("a hostile tool name cannot smuggle a separator into the path", () => {
  /* The invariant is NO "/" in the basename — a ".." inside one segment is inert. */
  const p = ioPath("r", "t", "in", "../..//etc");
  const base = p.split("/").pop()!;
  assert.ok(!base.includes("/") && p.split("/").filter(Boolean).length === 3);
  assert.deepEqual(ioFromPath(p), { runId: "r", toolId: "t", side: "in" });
});

test("a malformed path is refused, never guessed at", () => {
  assert.equal(ioFromPath("/nope"), null);
  assert.equal(ioFromPath("/a/b/c.sideways.txt"), null);
});

test("a stamped call routes by id; an unstamped one banks its stored text", () => {
  const withId = ioTarget("r1", "toolu_9", "out", "Bash", "whatever");
  assert.equal(withId, "/r1/toolu_9/Bash.out.txt");
  assert.equal(IO_INLINE.size, 0, "a raw-stream lookup needs no banked copy");

  const noId = ioTarget("r1", "", "in", "Bash", "the stored input");
  assert.ok(noId && /^\/r1\/inline/.test(noId), "an old record still opens — via a minted key");
  const key = noId!.split("/")[2];
  assert.equal(IO_INLINE.get(key), "the stored input");

  assert.equal(ioTarget("r1", "", "in", "Bash", ""), null,
    "nothing stored and nothing stamped — nothing to open");
});
