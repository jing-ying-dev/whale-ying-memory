# Reproducing the benchmark results

This document explains how the MemoryAgentBench · Conflict_Resolution numbers in the
README were produced, so a stranger can reproduce them. Two paths are available:

- **Standalone CLI** (no benchmark) — build a store from your own documents and ask
  questions. See the README Quick Start, section 4.
- **Full benchmark reproduction** — run WhaleYing against MemoryAgentBench to reproduce
  the published scores.

## Full benchmark reproduction

### Prerequisites

- Python 3.10+
- A DeepSeek API key (for extraction and reranking)
- An OpenAI-compatible answer endpoint (gpt-4o-mini, DeepSeek, or a local model)

### 1. Get MemoryAgentBench

```bash
git clone https://github.com/yyhugo/MemoryAgentBench.git
cd MemoryAgentBench
python -m pip install -r requirements.txt
```

The Conflict_Resolution dataset is loaded from HuggingFace (`ai-hyz/MemoryAgentBench`).
Note: this repository does not pin a MemoryAgentBench commit SHA — the local evaluation
ran against a checkout dated 2026-11; if the upstream interface changed, treat the
adapter in `memory/whale_ying.py` as the reference integration point.

### 2. Copy the WhaleYing adapter into the benchmark

`memory/whale_ying.py` implements the benchmark hooks `initialize_our_agent(...)` and
`handle_our_agent(...)`. The benchmark's `agent.py` must be pointed at these hooks, and
the agent config must select the "our_system" agent type.

The two configuration files used for the published runs are equivalent to:

```yaml
# agent config (answer model gpt-4o-mini, temperature 0.7)
agent_name: Agentic_memory_our_system
model: gpt-4o-mini
temperature: 0.7
```

```yaml
# dataset config
dataset: Conflict_Resolution
chunk_size: 4096
seed: 42
context_max_length: 300000
sub_dataset: factconsolidation_sh_262k   # or factconsolidation_mh_262k
max_test_samples: 1
shots: 0
```

### 3. Run

```bash
# Single-hop, 262k
python main.py \
  --agent_config configs/model_conf/our_system_gpt.yaml \
  --dataset_config configs/data_conf/Conflict_Resolution/Factconsolidation_sh_262k.yaml

# Multi-hop, 262k (DeepSeek answer model)
python main.py \
  --agent_config configs/model_conf/our_system_deepseek.yaml \
  --dataset_config configs/data_conf/Conflict_Resolution/Factconsolidation_mh_262k.yaml
```

Each run writes a JSON under `outputs/<agent>/Conflict_Resolution/`. The metric reported
in the README is **substring exact match** (`averaged_metrics.substring_exact_match`).

### 4. Verify the published numbers

The raw run outputs are committed under [`results/`](../results/):

- `results/singlehop-262k/` — the five gpt-4o-mini single-hop runs → mean 93.2%
- `results/multihop-262k/` — DeepSeek / gpt-4o-mini / direct full-context, 3 runs each

Recompute the means with:

```bash
python -c "import json,glob,statistics as s; \
  files=glob.glob('results/singlehop-262k/*.json'); \
  v=[json.load(open(f))['averaged_metrics']['substring_exact_match'] for f in files]; \
  print(sorted(v), s.mean(v))"
```

## Environment variables

- `DEEPSEEK_API_KEY` / `DEEPSEEK_API_BASE` / `DEEPSEEK_MODEL` — extraction and reranking
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` — the answer model
- `WHALE_YING_ITERATIVE=1` — enable Self-Ask multi-hop iteration in the adapter
- `WHALE_YING_STORE_SAVE` / `WHALE_YING_STORE_LOAD` — split extraction and evaluation
  into two processes (avoids re-extracting the store per answer model)

## Known gap

The LoCoMo numbers in the README are cited from the official LoCoMo evaluation and are
not bundled here: the LoCoMo data is CC BY-NC 4.0 (non-commercial), so its data files are
not redistributed. Reproduce them against the upstream [snap-research/LoCoMo](https://github.com/snap-research/LoCoMo).
