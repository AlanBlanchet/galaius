#!/usr/bin/env python3
"""Loopback fixture whose response body stays active beyond the console startup deadline."""

import signal
import socket
import sys
import time

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(1)
print(listener.getsockname()[1], flush=True)


signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
connection, _ = listener.accept()
with connection:
    connection.recv(4096)
    print("requested", file=sys.stderr, flush=True)
    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n{")
    while True:
        time.sleep(4)
        connection.sendall(b" ")
