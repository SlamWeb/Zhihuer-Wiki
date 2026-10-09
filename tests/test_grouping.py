"""Candidate grouping coverage and truthful backend provenance."""
import sys
import types

import pytest

from zhihuer_wiki.grouping import GroupingUnavailable, group_entries


def entries(count):
    return [{"entry_id": f"e-{i}", "topic_hint": "工作" if i % 2 else "音乐", "paraphrase": f"作者的具体表达{i}",
             "conditions": "在该情境下", "quotes": ["音乐文化经历" if i % 2 == 0 else "工作时间条件"]} for i in range(count)]


def test_small_corpus_skips_numerical_grouping_even_when_embedding_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    result = group_entries(entries(200))
    assert result["provenance"]["actual_backend"] == "none"
    assert result["provenance"]["requested_backend"] == "embedding"
    assert len(result["groups"]) == 1
    assert len(result["groups"][0]["entry_ids"]) == 200


def test_embedding_unavailable_never_silently_becomes_lexical(monkeypatch):
    pytest.importorskip("sklearn")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(GroupingUnavailable, match="explicitly select"):
        group_entries(entries(201))


def test_explicit_lexical_tfidf_is_deterministic_complete_and_labelled():
    pytest.importorskip("sklearn")
    values = entries(201)
    first = group_entries(values, backend="tfidf", candidate_groups=3)
    second = group_entries(values, backend="tfidf", candidate_groups=3)
    assert first == second
    ids = [e for group in first["groups"] for e in group["entry_ids"]]
    assert len(ids) == len(set(ids)) == 201
    assert set(ids) == {e["entry_id"] for e in values}
    assert first["provenance"]["actual_backend"] == "lexical-tfidf+kmeans"
    assert first["provenance"]["model"] is None


def test_invalid_backend_duplicate_ids_and_disable():
    with pytest.raises(ValueError, match="backend"):
        group_entries(entries(2), backend="magic")
    with pytest.raises(ValueError, match="Duplicate"):
        group_entries([entries(1)[0], entries(1)[0]])
    result = group_entries(entries(201), backend="none")
    assert result["provenance"]["actual_backend"] == "none"
    assert result["provenance"]["reason"] == "explicitly disabled"


def test_embedding_backend_records_actual_model_and_runs_on_cpu(monkeypatch):
    pytest.importorskip("sklearn")
    import numpy as np
    calls = {}
    class LocalFixtureModel:
        def __init__(self, name, **kwargs):
            calls.update(name=name, **kwargs)
        def encode(self, texts, **kwargs):
            calls["count"] = len(texts)
            return np.array([[i % 2, 1 - i % 2] for i in range(len(texts))], dtype=float)
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=LocalFixtureModel))
    result = group_entries(entries(201), embedding_model="fixture-local-path", candidate_groups=2)
    assert calls == {"name": "fixture-local-path", "device": "cpu", "count": 201}
    assert result["provenance"]["actual_backend"] == "sentence-transformers+kmeans"
    assert result["provenance"]["model"] == "fixture-local-path"
    assert sum(len(g["entry_ids"]) for g in result["groups"]) == 201
