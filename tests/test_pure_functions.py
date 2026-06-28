import math

import pytest

from src.agent import contains_sensitive_secret, extract_json_object
from src.embedding import EmbeddingError, normalize_and_resize
from src.memory_store import clean_relation


def test_normalize_and_resize_matryoshka_vector():
    vector = normalize_and_resize([3, 4, 99], 2)
    assert vector == pytest.approx([0.6, 0.8])
    assert math.sqrt(sum(v * v for v in vector)) == pytest.approx(1.0)


def test_vector_dimension_guard():
    with pytest.raises(EmbeddingError):
        normalize_and_resize([1, 2], 3)


def test_extract_json_from_fenced_response():
    assert extract_json_object('```json\n{"should_remember": false}\n```')[
        "should_remember"
    ] is False


def test_relation_is_sanitized():
    assert clean_relation("Works With / 合作") == "works_with___合作"


def test_sensitive_credentials_are_detected():
    assert contains_sensitive_secret("API key: sk-abcdefghijklmnopqrstuvwxyz")
    assert not contains_sensitive_secret("用户使用 1Password 管理自己的密码")
