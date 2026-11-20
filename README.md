# WhaleYing Memory

**Structured long-term memory for AI agents, built on DeepSeek — with conflict resolution and multi-hop retrieval.**

WhaleYing extracts facts, events, metrics, and identities from conversations or long documents. It tracks updates through supersession chains, retrieves current evidence with hybrid search, and supplies that evidence to an LLM for answering.

The name: **Whale** is DeepSeek's mascot (the system is built on the DeepSeek API) and evokes deep, long-term memory; **Ying** is the author's name.

The system is designed for questions where long-context inputs contain conflicting versions of a fact or require multiple retrieval steps.

## Results

The following results are for the current system: four-table extraction, temporal supersession and folding, hybrid retrieval, and Self-Ask-style iteration. Conflict-resolution scores use **MemoryAgentBench · Conflict_Resolution**.

| Task | WhaleYing | Reported comparison |
|---|---:|---|
| Single-hop, 262k, substring scoring | **93.2%** (gpt-4o-mini answer model; mean of 5 runs) | GPT-4o: 60%; BM25: 56%; gpt-4o-mini: 45% |
| Single-hop, 262k, substring scoring | **93.0%** (DeepSeek answer model, thinking disabled; single run) | — |
| Single-hop, 262k, substring scoring | **92.0%** (local Qwen2.5-Coder 7B answer model; single run) | — |
| Single-hop, 6k, substring scoring | **98%** (DeepSeek answer model, thinking disabled; single run) | — |
| Multi-hop, 262k, substring scoring | **48.3%** (DeepSeek answer model, thinking disabled; mean of 3 runs) | Paper’s BM25 3% · HippoRAG-v2 5% · Mem0 2% · MemGPT 3% · CAR (gpt-4o) 41% |
| Multi-hop, 262k | **25.3%** (gpt-4o-mini answer model; mean of 3 runs) | — |
| Multi-hop, 262k, direct full-context reading | — | DeepSeek without the memory system: 5.3% |
| Multi-hop, 6k, substring scoring | **66%** (65% exact; DeepSeek answer model, thinking disabled; single run) | — |

For the reported 262k comparisons, single-hop performance rose from 64% direct to 93.2% with memory; multi-hop rose from 5.3% direct to 48.3% with memory. Single-hop accuracy is stable across answer models — gpt-4o-mini (93.2%), DeepSeek with thinking disabled (93.0%), and a local Qwen2.5-Coder 7B (92.0%) all land in the same band. These are benchmark results under the stated configurations, not a guarantee of performance on other workloads. Answer-model temperature was 0.7 for all runs. Single-hop baselines are from MemoryAgentBench (arXiv 2507.05257); the 262k multi-hop baselines (BM25, HippoRAG-v2, Mem0, MemGPT) and CAR are as reported in arXiv 2606.01435.

## LoCoMo results

LoCoMo (long-conversation memory QA) uses the official F1 string-match metric and official QA prompt, with the DeepSeek answer model (temperature 0, thinking disabled). Two configurations are reported — we show both because the default conflict-resolution folding is the wrong tool for this temporal benchmark (see Limitations below):

| LoCoMo task (1,986 questions) | No folding (BM25 only) | Full config (folding + BM25 + vector + RRF) |
|---|---:|---:|
| Multi-hop | 19.9% | 20.4% |
| Single-hop | 28.3% | 30.1% |
| Temporal | 13.9% | 15.3% |
| Open-domain | 43.9% | 38.9% |
| Adversarial / abstention | 53.4% | 50.4% |
| **Overall F1** | **38.6%** | **36.3%** |

## Limitations (stated honestly)

- **LoCoMo is temporal** — its questions ask about the *past* — but the default folding keeps only the latest value of each fact. Folding is the right tool for conflict resolution (MemoryAgentBench FactConsolidation), the wrong tool for "what happened before". That is why the no-folding configuration scores higher on LoCoMo.
- **Temporal extraction is the weakest link** (13.9–15.3%): the extractor's parsing of relative time ("a few days later", "yesterday") is a known weakness.
- The LoCoMo numbers are **single runs**, not multi-run means, so they are less stable than the 5-run Conflict_Resolution numbers above.
- LoCoMo's questions mostly depend on the most recent 1–2 sessions (as other work has noted), so it does not strongly probe long-horizon memory.

## How it works

1. **Extract.** An LLM converts source text into four tables: `facts` for assertions, `cards` for events, `metrics` for measurements, and `identities` for entities. Deterministic validation checks the output, and citations must be substrings of the source.
2. **Resolve conflicts.** Supersession chains track versions of the same subject. Older values are marked with `invalidated_at`; folding retains the latest version by sequence number.
3. **Retrieve.** Multi-field BM25 and `all-MiniLM-L6-v2` vector search are combined using reciprocal rank fusion (RRF). Database embeddings are cached.
4. **Answer.** Retrieved evidence is passed to an LLM. For multi-hop questions, optional Self-Ask-style iteration decomposes the question into single-hop subquestions, retrieves evidence at each step, and builds an answer from the evidence chain.

## Installation / Quick Start

**Requirements:** Python 3.10+ and a DeepSeek API key for extraction. The answer model can use an OpenAI-compatible API, such as DeepSeek or OpenAI.

```bash
pip install rank-bm25 sentence-transformers python-dotenv
export DEEPSEEK_API_KEY="your-api-key"
```

The extractor itself uses the Python standard library and the project’s `deepseek_api` adapter; the packages above support the retrieval system.

To enable iterative multi-hop answering, set:

```bash
export WHALE_YING_ITERATIVE=1
```

Iterative answering is off by default. The workflow is **extract → fold superseded values → retrieve → answer**. See `extractor/extract.py` for the extraction flow and `memory/whale_ying.py` for retrieval, folding, and answering.

Extraction sends source text to the configured LLM API. Review your provider’s data handling before processing sensitive material.

## Project structure

```text
extractor/
├── extract.py          # Extraction flow
├── deepseek_api.py     # LLM adapter
├── schema.json         # Four-table schema
└── extract_rules.json  # Validation rules

memory/
└── whale_ying.py       # Retrieval, folding, and multi-hop answering
```

## License

Apache-2.0.