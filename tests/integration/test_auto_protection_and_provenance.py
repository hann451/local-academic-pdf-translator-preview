"""初期非翻訳領域の自動判定・実行経路(provenance)の回帰テスト（合成PDFのみ使用、実論文は不使用）。"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import AppConfig
from backend.domain.models import BlockStatus, JobState, TranslationJobConfig

pytestmark = pytest.mark.integration

BODY = (
    "In this paper we study the convergence of gradient methods on noisy objectives and "
    "show that the proposed scheme converges under mild assumptions on the step size."
)


def _make_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((72, 80), "A Synthetic Study of Gradient Convergence", fontsize=20)
    p1.insert_text((72, 120), "Alice Example, Bob Sample", fontsize=11)
    p1.insert_text((72, 140), "Dept. of Testing, University of Nowhere, Tokyo, Japan", fontsize=9)
    p1.insert_text((72, 154), "{alice,bob}@example.ac.jp", fontsize=9)
    p1.insert_text((72, 200), "Abstract", fontsize=12)
    p1.insert_textbox(pymupdf.Rect(72, 210, 523, 300), BODY, fontsize=10)
    p1.insert_text((72, 330), "1 Introduction", fontsize=12)
    p1.insert_textbox(pymupdf.Rect(72, 340, 523, 430), BODY, fontsize=10)
    p1.insert_text((72, 780), "doi:10.1000/xyz123 (c) 2026 Example Press", fontsize=8)
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((72, 40), "Synthetic Running Header", fontsize=8)
    p2.insert_text((72, 100), "References", fontsize=12)
    p2.insert_text((72, 120), "[1] A. Smith, Optimization Methods, Journal of ML, 2024.", fontsize=9)
    p2.insert_text((72, 134), "[2] B. Jones, Stochastic Descent, Proc. of X, 2023.", fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def api(isolated_config: AppConfig, tmp_path: Path):
    client = TestClient(create_app(config=isolated_config))
    token = client.get("/api/auth/session", headers={"Origin": "http://127.0.0.1:5173"}).json()[
        "token"
    ]
    headers = {"X-PaperBridge-Token": token, "Origin": "http://127.0.0.1:5173"}
    pdf = _make_pdf(tmp_path / "synthetic.pdf")
    with open(pdf, "rb") as f:
        doc = client.post(
            "/api/documents/upload",
            files={"file": ("synthetic.pdf", f, "application/pdf")},
            headers=headers,
        ).json()
    return client, headers, doc["id"]


def _blocks(client: TestClient, headers: dict, doc_id: str) -> list[dict]:
    return client.get(f"/api/documents/{doc_id}/blocks", headers=headers).json()


def test_auto_protection_categories_and_default_translation(api) -> None:
    client, headers, doc_id = api
    client.post(f"/api/documents/{doc_id}/parse", json={}, headers=headers)
    blocks = _blocks(client, headers, doc_id)
    by_cat: dict[str, list[dict]] = {}
    for b in blocks:
        by_cat.setdefault(b["semantic_category"], []).append(b)

    for cat in ("title", "reference_heading", "reference"):
        assert cat in by_cat, f"{cat} not detected: {sorted(by_cat)}"
        assert all(b["status"] == BlockStatus.PROTECTED_SKIP.value for b in by_cat[cat])
        assert all(b["protection_reason"].startswith("自動：") for b in by_cat[cat])
    # 参考文献は複数ページ目でも保護される
    assert any(b["page_index"] == 1 for b in by_cat["reference"])
    # 本文・節見出しは翻訳対象のまま
    for b in by_cat.get("body", []):
        assert b["status"] != BlockStatus.PROTECTED_SKIP.value
    # メールアドレスは数式扱いにならない
    assert all(
        b["semantic_category"] != "formula" for b in blocks if "@example.ac.jp" in b["source_text"]
    )


def test_user_override_persists_across_reparse_and_reapply(api) -> None:
    client, headers, doc_id = api
    client.post(f"/api/documents/{doc_id}/parse", json={}, headers=headers)
    title = next(b for b in _blocks(client, headers, doc_id) if b["semantic_category"] == "title")

    r = client.put(
        f"/api/documents/{doc_id}/blocks/{title['id']}/protection",
        json={"user_protection_override": "force_translate"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["status"] != BlockStatus.PROTECTED_SKIP.value

    bad = client.put(
        f"/api/documents/{doc_id}/blocks/{title['id']}/protection",
        json={"user_protection_override": "bogus"},
        headers=headers,
    )
    assert bad.status_code == 422

    # 再解析しても利用者の明示オーバーライドは維持される
    client.post(f"/api/documents/{doc_id}/parse", json={}, headers=headers)
    again = next(b for b in _blocks(client, headers, doc_id) if b["id"] == title["id"])
    assert again["user_protection_override"] == "force_translate"
    assert again["status"] != BlockStatus.PROTECTED_SKIP.value

    # 「自動判定に戻す」
    client.put(
        f"/api/documents/{doc_id}/blocks/{title['id']}/protection",
        json={"user_protection_override": None},
        headers=headers,
    )
    back = next(b for b in _blocks(client, headers, doc_id) if b["id"] == title["id"])
    assert back["status"] == BlockStatus.PROTECTED_SKIP.value

    # 再適用APIは存在し、404を正しく返す
    ok = client.post(
        f"/api/documents/{doc_id}/reapply-auto-protection",
        json={"reset_user_overrides": True},
        headers=headers,
    )
    assert ok.status_code == 200
    missing = client.post(
        "/api/documents/doc-000000000000/reapply-auto-protection", json={}, headers=headers
    )
    assert missing.status_code == 404


def test_execution_provenance_accounting(api) -> None:
    client, headers, doc_id = api
    cfg = TranslationJobConfig(use_mock_translator=True).model_dump()
    job = client.post(f"/api/documents/{doc_id}/jobs", json=cfg, headers=headers).json()
    assert job["state"] in (JobState.COMPLETED.value, JobState.COMPLETED_WITH_WARNINGS.value)
    prov = job["execution_provenance"]
    assert prov["is_mock"] is True
    assert prov["total_blocks"] == (
        prov["newly_inferenced_blocks"]
        + prov["cache_hit_blocks"]
        + prov["reused_existing_blocks"]
        + prov["manual_translation_blocks"]
        + prov["protected_skip_blocks"]
        + prov["fallback_original_blocks"]
    )
    assert prov["protected_skip_blocks"] >= 3  # タイトル・参考文献見出し・参考文献

    # 2回目は翻訳済みブロック再利用またはキャッシュが使われ、新規推論は発生しない
    job2 = client.post(f"/api/documents/{doc_id}/jobs", json=cfg, headers=headers).json()
    p2 = job2["execution_provenance"]
    assert p2["cache_hit_blocks"] + p2["reused_existing_blocks"] >= 1
    assert p2["newly_inferenced_blocks"] == 0


def _make_subprocess_mock(
    *,
    netstat_stdout: str = "",
    netstat_rc: int = 0,
    pid_process_map: dict[int, str] | None = None,
    pid_vulkan_map: dict[int, bool | None] | None = None,
    pid_cmdline_map: dict[int, str | None] | None = None,
    raise_timeout_on: str | None = None,
):
    import subprocess as _sp

    proc_map = pid_process_map or {}
    vk_map = pid_vulkan_map or {}
    cmd_map = pid_cmdline_map or {}
    calls: list[list[str]] = []

    def _fake_run(args, *a, **kw):
        cmd_list = [str(x) for x in args]
        calls.append(cmd_list)
        assert kw.get("timeout") is not None and kw["timeout"] <= 5.0
        exe = cmd_list[0].lower()
        if raise_timeout_on and raise_timeout_on in " ".join(cmd_list).lower():
            raise _sp.TimeoutExpired(cmd=cmd_list, timeout=kw["timeout"])

        if exe == "netstat":
            return _sp.CompletedProcess(cmd_list, netstat_rc, stdout=netstat_stdout, stderr="")

        if exe == "tasklist":
            joined = " ".join(cmd_list)
            pid = None
            for part in cmd_list:
                if part.startswith("PID eq "):
                    pid = int(part.split("PID eq ", 1)[1])
            if pid is None or pid not in proc_map:
                return _sp.CompletedProcess(
                    cmd_list,
                    0,
                    stdout="INFO: No tasks are running which match the specified criteria.\n",
                    stderr="",
                )
            img = proc_map[pid]
            if "/m" in [x.lower() for x in cmd_list] and "ggml-vulkan.dll" in joined.lower():
                vk_state = vk_map.get(pid, False)
                if vk_state is None:
                    return _sp.CompletedProcess(cmd_list, 1, stdout="", stderr="Access is denied.")
                if vk_state:
                    return _sp.CompletedProcess(
                        cmd_list,
                        0,
                        stdout=f'"{img}","{pid}","ggml-vulkan.dll"\n',
                        stderr="",
                    )
                return _sp.CompletedProcess(
                    cmd_list,
                    0,
                    stdout="INFO: No tasks are running which match the specified criteria.\n",
                    stderr="",
                )
            return _sp.CompletedProcess(
                cmd_list,
                0,
                stdout=f'"{img}","{pid}","Console","1","120,000 K"\n',
                stderr="",
            )

        if exe == "powershell":
            cmd_str = cmd_list[-1]
            pid = None
            if "ProcessId =" in cmd_str:
                pid_part = cmd_str.split("ProcessId =", 1)[1].split("'")[0].strip()
                if pid_part.isdigit():
                    pid = int(pid_part)
            cmdline = cmd_map.get(pid) if pid is not None else None
            if cmdline is None:
                return _sp.CompletedProcess(cmd_list, 1, stdout="", stderr="AccessDenied")
            return _sp.CompletedProcess(cmd_list, 0, stdout=cmdline + "\n", stderr="")

        return _sp.CompletedProcess(cmd_list, 1, stdout="", stderr="unexpected command")

    return _fake_run, calls


def test_probe_endpoint_matching_coexisting_cpu_gpu_and_no_cpu_or_gpu_overclaim(
    monkeypatch: pytest.MonkeyPatch,
    isolated_config: AppConfig,
) -> None:
    """Coexisting 8000 (ngl 99) and 18082 (ngl 0 + Vulkan DLL loaded) servers must resolve by endpoint."""
    import backend.diagnostics as diag_mod

    monkeypatch.setattr(diag_mod.os, "name", "nt")
    netstat_out = (
        "Active Connections\n"
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       38816\n"
        "  TCP    127.0.0.1:18082        0.0.0.0:0              LISTENING       39696\n"
    )
    fake_run, calls = _make_subprocess_mock(
        netstat_stdout=netstat_out,
        pid_process_map={38816: "llama-server.exe", 39696: "llama-server.exe"},
        pid_vulkan_map={38816: True, 39696: True},
        pid_cmdline_map={
            38816: r'"C:\tools\llama-server.exe" -m "C:\Users\secret_user\model.gguf" --port 8000 -ngl 99',
            39696: r'"C:\tools\llama-server.exe" -m "C:\Users\secret_user\model.gguf" --api-key SECRET123 --port 18082 -ngl 0 -t 4',
        },
    )
    monkeypatch.setattr(diag_mod.subprocess, "run", fake_run)

    # 1. Endpoint 18082 resolves to PID 39696 (not 38816), records n_gpu_layers=0 startup arg & Vulkan DLL loaded,
    #    and never claims "CPU" or "Vulkan GPU" as verified compute result or leaks full command line.
    pid_18082, status_18082 = diag_mod._probe_llama_server_process_and_vulkan(
        "http://127.0.0.1:18082/v1"
    )
    assert pid_18082 == 39696
    assert status_18082 == "unverified_from_api (startup_arg: n_gpu_layers=0; dll: ggml-vulkan.dll loaded)"
    assert "CPU" not in status_18082
    assert "Vulkan GPU" not in status_18082
    assert "secret_user" not in status_18082
    assert "SECRET123" not in status_18082

    # 2. Single target server on 8000 resolves to PID 38816 and keeps PC GPU separate from compute_backend.
    pid_8000, status_8000 = diag_mod._probe_llama_server_process_and_vulkan(
        "http://127.0.0.1:8000/v1"
    )
    assert pid_8000 == 38816
    assert status_8000 == "unverified_from_api (startup_arg: n_gpu_layers=99; dll: ggml-vulkan.dll loaded)"

    monkeypatch.setattr(
        diag_mod, "_query_windows_gpu_info", lambda: ("Synthetic Workstation GPU", 8192, "1.0.0")
    )
    monkeypatch.setattr(
        diag_mod.LlamaCppPlamoAdapter,
        "check_health",
        lambda self: {
            "status": "connected",
            "model_name": "plamo-3-translate",
            "model_file": "plamo.gguf",
            "build_info": "b12167",
        },
    )
    cfg_18082 = AppConfig(runtime_dir=isolated_config.runtime_dir, llama_base_url="http://127.0.0.1:18082/v1")
    sys_diag = diag_mod.collect_system_diagnostics(config=cfg_18082, probe_llama=True)
    assert sys_diag.gpu_name == "Synthetic Workstation GPU"
    assert sys_diag.llama_server_pid == 39696
    assert "Synthetic Workstation GPU" not in sys_diag.llama_compute_backend
    assert sys_diag.llama_compute_backend == status_18082


def test_probe_missing_vulkan_dll_does_not_claim_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Info messages, N/A, empty output, query failures, or missing target PID row must be 'unverified' without claiming not_loaded or CPU, while retaining PID and safe startup args."""
    import subprocess as _sp
    import backend.diagnostics as diag_mod

    monkeypatch.setattr(diag_mod.os, "name", "nt")
    netstat_out = "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       41000\n"

    # Case A: No startup GPU flag + INFO message -> (41000, "unverified_from_api")
    fake_run, calls = _make_subprocess_mock(
        netstat_stdout=netstat_out,
        pid_process_map={41000: "llama-server.exe"},
        pid_vulkan_map={41000: False},
        pid_cmdline_map={41000: r'"C:\tools\llama-server.exe" --port 8000'},
    )
    monkeypatch.setattr(diag_mod.subprocess, "run", fake_run)

    pid, status = diag_mod._probe_llama_server_process_and_vulkan("http://127.0.0.1:8000/v1")
    assert pid == 41000
    assert status == "unverified_from_api"
    assert "not_loaded" not in status
    assert "not loaded" not in status
    assert "CPU" not in status
    assert len(calls) == 4  # No extra process queries added

    # Case B: Various non-loaded outputs (English INFO, Japanese 情報, N/A row, wrong PID row, empty output, rc=1)
    #         must all return "unverified" in _check_vulkan_dll_loaded and preserve PID + startup_arg in full probe.
    non_loaded_responses = [
        (0, "INFO: No tasks are running which match the specified criteria.\n"),
        (0, "情報: 指定された基準に一致するタスクは実行されていません。\n"),
        (0, '"llama-server.exe","41000","N/A"\n'),
        (0, '"llama-server.exe","99999","ggml-vulkan.dll"\n'),
        (0, "\n"),
        (1, ""),
    ]
    for rc, module_stdout in non_loaded_responses:
        probe_calls: list[list[str]] = []

        def _custom_run(args, *a, _rc=rc, _out=module_stdout, **kw):
            cmd_list = [str(x) for x in args]
            probe_calls.append(cmd_list)
            exe = cmd_list[0].lower()
            if exe == "netstat":
                return _sp.CompletedProcess(cmd_list, 0, stdout=netstat_out, stderr="")
            if exe == "tasklist":
                if "/m" in [x.lower() for x in cmd_list]:
                    return _sp.CompletedProcess(cmd_list, _rc, stdout=_out, stderr="")
                return _sp.CompletedProcess(
                    cmd_list, 0, stdout='"llama-server.exe","41000","Console","1","100 K"\n', stderr=""
                )
            if exe == "powershell":
                return _sp.CompletedProcess(
                    cmd_list, 0, stdout="llama-server.exe --port 8000 -ngl 0\n", stderr=""
                )
            return _sp.CompletedProcess(cmd_list, 1, stdout="", stderr="")

        monkeypatch.setattr(diag_mod.subprocess, "run", _custom_run)
        assert diag_mod._check_vulkan_dll_loaded(41000) == "unverified"
        probe_calls.clear()
        p_id, p_status = diag_mod._probe_llama_server_process_and_vulkan("http://127.0.0.1:8000/v1")
        assert p_id == 41000
        assert p_status == "unverified_from_api (startup_arg: n_gpu_layers=0)"
        assert "not loaded" not in p_status
        assert "CPU" not in p_status
        assert len(probe_calls) == 4


