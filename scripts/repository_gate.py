#!/usr/bin/env python3
"""Staged-content scan for credential-like material, run by .githooks/pre-commit.

The hook materializes this file from the index and runs `hook` on every commit.
Added lines of every staged blob are matched against the detector table below;
any match exits non-zero and blocks the commit. Only the finding class and its
location are printed -- the matched bytes are never echoed, and a path that is
itself credential-shaped is redacted.
"""
import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

FindingClass = Literal[
    "private-key",
    "credential-prefix",
    "authorization",
    "credential-assignment",
    "credential-uri",
    "personal-path",
    "confidential-term",
    "unscannable-oversize",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Finding(_StrictModel):
    path: str
    line: int | Literal["binary"]
    finding_class: FindingClass


class _GateError(RuntimeError):
    pass


class RepositoryGate(BaseModel):
    root: Path

    # Detector declarations are structurally suppressed only while this exact tuple is parsed.
    _DETECTORS: ClassVar[tuple[tuple[FindingClass, bytes], ...]] = (
        ("private-key", rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
        ("credential-prefix", rb"(?:gh[pousr]_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|sk-ant-api03-[A-Za-z0-9_-]{40,}|sk-(?:proj-|svcacct-)[A-Za-z0-9_-]{32,}|sk-[A-Za-z0-9]{32,}|AKIA[A-Z0-9]{16})"),
        ("authorization", rb"(?i)(?:authorization\s*[:=]\s*|\bbearer\s+)[A-Za-z0-9._~+/=-]{20,}"),
        ("credential-assignment", rb"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\b\s*[:=]\s*['\"]?(?!example|placeholder|dummy|redacted|test)[^'\"\s]{12,}"),
        ("credential-uri", rb"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@"),
        ("personal-path", rb"(?:^|[=:'\"\s])/(?:home|Users)/[^/\s]+/[^\s'\"]+"),
    )
    _MAX_BLOB_BYTES = 8 * 1024 * 1024

    @classmethod
    def discover(cls, root: Path):
        probe = cls(root=root.resolve())
        top_level = probe._git(b"rev-parse", b"--show-toplevel")
        assert top_level is not None
        return cls(root=Path(os.fsdecode(top_level.rstrip(b"\n"))).resolve())

    def scan_staged(self):
        findings: list[_Finding] = []
        confidential_terms = self._confidential_terms()
        for path, unchanged_source in self._staged_destinations():
            display_path = os.fsdecode(path)
            size = self._blob_size(path)
            if size > self._MAX_BLOB_BYTES:
                findings.append(
                    _Finding(path=display_path, line="binary", finding_class="unscannable-oversize")
                )
                continue
            indexed = self._blob(b":./" + path)
            if indexed is None:
                raise _GateError("indexed scanner input is unavailable")
            if unchanged_source is not None:
                original = self._blob(b"HEAD:./" + unchanged_source)
                if original is None:
                    raise _GateError("rename source is unavailable")
                if original == indexed:
                    continue
            if b"\0" in indexed:
                findings.extend(self._scan_bytes(display_path, "binary", indexed, confidential_terms))
                continue
            added = self._added_lines(path, indexed, unchanged_source)
            for line_number, line in added:
                findings.extend(self._scan_bytes(display_path, line_number, line, confidential_terms))
        if findings:
            for finding in findings:
                path = json.dumps(
                    self._safe_finding_path(finding.path, confidential_terms), ensure_ascii=True
                )
                print(f"[repository-gate] {path}:{finding.line}: {finding.finding_class}", file=sys.stderr)
            print("[repository-gate] blocked: remove staged confidential content", file=sys.stderr)
            raise _GateError("staged content contains confidential material")

    def _git(self, *arguments: bytes, allowed_failure: bool = False):
        command = [b"git", *arguments]
        try:
            result = subprocess.run(command, cwd=os.fsencode(self.root), capture_output=True, check=False)
        except OSError as error:
            raise _GateError("Git executable unavailable") from error
        if result.returncode != 0:
            if allowed_failure:
                return None
            raise _GateError("Git command failed")
        return result.stdout

    def _staged_destinations(self):
        output = self._git(b"diff", b"--cached", b"--name-status", b"-z", b"--diff-filter=ACMR")
        assert output is not None
        fields = output.rstrip(b"\0").split(b"\0") if output else []
        paths: list[tuple[bytes, bytes | None]] = []
        index = 0
        while index < len(fields):
            status = fields[index]
            index += 1
            if status[:1] in {b"R", b"C"}:
                if index + 1 >= len(fields):
                    raise _GateError("Git returned malformed staged paths")
                source = fields[index]
                index += 1
                paths.append((fields[index], source if status[:1] == b"R" else None))
                index += 1
            else:
                if index >= len(fields):
                    raise _GateError("Git returned malformed staged paths")
                paths.append((fields[index], None))
                index += 1
        return paths

    def _blob(self, specification: bytes):
        return self._git(b"show", b"--no-textconv", specification, allowed_failure=True)

    def _blob_size(self, path: bytes):
        output = self._git(b"cat-file", b"-s", b":./" + path, allowed_failure=True)
        if output is None:
            raise _GateError("indexed scanner input is unavailable")
        try:
            return int(output)
        except ValueError as error:
            raise _GateError("Git returned an invalid indexed blob size") from error

    def _added_lines(self, path: bytes, indexed: bytes, renamed_from: bytes | None):
        new_lines = indexed.split(b"\n")
        diff = self._git(
            b"diff",
            b"--cached",
            b"--unified=0",
            b"--text",
            b"--no-color",
            b"--no-ext-diff",
            b"--no-textconv",
            b"--",
            *((renamed_from, path) if renamed_from is not None else (path,)),
        )
        assert diff is not None
        added: list[tuple[int, bytes]] = []
        for line in diff.split(b"\n"):
            hunk = re.match(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if hunk is None:
                continue
            start = int(hunk.group(1))
            count = int(hunk.group(2) or b"1")
            if count == 0:
                continue
            if start < 1 or start + count - 1 > len(new_lines):
                raise _GateError("Git returned invalid staged hunk metadata")
            added.extend((number, new_lines[number - 1]) for number in range(start, start + count))
        return added

    def _scan_bytes(
        self,
        path: str,
        line_number: int | Literal["binary"],
        content: bytes,
        confidential_terms: list[bytes],
    ):
        findings: list[_Finding] = []
        declaration_class: bytes | None = None
        if path == "scripts/repository_gate.py":
            declaration = re.fullmatch(rb'\s*\("([a-z-]+)", rb".*"\),\s*', content)
            if declaration is not None:
                declaration_class = declaration.group(1)
        for finding_class, pattern in self._DETECTORS:
            if declaration_class == finding_class.encode():
                continue
            if re.search(pattern, content):
                findings.append(_Finding(path=path, line=line_number, finding_class=finding_class))
        if any(term in content for term in confidential_terms):
            findings.append(_Finding(path=path, line=line_number, finding_class="confidential-term"))
        return findings

    def _safe_finding_path(self, path: str, confidential_terms: list[bytes]):
        encoded = os.fsencode(path)
        if any(re.search(pattern, encoded) for _finding_class, pattern in self._DETECTORS) or any(
            term in encoded for term in confidential_terms
        ):
            return "<redacted-path>"
        return path

    def _confidential_terms(self):
        configured = self._git(b"config", b"--local", b"--path", b"--get", b"interact.confidentialTermsFile", allowed_failure=True)
        if not configured:
            return []
        raw_path = os.fsdecode(configured.rstrip(b"\n"))
        git_dir_output = self._git(b"rev-parse", b"--absolute-git-dir")
        assert git_dir_output is not None
        git_dir = Path(os.fsdecode(git_dir_output.rstrip(b"\n"))).resolve()
        configured_path = Path(raw_path)
        path = git_dir / configured_path if not configured_path.is_absolute() else configured_path
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(git_dir)
            metadata = path.lstat()
        except (ValueError, OSError) as error:
            raise _GateError("confidential-term file must be beneath the Git directory") from error
        if resolved != path.absolute() or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise _GateError("confidential-term file must be a regular non-symlink with mode 0600")
        content = self._read_stable_file(path, metadata, "confidential-term")
        terms = [line for line in content.splitlines() if len(line) >= 4]
        return terms

    def _read_stable_file(self, path: Path, metadata: os.stat_result, label: str):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if before.st_dev != metadata.st_dev or before.st_ino != metadata.st_ino:
                    raise _GateError(f"{label} file changed during validation")
                content = stream.read(self._MAX_BLOB_BYTES + 1)
                after = os.fstat(stream.fileno())
        except OSError as error:
            raise _GateError(f"{label} file is unreadable") from error
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        final_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if identity != final_identity:
            raise _GateError(f"{label} file changed during validation")
        if len(content) > self._MAX_BLOB_BYTES:
            raise _GateError(f"{label} file is too large")
        return content

    @staticmethod
    def cli(argv: list[str]):
        parser = argparse.ArgumentParser(description="Scan staged content for credential-like material")
        subparsers = parser.add_subparsers(dest="action", required=True)
        subparsers.add_parser("scan-staged")
        # `.githooks/pre-commit` invokes this name; the commit gate is the staged scan.
        subparsers.add_parser("hook")
        parser.parse_args(argv)
        try:
            RepositoryGate.discover(Path.cwd()).scan_staged()
        except _GateError as error:
            print(f"[repository-gate] FAIL: {error}", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(RepositoryGate.cli(sys.argv[1:]))
