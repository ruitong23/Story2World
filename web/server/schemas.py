from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class LLMProfile(BaseModel):
    profile_name: str = Field(min_length=1, max_length=80)
    llm_base_url: str = Field(min_length=1, max_length=500)
    llm_model: str = Field(min_length=1, max_length=200)
    llm_api_key: str = Field(default="lm-studio", max_length=500)


class LLMProfileSaveRequest(LLMProfile):
    make_active: bool = True


class LLMProfileActivateRequest(BaseModel):
    profile_name: str = Field(min_length=1, max_length=80)


class LLMProfilesResponse(BaseModel):
    active_llm_profile: str
    profiles: list[LLMProfile] = Field(default_factory=list)
    env_override: dict[str, bool] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    session_id: str = Field(default="default", min_length=1, max_length=80)
    character_id: str = Field(min_length=1, max_length=160)
    message: str = Field(default="", max_length=12000)


class ChatResponse(BaseModel):
    session_id: str
    character_id: str
    reply: str
    used_sources: list[Any] = Field(default_factory=list)
    world_constraints: list[Any] = Field(default_factory=list)
    related_relationships: list[Any] = Field(default_factory=list)
    state_delta: dict[str, Any] = Field(default_factory=dict)
    character_state: dict[str, Any] = Field(default_factory=dict)
    scene_state: dict[str, Any] = Field(default_factory=dict)
    agent_activity: list[Any] = Field(default_factory=list)
    agent_trace: dict[str, Any] = Field(default_factory=dict)
    recovery_snapshot: dict[str, Any] = Field(default_factory=dict)
    rag_orchestration_summary: dict[str, Any] = Field(default_factory=dict)
    story_progress: dict[str, Any] = Field(default_factory=dict)


class ChatSessionResponse(BaseModel):
    session_id: str
    character_id: str
    has_session: bool = False
    recovery_snapshot: dict[str, Any] = Field(default_factory=dict)
    scene_state: dict[str, Any] = Field(default_factory=dict)
    character_state: dict[str, Any] = Field(default_factory=dict)
    agent_trace: dict[str, Any] = Field(default_factory=dict)
    state_revision: int = 0
    story_progress: dict[str, Any] = Field(default_factory=dict)


class ChatSaveResponse(BaseModel):
    session_id: str
    character_id: str
    saved: bool = True
    recovery_snapshot: dict[str, Any] = Field(default_factory=dict)
    state_revision: int = 0


class WorldAdminRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    session_id: str = Field(default="default", min_length=1, max_length=80)
    character_id: str | None = Field(default=None, max_length=160)
    message: str = Field(default="", max_length=12000)
    apply_changes: bool = True


class WorldAdminResponse(BaseModel):
    session_id: str
    character_id: str = ""
    reply: str = ""
    applied: bool = False
    event_id: str = ""
    state_revision: int = 0
    result: dict[str, Any] = Field(default_factory=dict)
    snapshot: dict[str, Any] = Field(default_factory=dict)


class SourcePreviewResponse(BaseModel):
    percent: float
    character_range: list[int] = Field(default_factory=list)
    summary: str
    excerpt: str


class AnchorPreviewResponse(BaseModel):
    project_id: str
    session_id: str
    character_id: str
    summary: str
    preview_material: str
    preview_cursor: int = 0
    current_anchor: dict[str, Any] = Field(default_factory=dict)
    focus_character_packet: dict[str, Any] = Field(default_factory=dict)
