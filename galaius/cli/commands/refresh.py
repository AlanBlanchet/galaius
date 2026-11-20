"""`galaius refresh`: re-fetch the live model catalog and benchmark scores the dashboard reads."""

from galaius.catalog import live_sources


def refresh_live_data() -> None:
    """Re-fetch the live model catalog and benchmark scores the dashboard reads.

    Python is the SOLE writer of those cache files — the extension used to fetch and write the
    catalog itself with a narrower schema, so whichever side wrote last decided whether prices
    existed. Now the extension asks for this instead. A source that did not refresh is printed
    with its reason and the age of the copy still served, and the command exits 1.
    """

    outcomes = live_sources.refresh_all()
    done = [name for name, why in outcomes.items() if why is None]
    if done:
        print("Refreshed: " + ", ".join(done))
    for name, why in outcomes.items():
        if why is not None:
            print(f"{name} NOT refreshed: {why}")
    if len(done) < len(outcomes):
        raise SystemExit(1)
