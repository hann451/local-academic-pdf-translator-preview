"""System diagnostics collector for Windows environment (Section 2 & 13.A).

Collects OS, CPU, RAM, GPU, Python version, PDF Engine status, Font availability,
and llama.cpp connection status (distinguishing model not found vs model load failure).
Dynamically queries Windows kernel32 GlobalMemoryStatusEx and display class registry keys.
"""

from __future__ import annotations

import csv
import ctypes
import io
import os
import platform
import re
import shlex
import subprocess
import sys
from urllib.parse import urlparse

from backend.config import AppConfig
from backend.domain.models import SystemDiagnostics
from backend.pdf.adapters.babeldoc_adapter import (
    are_babeldoc_assets_cached,
    is_babeldoc_package_installed,
)
from backend.translation.adapters.llama_cpp_adapter import LlamaCppPlamoAdapter


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _query_windows_ram_mb() -> int:
    if os.name == "nt":
        try:
            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
                return int(stat.ullTotalPhys // (1024 * 1024))
        except Exception:
            pass
    return 0


def _query_windows_gpu_info() -> tuple[str, int, str]:
    """Query Windows display class registry for primary GPU name, dedicated VRAM (MB), and driver version."""
    if os.name == "nt":
        try:
            import winreg

            class_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
            best_name = "Unknown GPU"
            best_vram_mb = 0
            best_driver = "Unknown"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, class_path) as root_key:
                idx = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(root_key, idx)
                        idx += 1
                    except OSError:
                        break
                    if not sub_name.isdigit():
                        continue
                    try:
                        with winreg.OpenKey(root_key, sub_name) as dev_key:
                            desc, _ = winreg.QueryValueEx(dev_key, "DriverDesc")
                            drv_ver, _ = winreg.QueryValueEx(dev_key, "DriverVersion")
                            vram_bytes = 0
                            try:
                                qw_mem, _ = winreg.QueryValueEx(
                                    dev_key, "HardwareInformation.qwMemorySize"
                                )
                                if isinstance(qw_mem, int):
                                    vram_bytes = qw_mem
                                elif isinstance(qw_mem, bytes) and len(qw_mem) >= 8:
                                    vram_bytes = int.from_bytes(qw_mem[:8], "little")
                            except OSError:
                                pass
                            vram_mb = int(vram_bytes // (1024 * 1024))
                            if vram_mb >= best_vram_mb and desc:
                                best_name = str(desc)
                                best_vram_mb = vram_mb
                                best_driver = str(drv_ver)
                    except OSError:
                        continue
            if best_name != "Unknown GPU":
                return best_name, best_vram_mb, best_driver
        except Exception:
            pass
    return "Unknown GPU", 0, "Unknown"


_SUBPROCESS_TIMEOUT_SEC = 2.0
_SAFE_NGL_VALUE_RE = re.compile(r"^(?:-?\d+|auto|all)$", re.IGNORECASE)
_SAFE_DEVICE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:,-]{1,32}$")


def _parse_local_endpoint(endpoint_url: str | None) -> tuple[str, int] | None:
    """Parse endpoint_url and return (normalized_loopback_host, port) only for valid local loopback endpoints."""
    if not isinstance(endpoint_url, str) or not endpoint_url.strip():
        return None
    try:
        parsed = urlparse(endpoint_url.strip())
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None
        host = (parsed.hostname or "").strip().lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return None
        port = parsed.port
        if port is None:
            port = 80 if scheme == "http" else 443
        if not (1 <= int(port) <= 65535):
            return None
        return host, int(port)
    except Exception:
        return None


def _allowed_listen_addresses_for_host(host: str) -> set[str]:
    ipv4_addrs = {"127.0.0.1", "0.0.0.0", "::ffff:127.0.0.1"}
    ipv6_addrs = {"::1", "::", "0:0:0:0:0:0:0:1", "0:0:0:0:0:0:0:0"}
    if host == "127.0.0.1":
        return ipv4_addrs
    if host == "::1":
        return ipv6_addrs
    if host == "localhost":
        return ipv4_addrs | ipv6_addrs
    return set()


def _split_netstat_endpoint(token: str) -> tuple[str, int] | None:
    raw_addr, sep, port_str = token.strip().rpartition(":")
    if not sep or not raw_addr or not port_str.isdigit():
        return None
    port = int(port_str)
    if not (1 <= port <= 65535):
        return None
    addr = raw_addr.strip()
    if addr.startswith("[") and addr.endswith("]"):
        addr = addr[1:-1]
    if "%" in addr:
        addr = addr.split("%", 1)[0]
    return addr.lower(), port


