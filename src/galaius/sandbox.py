"""Per-run isolation and default-deny egress for the shared compute pool (threat-model boundary
4, `~/.github/research/threat-model-shared-compute-2026-09-24.md`).

A pooled run never executes as a bare subprocess sharing the machine's real filesystem, PID
namespace or network with the next tenant scheduled there — that is correct only for today's
workspace-private path (`MachineRunner._run_script` / `_run_model`, unchanged, still the default
until `galaius_core.POOL_SHARING_ENABLED` flips). A pooled run instead gets:

1. A fresh, single-use gVisor (`runsc`) Docker container (`--rm`, its own filesystem staged from
   a per-run COPY of only the declared input files — never a bind-mount of the shared working
   directory, so nothing else on disk is even reachable to leak). Destroyed after the run;
   nothing added to it persists to the next container.
2. Default-deny egress: no `EgressPolicy.allow` entries -> `--network none` (Docker's built-in
   "no network device at all" mode — structurally unreachable, not merely firewalled). One or
   more `allow` entries -> a dedicated per-run bridge network plus an `nft` ruleset installed in
   this module's OWN base chains (a separate nftables table, evaluated independently of Docker's
   own tables at `prerouting`/`forward`, priority -300 — before Docker's NAT/forward decisions so
   a block can never race a permissive default): every declared `(host, port)` pair is resolved to
   IPs by THIS trusted process (never inside the sandbox), and `galaius_core.BLOCKED_EGRESS_HOSTS`
   is dropped+logged unconditionally, even if a caller mistakenly allow-lists it — verified 2026-
   09-24 against a real listener bound to 169.254.169.254 (metadata-endpoint address) sharing the
   same host: blocked despite the listener answering when reached directly.

gVisor picked over Firecracker/Kata (the threat model's other named candidate): installed +
verified working here (`docker run --runtime=runsc`, gVisor's own apt repo — the identical repo
and command work on an Ubuntu-based Scaleway image, no target-specific step). NVIDIA GPU
passthrough for a pooled vision/inference run is gVisor's `nvproxy` — UNVERIFIED against the exact
driver/GPU combination Scaleway ships for this codebase; route to `web-researcher` before a pooled
GPU run actually ships. Firecracker needs KVM nested-virt (unreliable on a cloud VM without a
bare-metal offer) and VFIO device passthrough for GPU access — a much heavier operational lift.
"""

import ipaddress
import json
import logging
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from galaius_core import BLOCKED_EGRESS_HOSTS, EgressPolicy

logger = logging.getLogger(__name__)

RUNTIME = "runsc"
#: Small, already-pinned base image (no build step, no registry allow-list to reason about beyond
#: the pull that happens once at machine enrollment time, outside any tenant's run).
DEFAULT_IMAGE = "python:3.12-slim"
_NFT_PRIORITY = -300  # before Docker's own DOCKER-USER/DOCKER-FORWARD chains (priority 0/-1 range).


class SandboxUnavailable(RuntimeError):
    """gVisor is not registered as a Docker runtime on this machine — a pooled run must refuse
    outright, never silently fall back to a bare subprocess or a namespace-only sandbox."""


def gvisor_available() -> bool:
    try:
        completed = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and RUNTIME in completed.stdout


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str


def _resolve(host: str) -> list[str]:
    """IPv4 addresses for `host`, resolved by THIS trusted process — the sandboxed container never
    performs its own DNS resolution for an allow-listed destination, since a malicious/compromised
    resolver inside the sandbox is untrusted input the firewall rule must not depend on. IPv4 only
    (this build's `nft` ruleset declares `ipv4_addr` sets; every `CLOUD_INSTANCE_CATALOG` egress
    target is IPv4 today — an IPv6-only allow entry is refused rather than silently dropped)."""
    try:
        parsed = ipaddress.ip_address(host)
        if parsed.version != 4:
            raise ValueError(f"IPv6 egress is not supported by this sandbox build: {host!r}")
        return [host]
    except ValueError as error:
        if "IPv6" in str(error):
            raise
    infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
    return sorted({info[4][0] for info in infos})


