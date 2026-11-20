"""The sections `status`, `providers` and `doctor` print: media transport, resolved models, Ollama, stale MCP servers, the extension, desktop sandboxes."""

import subprocess
from galaius.inference import ollama
from galaius.agents.providers import (
    MEDIA_PROVIDERS,
)
from galaius.cli.command_bootstrap import Config
from galaius.desktop import orphans
from galaius.diagnostics.extension_status import deliver_extension, extension_status
from galaius.models.registry import Model
from galaius.runtime import _LiveConfig, config
from galaius.mcp.registry import kill_stale_servers, latest_version, stale_servers


def print_media_transport(config: Config | _LiveConfig, indent: str = "  ") -> None:
    """Print the configured visual transport and registry-backed subscription CLI readiness."""
    order = tuple(config.media_provider_order)
    print(
        f"{indent}media        : backend={config.media_backend} "
        f"billing={config.media_billing}  order={' → '.join(order) or 'none'}"
    )
    for name in order:
        provider = MEDIA_PROVIDERS[name]
        availability = (
            "installed — subscription login is verified when a visual request runs"
            if provider.available()
            else f"not installed — install and log in with the {provider.binary} CLI"
        )
        warning = config.extra_usage_warning(name)
        confirmation = (
            f"warning: {warning}" if warning
            else "no-extra-usage confirmed (account state remains unverifiable)"
        )
        print(f"{indent}  · {name:<10} {availability}; {confirmation}")
    if config.media_billing == "session_only":
        print(
            f"{indent}  no metered API fallback by galaius; vendor session credit controls "
            "remain account-side; audio is disabled until media.billing=api_allowed"
        )
    elif config.media_backend == "session":
        print(
            f"{indent}  visual work stays on sessions; audio may use its configured API/local "
            "backend"
        )
    else:
        print(f"{indent}  metered API use is explicitly allowed")


def print_resolved_models(indent: str = "  ") -> None:
    """Print which model each VLM TASK resolves to with current keys — answers "what runs when I
    do X, and why that default?". Organised BY TASK (what a user actually picks) with the config
    knob (the role) named in brackets — different jobs can have different defaults without a
    separate model setting per task (else all resolve to the same best VLM). Real axes: MODALITY
    (image/component/video/audio) and STAKES — review_ui/verify_ui layer a quality tier on the
    image model, the sovereign tier's cheapest clearing model at low/medium, frontier at
    high/critical. Shared by status / providers / doctor / version so every surface tells one
    story. Every role is a CRITERION now — this always shows what it RESOLVES TO, never the bare
    rule text alone, and says plainly when nothing resolves (a blank here is the defect the owner
    has complained about before)."""

    Model.load_registry()

    def _resolved(role: str) -> tuple[str, str]:
        """(display string, bare model id) for a role given the current keys."""
        rule = config.criteria_for(role)
        try:
            mid = config.resolve_model(role)
        except RuntimeError as err:
            return f"— (criterion: {rule})  ⚠ {err}", ""
        return f"{mid} (criterion: {rule})", mid

    def _line(task: str, role: str) -> None:
        print(f"{indent}{task:<33}→ {_resolved(role)[0]}  [{role}]")
        # What it stepped over. "Are we using the best model?" is unanswerable from the chosen id
        # alone — reads the same whether the walk found the strongest available or a stale default
        # returned with no walk at all. Naming the stronger models it couldn't reach, and why,
        # makes the answer checkable and shows which key to add.
        explanation = config.explain_model(role)
        if explanation:
            print(f"{indent}{'':<33}  ↑ {explanation}")

    _line("screenshot(query) · describe", "image")
    _line("get_interactive_elements", "component")
    _line("transcribe", "audio")
    _line("record · video understanding", "video")

    # review_ui / verify_ui pick by STAKES, not role: sovereign tier's cheapest clearing model for
    # a quick look (low/medium), frontier image model for final sign-off (high/critical).
    frontier = _resolved("image")[1] or "—"
    sovereign = config.resolve_quality_model("low")
    if sovereign:
        critique = f"{sovereign} (low/medium tier) · {frontier} (high/critical)"
    else:
        critique = f"{frontier} (all tiers — sovereign criteria {config.tier_sovereign_criteria or '(default)'!r} clears nothing, so low/medium fall back to frontier)"
    print(f"{indent}{'review_ui · verify_ui (critique)':<33}→ {critique}")
    print(f"{indent}{'measure_ui (contrast)':<33}→ deterministic — no model")


#: Enough to see the shortlist without burying the rest of `doctor` — a cloud catalog is ~19 rows.
OLLAMA_SHOWN = 8


def print_ollama(indent: str = "  ") -> None:
    """Name what the Ollama daemon actually serves, so "did galaius see the model I pulled?" is
    answerable without guessing.

    Vision models are named individually — those are the ones galaius can DRIVE; the rest are
    counted, not listed, since a wall of text-only ids would bury the rest of doctor with nothing
    actionable. Silent when there's no daemon — someone not running Ollama must not read a line
    about it.
    """

    found = ollama.serving()
    if not found:
        return
    # Where they CAME from, not where we looked first: discovery walks a ladder, and this is also
    # the endpoint a completion is sent to — the two stay checkable against each other.
    where = found[0].base or ollama.candidate_bases()[0]
    vision = sorted((m for m in found if m.vision), key=lambda m: m.name)
    rest = sorted((m for m in found if not m.vision), key=lambda m: m.name)

    usable = f"{len(vision)} vision-capable" if vision else "none vision-capable"
    print(f"{indent}{'ollama':<14}: {len(found)} available on {where} — {usable}")
    for model in vision[:OLLAMA_SHOWN]:
        print(f"{indent}  · {model.describe()}")
    if len(vision) > OLLAMA_SHOWN:
        print(f"{indent}  · … +{len(vision) - OLLAMA_SHOWN} more vision models")
    if rest:
        names = ", ".join(m.name for m in rest[:OLLAMA_SHOWN])
        more = f", +{len(rest) - OLLAMA_SHOWN} more" if len(rest) > OLLAMA_SHOWN else ""
        print(f"{indent}  · text-only ({len(rest)}): {names}{more}")


