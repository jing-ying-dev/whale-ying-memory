# Results

Raw benchmark run outputs backing the README numbers. Every score below is
**substring exact match** on **MemoryAgentBench · Conflict_Resolution**.

## Directory layout

```
results/
├── multihop-262k/        # 3 runs × {deepseek, gpt-4o-mini, direct full-context}
├── singlehop-262k/       # 5 runs (gpt-4o-mini answer model)
└── README.md
```

## Verified numbers

### Multi-hop 262k (`multihop-262k/`)

| Answer model | Substring exact match (per run) | Mean |
|---|---|---|
| DeepSeek | 48 / 48 / 49 | **48.3%** |
| gpt-4o-mini | 26 / 25 / 25 | **25.3%** |
| Direct full context | 5 / 5 / 6 | **5.3%** |

### Single-hop 262k (`singlehop-262k/`)

Five runs with the gpt-4o-mini answer model: 93 / 93 / 93 / 94 / 93 →
**93.2% mean** (exact match mean 91.0%).

One additional run (86%) was excluded because a mobile-hotspot connection
dropped during store extraction and left the memory store incomplete; the file is
not included here. Re-extraction over a stable connection returned the 93–94% band
(see the README for the full note).

## How to read a result file

Each JSON contains:

- `agent_config` / `dataset_config` — model, temperature, dataset, and context limits.
- `data` — one entry per question (`query`, `answer` = gold label, `parsed_output`
  = model answer, plus `substring_exact_match` / `exact_match` / `f1` per entry).
- `averaged_metrics` — the per-run aggregate.

`max_test_samples: 1` means **one long document containing 100 question-answer
pairs** (the 262k-context sample), not one question. The `data` array therefore has
100 entries per file.

## Provenance and license

The question text (`query`) and gold labels (`answer`) come from the
[MemoryAgentBench](https://github.com/yyhugo/MemoryAgentBench) dataset
(Conflict_Resolution), which is released under the MIT License
(Copyright (c) 2026 Yuanzhe Hu). The model outputs (`parsed_output`) and the
extraction/retrieval configuration are our own.
