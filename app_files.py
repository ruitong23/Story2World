"""Shared file contracts and settings for the desktop applications."""

import json
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
GENERATED_DB_DIR = APP_DIR / "db"
GRAPH_DB_DIR = GENERATED_DB_DIR / "graph"
CANONICAL_DB_DIR = GENERATED_DB_DIR / "canonical"
AGENT_DB_DIR = GENERATED_DB_DIR / "agents"
RUNTIME_DB_DIR = GENERATED_DB_DIR / "runtime"


def generated_db_path(group, filename):
    if group == "graph":
        return GRAPH_DB_DIR / filename
    if group == "canonical":
        return CANONICAL_DB_DIR / filename
    if group == "agents":
        return AGENT_DB_DIR / filename
    if group == "runtime":
        return RUNTIME_DB_DIR / filename
    # Backward-compatible aliases for older code paths.
    if group in {"world", "characters"}:
        return CANONICAL_DB_DIR / filename
    raise ValueError(f"Unknown generated DB group: {group}")

PREPARATION_OUTPUTS = [
    ("db/graph/novel_ontology.json", "Ontology used by graph extraction"),
    ("db/graph/raw_graph_triples.json", "Single-pass skeleton graph triples for selected chunks"),
    ("db/graph/mention_weak_relations.json", "Mention-level weak relation evidence"),
    ("db/canonical/mention_alias_index.json", "Mention and alias index"),
    ("db/canonical/canonical_entities.json", "Resolved canonical entities"),
    ("db/graph/normalized_graph_triples.json", "Normalized graph"),
    ("db/canonical/canonical_relationship_db.json", "Canonical relationship database"),
    ("db/canonical/relationship_arc_db.json", "Relationship arc database"),
    ("db/graph/structured_world_graph.json", "Structured world graph aggregated from skeleton evidence"),
    ("db/canonical/character_state_db.json", "Character state database"),
    ("db/canonical/world_db.json", "Canonical world aggregate"),
    ("db/canonical/canonical_timeline_db.json", "Canonical baseline timeline"),
    ("db/canonical/canonical_event_db.json", "Canonical event database"),
    ("db/canonical/canonical_scene_beat_db.json", "Skeleton scene-beat database for RAG and pacing"),
    ("db/canonical/canonical_character_db.json", "Canonical character template database"),
    ("db/canonical/canonical_relationships_db.json", "Canonical relationship evidence database"),
    ("db/canonical/canonical_ability_db.json", "Canonical ability database"),
    ("db/canonical/canonical_item_db.json", "Canonical item database"),
    ("db/canonical/canonical_organization_db.json", "Canonical organization database"),
    ("db/canonical/canonical_location_db.json", "Canonical location database"),
    ("db/canonical/canonical_world_rule_db.json", "Canonical world-rule database"),
    ("db/canonical/canonical_knowledge_db.json", "Canonical knowledge database"),
    ("db/runtime/simulation_state_template.json", "Cutoff-based initial state template"),
    ("db/runtime/runtime_event_db.json", "Runtime event queue database"),
    ("db/runtime/runtime_relationship_db.json", "Runtime relationship database"),
    ("db/runtime/runtime_log.json", "Runtime event-sourcing log"),
    ("db/agents/agent_profiles.json", "Canonical agent profile templates"),
    ("db/agents/runtime_agent_state.json", "Runtime agent dynamic state"),
]

SIMULATION_REQUIRED_FILES = [
    ("db/canonical/world_db.json", "Canonical world aggregate"),
    ("db/canonical/canonical_timeline_db.json", "Canonical baseline timeline"),
    ("db/canonical/canonical_event_db.json", "Canonical event database"),
    ("db/runtime/simulation_state_template.json", "Cutoff-based initial state template"),
    ("db/runtime/runtime_event_db.json", "Runtime event queue database"),
    ("db/runtime/runtime_relationship_db.json", "Runtime relationship database"),
    ("db/canonical/canonical_character_db.json", "Canonical character template database"),
    ("db/agents/agent_profiles.json", "Canonical agent profile templates"),
    ("db/agents/runtime_agent_state.json", "Runtime agent dynamic state"),
    ("step17_runtime.py", "Step 17 simulation engine"),
]


def file_status(rows, base_dir=APP_DIR):
    return [
        {
            "name": name,
            "description": description,
            "exists": (Path(base_dir) / name).is_file(),
            "path": Path(base_dir) / name,
        }
        for name, description in rows
    ]


