from __future__ import annotations

import sys

from fastapi import APIRouter, HTTPException

from ..config import (
    DESKTOP_APP_DIR,
    delete_llm_profile,
    get_llm_settings,
    list_llm_profiles,
    save_llm_profile,
    set_active_llm_profile,
)
from ..schemas import (
    LLMProfileActivateRequest,
    LLMProfileSaveRequest,
    LLMProfilesResponse,
)


router = APIRouter(prefix="/llm", tags=["llm"])


@router.get("/profiles", response_model=LLMProfilesResponse)
def get_profiles():
    return list_llm_profiles()


@router.post("/profiles", response_model=LLMProfilesResponse)
def save_profile(payload: LLMProfileSaveRequest):
    data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    save_llm_profile(data, make_active=payload.make_active)
    return list_llm_profiles()


@router.post("/profiles/active", response_model=LLMProfilesResponse)
def activate_profile(payload: LLMProfileActivateRequest):
    try:
        set_active_llm_profile(payload.profile_name)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="LLM profile 不存在。") from error
    return list_llm_profiles()


@router.delete("/profiles/{profile_name}", response_model=LLMProfilesResponse)
def remove_profile(profile_name: str):
    delete_llm_profile(profile_name)
    return list_llm_profiles()


@router.post("/check")
def check_active_profile():
    settings = get_llm_settings()
    try:
        if str(DESKTOP_APP_DIR) not in sys.path:
            sys.path.insert(0, str(DESKTOP_APP_DIR))
        from llm_api import list_models

        models = list_models(settings["llm_base_url"], settings["llm_api_key"])
        selected = settings["llm_model"]
        return {
            "ok": True,
            "active_llm_profile": settings.get("active_llm_profile"),
            "selected_model": selected,
            "selected_model_found": selected in models,
            "models": models[:100],
        }
    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail=f"LLM server check failed: {error}",
        ) from error


@router.get("/usage")
def token_usage():
    if str(DESKTOP_APP_DIR) not in sys.path:
        sys.path.insert(0, str(DESKTOP_APP_DIR))
    from llm_api import token_usage_summary

    return token_usage_summary(limit=300)
