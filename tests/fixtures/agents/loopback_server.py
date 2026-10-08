"""The loopback HTTP server the fake servers here run: bound to 127.0.0.1 on a free port, which it
publishes whole (written beside, then renamed) so a reader never sees a partial number."""

import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer's own bind resolves the host's name (`socket.getfqdn`): a reverse lookup of
        # 127.0.0.1 that can take seconds on macOS. Its name is the address itself.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def serve(handler: type[BaseHTTPRequestHandler], port_file: Path) -> None:
    server = LoopbackServer(("127.0.0.1", 0), handler)
    partial = port_file.with_suffix(".partial")
    partial.write_text(str(server.server_port))
    partial.replace(port_file)
    server.serve_forever()
