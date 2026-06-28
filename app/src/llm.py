from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings


class LLMError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.AsyncClient(timeout=settings.ai_timeout_seconds)

    async def close(self) -> None:
        await self.http.aclose()

    def _url(self) -> str:
        base = self.settings.ai_base_url
        if not base:
            raise LLMError("尚未配置 AI_BASE_URL")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.settings.ai_extra_headers}
        if self.settings.ai_api_key:
            headers["Authorization"] = f"Bearer {self.settings.ai_api_key}"
        return headers

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int = 2048,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, str] | None = None,
    ) -> LLMResponse:
        if not model:
            raise LLMError("尚未配置模型名称")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format

        last_error: Exception | None = None
        for attempt in range(self.settings.ai_max_retries + 1):
            try:
                response = await self.http.post(
                    self._url(), headers=self._headers(), json=payload
                )
                if response.status_code >= 400:
                    body = response.text[:2000]
                    if response.status_code not in {408, 429, 500, 502, 503, 504}:
                        raise LLMError(
                            f"AI 接口返回 HTTP {response.status_code}：{body}",
                            response.status_code,
                        )
                    raise httpx.HTTPStatusError(
                        body, request=response.request, response=response
                    )
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError(f"AI 接口未返回 choices：{str(data)[:1000]}")
                message = choices[0].get("message") or {}
                content = message.get("content") or ""
                if isinstance(content, list):
                    content = "".join(
                        str(part.get("text", "")) if isinstance(part, dict) else str(part)
                        for part in content
                    )
                return LLMResponse(
                    content=str(content),
                    tool_calls=message.get("tool_calls") or [],
                    raw_message=message,
                )
            except LLMError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < self.settings.ai_max_retries:
                    await asyncio.sleep(min(2**attempt, 4))
        raise LLMError(f"AI 接口请求失败：{last_error}") from last_error

