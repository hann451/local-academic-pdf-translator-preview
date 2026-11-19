"""Unit tests for LlamaServerProcessManager safety guards, args, port release wait, fallback, and rollback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.config import AppConfig
from backend.models.catalog import load_model_catalog
from backend.models.registry import ModelRegistryManager
from backend.models.server_process import (
    LlamaServerConflictError,
    LlamaServerLaunchError,
    LlamaServerProcessManager,
    LlamaServerSafetyError,
    ProcessInfo,
    build_llama_server_launch_args,
)
from tests.unit.test_model_registry_and_catalog import build_synthetic_gguf, build_temp_catalog


class _FakePopen:
    def __init__(self, pid: int, exit_after_polls: int | None = None, returncode: int = 0) -> None:
        self.pid = pid
        self._exit_after_polls = exit_after_polls
        self._polls = 0
        self.returncode = returncode

    def poll(self) -> int | None:
        self._polls += 1
        if self._exit_after_polls is not None and self._polls >= self._exit_after_polls:
            return self.returncode
        return None


def _setup_two_models(tmp_path: Path) -> tuple[AppConfig, ModelRegistryManager, Path, Path, str, str]:
    b08 = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-0.8b", extra_padding=b"SRV08B")
    b2 = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"SRV2B")
    cat_path = build_temp_catalog(
        tmp_path,
        [
            {"model_id": "plamo-3-translate-0.8b-q4_k_m", "primary_bytes": b08},
            {"model_id": "plamo-3-translate-2b-q4_k_m", "primary_bytes": b2},
        ],
    )
    rt = tmp_path / "runtime"
    tools_vk = tmp_path / "tools" / "llama.cpp" / "b12167-vulkan-x64"
    tools_cpu = tmp_path / "tools" / "llama.cpp" / "b12167-cpu-x64"
    tools_vk.mkdir(parents=True)
    tools_cpu.mkdir(parents=True)
    vk_exe = tools_vk / "llama-server.exe"
    cpu_exe = tools_cpu / "llama-server.exe"
    vk_exe.write_bytes(b"VKEXE")
    cpu_exe.write_bytes(b"CPUEXE")

    cfg = AppConfig(
        runtime_dir=rt,
        model_catalog_path=cat_path,
        llama_base_url="http://127.0.0.1:8000/v1",
        llama_server_exe=vk_exe,
        llama_cpu_server_exe=cpu_exe,
    )
    cfg.ensure_directories()
    mgr = ModelRegistryManager(runtime_dir=rt, catalog_path=cat_path)

    f08 = tmp_path / "models" / "plamo-3-translate-0.8b-Q4_K_M.gguf"
    f2 = tmp_path / "models" / "plamo-3-translate-2b-Q4_K_M.gguf"
    f08.parent.mkdir(parents=True, exist_ok=True)
    f08.write_bytes(b08)
    f2.write_bytes(b2)

    r2 = mgr.register_local_model(file_path=f2, license_accepted=True, set_active=True)
    r08 = mgr.register_local_model(file_path=f08, license_accepted=True, set_active=False)
    return cfg, mgr, f08, f2, r08.registration_id, r2.registration_id


def test_build_launch_args_matches_launcher_conventions(tmp_path: Path) -> None:
    cat = load_model_catalog()
    m08 = cat.get_model("plamo-3-translate-0.8b-q4_k_m")
    assert m08 is not None
    model_path = tmp_path / "外部 フォルダ" / "plamo-3-translate-0.8b-Q4_K_M.gguf"

    vk_args = build_llama_server_launch_args(model_path=model_path, catalog_model=m08, port=8000)
    assert vk_args == [
        "--model",
        str(model_path),
        "--alias",
        "plamo-3-translate-0.8b-q4_k_m",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "-c",
        "8192",
        "-np",
        "1",
        "-ngl",
        "99",
        "--jinja",
        "--no-prefill-assistant",
        "--cors-origins",
        "localhost",
    ]

    cpu_args = build_llama_server_launch_args(
        model_path=model_path, catalog_model=m08, port=8001, host="localhost", use_cpu=True
    )
    assert cpu_args[cpu_args.index("-ngl") + 1] == "0"
    assert cpu_args[cpu_args.index("--port") + 1] == "8001"


def test_active_job_concurrent_lock_and_external_process_safety(tmp_path: Path) -> None:
    cfg, mgr, f08, _f2, reg08_id, _reg2_id = _setup_two_models(tmp_path)
    pm = LlamaServerProcessManager(config=cfg, registry=mgr)

    # 1. Active translation job raises LlamaServerConflictError
    with pytest.raises(LlamaServerConflictError, match="ModelSwitchConflict"):
        pm.restart_for_active_model(registration_id=reg08_id, has_active_job=True)

    # 2. Concurrent switch lock raises LlamaServerConflictError
    assert pm._switch_lock.acquire(blocking=False)
    try:
        with pytest.raises(LlamaServerConflictError, match="進行中"):
            pm.restart_for_active_model(registration_id=reg08_id, has_active_job=False)
    finally:
        pm._switch_lock.release()

    # 3. External non-llama-server process on port 8000 must NEVER be terminated
    pm._find_listening_pids_on_port = lambda host, port: [43210]  # type: ignore[method-assign]
    pm._inspect_process = lambda pid: ProcessInfo(  # type: ignore[method-assign]
        pid=pid,
        name="python.exe",
        executable_path=r"C:\Python313\python.exe",
        command_line=r"C:\Python313\python.exe -m http.server 8000",
    )
    stop_called: list[int] = []
    pm._stop_llama_process = lambda info, port: stop_called.append(info.pid)  # type: ignore[method-assign]

    with pytest.raises(LlamaServerSafetyError, match="外部プログラム"):
        pm.restart_for_active_model(registration_id=reg08_id, has_active_job=False)
    assert stop_called == []

    # 4. llama-server with conflicting explicit --port in CommandLine must also be protected
    pm._inspect_process = lambda pid: ProcessInfo(  # type: ignore[method-assign]
        pid=pid,
        name="llama-server.exe",
        executable_path=str(cfg.llama_server_exe),
        command_line=f'"{cfg.llama_server_exe}" --model "{f08}" --port 8001',
    )
    with pytest.raises(LlamaServerSafetyError, match="別ポート"):
        pm.restart_for_active_model(registration_id=reg08_id, has_active_job=False)
    assert stop_called == []

    # 5. Non-loopback endpoint rejected cleanly
    remote_cfg = AppConfig(runtime_dir=cfg.runtime_dir, llama_base_url="http://192.168.1.50:8000/v1")
    remote_pm = LlamaServerProcessManager(config=remote_cfg, registry=mgr)
    with pytest.raises(ValueError, match="ループバック以外"):
        remote_pm.restart_for_active_model()


def test_port_release_wait_and_vulkan_to_cpu_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg, mgr, f08, f2, reg08_id, _reg2_id = _setup_two_models(tmp_path)
    pm = LlamaServerProcessManager(
        config=cfg,
        registry=mgr,
        health_timeout_sec=3.0,
        cpu_health_timeout_sec=3.0,
        port_release_timeout_sec=3.0,
    )
    monkeypatch.setattr("backend.models.server_process.time.sleep", lambda _s: None)

    # Simulate old 2B llama-server on port 8000
    old_pid = 11111
    vk_fail_pid = 22222
    cpu_ok_pid = 33333
    alive_pids = {old_pid}

    pm._find_listening_pids_on_port = lambda host, port: [old_pid] if old_pid in alive_pids else []  # type: ignore[method-assign]
    pm._inspect_process = lambda pid: (  # type: ignore[method-assign]
        ProcessInfo(
            pid=pid,
            name="llama-server.exe",
            executable_path=str(cfg.llama_server_exe),
            command_line=f'"{cfg.llama_server_exe}" --model "{f2}" --port 8000 -ngl 99',
        )
        if pid in alive_pids
        else None
    )

    events: list[str] = []

    def fake_stop(info: ProcessInfo, port: int) -> None:
        events.append(f"stop:{info.pid}")
        alive_pids.discard(info.pid)

    bind_checks = {"count": 0}

    def fake_can_bind(host: str, port: int) -> bool:
        bind_checks["count"] += 1
        events.append(f"bind_check:{bind_checks['count']}")
        # Require 3 polls before port is reported free (Address already in use guard)
        return bind_checks["count"] >= 3

    spawned: list[tuple[Path, list[str]]] = []

    def fake_spawn(*, exe_path: Path, args: list[str], out_log: Path, err_log: Path) -> Any:
        spawned.append((exe_path, args))
        if exe_path == cfg.llama_server_exe:
            events.append("spawn_vulkan")
            return _FakePopen(vk_fail_pid, exit_after_polls=1, returncode=1)
        events.append("spawn_cpu")
        alive_pids.add(cpu_ok_pid)
        return _FakePopen(cpu_ok_pid, exit_after_polls=None)

    pm._stop_llama_process = fake_stop  # type: ignore[method-assign]
    pm._can_bind_port = fake_can_bind  # type: ignore[method-assign]
    pm._spawn_llama_process = fake_spawn  # type: ignore[method-assign]
    pm._probe_health_ok = lambda host, port: cpu_ok_pid in alive_pids  # type: ignore[method-assign]

    monkeypatch.setattr(
        "backend.models.server_process.LlamaCppPlamoAdapter.check_health",
        lambda self: (
            {
                "status": "connected",
                "model_name": "plamo-3-translate-0.8b-q4_k_m",
                "model_path_full": str(f08),
                "model_file": f08.name,
            }
            if cpu_ok_pid in alive_pids
            else {"status": "server_unreachable"}
        ),
    )

    res = pm.restart_for_active_model(registration_id=reg08_id)

    assert res.pid == cpu_ok_pid
    assert res.compute_mode == "cpu"
    assert res.stopped_pids == [old_pid]
    assert res.server_match.match_status == "matched"
    assert res.server_match.is_matched is True
    assert events.index("stop:11111") < events.index("bind_check:3") < events.index("spawn_vulkan")
    assert spawned[0][0] == cfg.llama_server_exe
    assert "-ngl" in spawned[0][1] and spawned[0][1][spawned[0][1].index("-ngl") + 1] == "99"
    assert spawned[1][0] == cfg.llama_cpu_server_exe
    assert spawned[1][1][spawned[1][1].index("-ngl") + 1] == "0"

    # Verify launch_verified + state file persisted
    active_after, _ = mgr.get_active_model()
    assert active_after is not None and active_after.verification.launch_verified is True
    state_data = json.loads(pm.state_path.read_text(encoding="utf-8"))
    assert state_data["pid"] == cpu_ok_pid
    assert state_data["compute_mode"] == "cpu"
    assert Path(state_data["model_path"]) == f08


def test_failed_switch_attempts_rollback_to_previous_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg, mgr, f08, f2, reg08_id, _reg2_id = _setup_two_models(tmp_path)
    pm = LlamaServerProcessManager(
        config=cfg,
        registry=mgr,
        health_timeout_sec=2.0,
        cpu_health_timeout_sec=2.0,
        port_release_timeout_sec=2.0,
    )
    monkeypatch.setattr("backend.models.server_process.time.sleep", lambda _s: None)

    old_pid = 41000
    rollback_pid = 42000
    rollback_calls: list[list[str]] = []

    pm._find_listening_pids_on_port = lambda host, port: [old_pid]  # type: ignore[method-assign]
    pm._inspect_process = lambda pid: ProcessInfo(  # type: ignore[method-assign]
        pid=pid,
        name="llama-server.exe",
        executable_path=str(cfg.llama_server_exe),
        command_line=f'"{cfg.llama_server_exe}" --model "{f2}" --alias plamo-3-translate-2b-q4_k_m --port 8000 -ngl 99',
    )
    pm._stop_llama_process = lambda info, port: None  # type: ignore[method-assign]
    pm._can_bind_port = lambda host, port: True  # type: ignore[method-assign]

    def fake_spawn(*, exe_path: Path, args: list[str], out_log: Path, err_log: Path) -> Any:
        chosen_model = args[args.index("--model") + 1]
        if Path(chosen_model) == f2:
            rollback_calls.append(args)
            return _FakePopen(rollback_pid, exit_after_polls=None)
        return _FakePopen(99999, exit_after_polls=1, returncode=1)

    pm._spawn_llama_process = fake_spawn  # type: ignore[method-assign]
    pm._probe_health_ok = lambda host, port: bool(rollback_calls)  # type: ignore[method-assign]

    with pytest.raises(LlamaServerLaunchError, match="自動復旧しました"):
        pm.restart_for_active_model(registration_id=reg08_id)

    assert len(rollback_calls) == 1
    assert Path(rollback_calls[0][rollback_calls[0].index("--model") + 1]) == f2
    state_data = json.loads(pm.state_path.read_text(encoding="utf-8"))
    assert state_data["pid"] == rollback_pid
    assert Path(state_data["model_path"]) == f2


def test_stop_llama_process_terminates_single_pid_without_console_ctrl_break(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import subprocess as sp

    cfg, mgr, _f08, f2, _reg08_id, _reg2_id = _setup_two_models(tmp_path)
    pm = LlamaServerProcessManager(config=cfg, registry=mgr)
    monkeypatch.setattr("backend.models.server_process.time.sleep", lambda _s: None)

    # Confirm module never uses ctypes.windll / GenerateConsoleCtrlEvent
    import backend.models.server_process as sp_mod

    assert not hasattr(sp_mod, "ctypes")

    # 1. Managed process stops via Popen.terminate() without invoking taskkill
    class _ManagedPopen:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.terminated = False

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

    managed_proc = _ManagedPopen(51001)
    pm._managed_procs[51001] = managed_proc  # type: ignore[assignment]
    pm._inspect_process = lambda pid: ProcessInfo(  # type: ignore[method-assign]
        pid=pid,
        name="llama-server.exe",
        executable_path=str(cfg.llama_server_exe),
        command_line=f'"{cfg.llama_server_exe}" --model "{f2}" --port 8000',
    )
    taskkill_calls: list[list[str]] = []
    monkeypatch.setattr(
        "backend.models.server_process.subprocess.run",
        lambda cmd, **kw: taskkill_calls.append(list(cmd)) or sp.CompletedProcess(cmd, 0, "", ""),
    )

    pm._stop_llama_process(
        ProcessInfo(pid=51001, name="llama-server.exe", executable_path=str(cfg.llama_server_exe)),
        8000,
    )
    assert managed_proc.terminated is True
    assert taskkill_calls == []

    # 2. External llama-server PID stops via taskkill.exe /PID <pid> /T /F
    ext_alive = {"alive": True}
    pm._is_pid_running_in_tasklist = lambda pid: ext_alive["alive"]  # type: ignore[method-assign]

    def fake_taskkill(cmd: list[str], **kw: Any) -> sp.CompletedProcess[str]:
        taskkill_calls.append(list(cmd))
        ext_alive["alive"] = False
        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("backend.models.server_process.subprocess.run", fake_taskkill)
    pm._stop_llama_process(
        ProcessInfo(
            pid=51002,
            name="llama-server.exe",
            executable_path=str(cfg.llama_server_exe),
            command_line=f'"{cfg.llama_server_exe}" --model "{f2}" --port 8000',
        ),
        8000,
    )
    assert taskkill_calls == [["taskkill.exe", "/PID", "51002", "/T", "/F"]]

    # 3. Refuses non-llama-server.exe and wrong-port PIDs
    ext_alive["alive"] = True
    with pytest.raises(LlamaServerSafetyError, match="llama-server.exe ではないため"):
        pm._stop_llama_process(ProcessInfo(pid=51003, name="notepad.exe"), 8000)

    with pytest.raises(LlamaServerSafetyError, match="対象ポート（8000）と一致しない"):
        pm._stop_llama_process(
            ProcessInfo(
                pid=51004,
                name="llama-server.exe",
                executable_path=str(cfg.llama_server_exe),
                command_line=f'"{cfg.llama_server_exe}" --model "{f2}" --port 8001',
            ),
            8000,
        )
