import math

import pytest

from enigram.domain import stats


def test_doc_id_strips_lightrag_chunk_suffix():
    assert stats.doc_id("2601.13754v1-c00012-chunk-000") == "2601.13754v1-c00012"
    assert stats.doc_id("p-c00001") == "p-c00001"


def test_context_recall():
    assert stats.context_recall({"a", "b"}, ["a", "c"]) == 0.5
    assert stats.context_recall(set(), ["a"]) is None


def test_context_precision_is_average_precision_over_ranks():
    assert stats.context_precision({"a", "b"}, ["c", "a", "b"]) == pytest.approx(
        (1 / 2 + 2 / 3) / 2
    )
    assert stats.context_precision({"a"}, ["b", "c"]) == 0.0
    assert stats.context_precision(set(), ["a"]) is None


def test_cohen_kappa_hand_computed():
    judge = [True] * 4 + [False, True] + [False] * 4
    human = [True] * 4 + [True, False] + [False] * 4
    pairs = list(zip(judge, human, strict=True))
    # observed 0.8, expected 0.5 -> (0.8 - 0.5) / 0.5
    assert stats.cohen_kappa(pairs) == pytest.approx(0.6)
    assert stats.accuracy(pairs) == 0.8


def test_cohen_kappa_degenerate_and_empty():
    assert stats.cohen_kappa([(True, True)] * 5) == 0.0
    assert stats.cohen_kappa([]) is None
    assert stats.accuracy([]) is None


def test_auroc():
    assert stats.auroc([0.9, 0.8, 0.3, 0.1], [True, False, True, False]) == 0.75
    assert stats.auroc([0.5, 0.5], [True, False]) == 0.5
    assert stats.auroc([0.9, 0.1], [True, True]) is None


def test_cv_threshold_separable_scores():
    scores = [0.9, 0.85, 0.8, 0.75, 0.7, 0.35, 0.3, 0.25, 0.2, 0.15]
    labels = [True] * 5 + [False] * 5
    result = stats.cv_threshold(scores, labels)
    assert 0.35 < result["threshold"] < 0.7
    assert result["accuracy"] == 1.0
    assert result["kappa"] == 1.0
    assert result["reason"] is None


def test_cv_threshold_needs_enough_labels_of_both_classes():
    assert stats.cv_threshold([0.9, 0.1], [True, False])["threshold"] is None
    one_class = stats.cv_threshold([0.9] * 6, [True] * 6)
    assert one_class["threshold"] is None and "both classes" in one_class["reason"]


def test_mean_excludes_missing_values():
    assert stats.mean([1.0, None, 0.0]) == (0.5, 2, 1)
    assert stats.mean([math.nan, None]) == (None, 0, 2)
