"""Configuration management for PaperBridge Local.

Handles Windows paths containing Japanese characters and spaces safely using pathlib.Path.
Enforces local loopback defaults and security limits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _default_runtime_dir() -> Path:
    env_dir = os.environ.get("PAPERBRIDGE_RUNTIME_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        return (Path(local_app_data) / "PaperBridgeLocal").resolve()
    return (Path.home() / ".paperbridge_local").resolve()


def _find_default_japanese_font(prefer_serif: bool = True) -> Path | None:
    win_fonts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
    candidates = (
        [
            "NotoSerifJP-VF.ttf",
            "NotoSansJP-VF.ttf",
            "BIZ-UDMinchoM.ttc",
            "YuGothR.ttc",
            "msmincho.ttc",
            "msgothic.ttc",
        ]
        if prefer_serif
        else [
            "NotoSansJP-VF.ttf",
            "BIZ-UDGothicR.ttc",
            "YuGothR.ttc",
            "meiryo.ttc",
            "msgothic.ttc",
        ]
    )
    for name in candidates:
        p = win_fonts / name
        if p.exists():
            return p
    return None


def _find_default_math_font() -> Path | None:
    env_font = os.environ.get("PAPERBRIDGE_MATH_FONT_PATH", "").strip()
    if env_font:
        p = Path(env_font).expanduser().resolve()
        if p.exists():
            return p
    win_fonts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
    for name in ("cambria.ttc", "seguisym.ttf"):
        p = win_fonts / name
        if p.exists():
            return p
    return None


@dataclass
class AppConfig:

    host: str = field(
        default_factory=lambda: os.environ.get("PAPERBRIDGE_HOST", "127.0.0.1")
    )
    port: int = field(
        default_factory=lambda: int(os.environ.get("PAPERBRIDGE_PORT", "8080"))
    )
    runtime_dir: Path = field(default_factory=_default_runtime_dir)

    # Translation / llama.cpp settings
    llama_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "PAPERBRIDGE_LLAMA_BASE_URL", "http://127.0.0.1:8000/v1"
        )
    )
    llama_model_alias: str = field(
        default_factory=lambda: os.environ.get(
            "PAPERBRIDGE_LLAMA_MODEL_ALIAS", "plamo-3-translate"
        )
    )
    allow_external_translation_host: bool = field(
        default_factory=lambda: os.environ.get(
            "PAPERBRIDGE_ALLOW_EXTERNAL_TRANSLATION_HOST", "false"
        ).lower()
        == "true"
    )

    # Context & budget defaults (Section 10)
    context_length: int = field(
        default_factory=lambda: int(
            os.environ.get("PAPERBRIDGE_CONTEXT_LENGTH", "8192")
        )
    )
    model_max_length: int = field(
        default_factory=lambda: int(
            os.environ.get("PAPERBRIDGE_MODEL_MAX_LENGTH", "32768")
        )
    )
    concurrency: int = field(
        default_factory=lambda: int(os.environ.get("PAPERBRIDGE_CONCURRENCY", "1"))
    )
    default_form: str = field(
        default_factory=lambda: os.environ.get("PAPERBRIDGE_DEFAULT_FORM", "plain")
    )

    # Typesetting defaults (Section 12)
    min_font_size_pt: float = field(
        default_factory=lambda: float(
            os.environ.get("PAPERBRIDGE_MIN_FONT_SIZE_PT", "8.0")
        )
    )
    font_serif_path: Path | None = field(
        default_factory=lambda: _find_default_japanese_font(prefer_serif=True)
    )
    font_sans_path: Path | None = field(
        default_factory=lambda: _find_default_japanese_font(prefer_serif=False)
    )
    font_math_path: Path | None = field(default_factory=_find_default_math_font)

    # Security & download gates (Section 16 & 17)
    allow_auto_asset_download: bool = field(
        default_factory=lambda: os.environ.get(
            "PAPERBRIDGE_ALLOW_AUTO_ASSET_DOWNLOAD", "false"
        ).lower()
        == "true"
    )
    telemetry_enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "PAPERBRIDGE_TELEMETRY_ENABLED", "false"
        ).lower()
        == "true"
    )
    max_upload_mb: int = field(
        default_factory=lambda: int(os.environ.get("PAPERBRIDGE_MAX_UPLOAD_MB", "100"))
    )
    max_pages: int = field(
        default_factory=lambda: int(os.environ.get("PAPERBRIDGE_MAX_PAGES", "300"))
    )
    job_timeout_sec: int = field(
        default_factory=lambda: int(
            os.environ.get("PAPERBRIDGE_JOB_TIMEOUT_SEC", "3600")
        )
    )
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8080",
            "http://localhost:8080",
        ]
    )

    def __post_init__(self) -> None:
        if os.name == "nt":
            parts = list(self.runtime_dir.parts)
            cleaned_parts = [parts[0]] + [p.rstrip(" .") or "_" for p in parts[1:]]
            self.runtime_dir = Path(*cleaned_parts)

    def ensure_directories(self) -> None:
        for sub in ("uploads", "working", "outputs", "cache", "db"):
            (self.runtime_dir / sub).mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        self.ensure_directories()
        return self.runtime_dir / "db" / "paperbridge.sqlite3"

    def validate_translation_url(self, url: str | None = None) -> bool:
        target = url or self.llama_base_url
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = (parsed.hostname or "").lower()
        if hostname in ("127.0.0.1", "localhost", "::1"):
            return True
        return self.allow_external_translation_host
