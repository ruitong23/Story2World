"""Shared file contracts and settings for the desktop applications."""

import json
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
GENERATED_DB_DIR = APP_DIR / "generated_db"
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
    ("generated_db/graph/novel_ontology.json", "Ontology used by graph extraction"),
    ("generated_db/graph/raw_graph_triples.json", "Validated graph triples for selected chunks"),
    ("generated_db/graph/mention_weak_relations.json", "Mention-level weak relation evidence"),
    ("generated_db/canonical/mention_alias_index.json", "Mention and alias index"),
    ("generated_db/canonical/canonical_entities.json", "Resolved canonical entities"),
    ("generated_db/graph/normalized_graph_triples.json", "Normalized graph"),
    ("generated_db/canonical/canonical_relationship_db.json", "Canonical relationship database"),
    ("generated_db/canonical/relationship_arc_db.json", "Relationship arc database"),
    ("generated_db/graph/structured_world_graph.json", "Structured world graph"),
    ("generated_db/canonical/character_state_db.json", "Character state database"),
    ("generated_db/canonical/world_db.json", "Canonical world aggregate"),
    ("generated_db/canonical/canonical_timeline_db.json", "Canonical baseline timeline"),
    ("generated_db/canonical/canonical_event_db.json", "Canonical event database"),
    ("generated_db/canonical/canonical_character_db.json", "Canonical character template database"),
    ("generated_db/canonical/canonical_ability_db.json", "Canonical ability database"),
    ("generated_db/canonical/canonical_item_db.json", "Canonical item database"),
    ("generated_db/canonical/canonical_organization_db.json", "Canonical organization database"),
    ("generated_db/canonical/canonical_location_db.json", "Canonical location database"),
    ("generated_db/canonical/canonical_world_rule_db.json", "Canonical world-rule database"),
    ("generated_db/canonical/canonical_knowledge_db.json", "Canonical knowledge database"),
    ("generated_db/runtime/simulation_state_template.json", "Cutoff-based initial state template"),
    ("generated_db/runtime/runtime_event_db.json", "Runtime event queue database"),
    ("generated_db/runtime/runtime_relationship_db.json", "Runtime relationship database"),
    ("generated_db/runtime/runtime_log.json", "Runtime event-sourcing log"),
    ("generated_db/agents/agent_profiles.json", "Canonical agent profile templates"),
    ("generated_db/agents/runtime_agent_state.json", "Runtime agent dynamic state"),
]

SIMULATION_REQUIRED_FILES = [
    ("generated_db/canonical/world_db.json", "Canonical world aggregate"),
    ("generated_db/canonical/canonical_timeline_db.json", "Canonical baseline timeline"),
    ("generated_db/canonical/canonical_event_db.json", "Canonical event database"),
    ("generated_db/runtime/simulation_state_template.json", "Cutoff-based initial state template"),
    ("generated_db/runtime/runtime_event_db.json", "Runtime event queue database"),
    ("generated_db/runtime/runtime_relationship_db.json", "Runtime relationship database"),
    ("generated_db/canonical/canonical_character_db.json", "Canonical character template database"),
    ("generated_db/agents/agent_profiles.json", "Canonical agent profile templates"),
    ("generated_db/agents/runtime_agent_state.json", "Runtime agent dynamic state"),
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
