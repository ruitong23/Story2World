from __future__ import annotations

import json
import sys
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from ..config import DESKTOP_APP_DIR, get_llm_settings
from ..project_store import load_output, normalize_id
from .relationship_builder import build_relationships


def _make_llm_callable():
    def call(
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.4,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        llm_settings = get_llm_settings()
        if str(DESKTOP_APP_DIR) not in sys.path:
            sys.path.insert(0, str(DESKTOP_APP_DIR))
        from llm_api import chat_completion

        return chat_completion(
            base_url=llm_settings["llm_base_url"],
            api_key=llm_settings["llm_api_key"],
            model=llm_settings["llm_model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            source="web_api",
            flow="chat_and_preview",
            response_format=response_format,
            timeout=600,
        )

    return call


def _deduplicate_narration(text: str) -> str:
    paragraphs = [
        item.strip() for item in str(text or "").split("\n\n") if item.strip()
    ]
    kept = []
    for paragraph in paragraphs:
        normalized = " ".join(paragraph.split())
        duplicate = False
        if len(normalized) >= 80:
            for existing in kept:
                existing_normalized = " ".join(existing.split())
                if len(existing_normalized) < 80:
                    continue
                if SequenceMatcher(
                    None,
                    normalized,
                    existing_normalized,
                    autojunk=False,
                ).ratio() >= 0.92:
                    duplicate = True
                    break
        if not duplicate:
            kept.append(paragraph)
    return "\n\n".join(kept)


def _collect_evidence_refs(value: Any, result: list[Any] | None = None) -> list[Any]:
    result = result if result is not None else []
    if isinstance(value, dict):
        refs = value.get("evidence_refs")
        if isinstance(refs, list):
            result.extend(item for item in refs if item)
        for child in value.values():
            _collect_evidence_refs(child, result)
    elif isinstance(value, list):
        for child in value:
            _collect_evidence_refs(child, result)
    unique = []
    seen = set()
    for item in result:
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    return unique


def _validation_constraints(result: dict[str, Any]) -> list[dict[str, Any]]:
    constraints = []
    validation = result.get("internal_validation", {})
    checks = []
    for proposal in validation.get("proposal_validations", []):
        checks.extend(proposal.get("checks", []))
    checks.extend(validation.get("event_validation", {}).get("checks", []))
    for check in checks:
        if check.get("outcome") == "allowed":
            continue
        constraints.append(
            {
                "category": check.get("category"),
                "outcome": check.get("outcome"),
                "reason": check.get("internal_reason", ""),
                "evidence_refs": check.get("evidence_refs", []),
            }
        )
    return constraints


def _load_session_runtime(
    project_path: Path,
    session_id: str,
):
    session_id = normalize_id(session_id, "session_id", 80)
    if str(DESKTOP_APP_DIR) not in sys.path:
        sys.path.insert(0, str(DESKTOP_APP_DIR))
    from step17_runtime import load_step17_runtime

    def output_path(group: str, filename: str) -> Path:
        for candidate in (
            project_path / "generated_db" / group / filename,
            project_path / "db" / group / filename,
            project_path / filename,
        ):
            if candidate.is_file():
                return candidate
        raise FileNotFoundError(f"处理结果缺少 {filename}。")

    world_path = output_path("canonical", "world_db.json")
    try:
        character_path = output_path("canonical", "canonical_character_db.json")
    except FileNotFoundError:
        character_path = output_path("canonical", "character_state_db.json")
    agent_path = output_path("agents", "agent_profiles.json")
    session_dir = project_path / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    runtime = load_step17_runtime(
        world_path=world_path,
        character_path=character_path,
        agent_path=agent_path,
        state_path=session_dir / "runtime" / "simulation_state.json",
        llm_callable=_make_llm_callable(),
    )
    return session_dir, runtime


def _load_character_db(project_path: Path) -> dict[str, Any]:
    try:
        return load_output(project_path, "canonical", "canonical_character_db.json")
    except FileNotFoundError:
        return load_output(project_path, "canonical", "character_state_db.json")


def _story_progress(store: Any) -> dict[str, Any]:
    runtime = store.runtime
    timeline = runtime.get("canonical_timeline") or []
    total = len(timeline)
    cursor = int(runtime.get("timeline_cursor", 0) or 0)
    cursor = max(0, min(cursor, total))
    reached = min(total, cursor + 1) if total and runtime.get("active_scene") else cursor
    return {
        "timeline_cursor": cursor,
        "canonical_reached_events": reached,
        "canonical_total_events": total,
        "canonical_percent": reached * 100 / total if total else 0.0,
        "runtime_event_count": len(store.branch.get("events", [])),
        "sidecar_committed_event_count": len(
            runtime.get("runtime_event_db", {}).get("runtime_committed_events", [])
        ),
    }


def _character_name(orchestrator: Any, character_id: str) -> str:
    try:
        profile = orchestrator._dynamic_profile(character_id)
        if profile.get("canonical_name"):
            return profile["canonical_name"]
    except Exception:
        pass
    character = getattr(orchestrator, "character_by_id", {}).get(character_id, {})
    if character.get("canonical_name"):
        return character["canonical_name"]
    profile = getattr(orchestrator, "agent_by_character_id", {}).get(character_id, {})
    return profile.get("canonical_name") or character_id


def _agent_trace(store: Any, orchestrator: Any, result: dict[str, Any] | None = None) -> dict[str, Any]:
    result = result or {}
    event = result.get("event", {}) if isinstance(result, dict) else {}
    if not event:
        for candidate in reversed(store.branch.get("events", [])):
            if candidate.get("narration") and candidate.get("event_type") in {
                "scene_opening_rendered",
                "immersive_scene_turn",
            }:
                event = candidate
                break
    pipeline = result.get("pipeline", {}) if isinstance(result, dict) else {}
    scene = dict(store.runtime.get("active_scene") or {})
    focus_id = scene.get("focus_character_id")
    catalog_by_id = {
        item.get("character_id"): item
        for item in orchestrator.agent_catalog()
        if item.get("character_id")
    }
    active_agent_ids = {
        str(item.get("character_id") or "").strip()
        for item in pipeline.get("nearby_npc_agents", [])
        if isinstance(item, dict) and str(item.get("character_id") or "").strip()
    }
    if not active_agent_ids and event:
        active_agent_ids = {
            str(item.get("character_id") or "").strip()
            for item in event.get("npc_agent_outputs", [])
            if isinstance(item, dict) and str(item.get("character_id") or "").strip()
        }
    controlled_agents = []
    passive_characters = []
    for character_id in scene.get("participant_ids", []):
        catalog = catalog_by_id.get(character_id, {})
        tier = catalog.get("tier", "reference")
        try:
            profile = orchestrator._dynamic_profile(character_id)
        except Exception:
            profile = {}
        runtime_mode = catalog.get("runtime_mode") or profile.get("runtime_mode") or ""
        control = (
            "MANUAL"
            if character_id == focus_id
            else store.runtime.get("agent_control", {}).get(character_id, "AUTO")
        )
        is_agent_controlled = bool(
            character_id == focus_id
            or character_id in active_agent_ids
            or (
                control == "AUTO"
                and tier in {"full", "light", "runtime"}
                and "reference" not in runtime_mode
            )
        )
        row = {
            "character_id": character_id,
            "name": _character_name(orchestrator, character_id),
            "tier": tier,
            "control": control,
            "runtime_mode": runtime_mode,
            "agent_awake": character_id in active_agent_ids,
        }
        if is_agent_controlled:
            controlled_agents.append(row)
        else:
            passive_characters.append(row)

    contributors = []
    seen_contributors = set()

    def add_contributor(name: Any, kind: str, status: Any = "") -> None:
        name = str(name or "").strip()
        kind = str(kind or "").strip()
        status = str(status or "").strip()
        if not name:
            return
        marker = (name, kind, status)
        if marker in seen_contributors:
            return
        seen_contributors.add(marker)
        contributors.append({"name": name, "kind": kind, "status": status})

    if pipeline.get("player_controller"):
        add_contributor("Player Controller", "角色控制", "ran")
    if pipeline.get("time_agent"):
        source = pipeline.get("time_agent", {}).get("source", "")
        add_contributor(
            "Time Service" if source else "Time Agent",
            "时间",
            "deterministic" if source else "ran",
        )
    rules = pipeline.get("rules_agent", {})
    if rules:
        add_contributor("Rule Checker", "规则", f"{rules.get('validation_count', 0)} checks")
    for item in pipeline.get("nearby_npc_agents", []):
        if not isinstance(item, dict):
            continue
        add_contributor(
            item.get("canonical_name") or item.get("character_id"),
            "Character Agent",
            item.get("visible_behavior")
            or item.get("action_intent", {}).get("description", "")
            or "proposal",
        )
    group_controller = pipeline.get("group_controller", {})
    if pipeline.get("group_controller_ran") or (
        isinstance(group_controller, dict) and group_controller.get("ran")
    ):
        labels = []
        if isinstance(group_controller, dict):
            labels = [
                str(item.get("label") or "").strip()
                for item in group_controller.get("groups", [])
                if isinstance(item, dict) and str(item.get("label") or "").strip()
            ]
        add_contributor(
            "Group Controller",
            "群体",
            "、".join(labels[:2]) if labels else "ran",
        )
    local_world = pipeline.get("local_world_agent", {})
    if pipeline.get("local_world_agent_ran") or local_world:
        ambient_count = (
            len(local_world.get("ambient_npc_reactions", []))
            if isinstance(local_world, dict)
            else 0
        )
        event_count = (
            len(local_world.get("new_events", []))
            if isinstance(local_world, dict)
            else 0
        )
        status_parts = []
        if isinstance(local_world, dict) and local_world.get("forced_progress"):
            status_parts.append("forced reveal")
        if ambient_count:
            status_parts.append(f"ambient NPC x{ambient_count}")
        if event_count:
            status_parts.append(f"events x{event_count}")
        add_contributor(
            "Local World Agent",
            "局部世界",
            " / ".join(status_parts) if status_parts else "ran",
        )
    if pipeline.get("gm_resolver_ran") or pipeline.get("gm_resolver"):
        add_contributor(
            "GM Resolver",
            "裁决",
            pipeline.get("gm_resolver", {}).get("outcome", "ran"),
        )
    if pipeline.get("global_world_agent_ran"):
        add_contributor("Global World Agent", "大世界", "ran")
    if pipeline.get("memory_agent"):
        memory = pipeline.get("memory_agent", {})
        add_contributor(
            "Memory Agent",
            "记忆",
            "compacted" if memory.get("summary_compaction_ran") else "recorded",
        )
    if pipeline.get("scene_renderer"):
        add_contributor("Scene Renderer", "叙事", "rendered")

    if not pipeline and event:
        if event.get("player_intent"):
            add_contributor("Player Controller", "角色控制", "committed")
        if event.get("elapsed_minutes") is not None:
            add_contributor("Time Service", "时间", f"{event.get('elapsed_minutes', 0)} min")
        validation_summary = event.get("validation_summary", {})
        if validation_summary:
            add_contributor("Rule Checker", "规则", validation_summary.get("status", "checked"))
        for item in event.get("npc_agent_outputs", []):
            if not isinstance(item, dict):
                continue
            add_contributor(
                item.get("canonical_name") or item.get("character_id"),
                "Character Agent",
                item.get("visible_behavior")
                or item.get("action_intent", {}).get("description", "")
                or "proposal",
            )
        local_world = event.get("local_world", {})
        group_controller = (
            local_world.get("group_controller", {})
            if isinstance(local_world, dict)
            else {}
        )
        if isinstance(group_controller, dict) and group_controller.get("ran"):
            labels = [
                str(item.get("label") or "").strip()
                for item in group_controller.get("groups", [])
                if isinstance(item, dict) and str(item.get("label") or "").strip()
            ]
            add_contributor(
                "Group Controller",
                "群体",
                "、".join(labels[:2]) if labels else "committed",
            )
        if isinstance(local_world, dict) and (
            local_world.get("world_changes")
            or local_world.get("new_events")
            or local_world.get("ambient_npc_reactions")
            or local_world.get("npc_position_updates")
            or event.get("event_type") == "immersive_scene_turn"
        ):
            ambient_count = len(local_world.get("ambient_npc_reactions", []))
            event_count = len(local_world.get("new_events", []))
            status_parts = []
            if local_world.get("forced_progress"):
                status_parts.append("forced reveal")
            if ambient_count:
                status_parts.append(f"ambient NPC x{ambient_count}")
            if event_count:
                status_parts.append(f"events x{event_count}")
            add_contributor(
                "Local World Agent",
                "局部世界",
                " / ".join(status_parts) if status_parts else "committed",
            )
        gm = event.get("gm_resolution", {})
        if isinstance(gm, dict) and gm:
            add_contributor("GM Resolver", "裁决", gm.get("outcome", "committed"))
        if event.get("world_projection"):
            add_contributor("Global World Agent", "大世界", "committed")
        if event.get("event_type") == "immersive_scene_turn":
            add_contributor("Memory Agent", "记忆", "recorded")
            add_contributor("Scene Renderer", "叙事", "rendered")
        elif event.get("event_type") == "scene_opening_rendered":
            add_contributor("Scene Renderer", "叙事", "opening rendered")

    return {
        "active_controlled_agents": controlled_agents,
        "active_passive_characters": passive_characters,
        "active_full_agents": controlled_agents,
        "active_other_agents": passive_characters,
        "turn_contributors": contributors,
        "scene": scene,
        "event_id": event.get("event_id", ""),
        "revision": event.get("revision_after", store.branch.get("head_revision", 0)),
        "pipeline_summary": {
            "local_world_agent_ran": bool(pipeline.get("local_world_agent_ran")),
            "gm_resolver_ran": bool(pipeline.get("gm_resolver_ran")),
            "global_world_agent_ran": bool(pipeline.get("global_world_agent_ran")),
            "nearby_npc_agent_count": len(pipeline.get("nearby_npc_agents", [])),
            "group_controller_ran": bool(pipeline.get("group_controller_ran")),
        },
    }


def _progress_event(value: int, label: str) -> dict[str, Any]:
    label = str(label or "正在处理")
    actor = "世界服务"
    detail = "正在执行本轮模拟的实际后端阶段"
    if "时间服务" in label or "时间 Agent" in label:
        actor = "时间服务"
        detail = "确定性估算行动耗时并推进世界时钟"
    elif "Group Controller" in label or "群体" in label:
        actor = "Group Controller"
        detail = "按常识统一模拟村民、士兵、群众等群体反应"
    elif "局部世界" in label:
        actor = "局部世界 Agent"
        detail = "更新附近环境、物品与局部事件"
    elif "GM" in label:
        actor = "GM Resolver"
        detail = "依据人物能力、世界规则与当前状态裁定结果"
    elif "Renderer" in label or "写作" in label:
        actor = "Scene Renderer"
        detail = "将人物行动、环境变化与裁定结果组织成场景"
    elif "保存" in label or "同步" in label or "存档" in label:
        actor = "存档系统"
        detail = "同步模拟状态、Agent 记忆与运行时数据库"
    elif "附近" in label:
        actor = "附近角色"
        detail = "在场角色正在独立观察并决定自己的行动"
    elif "正在观察并行动" in label:
        actor = label.split(" 正在", 1)[0]
        detail = "该角色正在依据自己的记忆、目标和当前所见作出反应"
    elif "角色" in label:
        actor = "玩家角色"
        detail = "结合身份、记忆与当前场景理解玩家行动"
    return {
        "progress": max(0, min(100, int(value))),
        "label": label,
        "actor": actor,
        "action": label,
        "detail": detail,
        "status": "running" if int(value) < 100 else "completed",
    }


def run_chat(
    project_path: Path,
    session_id: str,
    character_id: str,
    message: str,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    session_id = normalize_id(session_id, "session_id", 80)
    character_id = normalize_id(character_id, "character_id", 160)
    _session_dir, runtime = _load_session_runtime(project_path, session_id)
    orchestrator = runtime["orchestrator"]
    store = runtime["store"]
    def report(value: int, label: str) -> None:
        if progress_callback:
            progress_callback(_progress_event(value, label))

    active = store.runtime.get("active_scene") or {}
    if not active:
        opening_span = 35 if message.strip() else 100
        def report_opening(value: int, label: str) -> None:
            report(round(int(value) * opening_span / 100), label)

        result = orchestrator.start_character_experience(
            character_id, progress_callback=report_opening
        )
        reply = result.get("event", {}).get("narration", "")
        if message.strip():
            def report_turn(value: int, label: str) -> None:
                report(35 + round(int(value) * 65 / 100), label)

            result = orchestrator.run_turn(
                message.strip(), progress_callback=report_turn
            )
            reply = result.get("event", {}).get("narration", reply)
    else:
        active_character = active.get("focus_character_id")
        if active_character != character_id:
            raise ValueError("这个会话已经选择了另一个角色，请使用新的 session_id。")
        result = orchestrator.run_turn(
            message.strip() or "观察并让局势自然推进",
            progress_callback=report,
        )
        reply = result.get("event", {}).get("narration", "")
    reply = _deduplicate_narration(reply)
    event = result.get("event", {})
    used_sources = _collect_evidence_refs(result)
    used_fallback_sources = False
    if not used_sources:
        used_fallback_sources = True
        character_db = _load_character_db(project_path)
        character = character_db.get("character_by_id", {}).get(character_id)
        if not character:
            character = next(
                (
                    item
                    for item in character_db.get("characters", [])
                    if item.get("character_id") == character_id
                ),
                {},
            )
        candidates = [
            {
                "source_type": "character_profile_context",
                "source_chunk_id": item.get("source_chunk_id"),
                "source_text": item.get("source_text", ""),
            }
            for item in character.get("evidence", [])[:8]
            if item.get("source_text")
        ]
        for field in ("all_relations", "abilities", "owned_items", "used_items"):
            for record in character.get(field, []):
                for evidence in record.get("evidence", []):
                    if evidence.get("source_text"):
                        candidates.append(
                            {
                                "source_type": f"character_{field}",
                                "source_chunk_id": evidence.get("source_chunk_id"),
                                "source_text": evidence.get("source_text", ""),
                            }
                        )
        used_sources = []
        seen_sources = set()
        for source in candidates:
            marker = (
                source.get("source_chunk_id"),
                source.get("source_text"),
            )
            if marker in seen_sources:
                continue
            seen_sources.add(marker)
            used_sources.append(source)
            if len(used_sources) >= 12:
                break
    relationships = [
        item
        for item in build_relationships(
            load_output(project_path, "graph", "structured_world_graph.json")
        )
        if character_id
        in {
            item.get("canonical_source_id"),
            item.get("canonical_target_id"),
        }
    ]
    constraints = _validation_constraints(result)
    if used_fallback_sources:
        constraints.append(
            {
                "category": "grounding",
                "outcome": "uncertain",
                "reason": (
                    "Step 17 未返回本轮逐条 evidence_refs；used_sources "
                    "为角色档案与关系证据的回退上下文，回答中仍可能含模型补全内容。"
                ),
                "evidence_refs": used_sources,
            }
        )
    character_state = dict(
        store.runtime.get("character_runtime", {}).get(character_id, {})
    )
    character_state.setdefault("health", {
        "current": 100,
        "maximum": 100,
        "status": "状态良好",
    })
    scene_state = dict(store.runtime.get("active_scene") or {})
    pipeline = result.get("pipeline", {})
    agent_activity = [
        {
            "agent": item.get("canonical_name", ""),
            "activity": item.get("visible_behavior")
            or item.get("action_intent", {}).get("description", ""),
            "emotion": item.get("emotion", ""),
            "status": "completed",
        }
        for item in pipeline.get("nearby_npc_agents", [])
        if isinstance(item, dict)
    ]
    group_controller = pipeline.get("group_controller", {})
    if isinstance(group_controller, dict) and group_controller.get("ran"):
        for item in group_controller.get("groups", [])[:4]:
            if isinstance(item, dict):
                agent_activity.append({
                    "agent": item.get("label") or "周围人群",
                    "activity": item.get("visible_behavior")
                    or item.get("pressure")
                    or "群体作出反应",
                    "emotion": item.get("mood", ""),
                    "status": "completed",
                })
    local_world = pipeline.get("local_world_agent", {})
    if isinstance(local_world, dict):
        for item in local_world.get("ambient_npc_reactions", [])[:5]:
            if isinstance(item, dict):
                agent_activity.append({
                    "agent": item.get("speaker_label") or "附近路人",
                    "activity": item.get("visible_behavior")
                    or item.get("dialogue")
                    or "作出普通环境反应",
                    "emotion": "",
                    "status": "completed",
                })
        for item in local_world.get("new_events", [])[:5]:
            if isinstance(item, dict):
                agent_activity.append({
                    "agent": "世界",
                    "activity": item.get("narration")
                    or item.get("event_type", "环境发生变化"),
                    "emotion": "",
                    "status": "completed",
                })
            elif str(item or "").strip():
                agent_activity.append({
                    "agent": "世界",
                    "activity": str(item).strip(),
                    "emotion": "",
                    "status": "completed",
                })
    return {
        "session_id": session_id,
        "character_id": character_id,
        "reply": reply,
        "used_sources": used_sources,
        "world_constraints": constraints,
        "related_relationships": relationships,
        "state_delta": {
            "state_changes": event.get("state_changes", []),
            "elapsed_minutes": event.get("elapsed_minutes", 0),
            "state_revision": result.get("state_revision"),
        },
        "character_state": character_state,
        "scene_state": scene_state,
        "agent_activity": agent_activity,
        "agent_trace": _agent_trace(store, orchestrator, result),
        "recovery_snapshot": dict(store.runtime.get("recovery_snapshot") or {}),
        "rag_orchestration_summary": (
            event.get("rag_orchestration_summary")
            or pipeline.get("rag_orchestration_summary")
            or {}
        ),
        "story_progress": _story_progress(store),
    }


def get_chat_session(
    project_path: Path,
    session_id: str,
    character_id: str,
) -> dict[str, Any]:
    character_id = normalize_id(character_id, "character_id", 160)
    _session_dir, runtime = _load_session_runtime(project_path, session_id)
    store = runtime["store"]
    scene = dict(store.runtime.get("active_scene") or {})
    active_character = scene.get("focus_character_id")
    if active_character and active_character != character_id:
        raise ValueError("这个会话已经选择了另一个角色。")
    return {
        "session_id": session_id,
        "character_id": character_id,
        "has_session": bool(active_character),
        "recovery_snapshot": dict(
            store.runtime.get("recovery_snapshot") or {}
        ),
        "scene_state": scene,
        "character_state": dict(
            store.runtime.get("character_runtime", {}).get(
                character_id, {}
            )
        ),
        "agent_trace": _agent_trace(store, runtime["orchestrator"]),
        "state_revision": store.branch.get("head_revision", 0),
        "story_progress": _story_progress(store),
    }


def save_chat_session(
    project_path: Path,
    session_id: str,
    character_id: str,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    character_id = normalize_id(character_id, "character_id", 160)
    _session_dir, runtime = _load_session_runtime(project_path, session_id)
    store = runtime["store"]
    scene = store.runtime.get("active_scene") or {}
    if not scene:
        raise ValueError("当前会话还没有可保存的场景。")
    if scene.get("focus_character_id") != character_id:
        raise ValueError("这个会话已经选择了另一个角色。")

    def report(value: int, label: str) -> None:
        if progress_callback:
            progress_callback(_progress_event(value, label))

    snapshot = runtime["orchestrator"].create_manual_save(
        progress_callback=report
    )
    return {
        "session_id": session_id,
        "character_id": character_id,
        "saved": True,
        "recovery_snapshot": snapshot,
        "state_revision": store.branch.get("head_revision", 0),
    }


def get_world_admin_snapshot(
    project_path: Path,
    session_id: str,
    character_id: str | None = None,
) -> dict[str, Any]:
    session_id = normalize_id(session_id, "session_id", 80)
    normalized_character_id = (
        normalize_id(character_id, "character_id", 160)
        if character_id
        else ""
    )
    _session_dir, runtime = _load_session_runtime(project_path, session_id)
    store = runtime["store"]
    scene = store.runtime.get("active_scene") or {}
    active_character = scene.get("focus_character_id")
    if normalized_character_id and active_character and active_character != normalized_character_id:
        raise ValueError("这个会话已经选择了另一个角色。")
    snapshot = runtime["orchestrator"].world_admin_snapshot()
    return {
        "session_id": session_id,
        "character_id": normalized_character_id or active_character or "",
        "snapshot": snapshot,
        "state_revision": store.branch.get("head_revision", 0),
    }


def run_world_admin_chat(
    project_path: Path,
    session_id: str,
    message: str,
    character_id: str | None = None,
    apply_changes: bool = True,
) -> dict[str, Any]:
    session_id = normalize_id(session_id, "session_id", 80)
    normalized_character_id = (
        normalize_id(character_id, "character_id", 160)
        if character_id
        else ""
    )
    _session_dir, runtime = _load_session_runtime(project_path, session_id)
    store = runtime["store"]
    scene = store.runtime.get("active_scene") or {}
    active_character = scene.get("focus_character_id")
    if normalized_character_id and active_character and active_character != normalized_character_id:
        raise ValueError("这个会话已经选择了另一个角色。")
    result = runtime["orchestrator"].world_admin_chat(
        message,
        apply_changes=apply_changes,
    )
    return {
        "session_id": session_id,
        "character_id": normalized_character_id or active_character or "",
        "reply": result.get("reply", ""),
        "applied": result.get("applied", False),
        "event_id": result.get("event_id", ""),
        "state_revision": store.branch.get("head_revision", 0),
        "result": result,
        "snapshot": result.get("snapshot_after") or result.get("snapshot") or {},
    }
