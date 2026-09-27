#!/usr/bin/env python3
"""Local authenticated prompt catalog used by conversation integration tests."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Handler(BaseHTTPRequestHandler):
    content: str
    digest: str
    revision: str
    body_revision: str
    second_content: str | None
    second_digest: str | None
    second_revision: str | None
    corrupt_second: bool
    token: str
    log_file: Path

    def _record(self, status: int) -> None:
        with self.log_file.open("a") as log:
            log.write(json.dumps({"path": self.path, "status": status}) + "\n")

    def do_GET(self) -> None:
        if self.headers.get("authorization") != f"Bearer {self.token}":
            self._record(401)
            self.send_error(401)
            return
        if self.path == "/v1/catalog":
            entries = [{
                "key": {"namespace": "interact", "slug": "system"},
                "channel": "stable",
                "revision": self.revision,
                "digest": self.digest,
                "lock_version": 1,
            }]
            if self.second_digest is not None:
                entries.append({
                    "key": {"namespace": "interact", "slug": "review"},
                    "channel": "stable",
                    "revision": self.second_revision,
                    "digest": self.second_digest,
                    "lock_version": 1,
                })
            payload = {
                "entries": entries,
                "cursor": "complete-snapshot",
                "server_timestamp": datetime.now(UTC).isoformat(),
            }
        elif self.path == f"/v1/revisions/interact/system/{self.digest}":
            payload = {
                "key": {"namespace": "interact", "slug": "system"},
                "revision": self.body_revision,
                "digest": self.digest,
                "content": self.content,
                "source_commit": "synthetic",
                "created_at": datetime.now(UTC).isoformat(),
            }
        elif self.second_digest is not None and self.path == (
            f"/v1/revisions/interact/review/{self.second_digest}"
        ):
            payload = {
                "key": {"namespace": "interact", "slug": "review"},
                "revision": self.second_revision,
                "digest": self.second_digest,
                "content": (
                    f"{self.second_content} corrupt"
                    if self.corrupt_second else self.second_content
                ),
                "source_commit": "synthetic",
                "created_at": datetime.now(UTC).isoformat(),
            }
        else:
            self._record(404)
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._record(200)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--content", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--body-revision")
    parser.add_argument("--second-content")
    parser.add_argument("--second-revision")
    parser.add_argument("--corrupt-second", action="store_true")
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    _Handler.content = args.content
    _Handler.digest = hashlib.sha256(args.content.encode()).hexdigest()
    _Handler.revision = args.revision
    _Handler.body_revision = args.body_revision or args.revision
    _Handler.second_content = args.second_content
    _Handler.second_digest = (
        hashlib.sha256(args.second_content.encode()).hexdigest()
        if args.second_content is not None else None
    )
    _Handler.second_revision = args.second_revision
    _Handler.corrupt_second = args.corrupt_second
    _Handler.token = args.token
    _Handler.log_file = args.log_file
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    args.port_file.write_text(str(server.server_port))
    server.serve_forever()
