"""Unit and API tests for SystemUpdater (version check, offline/rate-limit safety, 409 job guard, and handoff)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import AppConfig
from backend.jobs.manager import JobManager
from backend.updater import (
    SystemUpdater,
    UpdateConflictError,
    UpdateLaunchError,
    get_local_version,
)
from backend.updater.manager import _default_spawn_detached


def _auth_headers(token: str = "test-tok") -> dict[str, str]:
    return {"X-PaperBridge-Token": token}


def test_get_local_version_supports_git_zip_marker_and_pyproject_fallback(tmp_path: Path) -> None:
    # 1. pyproject.toml fallback when neither .git nor .paperbridge_version.json exists
    zip_dir = tmp_path / "zip_install"
    zip_dir.mkdir()
    (zip_dir / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    short_ver, full_sha, is_git = get_local_version(zip_dir)
    assert short_ver == "v0.1.0"
    assert full_sha is None
    assert is_git is False

    # 2. .paperbridge_version.json marker in ZIP install
    sha_hex = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    (zip_dir / ".paperbridge_version.json").write_text(
        json.dumps({"commit_sha": sha_hex, "short_sha": sha_hex[:7]}),
        encoding="utf-8",
    )
    short_ver2, full_sha2, is_git2 = get_local_version(zip_dir)
    assert short_ver2 == "a1b2c3d"
    assert full_sha2 == sha_hex
    assert is_git2 is False

    # 3. Git repository HEAD resolution
    git_dir = tmp_path / "git_install"
    git_dir.mkdir()
    subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=git_dir, check=True, capture_output=True)
    (git_dir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=git_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=git_dir, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    short_git, full_git, is_git3 = get_local_version(git_dir)
    assert short_git == head[:7]
    assert full_git == head
    assert is_git3 is True


def test_check_for_updates_available_up_to_date_offline_and_rate_limit(tmp_path: Path) -> None:
    proj = tmp_path / "app_root"
    proj.mkdir()
    local_sha = "c7f9296000000000000000000000000000000001"
    remote_new_sha = "fe517c2b0f724785bd01fe6b1f9cbc45418ba2cc"
    (proj / ".paperbridge_version.json").write_text(
        json.dumps({"commit_sha": local_sha, "short_sha": local_sha[:7]}),
        encoding="utf-8",
    )
    cfg = AppConfig(runtime_dir=tmp_path / "runtime")

    mode = {"case": "available"}

    def handler(request: httpx.Request) -> httpx.Response:
        if mode["case"] == "available":
            return httpx.Response(
                200,
                json={
                    "sha": remote_new_sha,
                    "commit": {
                        "message": "feat: new updater\n\ndetails",
                        "committer": {"date": "2026-11-22T01:00:00Z"},
                    },
                },
            )
        if mode["case"] == "up_to_date":
            return httpx.Response(
                200,
                json={"sha": local_sha, "commit": {"message": "same commit"}},
            )
        if mode["case"] == "offline":
            raise httpx.ConnectError("Network unreachable", request=request)
        if mode["case"] == "timeout":
            raise httpx.ReadTimeout("Timed out", request=request)
        if mode["case"] == "rate_limit_403":
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0"},
                json={"message": "API rate limit exceeded"},
            )
        if mode["case"] == "rate_limit_429":
            return httpx.Response(429, json={"message": "Too Many Requests"})
        return httpx.Response(502, text="Bad Gateway")

    updater = SystemUpdater(
        config=cfg,
        project_root=proj,
        http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    # 1. Update available
    res_avail = updater.check_for_updates()
    assert res_avail.update_available is True
    assert res_avail.current_version == "c7f9296"
    assert res_avail.latest_version == "fe517c2"
    assert res_avail.status == "update_available"
    assert res_avail.latest_commit_message == "feat: new updater"

    # 2. Up to date
    mode["case"] = "up_to_date"
    res_same = updater.check_for_updates()
    assert res_same.update_available is False
    assert res_same.current_version == "c7f9296"
    assert res_same.latest_version == "c7f9296"
    assert res_same.status == "up_to_date"

    # 3. Offline & Timeout -> safe update_available=False
    for case in ("offline", "timeout", "http_502"):
        mode["case"] = case
        res_err = updater.check_for_updates()
        assert res_err.update_available is False
        assert res_err.current_version == "c7f9296"
        assert res_err.latest_version is None
        assert res_err.status == "offline_or_error"

    # 4. Rate limits (403 & 429) -> safe update_available=False
    for case in ("rate_limit_403", "rate_limit_429"):
        mode["case"] = case
        res_rl = updater.check_for_updates()
        assert res_rl.update_available is False
        assert res_rl.current_version == "c7f9296"
        assert res_rl.latest_version is None
        assert res_rl.status == "rate_limited"


def test_apply_update_spawns_external_script_stops_llama_and_shuts_down(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    apply_ps1 = proj / "scripts" / "apply_update.ps1"
    apply_ps1.write_text("# stub\n", encoding="utf-8")

    cfg = AppConfig(
        runtime_dir=tmp_path / "runtime",
        port=18080,
        llama_base_url="http://127.0.0.1:18000/v1",
    )
    cfg.ensure_directories()

    events: list[str] = []
    captured_cmd: list[str] = []
    done_ev = threading.Event()

    def fake_spawn(cmd: list[str], cwd: Path) -> int:
        captured_cmd.extend(cmd)
        events.append("spawn")
        return 42424

    def fake_stop_llama() -> list[int]:
        events.append("stop_llama")
        return [9999]

    def fake_shutdown() -> None:
        events.append("shutdown")
        done_ev.set()

    updater = SystemUpdater(
        config=cfg,
        project_root=proj,
        spawn_updater_fn=fake_spawn,
        stop_llama_fn=fake_stop_llama,
        shutdown_app_fn=fake_shutdown,
        shutdown_delay_sec=0.02,
    )

    # Active job rejects immediately without spawning
    with pytest.raises(UpdateConflictError, match="UpdateConflict"):
        updater.apply_update(has_active_job=True)
    assert events == []

    # Active download rejects immediately without spawning
    with pytest.raises(UpdateConflictError, match="UpdateConflict"):
        updater.apply_update(has_active_job=False, has_active_download=True)
    assert events == []

    # Normal apply succeeds, spawns script with ports & PID, stops llama, then shuts down app
    res = updater.apply_update(
        has_active_job=False,
        target_commit_sha="fe517c2b0f724785bd01fe6b1f9cbc45418ba2cc",
    )
    assert res.status == "updating"
    assert res.updater_pid == 42424
    assert (cfg.runtime_dir / ".update_in_progress").is_file()
    progress_file = cfg.runtime_dir / "update_progress.json"
    assert progress_file.is_file()
    progress_data = json.loads(progress_file.read_text(encoding="utf-8"))
    assert progress_data["percent"] == 2
    assert progress_data["status"] == "running"
    assert progress_data["stage"] == "launching_updater"
    assert str(apply_ps1) in captured_cmd
    assert "18080" in captured_cmd
    assert "18000" in captured_cmd
    assert "fe517c2b0f724785bd01fe6b1f9cbc45418ba2cc" in captured_cmd

    assert done_ev.wait(timeout=3.0)
    assert events == ["spawn", "stop_llama", "shutdown"]

    # Second call while update already started -> UpdateConflictError
    with pytest.raises(UpdateConflictError):
        updater.apply_update(has_active_job=False)


def test_apply_update_spawn_failure_records_failed_progress_and_clears_marker(tmp_path: Path) -> None:
    proj = tmp_path / "proj_fail"
    (proj / "scripts").mkdir(parents=True)
    (proj / "scripts" / "apply_update.ps1").write_text("# stub\n", encoding="utf-8")
    cfg = AppConfig(runtime_dir=tmp_path / "rt_fail")
    cfg.ensure_directories()

    def broken_spawn(cmd: list[str], cwd: Path) -> int:
        raise OSError("mock spawn failure")

    updater = SystemUpdater(config=cfg, project_root=proj, spawn_updater_fn=broken_spawn)
    with pytest.raises(UpdateLaunchError, match="mock spawn failure"):
        updater.apply_update(has_active_job=False)

    assert not (cfg.runtime_dir / ".update_in_progress").exists()
    progress_data = json.loads((cfg.runtime_dir / "update_progress.json").read_text(encoding="utf-8"))
    assert progress_data["status"] == "failed"
    assert progress_data["stage"] == "launch_failed"
    assert "mock spawn failure" in progress_data["error"]


@pytest.mark.skipif(os.name != "nt", reason="Windows detached PowerShell spawn check")
def test_default_spawn_detached_executes_powershell_script_on_windows(tmp_path: Path) -> None:
    out_file = tmp_path / "spawn_verified.txt"
    ps_script = tmp_path / "verify_spawn.ps1"
    ps_script.write_text(
        'param([string]$OutPath)\n'
        'Start-Sleep -Milliseconds 150\n'
        '[System.IO.File]::WriteAllText($OutPath, "SPAWN_OK", (New-Object System.Text.UTF8Encoding($false)))\n',
        encoding="utf-8-sig",
    )

    pid = _default_spawn_detached(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(ps_script),
            "-OutPath",
            str(out_file),
        ],
        tmp_path,
    )
    assert pid > 0

    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline and not out_file.is_file():
        time.sleep(0.1)

    assert out_file.is_file(), "powershell.exe exited without executing -File under _default_spawn_detached"
    assert out_file.read_text(encoding="utf-8") == "SPAWN_OK"


def test_apply_update_missing_script_raises_launch_error(tmp_path: Path) -> None:
    proj = tmp_path / "empty_proj"
    proj.mkdir()
    cfg = AppConfig(runtime_dir=tmp_path / "rt_missing")
    updater = SystemUpdater(config=cfg, project_root=proj)
    with pytest.raises(UpdateLaunchError, match="apply_update.ps1"):
        updater.apply_update(has_active_job=False)


def test_update_api_endpoints_auth_mock_check_409_conflict_and_apply(tmp_path: Path) -> None:
    proj = tmp_path / "api_proj"
    (proj / "scripts").mkdir(parents=True)
    (proj / "scripts" / "apply_update.ps1").write_text("# stub\n", encoding="utf-8")
    (proj / ".paperbridge_version.json").write_text(
        json.dumps({"commit_sha": "c7f9296000000000000000000000000000000000", "short_sha": "c7f9296"}),
        encoding="utf-8",
    )

    cfg = AppConfig(runtime_dir=tmp_path / "rt_api")
    cfg.ensure_directories()

    shutdown_called = threading.Event()
    spawned_cmds: list[list[str]] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sha": "abc1234000000000000000000000000000000000",
                "commit": {"message": "feat: latest update"},
            },
        )

    updater = SystemUpdater(
        config=cfg,
        project_root=proj,
        http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(transport_handler)),
        spawn_updater_fn=lambda cmd, cwd: (spawned_cmds.append(cmd), 51515)[1],
        stop_llama_fn=lambda: [],
        shutdown_app_fn=lambda: shutdown_called.set(),
        shutdown_delay_sec=0.02,
    )

    app = create_app(config=cfg, session_token="test-tok", system_updater=updater)
    mgr: JobManager = app.state.job_manager
    client = TestClient(app)

    # 1. Auth required
    assert client.get("/api/system/update/check").status_code == 401
    assert client.post("/api/system/update/apply").status_code == 401

    # 2. Check update succeeds
    check_resp = client.get("/api/system/update/check", headers=_auth_headers())
    assert check_resp.status_code == 200
    body = check_resp.json()
    assert body["update_available"] is True
    assert body["current_version"] == "c7f9296"
    assert body["latest_version"] == "abc1234"
    assert "利用可能" in body["message"]

    # 3. Apply rejected with 409 Conflict while a translation job is active
    with mgr._active_lock:
        mgr._active_jobs.add("job-running-update-guard")
    try:
        conflict_resp = client.post("/api/system/update/apply", headers=_auth_headers())
        assert conflict_resp.status_code == 409
        assert "UpdateConflict" in conflict_resp.json()["detail"]
        assert spawned_cmds == []
    finally:
        with mgr._active_lock:
            mgr._active_jobs.discard("job-running-update-guard")

    # 4. Apply succeeds when idle and triggers updater spawn + shutdown hook
    apply_resp = client.post("/api/system/update/apply", headers=_auth_headers())
    assert apply_resp.status_code == 200
    apply_body = apply_resp.json()
    assert apply_body["status"] == "updating"
    assert apply_body["updater_pid"] == 51515
    assert len(spawned_cmds) == 1
    assert shutdown_called.wait(timeout=3.0)
