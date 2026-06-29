from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=200_000)
    conversation_id: str | None = Field(default=None, max_length=128)
    system_prompt: str | None = Field(default=None, max_length=20_000)


class MemoryView(BaseModel):
    id: str
    text: str
    kind: str
    importance: int
    subject: Literal["user", "assistant"] = "user"
    score: float | None = None
    entities: list[dict[str, str]] = Field(default_factory=list)
    created_at: str | None = None


class ChatResponse(BaseModel):
    conversation_id: str
    message: str
    retrieved_memories: list[MemoryView]
    saved_memories: list[MemoryView]
    tool_events: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EntityInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(default="entity", min_length=1, max_length=80)


class CreateMemoryRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=50_000)
    kind: Literal["preference", "fact", "goal", "relationship", "constraint", "event", "other"] = "other"
    importance: int = Field(default=3, ge=1, le=5)
    subject: Literal["user", "assistant"] = "user"
    entities: list[EntityInput] = Field(default_factory=list, max_length=30)


class LinkMemoryRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    from_memory_id: str = Field(min_length=1, max_length=128)
    to_memory_id: str = Field(min_length=1, max_length=128)
    relation: str = Field(default="related", min_length=1, max_length=80)

