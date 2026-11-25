from datetime import date

import pytest

from daily_podcast.textutil import (
    extract_json,
    pack_chunks,
    sanitize_for_tts,
    spanish_date,
    split_long,
    title_similarity,
    truncate,
)


def test_spanish_date_uses_spanish_names():
    assert spanish_date(date(2026, 11, 20)) == "viernes, 20 de noviembre de 2026"


@pytest.mark.parametrize(
    "reply",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Aquí tienes el resultado: {"a": 1} Un saludo.',
    ],
)
def test_extract_json_tolerates_fences_and_prose(reply):
    assert extract_json(reply) == {"a": 1}


def test_extract_json_rejects_plain_text():
    with pytest.raises(ValueError):
        extract_json("no structured data here")


def test_sanitize_keeps_audio_tags_but_removes_markup():
    raw = "**Hoy** [excited] hay noticias. <break time='1s'/>\n# Titular\n- item"
    cleaned = sanitize_for_tts(raw)
    assert "[excited]" in cleaned
    assert "**" not in cleaned and "<break" not in cleaned and "#" not in cleaned
    assert "[short pause]" in cleaned


def test_split_long_respects_limit_and_sentences():
    text = "Primera frase corta. " * 20
    pieces = split_long(text, 60)
    assert all(len(piece) <= 60 for piece in pieces)
    assert "".join(pieces).replace(" ", "") == text.replace(" ", "")


def test_split_long_hard_cuts_a_pathological_sentence():
    pieces = split_long("x" * 250, 100)
    assert [len(p) for p in pieces] == [100, 100, 50]


def test_pack_chunks_never_exceeds_limit_and_keeps_order():
    blocks = [f"bloque {i} " * 5 for i in range(12)]
    chunks = pack_chunks(blocks, 120)
    assert all(len(chunk) <= 120 for chunk in chunks)
    assert chunks[0].startswith("bloque 0")
    assert "bloque 11" in chunks[-1]


def test_title_similarity_detects_same_event():
    a = "El Gobierno aprueba los presupuestos de 2027 en el Congreso"
    b = "Congreso aprueba presupuestos 2027 del Gobierno"
    c = "La selección española gana el partido amistoso en Madrid"
    assert title_similarity(a, b) >= 0.5
    assert title_similarity(a, c) < 0.5
    assert title_similarity("", a) == 0.0


def test_truncate_marks_omitted_text():
    assert truncate("abc", 10) == "abc"
    assert truncate("abcdef", 3).startswith("abc")
    assert "truncated 3 characters" in truncate("abcdef", 3)
