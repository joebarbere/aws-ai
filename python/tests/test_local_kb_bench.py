"""The bench's own arithmetic: ground truth, recall, overlap, percentiles, the vector cache."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from aws_ai.local_kb.bench import VectorCache, _percentiles, _recall, exact_top_k, overlap
from aws_ai.local_kb.bench_resources import _bytes, _delta


def unit(a: np.ndarray) -> np.ndarray:
    return a / np.linalg.norm(a, axis=1, keepdims=True)


def test_exact_top_k_is_brute_force_cosine_order() -> None:
    rng = np.random.default_rng(7)
    matrix, queries = unit(rng.normal(size=(300, 16))), unit(rng.normal(size=(5, 16)))
    got = exact_top_k(queries, matrix, 10)
    for q, row in zip(queries, got, strict=True):
        assert row == list(np.argsort(-(matrix @ q))[:10])


def test_exact_top_k_within_rows_returns_original_row_numbers() -> None:
    rng = np.random.default_rng(8)
    matrix, queries = unit(rng.normal(size=(100, 8))), unit(rng.normal(size=(3, 8)))
    rows = np.arange(7, 100, 50)  # 7 and 57: fewer rows than k
    got = exact_top_k(queries, matrix, 10, rows)
    assert all(sorted(r) == [7, 57] for r in got)


def test_recall_overlap_and_percentiles() -> None:
    assert _recall([[1, 2, 3], [4, 5, 9]], [[1, 2, 3], [4, 5, 6]], 3) == pytest.approx(
        0.8333, abs=1e-4
    )
    assert _recall([[1]], [[1]], 10) == 1.0  # fewer true neighbours than k
    assert overlap([[1, 2], [3, 4]], [[1, 2], [4, 5]]) == {"jaccard": 0.6667, "same_first": 0.5}
    p = _percentiles([i / 1000 for i in range(1, 101)])  # 1..100 ms
    assert (p["calls"], p["p50_ms"], p["p95_ms"]) == (100, 51.0, 95.0)


def test_vector_cache_embeds_each_text_once(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[float(len(t)), 1.0] for t in texts]

    cache = VectorCache(tmp_path / "v.sqlite", "m", 2)
    first = cache.get(["a", "bb", "a"], embed)
    assert calls == [["a", "bb"]]
    again = VectorCache(tmp_path / "v.sqlite", "m", 2).get(["bb", "ccc"], embed)
    assert calls[-1] == ["ccc"]
    assert first.dtype == np.float32 and first.tolist() == [[1, 1], [2, 1], [1, 1]]
    assert again.tolist() == [[2, 1], [3, 1]]
    assert VectorCache(tmp_path / "v.sqlite", "other-model", 2).get(["a"], embed).tolist() == [
        [1, 1]
    ]
    assert calls[-1] == ["a"]  # a different model is a different cache


def test_podman_units() -> None:
    assert _bytes("31.13MB") == pytest.approx(31.13e6)
    assert _bytes("2GiB") == 2 * 2**30
    assert _bytes("512kB") == 512e3
    assert _delta(1.5, 4.0) == 2.5 and _delta(None, 4.0) is None
