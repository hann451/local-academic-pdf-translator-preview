"""Safe local llama-server lifecycle manager for on-demand Web UI model switching."""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, Field

from backend.config import AppConfig
from backend.diagnostics import (
    _allowed_listen_addresses_for_host,
    _parse_local_endpoint,
    _split_netstat_endpoint,
    _verify_llama_server_pid,
)
from backend.models.catalog import CatalogModel
from backend.models.registry import (
    ModelRegistryManager,
    ModelServerMatchResult,
    ModelValidationError,
    RegisteredModel,
)
from backend.translation.adapters.llama_cpp_adapter import LlamaCppPlamoAdapter


class LlamaServerConflictError(RuntimeError):
    """Raised when an active translation job or concurrent restart blocks server switching."""


class LlamaServerSafetyError(RuntimeError):
    """Raised when refusing to terminate an unrelated or unverified process."""


class LlamaServerLaunchError(RuntimeError):
    """Raised when llama-server fails to stop old port cleanly, start, or become healthy."""


class LlamaServerRestartResult(BaseModel):
    status: Literal["restarted"] = "restarted"
    pid: int
    model_path: str
    model_alias: str
    registration_id: str
    catalog_model_id: str
    display_name: str
    endpoint_url: str
    compute_mode: Literal["vulkan", "cpu"]
    executable_path: str
    connection_status: str
    server_match: ModelServerMatchResult
    stopped_pids: list[int] = Field(default_factory=list)
    message_ja: str


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    executable_path: str | None = None
    command_line: str | None = None


@dataclass(frozen=True)
class PreviousServerSpec:
    model_path: Path
    catalog_model: CatalogModel | None
    alias: str
    use_cpu: bool
    exe_path: Path | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _probe_host_for_endpoint(host: str) -> str:
    if host == "::1":
        return "[::1]"
    return host


def _bind_host_for_endpoint(host: str) -> str:
    if host == "localhost":
        return "127.0.0.1"
    return host


def _parse_flag_value(tokens: list[str], flag_names: tuple[str, ...]) -> str | None:
    lower_names = {f.lower() for f in flag_names}
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        tok_lower = tok.lower()
        if tok_lower in lower_names:
            if idx + 1 < len(tokens):
                return tokens[idx + 1]
            return None
        for prefix in lower_names:
            if tok_lower.startswith(f"{prefix}="):
                return tok.split("=", 1)[1].strip().strip('"').strip("'")
        idx += 1
    return None


def _tokenize_command_line(command_line: str | None) -> list[str]:
    if not isinstance(command_line, str) or not command_line.strip():
        return []
    try:
        raw_tokens = shlex.split(command_line, posix=False)
    except ValueError:
        raw_tokens = command_line.split()
    return [t.strip().strip('"').strip("'") for t in raw_tokens if t.strip()]


def _extract_port_from_command_line(command_line: str | None) -> int | None:
    tokens = _tokenize_command_line(command_line)
    raw_port = _parse_flag_value(tokens, ("--port", "-p"))
    if raw_port and raw_port.isdigit():
        return int(raw_port)
    return None


def _extract_model_path_from_command_line(command_line: str | None) -> str | None:
    tokens = _tokenize_command_line(command_line)
    raw_model = _parse_flag_value(tokens, ("--model", "-m"))
    return raw_model or None


def _extract_alias_from_command_line(command_line: str | None) -> str | None:
    tokens = _tokenize_command_line(command_line)
    raw_alias = _parse_flag_value(tokens, ("--alias", "-a"))
    return raw_alias or None


def _command_line_is_cpu(command_line: str | None, exe_path: str | None) -> bool:
    if exe_path and "cpu" in Path(exe_path).parent.name.lower():
        return True
    tokens = _tokenize_command_line(command_line)
    ngl = _parse_flag_value(tokens, ("-ngl", "--n-gpu-layers", "--gpu-layers"))
    return ngl == "0"


def _is_llama_server_process(info: ProcessInfo) -> bool:
    name_ok = info.name.strip().lower() == "llama-server.exe"
    exe_ok = bool(info.executable_path) and Path(info.executable_path).name.lower() == "llama-server.exe"
    return name_ok or exe_ok


