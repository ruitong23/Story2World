from __future__ import annotations

import codecs
import unicodedata

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
    "utf-16",
    "utf-32",
)


def _bom_encoding(data: bytes) -> str | None:
    for marker, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(marker):
            return encoding
    return None


def _unique(values: list[str | None]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if not value:
            continue
        marker = value.lower().replace("_", "-")
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def _text_quality(text: str) -> float:
    if not text:
        return -1000
    sample = text[:200000]
    printable = 0
    controls = 0
    for character in sample:
        category = unicodedata.category(character)
        if character in "\n\r\t" or category[0] not in {"C", "Z"} or category == "Zs":
            printable += 1
        else:
            controls += 1
    length = max(1, len(sample))
    score = printable / length * 100
    score -= controls / length * 200
    score -= sample.count("\x00") / length * 500
    score -= sample.count("\ufffd") / length * 1000
    if any(character.isalpha() for character in sample):
        score += 5
    if "\n" in sample:
        score += 2
    return score


def decode_novel_bytes(data: bytes) -> tuple[str, str]:
    if not data:
        raise ValueError("上传文件为空。")

    detection = chardet.detect(data[:200000])
    detected = detection.get("encoding")
    confidence = float(detection.get("confidence") or 0)
    bom = _bom_encoding(data)
    candidates = _unique(
        [
            bom,
            "utf-8",
            "utf-8-sig",
            detected if detected and confidence >= 0.5 else None,
            *COMMON_ENCODINGS[2:],
            detected if detected and confidence < 0.5 else None,
        ]
    )

    decoded = []
    for rank, encoding in enumerate(candidates):
        try:
            text = data.decode(encoding, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
        quality = _text_quality(text)
        bonus = max(0, 8 - rank) * 0.15
        if encoding == bom:
            bonus += 10
        if encoding == detected and confidence >= 0.5:
            bonus += confidence * 4
        decoded.append((quality + bonus, quality, encoding, text))

    if not decoded:
        raise ValueError("无法识别 TXT 文件编码。")

    _, quality, encoding, text = max(decoded, key=lambda item: item[0])
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    if quality < 75 or len(text.strip()) < 20:
        raise ValueError("文件不像可读取的纯文本，请确认它不是改名后的 EPUB、PDF 或压缩包。")
    return text, encoding
