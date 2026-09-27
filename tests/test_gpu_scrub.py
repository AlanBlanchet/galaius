"""Adversarial proof of safeguard #3 (GPU reset/scrub between tenants, threat #1c): a pattern one
tenant plants in device memory is not readable by the next allocation once `scrub_all_free_memory`
has run. Real CUDA calls against the machine's own GPU via `ctypes`+`libcudart`; skipped where no
CUDA runtime is reachable."""

import subprocess
import sys

import pytest

from interact import gpu_scrub


def _cuda_reachable() -> bool:
    try:
        return gpu_scrub.device_count() > 0
    except gpu_scrub.CudaUnavailable:
        return False


pytestmark = pytest.mark.skipif(not _cuda_reachable(), reason="no CUDA-capable GPU reachable on this machine")

_SIZE = 16 * 1024 * 1024
_PATTERN = 0xCD


def test_gpu_reset_is_unsupported_on_this_consumer_card_so_the_scrub_fallback_applies() -> None:
    """Documents the real, machine-specific fact the scheduler must branch on (`GpuResetKind`):
    a GeForce-class card has no SR-IOV, so `nvidia-smi --gpu-reset` refuses — verified here, not
    assumed from the card's marketing name."""
    assert gpu_scrub.gpu_reset_supported(0) is False


def test_scrub_all_free_memory_leaves_the_next_allocation_provably_clean() -> None:
    """The decisive property: plant a marker in memory, free it (the state a caching allocator or
    a killed tenant process leaves behind), scrub, then allocate fresh and read back — every byte
    must be zero, proving `scrub_all_free_memory` actually overwrote what was there rather than
    relying on the driver having already done it."""
    pointer = gpu_scrub.alloc_and_fill(_SIZE, _PATTERN)
    planted = gpu_scrub.read_device_bytes(pointer, 4096)
    assert planted == bytes([_PATTERN]) * 4096
    gpu_scrub.cuda_free(pointer)

    scrubbed_bytes = gpu_scrub.scrub_all_free_memory(0, chunk_bytes=32 * 1024 * 1024)
    assert scrubbed_bytes >= _SIZE  # covered at least the block tenant A just freed

    import ctypes

    lib = gpu_scrub._cudart()
    lib.cudaSetDevice(0)
    fresh = ctypes.c_void_p()
    lib.cudaMalloc(ctypes.byref(fresh), ctypes.c_size_t(_SIZE))
    read_back = gpu_scrub.read_device_bytes(fresh.value, 4096)
    lib.cudaFree(fresh)

    assert read_back == bytes(4096)  # all zero: no trace of tenant A's 0xCD marker


def test_a_tenant_marker_left_in_vram_after_a_hard_process_kill_does_not_survive_to_the_next_process() -> None:
    """The real cross-tenant shape: tenant A's process is killed WITHOUT a clean `cudaFree` (a
    malicious or crashed script, exactly the actor the threat model assumes) — the next process on
    this machine must never read A's data. A genuine separate process, not a fork (CUDA contexts do
    not survive `fork()`, a documented CUDA constraint, not a workaround for this test). This
    machine's driver (595.91.07) already zeroes VRAM on process-exit reclaim, verified live here —
    a DRIVER-VERSION-DEPENDENT property the scheduler must never rely on alone, which is why
    `scrub_all_free_memory` (previous test) is still run and its `GpuScrubRecord` still checked
    before every tenant handoff, regardless of this result."""
    marker_hex = "cd" * 16
    plant = (
        "from interact import gpu_scrub\n"
        "import os\n"
        f"pointer = gpu_scrub.alloc_and_fill({_SIZE}, {_PATTERN})\n"
        f"assert gpu_scrub.read_device_bytes(pointer, 16).hex() == '{marker_hex}'\n"
        "os._exit(137)\n"  # no cudaFree; simulates SIGKILL of a malicious/crashed tenant
    )
    completed = subprocess.run([sys.executable, "-c", plant])
    assert completed.returncode == 137

    import ctypes

    lib = gpu_scrub._cudart()
    lib.cudaSetDevice(0)
    fresh = ctypes.c_void_p()
    lib.cudaMalloc(ctypes.byref(fresh), ctypes.c_size_t(_SIZE))
    read_back = gpu_scrub.read_device_bytes(fresh.value, 4096)
    lib.cudaFree(fresh)

    assert marker_hex not in read_back.hex()
