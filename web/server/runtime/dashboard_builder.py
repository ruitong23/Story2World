from __future__ import annotations

from typing import Any

from .relationship_builder import build_relationships


def build_dashboard(
    project_id: str,
    status: dict[str, Any],
    world: dict[str, Any],
    characters: dict[str, Any],
    agents: dict[str, Any],
    graph: dict[str, Any],
) -> dict[str, Any]:
    sections = world.get("world_sections", {})
    character_rows = characters.get("characters", [])
    agent_rows = agents.get("agents", [])
    timeline_count = len(
        world.get("canonical_timeline_db", {}).get("timeline_nodes", [])
    )
    scene_beat_count = len(
        world.get("canonical_scene_beat_db", {}).get("scene_beats", [])
    )
    main = sorted(
        character_rows,
        key=lambda item: (
            item.get("simulation_status") != "primary",
            item.get("canonical_name", ""),
        ),
    )[:12]
    return {
        "project_id": project_id,
        "status": status.get("status"),
        "characters_count": characters.get("character_count", len(character_rows)),
        "agents_count": agents.get("agent_count", len(agent_rows)),
        "locations_count": len(sections.get("locations", [])),
        "events_count": max(
            len(sections.get("events", [])),
            timeline_count,
            scene_beat_count,
        ),
        "relationships_count": len(build_relationships(graph)),
        "main_characters": [
            {
                "character_id": item.get("character_id"),
                "name": item.get("canonical_name"),
                "aliases": item.get("aliases", []),
                "description": item.get("background_summary", ""),
                "tier": item.get("build_policy", {}).get("profile_build_tier")
                or item.get("profile_tier"),
            }
            for item in main
        ],
        "current_world_progress": world.get("simulation_state_template")
        or world.get("simulation_state_db", {}),
        "available_features": ["chat", "relationships", "characters", "world"],
        "runtime_capabilities": {
            "source_preview": True,
            "db_anchor_preview": True,
            "recovery_snapshot": True,
            "rag_orchestration": True,
            "per_agent_sidecars": True,
            "scene_beats": bool(scene_beat_count),
        },
    }
