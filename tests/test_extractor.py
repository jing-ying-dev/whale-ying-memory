# -*- coding: utf-8 -*-
"""Offline unit tests for the extractor's deterministic, model-free functions.

These tests need no API key and no network. They exercise the gates a stranger
would want to trust before running the extractor on their own data:
schema/rules consistency, chunk splitting, quote-substring enforcement, and the
end-to-end assertion list coverage.
"""
import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = REPO_ROOT / "extractor"
if str(EXTRACTOR) not in sys.path:
    sys.path.insert(0, str(EXTRACTOR))

import extract  # noqa: E402


class TestSchemaAndRules(unittest.TestCase):
    """schema.json and extract_rules.json must agree, both ways."""

    @classmethod
    def setUpClass(cls):
        cls.schema = extract.load_json(str(EXTRACTOR / "schema.json"), "schema")
        cls.rules = extract.load_rules(cls.schema, str(EXTRACTOR / "extract_rules.json"))

    def test_rules_consistency_passes(self):
        self.assertTrue(getattr(self.rules, "report", {}).get("schema_rules_consistent"))

    def test_every_entity_has_file_key(self):
        for ent_name, ent in (self.schema.get("entities") or {}).items():
            self.assertTrue(ent.get("file_key"), "entity %r lacks file_key" % ent_name)


class TestChunkSplit(unittest.TestCase):
    def test_short_text_is_one_piece(self):
        self.assertEqual(extract.split_chunk("hello", limit=1200), [(0, 5)])

    def test_long_text_splits_without_dropping_chars(self):
        text = "a" * 3000
        spans = extract.split_chunk(text, limit=1200)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(text))
        # concatenation of the pieces reconstructs the original exactly
        self.assertEqual("".join(text[a:b] for a, b in spans), text)

    def test_split_respects_limit(self):
        text = "x" * 2500
        spans = extract.split_chunk(text, limit=1200)
        self.assertTrue(all(b - a <= 1200 for a, b in spans))


class TestEventHash(unittest.TestCase):
    def test_stable_and_distinct(self):
        h1 = extract.event_hash("chunk-a", 0)
        h2 = extract.event_hash("chunk-a", 0)
        h3 = extract.event_hash("chunk-a", 1)
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(len(h1), 16)


class TestQuoteSubstring(unittest.TestCase):
    """quote must be a contiguous substring of the source text."""

    @classmethod
    def setUpClass(cls):
        cls.schema = extract.load_json(str(EXTRACTOR / "schema.json"), "schema")
        cls.rules = extract.load_rules(cls.schema, str(EXTRACTOR / "extract_rules.json"))

    def test_valid_quote_has_no_violations(self):
        text = "In March 2025, Maya joined Northstar Labs as a research assistant."
        entities = {"cards": [{"action": "joined", "result": "research assistant",
                               "quote": {"原文": "Maya joined Northstar Labs"}}]}
        viol = extract.quote_substring_violations(entities, self.rules, text)
        self.assertEqual(viol, [])

    def test_non_substring_quote_is_flagged(self):
        text = "In March 2025, Maya joined Northstar Labs."
        entities = {"cards": [{"action": "joined", "result": "research assistant",
                               "quote": {"原文": "Maya joined a completely different place"}}]}
        viol = extract.quote_substring_violations(entities, self.rules, text)
        self.assertTrue(viol, "a non-substring quote must be reported")
        self.assertTrue(any(v.get("anchor") for v in viol))


class TestE2EAsserts(unittest.TestCase):
    """The shipped e2e_asserts.json must load and cover every query-side field."""

    def test_load_and_coverage(self):
        asserts = extract.load_e2e_asserts()
        self.assertGreaterEqual(len(asserts), 1)
        cov = extract.check_e2e_field_coverage(asserts)
        self.assertTrue(cov["ok"], "query-side contract fields uncovered: %s" % cov)


if __name__ == "__main__":
    unittest.main(verbosity=2)
