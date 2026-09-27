"""The workplace view's SIMULATION, exercised in a real browser.

The scene is plain JS embedded in the extension's webview, so nothing in the TypeScript unit
suite can reach it: those tests build strings, and every defect here lives in what the browser
RESOLVES from them. A still frame hides the whole class — the walker bug below rendered perfectly
in every screenshot of a standing team, and only appeared for the four seconds somebody walked.

The harness is the extension's own preview build (the same document, CSP and theme variables the
panel ships), so a pass here is a pass on what the panel renders.
"""

import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from threading import Event

import pytest

EXT = Path(__file__).resolve().parent.parent / "clients" / "vscode"
PREVIEW = EXT / "webview" / "workplace" / "dev" / "preview.ts"

#: NOT a list. The colours that make a sprite a person are read off the standing worker itself, so
#: this cannot drift from what the stylesheet declares. A hand-kept copy here disagreed with the
#: sim's own list — four of the six properties the fix copies were never checked, and the two that
#: were are carried by pre-existing code rather than by the fix. Adding a seventh colour to
#: `.wp-worker` now extends this test for free instead of silently escaping it.
COLOUR_PROPS_JS = """(el) => {
  const out = {};
  const style = getComputedStyle(el);
  for (let i = 0; i < style.length; i++) {
    const p = style[i];
    if (p.startsWith('--c-')) out[p] = style.getPropertyValue(p).trim();
  }
  return out;
}"""


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    """The real preview pages, built from source."""
    if not PREVIEW.exists() or shutil.which("npx") is None:
        _unavailable("the extension's webview toolchain is not available here")
    out = tmp_path_factory.mktemp("workplace")
    bundle = out / "preview.js"
    build = subprocess.run(
        ["npx", "esbuild", str(PREVIEW), "--bundle", f"--outfile={bundle}",
         "--format=cjs", "--platform=node", "--target=es2022"],
        cwd=EXT, capture_output=True, text=True,
    )
    if build.returncode != 0:
        pytest.fail(f"the webview would not build:\n{build.stderr}")
    subprocess.run(["node", str(bundle), str(out)], check=True, capture_output=True)
    return out


