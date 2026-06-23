"""Robust plain-text loading for multilingual novel sources."""

from __future__ import annotations

import codecs
import unicodedata
from pathlib import Path

import chardet


COMMON_ENCODINGS = (
    "utf-8",
    "utf-8-sig",
    "gb18030",
    "gbk",
    "gb2312",
    "big5",
    "shift_jis",
    "euc-jp",
    "euc-kr",
    "windows-1252",
)


def _unique(values):
    result = []
    seen = set()
    for value in values:
        if not value:
            continue
        normalized = str(value).strip().lower().replace("_", "-")
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


def _bom_encoding(raw_data):
    for marker, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if raw_data.startswith(marker):
            return encoding
    return None


def detect_encoding(file_path, sample_size=200000):
    raw_data = Path(file_path).read_bytes()[:sample_size]
    result = chardet.detect(raw_data)
    return {
        "encoding": result.get("encoding"),
        "confidence": float(result.get("confidence") or 0.0),
        "bom_encoding": _bom_encoding(raw_data),
    }


def _text_quality(text):
    if not text:
        return -1000.0
    sample = text[:200000]
    printable = 0
    controls = 0
    nulls = sample.count("\x00")
    replacements = sample.count("\ufffd")
    mojibake = sum(sample.count(marker) for marker in ("╣", "╠", "╬", "║", "�"))
    for character in sample:
        category = unicodedata.category(character)
        if character in "\n\r\t" or category[0] not in {"C", "Z"}:
            printable += 1
        elif category == "Zs":
            printable += 1
        else:
            controls += 1
    length = max(1, len(sample))
    score = printable / length * 100.0
    score -= controls / length * 200.0
    score -= nulls / length * 500.0
    score -= replacements / length * 1000.0
    score -= mojibake / length * 300.0
    if any(character.isalpha() for character in sample):
        score += 5.0
    if "\n" in sample:
        score += 2.0
    return score


def _encoding_candidates(detection):
    detected = detection.get("encoding")
    confidence = detection.get("confidence", 0.0)
    return _unique(
        [
            detection.get("bom_encoding"),
            "utf-8",
            "utf-8-sig",
            detected if detected and confidence >= 0.5 else None,
            *COMMON_ENCODINGS[2:],
            detected if detected and confidence < 0.5 else None,
            "utf-16",
            "utf-32",
        ]
    )


def read_novel_txt(file_path):
    file_path = Path(file_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Novel file does not exist: {file_path}")

    raw_data = file_path.read_bytes()
    if not raw_data:
        raise ValueError("Novel file is empty.")

    detection = detect_encoding(file_path)
    print("检测到的编码:", detection.get("encoding"))
    print("检测置信度:", detection.get("confidence"))

    decoded = []
    failures = []
    for rank, encoding in enumerate(_encoding_candidates(detection)):
        try:
            text = raw_data.decode(encoding, errors="strict")
        except (UnicodeDecodeError, LookupError) as error:
            failures.append(f"{encoding}: {error.__class__.__name__}")
            continue
        quality = _text_quality(text)
        priority_bonus = max(0, 8 - rank) * 0.15
        if encoding == detection.get("bom_encoding"):
            priority_bonus += 10
        if (
            encoding == detection.get("encoding")
            and detection.get("confidence", 0.0) >= 0.5
        ):
            priority_bonus += detection["confidence"] * 4
        decoded.append((quality + priority_bonus, quality, encoding, text))

    if not decoded:
        raise ValueError(
            "Novel text could not be decoded with supported encodings. "
            + "; ".join(failures[:8])
        )

    _, quality, encoding, text = max(decoded, key=lambda item: item[0])
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    if quality < 75 or len(text.strip()) < 20:
        raise ValueError(
            "The selected file does not look like readable plain text. "
            "Use a real TXT file instead of a renamed EPUB/PDF/archive."
        )

    print(f"\n成功使用编码读取: {encoding}")
    print("文本质量评分:", round(quality, 2))
    print("小说总字数:", len(text))
    print("\n前 1000 字预览：")
    print(text[:1000])
    return text
