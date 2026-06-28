from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    app_api_key: str = ""
    log_level: str = "INFO"

    ai_base_url: str = ""
    ai_api_key: str = ""
    memory_model: str = ""
    chat_model: str = ""
    ai_timeout_seconds: float = 120
    ai_max_retries: int = 2
    ai_extra_headers_json: str = "{}"
    chat_max_output_tokens: int = 2048
    memory_max_output_tokens: int = 800
    max_tool_rounds: int = 6

    embedding_base_url: str = "http://embedding:80"
    embedding_api_key: str = ""
    embedding_api_style: Literal["tei", "openai"] = "tei"
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_dimensions: int = Field(default=1024, ge=32, le=4096)
    embedding_context_size: int = Field(default=32768, ge=128, le=131072)
    embedding_query_instruction: str = (
        "Given a user's message, retrieve memories that are useful for personalizing the response"
    )
    embedding_timeout_seconds: float = 180

    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"

    memory_search_limit: int = Field(default=8, ge=1, le=50)
    memory_min_score: float = Field(default=0.30, ge=-1, le=1)
    memory_history_messages: int = Field(default=16, ge=0, le=100)
    memory_duplicate_threshold: float = Field(default=0.995, ge=0.8, le=1)

    @field_validator("ai_base_url", "embedding_base_url", "neo4j_uri")
    @classmethod
    def trim_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def ai_extra_headers(self) -> dict[str, str]:
        try:
            raw = json.loads(self.ai_extra_headers_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("AI_EXTRA_HEADERS_JSON 必须是合法 JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("AI_EXTRA_HEADERS_JSON 必须是 JSON 对象")
        return {str(k): str(v) for k, v in raw.items()}

    @property
    def safe_summary(self) -> dict[str, object]:
        return {
            "ai_base_url": self.ai_base_url,
            "memory_model": self.memory_model,
            "chat_model": self.chat_model,
            "embedding_base_url": self.embedding_base_url,
            "embedding_api_style": self.embedding_api_style,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "embedding_context_size": self.embedding_context_size,
            "neo4j_uri": self.neo4j_uri,
            "neo4j_database": self.neo4j_database,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

