from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import (
    DESKTOP_APP_DIR,
    ESTIMATED_PIPELINE_OVERHEAD_SECONDS,
    MAX_PIPELINE_WORKERS,
    SECONDS_PER_CHUNK,
    get_llm_settings,
)
from .project_store import read_json, read_status, utc_now, write_json, write_status


_executor = ThreadPoolExecutor(
    max_workers=MAX_PIPELINE_WORKERS,
    thread_name_prefix="novel-pipeline",
)
_futures: dict[str, Any] = {}


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟{secs}秒"
    return f"{secs}秒"


def _pipeline_quality(path: Path) -> dict[str, Any]:
    raw_path = path / "db" / "graph" / "raw_graph_triples.json"
    if not raw_path.is_file():
        raw_path = path / "generated_db" / "graph" / "raw_graph_triples.json"
    if not raw_path.is_file():
        raw_path = path / "raw_graph_triples.json"
    if not raw_path.is_file():
        return {}
    raw = read_json(raw_path)
    quality = raw.get("quality_summary", {})
    valid = int(quality.get("valid_chunk_count", 0))
    partial = int(quality.get("partial_chunk_count", 0))
    errors = int(quality.get("validation_error_count", 0))
    warnings = []
    if partial:
        warnings.append(
            f"{partial} 个 chunk 含有被校验器过滤的部分结果；有效证据仍已继续进入下游数据库。"
        )
    if errors:
        warnings.append(f"抽取阶段记录了 {errors} 条字段或证据校验问题。")
    return {
        "pipeline_quality": {
            "valid_chunk_count": valid,
            "partial_chunk_count": partial,
            "validation_error_count": errors,
            "node_count": quality.get("node_count", 0),
            "edge_count": quality.get("edge_count", 0),
        },
        "warnings": warnings,
    }


def start_pipeline(path: Path) -> dict[str, Any]:
    key = str(path.resolve())
    existing = _futures.get(key)
    if existing is not None and not existing.done():
        return read_status(path)
    status = read_status(path)
    if status.get("status") in {"queued", "processing"}:
        return status
    write_status(
        path,
        {
            "status": "queued",
            "current_step": "Queued",
            "message": "任务已进入队列",
            "error": None,
            "queue_started_at": utc_now(),
        },
    )
    _futures[key] = _executor.submit(_run_pipeline, path)
    return read_status(path)


def _append_log(path: Path, stream: str, message: str) -> None:
    record = {
        "timestamp": utc_now(),
        "stream": stream,
        "message": message.rstrip(),
    }
    with (path / "logs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _recent_pipeline_error(path: Path, max_lines: int = 8) -> str:
    log_path = path / "logs.jsonl"
    if not log_path.is_file():
        return ""
    records = []
    for line in log_path.read_text(encoding="utf-8").splitlines()[-120:]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = str(record.get("message") or "").strip()
        if not message:
            continue
        records.append(message)
    if not records:
        return ""
    traceback_start = None
    for index, message in enumerate(records):
        if message.startswith("Traceback "):
            traceback_start = index
    if traceback_start is not None:
        return "\n".join(records[traceback_start:][-max_lines:])
    interesting = [
        item
        for item in records
        if any(
            marker in item
            for marker in (
                "Error",
                "error",
                "Exception",
                "Traceback",
                "ValueError",
                "TypeError",
                "KeyError",
                "失败",
            )
        )
    ]
    return "\n".join((interesting or records)[-max_lines:])


def _run_pipeline(path: Path) -> None:
    status = read_status(path)
    chunk_size = int(status.get("chunk_size", 3000))
    overlap = int(status.get("overlap", 300))
    selected_chunks = int(status.get("selected_chunks", 0))
    command = [
        sys.executable,
        "-u",
        str(DESKTOP_APP_DIR / "pipeline_program.py"),
        "--novel",
        str(path / "raw.txt"),
        "--percent",
        "100",
        "--chunk-size",
        str(chunk_size),
        "--overlap",
        str(overlap),
        "--chunk-limit",
        str(selected_chunks),
    ]
    llm_settings = get_llm_settings()
    env = os.environ.copy()
    env["NOVEL_LLM_BASE_URL"] = llm_settings["llm_base_url"]
    env["NOVEL_LLM_MODEL"] = llm_settings["llm_model"]
    env["NOVEL_LLM_API_KEY"] = llm_settings["llm_api_key"]
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    env.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
    started = time.monotonic()
    initial_estimate = (
        selected_chunks * SECONDS_PER_CHUNK
        + ESTIMATED_PIPELINE_OVERHEAD_SECONDS
    )
    write_status(
        path,
        {
            "status": "processing",
            "current_step": "Starting pipeline",
            "progress": 0.01,
            "message": "正在启动小说处理流程",
            "started_at": utc_now(),
            "estimated_remaining_seconds": initial_estimate,
        },
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            if line.startswith("@@PROGRESS "):
                try:
                    payload = json.loads(line[len("@@PROGRESS ") :])
                except json.JSONDecodeError:
                    _append_log(path, "pipeline", line)
                    continue
                percent = float(payload.get("percent", 0))
                elapsed = time.monotonic() - started
                if percent >= 1:
                    measured_remaining = elapsed * (100 - percent) / percent
                else:
                    measured_remaining = initial_estimate
                baseline_remaining = max(
                    0, initial_estimate - elapsed
                )
                measured_remaining = max(measured_remaining, baseline_remaining)
                current = payload.get("current")
                total = payload.get("total")
                label = str(payload.get("label", "Processing"))
                is_chunk_progress = label.startswith("Step 9 ")
                is_batch_progress = label.startswith("Step 11 ")
                if is_chunk_progress and current is not None and total is not None:
                    chunk_remaining = max(0, int(total) - int(current))
                    measured_remaining = max(
                        measured_remaining, chunk_remaining * SECONDS_PER_CHUNK
                    )
                write_status(
                    path,
                    {
                        "status": "processing",
                        "current_step": label,
                        "progress": round(percent / 100, 4),
                        "message": label,
                        "current_chunk": current if is_chunk_progress else None,
                        "processing_chunk_total": total if is_chunk_progress else None,
                        "current_batch": current if is_batch_progress else None,
                        "processing_batch_total": total if is_batch_progress else None,
                        "elapsed_seconds": round(elapsed),
                        "estimated_remaining_seconds": round(measured_remaining),
                        "estimated_remaining_text": format_duration(measured_remaining),
                    },
                )
            else:
                _append_log(path, "pipeline", line)
        return_code = process.wait()
        if return_code != 0:
            detail = _recent_pipeline_error(path)
            message = f"Pipeline 退出码：{return_code}"
            if detail:
                message += "\n" + detail
            raise RuntimeError(message)
        metadata = read_json(path / "project.json", default={})
        metadata["updated_at"] = utc_now()
        metadata["completed_at"] = utc_now()
        write_json(path / "project.json", metadata)
        write_status(
            path,
            {
                "status": "ready",
                "current_step": "Complete",
                "progress": 1.0,
                "message": "小说世界已准备完成",
                "error": None,
                "completed_at": utc_now(),
                "elapsed_seconds": round(time.monotonic() - started),
                "estimated_remaining_seconds": 0,
                "estimated_remaining_text": "已完成",
                **_pipeline_quality(path),
            },
        )
    except Exception as error:
        _append_log(path, "server", repr(error))
        write_status(
            path,
            {
                "status": "failed",
                "current_step": "Failed",
                "message": "小说处理失败",
                "error": str(error),
                "failed_at": utc_now(),
            },
        )
