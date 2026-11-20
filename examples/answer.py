# -*- coding: utf-8 -*-
"""Answer a question against a prebuilt WhaleYing memory store.

Usage:
    python examples/answer.py --store memory.json --question "What is the current value of X?"
    python examples/answer.py --store memory.json --question "..." --iterative
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whaleying.standalone import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main(["ask"] + sys.argv[1:]))
