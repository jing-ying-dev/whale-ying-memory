# -*- coding: utf-8 -*-
"""Standalone CLI for WhaleYing Memory.

Lets the repository run end-to-end (build a store from documents, then ask questions)
without MemoryAgentBench. It reuses the extractor (extractor/extract.py) and the memory
adapter (memory/whale_ying.py), supplying only the two small pieces the benchmark normally
provides:

  * an OpenAI-compatible answer client (agent._create_oai_client)
  * retrieval-query extraction (agent._extract_retrieval_query)

The answer model reads the standard OpenAI environment variables (OPENAI_API_KEY /
OPENAI_BASE_URL / OPENAI_MODEL); extraction and reranking read the DeepSeek configuration
(DEEPSEEK_API_KEY / DEEPSEEK_API_BASE / DEEPSEEK_MODEL, see extractor/deepseek_api.py).
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "memory", REPO_ROOT / "extractor"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import whale_ying  # noqa: E402


class StandaloneAgent:
    """Minimal agent object providing the hooks whale_ying expects from MemoryAgentBench.

    whale_ying.initialize_our_agent() fills in the extractor schema/rules and the
    per-agent state (rows, pending chunks, retrieval cache); this class only carries the
    answer-model configuration and the two benchmark-supplied methods.
    """

    def __init__(self, answer_model=None, temperature=0.7, max_tokens=300,
                 retrieve_num=10, base_url=None, api_key=None):
        self.model = answer_model or _env_first("OPENAI_MODEL", "gpt-4o-mini")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.retrieve_num = retrieve_num
        self.sub_dataset = "factconsolidation"
        self.agent_name = "Long_context_agent"
        self._base_url = base_url or _env_first("OPENAI_BASE_URL", None)
        self._api_key = api_key or _env_first("OPENAI_API_KEY", None)

    def _create_oai_client(self):
        from openai import OpenAI
        kwargs = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return OpenAI(**kwargs)

    @staticmethod
    def _extract_retrieval_query(message):
        # Same regex the benchmark agent uses; a plain question has no such prefix and
        # falls through unchanged.
        for pattern in (r"Now Answer the Question:\s*(.*)",
                        r"Here is the conversation:\s*(.*)"):
            m = re.search(pattern, message, re.DOTALL)
            if m:
                return "".join(m.groups())
        return message


def _env_first(name, default):
    v = __import__("os").environ.get(name)
    return v if v else default


def build_store(doc_paths, out_path, chunk_chars=0):
    """Extract structured memory from one or more documents and save the store.

    The store is the raw `agent.rows` list (unfolded); folding happens at query time,
    matching the benchmark adapter's WHALE_YING_STORE_SAVE semantics.
    """
    agent = StandaloneAgent()
    whale_ying.initialize_our_agent(agent)
    for i, path in enumerate(doc_paths):
        text = Path(path).read_text(encoding="utf-8")
        if chunk_chars and chunk_chars > 0:
            for j in range(0, len(text), chunk_chars):
                agent._pending_chunks.append(
                    {"context_id": i, "seq": agent._chunk_seq, "text": text[j:j + chunk_chars]})
                agent._chunk_seq += 1
        else:
            agent._pending_chunks.append(
                {"context_id": i, "seq": agent._chunk_seq, "text": text})
            agent._chunk_seq += 1
    whale_ying._extract_pending(agent)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(agent.rows, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    return len(agent.rows)


def ask(store_path, question, iterative=False, answer_model=None, temperature=0.7,
        retrieve_num=10):
    """Load a store, retrieve evidence, and answer one question.

    Returns (answer_text, input_tokens, output_tokens). The answer may be empty when the
    answer model is unreachable or the question cannot be answered; errors go to stderr.
    """
    agent = StandaloneAgent(answer_model=answer_model, temperature=temperature,
                            retrieve_num=retrieve_num)
    whale_ying.initialize_our_agent(agent, {"retrieve_num": agent.retrieve_num})
    agent.rows = json.loads(Path(store_path).read_text(encoding="utf-8"))
    rows = whale_ying._fold_latest(agent.rows)
    retrieval_query = agent._extract_retrieval_query(question)
    final_rows = []
    if rows:
        order = whale_ying._hybrid_retrieve(agent, retrieval_query, rows)
        k = min(agent.retrieve_num, len(order))
        topk_rows = [rows[i] for i in order[:k]]
        final_rows = whale_ying._llm_rerank(agent, retrieval_query, topk_rows)
    if iterative and rows:
        return whale_ying._iterative_answer(agent, question, retrieval_query, rows,
                                            final_rows)
    return whale_ying._direct_answer(agent, question, final_rows)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="whaleying",
                                 description="WhaleYing Memory standalone CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ingest = sub.add_parser("ingest", help="build a memory store from documents")
    p_ingest.add_argument("--doc", required=True, action="append",
                          help="document file (repeatable)")
    p_ingest.add_argument("--out", required=True, help="output store JSON path")
    p_ingest.add_argument("--chunk-chars", type=int, default=0,
                          help="split each doc into chunks of this many chars (0 = one chunk per doc)")

    p_ask = sub.add_parser("ask", help="answer a question from a store")
    p_ask.add_argument("--store", required=True, help="store JSON path")
    p_ask.add_argument("--question", required=True, help="question to answer")
    p_ask.add_argument("--iterative", action="store_true", help="enable Self-Ask multi-hop iteration")
    p_ask.add_argument("--model", default=None, help="answer model (default OPENAI_MODEL or gpt-4o-mini)")
    p_ask.add_argument("--temperature", type=float, default=0.7)
    p_ask.add_argument("--retrieve-num", type=int, default=10)

    a = ap.parse_args(argv)
    if a.cmd == "ingest":
        n = build_store(a.doc, a.out, chunk_chars=a.chunk_chars)
        print("store saved: %d rows -> %s" % (n, a.out))
        return 0
    if a.cmd == "ask":
        answer, in_tok, out_tok = ask(a.store, a.question, iterative=a.iterative,
                                      answer_model=a.model, temperature=a.temperature,
                                      retrieve_num=a.retrieve_num)
        print(answer)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
