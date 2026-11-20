"""The editor extension's state, and delivering it on request."""

from galaius.diagnostics.extension_status import deliver_extension, extension_status


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