def print_stale_servers(indent: str = "  ", fix: bool = False) -> None:
    """Flag any RUNNING MCP server that loaded an OLDER version than is now available — the silent
    stale-server trap (a long-lived `galaius mcp` keeps serving code imported at startup, so a
    shipped fix never reaches it until reconnected). Names the pid so the user knows which editor
    window's server to reconnect. With ``fix=True`` (``galaius doctor --fix``) it restarts them
    itself (SIGTERM → editor respawns galaius on current code). Nothing printed when every server
    is current."""

    stale = stale_servers()
    if not stale:
        return
    latest = latest_version()
    if fix:
        killed = kill_stale_servers()  # only pids CONFIRMED terminated — never an unverified claim
        unresolved = [s for s in stale if s.get("pid") not in killed]
        if killed:
            pids = ", ".join(str(p) for p in killed)
            print(f"{indent}✓ stopped {len(killed)} stale MCP server(s) (pid {pids}), each was serving "
                  f"code older than v{latest}. Most hosts start a fresh v{latest} process on their next "
                  f"tool call — if galaius tools are still missing afterward, reconnect (or fully "
                  f"restart) that MCP connection in your editor; a stopped process is not a guarantee "
                  f"every host reconnects on its own (#144).")
        if unresolved:
            pids = ", ".join(str(s.get("pid")) for s in unresolved)
            print(f"{indent}⚠ could not confirm {len(unresolved)} stale server(s) stopped (pid {pids}) — "
                  f"not galaius mcp, already gone, or no permission to signal it. Reconnect that MCP "
                  f"connection (or fully restart the editor) to load v{latest}.")
        return
    print(f"{indent}⚠ stale MCP server(s) — serving code older than this tree; reconnect to load fixes")
    print(f"{indent}   (add --fix to this command to stop them):")
    for s in stale:
        if s.get("reason") == "code":
            # The common case between releases: the version never moved, the code did.
            print(f"{indent}    pid {s['pid']}: v{s.get('version')} — started before the last edit")
        else:
            print(f"{indent}    pid {s['pid']}: v{s.get('version')} (tree is v{latest})")


def print_extension_status(indent: str = "  ", fix: bool = False) -> None:
    """Flag a VS Code extension the user is running that is not the one in this tree.

    A version or compiled-bundle mismatch needs installation; a current bundle loaded before its
    build needs only a restart. Remedies are intentionally distinct — neither substitutes for the
    other — and nothing is printed when the running editor has this build.
    """

    st = extension_status()
    if not st:
        return
    if st["remedy"] == "install":
        if st["reason"] == "version":
            print(
                f"{indent}⚠ VS Code extension v{st['installed']} installed, "
                f"tree is v{st['tree']} — repackage and reinstall to load fixes"
            )
        else:
            print(
                f"{indent}⚠ VS Code extension v{st['installed']} compiled bundle differs from "
                "this tree — repackage and reinstall before restarting"
            )
        if fix:
            # Pair detection with remedy, only on explicit --fix: installing an extension makes VS
            # Code reload its extension hosts, killing whatever session asked for it — often this
            # command's own.

            print(f"{indent}   packaging and installing v{st['tree']}…")
            if deliver_extension():
                print(f"{indent}   ✓ installed v{st['tree']}. FULLY CLOSE AND REOPEN a window — a "
                      "reload is served by the same extension host and will not pick it up.")
            else:
                print(f"{indent}   could not deliver it; the messages above say why.")
    else:
        n, total = st.get("behind", 1), st.get("running", 1)
        print(f"{indent}⚠ VS Code extension v{st['installed']} was rebuilt after {n} of {total} "
              "running editor process(es) started — those are serving the OLD build")
        print(f"{indent}   a reinstall at the same version does NOT reach a running window; "
              "fully restart it (or use a fresh --user-data-dir)")


def print_sandboxes(real_display: str | None) -> None:
    """Which sandbox displays exist right now, and who owns each.

    Answers "why do I see two Xephyr windows?" — every galaius server owns its own sandbox, so
    one window per server that launched something is BY DESIGN. Not by design: a window whose
    owner is gone, called out here and swept on the next launch.
    """

    try:
        servers = orphans._list_x_servers()
    except (OSError, subprocess.SubprocessError):
        return
    if not servers:
        return
    print(f"  sandboxes     : {len(servers)} open (one per galaius server that has launched an app)")
    for srv in servers:
        where = orphans.display_of(srv.cmdline) or "?"
        clients = len(orphans.display_clients(where)) if where != real_display else 0
        owner = "ORPHANED — no owner left; swept on the next launch" if orphans.is_orphan(srv) \
            else f"owner pid {srv.ppid}"
        print(f"                  {where}  {clients} app(s)  ({owner})")
