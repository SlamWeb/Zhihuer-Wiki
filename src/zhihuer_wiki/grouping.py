"""Optional candidate groups. Every member is passed to semantic classification."""
from __future__ import annotations

import math


class GroupingUnavailable(ValueError):
    pass


def group_entries(entries: list[dict], *, backend: str = "embedding", threshold: int = 200,
                  embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                  candidate_groups: int | None = None) -> dict:
    if backend not in {"embedding", "tfidf", "none"}:
        raise ValueError("grouping_backend must be embedding, tfidf, or none")
    ids = [entry["entry_id"] for entry in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate canonical entry IDs")
    provenance = {
        "requested_backend": backend, "actual_backend": "none", "entry_count": len(entries),
        "threshold": threshold, "model": None, "random_state": None,
        "reason": "small corpus" if len(entries) <= threshold else "explicitly disabled",
    }
    if len(entries) <= threshold or backend == "none":
        return {"groups": [{"group_id": "candidate-0001", "entry_ids": ids}], "provenance": provenance}
    from sklearn.cluster import KMeans

    count = candidate_groups or max(2, min(64, math.ceil(math.sqrt(len(entries) / 2))))
    if not 1 <= count <= len(entries):
        raise ValueError("candidate_groups must be between 1 and the entry count")
    texts = ["\n".join([e["topic_hint"], e["paraphrase"], e["conditions"], *e["quotes"]]) for e in entries]
    if backend == "embedding":
        try:
            from sentence_transformers import SentenceTransformer
            # The first named-model run may download public weights into the normal
            # user cache; inference remains local CPU. Hugging Face offline flags
            # and explicitly supplied local model directories are respected.
            model = SentenceTransformer(embedding_model, device="cpu")
            vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        except Exception as exc:
            raise GroupingUnavailable(
                "Local embedding model unavailable. Install semantic extras and obtain the model, "
                "or explicitly select grouping_backend='tfidf'. No implicit lexical fallback."
            ) from exc
        provenance.update(actual_backend="sentence-transformers+kmeans", model=embedding_model)
    else:
        from sklearn.feature_extraction.text import TfidfVectorizer
        try:
            vectors = TfidfVectorizer(analyzer="char", ngram_range=(1, 3), max_features=50000).fit_transform(texts)
        except ValueError as exc:
            raise GroupingUnavailable("Lexical TF-IDF has no usable vocabulary") from exc
        provenance.update(actual_backend="lexical-tfidf+kmeans")
    labels = KMeans(n_clusters=count, random_state=0, n_init=10).fit_predict(vectors)
    # Label numbers are implementation details; IDs follow first occurrence order.
    members = {}
    for entry_id, label in zip(ids, labels):
        members.setdefault(int(label), []).append(entry_id)
    groups = [{"group_id": f"candidate-{i + 1:04d}", "entry_ids": group}
              for i, group in enumerate(members.values())]
    provenance.update(random_state=0, reason="candidate grouping only", candidate_group_count=len(groups))
    return {"groups": groups, "provenance": provenance}
