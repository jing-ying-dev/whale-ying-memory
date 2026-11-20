# -*- coding: utf-8 -*-
"""Offline smoke tests for the README Quick Start path and the standalone CLI.

These run `extract.py --dry-run` on the shipped sample chunk and import the
standalone CLI module — no model call, no API key, no network.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


class TestQuickStartDryRun(unittest.TestCase):
    """README step 3 must at least dry-run cleanly on the shipped sample chunk."""

    def test_dry_run_exits_zero(self):
        proc = subprocess.run(
            [PY, str(REPO_ROOT / "extractor" / "extract.py"),
             "--chunk", str(REPO_ROOT / "examples" / "sample_chunk.json"),
             "--out", str(REPO_ROOT / "out" / "smoke_entities.json"),
             "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT))
        self.assertEqual(proc.returncode, 0, "dry-run failed: %s" % proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertIn("prompt_len", summary)
        self.assertEqual(summary["chunk_chars"], 167)


class TestOfflineReplay(unittest.TestCase):
    """Run the full extraction pipeline offline via --replay (no model call).

    The replay fixture is a minimal, valid model response for the shipped sample
    chunk: one card whose quote is a substring of the source. This exercises
    validate -> quote substring gate -> traceability -> e2e, the same path a real
    extraction takes, without any API key or network.
    """

    def test_replay_extraction_exits_zero_and_produces_one_card(self):
        out = REPO_ROOT / "out" / "replay_entities.json"
        logdir = REPO_ROOT / "out" / "replay_log"
        proc = subprocess.run(
            [PY, str(REPO_ROOT / "extractor" / "extract.py"),
             "--chunk", str(REPO_ROOT / "examples" / "sample_chunk.json"),
             "--replay", str(REPO_ROOT / "tests" / "fixtures" / "replay_minimal.json"),
             "--out", str(out), "--log-dir", str(logdir)],
            capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT))
        self.assertEqual(proc.returncode, 0, "replay extraction failed: %s" % proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["counts"]["cards"], 1)
        self.assertTrue(summary["quote_substring_gate"]["gate_final"]["ok"])
        self.assertTrue(summary["traceability_gate"]["ok"])


class TestStandaloneImport(unittest.TestCase):
    def test_standalone_module_imports_and_agent_constructs(self):
        sys.path.insert(0, str(REPO_ROOT))
        from whaleying.standalone import StandaloneAgent, build_store, ask  # noqa: E402
        a = StandaloneAgent()
        self.assertEqual(a._extract_retrieval_query("What is X?"), "What is X?")
        self.assertTrue(callable(build_store))
        self.assertTrue(callable(ask))

    def test_cli_help_exits_zero(self):
        for script in ("examples/build_store.py", "examples/answer.py"):
            proc = subprocess.run(
                [PY, str(REPO_ROOT / script), "--help"],
                capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT))
            self.assertEqual(proc.returncode, 0, "%s --help failed: %s" % (script, proc.stderr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
