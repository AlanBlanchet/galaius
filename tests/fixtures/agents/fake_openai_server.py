#!/usr/bin/env python3
"""One-endpoint OpenAI-compatible HTTP server for explicit API-route tests."""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Handler(BaseHTTPRequestHandler):
    log_path: Path
    delay: float

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        with self.log_path.open("a") as stream:
            stream.write(json.dumps({
                "method": "POST",
                "path": self.path,
                "model": request["model"],
                "messages": request["messages"],
            }) + "\n")
        time.sleep(self.delay)
        payload = json.dumps({
            "id": "chatcmpl-synthetic",
            "object": "chat.completion",
            "created": 1,
            "model": request["model"],
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "synthetic api answer"},
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args()
    _Handler.log_path = args.log_file
    _Handler.delay = args.delay
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    args.port_file.write_text(str(server.server_port))
    server.serve_forever()
