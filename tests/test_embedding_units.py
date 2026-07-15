import numpy as np
import pytest

from src.embedding import dequantize, last_token_pool, normalize_and_resize


def test_dequantize_recovers_calibration_range():
    quantized = np.array([0, 255], dtype=np.uint8)
    restored = dequantize(quantized, -0.3009, 0.3952)
    assert restored[0] == pytest.approx(-0.3009)
    assert restored[1] == pytest.approx(0.3952)


def test_last_token_pool_uses_final_real_token():
    hidden = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    mask = np.array([[1, 1, 0], [1, 1, 1]])
    pooled = last_token_pool(hidden, mask)
    assert np.array_equal(pooled[0], hidden[0, 1])
    assert np.array_equal(pooled[1], hidden[1, 2])


def test_matryoshka_resize_renormalizes():
    vector = normalize_and_resize([3.0, 4.0, 99.0], 2)
    assert vector == pytest.approx([0.6, 0.8])