def _find_unique_listening_pid(host: str, port: int) -> int | None:
    """Find a unique LISTENING PID matching the target loopback host and port.

    Deduplicates multiple lines belonging to the same PID (e.g. dual-stack or wildcard listeners).
    Returns None if zero or multiple distinct PIDs remain.
    """
    allowed_addrs = _allowed_listen_addresses_for_host(host)
    if not allowed_addrs:
        return None
    proc = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
        check=False,
    )
    if proc.returncode != 0:
        return None
    candidate_pids: set[int] = set()
    for raw_line in (proc.stdout or "").splitlines():
        parts = raw_line.split()
        if len(parts) < 5:
            continue
        proto = parts[0].upper()
        if not proto.startswith("TCP"):
            continue
        state = parts[3].upper()
        if state != "LISTENING":
            continue
        parsed_ep = _split_netstat_endpoint(parts[1])
        if parsed_ep is None:
            continue
        listen_addr, listen_port = parsed_ep
        if listen_port != port or listen_addr not in allowed_addrs:
            continue
        pid_str = parts[4]
        if not pid_str.isdigit():
            continue
        pid_val = int(pid_str)
        if pid_val > 0:
            candidate_pids.add(pid_val)
    if len(candidate_pids) != 1:
        return None
    return next(iter(candidate_pids))


