"""The TLS trust for the server connections httpx does not open (machine channel, `urlopen` file
and prompt transfers): httpx's own CA bundle, environment ignored, as galaius's account clients do.

Python's default context loads Windows' system stores, the cached intermediates included, as trust
anchors. An expired cross-sign there (ISRG Root X2 signed by X1, expired 2025-09-15) fails a valid
chain with "certificate has expired" while every httpx call to the same server succeeds."""

import ssl
from functools import cache

import httpx


@cache
def server_tls() -> ssl.SSLContext:
    return httpx.create_ssl_context(trust_env=False)
