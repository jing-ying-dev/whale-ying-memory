# -*- coding: utf-8 -*-
"""Build a WhaleYing memory store from one or more documents.

Usage:
    python examples/build_store.py --doc document.txt --out memory.json
    python examples/build_store.py --doc a.txt --doc b.txt --out memory.json --chunk-chars 4000
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whaleying.standalone import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main(["ingest"] + sys.argv[1:]))
