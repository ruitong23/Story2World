"""Shared file contracts and settings for the desktop applications."""

import json
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
GENERATED_DB_DIR = APP_DIR / "generated_db"
WORLD_DB_DIR = GENERATED_DB_DIR / "world"
CHARACTER_DB_DIR = GENERATED_DB_DIR / "characters"
AGENT_DB_DIR = GENERATED_DB_DIR / "agents"


def generated_db_path(group, filename):
    if group == "world":
        return WORLD_DB_DIR / filename
    if group == "characters":
        return CHARACTER_DB_DIR / filename
    if group == "agents":
        return AGENT_DB_DIR / filename
    raise ValueError(f"Unknown generated DB group: {group}")

PREPARATION_OUTPUTS = [
    ("generated_db/world/novel_ontology.json", "Ontology used by graph extraction"),
    ("generated_db/world/raw_graph_triples.json", "Validated graph triples for selected chunks"),
    ("generated_db/world/mention_weak_relations.json", "Mention-level weak relation evidence"),
    ("generated_db/characters/mention_alias_index.json", "Mention and alias index"),
    ("generated_db/characters/canonical_entities.json", "Resolved canonical entities"),
    ("generated_db/world/normalized_graph_triples.json", "Normalized graph"),
    ("generated_db/world/canonical_relationships_db.json", "Canonical relationship database"),
    ("generated_db/world/relationship_arc_db.json", "Relationship arc database"),
    ("generated_db/world/structured_world_graph.json", "Structured world graph"),
    ("generated_db/characters/character_state_db.json", "Character state database"),
    ("generated_db/world/world_db.json", "Simulation world database"),
    ("generated_db/world/canonical_novel_db.json", "Canonical novel trajectory database"),
    ("generated_db/world/simulation_state_db.json", "Cutoff-based simulation state database"),
    ("generated_db/world/runtime_event_db.json", "Runtime event queue database"),
    ("generated_db/agents/agent_profiles.json", "Runtime agent profiles"),
]

SIMULATION_REQUIRED_FILES = [
    ("generated_db/world/world_db.json", "Simulation world database"),
    ("generated_db/world/canonical_novel_db.json", "Canonical novel trajectory database"),
    ("generated_db/world/simulation_state_db.json", "Cutoff-based simulation state database"),
    ("generated_db/world/runtime_event_db.json", "Runtime event queue database"),
    ("generated_db/world/canonical_relationships_db.json", "Canonical relationship database"),
    ("generated_db/world/relationship_arc_db.json", "Relationship arc database"),
    ("generated_db/characters/character_state_db.json", "Character state database"),
    ("generated_db/agents/agent_profiles.json", "Runtime agent profiles"),
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


def load_settings():
    defaults = {
        "llm_base_url": "http://localhost:1234/v1",
        "llm_model": "gemma-4-26b-a4b-it",
        "llm_api_key": "lm-studio",
    }
    if not SETTINGS_PATH.is_file():
        return defaults
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    return {**defaults, **saved}


def save_settings(settings):
    temporary = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(SETTINGS_PATH)