def test_probe_ipv6_wildcard_dedup_ambiguous_missing_and_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify IPv6, wildcard listeners, same-PID deduplication, multi-PID ambiguity, missing listener, and remote host."""
    import backend.diagnostics as diag_mod

    monkeypatch.setattr(diag_mod.os, "name", "nt")

    # Same PID (50010) listening on both IPv4 wildcard and IPv6 loopback for port 18082;
    # Different PIDs (60001, 60002) listening on port 19000 (ambiguous for localhost);
    # IPv6-only listener (70001) on [::1]:19001.
    netstat_out = (
        "  TCP    0.0.0.0:18082          0.0.0.0:0              LISTENING       50010\n"
        "  TCP    [::1]:18082            [::]:0                 LISTENING       50010\n"
        "  TCP    127.0.0.1:19000        0.0.0.0:0              LISTENING       60001\n"
        "  TCP    [::1]:19000            [::]:0                 LISTENING       60002\n"
        "  TCP    [::1]:19001            [::]:0                 LISTENING       70001\n"
    )
    fake_run, calls = _make_subprocess_mock(
        netstat_stdout=netstat_out,
        pid_process_map={
            50010: "llama-server.exe",
            60001: "llama-server.exe",
            60002: "llama-server.exe",
            70001: "llama-server.exe",
        },
        pid_vulkan_map={50010: True, 60001: True, 60002: True, 70001: False},
        pid_cmdline_map={
            50010: "llama-server.exe --port 18082 -ngl 0",
            60001: "llama-server.exe --port 19000 -ngl 99",
            60002: "llama-server.exe --port 19000 -ngl 0",
            70001: "llama-server.exe --host ::1 --port 19001",
        },
    )
    monkeypatch.setattr(diag_mod.subprocess, "run", fake_run)

    # Same PID across IPv4 wildcard + IPv6 loopback deduplicates to 50010 for localhost, 127.0.0.1, and [::1]
    assert diag_mod._probe_llama_server_process_and_vulkan("http://localhost:18082/v1")[0] == 50010
    assert diag_mod._probe_llama_server_process_and_vulkan("http://127.0.0.1:18082/v1")[0] == 50010
    assert diag_mod._probe_llama_server_process_and_vulkan("http://[::1]:18082/v1")[0] == 50010

    # IPv6-only listener matches [::1] and localhost, but NOT 127.0.0.1
    assert diag_mod._probe_llama_server_process_and_vulkan("http://[::1]:19001/v1")[0] == 70001
    assert diag_mod._probe_llama_server_process_and_vulkan("http://localhost:19001/v1")[0] == 70001
    assert diag_mod._probe_llama_server_process_and_vulkan("http://127.0.0.1:19001/v1") == (
        None,
        "unverified_from_api",
    )

    # Multiple distinct PIDs matching localhost:19000 must NOT pick the first PID
    assert diag_mod._probe_llama_server_process_and_vulkan("http://localhost:19000/v1") == (
        None,
        "unverified_from_api",
    )

    # Unbound target port 19999 must NOT fall back to 50010 or 60001
    assert diag_mod._probe_llama_server_process_and_vulkan("http://127.0.0.1:19999/v1") == (
        None,
        "unverified_from_api",
    )

    # Remote endpoint must not invoke any local subprocess probes
    calls.clear()
    assert diag_mod._probe_llama_server_process_and_vulkan("http://192.168.1.50:18082/v1") == (
        None,
        "unverified_from_api",
    )
    assert len(calls) == 0


def test_probe_permission_failure_invalid_url_timeout_and_job_report_propagation(
    monkeypatch: pytest.MonkeyPatch,
    isolated_config: AppConfig,
    tmp_path: Path,
) -> None:
    """Verify permission errors, invalid URLs, timeouts, and end-to-end job report provenance propagation."""
    import json
    import backend.diagnostics as diag_mod
    from backend.jobs.manager import JobManager
    from backend.translation.adapters.base import TranslationAdapter, TranslationAdapterResponse

    monkeypatch.setattr(diag_mod.os, "name", "nt")

    # 1. Permission denied when querying command line & modules -> returns PID with unverified_from_api
    netstat_out = (
        "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       38816\n"
        "  TCP    127.0.0.1:18082        0.0.0.0:0              LISTENING       39696\n"
    )
    fake_perm, _ = _make_subprocess_mock(
        netstat_stdout=netstat_out,
        pid_process_map={38816: "llama-server.exe", 39696: "llama-server.exe"},
        pid_vulkan_map={39696: None},
        pid_cmdline_map={39696: None},
    )
    monkeypatch.setattr(diag_mod.subprocess, "run", fake_perm)
    assert diag_mod._probe_llama_server_process_and_vulkan("http://127.0.0.1:18082/v1") == (
        39696,
        "unverified_from_api",
    )

    # 2. Invalid URLs and subprocess timeouts never raise and return (None, "unverified_from_api")
    for bad_url in ("", "not-a-url", "ftp://127.0.0.1:8000", "http://127.0.0.1:badport/v1", "http://127.0.0.1:0/v1"):
        assert diag_mod._probe_llama_server_process_and_vulkan(bad_url) == (
            None,
            "unverified_from_api",
        )

    fake_timeout, _ = _make_subprocess_mock(
        netstat_stdout=netstat_out,
        raise_timeout_on="netstat",
    )
    monkeypatch.setattr(diag_mod.subprocess, "run", fake_timeout)
    assert diag_mod._probe_llama_server_process_and_vulkan("http://127.0.0.1:18082/v1") == (
        None,
        "unverified_from_api",
    )

    # 3. Job final validation report records the connected 18082 server (PID 39696), not 8000 (PID 38816)
    fake_coexist, _ = _make_subprocess_mock(
        netstat_stdout=netstat_out,
        pid_process_map={38816: "llama-server.exe", 39696: "llama-server.exe"},
        pid_vulkan_map={38816: True, 39696: True},
        pid_cmdline_map={
            38816: "llama-server.exe --port 8000 -ngl 99",
            39696: r'llama-server.exe -m "C:\private_path\plamo.gguf" --port 18082 -ngl 0',
        },
    )
    monkeypatch.setattr(diag_mod.subprocess, "run", fake_coexist)

    class _StubEndpointAdapter(TranslationAdapter):
        adapter_version = "stub-llamacpp-1.0"
        prompt_version = "pfml3-v1"
        is_mock = False
        base_url = "http://127.0.0.1:18082/v1"

        def check_health(self) -> dict[str, str | bool | None]:
            return {
                "status": "connected",
                "is_mock": False,
                "model_name": "plamo-3-translate",
                "model_file": "plamo-3-translate-2b-Q4_K_M.gguf",
                "build_info": "b12167-240d68f",
            }

        def translate_segment(
            self,
            source_text: str,
            source_lang: str = "en",
            target_lang: str = "ja",
            form: str = "plain",
            context: str | None = None,
            history=None,
            glossary_entries=None,
            context_budget_tokens: int = 8192,
        ) -> TranslationAdapterResponse:
            from backend.translation.adapters.mock_adapter import MockPlamoAdapter

            res = MockPlamoAdapter().translate_segment(
                source_text=source_text,
                source_lang=source_lang,
                target_lang=target_lang,
                form=form,
                context=context,
                history=history,
                glossary_entries=glossary_entries,
                context_budget_tokens=context_budget_tokens,
            )
            res.is_mock = False
            return res

    cfg = AppConfig(
        runtime_dir=isolated_config.runtime_dir,
        llama_base_url="http://127.0.0.1:18082/v1",
    )
    mgr = JobManager(config=cfg, translation_adapter=_StubEndpointAdapter())
    pdf_path = _make_pdf(tmp_path / "provenance_report_check.pdf")
    doc = mgr.register_document(pdf_path)
    job = mgr.create_job(doc.id, TranslationJobConfig(use_mock_translator=False))
    completed = mgr.run_job(job.id)
    assert completed.state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS)

    prov = completed.execution_provenance
    assert prov["endpoint_url"] == "http://127.0.0.1:18082/v1"
    assert prov["engine_process"] == "llama-server.exe (PID 39696) [b12167-240d68f]"
    assert "38816" not in prov["engine_process"]
    assert prov["compute_backend"] == (
        "unverified_from_api (startup_arg: n_gpu_layers=0; dll: ggml-vulkan.dll loaded)"
    )
    assert "private_path" not in json.dumps(prov, ensure_ascii=False)

    report_file = cfg.runtime_dir / "outputs" / completed.id / f"{doc.id}.report.json"
    assert report_file.exists()
    report_data = json.loads(report_file.read_text(encoding="utf-8"))
    assert report_data["execution_provenance"]["engine_process"] == prov["engine_process"]
    assert report_data["execution_provenance"]["compute_backend"] == prov["compute_backend"]
    assert "38816" not in json.dumps(report_data, ensure_ascii=False)
    assert "private_path" not in json.dumps(report_data, ensure_ascii=False)

    # 4. Non-Windows platform fallback returns (None, "unverified_from_api") without error
    monkeypatch.setattr(diag_mod.os, "name", "posix")
    assert diag_mod._probe_llama_server_process_and_vulkan("http://127.0.0.1:18082/v1") == (
        None,
        "unverified_from_api",
    )
