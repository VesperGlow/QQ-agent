"""真实模型冒烟测试：下载 uint8 量化的 Qwen3-Embedding 并验证语义检索方向正确。

模型约 640MB，默认跳过；CI 里显式设 RUN_EMBEDDING_SMOKE=1 运行（模型目录有缓存）。
"""
import asyncio
import os

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_EMBEDDING_SMOKE"),
    reason="需要下载 640MB 模型，仅在 RUN_EMBEDDING_SMOKE=1 时运行",
)


def test_real_model_semantic_sanity():
    from src.config import Settings
    from src.embedding import EmbeddingClient

    settings = Settings(_env_file=None, embedding_context_size=512)
    client = EmbeddingClient(settings)
    try:
        docs = asyncio.run(
            client.embed(["用户养了一只叫年糕的猫", "用户家里有只小猫咪", "用户今天买了新键盘"])
        )
        cat1, cat2, keyboard = (np.asarray(v, dtype=np.float32) for v in docs)
        # 归一化检查
        for vector in (cat1, cat2, keyboard):
            assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-3)
            assert vector.shape == (settings.embedding_dimensions,)
        # 语义方向：两句猫的相似度必须高于猫 vs 键盘
        assert cat1 @ cat2 > cat1 @ keyboard + 0.05
        # 查询走 instruction 前缀路径
        query = np.asarray(
            asyncio.run(client.embed(["我的宠物叫什么名字"], is_query=True))[0],
            dtype=np.float32,
        )
        assert query @ cat1 > query @ keyboard
    finally:
        asyncio.run(client.close())