def default_llm_settings():
    return {
        "profile_name": "Local LM Studio",
        "llm_base_url": "http://localhost:1234/v1",
        "llm_model": "gemma-4-26b-a4b-it",
        "llm_api_key": "lm-studio",
    }


def _normalize_profile(profile, fallback_name="Local LM Studio"):
    defaults = default_llm_settings()
    raw = {**defaults, **(profile or {})}
    name = str(raw.get("profile_name") or raw.get("name") or fallback_name).strip()
    base_url = str(raw.get("llm_base_url") or raw.get("base_url") or "").strip()
    model = str(raw.get("llm_model") or raw.get("model") or "").strip()
    api_key = str(raw.get("llm_api_key") or raw.get("api_key") or "").strip()
    return {
        "profile_name": name or fallback_name,
        "llm_base_url": base_url or defaults["llm_base_url"],
        "llm_model": model or defaults["llm_model"],
        "llm_api_key": api_key or defaults["llm_api_key"],
    }


def _settings_with_profiles(saved):
    defaults = default_llm_settings()
    saved = saved or {}
    profiles = []
    for item in saved.get("llm_profiles", []) or []:
        if isinstance(item, dict):
            profiles.append(_normalize_profile(item))
    active_name = str(saved.get("active_llm_profile") or "").strip()
    legacy = _normalize_profile(
        {
            "profile_name": active_name or saved.get("profile_name"),
            "llm_base_url": saved.get("llm_base_url"),
            "llm_model": saved.get("llm_model"),
            "llm_api_key": saved.get("llm_api_key"),
        },
        fallback_name=defaults["profile_name"],
    )
    if not profiles:
        profiles = [legacy]
    elif not any(item["profile_name"] == legacy["profile_name"] for item in profiles):
        profiles.insert(0, legacy)
    active_name = active_name or legacy["profile_name"] or profiles[0]["profile_name"]
    active = next(
        (item for item in profiles if item["profile_name"] == active_name),
        profiles[0],
    )
    return {
        **saved,
        **active,
        "active_llm_profile": active["profile_name"],
        "llm_profiles": profiles,
    }


def load_settings():
    defaults = _settings_with_profiles({})
    if not SETTINGS_PATH.is_file():
        return defaults
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    return _settings_with_profiles(saved)


def save_settings(settings):
    settings = _settings_with_profiles(settings)
    temporary = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(SETTINGS_PATH)


def llm_profiles():
    settings = load_settings()
    return {
        "active_llm_profile": settings["active_llm_profile"],
        "profiles": settings["llm_profiles"],
    }


def save_llm_profile(profile, make_active=True):
    settings = load_settings()
    normalized = _normalize_profile(profile)
    profiles = [
        item
        for item in settings.get("llm_profiles", [])
        if item["profile_name"] != normalized["profile_name"]
    ]
    profiles.append(normalized)
    profiles.sort(key=lambda item: item["profile_name"].casefold())
    active_name = (
        normalized["profile_name"]
        if make_active
        else settings.get("active_llm_profile") or normalized["profile_name"]
    )
    active = next(
        (item for item in profiles if item["profile_name"] == active_name),
        profiles[0],
    )
    settings.update(
        {
            **active,
            "active_llm_profile": active["profile_name"],
            "llm_profiles": profiles,
        }
    )
    save_settings(settings)
    return load_settings()


def set_active_llm_profile(profile_name):
    settings = load_settings()
    profiles = settings.get("llm_profiles", [])
    active = next(
        (item for item in profiles if item["profile_name"] == profile_name),
        None,
    )
    if active is None:
        raise KeyError(profile_name)
    settings.update(
        {
            **active,
            "active_llm_profile": active["profile_name"],
            "llm_profiles": profiles,
        }
    )
    save_settings(settings)
    return load_settings()


def delete_llm_profile(profile_name):
    settings = load_settings()
    profiles = [
        item
        for item in settings.get("llm_profiles", [])
        if item["profile_name"] != profile_name
    ]
    if not profiles:
        profiles = [default_llm_settings()]
    active_name = settings.get("active_llm_profile")
    if active_name == profile_name:
        active_name = profiles[0]["profile_name"]
    active = next(
        (item for item in profiles if item["profile_name"] == active_name),
        profiles[0],
    )
    settings.update(
        {
            **active,
            "active_llm_profile": active["profile_name"],
            "llm_profiles": profiles,
        }
    )
    save_settings(settings)
    return load_settings()
