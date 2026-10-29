from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..project_store import load_output


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", "\n").split())


def source_excerpt(text: str, percent: float, window: int = 3000) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        raise ValueError("Novel text is empty.")
    percent = max(1.0, min(100.0, float(percent or 1.0)))
    anchor = int(len(text) * percent / 100.0)
    half = max(300, window // 2)
    start = max(0, anchor - half)
    end = min(len(text), start + window)
    if end - start < window:
        start = max(0, end - window)
    return {
        "excerpt": text[start:end],
        "start": start,
        "end": end,
        "anchor": anchor,
        "total": len(text),
        "percent": percent,
    }


def summarize_source_excerpt(
    llm: Callable[..., str],
    excerpt_info: dict[str, Any],
    chunk_size: int,
    overlap: int,
    selected_chunks: int | None = None,
) -> str:
    system = (
        "你是给用户看的小说剧情预览助手，不是技术顾问。只根据用户给出的"
        "原文片段总结当前大概剧情位置，不使用外部知识，不剧透片段之外的"
        "内容。禁止写技术方案、代码、schema、pipeline、API、字段或表格。"
    )
    user = (
        f"所选进度：{excerpt_info['percent']:.1f}%\n"
        f"预览范围：全文字符 {excerpt_info['start']} 到 "
        f"{excerpt_info['end']}，总长 {excerpt_info['total']}\n"
        f"chunk_size={chunk_size}，overlap={overlap}，"
        f"selected_chunks={selected_chunks or '未指定'}\n\n"
        "请用简体中文输出：\n"
        "1. 一句话说明当前大概到了什么剧情段落。\n"
        "2. 列出主要人物、地点、冲突或任务。\n"
        "3. 说明如果从这里开始抽取 DB，用户大概会进入什么故事局面。\n"
        "4. 最后提醒：这只是所选位置附近约3000字的局部预览。\n\n"
        "原文片段：\n"
        f"{excerpt_info['excerpt']}"
    )
    return str(llm(system, user, temperature=0.2, max_tokens=900)).strip()


def _source_orders(record: dict[str, Any]) -> set[int]:
    values: set[int] = set()
    for value in (
        record.get("first_seen_order"),
        *record.get("source_chunk_ids", []),
    ):
        try:
            values.add(int(value))
        except (TypeError, ValueError):
            pass
    for evidence in [
        *record.get("evidence", []),
        *record.get("evidence_refs", []),
    ]:
        try:
            values.add(int(evidence.get("source_chunk_id")))
        except (AttributeError, TypeError, ValueError):
            pass
    return values


def _raw_chunk_contexts(
    project_path: Path,
    names: set[str],
    source_orders: set[int],
) -> list[dict[str, Any]]:
    try:
        graph = load_output(project_path, "graph", "raw_graph_triples.json")
    except FileNotFoundError:
        return []
    target_orders = set(source_orders)
    if not target_orders:
        return []
    contexts = []
    for chunk in graph.get("results", []):
        try:
            chunk_order = int(chunk.get("chunk_id"))
        except (TypeError, ValueError):
            chunk_order = None
        text = json.dumps(chunk, ensure_ascii=False)
        if chunk_order not in target_orders and not any(name in text for name in names):
            continue
        nodes = [
            node
            for node in chunk.get("nodes", [])
            if chunk_order in source_orders
            or any(name in json.dumps(node, ensure_ascii=False) for name in names)
        ][:16]
        edges = [
            edge
            for edge in chunk.get("edges", [])
            if chunk_order in source_orders
            or any(name in json.dumps(edge, ensure_ascii=False) for name in names)
        ][:24]
        if nodes or edges:
            contexts.append(
                {
                    "chunk_id": chunk.get("chunk_id"),
                    "chunk_index": chunk.get("chunk_index"),
                    "nodes": nodes,
                    "edges": edges,
                }
            )
        if len(contexts) >= 6:
            break
    return contexts


def _evidence_digest(
    names: set[str],
    raw_contexts: list[dict[str, Any]],
    scene_beats: list[dict[str, Any]],
) -> dict[str, list[str]]:
    digest: dict[str, list[str]] = {
        "identity_or_forms": [],
        "locations": [],
        "abilities": [],
        "items": [],
        "relationship_or_conflict_lines": [],
        "nearby_event_lines": [],
        "raw_focus_descriptions": [],
    }
    for chunk in raw_contexts:
        for node in chunk.get("nodes", []):
            name = _clean(node.get("surface_name"))
            line = "：".join(
                part for part in (name, _clean(node.get("description"))) if part
            )
            if not line:
                continue
            node_type = node.get("type")
            if name in names or any(item and item in line for item in names):
                digest["raw_focus_descriptions"].append(line)
            if node_type in {"TitleOrIdentity", "Identity", "Form"}:
                digest["identity_or_forms"].append(line)
            elif node_type == "Location":
                digest["locations"].append(line)
            elif node_type == "Ability":
                digest["abilities"].append(line)
            elif node_type in {"Artifact", "Item", "Weapon"}:
                digest["items"].append(line)
        for edge in chunk.get("edges", []):
            summary = _clean(edge.get("relation_summary") or edge.get("summary"))
            if not summary:
                continue
            digest["nearby_event_lines"].append(summary)
            edge_text = json.dumps(edge, ensure_ascii=False)
            if any(name and name in edge_text for name in names):
                digest["relationship_or_conflict_lines"].append(summary)
    for beat in scene_beats:
        text = _clean(beat.get("summary") or beat.get("event"))
        if text:
            digest["nearby_event_lines"].append(text)
    for key, values in list(digest.items()):
        deduped = []
        seen = set()
        for value in values:
            if value and value not in seen:
                deduped.append(value)
                seen.add(value)
            if len(deduped) >= 12:
                break
        digest[key] = deduped
    return digest


def _render_anchor_material(
    focus_name: str,
    character: dict[str, Any],
    current_anchor: dict[str, Any],
    evidence_digest: dict[str, list[str]],
    gaps: list[str],
    related_names: list[str],
    related_beats: list[str],
) -> str:
    lines = [
        f"焦点角色：{focus_name or '资料暂缺'}",
        f"当前剧情锚点：{current_anchor.get('event') or '资料暂缺'}",
        f"锚点顺序：{current_anchor.get('scheduled_order') or '资料暂缺'}",
        f"角色身份描述：{character.get('description') or '资料暂缺'}",
        f"首次出场顺序：{character.get('first_seen_order') or '资料暂缺'}",
        "身份/别名/形态证据："
        + "；".join(evidence_digest.get("identity_or_forms") or ["资料暂缺"]),
        "地点证据：" + "；".join(evidence_digest.get("locations") or ["资料暂缺"]),
        "能力证据：" + "；".join(evidence_digest.get("abilities") or ["资料暂缺"]),
        "物品证据：" + "；".join(evidence_digest.get("items") or ["资料暂缺"]),
        "关系/冲突证据："
        + "；".join(
            evidence_digest.get("relationship_or_conflict_lines")
            or ["资料暂缺"]
        ),
        "附近事件证据："
        + "；".join(evidence_digest.get("nearby_event_lines") or ["资料暂缺"]),
        "角色原始描述："
        + "；".join(evidence_digest.get("raw_focus_descriptions") or ["资料暂缺"]),
        "相关表面名：" + "；".join(related_names or ["资料暂缺"]),
        "资料缺口：" + "；".join(gaps or ["暂无明显缺口"]),
    ]
    if related_beats:
        lines.append("相关剧情片段：" + "；".join(related_beats[:5]))
    return "\n".join(lines)


def build_anchor_preview(
    project_path: Path,
    runtime: dict[str, Any],
    llm: Callable[..., str],
    character_id: str,
    progress_percent: float | None = None,
) -> dict[str, Any]:
    orchestrator = runtime["orchestrator"]
    characters = runtime["character_db"].get("characters", [])
    by_id = {item.get("character_id"): item for item in characters}
    record = by_id.get(character_id)
    if not record:
        raise ValueError("角色不存在。")
    timeline = (
        orchestrator.store.runtime.get("canonical_timeline")
        or orchestrator.canonical_timeline
        or runtime["world_db"].get("canonical_timeline_db", {}).get(
            "timeline_nodes", []
        )
        or []
    )
    if timeline and progress_percent and progress_percent > 0:
        cursor = round((len(timeline) - 1) * float(progress_percent) / 100)
    elif timeline:
        try:
            cursor, _anchor = orchestrator._opening_anchor(character_id)
        except Exception:
            cursor = int(orchestrator.store.runtime.get("timeline_cursor", 0) or 0)
    else:
        cursor = 0
    cursor = max(0, min(cursor, max(0, len(timeline) - 1)))
    current_anchor = timeline[cursor] if timeline else {}

    canonical_novel = runtime["world_db"].get("canonical_novel_db", {})
    entity_track = canonical_novel.get("entity_tracks", {}).get(character_id, {})
    names = {
        _clean(record.get("canonical_name")),
        _clean(entity_track.get("canonical_name")),
        *[_clean(item) for item in record.get("aliases", [])],
        *[_clean(item) for item in record.get("forms", [])],
        *[_clean(item) for item in entity_track.get("aliases", [])],
        *[_clean(item) for item in entity_track.get("forms", [])],
    }
    names = {item for item in names if item}
    source_orders = _source_orders(record) | _source_orders(entity_track)
    if not source_orders:
        for value in current_anchor.get("source_chunk_ids", []):
            try:
                source_orders.add(int(value))
            except (TypeError, ValueError):
                pass
    scene_beats = []
    for beat in timeline:
        beat_text = json.dumps(beat, ensure_ascii=False)
        if character_id in beat_text or any(name in beat_text for name in names):
            scene_beats.append(beat)
        else:
            beat_orders = set()
            for value in beat.get("source_chunk_ids", []):
                try:
                    beat_orders.add(int(value))
                except (TypeError, ValueError):
                    pass
            if beat_orders & source_orders:
                scene_beats.append(beat)
    raw_contexts = _raw_chunk_contexts(project_path, names, source_orders)
    digest = _evidence_digest(names, raw_contexts, scene_beats)
    related_names = set()
    for chunk in raw_contexts:
        for node in chunk.get("nodes", []):
            if node.get("surface_name"):
                related_names.add(node["surface_name"])
        for edge in chunk.get("edges", []):
            if edge.get("source_surface_name"):
                related_names.add(edge["source_surface_name"])
            if edge.get("target_surface_name"):
                related_names.add(edge["target_surface_name"])
    gaps = []
    if character_id not in orchestrator.agent_by_character_id:
        gaps.append("没有预制角色画像，需要运行时用原文片段补充性格、口吻和行动习惯")
    if not digest.get("relationship_or_conflict_lines"):
        gaps.append("没有直接关系线，需要从附近事件推断人物关系或冲突")
    if not raw_contexts:
        gaps.append("没有原始片段上下文，需要回查原文")
    character = {
        "character_id": character_id,
        "canonical_name": record.get("canonical_name")
        or entity_track.get("canonical_name"),
        "aliases": record.get("aliases", []),
        "titles": record.get("titles", []),
        "forms": record.get("forms", []) or entity_track.get("forms", []),
        "first_seen_order": record.get("first_seen_order")
        or entity_track.get("first_seen_order"),
        "description": record.get("background_summary")
        or "；".join(entity_track.get("descriptions", [])),
    }
    material = _render_anchor_material(
        character.get("canonical_name", ""),
        character,
        current_anchor,
        digest,
        gaps,
        sorted(related_names)[:40],
        [_clean(item.get("summary") or item.get("event")) for item in scene_beats],
    )
    system = (
        "你是给玩家看的小说剧情预览撰稿人，不是技术顾问。只根据给出的"
        "事实清单写角色开局预览，不使用外部知识。禁止写技术方案、代码、"
        "schema、pipeline、API、节点、边、字段、ID、表格。证据不足时写"
        "“资料暂缺”，不要编造。第一行必须直接是“角色名 - 剧情阶段”。"
    )
    user = (
        "请只根据下面的事实清单写预览，不要提到事实清单、技术系统或内部结构。\n\n"
        "输出格式：\n"
        "角色名 - 当前剧情阶段\n"
        "2到4句话可玩定位。\n\n"
        "【已知信息】\n"
        "- 身份/别名/形态：...\n"
        "- 目标或动机：...\n"
        "- 地点：...\n"
        "- 人物关系：...\n\n"
        "【能力与限制】\n"
        "- 能力：...\n"
        "- 物品：...\n"
        "- 弱点或限制：...\n\n"
        "【前因后果】\n"
        "...\n\n"
        "【开局切入点】\n"
        "1. ...\n2. ...\n3. ...\n\n"
        "【资料缺口】\n"
        "- ...\n\n"
        "事实清单：\n"
        f"{material}"
    )
    summary = str(llm(system, user, temperature=0.2, max_tokens=1600)).strip()
    return {
        "summary": summary,
        "preview_material": material,
        "preview_cursor": cursor,
        "current_anchor": current_anchor,
        "focus_character_packet": {
            "character": character,
            "evidence_digest": digest,
            "raw_chunk_contexts": raw_contexts,
            "related_scene_beats": scene_beats[:8],
            "evidence_gaps": gaps,
        },
    }