def _verify_llama_server_pid(pid: int) -> bool:
    proc = subprocess.run(
        ["tasklist", "/fi", f"PID eq {int(pid)}", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
        check=False,
    )
    if proc.returncode != 0:
        return False
    out = (proc.stdout or "").strip()
    if not out:
        return False
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[1].strip() == str(int(pid)):
            return row[0].strip().lower() == "llama-server.exe"
    return False


def _check_vulkan_dll_loaded(pid: int) -> str:
    """Return 'loaded' only when a valid CSV row for the target PID confirms ggml-vulkan.dll; otherwise 'unverified'."""
    proc = subprocess.run(
        ["tasklist", "/fi", f"PID eq {int(pid)}", "/m", "ggml-vulkan.dll", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
        check=False,
    )
    if proc.returncode != 0:
        return "unverified"
    out = (proc.stdout or "").strip()
    if not out:
        return "unverified"
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 3 and row[1].strip() == str(int(pid)) and row[0].strip().lower() == "llama-server.exe":
            modules_col = row[2].strip().lower()
            if "ggml-vulkan.dll" in modules_col:
                return "loaded"
            return "unverified"
    return "unverified"


def _extract_safe_gpu_startup_args(command_line: str | None) -> list[str]:
    """Extract only validated GPU-layer/device startup flags without retaining full command line or paths."""
    if not isinstance(command_line, str) or not command_line.strip():
        return []
    try:
        raw_tokens = shlex.split(command_line, posix=False)
    except ValueError:
        raw_tokens = command_line.split()
    tokens = [t.strip().strip('"').strip("'") for t in raw_tokens if t.strip()]

    ngl_val: str | None = None
    device_val: str | None = None
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        tok_lower = tok.lower()
        if tok_lower in ("-ngl", "--n-gpu-layers", "--gpu-layers"):
            if idx + 1 < len(tokens):
                candidate = tokens[idx + 1]
                if _SAFE_NGL_VALUE_RE.fullmatch(candidate):
                    ngl_val = candidate.lower()
                else:
                    ngl_val = None
                idx += 2
                continue
        elif tok_lower.startswith(("-ngl=", "--n-gpu-layers=", "--gpu-layers=")):
            candidate = tok.split("=", 1)[1].strip().strip('"').strip("'")
            if _SAFE_NGL_VALUE_RE.fullmatch(candidate):
                ngl_val = candidate.lower()
            else:
                ngl_val = None
        elif tok_lower in ("--device", "-dev"):
            if idx + 1 < len(tokens):
                candidate = tokens[idx + 1]
                if _SAFE_DEVICE_VALUE_RE.fullmatch(candidate):
                    device_val = candidate
                else:
                    device_val = None
                idx += 2
                continue
        elif tok_lower.startswith(("--device=", "-dev=")):
            candidate = tok.split("=", 1)[1].strip().strip('"').strip("'")
            if _SAFE_DEVICE_VALUE_RE.fullmatch(candidate):
                device_val = candidate
            else:
                device_val = None
        idx += 1

    extracted: list[str] = []
    if ngl_val is not None:
        extracted.append(f"n_gpu_layers={ngl_val}")
    if device_val is not None:
        extracted.append(f"device={device_val}")
    return extracted


def _query_process_gpu_startup_args(pid: int) -> list[str]:
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine",
        ],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return _extract_safe_gpu_startup_args(proc.stdout)


def _probe_llama_server_process_and_vulkan(
    endpoint_url: str | None = None,
) -> tuple[int | None, str]:
    """Probe the local llama-server.exe process bound to endpoint_url.

    Separates actual compute backend (always unverified_from_api via HTTP) from
    verified process startup arguments and loaded DLL state. Never infers CPU execution
    solely from missing Vulkan DLL or -ngl 0 startup configuration, and never probes
    local processes for remote endpoints.
    """
    if os.name != "nt":
        return None, "unverified_from_api"
    parsed = _parse_local_endpoint(endpoint_url)
    if parsed is None:
        return None, "unverified_from_api"
    host, port = parsed
    try:
        pid = _find_unique_listening_pid(host, port)
        if pid is None:
            return None, "unverified_from_api"
        if not _verify_llama_server_pid(pid):
            return None, "unverified_from_api"

        try:
            startup_args = _query_process_gpu_startup_args(pid)
        except Exception:
            startup_args = []
        try:
            dll_state = _check_vulkan_dll_loaded(pid)
        except Exception:
            dll_state = "unverified"

        notes: list[str] = []
        if startup_args:
            notes.append(f"startup_arg: {', '.join(startup_args)}")
        if dll_state == "loaded":
            notes.append("dll: ggml-vulkan.dll loaded")

        if not notes:
            return pid, "unverified_from_api"
        return pid, f"unverified_from_api ({'; '.join(notes)})"
    except Exception:
        return None, "unverified_from_api"


def collect_system_diagnostics(
    config: AppConfig | None = None,
    probe_llama: bool = False,
) -> SystemDiagnostics:
    cfg = config or AppConfig()
    font_path = cfg.font_serif_path or cfg.font_sans_path

    babeldoc_ver: str | None = None
    if is_babeldoc_package_installed():
        try:
            import importlib.metadata

            babeldoc_ver = importlib.metadata.version("babeldoc")
        except Exception:
            babeldoc_ver = "installed"

    llama_status = "not_probed"
    loaded_model: str | None = None
    llama_model_file: str | None = None
    llama_build: str | None = None
    llama_backend = "unknown"
    llama_pid: int | None = None
    if probe_llama:
        llama_backend = "unverified_from_api"
        try:
            adapter = LlamaCppPlamoAdapter(config=cfg, timeout_sec=2.0)
            health = adapter.check_health()
            llama_status = str(health.get("status", "server_unreachable"))
            loaded_model = (
                health.get("model_name") if isinstance(health.get("model_name"), str) else None
            )
            llama_model_file = (
                health.get("model_file") if isinstance(health.get("model_file"), str) else None
            )
            llama_build = (
                health.get("build_info") if isinstance(health.get("build_info"), str) else None
            )
            if llama_status == "connected":
                llama_pid, llama_backend = _probe_llama_server_process_and_vulkan(
                    cfg.llama_base_url
                )
        except Exception:
            llama_status = "server_unreachable"
            llama_pid = None
            llama_backend = "unverified_from_api"

    ram_mb = _query_windows_ram_mb()
    gpu_name, gpu_vram_mb, gpu_driver = _query_windows_gpu_info()

    return SystemDiagnostics(
        os_name=f"{platform.system()} {platform.release()}",
        os_version=platform.version(),
        cpu_name=platform.processor() or "Unknown CPU",
        cpu_cores=os.cpu_count() // 2 if os.cpu_count() else 1,
        cpu_threads=os.cpu_count() or 1,
        ram_total_mb=ram_mb,
        gpu_name=gpu_name,
        gpu_vram_mb=gpu_vram_mb,
        gpu_driver_version=gpu_driver,
        python_version=sys.version.split()[0],
        pdf_engine_primary="native_preserving_pymupdf (BabelDOC v0.6.4 adapter ready)",
        babeldoc_installed=is_babeldoc_package_installed(),
        babeldoc_version=babeldoc_ver,
        babeldoc_assets_cached=are_babeldoc_assets_cached(),
        japanese_font_available=bool(font_path and font_path.exists()),
        japanese_font_path=str(font_path) if font_path else None,
        llama_server_url=cfg.llama_base_url,
        llama_connection_status=llama_status,
        loaded_model_name=loaded_model,
        llama_model_path=llama_model_file,
        llama_build_info=llama_build,
        llama_compute_backend=llama_backend,
        llama_server_pid=llama_pid,
        loopback_only_enforced=not cfg.allow_external_translation_host,
    )