@pytest.fixture(scope="module")
def browser():
    """ONE browser for the module.

    `asyncio_mode = "auto"` puts every test inside an event loop, and Playwright's sync API
    refuses to open a second context inside one — so a per-test `sync_playwright()` passes when
    run alone and errors in a full run, which is the worst kind of test. It is also one chromium
    instead of six on a machine with no swap.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def page(scene, browser):
    # Explicitly NOT reduced motion: every animation in this file is the thing under test, and a
    # harness that quietly disables them reports "identical" no matter what broke.
    pg = browser.new_page(viewport={"width": 1100, "height": 800},
                          reduced_motion="no-preference")
    pg.goto((scene / "live.html").as_uri())
    pg.wait_for_timeout(600)
    yield pg
    pg.close()


# NO traveller-colour tests here any more, and the reason is worth keeping.
#
# They guarded a real bug: a walking character was a CLONE of its sprite, reparented onto a
# traffic layer outside its pod, so it lost the `--accent` it inherited from there and wore
# another team's colour for the whole walk. Invisible in every still frame of a standing team.
#
# The tile rewrite deleted the mechanism. A body now owns a tile coordinate and the element ITSELF
# moves — `wp-travelling`, `makeWalker` and the copied-property list are all gone from sim.ts. So
# the defect is not fixed, it is unreachable: there is no clone and no reparenting for a custom
# property to fall out of. A test for a mechanism that does not exist passes forever and protects
# nothing, which is the same trap as the contrast guard that ERRORED instead of failing when its
# element was renamed.
#
# What replaced the coverage: `test_every_word_in_the_world_is_readable` measures what actually
# paints in both themes, and the movement tests below check that bodies genuinely move.

def test_saying_something_makes_the_sender_walk(page):
    """"Place the characters on it and make them move (when interacting)."

    The previous version of this asserted that two COURIER SPRITES did not overlap. The tile
    rewrite deleted couriers-as-sprites: a note now makes the SENDER cross the building to deliver
    it, which is the behaviour actually asked for. The old test skipped silently once `.wp-errand`
    stopped existing — a skip is not a pass, and a test that can only skip protects nothing.
    """
    moved = page.evaluate(
        """() => {
          const wp = window.__wp;
          if (!wp || !wp.note || !wp.bodies) return {error: 'the sim exposes no messaging seam'};
          wp.tick.on = false;
          if (wp.clearNotes) wp.clearNotes();
          const ids = Object.keys(wp.bodies);
          if (ids.length < 2) return {error: 'need two people for one to walk to the other'};
          // Two people as far apart as the roster allows, so a delivery is a real journey.
          const from = ids[0], to = ids[ids.length - 1];
          const before = {x: wp.bodies[from].x, y: wp.bodies[from].y};
          wp.note(from, to, 'come and look at this');
          // Step the engine forward rather than waiting on its own schedule — racing a
          // spontaneous trip is exactly how the last movement bug stayed invisible.
          for (let t = 0; t < 40; t++) wp.step(performance.now() + t * 120);
          const after = {x: wp.bodies[from].x, y: wp.bodies[from].y};
          // Hand the clock back RUNNING. The page fixture is shared, and a test that freezes the
          // engine and walks away leaves every later test sampling a still photograph — which is
          // how an overlap check passed alone and failed in the suite.
          wp.tick.on = true;
          return {from, to, before, after,
                  travelled: Math.abs(after.x - before.x) + Math.abs(after.y - before.y)};
        }"""
    )
    assert "error" not in moved, moved.get("error")
    assert moved["travelled"] > 0, (
        f"{moved['from']} said something to {moved['to']} and never left its tile "
        f"({moved['before']} -> {moved['after']}) — interaction has to cause movement")


def test_reduced_motion_never_starts_a_frame_loop_or_mutates_actor_coordinates(scene, browser):
    """Reduced motion is a stopped simulation, not CSS hiding a running one."""
    pg = browser.new_page(viewport={"width": 1100, "height": 800}, reduced_motion="reduce")
    pg.add_init_script(
        """
          window.__raf_requests = 0;
          const native = window.requestAnimationFrame.bind(window);
          window.requestAnimationFrame = (callback) => {
            window.__raf_requests += 1;
            return native(callback);
          };
        """
    )
    pg.goto((scene / "live.html").as_uri())
    before = pg.evaluate(
        """() => ({
          requests: window.__raf_requests,
          actors: [...document.querySelectorAll('.wp-actor')].map((el) => ({
            id: el.dataset.runId,
            style: el.getAttribute('style'),
            transform: getComputedStyle(el).transform,
          })),
        })"""
    )
    pg.wait_for_timeout(400)
    after = pg.evaluate(
        """() => ({
          requests: window.__raf_requests,
          actors: [...document.querySelectorAll('.wp-actor')].map((el) => ({
            id: el.dataset.runId,
            style: el.getAttribute('style'),
            transform: getComputedStyle(el).transform,
          })),
        })"""
    )
    pg.close()

    assert before["requests"] == 0 and after["requests"] == 0, (
        "prefers-reduced-motion started an endless animation-frame chain: "
        f"{before['requests']} requests at first paint, {after['requests']} after 400ms"
    )
    assert after["actors"] == before["actors"], (
        "actor coordinates mutated while reduced motion promised a still workplace"
    )


def test_same_position_cast_replacement_reapplies_label_rows(page):
    """A cached layout belongs to actor elements, not only their unchanged coordinates."""
    missing = page.evaluate(
        """() => {
          window.__wp.run(false);
          window.__wp.step(performance.now());
          const cast = document.querySelector('.wp-cast').cloneNode(true);
          for (const actor of cast.querySelectorAll('.wp-actor')) actor.removeAttribute('data-ph');
          window.__wpApply(cast.outerHTML);
          window.__wp.step(performance.now() + 16);
          const missing = [...document.querySelectorAll('.wp-actor')]
            .filter((actor) => !actor.hasAttribute('data-ph'))
            .map((actor) => actor.dataset.runId);
          window.__wp.run(true);
          return missing;
        }"""
    )
    assert not missing, f"replacement actors skipped cached label layout: {missing}"


def test_empty_team_camera_opens_on_the_building_not_empty_terrain(scene, browser):
    pg = browser.new_page(viewport={"width": 700, "height": 800})
    pg.goto((scene / "empty.html").as_uri())
    camera = pg.evaluate("() => ({x: window.__wp.cam.x, y: window.__wp.cam.y})")
    pg.close()
    assert camera["x"] > 0 and camera["y"] > 0, (
        f"empty team opened over unused terrain instead of its building: {camera}"
    )


def test_terminal_actors_settle_without_looking_fallen_or_stuck(page):
    """Terminal is calm and settled; a quarter-turn reads as fallen or broken."""
    terminal = page.evaluate(
        """() => {
          const actor = document.querySelector('.wp-actor[data-status="done"]');
          if (!actor) return null;
          const doll = actor.querySelector('.wp-doll');
          const box = doll.getBoundingClientRect();
          return {
            posture: actor.dataset.posture,
            transform: getComputedStyle(doll).transform,
            width: box.width,
            height: box.height,
          };
        }"""
    )
    assert terminal is not None, "the realistic scene has no terminal actor to inspect"
    matrix = [float(part) for part in terminal["transform"][7:-1].split(",")]
    quarter_turned = (
        abs(matrix[0]) < 0.01
        and abs(matrix[3]) < 0.01
        and abs(matrix[1]) > 0.99
        and abs(matrix[2]) > 0.99
    )
    assert terminal["posture"] != "lounge" and not quarter_turned, (
        "terminal actor looks fallen or stuck at a quarter-turn: " + str(terminal)
    )


def test_people_are_primary_and_lineage_survives_without_names_or_colour(page):
    """The workplace must read as people before it reads as floor or labels.

    The reported scene made every regular actor a 32px interchangeable token, then put the only
    parent/child distinction in a seven-pixel nameplate that children hide at rest.  This measures
    the rendered bodies at the readable opening camera and inspects the body-level silhouettes
    with every nameplate hidden and every accent forced to the same colour.  A relationship cue
    that disappears under those conditions is still nameplate- or colour-dependent.
    """
    measured = page.evaluate(
        """() => {
          const actors = [...document.querySelectorAll('.wp-actor')];
          const regular = actors.filter((actor) => !actor.classList.contains('is-brain'));
          for (const actor of actors) {
            actor.style.setProperty('--accent', '#888');
            const tag = actor.querySelector('.wp-tag');
            if (tag) tag.style.display = 'none';
          }
          const body = (actor) => actor.querySelector('.wp-body').getBoundingClientRect();
          const cue = (actor) => {
            const style = getComputedStyle(actor.querySelector('.wp-body'), '::before');
            return {
              content: style.content,
              width: parseFloat(style.width) || 0,
              height: parseFloat(style.height) || 0,
              opacity: parseFloat(style.opacity) || 0,
              border: style.borderStyle,
            };
          };
          const root = actors.find((actor) => actor.dataset.lineage === 'root');
          const child = actors.find((actor) => actor.dataset.lineage === 'child');
          return {
            regular: regular.map((actor) => ({
              id: actor.dataset.runId,
              width: body(actor).width,
              height: body(actor).height,
            })),
            rootCue: root && cue(root),
            childCue: child && cue(child),
          };
        }"""
    )
    assert measured["regular"], "the realistic workplace has no regular actors to measure"
    undersized = [actor for actor in measured["regular"] if min(actor["width"], actor["height"]) < 40]
    assert not undersized, f"regular actors are still tiny/interchangeable: {undersized[:4]}"
    for role in ("rootCue", "childCue"):
        cue = measured[role]
        assert cue and cue["content"] != "none", f"{role} has no body-level shape"
        assert cue["width"] >= 6 and cue["height"] >= 6 and cue["opacity"] >= 0.7, (
            f"{role} disappears without the nameplate or pod colour: {cue}"
        )


def test_forty_eight_actors_hold_frame_budget_and_input_reaches_the_next_paint(scene, browser):
    """Renderer work stays cheap even when the host cannot deliver every frame on time."""
    workers = [
        {
            "run_id": f"volume-{index}",
            "name": f"worker {index}",
            "agent": "frontend" if index % 2 else "backend",
            "status": "running",
            "zone": "code",
            "activity": "reviewing the production workspace",
            "parent_run_id": None if index % 6 == 0 else f"volume-{index - index % 6}",
            "project": "interact",
            "cost_usd": None,
            "input_tokens": None,
            "idle_seconds": index % 60,
            "department": f"production-{index % 4}",
            "room": f"Production {index % 4}",
        }
        for index in range(48)
    ]
    render = subprocess.run(
        [
            "node",
            "-e",
            (
                "const workplace=require(process.argv[1]);"
                "process.stdout.write(workplace.renderActors(JSON.parse(process.argv[2])))"
            ),
            str(EXT / "out" / "workplace.js"),
            json.dumps({"workers": workers, "links": [], "at": 0}),
        ],
        cwd=EXT,
        capture_output=True,
        text=True,
    )
    assert render.returncode == 0, render.stderr
    renderer_jank_ms = int(os.environ.get("WORKPLACE_TEST_RENDERER_JANK_MS", "0"))
    assert 0 <= renderer_jank_ms <= 100, "the test-only renderer delay must be 0..100ms"
    input_jank_ms = int(os.environ.get("WORKPLACE_TEST_INPUT_JANK_MS", "0"))
    assert 0 <= input_jank_ms <= 100, "the test-only input delay must be 0..100ms"
    page = browser.new_page(
        viewport={"width": 1100, "height": 800}, reduced_motion="no-preference"
    )
    page.add_init_script(
        f"""
        (() => {{
          const nativeFrame = window.requestAnimationFrame.bind(window);
          const probe = window.__workplaceFrameProbe = {{
            control: '',
            enabled: false,
            flushes: 0,
            geometryHooks: 0,
            geometryReads: 0,
            inputJankMs: {input_jank_ms},
            rendererJankMs: {renderer_jank_ms},
            sequence: 0,
            samples: [],
          }};
          window.addEventListener('keydown', (event) => {{
            if (event.key !== 'f' || !probe.inputJankMs) return;
            const jankEnds = performance.now() + probe.inputJankMs;
            while (performance.now() < jankEnds) {{}}
          }}, {{capture: true}});
          for (const property of ['clientWidth', 'clientHeight']) {{
            const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, property);
            if (!descriptor || !descriptor.configurable || !descriptor.get) continue;
            Object.defineProperty(Element.prototype, property, {{
              ...descriptor,
              get() {{
                if (probe.enabled && this.classList?.contains('wp-mini')) {{
                  probe.geometryReads += 1;
                }}
                return descriptor.get.call(this);
              }},
            }});
            probe.geometryHooks += 1;
          }}
          window.requestAnimationFrame = (callback) => nativeFrame((at) => {{
            if (callback.name !== 'frame' || !probe.enabled) {{ callback(at); return; }}
            const active = Boolean(window.__wp && window.__wp.tick && window.__wp.tick.on);
            // Bring unrelated browser-owned animation/style work current before the renderer's
            // envelope begins. The second read stays INSIDE the envelope, so any style/layout
            // dirtied by `frame` itself is still paid for by the renderer measurement.
            const calibrationStarted = performance.now();
            void document.documentElement.offsetWidth;
            probe.flushes += 1;
            const calibration = performance.now() - calibrationStarted;
            const sequence = ++probe.sequence;
            console.timeStamp(
              `wp-self|start|${{probe.control}}|${{sequence}}|${{active ? 1 : 0}}`
            );
            const started = performance.now();
            callback(at);
            if (active && probe.rendererJankMs) {{
              const jankEnds = performance.now() + probe.rendererJankMs;
              while (performance.now() < jankEnds) {{}}
            }}
            // Any renderer-owned layout read remains inside callback(at). The calibration flush
            // above prevents unrelated pending browser style work from entering that envelope;
            // a second harness-owned flush here would instead charge the renderer for work the
            // browser normally batches after every requestAnimationFrame callback has returned.
            console.timeStamp(
              `wp-self|end|${{probe.control}}|${{sequence}}|${{active ? 1 : 0}}`
            );
            probe.samples.push({{
              at,
              active,
              calibration,
              duration: performance.now() - started,
            }});
          }});
        }})();
        """
    )
    page.goto((scene / "dark.html").as_uri())
    cdp = page.context.new_cdp_session(page)
    trace_events = []
    trace_complete = Event()
    cdp.on("Tracing.dataCollected", lambda event: trace_events.extend(event["value"]))
    cdp.on("Tracing.tracingComplete", lambda _: trace_complete.set())
    cdp.send(
        "Tracing.start",
        {
            "categories": "devtools.timeline,blink.console",
            "options": "sampling-frequency=10000",
        },
    )
    try:
        result = page.evaluate(
            """async (actors) => {
          window.__wpApply(actors);
          const wp = window.__wp;
          const probe = window.__workplaceFrameProbe;
          const quantile = (values, q) => {
            const sorted = values.slice().sort((a, b) => a - b);
            return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
          };
          const nextPaint = () => new Promise((painted) => requestAnimationFrame(painted));
          // Let the replacement cast finish its first minimap/layout pass before measuring the
          // camera transition. Initial construction is not a frame-loop cost.
          while (document.querySelectorAll('.wp-dots i').length < 48) await nextPaint();

          const sample = async (name, contentionMs) => {
            wp.run(false);
            wp.follow(false);
            wp.zoom(2);
            await nextPaint();
            probe.flushes = 0;
            probe.geometryReads = 0;
            probe.samples.length = 0;
            probe.control = name;
            probe.enabled = true;

            let contend = contentionMs > 0;
            const hostContention = () => {
              if (!contend) return;
              const ends = performance.now() + contentionMs;
              while (performance.now() < ends) {}
              requestAnimationFrame(hostContention);
            };
            if (contend) requestAnimationFrame(hostContention);

            wp.run(true);
            wp.whole();
            let previous = 0;
            let active = true;
            const schedule = [];
            await new Promise((done) => {
              const tick = (at) => {
                if (previous) schedule.push({active, duration: at - previous});
                if (schedule.length >= 160) { done(); return; }
                previous = at;
                active = !active;
                wp.run(active);
                requestAnimationFrame(tick);
              };
              requestAnimationFrame(tick);
            });
            probe.enabled = false;

            wp.run(true);
            const view = document.querySelector('.wp-view');
            const observeInput = async (key, expectsFollow) => {
              // Every observation starts at the same boundary: immediately after one calibration
              // frame, then exactly one key dispatch, then the next animation frame. There is no
              // retry or post-hoc sample selection.
              wp.follow(false);
              await nextPaint();
              const started = performance.now();
              view.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true}));
              const applied = wp.cam.follow;
              if (applied !== expectsFollow) {
                throw new Error(
                  `${key} synchronously left camera follow=${applied}; expected ${expectsFollow}`
                );
              }
              await nextPaint();
              const duration = performance.now() - started;
              if (expectsFollow) wp.follow(false);
              return {duration, applied};
            };
            const controlPaint = [];
            const inputToPaint = [];
            const inputPaintExcess = [];
            const inputApplied = [];
            const inputPairOrder = [];
            for (let pairIndex = 0; pairIndex < 24; pairIndex++) {
              const order = pairIndex % 2
                ? [['input', 'f', true], ['control', 'q', false]]
                : [['control', 'q', false], ['input', 'f', true]];
              inputPairOrder.push(order.map((item) => item[0]).join('-'));
              const pair = {};
              for (const [kind, key, expectsFollow] of order) {
                pair[kind] = await observeInput(key, expectsFollow);
              }
              controlPaint.push(pair.control.duration);
              inputToPaint.push(pair.input.duration);
              inputPaintExcess.push(pair.input.duration - pair.control.duration);
              inputApplied.push(pair.input.applied);
            }
            wp.run(false);
            contend = false;

            const activeSchedule = schedule.filter((item) => item.active).map((item) => item.duration);
            const pausedSchedule = schedule.filter((item) => !item.active).map((item) => item.duration);
            const activeWork = probe.samples.filter((item) => item.active).map((item) => item.duration);
            const pausedWork = probe.samples.filter((item) => !item.active).map((item) => item.duration);
            const calibrations = probe.samples.map((item) => item.calibration);
            // Wall time around a callback can include browser/host work that is independent of
            // the workplace. The loop alternates live and paused frames in one run, so adjacent
            // frames share that condition. This copy remains as a diagnostic; the assertions use
            // the same adjacent pairs on Chromium's renderer thread clock below.
            const selfWork = [];
            for (let i = 1; i < probe.samples.length; i += 2) {
              const pair = probe.samples.slice(i - 1, i + 1);
              const activeItem = pair.find((item) => item.active);
              const pausedItem = pair.find((item) => !item.active);
              if (activeItem && pausedItem) {
                selfWork.push(Math.max(0, activeItem.duration - pausedItem.duration));
              }
            }
            const slowAt = quantile(pausedSchedule, 0.5) * 1.5;
            const activeSlow = activeSchedule.filter((duration) => duration > slowAt).length;
            const pausedSlow = pausedSchedule.filter((duration) => duration > slowAt).length;
            return {
              name,
              contentionMs,
              scheduleFrames: schedule.length,
              activeScheduleP95: quantile(activeSchedule, 0.95),
              pausedScheduleP95: quantile(pausedSchedule, 0.95),
              scheduleP95Delta: quantile(activeSchedule, 0.95) - quantile(pausedSchedule, 0.95),
              activeSlow,
              pausedSlow,
              slowExcess: activeSlow - pausedSlow,
              activeWorkCount: activeWork.length,
              pausedWorkCount: pausedWork.length,
              rawActiveWorkP95: quantile(activeWork, 0.95),
              rawPausedWorkP95: quantile(pausedWork, 0.95),
              selfWorkCount: selfWork.length,
              selfWorkP95: quantile(selfWork, 0.95),
              selfWorkMax: Math.max(...selfWork),
              calibrationP95: quantile(calibrations, 0.95),
              geometryReads: probe.geometryReads,
              layoutFlushes: probe.flushes,
              inputControl: controlPaint,
              inputToPaint,
              inputPaintExcess,
              inputApplied,
              inputPairOrder,
            };
          };

          const controls = [await sample('idle', 0), await sample('host-contention', 8)];
          wp.run(true);
          return {
            actors: document.querySelectorAll('.wp-actor').length,
            boundActors: Object.keys(wp.bodies).length,
            geometryHooks: probe.geometryHooks,
            inputJankMs: probe.inputJankMs,
            rendererJankMs: probe.rendererJankMs,
            controls,
          };
            }""",
            render.stdout,
        )
    finally:
        cdp.send("Tracing.end")
        while not trace_complete.is_set():
            page.wait_for_timeout(25)
        cdp.detach()

    markers = {}
    for event in trace_events:
        if event.get("name") != "TimeStamp" or "tts" not in event:
            continue
        message = event.get("args", {}).get("data", {}).get("message", "")
        if not message.startswith("wp-self|"):
            continue
        _prefix, edge, control_name, sequence, active = message.split("|")
        key = (control_name, int(sequence), active == "1", event["tid"])
        markers.setdefault(key, {})[edge] = event["tts"]

    thread_samples = {control["name"]: [] for control in result["controls"]}
    for (control_name, sequence, active, _thread), edges in markers.items():
        if set(edges) != {"start", "end"}:
            continue
        thread_samples[control_name].append(
            {
                "sequence": sequence,
                "active": active,
                "duration": (edges["end"] - edges["start"]) / 1000,
            }
        )

    def quantile(values, fraction):
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]

    def nearest_rank_p95(values):
        ordered = sorted(values)
        return ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None

    for control in result["controls"]:
        samples = sorted(thread_samples[control["name"]], key=lambda item: item["sequence"])
        self_work = []
        for index in range(1, len(samples), 2):
            pair = samples[index - 1:index + 1]
            active_sample = next((item for item in pair if item["active"]), None)
            paused_sample = next((item for item in pair if not item["active"]), None)
            if active_sample and paused_sample:
                self_work.append(
                    max(0, active_sample["duration"] - paused_sample["duration"])
                )
        assert self_work, f"CDP produced no complete renderer thread-clock pairs: {markers}"
        control["wallSelfWorkP95"] = control.pop("selfWorkP95")
        control["wallSelfWorkMax"] = control.pop("selfWorkMax")
        control["selfWorkCount"] = len(self_work)
        control["selfWorkP95"] = quantile(self_work, 0.95)
        control["selfWorkMax"] = max(self_work)
        control["inputControlP95"] = nearest_rank_p95(control["inputControl"])
        control["inputP95"] = nearest_rank_p95(control["inputToPaint"])
        control["pairedExcessP95"] = nearest_rank_p95(control["inputPaintExcess"])
        control["inputAppliedCount"] = sum(control["inputApplied"])
    page.close()
    if os.environ.get("WORKPLACE_PERF_REPORT"):
        print("workplace_performance=" + json.dumps(result, sort_keys=True))
    # A 20 ms input budget is only measurable on a host that can still schedule frames. The
    # paused control asks the browser for a 16 ms cadence with nothing to draw; when THAT comes
    # back at 100-180 ms the machine is saturated (a desktop full of Chrome renderers, a parallel
    # suite) and every number below measures the host, not the scene. Say so instead of failing.
    saturated = {
        control["name"]: control["pausedScheduleP95"]
        for control in result["controls"]
        if control["pausedScheduleP95"] > 60
    }
    if saturated:
        pytest.skip(
            f"host cannot schedule an idle 16ms frame loop (paused schedule p95 {saturated}); "
            "the 20ms input budget is unmeasurable here"
        )
    paired_excess_failures = {
        control["name"]: control["pairedExcessP95"]
        for control in result["controls"]
        if control["pairedExcessP95"] > 20
    }
    assert not paired_excess_failures, (
        f"paired input excess p95 exceeds 20ms: {paired_excess_failures}; all metrics: {result}"
    )
    assert result["actors"] == 48, result
    assert result["boundActors"] == 48, (
        f"only {result['boundActors']} of 48 rendered actors participate in the active loop"
    )
    assert result["geometryHooks"] == 2, "the browser exposed no minimap geometry-read seam"
    for control in result["controls"]:
        assert control["geometryReads"] == 0, (
            f"{control['name']} active frames synchronously read minimap geometry: {control}"
        )
        assert control["scheduleFrames"] == 160, control
        assert control["activeWorkCount"] >= 80 and control["pausedWorkCount"] >= 80, control
        assert control["selfWorkCount"] >= 80, control
        assert control["layoutFlushes"] == (
            control["activeWorkCount"] + control["pausedWorkCount"]
        ), f"renderer timing omitted its pre-frame layout calibration: {control}"
        assert control["selfWorkP95"] <= 8, (
            f"{control['name']} renderer-work p95 {control['selfWorkP95']:.2f}ms exceeds 8ms; "
            f"all metrics: {result}"
        )
        assert control["selfWorkMax"] <= 20, (
            f"{control['name']} renderer work blocked one frame for "
            f"{control['selfWorkMax']:.2f}ms; all metrics: {result}"
        )
        assert control["scheduleP95Delta"] <= 8, (
            f"{control['name']} active scheduling added {control['scheduleP95Delta']:.2f}ms "
            f"over interleaved paused frames; all metrics: {result}"
        )
        assert control["slowExcess"] <= max(1, control["scheduleFrames"] * 0.02), (
            f"{control['name']} produced {control['slowExcess']} more visibly late active "
            f"frames than interleaved paused frames; all metrics: {result}"
        )
        assert len(control["inputControl"]) == 24, control
        assert len(control["inputToPaint"]) == 24, control
        assert len(control["inputPaintExcess"]) == 24, control
        assert control["inputPairOrder"] == [
            "control-input" if index % 2 == 0 else "input-control" for index in range(24)
        ], control
        assert len(control["inputApplied"]) == 24, control
        assert control["inputAppliedCount"] == 24, (
            f"{control['name']} applied only {control['inputAppliedCount']} of 24 inputs"
        )
        for input_latency, control_latency, signed_delta in zip(
            control["inputToPaint"],
            control["inputControl"],
            control["inputPaintExcess"],
            strict=True,
        ):
            assert signed_delta == pytest.approx(input_latency - control_latency), control
        input_limit = min(100, max(33.4, control["inputControlP95"]) + 20)
        assert control["inputP95"] <= input_limit, (
            f"{control['name']} input p95 {control['inputP95']:.2f}ms exceeds its "
            f"{input_limit:.2f}ms calibrated limit; all metrics: {result}"
        )


# --- The chat panel's reliability, in a real browser -----------------------------------------
#
# "I want to be able to chat just like in claude code in vscode. And i still have none of that
# that is actually reliable." Two failures that destroy typed text, both invisible to any test
# that only builds strings:
#   - a failed send used to clear the box before the send was confirmed;
#   - a streaming update must not rebuild the document under someone who is mid-sentence.




def _unavailable(why: str) -> None:
    """Skip locally, FAIL in CI.

    Skipping locally is fine — not every machine has the webview toolchain. Skipping in CI makes
    the guard decorative exactly where it is the only thing watching: a probe that silently does
    not run is a note, not an invariant, and this file has already shipped that failure once (four
    tests reported "fixture is not present" for weeks after /tmp was cleaned). `test_paths.py`
    already draws this line; these fixtures did not.
    """
    if os.environ.get("CI"):
        pytest.fail(f"{why} — this must not skip in CI, it is the only thing checking this")
    pytest.skip(why)


@pytest.fixture(scope="session")
def panel_pages(tmp_path_factory):
    """Render the side panel's documents from source, once per session.

    The generator lives IN the repo (`clients/vscode/webview/dev/panels.ts`). It used to be a
    hand-written file in /tmp, so when /tmp was cleaned these tests reported "fixture is not
    present" and skipped — silently, which reads as a deliberate skip rather than a guard that has
    gone. A fixture outside the repo is a test that stops guarding without telling anyone.
    """
    ext = Path(__file__).resolve().parent.parent / "clients" / "vscode"
    out = tmp_path_factory.mktemp("panels")
    bundle = out / "panels.js"
    build = subprocess.run(
        ["npx", "esbuild", "webview/dev/panels.ts", "--bundle", f"--outfile={bundle}",
         "--format=cjs", "--platform=node", "--target=es2022"],
        cwd=ext, capture_output=True, text=True,
    )
    if build.returncode != 0:
        _unavailable(f"could not build the panel fixture: {build.stderr[-300:]}")
    run = subprocess.run(["node", str(bundle), str(out)], capture_output=True, text=True)
    if run.returncode != 0:
        _unavailable(f"could not render the panel fixture: {run.stderr[-300:]}")
    return out


# --- The world's text stays readable, measured on what actually paints ------------------------
#
# A CSS-parsing guard for this already existed and stopped protecting anything the moment the
# tile rewrite renamed the element it watched: it now ERRORS with "no rule '.wp-plate {'" rather
# than failing, and a real light-theme regression walked straight through the gap — character
# names at 1.9:1 against a 4.5:1 floor.
#
# So this measures the RENDERED result instead. It cannot be dodged by a rename, it sees through
# color-mix() and inherited backgrounds that a text parse cannot resolve, and it fails rather than
# errors when a selector disappears.

#: WCAG AA for body text; large text (>=18px, or >=14px bold) may sit at 3.0.
AA_SMALL = 4.5
AA_LARGE = 3.0

_CONTRAST_JS = """() => {
  const px = (c) => {
    const cv = document.createElement('canvas'); cv.width = cv.height = 1;
    const x = cv.getContext('2d'); x.fillStyle = c; x.fillRect(0, 0, 1, 1);
    return [...x.getImageData(0, 0, 1, 1).data].slice(0, 3);
  };
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('[class^="wp-"], [class*=" wp-"]')) {
    if (!el.textContent || !el.textContent.trim()) continue;
    if (el.children.length) continue;                 // only leaves actually paint text
    const cls = el.className.toString().trim().split(/\\s+/)[0];
    if (!cls || seen.has(cls)) continue;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    seen.add(cls);
    // Walk up for the first non-transparent backdrop — the colour the text is really read on.
    let node = el, bg = 'rgba(0, 0, 0, 0)';
    while (node && (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent')) {
      bg = getComputedStyle(node).backgroundColor; node = node.parentElement;
    }
    out.push({cls, size: parseFloat(s.fontSize), weight: s.fontWeight,
              fg: px(s.color), bg: px(bg)});
  }
  return out;
}"""


def _ratio(fg, bg) -> float:
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    def lum(c):
        return 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2])

    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["live", "live-light"])
def test_every_word_in_the_world_is_readable(scene, browser, theme):
    """Both themes, every text role the scene actually paints.

    The light theme is where this breaks: dark backdrops flatter almost any ink, so a guard that
    only ever ran against the dark build passes while the light one is unreadable.
    """
    page_file = scene / f"{theme}.html"
    if not page_file.exists():
        _unavailable(f"{theme}.html was not built")
    pg = browser.new_page(viewport={"width": 1400, "height": 900})
    pg.goto(page_file.as_uri())
    pg.wait_for_timeout(800)
    measured = pg.evaluate(_CONTRAST_JS)
    pg.close()

    assert measured, "no text found in the scene at all — the selectors have moved"
    failures = []
    for item in measured:
        big = item["size"] >= 18 or (item["size"] >= 14 and int(item["weight"] or 400) >= 700)
        floor = AA_LARGE if big else AA_SMALL
        ratio = _ratio(item["fg"], item["bg"])
        if ratio < floor:
            failures.append(f"{item['cls']} at {item['size']:.0f}px: {ratio:.1f}:1 < {floor}")
    assert not failures, f"{theme} text below the readable floor:\n  " + "\n  ".join(failures)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_the_rail_stays_readable_too(browser, panel_pages, theme):
    """The same measurement, on the panel rather than the world.

    The rail grew two new text roles after its last contrast check — the per-row action glyphs
    and the brain badge — which is exactly how a surface drifts under a floor: not in one big
    change, but one small addition at a time, each looking fine against a dark backdrop.
    """
    fixture = panel_pages / "rail" / f"{theme}.html"
    pg = browser.new_page(viewport={"width": 292, "height": 460})
    pg.goto(fixture.as_uri())
    pg.wait_for_timeout(250)
    measured = pg.evaluate(_CONTRAST_JS.replace('[class^="wp-"], [class*=" wp-"]',
                                                ".scope,.counts,.who,.note,.chip,.act,.brain"))
    pg.close()

    assert measured, "no text found in the rail — the selectors have moved"
    failures = [
        f"{i['cls']} at {i['size']:.0f}px: {_ratio(i['fg'], i['bg']):.1f}:1"
        for i in measured
        if _ratio(i["fg"], i["bg"]) < (AA_LARGE if i["size"] >= 18 else AA_SMALL)
    ]
    assert not failures, f"rail {theme} below the readable floor:\n  " + "\n  ".join(failures)


@pytest.mark.parametrize("theme", ["live", "live-light"])
def test_no_two_words_in_the_world_are_drawn_on_top_of_each_other(scene, browser, theme):
    """At a real team's density, in both themes.

    The complaint that started this rewrite was partly that text out-massed the characters. It
    still does when two long activity strings land in the same room: "reviewing the diff a…"
    over "benchmark harness di…" is not a legible workplace, it is a collision.

    Measured over every VISIBLE text-bearing leaf, and all THREE halves of that were learned the
    hard way. Scoping by class compared three header elements while every character label went
    unchecked — a measurement that reports zero because it looked in the wrong place. Then
    counting laid-out-but-invisible elements reported four collisions that are not on screen at
    all, because the activity bubbles sit at opacity 0 until hovered.

    And now the third: the world is a bounded viewport with `overflow: hidden` and a building
    deliberately bigger than it, so most of the cast is OUTSIDE the frame at any moment — and a
    clipped-away element still reports a full bounding box. That is how "artist" was found lying
    over the header while sitting six hundred and fifty pixels above the top of the panel. The
    board by the door is the same case one step in: an opaque strip laid OVER the world at
    z-index 500, so anything under it is covered rather than collided with.

    So every box is CLIPPED to what a reader can actually see — the frame, minus that strip —
    before anything is compared, and a box with nothing left is not a label at all. The assertion
    is unchanged; the scope is now the screen.
    """
    page_file = scene / f"{theme}.html"
    if not page_file.exists():
        _unavailable(f"{theme}.html was not built")
    pg = browser.new_page(viewport={"width": 1400, "height": 900})
    pg.goto(page_file.as_uri())
    pg.wait_for_timeout(2500)          # let the cast settle where it actually stands
    result = pg.evaluate(
        """() => {
          // What a reader can see: the world's own clipped viewport, minus the opaque strip laid
          // over the top of it. Everything is measured inside this and nowhere else.
          const view = document.querySelector('.wp-view').getBoundingClientRect();
          const hud = document.querySelector('.wp-hud');
          const bar = hud ? hud.getBoundingClientRect().bottom : view.top;
          const frame = {l: view.left, r: view.right, t: Math.max(view.top, bar), b: view.bottom};
          const boxes = [];
          for (const el of document.querySelectorAll('*')) {
            const t = (el.textContent || '').trim();
            if (!t || el.children.length) continue;
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            // EFFECTIVE opacity, walking ancestors. The activity bubbles sit at opacity 0 until
            // you hover or until what they say changes — they occupy layout but nobody can see
            // them, and counting those reported four collisions that do not exist on screen.
            let op = 1, n = el;
            while (n && n !== document.body) {
              op *= parseFloat(getComputedStyle(n).opacity) || 0;
              if (getComputedStyle(n).visibility === 'hidden') op = 0;
              n = n.parentElement;
            }
            if (op <= 0.05) continue;
            // Clip to the frame. A label scrolled out of the world, or lying under the board by
            // the door, is not on screen and cannot be drawn over anything.
            const own = hud && hud.contains(el);
            const box = own
              ? {x: r.left, y: r.top, w: r.width, h: r.height}
              : {x: Math.max(r.left, frame.l), y: Math.max(r.top, frame.t),
                 w: Math.min(r.right, frame.r) - Math.max(r.left, frame.l),
                 h: Math.min(r.bottom, frame.b) - Math.max(r.top, frame.t)};
            if (box.w <= 2 || box.h <= 2) continue;
            boxes.push({t: t.slice(0, 24), x: box.x, y: box.y, w: box.w, h: box.h});
          }
          const hits = [];
          for (let i = 0; i < boxes.length; i++)
            for (let j = i + 1; j < boxes.length; j++) {
              const a = boxes[i], b = boxes[j];
              const ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
              const oy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
              // A couple of pixels of antialiasing overlap is not a collision.
              if (ox > 2 && oy > 2) hits.push(`"${a.t}" over "${b.t}"`);
            }
          return {count: boxes.length, hits};
        }"""
    )
    pg.close()
    assert result["count"] > 10, "almost no text found — the scene did not render"
    assert not result["hits"], (
        f"{theme}: {len(result['hits'])} labels drawn over each other:\n  "
        + "\n  ".join(result["hits"][:8])
    )


def test_nobody_stands_on_top_of_anybody(page):
    """Passing through is fine; standing on each other is not.

    Two reports of "crowding" in this scene turned out to be measurement artifacts — a screenshot
    taken mid-walk, and a label check that counted invisible elements. The distinction that
    actually matters is DURATION: two bodies crossing paths overlap for a moment, which is normal
    in a tile world and cheaper than collision avoidance; two bodies sharing a spot for most of a
    window are drawn on top of each other and one of them is invisible.

    So this samples over time and fails only on a PAIR that persists, never on the instantaneous
    count. A test that failed on any overlap at all would fail on a corridor.
    """
    # Never assume the engine is running: a shared page fixture means an earlier test may have
    # frozen it, and sampling a still scene would measure a photograph rather than a workplace.
    page.evaluate("() => { if (window.__wp && window.__wp.tick) window.__wp.tick.on = true; }")
    page.wait_for_timeout(400)

    samples, seen = 14, {}
    for _ in range(samples):
        for pair in page.evaluate(
            """() => {
              const a = [...document.querySelectorAll('.wp-actor')].map(el => {
                const b = el.querySelector('.wp-body') || el;
                const r = b.getBoundingClientRect();
                return {id: el.getAttribute('data-run-id'),
                        x: r.left, y: r.top, w: r.width, h: r.height};
              }).filter(z => z.w && z.h);
              const out = [];
              for (let i = 0; i < a.length; i++)
                for (let j = i + 1; j < a.length; j++) {
                  const ox = Math.min(a[i].x+a[i].w, a[j].x+a[j].w) - Math.max(a[i].x, a[j].x);
                  const oy = Math.min(a[i].y+a[i].h, a[j].y+a[j].h) - Math.max(a[i].y, a[j].y);
                  if (ox > 3 && oy > 3) out.push([a[i].id, a[j].id].sort().join(' + '));
                }
              return out;
            }"""
        ):
            seen[pair] = seen.get(pair, 0) + 1
        page.wait_for_timeout(600)

    stuck = {p: n for p, n in seen.items() if n >= samples * 0.8}
    assert not stuck, (
        "characters sharing a spot for most of the window — one of them cannot be seen:\n  "
        + "\n  ".join(f"{p}: {n}/{samples} frames" for p, n in stuck.items())
    )


def test_no_two_settled_bodies_ever_share_a_seat(page):
    """The exact-zero-distance class, driven deterministically instead of waited for.

    A stroller could lawfully stop ON the seat of a body that was itself out walking — the claim
    check covered positions and destinations, never the seats — and the owner's return is
    unconditional, so the two settled at literally 0.0 tiles: one sprite entirely hidden, two
    nameplates over one visible body. Wall-clock sampling (the test above) catches a pair only if
    it persists through its window; this one steps the engine's own clock through ten simulated
    minutes and checks every two simulated seconds, so the trespass-and-return sequence has
    hundreds of chances to occur and zero opportunity to hide between samples.

    Settled means pathless: walkers crossing are fine, and a courier standing two tiles out for a
    delivery is a conversation, not a collision — the assertion is the HARD floor only, two
    stationary bodies inside one tile of each other.
    """
    shared = page.evaluate(
        """() => {
          const wp = window.__wp;
          wp.tick.on = false;
          const bad = [];
          let t = 1000;
          for (let checkpoint = 0; checkpoint < 300; checkpoint++) {
            for (let i = 0; i < 120; i++) { t += 16.7; wp.step(t); }
            const settled = Object.values(wp.bodies).filter((b) => !b.path && b.el);
            for (let i = 0; i < settled.length; i++)
              for (let j = i + 1; j < settled.length; j++) {
                const a = settled[i], b = settled[j];
                if (Math.abs(a.x - b.x) < 0.75 && Math.abs(a.y - b.y) < 0.75)
                  bad.push(`${a.id} + ${b.id} at (${a.x.toFixed(1)},${a.y.toFixed(1)}) t=${(t / 1000).toFixed(0)}s`);
              }
            if (bad.length > 4) break;
          }
          wp.tick.on = true;
          return bad;
        }"""
    )
    assert not shared, (
        "two stationary bodies inside one tile of each other — one of them is invisible:\n  "
        + "\n  ".join(shared)
    )


def test_a_shadow_never_swallows_the_click_meant_for_a_glyph(page):
    """A cast shadow is scenery; it must not be in the way of the thing it falls on.

    Five capability glyphs failed a hit-test at their own geometric centre, and it looked like a
    z-order bug across sixteen actors. It was not: two of the five were `.wp-shade` — a shadow cast
    down-and-right, lying over the NEIGHBOUR's glyphs with no `pointer-events: none`. Decoration
    that intercepts a pointer is a defect no screenshot can show, because the pixels are correct.
    """
    offenders = page.evaluate(
        """() => [...document.querySelectorAll('.wp-shade')]
              .filter((el) => getComputedStyle(el).pointerEvents !== 'none').length"""
    )
    assert offenders == 0, f"{offenders} shadow(s) can intercept a click meant for what they fall on"


def test_you_can_pull_back_further_than_the_old_camera_allowed(page):
    """Alan: "I also can't pan and unzoom easily in the team window."

    The root cause was not discoverability. `camFit` clamped zoom to WHOLE NUMBERS >= 1, and at
    zoom 1 the viewport already spans less than the building — so no reachable scale showed the
    whole company, the diagnostic handle included. Whole numbers can only zoom IN. Seeing
    everything needs a fractional rung, quantised to the sprite's own pixel so the art stays crisp
    rather than smearing between device pixels.

    So the guard is not "does a button exist" but "is there a scale below 1 at all" — the thing
    whose absence made the complaint unfixable by any amount of better affordances.
    """
    seen = page.evaluate(
        """() => {
             const wp = window.__wp;
             if (!wp || !wp.whole || !wp.cam) return null;
             wp.whole();
             const at_whole = wp.cam.zoom;
             const tiles = (z) => window.innerWidth / (wp.world.tile * z);
             return { at_whole, tiles_whole: tiles(at_whole), tiles_at_one: tiles(1) };
           }"""
    )
    assert seen is not None, "the camera exposes no handle to measure — it must stay measurable"
    assert seen["at_whole"] < 1, (
        f"whole-floor settled at zoom {seen['at_whole']}, so it can only zoom IN; the fractional "
        "rungs are what make pulling back possible"
    )
    assert seen["tiles_whole"] > seen["tiles_at_one"], (
        "pulling back shows no more of the world than zoom 1 did"
    )


def test_every_standing_place_has_the_thing_it_belongs_to_behind_it(tmp_path_factory):
    """Alan: "sprites are very weirdly placed, and are just simply bad."

    That defect had no symptom a screenshot could catch. Everybody was on the floor and nobody was
    in a wall; the placement was simply MEANINGLESS — two ranks that collapsed onto the same row in
    any depth-6 room, an overflow rule that pushed the extra bodies one row NORTH onto the desks
    they were meant to be working at, and a "rest" rank standing three tiles from the nearest thing
    to sit on. Nothing throws, so nothing failed, and by the time it is visible in pixels it is one
    frame of a simulation that never holds still.

    So the rule is checked against the BUILT WORLD, where it is one line: a place is defined by
    what is behind it. Every place that claims something to sit on must have a desk, a couch or a
    bench on the tile directly north of it; a place with nothing declares `perch: false` and its
    occupant is drawn standing. A seat that can say neither is the bug.
    """
    probe = EXT / "webview" / "workplace" / "dev" / "placement.ts"
    if not probe.exists() or shutil.which("npx") is None:
        _unavailable("the extension's webview toolchain is not available here")
    out = tmp_path_factory.mktemp("placement") / "placement.js"
    build = subprocess.run(
        ["npx", "esbuild", str(probe), "--bundle", f"--outfile={out}",
         "--format=cjs", "--platform=node", "--target=es2022"],
        cwd=EXT, capture_output=True, text=True,
    )
    if build.returncode != 0:
        pytest.fail(f"the placement probe would not build:\n{build.stderr}")
    run = subprocess.run(["node", str(out)], capture_output=True, text=True)
    assert run.returncode == 0, (
        "somebody is standing where nothing is:\n" + run.stdout + run.stderr)


def test_the_art_holds_its_contract(tmp_path_factory):
    """The probe this runs guarded the projected-shadow system through six "floating trees"
    reports. The projection died with the hand-drawn art — the Kenney tiles bake their own
    grounding — but the CLASS of defect it guarded did not: art detached from the cell it claims
    to stand on, a `<use>` whose target does not exist (which renders NOTHING, silently), layers
    painting in an order nobody decided, and a seat sealed off from its own door. The probe now
    asserts those, against the composed tiles AND the rendered document, and it is checked HERE
    because a probe nothing runs is a note, not an invariant.
    """
    probe = EXT / "webview" / "workplace" / "dev" / "shadows.ts"
    if not probe.exists() or shutil.which("npx") is None:
        _unavailable("the extension's webview toolchain is not available here")
    out = tmp_path_factory.mktemp("shadows") / "shadows.js"
    build = subprocess.run(
        ["npx", "esbuild", str(probe), "--bundle", f"--outfile={out}",
         "--format=cjs", "--platform=node", "--target=es2022"],
        cwd=EXT, capture_output=True, text=True,
    )
    if build.returncode != 0:
        pytest.fail(f"the shadow probe would not build:\n{build.stderr}")
    run = subprocess.run(["node", str(out)], capture_output=True, text=True)
    assert run.returncode == 0, (
        "a shadow is detached, or thrown the wrong way:\n" + run.stdout + run.stderr)


def test_nobodys_line_hangs_off_the_edge_of_the_panel(scene, browser):
    """At the width this actually ships in: a VS Code side bar.

    A line is centred on its body and held at a constant SCREEN size, so at 400px most of the floor
    is within half a bubble of an edge and a sentence gets cut in half by the viewport. An
    independent critic found exactly that — `"...face graph ..."` with no owner attached.

    Two things had to be true and neither was. WHO gets a bubble was decided by the speech layout,
    which runs every 420ms — and in 420ms a body walks a tile and a half and the camera can cross a
    room, so the character was long gone by the time anyone looked. And the layout culled against
    the LOGICAL camera while the paint uses the one that has actually glided there, which are a
    room apart during a move. Both are now settled per FRAME, in screen terms.

    Sampled over frames rather than once, because this defect is a fraction of a second wide: a
    single screenshot catches it only by luck, which is how it survived a whole rebuild.
    """
    page_file = scene / "live.html"
    if not page_file.exists():
        _unavailable("live.html was not built")
    pg = browser.new_page(viewport={"width": 400, "height": 900}, reduced_motion="no-preference")
    pg.goto(page_file.as_uri())
    pg.wait_for_timeout(2000)
    got = pg.evaluate(
        """() => new Promise((done) => {
             const v = document.querySelector('.wp-view').getBoundingClientRect();
             let worst = 0, readings = 0, frames = 0, who = '';
             const tick = () => {
               frames++;
               for (const a of document.querySelectorAll('.wp-actor.is-saying')) {
                 const say = a.querySelector('.wp-say');
                 const r = say.getBoundingClientRect();
                 readings++;
                 const over = Math.max(v.left - r.left, r.right - v.right);
                 if (over > worst) { worst = over; who = say.textContent.trim().slice(0, 24); }
               }
               if (frames < 240) requestAnimationFrame(tick);
               else done({frames, readings, worst: Math.round(worst), who});
             };
             requestAnimationFrame(tick);
           })"""
    )
    pg.close()
    assert got["readings"] > 50, f"almost nobody spoke in {got['frames']} frames — the scene is not live"
    # Two pixels of antialiasing is not a line hanging off the panel; 173 was.
    assert got["worst"] <= 3, (
        f'a line hung {got["worst"]}px outside the panel ("{got["who"]}") '
        f"over {got['frames']} frames"
    )


def test_the_camera_does_not_rewrite_the_scene_sixty_times_a_second(page):
    """`camApply` stamped `data-far` / `data-follow` on the scene root every frame, whether or not
    they had changed — sixty attribute writes a second, each one an invalidation, for a value that
    changes when you touch the camera and at no other time.

    Written as a guard rather than a claimed win: measured frame time was the same before and
    after (the honest answer, after sampling a control), so this pins the CORRECTNESS — a still
    camera should be silent — instead of a speedup nobody demonstrated.
    """
    writes = page.evaluate(
        """() => new Promise((done) => {
             const root = document.querySelector('.wp-world') || document.body;
             let n = 0;
             const obs = new MutationObserver((rs) => {
               for (const r of rs) if (r.attributeName === 'data-far' || r.attributeName === 'data-follow') n++;
             });
             obs.observe(root, { attributes: true });
             setTimeout(() => { obs.disconnect(); done(n); }, 900);
           })"""
    )
    assert writes <= 2, (
        f"the camera rewrote its own state {writes} times while nothing about it changed"
    )


def test_plants_hold_still_and_a_calm_floor_stays_calm(page):
    """'There are plants still moving inside' — the eighth 'floating trees' report, finally
    decoded: the pot plants carried an ambient sway animation, and settled agents re-roamed
    every 0.9-2.6 SECONDS. Motion must carry meaning: a plant has no state to express, and a
    resting colleague sits still. Task walks (spawn, errand, courier) stay untouched."""
    swaying = page.evaluate(
        "document.querySelectorAll('.wp-lv-sway').length")
    assert swaying == 0, "a plant is animated — plants have no state to express"
    # Ambient calm: over 6 real seconds with no registry events, the floor's total wandering
    # must be a whisper. Before the fix every stander re-rolled a stroll ~every 2s.
    drift = page.evaluate(
        """(async () => {
             // TILE position, never the transform string: bodies breathe (a sub-pixel bob) at
             // rest, which is life, not wandering. What the owner cannot click is a body that
             // TRAVELS, so travel is what this measures.
             const sim = window.__wp;
             const pos = () => Object.fromEntries(Object.entries(sim.bodies).map(
               ([id, b]) => [id, Math.round(b.x * 4) + ':' + Math.round(b.y * 4)]));
             // Silence the harness's own driver first: this preview swaps the WHOLE team
             // between two snapshots every 7s, so bodies are under standing orders to walk —
             // that is the fixture's purpose (pinned by the tests above). Ambient calm can only
             // be measured with nothing happening, so stop the scene changes and the couriers.
             for (let i = 1; i < 9999; i++) window.clearInterval(i);
             for (let i = 1; i < 9999; i++) window.clearTimeout(i);
             if (sim.clearNotes) sim.clearNotes();
             // Let the cast finish whatever it was doing — arriving is legitimate motion, and
             // earlier tests on this shared page leave journeys in flight. The complaint is
             // about a SETTLED floor that will not sit still, so wait for settled.
             const walking = () => Object.values(sim.bodies).filter(b => b.path).length;
             for (let i = 0; i < 40 && walking() > 0; i++) {
               await new Promise(r => setTimeout(r, 250));
             }
             if (sim.clearNotes) sim.clearNotes();
             await new Promise(r => setTimeout(r, 500));
             const before = pos();
             await new Promise(r => setTimeout(r, 6000));
             const after = pos();
             let moved = 0;
             for (const id in before) if (after[id] !== before[id]) moved++;
             return { moved, total: Object.keys(before).length };
           })()""")
    assert drift["total"] > 3, "the fixture floor lost its cast — fixture bug, not a pass"
    assert drift["moved"] <= max(2, drift["total"] // 8), (
        f"{drift['moved']}/{drift['total']} actors wandered in 6s — the floor is still restless")


def test_a_body_under_the_pointer_holds_still(page):
    """'The agents are moving too much. I can't click on them' — a walker freezes while the
    pointer rests on it, so a click lands on a colleague, never on where one used to be."""
    frozen = page.evaluate(
        """(async () => {
             const sim = window.__wp; if (!sim || !sim.send || !sim.bodies) return { skip: true };
             // Send somebody on a long walk, then park the pointer on them mid-journey.
             const ids = Object.keys(sim.bodies);
             if (ids.length < 2) return { skip: true };
             const id = ids[0], b = sim.bodies[id], far = sim.bodies[ids[ids.length - 1]];
             // Another body's own seat is guaranteed reachable, so the walk really starts.
             sim.send(id, Math.round(far.seatX ?? far.x), Math.round(far.seatY ?? far.y));
             await new Promise(r => setTimeout(r, 400));
             const a = document.querySelector(`.wp-actor[data-run-id="${id}"]`);
             if (!a || !b.path) return { skip: true };
             const r = a.getBoundingClientRect();
             const ev = new MouseEvent('pointermove',
               { clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, bubbles: true });
             document.querySelector('.wp-view').dispatchEvent(ev);
             await new Promise(r2 => setTimeout(r2, 200));
             const t1 = a.style.transform;
             await new Promise(r2 => setTimeout(r2, 900));
             const t2 = a.style.transform;
             return { t1, t2 };
           })()""")
    if frozen.get("skip"):
        pytest.skip("harness exposes no walk hook")
    assert frozen["t1"] == frozen["t2"], "a hovered walker kept moving — the click has no target"
