"""`python -m galaius`: the same CLI as the `galaius` program (the Windows logon task starts it
through `pythonw`, which has no console window)."""

from galaius.cli import main

main()
