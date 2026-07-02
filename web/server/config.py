from __future__ import annotations

import os
import sys
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = WEB_ROOT.parent
DESKTOP_APP_DIR = WORKSPACE_ROOT / "navelmaker2_desktop"
USERS_ROOT = WEB_ROOT / "users"

MAX_UPLOAD_BYTES = 6 * 1024 * 1024
DEFAULT_CHUNK_SIZE = 3000
DEFAULT_OVERLAP = 300
SECONDS_PER_CHUNK = 60
ESTIMATED_PIPELINE_OVERHEAD_SECONDS = 45
MAX_PIPELINE_WORKERS = max(1, int(os.getenv("NAVELMAKER_PIPELINE_WORKERS", "1")))

LLM_BASE_URL = os.getenv("NOVEL_LLM_BASE_URL", "http://localhost:1234/v1")
LLM_MODEL = os.getenv("NOVEL_LLM_MODEL", "gemma-4-26b-a4b-it")
LLM_API_KEY = os.getenv("NOVEL_LLM_API_KEY", "lm-studio")
ALLOWED_ORIGINS = [
    item.strip()
    for item in os.getenv("NAVELMAKER_ALLOWED_ORIGINS", "*").split(",")
    if item.strip()
]


def ensure_layout() -> None:
    USERS_ROOT.mkdir(parents=True, exist_ok=True)
    if not (DESKTOP_APP_DIR / "pipeline_program.py").is_file():
        raise RuntimeError(
            f"找不到现有 pipeline：{DESKTOP_APP_DIR / 'pipeline_program.py'}"
        )


def _desktop_app_files():
    if str(DESKTOP_APP_DIR) not in sys.path:
        sys.path.insert(0, str(DESKTOP_APP_DIR))
    import app_files

    return app_files


def get_llm_settings() -> dict:
    settings = _desktop_app_files().load_settings()
    return {
        "profile_name": settings.get("active_llm_profile", ""),
        "llm_base_url": os.getenv(
            "NOVEL_LLM_BASE_URL",
            settings.get("llm_base_url") or LLM_BASE_URL,
        ),
        "llm_model": os.getenv(
            "NOVEL_LLM_MODEL",
            settings.get("llm_model") or LLM_MODEL,
        ),
        "llm_api_key": (
            os.getenv("NOVEL_LLM_API_KEY", "").strip()
            or settings.get("llm_api_key")
            or LLM_API_KEY
        ),
        "active_llm_profile": settings.get("active_llm_profile", ""),
        "llm_profiles": settings.get("llm_profiles", []),
        "env_override": {
            "base_url": bool(os.getenv("NOVEL_LLM_BASE_URL")),
            "model": bool(os.getenv("NOVEL_LLM_MODEL")),
            "api_key": bool(os.getenv("NOVEL_LLM_API_KEY")),
        },
    }


def list_llm_profiles() -> dict:
    settings = get_llm_settings()
    return {
        "active_llm_profile": settings.get("active_llm_profile"),
        "profiles": settings.get("llm_profiles", []),
        "env_override": settings.get("env_override", {}),
    }


def save_llm_profile(profile: dict, make_active: bool = True) -> dict:
    return _desktop_app_files().save_llm_profile(profile, make_active=make_active)


def set_active_llm_profile(profile_name: str) -> dict:
    return _desktop_app_files().set_active_llm_profile(profile_name)


def delete_llm_profile(profile_name: str) -> dict:
    return _desktop_app_files().delete_llm_profile(profile_name)
