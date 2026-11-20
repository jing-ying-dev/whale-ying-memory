# -*- coding: utf-8 -*-
"""Offline unit tests for the memory adapter's deterministic folding and retrieval helpers.

No API key or network is needed: these functions fold rows, tokenize text, and build
keys without calling any model.
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY = REPO_ROOT / "memory"
if str(MEMORY) not in sys.path:
    sys.path.insert(0, str(MEMORY))

import whale_ying  # noqa: E402


def _metric_row(name, value, version, invalidated_at=None):
    return {"table": "metrics", "指标名": name, "值": value, "单位": "req/s",
            "_item_key": "metric:" + name, "_version": version,
            "invalidated_at": invalidated_at}


class TestFoldLatest(unittest.TestCase):
    def test_keeps_only_latest_version(self):
        rows = [
            _metric_row("throughput", 30, 0, invalidated_at=1),
            _metric_row("throughput", 37, 1),
        ]
        folded = whale_ying._fold_latest(rows)
        values = [r["值"] for r in folded if r.get("table") == "metrics"]
        self.assertEqual(values, [37])

    def test_invalidated_latest_is_dropped_when_a_newer_one_exists(self):
        # two versions, the newer one invalidated -> older (valid) survives
        rows = [
            _metric_row("throughput", 30, 0),
            _metric_row("throughput", 40, 1, invalidated_at=2),
        ]
        folded = whale_ying._fold_latest(rows)
        values = [r["值"] for r in folded if r.get("table") == "metrics"]
        self.assertEqual(values, [30])

    def test_distinct_keys_are_both_kept(self):
        rows = [
            _metric_row("throughput", 37, 0),
            _metric_row("latency", 5, 0),
        ]
        folded = whale_ying._fold_latest(rows)
        names = sorted(r["指标名"] for r in folded if r.get("table") == "metrics")
        self.assertEqual(names, ["latency", "throughput"])


class TestTokenize(unittest.TestCase):
    def test_drops_stopwords_and_short_tokens(self):
        toks = whale_ying._tokenize("the cat is on the mat")
        # "the"/"is"/"on" are stopwords; "cat"/"mat" survive
        self.assertIn("cat", toks)
        self.assertIn("mat", toks)
        self.assertNotIn("the", toks)

    def test_lowercases_and_keeps_identifiers(self):
        toks = whale_ying._tokenize("Throughput-v2 metric_name")
        self.assertIn("throughput-v2", toks)
        self.assertIn("metric_name", toks)


class TestItemKey(unittest.TestCase):
    def test_metric_key(self):
        self.assertEqual(whale_ying._item_key({"指标名": "x"}, "metrics"), "metric:x")

    def test_fact_key_is_subject_based(self):
        self.assertEqual(whale_ying._item_key({"等式": "maya works at northstar"}, "facts"),
                         "fact:maya|works at")


if __name__ == "__main__":
    unittest.main(verbosity=2)
