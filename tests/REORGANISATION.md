# Test suite reorganisation — 2026-09-17

Owner: "2854 tests is too much. You really need to review all of them, re-group them, remove
duplicate code, be smart about this, if you see elements that repeat between files, find a way
to re-group them".

## What the suite actually is

| measure | value |
| --- | --- |
| tests | 2874 |
| files | 167 |
| lines | 38 771 |
| files holding 5 tests or fewer | 45 (136 tests between them) |
| fixtures defined inside test files | 96 |
| fixtures in `conftest.py` | 7 |
| test names repeated across files | 3 |

The tests themselves are not duplicates: 2874 tests carry 2871 distinct names. The repetition is
in the SCAFFOLDING and in how the files are cut:

- `_mgr` (build a headless BrowserManager) — defined in 10 files, 3 drifted bodies
- `_home` (point HOME at tmp_path) — 9 files, 5 byte-identical
- `_ready` (start the browser or skip) — 6 files
- `_png` (a synthetic PNG) — 5 files
- `_el` (build a fake element) — 4 files
- `_win` (a fake window) — 3 files

and 45 files exist because a single reported issue got its own file, so one subject is spread
over a dozen entry points (browser actions live in `test_tabs_and_refs`, `test_dialogs`,
`test_browser_crash`, `test_hover_settle`, `test_annotate_elements`, `test_emulate_device`,
`test_dispatch_resilience`, …).

## Target

1. **One support package, `tests/support/`** — `browser.py` (`browser_manager`, `ready_or_skip`),
   `env.py` (`isolated_home`), `media.py` (`solid_png`), growing as each cluster hoists what it
   repeats. Every per-file copy deleted, never left beside the shared one.
2. **One file per subject.** A file earns its own name when it holds a subject, not an issue
   number. 167 → about 90, by merging each cluster's ≤5-test files into the subject file they
   belong to, keeping every assertion.
3. **Parametrise what differs only in data.** The large files (`test_subscription_media` 125,
   `test_conversation_console` 112, `test_agent_registry` 99) carry runs of tests differing by one
   value; those collapse into table-driven cases without losing a case.
4. **Delete only what another test already proves**, and say which test covers it.

## Rule for every move

No assertion disappears. A test may change shape (merged file, parametrised case, shared
fixture); it may not silently stop checking what it checked. Each cluster reports the count it
started with, the count it ends with, and where every dropped test's coverage now lives.

## Result (2026-09-17)

Four waves and three independent audits. Waves 1-2 merged files by subject and hunted duplicate
tests; the tester's audit then found that merging had put unrelated subjects in one file, so
waves 3-4 split those back out by subject and adopted `tests/support/` across the tree.

| measure | before | after |
| --- | --- | --- |
| `.py` files under `tests/` | 184 | 167 |
| test modules (`tests/*.py`) | 167 | 144 |
| tests collected | 2874 | 2890 |
| lines | 41 025 | 41 329 |
| test names repeated across files | 3 | 0 |
| test module importing a sibling test module | 2 | 0 |
| helper copies (`_mgr`, `_home`, `_ready`, `_png`, `_el`, `_win`, `_record`, `_git`, `_ascapture`, `make_window`, scripted providers, `PROVIDERS` patches, `load_policy` patches, model builders) | 60+ | 0 |

The count went UP, by the 22 tests added this round for two launcher bugs found on the way.

### Why the test count did not fall

Every cluster was told to delete a test only when another test proves the same thing, naming the
survivor. Across eight clusters the answer was the same: **zero semantic duplicates**. A
suite-wide scan of normalised test bodies (string literals ignored) finds 30 groups of
same-shaped tests covering 75 functions, and each reads as a different branch, error or boundary
value — `test_click_partial_coordinates` and `test_scroll_partial_coordinates_rejected` have the
same shape and check different actions. Runs of data-only tests became parametrised tables:
same cases, less code.

So 2874 was what this product costs to check. What was too much was 167 modules and sixty copies
of a dozen helpers.

### `tests/support/`

One module per concern, every symbol with real importers:

- `browser.py` — `browser_manager`, `browser_config`, `ready_or_skip`
- `media.py` — `solid_png`, `varied_png`
- `desktop.py` — `bare_nested_backend`, `desktop_window`, `interactive_element`, `RecordingBackend`
- `agents.py` — `register_run`, `ScriptedProvider`, `install_provider`, `use_policy`
- `models.py` — `model`, `catalog_dict`, `catalog_json`
- `git.py` — `run_git`, `git_out`, `init_repo`, `commit_all`
- `capture.py` — `async_capture`

`conftest.py` keeps what pytest must own: the autouse HOME/config isolation (which replaced 51
of the 55 hand-written `setenv("HOME")` lines), `reset_model_registry`, `http_origin`.

### Independent verdicts

- STRUCTURE (`tester`, read-only): 136 PASS / 4 FAIL on the one-subject rule, zero orphans, and
  no-assertion-lost PASS — 54 deleted files and 359 test functions traced by name into today's
  tree, 15 unmatched and every one explained. The 4 FAIL files were split afterwards.
- COHESION (`generalizer`): FAIL three times, each round naming fewer and smaller rows; every
  named row was closed by an engineer before the next audit.

### Two production bugs found on the way

- `registry._write` merged a run with `run.model_dump()` (Python objects) then `json.dumps()`,
  which raises on `agent_ref`'s UUIDs. Four `test_agent_catalog.py` tests had been failing on it.
- The launcher's quota fall-through did not work: it only ran when no provider was named, its
  refusal pattern matched the healthy `rate_limit ... allowed` line, a second candidate of the
  same vendor reused the dead child's session id, and nothing remembered the refusal. See
  `src/interact/agents/quota.py`.
