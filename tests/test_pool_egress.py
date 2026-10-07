"""Adversarial proof of safeguard #2 (default-deny egress + explicit allow-list): a pooled run
reaches only its declared destinations; an unlisted host and the cloud metadata endpoint are
blocked even when something real is listening there. Real gVisor containers + a real per-run
`nft` ruleset; needs passwordless (or already-cached) sudo for `nft`, skipped otherwise."""

import http.server
import subprocess
import threading
from uuid import uuid4

import pytest
from galaius_core import EgressAllowEntry, EgressPolicy

from galaius.sandbox import gvisor_available, run_pooled

_METADATA_IP = "169.254.169.254"
_ALLOWED_IP = "1.1.1.1"    # Cloudflare's own resolver — always up, used only as a reachability probe.
_UNLISTED_IP = "8.8.8.8"   # Google's own resolver — real, up, deliberately NOT allow-listed.

_CONNECT_SNIPPET = "import socket,sys; s=socket.socket(); s.settimeout(3)\ntry:\n s.connect((sys.argv[1], int(sys.argv[2])))\n print('OPEN')\nexcept OSError as e:\n print('BLOCKED', e)\n"


def _nft_ready() -> bool:
    try:
        return subprocess.run(["sudo", "-n", "nft", "list", "tables"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not (gvisor_available() and _nft_ready()), reason="needs gVisor + passwordless sudo nft on this machine")


@pytest.fixture
def metadata_listener():
    """A REAL HTTP server bound to the metadata-endpoint address — proves a block is the firewall
    acting, not merely nobody answering there."""
    subprocess.run(["sudo", "ip", "addr", "add", f"{_METADATA_IP}/32", "dev", "lo"], capture_output=True, timeout=10)
    server = http.server.HTTPServer((_METADATA_IP, 8080), http.server.SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        subprocess.run(["sudo", "ip", "addr", "del", f"{_METADATA_IP}/32", "dev", "lo"], capture_output=True, timeout=10)


def test_metadata_endpoint_is_blocked_even_with_a_real_listener(metadata_listener) -> None:
    policy = EgressPolicy(allow=(EgressAllowEntry(host=_ALLOWED_IP, port=443),))
    result = run_pooled(run_id=uuid4(), command=["python3", "-c", _CONNECT_SNIPPET, _METADATA_IP, "8080"], egress=policy, timeout=30)
    assert "BLOCKED" in result.stdout


def test_allowed_host_is_reachable_and_unlisted_host_is_blocked() -> None:
    policy = EgressPolicy(allow=(EgressAllowEntry(host=_ALLOWED_IP, port=443),))

    allowed = run_pooled(run_id=uuid4(), command=["python3", "-c", _CONNECT_SNIPPET, _ALLOWED_IP, "443"], egress=policy, timeout=30)
    unlisted = run_pooled(run_id=uuid4(), command=["python3", "-c", _CONNECT_SNIPPET, _UNLISTED_IP, "443"], egress=policy, timeout=30)

    assert "OPEN" in allowed.stdout
    assert "BLOCKED" in unlisted.stdout


def test_a_run_declaring_no_egress_need_gets_no_network_at_all() -> None:
    result = run_pooled(run_id=uuid4(), command=["python3", "-c", _CONNECT_SNIPPET, _ALLOWED_IP, "443"], timeout=30)
    assert "BLOCKED" in result.stdout


def test_blocked_hosts_are_never_allow_listable_even_if_the_caller_tries() -> None:
    """Defense in depth: a caller mistakenly passing the metadata address as an `allow` entry
    still gets it dropped — `_EgressNetwork` filters it out before the `nft` ruleset is built."""
    policy = EgressPolicy(allow=(EgressAllowEntry(host=_METADATA_IP, port=8080), EgressAllowEntry(host=_ALLOWED_IP, port=443)))
    result = run_pooled(run_id=uuid4(), command=["python3", "-c", _CONNECT_SNIPPET, _METADATA_IP, "8080"], egress=policy, timeout=30)
    assert "BLOCKED" in result.stdout


def test_egress_block_is_logged() -> None:
    policy = EgressPolicy(allow=(EgressAllowEntry(host=_ALLOWED_IP, port=443),))
    marker_run_id = uuid4()
    run_pooled(run_id=marker_run_id, command=["python3", "-c", _CONNECT_SNIPPET, _UNLISTED_IP, "443"], egress=policy, timeout=30)
    log = subprocess.run(["sudo", "dmesg"], capture_output=True, text=True, timeout=10).stdout
    assert f"POOL-EGRESS-BLOCKED-DEFAULT galaius-pool-{marker_run_id.hex[:20]}" in log
