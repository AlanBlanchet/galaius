"""The `BROWSER` an agent program's own sign-in runs with (`AgentPrograms`): the address the program
opens is handed to its sign-in job, which shows it to this PC's owner on the web, then opened as the
system opens any address. Where no browser opens (a runner with no desktop, a failed opener), the
owner still has the address to open on this PC. Nothing goes over the network from here: the
address only lands in a file of the job's own folder."""

import logging
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)
#: The environment variable carrying the `SignInHandoff` (JSON) to the helper.
HANDOFF_VARIABLE = "GALAIUS_SIGN_IN_HANDOFF"
#: The helper's console-script name (`[project.scripts]`).
HELPER = "galaius-sign-in-browser"


class SignInHandoff(BaseModel):
    """Where one sign-in job reads the address its program opened, and the opener the person had."""

    model_config = ConfigDict(frozen=True)

    #: The file the address is written to (inside the job's private folder).
    link_file: Path
    #: The `BROWSER` the program would have used without the helper ("": the system's own opener).
    browser: str = ""
    #: Whether the address is also opened here; False for a sign-in whose own printed address opens in the person's
    #: browser wherever he is (`AgentSignIn.paste`): no browser pops up on this PC.
    opens: bool = True

    @staticmethod
    def helper() -> Path | None:
        """This runtime's helper executable: beside its interpreter (a virtual environment, the
        default install) or in its user scripts folder (`pip --user`). None when this install has none."""
        folders = dict.fromkeys([sysconfig.get_path("scripts"), str(Path(sys.executable).parent), sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user"))])
        found = shutil.which(HELPER, path=os.pathsep.join(folders))
        return None if found is None else Path(found)

    def environment(self) -> dict[str, str]:
        """What the sign-in runs with on top of its environment: the helper as `BROWSER`; in an install without it, a
        `BROWSER` opening nothing when the address must not open here (`opens`), else nothing (said in the log: the
        program opens the browser itself, its address never reaches the web, and the web offers the terminal command)."""
        helper = self.helper()
        if helper is None:
            if not self.opens and (quiet := shutil.which("true")) is not None:  # its printed address reaches the web anyway
                return {"BROWSER": quiet}
            logger.warning("%s is missing from this install: a sign-in's address cannot reach the web", HELPER)
            return {}
        return {"BROWSER": str(helper), HANDOFF_VARIABLE: self.model_dump_json()}

    def link(self) -> str | None:
        """The last address the program opened, None before it opened one."""
        try:
            return self.link_file.read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None

    def hand(self, address: str) -> None:
        """`address` written whole (a reader never sees half of it)."""
        partial = self.link_file.with_suffix(".partial")
        partial.write_text(address, encoding="utf-8")
        partial.replace(self.link_file)

    def open(self, address: str) -> None:
        """`address` opened as the system would without the helper; never waited for. The opener runs
        with the person's own `BROWSER` back (xdg-open falls back to `$BROWSER`: the helper again)."""
        if sys.platform == "win32" and not self.browser:
            os.startfile(address)  # the person's default browser, as Windows opens any address
            return
        opener = self.browser or ("open" if sys.platform == "darwin" else shutil.which("xdg-open"))
        if not opener:
            return
        environment = {name: value for name, value in os.environ.items() if name not in (HANDOFF_VARIABLE, "BROWSER")}
        subprocess.Popen([opener, address], env={**environment, **({"BROWSER": self.browser} if self.browser else {})},
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=sys.platform != "win32")


def main(arguments: list[str] | None = None) -> int:
    """`galaius-sign-in-browser <address>`: called by the signing-in program as its `BROWSER`."""
    handoff = SignInHandoff.model_validate_json(os.environ[HANDOFF_VARIABLE])
    for address in sys.argv[1:] if arguments is None else arguments:
        handoff.hand(address)
        if not handoff.opens:
            continue
        try:
            handoff.open(address)
        except OSError:  # no opener here: the owner opens the address the web shows
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
