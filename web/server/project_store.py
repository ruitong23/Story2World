from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import USERS_ROOT


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-\u3400-\u9fff]{1,64}$")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_username(username: str) -> str:
    value = str(username or "").strip()
    if not _SAFE_NAME.fullmatch(value):
        raise ValueError("用户名只能包含中英文、数字、下划线和短横线，长度 1–64。")
    return value


def normalize_id(value: str, label: str = "ID", max_length: int = 160) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or len(cleaned) > max_length or not re.fullmatch(
        r"[A-Za-z0-9_.\-]+", cleaned
    ):
        raise ValueError(f"{label} 格式不正确。")
    return cleaned


def user_dir(username: str, create: bool = False) -> Path:
    path = USERS_ROOT / normalize_username(username)
    if create:
        (path / "projects").mkdir(parents=True, exist_ok=True)
        profile = path / "user.json"
        if not profile.exists():
            write_json(
                profile,
                {"username": path.name, "created_at": utc_now()},
            )
    return path


def project_dir(username: str, project_id: str, must_exist: bool = True) -> Path:
    path = user_dir(username) / "projects" / normalize_id(project_id, "project_id")
    if must_exist and not path.is_dir():
        raise FileNotFoundError("项目不存在。")
    return path


def create_project(username: str, filename: str, settings: dict[str, Any]) -> Path:
    root = user_dir(username, create=True) / "projects"
    project_id = "project_" + uuid.uuid4().hex[:16]
    path = root / project_id
    (path / "sessions").mkdir(parents=True)
    now = utc_now()
    metadata = {
        "project_id": project_id,
        "username": normalize_username(username),
        "original_filename": filename,
        "settings": settings,
        "created_at": now,
        "updated_at": now,
    }
    write_json(path / "project.json", metadata)
    write_status(
        path,
        {
            "project_id": project_id,
            "username": normalize_username(username),
            "status": "uploaded",
            "current_step": "Waiting to start",
            "progress": 0.0,
            "message": "小说已上传，等待处理",
            "error": None,
            "created_at": now,
            "updated_at": now,
            **settings,
        },
    )
    return path


def lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        if default is not None:
            return default
        raise FileNotFoundError(f"文件不存在：{path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_status(path: Path) -> dict[str, Any]:
    return read_json(path / "status.json")


def write_status(path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    status_path = path / "status.json"
    with lock_for(status_path):
        current = read_json(status_path, default={})
        current.update(updates)
        current["updated_at"] = utc_now()
        write_json(status_path, current)
        return current


def list_projects(username: str) -> list[dict[str, Any]]:
    root = user_dir(username) / "projects"
    if not root.is_dir():
        return []
    result = []
    for path in root.iterdir():
        if not path.is_dir():
            continue
        metadata = read_json(path / "project.json", default={})
        status = read_json(path / "status.json", default={})
        result.append(
            {
                **metadata,
                "status": status.get("status", "unknown"),
                "progress": status.get("progress", 0),
                "message": status.get("message", ""),
            }
        )
    return sorted(result, key=lambda item: item.get("created_at", ""), reverse=True)


def load_output(path: Path, group: str, filename: str) -> Any:
    candidates = [
        path / "generated_db" / group / filename,
        path / "db" / group / filename,
        path / filename,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return read_json(candidate)
    raise FileNotFoundError(f"处理结果缺少 {filename}。")
