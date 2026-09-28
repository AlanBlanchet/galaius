"""Files only this computer's user may read: the machine credential, the CLI's account key, the
folders holding them. One owner of "private" per operating system (`PRIVATE_FILES`):

- POSIX: mode 0600 / 0700, owned by the current uid, written without following a link. Secrets are
  stored as they are: the mode is the protection.
- Windows: a protected DACL with one entry, the current user's SID (nothing inherited from the
  profile folder); a file is accepted only when no other principal than that user, SYSTEM or the
  Administrators group (who can read any file anyway) is granted access, by plain allow entries
  only (a conditional or object allow entry refuses the file). Secrets are also sealed with DPAPI
  in the user's scope (`dpapi:` + base64; a token its owner wrote by hand stays as written). What
  that adds is narrow: a copy of the file taken off this computer (backup, synced folder, disk
  image) is unreadable without this user's Windows logon. Any program running as this user can
  unseal it (the entropy is public), exactly as it could read a 0600 file on Linux. An
  administrator resetting a local account's password loses the sealed value: reading it then says
  to sign in again (`interact login`)."""

import base64
import os
import stat
import sys
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

if sys.platform == "win32":
    import ntsecuritycon
    import pywintypes
    import win32api
    import win32crypt
    import win32cryptcon
    import win32security


