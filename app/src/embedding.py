from __future__ import annotations

import asyncio
import logging
import math
import threading
from pathlib import Path
from typing import Any, Sequence

import httpx
import numpy as np

from .config import Settings

logger = logging.getLogger(__name__)


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


def dequantize(array: np.ndarray, out_min: float, out_max: float) -> np.ndarray:
    """把 uint8 非对称线性量化的输出还原为 float32。

    量化把 [out_min, out_max] 线性映射到 [0, 255]；余弦相似度对平移不封闭，
    必须先还原原值再归一化，不能直接拿 uint8 点积。
    """
    scale = (out_max - out_min) / 255.0
    return array.astype(np.float32) * scale + out_min


def last_token_pool(hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """Qwen3-Embedding 系列用最后一个真实 token 的隐状态作为句向量。"""
    lengths = attention_mask.sum(axis=1) - 1
    return hidden[np.arange(hidden.shape[0]), lengths]


class LocalOnnxEmbedder:
    """进程内 ONNX 推理，无独立 embedding 服务。

    模型权重是常驻内存的大头（uint8 量化的 Qwen3-0.6B 约 650MB），
    通过关闭 ORT 内存 arena、限制线程数与输入长度控制峰值。
    推理逐条进行（无 batch padding），互斥锁串行化避免并发放大内存。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._session: Any = None
        self._tokenizer: Any = None
        self._eos_id: int | None = None
        self._init_lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._init_lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            from huggingface_hub import constants, snapshot_download
            from tokenizers import Tokenizer

            logger.info("正在加载本地 embedding 模型 %s ...", self.settings.embedding_model)
            # 用 local_dir 直下（不走 hub 缓存的 symlink 布局）：Windows 无开发者
            # 模式、部分 Docker volume 驱动都不支持 symlink，直下在哪都能跑。
            target = (
                Path(constants.HF_HUB_CACHE).parent
                / "local"
                / self.settings.embedding_model.replace("/", "--")
            )
            snapshot = Path(
                snapshot_download(
                    self.settings.embedding_model,
                    local_dir=target,
                    allow_patterns=[
                        "*.onnx",
                        "*.onnx_data",
                        "*.onnx.data",
                        "tokenizer.json",
                        "tokenizer_config.json",
                        "special_tokens_map.json",
                        "config.json",
                    ],
                )
            )
            onnx_files = sorted(
                snapshot.rglob("*.onnx"), key=lambda p: p.stat().st_size, reverse=True
            )
            if not onnx_files:
                raise EmbeddingError(f"模型仓库 {self.settings.embedding_model} 里没有 .onnx 文件")
            tokenizer_file = next(snapshot.rglob("tokenizer.json"), None)
            if tokenizer_file is None:
                raise EmbeddingError(f"模型仓库 {self.settings.embedding_model} 里没有 tokenizer.json")

            tokenizer = Tokenizer.from_file(str(tokenizer_file))
            tokenizer.enable_truncation(max_length=self.settings.embedding_context_size)
            for candidate in ("<|endoftext|>", "</s>", "<|im_end|>"):
                token_id = tokenizer.token_to_id(candidate)
                if token_id is not None:
                    self._eos_id = token_id
                    break

            options = ort.SessionOptions()
            # arena 会为峰值持续保留大块内存；关闭后让激活值内存用完即还。
            options.enable_cpu_mem_arena = False
            options.intra_op_num_threads = self.settings.embedding_threads
            self._session = ort.InferenceSession(
                str(onnx_files[0]), sess_options=options, providers=["CPUExecutionProvider"]
            )
            self._tokenizer = tokenizer
            logger.info("本地 embedding 模型加载完成：%s", onnx_files[0].name)

    def embed_one(self, text: str) -> list[float]:
        self._ensure_loaded()
        encoding = self._tokenizer.encode(text)
        ids = list(encoding.ids)
        # Qwen3-Embedding 约定输入以 EOS 结尾（最后 token 池化取的就是它）。
        if self._eos_id is not None and (not ids or ids[-1] != self._eos_id):
            ids = ids[: self.settings.embedding_context_size - 1] + [self._eos_id]
        input_ids = np.asarray([ids], dtype=np.int64)
        attention_mask = np.ones_like(input_ids)
        feeds: dict[str, np.ndarray] = {}
        for graph_input in self._session.get_inputs():
            if graph_input.name == "input_ids":
                feeds[graph_input.name] = input_ids
            elif graph_input.name == "attention_mask":
                feeds[graph_input.name] = attention_mask
            elif graph_input.name == "position_ids":
                feeds[graph_input.name] = np.arange(len(ids), dtype=np.int64)[None, :]
            else:
                raise EmbeddingError(f"ONNX 模型需要未知输入 {graph_input.name}")
        output = self._session.run(None, feeds)[0]
        if output.ndim == 3:
            output = last_token_pool(output, attention_mask)
        if output.dtype == np.uint8:
            output = dequantize(
                output,
                self.settings.embedding_output_min,
                self.settings.embedding_output_max,
            )
        return [float(v) for v in np.asarray(output[0], dtype=np.float32)]


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.AsyncClient(timeout=settings.embedding_timeout_seconds)
        self._local = LocalOnnxEmbedder(settings) if settings.embedding_api_style == "local" else None
        # 推理释放 GIL 但串行执行，防止并发请求把激活值内存翻倍。
        self._infer_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.http.aclose()

    async def warmup(self) -> None:
        """启动时预热：下载/加载模型并跑一次推理，避免首条消息长时间等待。"""
        if self._local is None:
            return
        async with self._infer_lock:
            await asyncio.to_thread(self._local.embed_one, "warmup")

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

        if self._local is not None:
            vectors = []
            async with self._infer_lock:
                for text in prepared:
                    vectors.append(await asyncio.to_thread(self._local.embed_one, text))
        else:
            vectors = await self._embed_openai(prepared)
        return [
            normalize_and_resize(vector, self.settings.embedding_dimensions)
            for vector in vectors
        ]

    async def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        base = self.settings.embedding_base_url
        if not base:
            raise EmbeddingError("EMBEDDING_API_STYLE=openai 时必须设置 EMBEDDING_BASE_URL")
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
            if self._local is not None:
                return self._local._session is not None
            await self.embed(["health check"])
            return True
        except Exception:
            return False
