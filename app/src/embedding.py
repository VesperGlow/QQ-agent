from __future__ import annotations

import math
from typing import Sequence

import httpx

from .config import Settings


class EmbeddingError(RuntimeError):
    pass


def normalize_and_resize(vector: Sequence[float], dimensions: int) -> list[float]:
    if len(vector) < dimensions:
        raise EmbeddingError(
            f"Embedding 返回 {len(vector)} 维，但 EMBEDDING_DIMENSIONS={dimensions}"
        )
    resized = [float(value) for value in vector[:dimensions]]
    norm = math.sqrt(sum(value * value for value in resized))
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingError("Embedding 返回了无效的零向量或非有限数值")
    return [value / norm for value in resized]


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.AsyncClient(timeout=settings.embedding_timeout_seconds)

    async def close(self) -> None:
        await self.http.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.embedding_api_key:
            headers["Authorization"] = f"Bearer {self.settings.embedding_api_key}"
        return headers

    async def embed(self, texts: Sequence[str], *, is_query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        prepared = [text.strip() for text in texts]
        if any(not text for text in prepared):
            raise EmbeddingError("不能向量化空文本")
        if is_query:
            instruction = self.settings.embedding_query_instruction.strip()
            if instruction:
                prepared = [f"Instruct: {instruction}\nQuery: {text}" for text in prepared]

        if self.settings.embedding_api_style == "tei":
            vectors = await self._embed_tei(prepared)
        else:
            vectors = await self._embed_openai(prepared)
        return [
            normalize_and_resize(vector, self.settings.embedding_dimensions)
            for vector in vectors
        ]

    async def _embed_tei(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.settings.embedding_base_url}/embed"
        try:
            response = await self.http.post(
                url,
                headers=self._headers(),
                json={"inputs": texts, "truncate": True},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = getattr(exc.response, "text", "")[:1000] if exc.response else str(exc)
            raise EmbeddingError(f"TEI 请求失败：{detail}") from exc
        data = response.json()
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            raise EmbeddingError("TEI 返回格式不是二维向量数组")
        return data

    async def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        base = self.settings.embedding_base_url
        url = base if base.endswith("/embeddings") else f"{base}/embeddings"
        payload = {
            "model": self.settings.embedding_model,
            "input": texts,
            "dimensions": self.settings.embedding_dimensions,
        }
        try:
            response = await self.http.post(url, headers=self._headers(), json=payload)
            if response.status_code == 400 and "dimension" in response.text.lower():
                payload.pop("dimensions")
                response = await self.http.post(url, headers=self._headers(), json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = getattr(exc.response, "text", "")[:1000] if exc.response else str(exc)
            raise EmbeddingError(f"OpenAI-compatible Embedding 请求失败：{detail}") from exc
        data = response.json().get("data", [])
        data.sort(key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in data]
        if len(vectors) != len(texts) or any(not isinstance(v, list) for v in vectors):
            raise EmbeddingError("Embedding 接口返回的向量数量或格式不正确")
        return vectors

    async def health(self) -> bool:
        try:
            if self.settings.embedding_api_style == "tei":
                response = await self.http.get(f"{self.settings.embedding_base_url}/health")
                return response.status_code == 200
            await self.embed(["health check"])
            return True
        except Exception:
            return False