class PrivateFiles(BaseModel):
    """What "private to this user" means on this system (`restrict`, `_verify`, `seal`, `unseal`,
    `_settle`), and everything built on it: atomic private writes, checked reads, one-secret files."""

    model_config = ConfigDict(frozen=True)
    #: The largest private file read back (credentials and small JSON settings, never data).
    limit: ClassVar[int] = 1 << 20

    def directory(self, path: Path) -> Path:
        """`path` (and missing parents) created, then made private; returns it."""
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.restrict(path)
        return path

    def restrict(self, path: Path) -> None:
        raise NotImplementedError

    def check(self, path: Path) -> None:
        """PermissionError unless `path` is a regular file (or folder) only this user can read."""
        self._verify(path, path.lstat())

    def _verify(self, path: Path, info: os.stat_result) -> None:
        raise NotImplementedError

    def seal(self, secret: str) -> str:
        """How a secret is stored at rest (`unseal` reads it back)."""
        return secret

    def unseal(self, stored: str) -> str:
        return stored

    def write_text(self, path: Path, text: str) -> None:
        """Atomically replaced: a temporary private file beside it, flushed, then renamed over it."""
        self.directory(path.parent)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                self.restrict(temporary)
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._settle(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def read_text(self, path: Path) -> str:
        """The file's text, checked on the descriptor it is read through (no link followed);
        ValueError past `limit`."""
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as stream:
            self._verify(path, os.fstat(stream.fileno()))
            content = stream.read(self.limit + 1)
        if len(content) > self.limit:
            raise ValueError(f"{path} is larger than a private settings file can be")
        return content.decode("utf-8")

    def write_secret(self, path: Path, secret: str) -> None:
        self.write_text(path, self.seal(secret) + "\n")

    def read_secret(self, path: Path) -> str:
        """One secret (a token or key) from its own file, sealed by `write_secret` or written by its
        owner; ValueError when missing, not private, a link, empty or over 4 KiB."""
        try:
            stored = self.read_text(path).strip()
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError(f"the token file {path} cannot be read safely ({error})") from error
        if not stored or len(stored) > 4096:
            raise ValueError(f"the token file {path} is empty or larger than 4 KiB")
        return self.unseal(stored)

    def _settle(self, folder: Path) -> None:
        """The rename itself survives a power cut."""
        raise NotImplementedError


class PosixPrivateFiles(PrivateFiles):
    """POSIX: private = the mode bits and the owner uid; secrets stored as they are."""

    def restrict(self, path: Path) -> None:
        path.chmod(0o700 if path.is_dir() else 0o600)

    def _verify(self, path: Path, info: os.stat_result) -> None:
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise PermissionError(f"{path} must be private to this user (mode 0600, a folder 0700, owned by you)")

    def _settle(self, folder: Path) -> None:
        """The folder's entry is flushed."""
        descriptor = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class WindowsPrivateFiles(PrivateFiles):
    """Windows: private = a protected DACL naming only this user, and secrets sealed by DPAPI."""

    #: Principals that already read every file on the computer: their entries never make a file public.
    trusted: ClassVar[frozenset[str]] = frozenset({"S-1-5-18", "S-1-5-32-544"})
    prefix: ClassVar[str] = "dpapi:"
    #: Mixed into every sealed value. Public (it is in this source): it keeps interact's blobs apart
    #: from other programs' DPAPI data, never from a program of this user that reads this file.
    entropy: ClassVar[bytes] = b"interact.private-files.v1"

    @staticmethod
    def user():
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), ntsecuritycon.TOKEN_QUERY)
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]

    def restrict(self, path: Path) -> None:
        user = self.user()
        inherit = (ntsecuritycon.OBJECT_INHERIT_ACE | ntsecuritycon.CONTAINER_INHERIT_ACE) if path.is_dir() else 0
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inherit, ntsecuritycon.FILE_ALL_ACCESS, user)
        win32security.SetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            user, None, dacl, None)

    def _verify(self, path: Path, info: os.stat_result) -> None:
        # Windows opens through a link: the path's own entry says whether it is one.
        if (info.st_file_attributes | path.lstat().st_file_attributes) & stat.FILE_ATTRIBUTE_REPARSE_POINT or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise PermissionError(f"{path} must be a plain file or folder (not a link or junction)")
        descriptor = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                                         win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
        user, owner, dacl = win32security.ConvertSidToStringSid(self.user()), descriptor.GetSecurityDescriptorOwner(), descriptor.GetSecurityDescriptorDacl()
        allowed = {user, *self.trusted}
        # An inherit-only entry grants nothing on this file or folder (only on what is created in it, where it is checked).
        entries = () if dacl is None else tuple(ace for ace in (dacl.GetAce(index) for index in range(dacl.GetAceCount()))
                                                if not ace[0][1] & ntsecuritycon.INHERIT_ONLY_ACE)
        # Deny entries only narrow access; any allow entry but a plain one (conditional, object, callback) refuses the file.
        kinds = {ace[0][0] for ace in entries} - {ntsecuritycon.ACCESS_ALLOWED_ACE_TYPE, ntsecuritycon.ACCESS_DENIED_ACE_TYPE}
        others = {win32security.ConvertSidToStringSid(ace[-1]) for ace in entries if ace[0][0] == ntsecuritycon.ACCESS_ALLOWED_ACE_TYPE} - allowed
        owned = win32security.ConvertSidToStringSid(owner)
        if dacl is None or kinds or owned not in allowed or others:
            reason = "no access list" if dacl is None else f"unusual access entries {sorted(kinds)}" if kinds else f"owned by {owned}" if owned not in allowed else f"also readable by {', '.join(sorted(others))}"
            raise PermissionError(f"{path} must be private to this user ({reason}; run `interact login` again to rewrite it)")

    def seal(self, secret: str) -> str:
        sealed = win32crypt.CryptProtectData(secret.encode("utf-8"), "interact", self.entropy, None, None, win32cryptcon.CRYPTPROTECT_UI_FORBIDDEN)
        return self.prefix + base64.b64encode(sealed).decode("ascii")

    def unseal(self, stored: str) -> str:
        """A sealed value opened; one its owner wrote by hand (no `prefix`) as it is."""
        if not stored.startswith(self.prefix):
            return stored
        try:
            _, opened = win32crypt.CryptUnprotectData(base64.b64decode(stored[len(self.prefix):], validate=True), self.entropy, None, None, win32cryptcon.CRYPTPROTECT_UI_FORBIDDEN)
        except (pywintypes.error, ValueError) as error:
            raise ValueError("this key cannot be unsealed by this Windows user (password reset, or copied from another computer); run `interact login` again") from error
        return opened.decode("utf-8")

    def _settle(self, folder: Path) -> None:
        """NTFS journals the rename; a folder cannot be opened as a file to flush it."""


PRIVATE_FILES: PrivateFiles = WindowsPrivateFiles() if sys.platform == "win32" else PosixPrivateFiles()
