from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..project_store import load_output, project_dir, read_status
from ..runtime.dashboard_builder import build_dashboard
from ..runtime.relationship_builder import build_relationships


router = APIRouter()


def _ready_project(username: str, project_id: str):
    path = project_dir(username, project_id)
    status = read_status(path)
    if status.get("status") != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"项目尚未完成，当前状态：{status.get('status')}",
        )
    return path, status


def _load_character_db(path):
    try:
        return load_output(path, "canonical", "canonical_character_db.json")
    except FileNotFoundError:
        return load_output(path, "canonical", "character_state_db.json")


def _load_relationship_source(path):
    for group, filename in (
        ("canonical", "canonical_relationship_db.json"),
        ("canonical", "canonical_relationships_db.json"),
        ("graph", "structured_world_graph.json"),
    ):
        try:
            return load_output(path, group, filename)
        except FileNotFoundError:
            continue
    raise FileNotFoundError("处理结果缺少关系数据库。")


def _load_world_db(path):
    world = load_output(path, "canonical", "world_db.json")
    if "canonical_scene_beat_db" not in world:
        try:
            world["canonical_scene_beat_db"] = load_output(
                path, "canonical", "canonical_scene_beat_db.json"
            )
        except FileNotFoundError:
            pass
    return world


@router.get("/projects/{project_id}/dashboard")
def dashboard(project_id: str, username: str = Query(...)):
    try:
        path, status = _ready_project(username, project_id)
        return build_dashboard(
            project_id,
            status,
            _load_world_db(path),
            _load_character_db(path),
            load_output(path, "agents", "agent_profiles.json"),
            _load_relationship_source(path),
        )
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/projects/{project_id}/characters")
def characters(project_id: str, username: str = Query(...)):
    try:
        path, _ = _ready_project(username, project_id)
        db = _load_character_db(path)
        agents = load_output(path, "agents", "agent_profiles.json")
        graph = _load_relationship_source(path)
        agent_ids = {item.get("character_id") for item in agents.get("agents", [])}
        relationship_counts = {}
        for relation in build_relationships(graph):
            for key in ("canonical_source_id", "canonical_target_id"):
                character_id = relation.get(key)
                if character_id:
                    relationship_counts[character_id] = (
                        relationship_counts.get(character_id, 0) + 1
                    )
        rows = []
        for item in db.get("characters", []):
            abilities = item.get("abilities", [])
            owned_items = item.get("owned_items", [])
            used_items = item.get("used_items", [])
            item_ids = {
                value.get("entity_id")
                for value in [
                    *owned_items,
                    *used_items,
                ]
                if value.get("entity_id")
            }
            rows.append(
                {
                    "character_id": item.get("character_id"),
                    "name": item.get("canonical_name"),
                    "aliases": item.get("aliases", []),
                    "titles": item.get("titles", []),
                    "short_description": item.get("background_summary", ""),
                    "available_as_agent": bool(item.get("character_id")),
                    "has_prebuilt_agent_profile": item.get("character_id") in agent_ids,
                    "relationship_count": relationship_counts.get(
                        item.get("character_id"), 0
                    ),
                    "ability_count": len(abilities),
                    "item_count": len(item_ids),
                    "abilities": [
                        {
                            "entity_id": value.get("entity_id"),
                            "name": value.get("name"),
                            "relation_type": value.get("relation_type"),
                        }
                        for value in abilities
                    ],
                    "items": [
                        {
                            "entity_id": value.get("entity_id"),
                            "name": value.get("name"),
                            "relation_type": value.get("relation_type"),
                        }
                        for value in [*owned_items, *used_items]
                    ],
                    "tier": item.get("build_policy", {}).get(
                        "profile_build_tier"
                    )
                    or item.get("profile_tier"),
                }
            )
        tier_weight = {"full": 4, "light": 3, "reference": 2, None: 0}
        rows.sort(
            key=lambda item: (
                -(
                    tier_weight.get(item.get("tier"), 1) * 1000
                    + int(item.get("relationship_count") or 0) * 20
                    + int(item.get("ability_count") or 0) * 15
                    + int(item.get("item_count") or 0) * 10
                    + len(item.get("aliases") or [])
                    + len(item.get("titles") or [])
                ),
                str(item.get("name") or "").casefold(),
            )
        )
        return {"project_id": project_id, "characters": rows}
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/projects/{project_id}/relationships")
def relationships(project_id: str, username: str = Query(...)):
    try:
        path, _ = _ready_project(username, project_id)
        rows = build_relationships(
            _load_relationship_source(path)
        )
        return {"project_id": project_id, "relationships": rows}
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/projects/{project_id}/world")
def world(project_id: str, username: str = Query(...)):
    try:
        path, _ = _ready_project(username, project_id)
        return _load_world_db(path)
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
