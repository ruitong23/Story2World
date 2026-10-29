from __future__ import annotations

import json
import os
import time
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


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "y", "on", "enabled", "enable"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "disable"}:
        return False
    return default


def strip_thinking_blocks(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    for tag in ("think", "thinking", "reasoning"):
        lower = text.casefold()
        open_tag = f"<{tag}>"
        close_tag = f"</{tag}>"
        while open_tag in lower:
            start = lower.find(open_tag)
            end = lower.find(close_tag, start + len(open_tag))
            if end < 0:
                json_positions = [
                    pos
                    for pos in (
                        text.find("{", start + len(open_tag)),
                        text.find("[", start + len(open_tag)),
                    )
                    if pos >= 0
                ]
                if json_positions:
                    text = text[min(json_positions):].strip()
                else:
                    text = text[:start].strip()
                break
            text = (text[:start] + text[end + len(close_tag):]).strip()
            lower = text.casefold()
    return text.strip()


def with_thinking_instruction(messages: list[dict[str, str]], thinking_enabled: bool) -> list[dict[str, str]]:
    if thinking_enabled:
        instruction = (
            "If you use internal reasoning, do not include it in the answer. "
            "Never output <think>, reasoning_content, analysis, or chain-of-thought. "
            "Return only the requested final content."
        )
    else:
        instruction = (
            "Thinking/reasoning mode is disabled for this request. Do not think out loud. "
            "Do not output <think>, reasoning_content, analysis, hidden reasoning, or chain-of-thought. "
            "Return only the requested final answer."
        )
    result = [dict(item) for item in messages]
    if result and result[0].get("role") == "system":
        result[0]["content"] = instruction + "\n\n" + str(result[0].get("content", ""))
    else:
        result.insert(0, {"role": "system", "content": instruction})
    return result


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
    extra: dict[str, Any] | None = None,
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
    if extra:
        record.update(extra)
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
    thinking_enabled: bool = False,
    timeout: int = 600,
) -> str:
    thinking_enabled = parse_bool(thinking_enabled, default=False)
    messages = with_thinking_instruction(messages, thinking_enabled)

    def thinking_control_variants():
        if thinking_enabled:
            return [{}]
        return [
            {
                "reasoning_effort": "none",
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
                "thinking": {"type": "disabled"},
                "reasoning": {"effort": "none"},
            },
            {
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            {
                "reasoning_effort": "none",
                "thinking": {"type": "disabled"},
            },
            {},
        ]

    def build_payload(next_response_format, thinking_controls):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        payload.update(thinking_controls)
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
    selected_response_format = None
    selected_thinking_controls: dict[str, Any] = {}
    request_started = time.perf_counter()
    for next_response_format in response_formats:
        for thinking_controls in thinking_control_variants():
            payload = build_payload(next_response_format, thinking_controls)
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
                selected_response_format = next_response_format
                selected_thinking_controls = dict(thinking_controls)
                break
            except urllib.error.HTTPError as error:
                last_error = RuntimeError(f"HTTP {error.code}: {_error_detail(error)}")
                if error.code in {400, 422} and (
                    thinking_controls or next_response_format
                ):
                    continue
                raise last_error from error
        if body is not None:
            break
    if body is None:
        raise last_error or RuntimeError("LLM request failed.")

    message = body["choices"][0].get("message", {})
    raw_content = message.get("content") or ""
    content = strip_thinking_blocks(raw_content).strip()
    elapsed_seconds = round(time.perf_counter() - request_started, 3)
    raw_casefold = str(raw_content).casefold()
    log_token_usage(
        source=source,
        flow=flow,
        base_url=base_url,
        model=model,
        usage=body.get("usage"),
        prompt_text=prompt_text,
        completion_text=content,
        extra={
            "elapsed_seconds": elapsed_seconds,
            "thinking_enabled": bool(thinking_enabled),
            "thinking_controls_sent": sorted(selected_thinking_controls.keys()),
            "response_format_sent": (
                selected_response_format.get("type")
                if isinstance(selected_response_format, dict)
                else ""
            ),
            "raw_completion_chars": len(str(raw_content)),
            "stripped_completion_chars": len(content),
            "thinking_markup_detected": any(
                marker in raw_casefold
                for marker in (
                    "<think",
                    "<thinking",
                    "<reasoning",
                    "reasoning_content",
                )
            ),
        },
    )
    return content
