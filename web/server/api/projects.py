from __future__ import annotations

import math
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status

from ..config import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    ESTIMATED_PIPELINE_OVERHEAD_SECONDS,
    MAX_UPLOAD_BYTES,
    SECONDS_PER_CHUNK,
)
from ..project_store import (
    create_project,
    list_projects,
    normalize_username,
    project_dir,
    read_status,
    user_dir,
    write_status,
)
from ..schemas import UserCreate
from ..task_runner import format_duration, start_pipeline
from ..text_decoder import decode_novel_bytes
from ..runtime.chat_runtime import _make_llm_callable
from ..runtime.preview_builder import source_excerpt, summarize_source_excerpt


router = APIRouter()


def _decode_text(data: bytes) -> str:
    try:
        text, _ = decode_novel_bytes(data)
        return text
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _section_contents(text: str) -> tuple[list[str], str]:
    chinese = re.compile(
        r"(?m)^\s*(第[一二三四五六七八九十百千万零〇\d]+[章回节卷部].*|"
        r"卷[一二三四五六七八九十百千万零〇\d]+.*|序章.*|楔子.*|引子.*|后记.*)\s*$"
    )
    english = re.compile(r"(?mi)^(chapter\s+[\divxlcdm]+.*|chapter\s+[a-z]+.*)$")
    for pattern, method in (
        (chinese, "chinese_chapter"),
        (english, "english_chapter"),
    ):
        matches = list(pattern.finditer(text))
        if len(matches) < 2:
            continue
        sections = []
        if matches[0].start() > 0:
            intro = text[: matches[0].start()].strip()
            if intro:
                sections.append(intro)
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            content = text[match.end() : end].strip()
            if content:
                sections.append(content)
        return sections, method

    paragraphs = [item.strip() for item in re.split(r"\n+", text) if item.strip()]
    sections = []
    current = []
    current_len = 0
    for paragraph in paragraphs:
        if current and current_len + len(paragraph) > 5000:
            sections.append("\n".join(current))
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph)
    if current:
        sections.append("\n".join(current))
    return sections or [text], "paragraph_section"


def _chunk_count(sections: list[str], chunk_size: int, overlap: int) -> int:
    stride = chunk_size - overlap
    total = 0
    for section in sections:
        if not section:
            continue
        total += max(1, math.ceil(len(section) / stride))
    return max(1, total)


def _estimate(text: str, chunk_size: int, overlap: int) -> dict:
    if chunk_size <= 0:
        raise HTTPException(status_code=400, detail="chunk_size 必须大于 0。")
    if overlap < 0 or overlap >= chunk_size:
        raise HTTPException(
            status_code=400, detail="overlap 必须大于等于 0 且小于 chunk_size。"
        )
    sections, split_method = _section_contents(text)
    total = _chunk_count(sections, chunk_size, overlap)
    return {
        "character_count": len(text),
        "section_count": len(sections),
        "split_method": split_method,
        "chunk_size": chunk_size,
        "overlap": overlap,
        "estimated_total_chunks": total,
        "seconds_per_chunk": SECONDS_PER_CHUNK,
        "estimated_pipeline_overhead_seconds": ESTIMATED_PIPELINE_OVERHEAD_SECONDS,
        "estimated_full_seconds": (
            total * SECONDS_PER_CHUNK + ESTIMATED_PIPELINE_OVERHEAD_SECONDS
        ),
        "estimated_full_text": format_duration(
            total * SECONDS_PER_CHUNK + ESTIMATED_PIPELINE_OVERHEAD_SECONDS
        ),
        "note": "这是按每个 chunk 约 1 分钟计算的准备阶段估算，实际时间会受模型速度和后续整理步骤影响。",
    }


async def _read_upload(file: UploadFile) -> tuple[bytes, str]:
    filename = Path(file.filename or "").name
    if not filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="只允许上传 .txt 文件。")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="TXT 文件不能超过 6MB。")
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空。")
    return data, filename


