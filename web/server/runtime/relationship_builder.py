from __future__ import annotations

from typing import Any


def build_relationships(graph: dict[str, Any]) -> list[dict[str, Any]]:
    entity_names = {
        item.get("entity_id"): item.get("canonical_name")
        for item in graph.get("entities", [])
    }
    entity_types = {
        item.get("entity_id"): item.get("entity_type")
        for item in graph.get("entities", [])
    }
    rows = graph.get("relationships", [])
    if rows:
        weak_types = {
            "CO_OCCURS_IN_SCENE",
            "MENTIONED_TOGETHER",
            "SHARES_LOCATION",
            "SAME_SCENE",
        }
        rows = [
            item
            for item in rows
            if item.get("relationship_type") not in weak_types
            and item.get("type") not in weak_types
            and item.get("relation_type") not in weak_types
        ]
    if not rows:
        rows = graph.get("relations", [])
    if not rows:
        rows = graph.get("relationship_facts", [])
    if not rows:
        rows = graph.get("identity_facts", [])
    result = []
    for item in rows:
        source_id = item.get("source_entity_id") or item.get("canonical_source_id")
        target_id = item.get("target_entity_id") or item.get("canonical_target_id")
        if source_id and target_id and source_id == target_id:
            continue
        if entity_types and (
            entity_types.get(source_id) != "Character"
            or entity_types.get(target_id) != "Character"
        ):
            continue
        evidence = item.get("evidence", [])
        first_evidence = evidence[0] if evidence else {}
        result.append(
            {
                "source_character": item.get("source_canonical_name")
                or item.get("source_name")
                or item.get("source")
                or entity_names.get(item.get("source_entity_id"))
                or first_evidence.get("source_surface_name"),
                "relationship_type": item.get("type")
                or item.get("relationship_type")
                or item.get("relation_type"),
                "target_character": item.get("target_canonical_name")
                or item.get("target_name")
                or item.get("target")
                or entity_names.get(item.get("target_entity_id"))
                or first_evidence.get("target_surface_name"),
                "description": item.get("relation_summary")
                or item.get("description")
                or first_evidence.get("relation_summary", ""),
                "confidence": item.get("confidence", ""),
                "source_event": item.get("source_event", ""),
                "source_text": item.get("source_text")
                or first_evidence.get("source_text", ""),
                "source_chunk_id": item.get("source_chunk_id")
                if item.get("source_chunk_id") is not None
                else first_evidence.get("source_chunk_id"),
                "canonical_source_id": item.get("source_entity_id")
                or item.get("canonical_source_id"),
                "canonical_target_id": item.get("target_entity_id")
                or item.get("canonical_target_id"),
            }
        )
    return result
