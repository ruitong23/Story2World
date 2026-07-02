from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
TOKEN_USAGE_PATH = APP_DIR / "token_usage.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_detail(error: urllib.error.HTTPError) -> str:
    detail = error.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(detail)
        inner = payload.get("error", payload)
        if isinstance(inner, dict):
            return inner.get("message") or json.dumps(inner, ensure_ascii=False)
        return str(inner)
    except json.JSONDecodeError:
        return detail.strip() or str(error)


def request_json(url: str, api_key: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code}: {_error_detail(error)}") from error


def list_models(base_url: str, api_key: str) -> list[str]:
    payload = request_json(base_url.rstrip("/") + "/models", api_key, timeout=20)
    models = []
    for item in payload.get("data", []):
        model_id = item.get("id")
        if model_id:
            models.append(str(model_id))
    return sorted(set(models), key=str.casefold)


def estimate_tokens(*texts: str) -> int:
    chars = sum(len(text or "") for text in texts)
    return max(1, round(chars / 3))


def log_token_usage(
    *,
    source: str,
    flow: str,
    base_url: str,
    model: str,
    usage: dict[str, Any] | None,
    prompt_text: str = "",
    completion_text: str = "",
    estimated: bool = False,
) -> dict[str, Any]:
    usage = usage or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if prompt_tokens is None:
        prompt_tokens = estimate_tokens(prompt_text)
        estimated = True
    if completion_tokens is None:
        completion_tokens = estimate_tokens(completion_text)
        estimated = True
    if total_tokens is None:
        total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
    record = {
        "timestamp": _now(),
        "source": source,
        "flow": flow,
        "base_url_host": base_url.split("//")[-1].split("/")[0],
        "model": model,
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "estimated": bool(estimated),
    }
    TOKEN_USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TOKEN_USAGE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def token_usage_summary(limit: int = 500) -> dict[str, Any]:
    records = []
    if TOKEN_USAGE_PATH.is_file():
        for line in TOKEN_USAGE_PATH.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "call_count": len(records),
    }
    by_source: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    for record in records:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            totals[key] += int(record.get(key) or 0)
        for bucket, name in ((by_source, record.get("source")), (by_model, record.get("model"))):
            name = str(name or "unknown")
            row = bucket.setdefault(
                name,
                {"name": name, "call_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
            row["call_count"] += 1
            row["prompt_tokens"] += int(record.get("prompt_tokens") or 0)
            row["completion_tokens"] += int(record.get("completion_tokens") or 0)
            row["total_tokens"] += int(record.get("total_tokens") or 0)
    return {
        "totals": totals,
        "by_source": sorted(by_source.values(), key=lambda item: item["total_tokens"], reverse=True),
        "by_model": sorted(by_model.values(), key=lambda item: item["total_tokens"], reverse=True),
        "recent": records[-limit:][::-1],
        "path": str(TOKEN_USAGE_PATH),
    }


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    source: str,
    flow: str,
    response_format: dict[str, Any] | None = None,
    timeout: int = 600,
) -> str:
    def build_payload(next_response_format):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if next_response_format:
            payload["response_format"] = next_response_format
        return payload

    response_formats = [response_format]
    if response_format:
        if response_format.get("type") != "json_object":
            response_formats.append({"type": "json_object"})
        response_formats.append(None)

    prompt_text = "\n".join(item.get("content", "") for item in messages)
    last_error = None
    body = None
    for next_response_format in response_formats:
        payload = build_payload(next_response_format)
        request = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            last_error = RuntimeError(f"HTTP {error.code}: {_error_detail(error)}")
            if error.code in {400, 422} and next_response_format:
                continue
            raise last_error from error
    if body is None:
        raise last_error or RuntimeError("LLM request failed.")

    content = body["choices"][0]["message"]["content"].strip()
    log_token_usage(
        source=source,
        flow=flow,
        base_url=base_url,
        model=model,
        usage=body.get("usage"),
        prompt_text=prompt_text,
        completion_text=content,
    )
    return content
