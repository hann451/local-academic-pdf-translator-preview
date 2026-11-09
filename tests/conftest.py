"""Pytest shared fixtures and marker separation for PaperBridge Local test suite (Section 8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import AppConfig
from scripts.generate_test_pdfs import generate_all_test_fixtures


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-real-llm",
        action="store_true",
        default=False,
        help="Run real llama.cpp + PLaMo 3 Translate and Vulkan GPU tests (skipped by default in offline CI).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_real = bool(config.getoption("--run-real-llm"))
    markexpr = str(getattr(config.option, "markexpr", "") or "")
    explicitly_requested = run_real or ("real_llm" in markexpr) or ("gpu" in markexpr)

    if explicitly_requested:
        return

    skip_real_marker = pytest.mark.skip(
        reason=(
            "実機LLM/GPUテスト (real_llm / gpu) は通常CIではスキップされます。"
            "実行するには --run-real-llm または -m real_llm を指定してください（モデルの自動DLは行いません）。"
        )
    )
    for item in items:
        if "real_llm" in item.keywords or "gpu" in item.keywords:
            item.add_marker(skip_real_marker)


@pytest.fixture(scope="session")
def generated_pdfs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    fixture_dir = tmp_path_factory.mktemp("pdf_fixtures")
    return generate_all_test_fixtures(fixture_dir)


@pytest.fixture()
def isolated_config(tmp_path: Path) -> AppConfig:
    runtime = tmp_path / " paperbridge 実行データ (テスト) "
    cfg = AppConfig(runtime_dir=runtime)
    cfg.ensure_directories()
    return cfg
