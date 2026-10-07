"""Where a signed release comes from, and the keys it must be signed by.

The document is verified against the public keys this package itself carries
(`galaius/data/release-keys/*.pub`) before one byte of it is believed: a server, a DNS answer or a
local port can hand out files, never make them trusted."""

import base64
import hashlib
import re
from importlib.resources import files
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat, load_pem_private_key, load_pem_public_key
from pydantic import BaseModel, ConfigDict, field_validator

from galaius import USER_AGENT
from galaius.upgrade.release import Release, ReleaseFile, ReleaseRefused


class ReleaseKeys(BaseModel):
    """The ed25519 public keys a release must be signed by: those this package ships, minus every
    key retired (by this package or any runtime this computer ran), unless a caller names others
    (tests, a key rotation being prepared)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    keys: tuple[Ed25519PublicKey, ...]
    folder: ClassVar = files("galaius") / "data" / "release-keys"

    @staticmethod
    def fingerprint_of(key: Ed25519PublicKey) -> str:
        return "sha256:" + hashlib.sha256(key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).hexdigest()

    @classmethod
    def retired_by_package(cls) -> set[str]:
        """The keys this package retires (`retired.txt`: one fingerprint per line, then a reason)."""
        entry = cls.folder / "retired.txt"
        lines = entry.read_text(encoding="utf-8").splitlines() if entry.is_file() else []
        return {line.split()[0] for line in lines if line.startswith("sha256:")}

    @classmethod
    def shipped(cls, retired: set[str] = frozenset()) -> "ReleaseKeys":
        found = [load_pem_public_key(entry.read_bytes()) for entry in cls.folder.iterdir() if entry.name.endswith(".pub")] if cls.folder.is_dir() else []
        gone = retired | cls.retired_by_package()
        return cls(keys=tuple(key for key in found if cls.fingerprint_of(key) not in gone))

    def without(self, retired: set[str]) -> "ReleaseKeys":
        return self.model_copy(update={"keys": tuple(key for key in self.keys if self.fingerprint_of(key) not in retired)})

    def verify(self, document: bytes, signature: bytes) -> Release:
        """The release `document` says, once one of these keys signed exactly these bytes."""
        if not self.keys:
            raise ReleaseRefused("this install trusts no release key: automatic upgrades stay off")
        try:
            raw = base64.b64decode(signature.strip(), validate=True)
        except ValueError as error:
            raise ReleaseRefused("the release signature is not base64") from error
        for key in self.keys:
            try:
                key.verify(raw, document)
            except InvalidSignature:
                continue
            return Release.model_validate_json(document)
        raise ReleaseRefused("the release is not signed by a key this install trusts")


class ReleaseSigner(BaseModel):
    """The publisher's side: one ed25519 private key, kept outside every repository."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    key: Ed25519PrivateKey

    @classmethod
    def generated(cls) -> "ReleaseSigner":
        return cls(key=Ed25519PrivateKey.generate())

    @classmethod
    def load(cls, path: Path) -> "ReleaseSigner":
        return cls(key=load_pem_private_key(path.read_bytes(), password=None))

    def private_pem(self) -> bytes:
        return self.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())

    def public_pem(self) -> bytes:
        return self.key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

    def keys(self) -> ReleaseKeys:
        return ReleaseKeys(keys=(self.key.public_key(),))

    def fingerprint(self) -> str:
        return ReleaseKeys.fingerprint_of(self.key.public_key())

    def signature(self, document: bytes) -> bytes:
        """`release.json.sig` for these exact document bytes."""
        return base64.b64encode(self.key.sign(document)) + b"\n"

    def sign(self, release: Release) -> tuple[bytes, bytes]:
        """(document, signature) exactly as a source serves them."""
        document = release.document()
        return document, self.signature(document)

class ReleaseSource(BaseModel):
    """Where releases come from: `document`, `signature` and every signed file under one base
    URL. https only; plain http only on this computer's own loopback (the owner's SSH tunnel), and
    that only because the signature, not the channel, is what is trusted."""

    model_config = ConfigDict(frozen=True)
    base: str
    kind: Literal["server", "github"]
    document_name: ClassVar[str] = "release.json"
    signature_name: ClassVar[str] = "release.json.sig"
    timeout: ClassVar[httpx.Timeout] = httpx.Timeout(60.0, connect=10.0)
    loopback: ClassVar[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

    @field_validator("base")
    @classmethod
    def _transport(cls, base: str) -> str:
        parts = urlsplit(base)
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in cls.loopback):
            raise ValueError(f"{base}: releases come over https (plain http only from this computer's loopback)")
        return base.rstrip("/")

    @classmethod
    def server(cls, origin: str) -> "ReleaseSource":
        return cls(base=f"{origin.rstrip('/')}/install/release", kind="server")

    @classmethod
    def github(cls, repository: str = "AlanBlanchet/galaius") -> "ReleaseSource":
        """The latest GitHub release's assets (`releases/latest/download/<name>` redirects to them)."""
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository):
            raise ValueError(f"{repository}: not an owner/name GitHub repository")
        return cls(base=f"https://github.com/{repository}/releases/latest/download", kind="github")

    def client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, follow_redirects=self.kind == "github", headers={"User-Agent": USER_AGENT}, trust_env=False)

    def fetch(self, http: httpx.Client, name: str, limit: int) -> bytes:
        with http.stream("GET", f"{self.base}/{name}") as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content += chunk
                if len(content) > limit:
                    raise ReleaseRefused(f"{name} is larger than a release file may be")
        return bytes(content)

    def latest(self, http: httpx.Client, keys: ReleaseKeys) -> Release:
        document = self.fetch(http, self.document_name, 64 << 10)
        return keys.verify(document, self.fetch(http, self.signature_name, 4 << 10))

    def download(self, http: httpx.Client, file: ReleaseFile, into: Path) -> Path:
        """`file` into the folder `into`, checked against its signed sha256 on the bytes written."""
        path = into / file.file
        path.write_bytes(file.check(self.fetch(http, file.file, 256 << 20)))
        return path
