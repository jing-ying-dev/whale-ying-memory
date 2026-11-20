## Architecture

WhaleYing Memory is a long-term memory system for AI agents. It turns conversations or long documents into structured records, resolves superseded values, retrieves relevant evidence, and supplies that evidence to an LLM for answering.

### 1. Extraction and data model

The extractor uses an LLM to produce four tables:

| Table | Contents |
|---|---|
| `facts` | Declarative assertions, such as “X is Y” |
| `cards` | Event records describing what happened |
| `metrics` | Measurements with values and units |
| `identities` | Records identifying entities |

Extraction includes deterministic validation. Each quotation must be a substring of the original text.

### 2. Supersession and fold

When there are multiple versions for the same subject, the supersession mechanism orders them by sequence number. Older values are marked invalid with `invalidated_at`; the **fold** operation retains the latest version.

This gives conflict resolution an explicit notion of which value supersedes another, rather than treating retrieved text chunks as equally current.

### 3. Retrieval

The retriever combines:

- Multi-field BM25 retrieval.
- Vector retrieval using `all-MiniLM-L6-v2`.
- Reciprocal Rank Fusion (RRF) to combine rankings.

The system folds superseded records to select current values during retrieval. Database-wide vectors are built once and cached; the reported retrieval speedup from caching was approximately 1,340× in the measured test.

### 4. Multi-hop answering

For multi-hop questions, the iterative answerer uses a Self-Ask-style process:

1. Split the question into single-hop subquestions.
2. Retrieve evidence for each hop.
3. Assemble an evidence chain and produce the final answer.

`WHALE_YING_ITERATIVE` switches iterative answering on or off. The default is single-hop answering.

## Glossary

| Term | Meaning |
|---|---|
| **BM25** | A text-retrieval method used here across multiple fields. |
| **cards** | The table of event records: what happened. |
| **Extractor** | The component that converts conversations or long documents into the four structured tables. |
| **facts** | The table of declarative assertions, such as “X is Y.” |
| **fold** | The operation that retains the latest version in a supersession chain. |
| **identities** | The table recording who entities are. |
| **`invalidated_at`** | The field used to mark an older, superseded value as invalid. |
| **metrics** | The table of measurements with values and units. |
| **Multi-hop** | Answering that requires a chain of intermediate questions or evidence rather than one retrieval step. |
| **RRF** | Reciprocal Rank Fusion, used to combine the BM25 and vector retrieval rankings. |
| **Self-Ask** | An approach that breaks a question into subquestions; WhaleYing Memory retrieves evidence hop by hop before answering. |
| **Supersession** | The versioning mechanism that orders values for the same subject, invalidates older values, and identifies the latest one. |
| **Vector retrieval** | Retrieval using vectors generated with `all-MiniLM-L6-v2`; database-wide vectors are cached. |
