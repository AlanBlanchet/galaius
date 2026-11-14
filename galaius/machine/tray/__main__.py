"""`python -m galaius.machine.tray`: the tray icon process (`TrayApp`), apart from the CLI: it loads only what it shows."""

from galaius.machine.tray.app import TrayApp

if __name__ == "__main__":
    raise SystemExit(TrayApp.main())