class _EgressNetwork:
    """One per-run Docker bridge network plus its `nft` allow-list, torn down together after the
    run. A run with an empty `EgressPolicy` never constructs one of these — it runs on
    `--network none` instead, network hardware absent entirely."""

    def __init__(self, run_id: UUID, policy: EgressPolicy) -> None:
        self.name = f"galaius-pool-{run_id.hex[:20]}"
        self.table = f"galaius_pool_{run_id.hex[:20]}"
        self.policy = policy
        self._bridge_if: str | None = None

    def __enter__(self) -> "_EgressNetwork":
        # Everything from here on must leave no dangling Docker network if it raises: a failed
        # `nft` install (a bad hostname, a transient sudo hiccup) must never leave a
        # firewall-less bridge network sitting around for a later run to accidentally join.
        subprocess.run(["docker", "network", "create", "--driver", "bridge", self.name], check=True, capture_output=True, text=True, timeout=20)
        try:
            network_id = subprocess.run(["docker", "network", "inspect", self.name, "--format", "{{.Id}}"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
            self._bridge_if = f"br-{network_id[:12]}"
            allowed_pairs: set[tuple[str, int]] = set()
            for entry in self.policy.allow:
                if entry.host in BLOCKED_EGRESS_HOSTS:
                    continue  # never installed as an allow rule regardless of what the caller declared
                for ip in _resolve(entry.host):
                    allowed_pairs.add((ip, entry.port))
            blocked_ips: set[str] = set()
            for host in BLOCKED_EGRESS_HOSTS:
                try:
                    blocked_ips.update(_resolve(host))
                except socket.gaierror:
                    pass  # a metadata hostname unreachable/unresolvable on THIS network has nothing to block
                except ValueError:
                    logger.warning("blocked host %r is IPv6-only; this sandbox build enforces IPv4 egress rules only", host)
            ruleset = self._nft_ruleset(blocked_ips, allowed_pairs)
            subprocess.run(["sudo", "nft", "-f", "-"], input=ruleset, check=True, capture_output=True, text=True, timeout=15)
        except BaseException:
            subprocess.run(["docker", "network", "rm", self.name], capture_output=True, text=True, timeout=20)
            raise
        return self

    def _nft_ruleset(self, blocked_ips: set[str], allowed_pairs: set[tuple[str, int]]) -> str:
        blocked_elements = ", ".join(sorted(blocked_ips)) or "169.254.169.254"
        allow_elements = ", ".join(f"{ip} . {port}" for ip, port in sorted(allowed_pairs)) or "0.0.0.0 . 1"
        return f"""
table inet {self.table} {{
  set blocked_hosts {{ type ipv4_addr; elements = {{ {blocked_elements} }} }}
  set allowed_dests {{ type ipv4_addr . inet_service; elements = {{ {allow_elements} }} }}
  chain prerouting {{
    type filter hook prerouting priority {_NFT_PRIORITY}; policy accept;
    iifname "{self._bridge_if}" ip daddr @blocked_hosts counter log prefix "POOL-EGRESS-BLOCKED-METADATA {self.name} " drop
  }}
  chain forward {{
    type filter hook forward priority {_NFT_PRIORITY}; policy accept;
    iifname "{self._bridge_if}" ip daddr @blocked_hosts counter log prefix "POOL-EGRESS-BLOCKED-METADATA {self.name} " drop
    iifname "{self._bridge_if}" ip daddr . tcp dport @allowed_dests counter accept
    iifname "{self._bridge_if}" counter log prefix "POOL-EGRESS-BLOCKED-DEFAULT {self.name} " drop
  }}
}}
""".strip()

    def __exit__(self, *exc_info: object) -> None:
        subprocess.run(["sudo", "nft", "delete", "table", "inet", self.table], capture_output=True, text=True, timeout=15)
        subprocess.run(["docker", "network", "rm", self.name], capture_output=True, text=True, timeout=20)


def run_pooled(
    *,
    run_id: UUID,
    command: list[str],
    input_files: dict[str, bytes] | None = None,
    env: dict[str, str] | None = None,
    egress: EgressPolicy | None = None,
    image: str = DEFAULT_IMAGE,
    timeout: float = 120,
    memory_limit_mb: int = 2048,
    cpu_limit: float = 2.0,
) -> SandboxResult:
    """Runs `command` inside a fresh, single-tenant gVisor container. `input_files` are copied
    (never bind-mounted from the caller's own working directory) into a per-run staging directory
    that IS the container's whole `/work` — the sandboxed process can see exactly those files and
    nothing else on the host. The staging directory and the container are both destroyed on every
    exit path (normal, timeout, exception)."""
    if not gvisor_available():
        raise SandboxUnavailable("gVisor (runsc) is not registered as a Docker runtime on this machine")
    policy = egress or EgressPolicy()
    with tempfile.TemporaryDirectory(prefix=f"galaius-pool-{run_id.hex[:8]}-") as staging:
        work = Path(staging)
        for relative_path, content in (input_files or {}).items():
            target = (work / relative_path).resolve()
            if not target.is_relative_to(work.resolve()):
                raise ValueError(f"input file path escapes the run's staging directory: {relative_path!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        docker_args = [
            "docker", "run", "--rm", "--platform", "linux/amd64", "--runtime", RUNTIME,
            "--memory", f"{memory_limit_mb}m", "--cpus", str(cpu_limit),
            "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,size=256m",
            "-v", f"{work}:/work:rw", "-w", "/work",
        ]
        for key, value in (env or {}).items():
            docker_args += ["-e", f"{key}={value}"]

        with _egress_context(run_id, policy) as network_name:
            docker_args += ["--network", network_name or "none", image, *command]
            try:
                completed = subprocess.run(docker_args, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(f"pooled run exceeded its {timeout}s timeout") from error
        return SandboxResult(exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


def _egress_context(run_id: UUID, policy: EgressPolicy):
    """`--network none` (no allow-list declared: no network device at all) or a torn-down-after
    per-run bridge with its `nft` allow-list installed — one context manager either way, so
    `run_pooled` never branches on which case it is."""
    if not policy.allow:
        from contextlib import contextmanager

        @contextmanager
        def _none():
            yield None

        return _none()
    return _EgressNetworkContext(run_id, policy)


class _EgressNetworkContext:
    def __init__(self, run_id: UUID, policy: EgressPolicy) -> None:
        self._network = _EgressNetwork(run_id, policy)

    def __enter__(self) -> str:
        self._network.__enter__()
        return self._network.name

    def __exit__(self, *exc_info: object) -> None:
        self._network.__exit__(*exc_info)
