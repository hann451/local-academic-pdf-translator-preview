"""Self-update version check and external updater handoff manager.

Design principles:
- Version check gracefully handles offline environments and GitHub API rate limits,
  returning `update_available: false` without raising HTTP 500.
- Update apply rejects execution with `UpdateConflictError` (HTTP 409) whenever
  a translation job or model download is active.
- Spawns `scripts/apply_update.ps1` as a detached independent process, then safely
  terminates verified local `llama-server` and FastAPI processes after flushing the
  HTTP response.
- Never modifies or deletes user data (`%LOCALAPPDATA%\\PaperBridgeLocal`), GGUF models,
  `.env`, or `uat-artifacts/`.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, Field

from backend.config import AppConfig
from backend.diagnostics import _parse_local_endpoint
from backend.models.server_process import LlamaServerProcessManager

DEFAULT_UPDATE_REPO = "hann451/local-academic-pdf-translator-preview"
DEFAULT_UPDATE_BRANCH = "main"

_HEX_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UpdateConflictError(RuntimeError):
    """Raised when an active translation job, download, or ongoing update blocks apply."""


class UpdateLaunchError(RuntimeError):
    """Raised when the external updater script cannot be located or launched."""


class UpdateCheckResponse(BaseModel):
    update_available: bool = False
    current_version: str
    latest_version: str | None = None
    message: str
    status: Literal[
        "up_to_date",
        "update_available",
        "offline_or_error",
        "rate_limited",
    ] = "up_to_date"
    is_git_repo: bool = False
    latest_commit_message: str | None = None
    latest_commit_date: str | None = None
    checked_at: str = Field(default_factory=_utc_now_iso)


class UpdateApplyResponse(BaseModel):
    status: Literal["updating"] = "updating"
    message: str
    updater_pid: int | None = None
    log_path: str | None = None


def _is_hex_sha(value: str | None) -> bool:
    return bool(value and _HEX_SHA_RE.fullmatch(value.strip()))


def _shas_match(local_sha: str | None, remote_sha: str | None) -> bool:
    if not _is_hex_sha(local_sha) or not _is_hex_sha(remote_sha):
        return False
    a = local_sha.strip().lower()  # type: ignore[union-attr]
    b = remote_sha.strip().lower()  # type: ignore[union-attr]
    return a == b or b.startswith(a) or a.startswith(b)


def get_local_version(project_root: Path | None = None) -> tuple[str, str | None, bool]:
    """Return `(short_display_version, full_commit_sha_or_none, is_git_repo)`."""
    root = (
        Path(project_root).resolve()
        if project_root is not None
        else Path(__file__).resolve().parent.parent.parent
    )
    is_git = (root / ".git").exists()

    if is_git:
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            sha = (proc.stdout or "").strip()
            if proc.returncode == 0 and _is_hex_sha(sha):
                return sha[:7], sha, True
        except Exception:
            pass

    version_marker = root / ".paperbridge_version.json"
    if version_marker.is_file():
        try:
            data = json.loads(version_marker.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                full_sha = str(data.get("commit_sha") or "").strip()
                short_sha = str(data.get("short_sha") or "").strip()
                if _is_hex_sha(full_sha):
                    return (short_sha or full_sha[:7]), full_sha, is_git
                if _is_hex_sha(short_sha):
                    return short_sha[:7], short_sha, is_git
        except Exception:
            pass

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            ver = str((parsed.get("project") or {}).get("version") or "").strip()
            if ver:
                return f"v{ver}", None, is_git
        except Exception:
            pass

    return "unknown", None, is_git


def _default_spawn_detached(cmd: list[str], cwd: Path) -> int:
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    return int(proc.pid)


def _default_shutdown_process() -> None:
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        pass
    # Safety fallback on Windows in case uvicorn event loop does not exit promptly
    time.sleep(2.5)
    os._exit(0)


class SystemUpdater:
    """Checks GitHub for updates and orchestrates safe self-update handoff."""

    def __init__(
        self,
        config: AppConfig,
        *,
        project_root: Path | None = None,
        llama_server_manager: LlamaServerProcessManager | None = None,
        repo_slug: str | None = None,
        branch: str | None = None,
        http_client_factory: Callable[[], httpx.Client] | None = None,
        spawn_updater_fn: Callable[[list[str], Path], int] | None = None,
        stop_llama_fn: Callable[[], list[int]] | None = None,
        shutdown_app_fn: Callable[[], None] | None = None,
        shutdown_delay_sec: float = 0.5,
    ) -> None:
        self.config = config
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parent.parent.parent
        )
        self.llama_server_manager = llama_server_manager
        self.repo_slug = (
            repo_slug
            or os.environ.get("PAPERBRIDGE_UPDATE_REPO", "").strip()
            or DEFAULT_UPDATE_REPO
        )
        self.branch = (
            branch
            or os.environ.get("PAPERBRIDGE_UPDATE_BRANCH", "").strip()
            or DEFAULT_UPDATE_BRANCH
        )
        self.http_client_factory = http_client_factory
        self._spawn_updater_fn = spawn_updater_fn or _default_spawn_detached
        self._stop_llama_fn = stop_llama_fn
        self._shutdown_app_fn = shutdown_app_fn or _default_shutdown_process
        self.shutdown_delay_sec = shutdown_delay_sec
        self._apply_lock = threading.Lock()
        self._apply_started = False
        self._last_latest_sha: str | None = None

    @property
    def github_commit_api_url(self) -> str:
        return f"https://api.github.com/repos/{self.repo_slug}/commits/{self.branch}"

    def _new_client(self) -> httpx.Client:
        if self.http_client_factory is not None:
            return self.http_client_factory()
        return httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(8.0, connect=5.0),
        )

    def get_current_version_display(self) -> str:
        short_ver, _, _ = get_local_version(self.project_root)
        return short_ver

    def check_for_updates(self) -> UpdateCheckResponse:
        """Query GitHub API for latest commit on target branch with safe offline/rate-limit handling."""
        current_short, current_full, is_git = get_local_version(self.project_root)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "PaperBridgeLocal-Updater/1.0",
        }
        try:
            with self._new_client() as client:
                resp = client.get(self.github_commit_api_url, headers=headers)
        except (httpx.TimeoutException, httpx.RequestError, OSError):
            return UpdateCheckResponse(
                update_available=False,
                current_version=current_short,
                latest_version=None,
                message="オフラインまたはネットワークエラーのため更新を確認できませんでした。",
                status="offline_or_error",
                is_git_repo=is_git,
            )
        except Exception:
            return UpdateCheckResponse(
                update_available=False,
                current_version=current_short,
                latest_version=None,
                message="更新確認中に予期しないエラーが発生しました。",
                status="offline_or_error",
                is_git_repo=is_git,
            )

        if resp.status_code in (403, 429):
            remaining = (resp.headers.get("x-ratelimit-remaining") or "").strip()
            body_lower = (resp.text or "").lower()
            if resp.status_code == 429 or remaining == "0" or "rate limit" in body_lower:
                return UpdateCheckResponse(
                    update_available=False,
                    current_version=current_short,
                    latest_version=None,
                    message="GitHub API の利用制限（Rate Limit）に達しました。しばらく時間を置いてから再確認してください。",
                    status="rate_limited",
                    is_git_repo=is_git,
                )
            return UpdateCheckResponse(
                update_available=False,
                current_version=current_short,
                latest_version=None,
                message=f"GitHub API へのアクセスが拒否されました（HTTP {resp.status_code}）。",
                status="offline_or_error",
                is_git_repo=is_git,
            )

        if resp.status_code != 200:
            return UpdateCheckResponse(
                update_available=False,
                current_version=current_short,
                latest_version=None,
                message=f"最新バージョン情報を取得できませんでした（HTTP {resp.status_code}）。",
                status="offline_or_error",
                is_git_repo=is_git,
            )

        try:
            payload: Any = resp.json()
        except ValueError:
            return UpdateCheckResponse(
                update_available=False,
                current_version=current_short,
                latest_version=None,
                message="GitHub API の応答形式を解析できませんでした。",
                status="offline_or_error",
                is_git_repo=is_git,
            )

        remote_sha = str(payload.get("sha") or "").strip() if isinstance(payload, dict) else ""
        if not _is_hex_sha(remote_sha):
            return UpdateCheckResponse(
                update_available=False,
                current_version=current_short,
                latest_version=None,
                message="GitHub API の応答に有効なコミット SHA が含まれていませんでした。",
                status="offline_or_error",
                is_git_repo=is_git,
            )

        self._last_latest_sha = remote_sha
        latest_short = remote_sha[:7]
        commit_obj = payload.get("commit") if isinstance(payload, dict) else None
        commit_msg: str | None = None
        commit_date: str | None = None
        if isinstance(commit_obj, dict):
            raw_msg = commit_obj.get("message")
            if isinstance(raw_msg, str) and raw_msg.strip():
                commit_msg = raw_msg.strip().splitlines()[0]
            committer = commit_obj.get("committer") or commit_obj.get("author")
            if isinstance(committer, dict) and isinstance(committer.get("date"), str):
                commit_date = committer["date"]

        if _shas_match(current_full or current_short, remote_sha):
            return UpdateCheckResponse(
                update_available=False,
                current_version=current_short,
                latest_version=latest_short,
                message="現在、最新バージョンを使用しています。",
                status="up_to_date",
                is_git_repo=is_git,
                latest_commit_message=commit_msg,
                latest_commit_date=commit_date,
            )

        return UpdateCheckResponse(
            update_available=True,
            current_version=current_short,
            latest_version=latest_short,
            message="最新のアップデートが利用可能です",
            status="update_available",
            is_git_repo=is_git,
            latest_commit_message=commit_msg,
            latest_commit_date=commit_date,
        )

    def _stop_llama_safely(self) -> list[int]:
        if self._stop_llama_fn is not None:
            return self._stop_llama_fn()
        if self.llama_server_manager is not None:
            return self.llama_server_manager.stop_running_server()
        return []

    def apply_update(
        self,
        *,
        has_active_job: bool | Callable[[], bool] = False,
        has_active_download: bool | Callable[[], bool] = False,
        target_commit_sha: str | None = None,
    ) -> UpdateApplyResponse:
        """Guard active jobs, spawn external updater script, and schedule graceful shutdown."""
        active_job = bool(has_active_job() if callable(has_active_job) else has_active_job)
        if active_job:
            raise UpdateConflictError(
                "UpdateConflict: 翻訳ジョブ実行中はアップデートを実行できません。"
                "進行中のジョブを完了または停止してから再試行してください。"
            )

        active_dl = bool(has_active_download() if callable(has_active_download) else has_active_download)
        if active_dl:
            raise UpdateConflictError(
                "UpdateConflict: モデルのダウンロード進行中はアップデートを実行できません。"
                "ダウンロード完了またはキャンセル後に再試行してください。"
            )

        with self._apply_lock:
            if self._apply_started:
                raise UpdateConflictError(
                    "UpdateConflict: アップデート処理は既に進行中です。"
                )
            self._apply_started = True

        apply_script = self.project_root / "scripts" / "apply_update.ps1"
        if not apply_script.is_file():
            with self._apply_lock:
                self._apply_started = False
            raise UpdateLaunchError(
                f"外部アップデータースクリプトが見つかりません: {apply_script}"
            )

        parsed_llama = _parse_local_endpoint(self.config.llama_base_url)
        llama_port = parsed_llama[1] if parsed_llama is not None else 8000
        backend_port = int(self.config.port)
        current_pid = os.getpid()
        sha_to_pass = (target_commit_sha or self._last_latest_sha or "").strip()

        signal_path = self.config.runtime_dir / ".update_in_progress"
        log_path = self.config.runtime_dir / "logs" / "updater.log"

        try:
            self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
            signal_payload = {
                "started_at": _utc_now_iso(),
                "backend_pid": current_pid,
                "backend_port": backend_port,
                "llama_port": llama_port,
            }
            signal_path.write_text(
                json.dumps(signal_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            cmd = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(apply_script),
                "-ProjectRoot",
                str(self.project_root),
                "-UserDataDir",
                str(self.config.runtime_dir),
                "-BackendPort",
                str(backend_port),
                "-LlamaPort",
                str(llama_port),
                "-BackendPid",
                str(current_pid),
                "-RepoSlug",
                self.repo_slug,
                "-Branch",
                self.branch,
            ]
            if sha_to_pass and _is_hex_sha(sha_to_pass):
                cmd.extend(["-TargetCommitSha", sha_to_pass])

            updater_pid = self._spawn_updater_fn(cmd, self.project_root)
        except Exception as exc:
            try:
                signal_path.unlink(missing_ok=True)
            except OSError:
                pass
            with self._apply_lock:
                self._apply_started = False
            raise UpdateLaunchError(
                f"外部アップデーターの起動に失敗しました: {exc}"
            ) from exc

        def _deferred_shutdown() -> None:
            if self.shutdown_delay_sec > 0:
                time.sleep(self.shutdown_delay_sec)
            try:
                self._stop_llama_safely()
            except Exception:
                pass
            self._shutdown_app_fn()

        threading.Thread(target=_deferred_shutdown, daemon=True).start()

        return UpdateApplyResponse(
            status="updating",
            message="アップデートを開始しました。アプリを終了して最新版の適用と再起動を行います。",
            updater_pid=updater_pid,
            log_path=str(log_path),
        )
