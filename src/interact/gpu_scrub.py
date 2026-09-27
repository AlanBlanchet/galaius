"""GPU memory reset/scrub between pooled tenants (threat-model safeguard #3, threat #1c):
`cudaMalloc` does not zero the memory it hands back, so a tenant B allocation on the same physical
accelerator right after tenant A's can read fragments of A's weights/activations/images still
sitting in device memory. This machine's GPUs are GeForce-class (no SR-IOV) — `nvidia-smi
--gpu-reset` is unsupported, verified 2026-09-25 (`nvidia-smi --help` lists `-r`/`--gpu-reset` as
requiring "SR-IOV virtual functions" support this card does not report). The fallback proven here:
allocate every free byte of device memory, overwrite it, free it — a genuine whole-device
overwrite, not a partial one, since CUDA's allocator can only hand a NEW tenant memory this process
already released.

`ctypes` against `libcudart` directly — no torch/pycuda dependency for a scheduler-side safety
primitive that must work even on a machine that has never provisioned the vision venv."""

import ctypes
import ctypes.util
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_CUDA_SUCCESS = 0


class CudaUnavailable(RuntimeError):
    """No CUDA runtime library found, or no device reachable — a pooled GPU run must refuse
    outright rather than assume the device is clean."""


def _cudart() -> ctypes.CDLL:
    name = ctypes.util.find_library("cudart") or "libcudart.so"
    try:
        return ctypes.CDLL(name)
    except OSError as error:
        raise CudaUnavailable(f"libcudart not loadable: {error}") from error


@dataclass(frozen=True)
class DeviceMemoryInfo:
    free_bytes: int
    total_bytes: int


def device_count() -> int:
    lib = _cudart()
    count = ctypes.c_int(0)
    if lib.cudaGetDeviceCount(ctypes.byref(count)) != _CUDA_SUCCESS:
        raise CudaUnavailable("cudaGetDeviceCount failed")
    return count.value


def memory_info(device: int = 0) -> DeviceMemoryInfo:
    lib = _cudart()
    if lib.cudaSetDevice(device) != _CUDA_SUCCESS:
        raise CudaUnavailable(f"cudaSetDevice({device}) failed")
    free_bytes, total_bytes = ctypes.c_size_t(0), ctypes.c_size_t(0)
    if lib.cudaMemGetInfo(ctypes.byref(free_bytes), ctypes.byref(total_bytes)) != _CUDA_SUCCESS:
        raise CudaUnavailable("cudaMemGetInfo failed")
    return DeviceMemoryInfo(free_bytes=free_bytes.value, total_bytes=total_bytes.value)


def alloc_and_fill(nbytes: int, pattern: int, device: int = 0) -> int:
    """Allocates `nbytes` on `device` and fills every byte with `pattern` (0-255). Returns the raw
    device pointer — the caller is responsible for `cuda_free`. Used by the adversarial test to
    plant a marker as "tenant A", and by `scrub_all_free_memory` to overwrite everything free."""
    lib = _cudart()
    lib.cudaSetDevice(device)
    pointer = ctypes.c_void_p()
    if lib.cudaMalloc(ctypes.byref(pointer), ctypes.c_size_t(nbytes)) != _CUDA_SUCCESS:
        raise CudaUnavailable(f"cudaMalloc({nbytes}) failed")
    if lib.cudaMemset(pointer, ctypes.c_int(pattern), ctypes.c_size_t(nbytes)) != _CUDA_SUCCESS:
        lib.cudaFree(pointer)
        raise CudaUnavailable("cudaMemset failed")
    lib.cudaDeviceSynchronize()
    return pointer.value


def cuda_free(pointer: int, device: int = 0) -> None:
    lib = _cudart()
    lib.cudaSetDevice(device)
    lib.cudaFree(ctypes.c_void_p(pointer))


def read_device_bytes(pointer: int, nbytes: int, device: int = 0) -> bytes:
    """Copies `nbytes` device->host from `pointer` — what "tenant B" would see if it happened to
    receive the same physical range CUDA just handed it."""
    lib = _cudart()
    lib.cudaSetDevice(device)
    buffer = ctypes.create_string_buffer(nbytes)
    cudaMemcpyDeviceToHost = 2
    if lib.cudaMemcpy(buffer, ctypes.c_void_p(pointer), ctypes.c_size_t(nbytes), ctypes.c_int(cudaMemcpyDeviceToHost)) != _CUDA_SUCCESS:
        raise CudaUnavailable("cudaMemcpy (device->host) failed")
    return buffer.raw


def gpu_reset_supported(device: int = 0) -> bool:
    """Whether this GPU supports the driver's own hardware reset (`nvidia-smi --gpu-reset`) —
    typically only datacenter cards with SR-IOV. Consumer/GeForce cards do not; the caller must
    fall back to `scrub_all_free_memory`."""
    import subprocess

    completed = subprocess.run(["nvidia-smi", "--query-gpu=index", "--format=csv,noheader", "--gpu-reset", "-i", str(device)], capture_output=True, text=True, timeout=10)
    return completed.returncode == 0


def scrub_all_free_memory(device: int = 0, chunk_bytes: int = 256 * 1024 * 1024) -> int:
    """Overwrites every currently-free byte of device memory with zero, then releases it all — the
    proof a new tenant gets a clean device even without a hardware reset. Allocates in
    `chunk_bytes` chunks (CUDA typically cannot satisfy one single allocation covering literally
    all free memory: allocator overhead, fragmentation) until allocation fails, memsets each chunk,
    then frees every chunk. Returns the total bytes scrubbed."""
    lib = _cudart()
    lib.cudaSetDevice(device)
    pointers: list[ctypes.c_void_p] = []
    scrubbed = 0
    try:
        while True:
            pointer = ctypes.c_void_p()
            if lib.cudaMalloc(ctypes.byref(pointer), ctypes.c_size_t(chunk_bytes)) != _CUDA_SUCCESS:
                break
            if lib.cudaMemset(pointer, 0, ctypes.c_size_t(chunk_bytes)) != _CUDA_SUCCESS:
                lib.cudaFree(pointer)
                break
            pointers.append(pointer)
            scrubbed += chunk_bytes
        lib.cudaDeviceSynchronize()
    finally:
        for pointer in pointers:
            lib.cudaFree(pointer)
    logger.info("scrubbed %d bytes of device %d free memory across %d chunks", scrubbed, device, len(pointers))
    return scrubbed
