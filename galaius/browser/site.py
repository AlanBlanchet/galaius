"""One site a lent browser may reach (`BrowserManager.lend`): its origin, and the proxy its context goes through.

The browser's own checks (a route, a page brought back) see a navigation's first request only: a redirect's later hops,
a subresource, a websocket leave before them. The proxy sees every connection the context opens, loopback included
(`LOOPBACK_PROXIED`), and opens one only to the lent site's host and port: whatever a connection carries after its first request
reaches that site, never another."""

import re
import socket
import threading
from urllib.parse import urlsplit

#: Chromium sends loopback addresses past a proxy unless told otherwise.
LOOPBACK_PROXIED = "<-loopback>"
HEAD_MAX = 16384


#: What a browser reads otherwise than Python's `urlsplit` (`\\` as `/`, a tab or a newline dropped, `user@` before a
#: host): an address holding one is no address of the lent site, whatever `urlsplit` makes of it.
_AMBIGUOUS = re.compile(r"[\\\s@%\x00-\x1f\x7f]")


def origin(url: str) -> str | None:
    """`url`'s origin (`scheme://host:port`, lower-cased) when it is a plain http(s) address a browser reads the same
    way (`_AMBIGUOUS`), its port 1-65535; None otherwise."""
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or _AMBIGUOUS.search(parts.netloc) or "\\" in url or port == 0:
        return None
    return f"{parts.scheme}://{parts.hostname.lower()}:{port or (443 if parts.scheme == 'https' else 80)}"


class SiteProxy:
    """An HTTP proxy on a loopback port of its own reaching `site`'s host and port alone (CONNECT for https and
    websockets, absolute-form for plain http); anything else answered 403."""

    def __init__(self, site: str) -> None:
        parts = urlsplit(site)
        self.host, self.port = (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)
        self._server = socket.create_server(("127.0.0.1", 0))
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def server(self) -> str:
        return f"http://127.0.0.1:{self._server.getsockname()[1]}"

    def close(self) -> None:
        self._server.close()

    def allowed(self, request_line: str) -> bool:
        method, _, rest = request_line.partition(" ")
        target = rest.split(" ", 1)[0]
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            return host.strip("[]").lower() == self.host and port == str(self.port)
        parts = urlsplit(target)
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            return False
        return parts.scheme == "http" and (parts.hostname or "").lower() == self.host and port == self.port

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return   # closed with its session
            threading.Thread(target=self._carry, args=(client,), daemon=True).start()

    def _carry(self, client: socket.socket) -> None:
        with client:
            head = b""
            while b"\r\n\r\n" not in head and len(head) < HEAD_MAX:
                if not (chunk := client.recv(4096)):
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            if not self.allowed(line):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            try:
                upstream = socket.create_connection((self.host, self.port), timeout=20)
            except OSError:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            with upstream:
                upstream.settimeout(None)
                if line.startswith("CONNECT "):
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    head = head.split(b"\r\n\r\n", 1)[1]
                else:
                    head = _origin_form(head)
                if head:
                    upstream.sendall(head)
                forward = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
                forward.start()
                _pipe(upstream, client)
                forward.join(timeout=5)


def _origin_form(head: bytes) -> bytes:
    """A plain-http request as its server expects it (`GET /path`, never the absolute address a proxy is sent), and its
    connection closed after its answer: the browser's next request opens a connection of its own, checked again."""
    lines, _, body = head.partition(b"\r\n\r\n")
    first, *headers = lines.split(b"\r\n")
    method, target, version = first.split(b" ", 2)
    parts = urlsplit(target.decode("latin-1"))
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    kept = [header for header in headers if header.split(b":", 1)[0].strip().lower() not in (b"connection", b"proxy-connection", b"keep-alive")]
    return b"\r\n".join([b" ".join((method, path.encode("latin-1"), version)), *kept, b"Connection: close"]) + b"\r\n\r\n" + body


def _pipe(source: socket.socket, sink: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        for end in (source, sink):
            try:
                end.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
