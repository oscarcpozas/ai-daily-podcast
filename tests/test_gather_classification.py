"""The dropped-classification warning must carry the offending value."""

from __future__ import annotations

import logging

from daily_podcast.pipeline.gather import _split_classification


def test_an_invalid_classification_logs_what_the_model_sent(caplog):
    item = {
        "title": "El Congreso aprueba los presupuestos",
        "url": "https://www.rtve.es/noticias/20261125/presupuestos.shtml",
        "category": "COOKING",
        "sentiment": "neutral",
        "relevance": 4,
    }

    with caplog.at_level(logging.WARNING):
        article = _split_classification(item)

    assert "classification" not in article
    assert "COOKING" in caplog.text
