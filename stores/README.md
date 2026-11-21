# Prebuilt stores

Two prebuilt memory stores are shipped so you can reproduce the headline
benchmark numbers without re-running the (expensive) extraction stage. Load one
of these, then run retrieval + answering directly.

## What is here

| File | Rows | Tables | Backs which number |
|---|---|---|---|
| `our_system_store_262k.json` | 18,124 | facts 18,122 · identities 2 | Single-hop **93.2%** |
| `our_system_store_mh262k.json` | 17,904 | facts 17,890 · metrics 1 · cards 5 · identities 8 | Multi-hop **48.3%** |

Each row is one structured record from the extractor (four-table output), plus
supersession bookkeeping (`_version`, `_item_key`, `valid_from`, `valid_to`,
`invalidated_at`) that `memory/whale_ying.py` folds at query time.

## Provenance and license

The facts in these stores are extracted from the
[MemoryAgentBench](https://github.com/yyhugo/MemoryAgentBench) Conflict_Resolution
dataset (MIT License, © 2026 Yuanzhe Hu). The `quote.原文` field is a verbatim
substring of that dataset's source text. Redistribution is permitted under MIT
with this attribution.

## Extraction and retrieval figures (including timing)

- **Extraction** (both stores use DeepSeek-flash, `disable_thinking=True`):
  - single-hop store: 67 chunks, 18,124 rows. Re-extracted 2026-11-21 for timing:
    ~84 minutes end-to-end (serial, ~75 s/chunk). An earlier journal note
    ("reuse saves 86 minutes") matches this serial figure.
  - multi-hop store: 67 chunks → 870 pieces, 8-way concurrent extraction,
    ~11.5 minutes (recorded 2026-11-20, see journal `1120-2`).
  - The single-hop vs multi-hop timing gap is **serial vs 8-way concurrent**
    extraction, not the thinking toggle (both disable thinking).
- **Answering** (gpt-4o-mini or DeepSeek, temperature 0.7):
  - 100 single-hop questions: ~4 minutes.
  - 100 multi-hop questions: ~15–25 minutes (Self-Ask iteration).
- **Retrieval** (hybrid BM25 + `all-MiniLM-L6-v2` vectors + RRF):
  - before vector caching: ~30–60 s per question (whole library re-encoded each time);
  - after caching: build once (~42.9 s for 16,000 rows), then ~0.032 s per question
    (~1,340× speedup). Whole 262k run went from ~60 min to ~16.5 min.
  - rerank: ~1.2 s; a single answer call: ~1.3 s.

## How to use

```bash
# Answer a question against the prebuilt single-hop store
python examples/answer.py --store stores/our_system_store_262k.json \
  --question "Who is the author of Dianetics?"

# Multi-hop with iteration
python examples/answer.py --store stores/our_system_store_mh262k.json \
  --question "..." --iterative
```

The `--store` path accepts any of the JSON files above; `examples/answer.py`
folds, retrieves, reranks, and answers without needing `DEEPSEEK_API_KEY` for
extraction (reranking still reads `DEEPSEEK_API_KEY`; answering reads
`OPENAI_API_KEY` / `OPENAI_MODEL`).
