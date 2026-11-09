"""llama.cpp Local Server Adapter for PLaMo 3 Translate (Section 9, 10, 13.A, 16).

Connects to `llama-server` over local loopback (`127.0.0.1` / `localhost` by default).
Uses the `/v1/completions` API with raw PFML3 prompts and `PLAMO_STOP_TOKENS`
as recommended by PFN's official PLaMo 3 Translate documentation.
Distinguishes server unreachable vs model missing vs model load error during diagnostics.
"""

from __future__ import annotations

import httpx

from backend.config import AppConfig
from backend.domain.models import GlossaryEntry
from backend.translation.adapters.base import (
    SecurityPolicyViolationError,
    TranslationAdapter,
    TranslationAdapterResponse,
    TranslationConnectionError,
)
from backend.translation.prompts.pfml3 import (
    PLAMO_MODEL_MAX_TOKENS,
    PLAMO_STOP_TOKENS,
    HistoryPair,
    plan_prompt_within_budget,
    strip_plamo_control_tokens,
)


class LlamaCppPlamoAdapter(TranslationAdapter):
    adapter_version: str = "llamacpp-pfml3-1.0.0"
    prompt_version: str = "pfml3-v1"
    is_mock: bool = False

    def __init__(
        self,
        config: AppConfig | None = None,
        base_url: str | None = None,
        model_alias: str | None = None,
        quantization: str = "Q4_K_M",
        timeout_sec: float = 120.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config or AppConfig()
        self.base_url = (base_url or self.config.llama_base_url).rstrip("/")
        self.model_alias = model_alias or self.config.llama_model_alias
        self.quantization = quantization
        self.timeout_sec = timeout_sec
        self._client = http_client

        # Enforce loopback-only unless explicitly permitted (Section 16)
        if not self.config.validate_translation_url(self.base_url):
            raise SecurityPolicyViolationError(
                f"External translation endpoint '{self.base_url}' is blocked by default security policy. "
                "Only 127.0.0.1 / localhost is allowed unless PAPERBRIDGE_ALLOW_EXTERNAL_TRANSLATION_HOST=true."
            )

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=self.timeout_sec)

    def check_health(self) -> dict[str, str | bool | None]:
        """Distinguish between server unreachable, model not found, model loading failure, and ready."""
        if not self.config.validate_translation_url(self.base_url):
            return {
                "status": "blocked_external_host",
                "is_mock": False,
                "model_name": None,
                "quantization": self.quantization,
                "detail": "ループバック以外の翻訳先URLがセキュリティ設定により拒否されました。",
            }

        client = self._get_client()
        should_close = self._client is None
        try:
            models_url = f"{self.base_url}/models"
            resp = client.get(models_url)
            if resp.status_code == 503:
                return {
                    "status": "model_load_failed",
                    "is_mock": False,
                    "model_name": None,
                    "quantization": self.quantization,
                    "detail": "llama-server は応答しましたがモデルがロード中または読み込み失敗（HTTP 503）です。",
                }
            if resp.status_code != 200:
                return {
                    "status": "model_load_failed",
                    "is_mock": False,
                    "model_name": None,
                    "quantization": self.quantization,
                    "detail": f"llama-server /v1/models が異常ステータス HTTP {resp.status_code} を返しました。",
                }
            data = resp.json()
            models = data.get("data", []) if isinstance(data, dict) else []
            if not models:
                return {
                    "status": "model_not_found",
                    "is_mock": False,
                    "model_name": None,
                    "quantization": self.quantization,
                    "detail": "llama-server にモデルが登録されていません（モデル未検出）。",
                }
            first_model_id = str(models[0].get("id", self.model_alias))
            model_path_name: str | None = None
            build_info: str | None = None
            try:
                root_url = self.base_url.removesuffix("/v1")
                props_resp = client.get(f"{root_url}/props")
                if props_resp.status_code == 200:
                    pdata = props_resp.json()
                    if isinstance(pdata, dict):
                        raw_mp = pdata.get("model_path")
                        if isinstance(raw_mp, str) and raw_mp.strip():
                            from pathlib import Path as _P

                            model_path_name = _P(raw_mp).name
                        raw_bi = pdata.get("build_info")
                        if isinstance(raw_bi, str) and raw_bi.strip():
                            build_info = raw_bi.strip()
            except Exception:
                pass
            return {
                "status": "connected",
                "is_mock": False,
                "model_name": first_model_id,
                "model_file": model_path_name,
                "build_info": build_info,
                "quantization": self.quantization,
                "detail": f"llama-server 接続成功（モデル: {first_model_id}）",
            }
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            return {
                "status": "server_unreachable",
                "is_mock": False,
                "model_name": None,
                "quantization": self.quantization,
                "detail": f"llama-server ({self.base_url}) に接続できません: {type(exc).__name__}",
            }
        finally:
            if should_close:
                client.close()

    def translate_segment(
        self,
        source_text: str,
        source_lang: str = "en",
        target_lang: str = "ja",
        form: str = "plain",
        context: str | None = None,
        history: list[HistoryPair] | None = None,
        glossary_entries: list[GlossaryEntry] | None = None,
        context_budget_tokens: int = 8192,
    ) -> TranslationAdapterResponse:
        if not self.config.validate_translation_url(self.base_url):
            raise SecurityPolicyViolationError(
                f"Refusing to send text to non-loopback URL: {self.base_url}"
            )

        budget_plan = plan_prompt_within_budget(
            source_text=source_text,
            context_budget_tokens=context_budget_tokens,
            model_max_tokens=PLAMO_MODEL_MAX_TOKENS,
            source_lang=source_lang,
            target_lang=target_lang,
            form=form,
            context=context,
            history=history,
            glossary_entries=glossary_entries,
        )

        payload = {
            "model": self.model_alias,
            "prompt": budget_plan.prompt,
            "temperature": 0.0,
            "max_tokens": budget_plan.reserved_output_tokens,
            "stop": PLAMO_STOP_TOKENS,
        }

        client = self._get_client()
        should_close = self._client is None
        try:
            resp = client.post(f"{self.base_url}/completions", json=payload)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, httpx.NetworkError) as exc:
            raise TranslationConnectionError(
                f"Failed to communicate with llama-server at {self.base_url}: {exc}"
            ) from exc
        finally:
            if should_close:
                client.close()

        choices = body.get("choices", [])
        if not choices:
            raise TranslationConnectionError("llama-server returned empty choices list")

        choice0 = choices[0]
        raw_text = str(choice0.get("text", ""))
        finish_reason = str(choice0.get("finish_reason") or "stop")
        usage = body.get("usage") or {}

        cleaned = strip_plamo_control_tokens(raw_text)
        return TranslationAdapterResponse(
            translated_text=cleaned,
            raw_output=raw_text,
            prompt_used=budget_plan.prompt,
            model_name=self.model_alias,
            quantization=self.quantization,
            finish_reason=finish_reason,
            is_mock=False,
            prompt_tokens=int(usage.get("prompt_tokens", budget_plan.estimated_input_tokens)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )
