from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 支持在 MCP 配置里用 ${NAME} 或 $NAME 引用环境变量，方便“只在 env 填 key”。
_ENV_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        resolved = os.environ.get(name)
        if resolved is None:
            raise ValueError(f"MCP_SERVERS_JSON 引用了未设置的环境变量 {name}")
        return resolved

    return _ENV_REF.sub(replace, value)


def _string_list(value: Any, index: int, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"MCP_SERVERS_JSON[{index}] 的 {field_name} 必须是字符串数组")
    return [str(item).strip() for item in value if str(item).strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    app_api_key: str = ""
    log_level: str = "INFO"
    # 默认人设（只写性格/口吻）。请求未带 system_prompt 时用它；留空则用内置默认人设。
    # 系统指令层（输出格式/记忆工具/安全）始终生效、与此无关。
    persona_prompt: str = ""
    # 系统指令层。留空用内置默认（推荐）；非空则整体覆盖，需自行包含格式/安全等约束。
    # 多行用字面量 \n 分隔（会被还原为换行），方便放进单行 env。
    system_instructions: str = ""

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

    # 远程 MCP 工具服务器。JSON 数组，每项形如：
    # {"name":"tavily","url":"https://mcp.tavily.com/mcp/?tavilyApiKey=tvly-xxx"}
    # 可选字段：transport（streamable_http|sse，默认 streamable_http）、headers（对象）、enabled（默认 true）。
    mcp_servers_json: str = "[]"
    mcp_timeout_seconds: float = 300
    mcp_result_max_chars: int = 12000

    # SQLite 数据库文件；所有对话、记忆、情绪都在这一个文件里。
    db_path: str = "/data/memory.db"

    # local：进程内 ONNX 推理（默认，零外部依赖）；openai：远程 OpenAI-compatible 接口。
    embedding_api_style: Literal["local", "openai"] = "local"
    embedding_model: str = "electroglyph/Qwen3-Embedding-0.6B-onnx-uint8"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_dimensions: int = Field(default=1024, ge=32, le=4096)
    # 单条输入的最大 token 数。记忆和聊天消息都很短，压低上限即压低推理峰值内存。
    embedding_context_size: int = Field(default=2048, ge=64, le=32768)
    embedding_query_instruction: str = (
        "Given a user's message, retrieve memories that are useful for personalizing the response"
    )
    embedding_timeout_seconds: float = 180
    embedding_threads: int = Field(default=4, ge=1, le=32)
    # uint8 量化输出的还原区间（electroglyph 量化版模型卡给出的标定值）。
    embedding_output_min: float = -0.3009
    embedding_output_max: float = 0.3952

    memory_search_limit: int = Field(default=8, ge=1, le=50)
    memory_min_score: float = Field(default=0.30, ge=-1, le=1)
    memory_history_messages: int = Field(default=16, ge=0, le=100)
    memory_duplicate_threshold: float = Field(default=0.995, ge=0.8, le=1)
    # 对纯寒暄/填充类短消息跳过记忆筛选与情绪抽取，省一次便宜模型调用。
    memory_judge_skip_trivial: bool = True

    # 滚动摘要：把滑出短期窗口的旧消息压缩进会话摘要，超长对话也能保留连续性。
    conversation_summary_enabled: bool = True
    # 累计这么多条"已滑出窗口且未摘要"的消息才触发一次摘要更新（用便宜模型）。
    conversation_summary_batch: int = Field(default=10, ge=2, le=100)
    conversation_summary_max_chars: int = Field(default=1000, ge=100, le=8000)

    # 时序加权检索：在向量相似度之上叠加新近度、记忆等级与关键词命中。
    # 相似度仍是主导，其余为小幅加成；全设 0 即退回纯相似度排序。
    memory_similarity_weight: float = Field(default=1.0, ge=0)
    memory_recency_weight: float = Field(default=0.15, ge=0)
    memory_importance_weight: float = Field(default=0.10, ge=0)
    memory_keyword_weight: float = Field(default=0.08, ge=0)
    memory_recency_halflife_days: float = Field(default=30.0, gt=0)

    # 记忆等级 1..9 的保留天数梯度（逗号分隔 9 个数字）；等级 10 永久。
    # 记忆每次被再次提及都会以当下时间续期，常被提起的记忆自然活得久。
    memory_level_ttl_days: str = "2,4,7,14,30,60,120,240,365"

    # 情绪时间线：从对话抽取用户情绪、按时间成链，让助手感知跨会话趋势。
    mood_tracking_enabled: bool = True
    mood_trend_days: int = Field(default=7, ge=1, le=90)
    mood_recent_limit: int = Field(default=50, ge=1, le=500)

    # 时间感知：每轮注入当前北京时间及与上一条消息的间隔，让助手能自然问候/衔接语气。
    time_awareness_enabled: bool = True

    @field_validator("ai_base_url", "embedding_base_url")
    @classmethod
    def trim_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def memory_level_ttls(self) -> list[float]:
        try:
            values = [float(part) for part in self.memory_level_ttl_days.split(",")]
        except ValueError as exc:
            raise ValueError("MEMORY_LEVEL_TTL_DAYS 必须是逗号分隔的数字") from exc
        if len(values) != 9 or any(v <= 0 for v in values):
            raise ValueError("MEMORY_LEVEL_TTL_DAYS 需要恰好 9 个正数（等级 1..9）")
        return values

    @property
    def mcp_servers(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.mcp_servers_json or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("MCP_SERVERS_JSON 必须是合法 JSON") from exc
        if not isinstance(raw, list):
            raise ValueError("MCP_SERVERS_JSON 必须是 JSON 数组")
        servers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError("MCP_SERVERS_JSON 的每一项必须是对象")
            if not item.get("enabled", True):
                continue
            name = str(item.get("name", "")).strip()
            url = _expand_env(str(item.get("url", "")).strip())
            if not name or not url:
                raise ValueError(f"MCP_SERVERS_JSON[{index}] 缺少 name 或 url")
            if name in seen:
                raise ValueError(f"MCP_SERVERS_JSON 出现重复的 name：{name}")
            seen.add(name)
            transport = str(item.get("transport", "streamable_http")).strip().lower()
            if transport not in {"streamable_http", "sse"}:
                raise ValueError(f"MCP_SERVERS_JSON[{index}] 的 transport 只能是 streamable_http 或 sse")
            headers = item.get("headers") or {}
            if not isinstance(headers, dict):
                raise ValueError(f"MCP_SERVERS_JSON[{index}] 的 headers 必须是对象")
            include = _string_list(item.get("tools"), index, "tools")
            exclude = _string_list(item.get("exclude"), index, "exclude")
            servers.append(
                {
                    "name": name,
                    "url": url,
                    "transport": transport,
                    "headers": {str(k): _expand_env(str(v)) for k, v in headers.items()},
                    "include": include,
                    "exclude": exclude,
                }
            )
        return servers

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
            "embedding_api_style": self.embedding_api_style,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "embedding_context_size": self.embedding_context_size,
            "db_path": self.db_path,
            "memory_level_ttl_days": self.memory_level_ttl_days,
            "mcp_servers": [server["name"] for server in self.mcp_servers],
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