def build_llama_server_launch_args(
    *,
    model_path: Path,
    catalog_model: CatalogModel | None,
    port: int,
    host: str = "127.0.0.1",
    use_cpu: bool = False,
    alias_override: str | None = None,
) -> list[str]:
    """Build llama-server CLI flags consistent with scripts/model_registry_common.ps1."""
    alias = alias_override or "plamo-3-translate"
    context_len = 8192
    parallel_slots = 1
    gpu_layers = 99
    extra_args = ["--jinja", "--no-prefill-assistant"]

    if catalog_model is not None:
        ls = catalog_model.launch_settings
        alias = alias_override or ls.alias or catalog_model.model_id or alias
        if ls.context_length > 0:
            context_len = int(ls.context_length)
        if ls.parallel_slots > 0:
            parallel_slots = int(ls.parallel_slots)
        gpu_layers = int(ls.gpu_layers_auto)
        if ls.extra_args is not None:
            extra_args = [str(x).strip() for x in ls.extra_args if str(x).strip()]

    ngl_val = "0" if use_cpu else str(gpu_layers)
    args = [
        "--model",
        str(model_path),
        "--alias",
        alias,
        "--host",
        _bind_host_for_endpoint(host),
        "--port",
        str(port),
        "-c",
        str(context_len),
        "-np",
        str(parallel_slots),
        "-ngl",
        ngl_val,
    ]
    args.extend(extra_args)
    args.extend(["--cors-origins", "localhost"])
    return args