@router.post("/users")
def create_user(payload: UserCreate):
    try:
        path = user_dir(payload.username, create=True)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"username": path.name, "status": "ready"}


@router.get("/users/{username}/projects")
def get_user_projects(username: str):
    try:
        return {"username": normalize_username(username), "projects": list_projects(username)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/projects/estimate")
async def estimate_project(
    file: UploadFile = File(...),
    chunk_size: int = Form(DEFAULT_CHUNK_SIZE),
    overlap: int = Form(DEFAULT_OVERLAP),
):
    data, _ = await _read_upload(file)
    return _estimate(_decode_text(data), chunk_size, overlap)


@router.post("/projects/source-preview")
async def preview_source_moment(
    file: UploadFile = File(...),
    selected_chunks: int | None = Form(None),
    chunk_size: int = Form(DEFAULT_CHUNK_SIZE),
    overlap: int = Form(DEFAULT_OVERLAP),
):
    data, _ = await _read_upload(file)
    text = _decode_text(data)
    estimate = _estimate(text, chunk_size, overlap)
    total = max(1, int(estimate.get("estimated_total_chunks") or 1))
    chosen = total if selected_chunks in (None, 0) else int(selected_chunks)
    if chosen < 1 or chosen > total:
        raise HTTPException(
            status_code=400,
            detail=f"selected_chunks 必须在 1 到 {total} 之间。",
        )
    percent = max(1.0, min(100.0, chosen * 100 / total))
    excerpt_info = source_excerpt(text, percent)
    try:
        summary = summarize_source_excerpt(
            _make_llm_callable(),
            excerpt_info,
            chunk_size,
            overlap,
            selected_chunks=chosen,
        )
    except Exception as error:
        raise HTTPException(
            status_code=500, detail=f"原文预览失败：{error}"
        ) from error
    return {
        "percent": percent,
        "character_range": [
            excerpt_info["start"],
            excerpt_info["end"],
            excerpt_info["total"],
        ],
        "summary": summary,
        "excerpt": excerpt_info["excerpt"],
    }


@router.post("/projects", status_code=status.HTTP_202_ACCEPTED)
async def upload_project(
    username: str = Form(...),
    file: UploadFile = File(...),
    selected_chunks: int | None = Form(None),
    chunk_size: int = Form(DEFAULT_CHUNK_SIZE),
    overlap: int = Form(DEFAULT_OVERLAP),
    auto_start: bool = Form(True),
):
    try:
        username = normalize_username(username)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    data, filename = await _read_upload(file)
    text = _decode_text(data)
    estimate = _estimate(text, chunk_size, overlap)
    total = estimate["estimated_total_chunks"]
    chosen = total if selected_chunks in (None, 0) else selected_chunks
    if chosen < 1 or chosen > total:
        raise HTTPException(
            status_code=400,
            detail=f"selected_chunks 必须在 1 到 {total} 之间。",
        )
    settings = {
        **estimate,
        "selected_chunks": chosen,
        "estimated_selected_seconds": (
            chosen * SECONDS_PER_CHUNK + ESTIMATED_PIPELINE_OVERHEAD_SECONDS
        ),
        "estimated_selected_text": format_duration(
            chosen * SECONDS_PER_CHUNK + ESTIMATED_PIPELINE_OVERHEAD_SECONDS
        ),
    }
    path = create_project(username, filename, settings)
    (path / "raw.txt").write_bytes(data)
    if auto_start:
        start_pipeline(path)
    return {
        "username": username,
        "project_id": path.name,
        "status": read_status(path)["status"],
        **settings,
        "status_url": f"/projects/{path.name}/status?username={username}",
    }


@router.post("/projects/{project_id}/start", status_code=status.HTTP_202_ACCEPTED)
def start_project(project_id: str, username: str = Query(...)):
    try:
        path = project_dir(username, project_id)
        return start_pipeline(path)
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/projects/{project_id}/status")
def get_project_status(project_id: str, username: str = Query(...)):
    try:
        return read_status(project_dir(username, project_id))
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
