"""Output folders for generated simulation databases."""

from __future__ import annotations

import shutil
from pathlib import Path


GENERATED_DB_DIR = Path("generated_db")
WORLD_DB_DIR = GENERATED_DB_DIR / "world"
CHARACTER_DB_DIR = GENERATED_DB_DIR / "characters"
AGENT_DB_DIR = GENERATED_DB_DIR / "agents"


WORLD_FILES = [
    "novel_ontology.json",
    "raw_graph_triples.json",
    "normalized_graph_triples.json",
    "structured_world_graph.json",
    "world_db.json",
    "canonical_novel_db.json",
    "simulation_state_db.json",
    "runtime_event_db.json",
    "mention_weak_relations.json",
    "canonical_relationships_db.json",
    "relationship_arc_db.json",
]

CHARACTER_FILES = [
    "mention_alias_index.json",
    "canonical_entities.json",
    "character_state_db.json",
]

AGENT_FILES = [
    "agent_profiles.json",
]


def generated_path(group, filename, base_dir=Path(".")):
    base_dir = Path(base_dir)
    if group == "world":
        return base_dir / WORLD_DB_DIR / filename
    if group == "characters":
        return base_dir / CHARACTER_DB_DIR / filename
    if group == "agents":
        return base_dir / AGENT_DB_DIR / filename
    raise ValueError(f"Unknown generated DB group: {group}")


def ensure_generated_dirs(base_dir=Path(".")):
    base_dir = Path(base_dir)
    for directory in (WORLD_DB_DIR, CHARACTER_DB_DIR, AGENT_DB_DIR):
        (base_dir / directory).mkdir(parents=True, exist_ok=True)


def publish_generated_databases(base_dir=Path(".")):
    base_dir = Path(base_dir)
    ensure_generated_dirs(base_dir)
    published = {}
    for group, filenames in (
        ("world", WORLD_FILES),
        ("characters", CHARACTER_FILES),
        ("agents", AGENT_FILES),
    ):
        for filename in filenames:
            source = base_dir / filename
            if not source.is_file():
                continue
            target = generated_path(group, filename, base_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            published[filename] = str(target)
    return published