class LlamaServerProcessManager:
    """Safely stops owned/verified local llama-server processes and restarts with active model."""

    def __init__(
        self,
        config: AppConfig,
        registry: ModelRegistryManager,
        *,
        project_root: Path | None = None,
        health_timeout_sec: float = 45.0,
        cpu_health_timeout_sec: float = 60.0,
        port_release_timeout_sec: float = 10.0,
    ) -> None:
        self.config = config
        self.registry = registry
        self.project_root = (
            Path(project_root)
            if project_root is not None
            else Path(__file__).resolve().parent.parent.parent
        )
        self.health_timeout_sec = health_timeout_sec
        self.cpu_health_timeout_sec = cpu_health_timeout_sec
        self.port_release_timeout_sec = port_release_timeout_sec
        self.state_path = self.config.runtime_dir / "llama_server_state.json"
        self._switch_lock = threading.Lock()
        self._managed_procs: dict[int, subprocess.Popen[Any]] = {}

    def _read_state_file(self) -> dict[str, Any] | None:
        if not self.state_path.is_file():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _write_state_file(self, payload: dict[str, Any]) -> None:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _prune_exited_procs(self) -> None:
        dead = [pid for pid, proc in self._managed_procs.items() if proc.poll() is not None]
        for pid in dead:
            self._managed_procs.pop(pid, None)

    def _find_listening_pids_on_port(self, host: str, port: int) -> list[int]:
        allowed_addrs = _allowed_listen_addresses_for_host(host)
        if not allowed_addrs:
            return []
        proc = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode != 0:
            return []
        pids: set[int] = set()
        for raw_line in (proc.stdout or "").splitlines():
            parts = raw_line.split()
            if len(parts) < 5:
                continue
            if not parts[0].upper().startswith("TCP"):
                continue
            if parts[3].upper() != "LISTENING":
                continue
            parsed_ep = _split_netstat_endpoint(parts[1])
            if parsed_ep is None:
                continue
            listen_addr, listen_port = parsed_ep
            if listen_port != port or listen_addr not in allowed_addrs:
                continue
            if parts[4].isdigit():
                pid_val = int(parts[4])
                if pid_val > 0:
                    pids.add(pid_val)
        return sorted(pids)

    def _inspect_process(self, pid: int) -> ProcessInfo | None:
        if os.name != "nt":
            return None
        if not self._is_pid_running_in_tasklist(pid):
            return None
        name = "llama-server.exe" if _verify_llama_server_pid(pid) else ""
        exe_path: str | None = None
        cmd_line: str | None = None
        try:
            ps_cmd = (
                f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' "
                "-ErrorAction SilentlyContinue; "
                "if ($p) { $p | Select-Object Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress }"
            )
            res = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5.0,
                check=False,
            )
            out = (res.stdout or "").strip()
            if res.returncode == 0 and out:
                parsed = json.loads(out)
                if isinstance(parsed, dict):
                    if isinstance(parsed.get("Name"), str) and parsed["Name"].strip():
                        name = parsed["Name"].strip()
                    if isinstance(parsed.get("ExecutablePath"), str) and parsed["ExecutablePath"].strip():
                        exe_path = parsed["ExecutablePath"].strip()
                    if isinstance(parsed.get("CommandLine"), str) and parsed["CommandLine"].strip():
                        cmd_line = parsed["CommandLine"].strip()
        except Exception:
            pass
        return ProcessInfo(
            pid=int(pid),
            name=name or "unknown",
            executable_path=exe_path,
            command_line=cmd_line,
        )

    def _is_pid_running_in_tasklist(self, pid: int) -> bool:
        proc = subprocess.run(
            ["tasklist", "/fi", f"PID eq {int(pid)}", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if proc.returncode != 0:
            return False
        for line in (proc.stdout or "").splitlines():
            if f'"{int(pid)}"' in line:
                return True
        return False

    def _is_pid_alive(self, pid: int) -> bool:
        managed = self._managed_procs.get(pid)
        if managed is not None:
            return managed.poll() is None
        return self._is_pid_running_in_tasklist(pid)

    def _wait_pid_exit(self, pid: int, timeout_sec: float) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if not self._is_pid_alive(pid):
                self._managed_procs.pop(pid, None)
                return True
            time.sleep(0.15)
        return not self._is_pid_alive(pid)

    def _assert_safe_llama_target(self, info: ProcessInfo, port: int, *, phase: str) -> None:
        if not _is_llama_server_process(info):
            raise LlamaServerSafetyError(
                f"安全性保護 ({phase}): ポート {port} で稼働中のプロセス（{info.name}, PID={info.pid}）は "
                "llama-server.exe ではないため自動終了できません。"
            )
        cmd_port = _extract_port_from_command_line(info.command_line)
        if cmd_port is not None and cmd_port != port:
            raise LlamaServerSafetyError(
                f"安全性保護 ({phase}): PID={info.pid} の起動引数ポート（{cmd_port}）が対象ポート（{port}）と一致しないため終了を中止しました。"
            )

    def _stop_llama_process(self, info: ProcessInfo, port: int) -> None:
        self._assert_safe_llama_target(info, port, phase="事前照合")

        if not self._is_pid_alive(info.pid):
            self._managed_procs.pop(info.pid, None)
            return

        # Never use GenerateConsoleCtrlEvent: it broadcasts CTRL_BREAK to all processes
        # sharing the console session on Windows and can terminate uvicorn or siblings.
        managed = self._managed_procs.get(info.pid)
        if managed is not None and managed.poll() is None:
            recheck = self._inspect_process(info.pid)
            if recheck is not None:
                self._assert_safe_llama_target(recheck, port, phase="再照合")
                managed.terminate()
                if self._wait_pid_exit(info.pid, 3.0):
                    return

        recheck = self._inspect_process(info.pid)
        if recheck is None:
            self._managed_procs.pop(info.pid, None)
            return
        self._assert_safe_llama_target(recheck, port, phase="再照合")

        subprocess.run(
            ["taskkill.exe", "/PID", str(info.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if not self._wait_pid_exit(info.pid, 5.0):
            raise LlamaServerLaunchError(
                f"既存の llama-server (PID={info.pid}) が制限時間内に終了しませんでした。"
            )

    def _can_bind_port(self, host: str, port: int) -> bool:
        if self._find_listening_pids_on_port(host, port):
            return False
        family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        bind_host = "::1" if host == "::1" else "127.0.0.1"
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((bind_host, port))
            return True
        except OSError:
            return False
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _wait_for_port_release(self, host: str, port: int, timeout_sec: float | None = None) -> None:
        wait_sec = timeout_sec if timeout_sec is not None else self.port_release_timeout_sec
        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            if self._can_bind_port(host, port):
                return
            time.sleep(0.15)
        raise LlamaServerLaunchError(
            f"ポート {port} の解放待機が {wait_sec:.1f} 秒以内に完了しませんでした"
            "（Address already in use を防止するため起動を中止しました）。"
        )

    def _resolve_server_executables(
        self,
        *,
        preferred_exe: Path | None = None,
    ) -> tuple[Path | None, Path | None]:
        state = self._read_state_file() or {}
        state_exe_raw = state.get("executable_path")
        state_exe = Path(state_exe_raw) if isinstance(state_exe_raw, str) and state_exe_raw.strip() else None

        parent_root = self.project_root.parent
        sibling_roots: list[Path] = [
            parent_root / "translate",
            parent_root / "PaperBridgeLocal",
        ]

        vulkan_candidates: list[Path | None] = [
            self.config.llama_server_exe,
            preferred_exe if preferred_exe and "cpu" not in preferred_exe.parent.name.lower() else None,
            preferred_exe.parent.parent / "b12167-vulkan-x64" / "llama-server.exe" if preferred_exe else None,
            preferred_exe.parent.parent / "llama.cpp-vulkan" / "llama-server.exe" if preferred_exe else None,
            state_exe if state_exe and "cpu" not in state_exe.parent.name.lower() else None,
            self.project_root / "tools" / "llama.cpp" / "b12167-vulkan-x64" / "llama-server.exe",
            self.project_root / "tools" / "llama.cpp-vulkan" / "llama-server.exe",
            self.config.runtime_dir / "tools" / "llama.cpp" / "b12167-vulkan-x64" / "llama-server.exe",
            self.config.runtime_dir / "tools" / "llama.cpp-vulkan" / "llama-server.exe",
        ]
        cpu_candidates: list[Path | None] = [
            self.config.llama_cpu_server_exe,
            preferred_exe if preferred_exe and "cpu" in preferred_exe.parent.name.lower() else None,
            preferred_exe.parent.parent / "b12167-cpu-x64" / "llama-server.exe" if preferred_exe else None,
            preferred_exe.parent.parent / "llama.cpp-cpu" / "llama-server.exe" if preferred_exe else None,
            state_exe if state_exe and "cpu" in state_exe.parent.name.lower() else None,
            self.project_root / "tools" / "llama.cpp" / "b12167-cpu-x64" / "llama-server.exe",
            self.project_root / "tools" / "llama.cpp-cpu" / "llama-server.exe",
            self.config.runtime_dir / "tools" / "llama.cpp" / "b12167-cpu-x64" / "llama-server.exe",
            self.config.runtime_dir / "tools" / "llama.cpp-cpu" / "llama-server.exe",
        ]

        for root in sibling_roots:
            vulkan_candidates.extend(
                [
                    root / "tools" / "llama.cpp" / "b12167-vulkan-x64" / "llama-server.exe",
                    root / "tools" / "llama.cpp-vulkan" / "llama-server.exe",
                ]
            )
            cpu_candidates.extend(
                [
                    root / "tools" / "llama.cpp" / "b12167-cpu-x64" / "llama-server.exe",
                    root / "tools" / "llama.cpp-cpu" / "llama-server.exe",
                ]
            )

        vulkan_exe = next((p.resolve() for p in vulkan_candidates if p is not None and p.is_file()), None)
        cpu_exe = next((p.resolve() for p in cpu_candidates if p is not None and p.is_file()), None)
        return vulkan_exe, cpu_exe

    def _spawn_llama_process(
        self,
        *,
        exe_path: Path,
        args: list[str],
        out_log: Path,
        err_log: Path,
    ) -> subprocess.Popen[Any]:
        out_log.parent.mkdir(parents=True, exist_ok=True)
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        with out_log.open("wb") as out_f, err_log.open("wb") as err_f:
            proc = subprocess.Popen(
                [str(exe_path), *args],
                cwd=str(exe_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                creationflags=creationflags,
            )
        self._managed_procs[int(proc.pid)] = proc
        return proc

    def _read_log_tail(self, log_path: Path, max_lines: int = 12) -> str:
        if not log_path.is_file():
            return ""
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-max_lines:])
        except Exception:
            return ""

    def _probe_health_ok(self, host: str, port: int) -> bool:
        health_url = f"http://{_probe_host_for_endpoint(host)}:{port}/health"
        try:
            with httpx.Client(timeout=1.5) as client:
                resp = client.get(health_url)
                if resp.status_code != 200:
                    return False
                try:
                    data = resp.json()
                    if isinstance(data, dict) and "status" in data:
                        return str(data["status"]).lower() == "ok"
                except ValueError:
                    pass
                return True
        except Exception:
            return False

    def _check_loaded_model_matches(
        self,
        *,
        expected_alias: str,
        active_reg: RegisteredModel,
    ) -> tuple[bool, dict[str, Any] | None, ModelServerMatchResult | None]:
        adapter = LlamaCppPlamoAdapter(
            config=self.config,
            model_alias=expected_alias,
            timeout_sec=2.0,
        )
        health = adapter.check_health()
        if not isinstance(health, dict) or health.get("status") != "connected":
            return False, health if isinstance(health, dict) else None, None

        match_res = self.registry.compare_with_running_server(health)
        model_name = str(health.get("model_name") or "")
        if match_res.match_status == "mismatch":
            return False, health, match_res
        if match_res.is_matched and (
            match_res.match_status in ("matched", "matched_same_model_different_path")
            or model_name == expected_alias
            or match_res.running_identified_model_id == active_reg.catalog_model_id
        ):
            return True, health, match_res
        return False, health, match_res

    def _launch_and_wait_ready(
        self,
        *,
        exe_path: Path,
        model_path: Path,
        catalog_model: CatalogModel | None,
        alias_override: str | None = None,
        host: str,
        port: int,
        use_cpu: bool,
        target_reg: RegisteredModel,
        timeout_sec: float,
    ) -> tuple[int, str, ModelServerMatchResult]:
        logs_dir = self.config.runtime_dir / "logs"
        out_log = logs_dir / "llama-server.out.log"
        err_log = logs_dir / "llama-server.err.log"
        args = build_llama_server_launch_args(
            model_path=model_path,
            catalog_model=catalog_model,
            port=port,
            host=host,
            use_cpu=use_cpu,
            alias_override=alias_override,
        )
        expected_alias = (
            alias_override
            or (catalog_model.launch_settings.alias if catalog_model else None)
            or (catalog_model.model_id if catalog_model else None)
            or "plamo-3-translate"
        )
        proc = self._spawn_llama_process(
            exe_path=exe_path,
            args=args,
            out_log=out_log,
            err_log=err_log,
        )
        pid = int(proc.pid)
        deadline = time.monotonic() + timeout_sec
        last_health: dict[str, Any] | None = None
        last_match: ModelServerMatchResult | None = None

        while time.monotonic() < deadline:
            exit_code = proc.poll()
            if exit_code is not None:
                self._managed_procs.pop(pid, None)
                err_tail = self._read_log_tail(err_log) or self._read_log_tail(out_log)
                raise LlamaServerLaunchError(
                    f"llama-server が起動直後に終了しました (exit={exit_code})"
                    + (f":\n{err_tail}" if err_tail else "。")
                )

            if self._probe_health_ok(host, port):
                ok, health, match_res = self._check_loaded_model_matches(
                    expected_alias=expected_alias,
                    active_reg=target_reg,
                )
                last_health = health
                last_match = match_res
                if ok and match_res is not None:
                    return pid, expected_alias, match_res

            time.sleep(0.3)

        # Timeout: terminate hung server cleanly so it doesn't hold port/memory
        try:
            self._stop_llama_process(
                ProcessInfo(
                    pid=pid,
                    name=exe_path.name,
                    executable_path=str(exe_path),
                ),
                port,
            )
            self._wait_for_port_release(host, port, timeout_sec=5.0)
        except Exception:
            pass

        err_tail = self._read_log_tail(err_log) or self._read_log_tail(out_log)
        detail_parts = [f"llama-server の起動またはモデル一致確認が {timeout_sec:.0f} 秒以内に完了しませんでした。"]
        if last_health:
            detail_parts.append(f"health={last_health.get('status')} model={last_health.get('model_name')}")
        if last_match:
            detail_parts.append(f"match={last_match.match_status}")
        if err_tail:
            detail_parts.append(f"ログ末尾:\n{err_tail}")
        raise LlamaServerLaunchError(" ".join(detail_parts))

    def _capture_previous_server(
        self,
        *,
        host: str,
        port: int,
        proc_infos: list[ProcessInfo],
    ) -> PreviousServerSpec | None:
        exe_path: Path | None = None
        cmd_line: str | None = None
        for info in proc_infos:
            if info.executable_path and exe_path is None:
                exe_path = Path(info.executable_path)
            if info.command_line and cmd_line is None:
                cmd_line = info.command_line

        prev_model_str = _extract_model_path_from_command_line(cmd_line)
        prev_alias = _extract_alias_from_command_line(cmd_line) or ""
        if not prev_model_str:
            try:
                health = LlamaCppPlamoAdapter(config=self.config, timeout_sec=1.5).check_health()
                if isinstance(health, dict):
                    mp = health.get("model_path_full")
                    if isinstance(mp, str) and mp.strip():
                        prev_model_str = mp.strip()
                    if not prev_alias and isinstance(health.get("model_name"), str):
                        prev_alias = str(health["model_name"]).strip()
            except Exception:
                pass

        state = self._read_state_file() or {}
        if not prev_model_str and state.get("port") == port and isinstance(state.get("model_path"), str):
            prev_model_str = str(state["model_path"])
        if not prev_alias and state.get("port") == port and isinstance(state.get("model_alias"), str):
            prev_alias = str(state["model_alias"])
        if exe_path is None and state.get("port") == port and isinstance(state.get("executable_path"), str):
            exe_path = Path(str(state["executable_path"]))

        if not prev_model_str:
            return None
        prev_path = Path(prev_model_str)
        if not prev_path.is_file():
            return None

        catalog, reg = self.registry.get_registry_snapshot(auto_migrate=False, revalidate=False)
        norm_prev = os.path.normcase(os.path.realpath(str(prev_path)))
        prev_reg = next(
            (m for m in reg.models if os.path.normcase(os.path.realpath(m.file_path)) == norm_prev),
            None,
        )
        prev_cat = catalog.get_model(prev_reg.catalog_model_id) if prev_reg else None
        if prev_cat is None and prev_alias:
            prev_cat = next(
                (c for c in catalog.models if c.launch_settings.alias == prev_alias or c.model_id == prev_alias),
                None,
            )
        use_cpu = _command_line_is_cpu(cmd_line, str(exe_path) if exe_path else None)
        return PreviousServerSpec(
            model_path=prev_path,
            catalog_model=prev_cat,
            alias=prev_alias or (prev_cat.launch_settings.alias if prev_cat else "plamo-3-translate"),
            use_cpu=use_cpu,
            exe_path=exe_path,
        )

    def _attempt_rollback(
        self,
        *,
        previous: PreviousServerSpec,
        host: str,
        port: int,
        vulkan_exe: Path | None,
        cpu_exe: Path | None,
    ) -> str:
        rollback_exe = previous.exe_path
        if rollback_exe is None or not rollback_exe.is_file():
            rollback_exe = cpu_exe if previous.use_cpu else (vulkan_exe or cpu_exe)
        if rollback_exe is None or not rollback_exe.is_file():
            return "元の推論サーバーを復元する実行ファイルが見つかりませんでした。"

        use_cpu = previous.use_cpu or (vulkan_exe is None and rollback_exe == cpu_exe)
        args = build_llama_server_launch_args(
            model_path=previous.model_path,
            catalog_model=previous.catalog_model,
            port=port,
            host=host,
            use_cpu=use_cpu,
            alias_override=previous.alias,
        )
        logs_dir = self.config.runtime_dir / "logs"
        proc = self._spawn_llama_process(
            exe_path=rollback_exe,
            args=args,
            out_log=logs_dir / "llama-server.out.log",
            err_log=logs_dir / "llama-server.err.log",
        )
        deadline = time.monotonic() + (self.cpu_health_timeout_sec if use_cpu else self.health_timeout_sec)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self._managed_procs.pop(int(proc.pid), None)
                return f"以前の構成（{previous.model_path.name}）への自動復旧起動も失敗しました。"
            if self._probe_health_ok(host, port):
                self._write_state_file(
                    {
                        "pid": int(proc.pid),
                        "port": port,
                        "executable_path": str(rollback_exe),
                        "model_path": str(previous.model_path),
                        "model_alias": previous.alias,
                        "registration_id": None,
                        "compute_mode": "cpu" if use_cpu else "vulkan",
                        "updated_at": _utc_now_iso(),
                    }
                )
                return f"以前稼働していたモデル（{previous.model_path.name}, PID={proc.pid}）へ自動復旧しました。"
            time.sleep(0.3)
        return f"以前の構成（{previous.model_path.name}）への自動復旧がタイムアウトしました。"

    def restart_for_active_model(
        self,
        *,
        registration_id: str | None = None,
        use_cpu: bool | None = None,
        has_active_job: bool | Callable[[], bool] = False,
    ) -> LlamaServerRestartResult:
        """Stop running llama-server cleanly and launch it with the active registered model."""
        if not self._switch_lock.acquire(blocking=False):
            raise LlamaServerConflictError(
                "ModelSwitchConflict: 推論サーバーの再起動・モデル切替処理が既に進行中です。"
            )
        try:
            self._prune_exited_procs()
            active_job_now = bool(has_active_job() if callable(has_active_job) else has_active_job)
            if active_job_now:
                raise LlamaServerConflictError(
                    "ModelSwitchConflict: 翻訳ジョブ実行中は推論サーバーの再起動・モデル切替を行えません。"
                )

            parsed = _parse_local_endpoint(self.config.llama_base_url)
            if parsed is None:
                raise ValueError(
                    f"ループバック以外の推論サーバーURLは自動切替できません: {self.config.llama_base_url}"
                )
            host, port = parsed

            if registration_id:
                active_reg = self.registry.select_active_model(
                    registration_id,
                    has_active_job=active_job_now,
                )
                catalog = self.registry.get_catalog()
                active_cat = catalog.get_model(active_reg.catalog_model_id)
            else:
                active_reg, active_cat = self.registry.get_active_model()

            if active_reg is None:
                raise ModelValidationError(
                    "使用する登録済みモデルが選択されていません。画面4またはホームでモデルを選択してください。"
                )
            if not active_reg.is_usable:
                raise ModelValidationError(
                    active_reg.verification.validation_error
                    or f"選択中のモデル「{active_reg.display_name}」は無効またはファイル欠落状態です。"
                )
            if active_cat is None:
                raise ModelValidationError(
                    f"カタログ外モデル '{active_reg.catalog_model_id}' では推論サーバーを起動できません。"
                )
            target_model_path = Path(active_reg.file_path)
            if not target_model_path.is_file():
                raise ModelValidationError(
                    f"選択中モデルの GGUF ファイルが見つかりません: {target_model_path}"
                )

            # Collect verified target processes listening on port (never touch non-llama processes)
            listening_pids = self._find_listening_pids_on_port(host, port)
            proc_infos: list[ProcessInfo] = []
            for pid in listening_pids:
                info = self._inspect_process(pid)
                if info is None:
                    continue
                if not _is_llama_server_process(info):
                    raise LlamaServerSafetyError(
                        f"安全性保護: ポート {port} は外部プログラム（{info.name}, PID={pid}）が使用しているため終了できません。"
                    )
                cmd_port = _extract_port_from_command_line(info.command_line)
                if cmd_port is not None and cmd_port != port:
                    raise LlamaServerSafetyError(
                        f"安全性保護: PID={pid} は別ポート（{cmd_port}）のサーバーであるため終了しません。"
                    )
                proc_infos.append(info)

            # Include tracked state PID on the same port only if it is genuinely our llama-server
            state = self._read_state_file()
            if state and state.get("port") == port and isinstance(state.get("pid"), int):
                state_pid = int(state["pid"])
                if state_pid not in {p.pid for p in proc_infos}:
                    s_info = self._inspect_process(state_pid)
                    if s_info is not None and _is_llama_server_process(s_info):
                        s_port = _extract_port_from_command_line(s_info.command_line)
                        if s_port is None or s_port == port:
                            proc_infos.append(s_info)

            preferred_exe = next(
                (Path(p.executable_path) for p in proc_infos if p.executable_path),
                None,
            )
            vulkan_exe, cpu_exe = self._resolve_server_executables(preferred_exe=preferred_exe)
            if vulkan_exe is None and cpu_exe is None:
                raise LlamaServerLaunchError(
                    "llama-server.exe が見つかりませんでした。tools/llama.cpp 配下を確認するか、"
                    "PAPERBRIDGE_LLAMA_SERVER_EXE 環境変数で実行ファイルパスを指定してください。"
                )

            previous_server = self._capture_previous_server(
                host=host,
                port=port,
                proc_infos=proc_infos,
            )
            inferred_cpu = any(_command_line_is_cpu(p.command_line, p.executable_path) for p in proc_infos)
            prefer_cpu = bool(use_cpu) if use_cpu is not None else inferred_cpu

            stopped_pids: list[int] = []
            for info in proc_infos:
                self._stop_llama_process(info, port)
                stopped_pids.append(info.pid)

            if stopped_pids or not self._can_bind_port(host, port):
                self._wait_for_port_release(host, port)

            # Re-check active job right before spawning in case a job started externally
            active_job_recheck = bool(has_active_job() if callable(has_active_job) else has_active_job)
            if active_job_recheck:
                raise LlamaServerConflictError(
                    "ModelSwitchConflict: 翻訳ジョブ実行中のため推論サーバー起動を中止しました。"
                )

            errors: list[str] = []
            launched_pid: int | None = None
            launched_alias = ""
            launched_match: ModelServerMatchResult | None = None
            launched_mode: Literal["vulkan", "cpu"] = "vulkan"
            launched_exe: Path | None = None

            if not prefer_cpu and vulkan_exe is not None:
                try:
                    launched_pid, launched_alias, launched_match = self._launch_and_wait_ready(
                        exe_path=vulkan_exe,
                        model_path=target_model_path,
                        catalog_model=active_cat,
                        host=host,
                        port=port,
                        use_cpu=False,
                        target_reg=active_reg,
                        timeout_sec=self.health_timeout_sec,
                    )
                    launched_mode = "vulkan"
                    launched_exe = vulkan_exe
                except LlamaServerLaunchError as exc:
                    errors.append(f"Vulkan起動失敗: {exc}")
                    try:
                        self._wait_for_port_release(host, port, timeout_sec=5.0)
                    except LlamaServerLaunchError:
                        pass

            if launched_pid is None and cpu_exe is not None:
                try:
                    launched_pid, launched_alias, launched_match = self._launch_and_wait_ready(
                        exe_path=cpu_exe,
                        model_path=target_model_path,
                        catalog_model=active_cat,
                        host=host,
                        port=port,
                        use_cpu=True,
                        target_reg=active_reg,
                        timeout_sec=self.cpu_health_timeout_sec,
                    )
                    launched_mode = "cpu"
                    launched_exe = cpu_exe
                except LlamaServerLaunchError as exc:
                    errors.append(f"CPUフォールバック起動失敗: {exc}")

            if launched_pid is None or launched_match is None or launched_exe is None:
                rollback_note = ""
                if previous_server is not None:
                    rollback_note = " / " + self._attempt_rollback(
                        previous=previous_server,
                        host=host,
                        port=port,
                        vulkan_exe=vulkan_exe,
                        cpu_exe=cpu_exe,
                    )
                raise LlamaServerLaunchError(
                    "選択モデルでの llama-server 起動に失敗しました: "
                    + " | ".join(errors or ["利用可能な起動バイナリがありません。"])
                    + rollback_note
                )

            self.registry.mark_runtime_verified(
                active_reg.registration_id,
                launch_verified=True,
                translation_verified=False,
            )
            self._write_state_file(
                {
                    "pid": launched_pid,
                    "port": port,
                    "executable_path": str(launched_exe),
                    "model_path": str(target_model_path),
                    "model_alias": launched_alias,
                    "registration_id": active_reg.registration_id,
                    "compute_mode": launched_mode,
                    "updated_at": _utc_now_iso(),
                }
            )
            mode_label = "Vulkan GPU (-ngl 99)" if launched_mode == "vulkan" else "CPU (-ngl 0)"
            return LlamaServerRestartResult(
                status="restarted",
                pid=launched_pid,
                model_path=str(target_model_path),
                model_alias=launched_alias,
                registration_id=active_reg.registration_id,
                catalog_model_id=active_reg.catalog_model_id,
                display_name=active_reg.display_name,
                endpoint_url=self.config.llama_base_url,
                compute_mode=launched_mode,
                executable_path=str(launched_exe),
                connection_status="connected",
                server_match=launched_match,
                stopped_pids=stopped_pids,
                message_ja=(
                    f"推論サーバーを選択モデル「{active_reg.display_name}」で再起動しました"
                    f"（PID={launched_pid}, {mode_label}）。"
                ),
            )
        finally:
            self._switch_lock.release()
