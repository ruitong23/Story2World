from __future__ import annotations

import json
import queue
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..project_store import lock_for, normalize_id, project_dir, read_status
from ..runtime.chat_runtime import (
    _load_session_runtime,
    _make_llm_callable,
    get_chat_session,
    get_world_admin_snapshot,
    run_chat,
    run_world_admin_chat,
    save_chat_session,
)
from ..runtime.preview_builder import build_anchor_preview
from ..schemas import (
    AnchorPreviewResponse,
    ChatRequest,
    ChatResponse,
    ChatSaveResponse,
    ChatSessionResponse,
    WorldAdminRequest,
    WorldAdminResponse,
)


router = APIRouter()


@router.post("/projects/{project_id}/chat", response_model=ChatResponse)
def chat(project_id: str, payload: ChatRequest):
    try:
        path = project_dir(payload.username, project_id)
        status = read_status(path)
        if status.get("status") != "ready":
            raise HTTPException(status_code=409, detail="项目尚未处理完成。")
        session_id = normalize_id(payload.session_id, "session_id", 80)
        with lock_for(path / "sessions" / f"{session_id}.lock"):
            return run_chat(
                path,
                session_id,
                payload.character_id,
                payload.message,
            )
    except HTTPException:
        raise
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"对话运行失败：{error}") from error


@router.post("/projects/{project_id}/chat/stream")
def chat_stream(project_id: str, payload: ChatRequest):
    path = project_dir(payload.username, project_id)
    status = read_status(path)
    if status.get("status") != "ready":
        raise HTTPException(status_code=409, detail="项目尚未处理完成。")
    session_id = normalize_id(payload.session_id, "session_id", 80)
    events: queue.Queue[dict] = queue.Queue()

    def worker():
        try:
            with lock_for(path / "sessions" / f"{session_id}.lock"):
                result = run_chat(
                    path,
                    session_id,
                    payload.character_id,
                    payload.message,
                    progress_callback=lambda item: events.put(
                        {"type": "progress", "data": item}
                    ),
                )
            events.put({"type": "result", "data": result})
        except Exception as error:
            events.put({"type": "error", "error": str(error)})
        finally:
            events.put({"type": "done"})

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            item = events.get()
            yield json.dumps(item, ensure_ascii=False) + "\n"
            if item["type"] == "done":
                break

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/projects/{project_id}/chat/session",
    response_model=ChatSessionResponse,
)
def chat_session(
    project_id: str,
    username: str,
    session_id: str,
    character_id: str,
):
    try:
        path = project_dir(username, project_id)
        session_id = normalize_id(session_id, "session_id", 80)
        with lock_for(path / "sessions" / f"{session_id}.lock"):
            return get_chat_session(path, session_id, character_id)
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500, detail=f"读取会话失败：{error}"
        ) from error


@router.get(
    "/projects/{project_id}/chat/anchor-preview",
    response_model=AnchorPreviewResponse,
)
def chat_anchor_preview(
    project_id: str,
    username: str,
    session_id: str,
    character_id: str,
    progress_percent: float | None = None,
):
    try:
        path = project_dir(username, project_id)
        status = read_status(path)
        if status.get("status") != "ready":
            raise HTTPException(status_code=409, detail="项目尚未处理完成。")
        session_id = normalize_id(session_id, "session_id", 80)
        character_id = normalize_id(character_id, "character_id", 160)
        with lock_for(path / "sessions" / f"{session_id}.lock"):
            _session_dir, runtime = _load_session_runtime(path, session_id)
            preview = build_anchor_preview(
                path,
                runtime,
                _make_llm_callable(),
                character_id,
                progress_percent=progress_percent,
            )
        return {
            "project_id": project_id,
            "session_id": session_id,
            "character_id": character_id,
            **preview,
        }
    except HTTPException:
        raise
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500, detail=f"生成剧情预览失败：{error}"
        ) from error


@router.get(
    "/projects/{project_id}/chat/world-admin",
    response_model=WorldAdminResponse,
)
def chat_world_admin_snapshot(
    project_id: str,
    username: str,
    session_id: str = "default",
    character_id: str | None = None,
):
    try:
        path = project_dir(username, project_id)
        status = read_status(path)
        if status.get("status") != "ready":
            raise HTTPException(status_code=409, detail="项目尚未处理完成。")
        session_id = normalize_id(session_id, "session_id", 80)
        with lock_for(path / "sessions" / f"{session_id}.lock"):
            result = get_world_admin_snapshot(
                path,
                session_id,
                character_id,
            )
        return {
            **result,
            "reply": "",
            "applied": False,
            "event_id": "",
            "result": {},
        }
    except HTTPException:
        raise
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500, detail=f"读取世界管理员状态失败：{error}"
        ) from error


@router.post(
    "/projects/{project_id}/chat/world-admin",
    response_model=WorldAdminResponse,
)
def chat_world_admin(project_id: str, payload: WorldAdminRequest):
    try:
        path = project_dir(payload.username, project_id)
        status = read_status(path)
        if status.get("status") != "ready":
            raise HTTPException(status_code=409, detail="项目尚未处理完成。")
        session_id = normalize_id(payload.session_id, "session_id", 80)
        with lock_for(path / "sessions" / f"{session_id}.lock"):
            return run_world_admin_chat(
                path,
                session_id,
                payload.message,
                character_id=payload.character_id,
                apply_changes=payload.apply_changes,
            )
    except HTTPException:
        raise
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500, detail=f"世界管理员对话失败：{error}"
        ) from error


@router.post(
    "/projects/{project_id}/chat/save",
    response_model=ChatSaveResponse,
)
def chat_save(project_id: str, payload: ChatRequest):
    try:
        path = project_dir(payload.username, project_id)
        session_id = normalize_id(payload.session_id, "session_id", 80)
        with lock_for(path / "sessions" / f"{session_id}.lock"):
            return save_chat_session(
                path,
                session_id,
                payload.character_id,
            )
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"保存会话失败：{error}") from error
