"""Verify llama_base_url (:8000 and :8001) matches diagnostics display, health probe targets, and job requests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import AppConfig
from backend.models.registry import load_model_catalog
from tests.unit.test_model_registry_and_catalog import build_synthetic_gguf, build_temp_catalog


@pytest.mark.integration
@pytest.mark.parametrize(
    ("port", "catalog_id", "gguf_name"),
    [
        (8000, "plamo-3-translate-2b-q4_k_m", "plamo-3-translate-2b"),
        (8001, "plamo-3-translate-0.8b-q4_k_m", "plamo-3-translate-0.8b"),
    ],
)
def test_diagnostics_and_real_requests_target_configured_llama_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generated_pdfs: dict[str, Path],
    port: int,
    catalog_id: str,
    gguf_name: str,
) -> None:
    cat = load_model_catalog()
    entry = cat.get_model(catalog_id)
    assert entry is not None

    base_url = f"http://127.0.0.1:{port}/v1"
    rt = tmp_path / f"rt_{port}"
    gguf_bytes = build_synthetic_gguf(
        architecture="plamo3",
        model_name=gguf_name,
        extra_padding=f"ENDPOINT_{port}".encode("utf-8"),
    )
    cat_dir = tmp_path / f"cat_{port}"
    cat_dir.mkdir(parents=True, exist_ok=True)
    cat_path = build_temp_catalog(
        cat_dir,
        [{"model_id": catalog_id, "primary_bytes": gguf_bytes}],
    )
    model_path = tmp_path / f"src_{port}" / entry.filename
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(gguf_bytes)

    recorded_urls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        recorded_urls.append(str(request.url))
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": entry.launch_settings.alias}]},
            )
        if request.url.path == "/props":
            return httpx.Response(
                200,
                json={
                    "model_path": str(model_path),
                    "model_alias": entry.launch_settings.alias,
                    "build_info": "b12167-test",
                    "default_generation_settings": {"n_ctx": 8192},
                },
            )
        if request.url.path == "/v1/completions":
            body = json.loads(request.content.decode("utf-8"))
            assert body["model"] == entry.launch_settings.alias
            return httpx.Response(
                200,
                json={
                    "choices": [{"text": "実験では {v1} を用いた。", "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    orig_client_cls = httpx.Client

    def _patched_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        if "app" not in kwargs and "transport" not in kwargs:
            kwargs["transport"] = transport
        return orig_client_cls(*args, **kwargs)

    monkeypatch.setattr("backend.translation.adapters.llama_cpp_adapter.httpx.Client", _patched_client)

    cfg = AppConfig(
        runtime_dir=rt,
        model_catalog_path=cat_path,
        llama_base_url=base_url,
        llama_model_alias=entry.launch_settings.alias,
    )
    cfg.ensure_directories()
    app = create_app(config=cfg, session_token="test-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "test-tok"}

    reg = client.post(
        "/api/models/register-local",
        headers=headers,
        json={
            "file_path": str(model_path),
            "expected_catalog_model_id": catalog_id,
            "register_mode": "reference",
            "license_accepted": True,
            "set_active": True,
        },
    )
    assert reg.status_code == 200

    diag = client.get("/api/diagnostics?probe_llama=true", headers=headers)
    assert diag.status_code == 200
    diag_data = diag.json()
    assert diag_data["llama_server_url"] == base_url
    assert diag_data["llama_connection_status"] == "connected"
    assert diag_data["loaded_model_name"] == entry.launch_settings.alias

    with open(generated_pdfs["single_col_math"], "rb") as fh:
        doc = client.post(
            "/api/documents/upload",
            headers=headers,
            files={"file": ("single.pdf", fh, "application/pdf")},
        ).json()

    recorded_urls.clear()
    job = client.post(
        f"/api/documents/{doc['id']}/jobs",
        headers=headers,
        json={"model_id": catalog_id, "use_mock_translator": False},
    )
    assert job.status_code == 200
    job_data = job.json()
    assert job_data["execution_provenance"]["endpoint_url"] == base_url
    assert f"http://127.0.0.1:{port}/v1/completions" in recorded_urls
    assert all(u.startswith(f"http://127.0.0.1:{port}/") for u in recorded_urls)
