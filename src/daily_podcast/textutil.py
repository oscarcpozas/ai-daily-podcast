"""Small, dependency-free text helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date

_MONTHS_ES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
_WEEKDAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def spanish_date(day: date) -> str:
    """``date(2026, 11, 20)`` → ``"viernes, 20 de noviembre de 2026"``."""
    return f"{_WEEKDAYS_ES[day.weekday()]}, {day.day} de {_MONTHS_ES[day.month - 1]} de {day.year}"


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> object:
    """Parse JSON from a model reply, tolerating code fences and surrounding prose."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("No JSON object found in model reply")
    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(cleaned[start:])
    return value


_SSML_BREAK = re.compile(r"<break\b[^>]*/?>", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_MARKDOWN = re.compile(r"(\*\*|__|^#+\s*|^\s*[-*]\s+)", re.MULTILINE)
_SPACES = re.compile(r"[ \t]+")


def sanitize_for_tts(text: str) -> str:
    """Make model output safe for ElevenLabs v3: audio tags yes, SSML and Markdown no."""
    text = _SSML_BREAK.sub(" [short pause] ", text)
    text = _HTML_TAG.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = _SPACES.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def split_long(text: str, max_chars: int) -> list[str]:
    """Split text on sentence boundaries so every piece fits ``max_chars``."""
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        while len(sentence) > max_chars:  # pathological sentence: hard cut on whitespace
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        candidate = f"{current} {sentence}".strip()
        if len(candidate) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def pack_chunks(blocks: Iterable[str], max_chars: int, separator: str = "\n\n") -> list[str]:
    """Greedily pack text blocks into chunks of at most ``max_chars`` characters."""
    chunks: list[str] = []
    current = ""
    for block in blocks:
        for piece in split_long(block.strip(), max_chars):
            if not piece:
                continue
            candidate = f"{current}{separator}{piece}" if current else piece
            if len(candidate) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


_WORD = re.compile(r"\w+", re.UNICODE)


def title_similarity(a: str, b: str) -> float:
    """Jaccard similarity of the meaningful words of two headlines."""
    words_a = {w for w in _WORD.findall(a.lower()) if len(w) > 3}
    words_b = {w for w in _WORD.findall(b.lower()) if len(w) > 3}
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n\n[… truncated {omitted} characters]"
