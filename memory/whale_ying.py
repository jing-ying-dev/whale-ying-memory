# -*- coding: utf-8 -*-
"""
whale_ying — a thin adapter connecting the memory system (extractor) to MemoryAgentBench.

1. memorize: accumulate chunks fed in by the benchmark, then hand them to the extractor
   concurrently (extract()) on the first query (chunks are independent and can run in
   parallel; previously they were processed serially).
2. query: fold to the latest values (_fold_latest), BM25 + vector + RRF hybrid retrieval,
   then LLM reranking (RankGPT-style listwise, following sunnweiwei/RankGPT's
   permutation_pipeline), which is fed into the prompt for gpt-4o-mini to answer.
3. No smuggling: no translation / fallback / rerank-beyond-rerank.

memory_construction_time semantics: changed from "retrieval time" to "cumulative extraction time".
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Repository-local paths. The open-source layout keeps the extractor next to this
# adapter so the module remains portable after cloning.
REPO_ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR_DIR = REPO_ROOT / "extractor"
if str(EXTRACTOR_DIR) not in sys.path:
    sys.path.insert(0, str(EXTRACTOR_DIR))

SCHEMA_PATH = EXTRACTOR_DIR / "schema.json"
RULES_PATH = EXTRACTOR_DIR / "extract_rules.json"


_PREP = ("by", "in", "to", "at", "of", "with", "from", "on", "for", "under")
_STOP = ("the", "a", "an")


def _fact_subject(row):
    """Fact identity key = equation subject + relation (copula uses the subject,
    relation uses subject|verb phrase). Extracted with deterministic regex, not by
    asking an LLM to fill discrete fields."""
    eq = str(row.get("等式") or row.get("左值指标名") or "").strip()
    m = re.match(r"(.*?)\s+(is|was|plays|speaks|works|died|worked)\b", eq)
    if not m:
        return eq.strip().lower()
    subj = m.group(1).strip().lower()
    verb = m.group(2)
    right = eq[m.end():].strip()
    rw = [w for w in right.lower().split() if w not in _STOP]
    if not any(w in _PREP for w in rw[:3]):
        return subj
    phrase = []
    for w in right.lower().split():
        phrase.append(w)
        if w in _PREP:
            break
    vp = " ".join(phrase).strip()
    if verb in ("plays", "speaks", "works", "died", "worked"):
        vp = verb + " " + vp
    return subj + "|" + vp


def _item_key(row, table):
    """Item key = each of the four tables' own primary key (metrics uses the metric name, facts uses the equation subject)."""
    if table == "metrics":
        return "metric:" + str(row.get("指标名", ""))
    if table == "facts":
        return "fact:" + _fact_subject(row)
    return None


def _fold_latest(rows):
    """Fold to latest (supersession current + drop discarded, as in our_integration.py)."""
    deleted_objs = set()
    for r in rows:
        if r.get("table") == "deleted":
            obj = r.get("object")
            if obj:
                deleted_objs.add(str(obj).lower())
    latest = {}
    for r in rows:
        t = r.get("table")
        if t == "deleted":
            continue
        if t == "cards" and deleted_objs:
            txt = json.dumps(r, ensure_ascii=False, default=str).lower()
            if any(obj in txt for obj in deleted_objs):
                continue
        k = r.get("_item_key")
        if t not in ("metrics", "facts") or k is None:
            latest["row_%d" % id(r)] = r
            continue
        if r.get("invalidated_at") is not None:
            continue
        v = r.get("_version") or 0
        if k not in latest or v >= (latest[k].get("_version") or 0):
            latest[k] = r
    return list(latest.values())


def _append_entities(agent, entities):
    """Append one batch of extraction results (four-table rows) into agent.rows following bi-temporal/supersession rules."""
    for table in ("metrics", "facts", "cards", "identities"):
        for row in entities.get(table, []):
            if not isinstance(row, dict):
                continue
            key = _item_key(row, table)
            new_ver = len(agent.rows)
            vf = row.get("time_norm")
            row["_item_key"] = key
            row["_version"] = new_ver
            row["valid_from"] = vf
            row["valid_to"] = None
            row["invalidated_at"] = None
            if table in ("metrics", "facts") and key:
                for old in agent.rows:
                    if (old.get("table") == table and old.get("_item_key") == key
                            and old.get("invalidated_at") is None):
                        old["invalidated_at"] = new_ver
                        old["valid_to"] = vf
            agent.rows.append({"table": table, **row})


def _extract_one(agent, context_id, seq, text):
    """Extract one chunk, returning (entities, dt). Exceptions are swallowed and yield empty entities without interrupting the batch."""
    chunk = {"source_chunk_id": "ctx%d_c%d" % (context_id or 0, seq),
             "text": text, "time_ref": ""}
    t0 = time.time()
    try:
        result = agent.ex.extract(chunk, agent.schema, rules=agent.rules)
        entities = (result or {}).get("entities", {})
    except Exception as e:  # noqa: BLE001
        print("[whale_ying] extract failed: %s" % e, file=sys.stderr)
        entities = {}
    return entities, time.time() - t0


def _extract_pending(agent):
    """Extract all pending chunks concurrently and merge them into agent.rows in original order (preserving _version's serial semantics)."""
    chunks = agent._pending_chunks
    agent._pending_chunks = []
    if not chunks:
        return
    results = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=min(len(chunks), 8)) as ex:
        futs = {ex.submit(_extract_one, agent, c["context_id"], c["seq"], c["text"]): i
                for i, c in enumerate(chunks)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001
                print("[whale_ying] extract thread failed: %s" % e, file=sys.stderr)
    for i, (c, r) in enumerate(zip(chunks, results)):
        if r is None:
            continue
        entities, dt = r
        agent._extract_time += dt
        agent._n_chunks += 1
        _append_entities(agent, entities)
        print("[whale_ying] chunk %d extract %.1fs (累计 %.0fs)" %
              (c["seq"] + 1, dt, agent._extract_time), file=sys.stderr)


def initialize_our_agent(agent, agent_config=None):
    config = agent_config or {}
    agent.retrieve_num = config.get("retrieve_num", 10)
    agent.rows = []
    agent._chunk_seq = 0
    agent._pending_chunks = []   # concurrent extraction: accumulate first, extract later
    agent._extract_time = 0.0    # cumulative extraction time (memorize phase)
    agent._n_chunks = 0
    agent._bm25 = None
    agent._bm25_texts = None
    agent._emb_model = None
    agent._emb_failed = False    # if the vector model fails to load, permanently fall back to BM25-only
    agent._retr_cache = None     # retrieval cache: BM25 index + whole-library vectors built once (avoids re-encoding the whole library per question)
    # Load a repository-local .env when present; secrets remain outside source control.
    try:
        from dotenv import load_dotenv
        load_dotenv(str(REPO_ROOT / ".env"))
    except Exception:
        pass
    import extract as ex
    agent.ex = ex
    agent.schema = ex.load_json(str(SCHEMA_PATH), "schema")
    agent.rules = ex.load_rules(agent.schema, str(RULES_PATH))
    print("\n[whale_ying] extractor loaded\n")


# ---------------- Retrieval: multi-field BM25 (subject 3:1 body) + vector + RRF ----------------
# JSON field-name pollution plus small-corpus bi-encoder noise are the two causes of
# "noisy facts replacing real ones"; building a separate IDF for the short subject
# field recovers the signal. The tokenizer keeps only a-z0-9_-/., length >= 3, drops stopwords.
_TOKEN_RE = re.compile(r"[a-z0-9_\-/.]+")
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "to", "at", "for", "with", "on", "by", "from",
    "is", "was", "are", "were", "be", "been", "and", "or", "that", "this",
    "it", "its", "as", "has", "have", "had", "which", "who", "what", "where",
    "when", "how", "do", "does", "did", "not", "no", "he", "she", "they", "we",
    "you", "i",
})


def _tokenize(text):
    """Clean tokenization: keep only a-z0-9_-/., length >= 3, drop stopwords (fixes field-name pollution + high-frequency word dilution)."""
    return [t for t in _TOKEN_RE.findall(str(text or "").lower())
            if len(t) >= 3 and t not in _STOPWORDS]


def _subject_body(row):
    """(subject, body): subject = high-signal short field (weight 3), body = long content (weight 1)."""
    t = row.get("table")
    if t == "facts":
        return (str(row.get("左值指标名") or ""), str(row.get("等式") or ""))
    if t == "metrics":
        return (str(row.get("指标名") or ""), "%s = %s" % (row.get("指标名", ""), row.get("值", "")))
    if t == "cards":
        return (str(row.get("action") or ""), "%s | %s" % (row.get("action", ""), row.get("result", "")))
    return (str(row.get("名称") or row.get("完整原文写法") or ""), "")


def _build_retrieval_cache(agent, rows):
    """Build the retrieval cache (BM25 index + whole-library vectors) once; each
    subsequent question only computes the query side. Previously the whole library
    was re-encoded per question (16k rows, ~60s), which dominated rerun time
    (build the index once, query many times, as with FAISS)."""
    from rank_bm25 import BM25Okapi
    subj_strs, body_strs = [], []
    for r in rows:
        s, b = _subject_body(r)
        subj_strs.append(s)
        body_strs.append(b)
    subj_tok = [_tokenize(s) for s in subj_strs]
    body_tok = [_tokenize(b) for b in body_strs]
    subjects = [st or bt for st, bt in zip(subj_tok, body_tok)]
    bodies = [bt or st for st, bt in zip(subj_tok, body_tok)]
    bm25_s = BM25Okapi(subjects) if subjects else None
    bm25_b = BM25Okapi(bodies) if bodies else None
    ce = None
    if not agent._emb_failed:
        try:
            if agent._emb_model is None:
                from sentence_transformers import SentenceTransformer
                agent._emb_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
            # vector side uses the body text (body equation first, fall back to subject), no longer feed the whole json
            vec_texts = [b or s for s, b in zip(subj_strs, body_strs)]
            ce = agent._emb_model.encode(vec_texts, normalize_embeddings=True)
        except Exception as e:  # noqa: BLE001
            print("[whale_ying] vector model failed, fallback to BM25-only: %s" % e,
                  file=sys.stderr)
            agent._emb_failed = True
    agent._retr_cache = {"n": len(rows), "bm25_s": bm25_s, "bm25_b": bm25_b, "ce": ce}
    return agent._retr_cache


def _hybrid_retrieve(agent, message, rows):
    """Hybrid retrieval: multi-field BM25 (subject and body each build their own IDF)
    + vector + RRF. The BM25 index and whole-library vectors are built once (cached
    and reused); each question only computes the query encoding + dot product."""
    cache = agent._retr_cache
    if cache is None or cache.get("n") != len(rows):
        cache = _build_retrieval_cache(agent, rows)
    n = cache["n"]
    bm25_s, bm25_b, ce = cache["bm25_s"], cache["bm25_b"], cache["ce"]
    q = _tokenize(message)
    if bm25_s is not None and q:
        ss = bm25_s.get_scores(q)
        bb = bm25_b.get_scores(q) if bm25_b is not None else [0.0] * n
        bm25_scores = [3.0 * a + 1.0 * b for a, b in zip(ss, bb)]
    else:
        bm25_scores = [0.0] * n

    vec_sims = None
    if ce is not None:
        try:
            qe = agent._emb_model.encode([message], normalize_embeddings=True)[0]
            vec_sims = ce @ qe
        except Exception as e:  # noqa: BLE001
            print("[whale_ying] query encode failed, fallback to BM25-only: %s" % e,
                  file=sys.stderr)
            cache["ce"] = None

    if vec_sims is not None:
        bm25_order = sorted(range(n), key=lambda i: -float(bm25_scores[i]))
        vec_order = sorted(range(n), key=lambda i: -float(vec_sims[i]))
        bm25_rank = {i: r for r, i in enumerate(bm25_order, 1)}
        vec_rank = {i: r for r, i in enumerate(vec_order, 1)}
        scores = {i: 1.0 / (60 + bm25_rank[i]) + 1.0 / (60 + vec_rank[i]) for i in range(n)}
    else:
        scores = {i: float(bm25_scores[i]) for i in range(n)}
    order = sorted(range(n), key=lambda i: -scores[i])
    return order


# ---------------------------------------------------------------- LLM rerank + dedup (RankGPT-style + keep latest per subject)

def _card_pos(r):
    """Extract a card's source position (extraction position), used for ordering (old -> new)."""
    if isinstance(r, dict):
        try:
            return int(r.get("抽取位置") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _card_summary(r):
    """One-sentence natural-language summary of a card (for the rerank/dedup LLM to read; it reads only this sentence, not the json field names)."""
    t = r.get("table")
    if t == "facts":
        return str(r.get("等式") or r.get("左值指标名") or "")
    if t == "metrics":
        return "%s = %s" % (r.get("指标名", ""), r.get("值", ""))
    if t == "cards":
        return "%s | %s" % (r.get("action", ""), r.get("result", ""))
    return str(r.get("名称") or r.get("完整原文写法") or "")


def _build_rerank_prompt(query, summaries):
    """Pure ranking: sort by relevance from high to low. Dedup is already handled by the storage-side deterministic subject folding; this only ranks."""
    num = len(summaries)
    parts = ["query: %s" % query, ""]
    parts.append("下面有 %d 张记忆卡片（每张编号 [i]，内容=一句自然语言总结）：" % num)
    for i, s in enumerate(summaries, 1):
        parts.append("[%d] %s" % (i, " ".join(s.split())[:300]))
    parts.append("")
    parts.append("注意：卡片中有很多出乎常识的东西，不许按照自己的世界知识进行排序。")
    parts.append("任务：浏览每张记忆卡片，判断与 query 的相关度，按相关度从高到低排序。")
    parts.append("任务内容只为排序，不要偏离，不要解释排序理由，最终排序完成后结束轮次。输出格式 [] > []。")
    return "\n".join(parts)


def _parse_rank(text, num):
    """Parse the ranking indices output by the LLM (dedup + fill in missing, guaranteeing a full permutation)."""
    order = []
    for d in re.findall(r"\d+", text):
        i = int(d) - 1
        if 0 <= i < num and i not in order:
            order.append(i)
    order += [i for i in range(num) if i not in order]
    return order


def _llm_rerank(agent, query, rows):
    """Listwise rerank + dedup the top-K rows. First sort by extraction position
    ascending (old -> new), then let the LLM rank and keep the latest. Returns the
    reranked + deduped row list (order given by the LLM); on failure, the original order."""
    if len(rows) <= 1:
        return list(rows)
    sorted_rows = sorted(rows, key=_card_pos)
    summaries = [_card_summary(r) for r in sorted_rows]
    try:
        import deepseek_api
        resp = deepseek_api.call(_build_rerank_prompt(query, summaries),
                                 temperature=0.0, max_tokens=500,
                                 response_format_json=False,
                                 disable_thinking=True)
        order = _parse_rank(resp.get("text") or "", len(summaries))
        return [sorted_rows[i] for i in order]
    except Exception as e:  # noqa: BLE001
        print("[whale_ying] rerank failed, keep original order: %s" % e, file=sys.stderr)
        return list(rows)


# ---------------------------------------------------------------- post-retrieval assembly (CAR-style: extract candidates -> deterministically pick latest)

CANDIDATE_PROMPT = """You are given retrieved items from a knowledge pool. Each item has a FRESHNESS marker (the prefix integer) — higher marker = newer version.

Your job: identify EVERY item that DIRECTLY answers the question, and extract the answer entity from each.

Do NOT compare freshness markers. Do NOT pick a "best" one. Include ALL items that match.

Rules:
1. An item directly answers the question only if BOTH its subject AND its predicate exactly match what the question asks about. The predicate noun used in the question (e.g., "spouse", "sport", "profession", "religion") must be the same noun used in the item. Related-but-different predicates do NOT match.
2. The subject named in the question must appear verbatim in the matching item. A different entity with a similar name does NOT match.
3. If a subject has multiple conflicting values (e.g., the same person with two different values at different freshness markers), INCLUDE BOTH as separate candidates. Do not pick.
4. If no item answers the question, return an empty list.
5. Copy the item's text verbatim into `fact_text`.

Question: {question}

Items:
{pool}

Return ONLY valid JSON: {"candidates": [{"serial": <int>, "fact_text": "<verbatim>", "answer_entity": "<extracted>"}, ...]}"""


def _format_pool(rows):
    """Format the top-K rows into a knowledge pool of "index. equation" (index = _version, i.e. insertion order ~= benchmark serial)."""
    lines = []
    for r in rows:
        serial = r.get("_version") if isinstance(r.get("_version"), int) else 0
        text = str(r.get("等式") or r.get("左值指标名") or "").strip()
        lines.append("%d. %s" % (serial, text))
    return "\n".join(lines)


def _extract_candidates(agent, query, rows):
    """LLM candidate extraction: find only semantic matches, don't pick a winner (per CAR CANDIDATE_PROMPT). Returns (candidates, in_tok, out_tok)."""
    pool = _format_pool(rows)
    try:
        resp = agent._create_oai_client().chat.completions.create(
            model=agent.model,
            response_format={"type": "json_object"},
            temperature=0.0,
            messages=[{"role": "user",
                       "content": CANDIDATE_PROMPT.replace("{question}", query).replace("{pool}", pool)}],
        )
        raw = resp.choices[0].message.content or "{}"
        candidates = json.loads(raw).get("candidates", [])
        in_tok = resp.usage.prompt_tokens
        out_tok = resp.usage.completion_tokens
    except Exception:  # noqa: BLE001 — parse/call failure returns empty, handled by the fallback
        return [], 0, 0
    out = [c for c in candidates if isinstance(c, dict) and c.get("answer_entity")]
    return out, in_tok, out_tok


def _freshness_pick(candidates):
    """Deterministically "pick the latest": the largest serial wins (per CAR max(candidates, key=serial))."""
    if not candidates:
        return ""
    chosen = max(candidates, key=lambda c: c.get("serial") if isinstance(c.get("serial"), int) else 0)
    return str(chosen.get("answer_entity") or "").strip()


def _direct_answer(agent, message, final_rows, chain=None):
    """Answer in one step. Returns (text, in_tok, out_tok). `chain` is an optional list of [(sub-question, intermediate answer)] placed before the memory."""
    mem_lines = []
    if chain:
        mem_lines.append("Resolved intermediate steps (sub-question -> answer):")
        mem_lines.extend("%s -> %s" % (q, a) for q, a in chain)
        mem_lines.append("")
    mem_lines += ["Memory %d:\n%s" % (i + 1, json.dumps(r, ensure_ascii=False, default=str))
                  for i, r in enumerate(final_rows)]
    memory_string = "\n".join(mem_lines)
    ask = (memory_string + "\n" + message) if memory_string else message
    try:
        from utils.templates import get_template
        from utils.eval_data_utils import format_chat
        system_message = get_template(agent.sub_dataset, "system", agent.agent_name)
        format_message = format_chat(message=ask, system_message=system_message)
    except ImportError:
        # Standalone (no MemoryAgentBench utils package): inline the equivalent system
        # message and chat formatting so the adapter runs without the benchmark installed.
        system_message = ("You are a helpful assistant that can read the context "
                          "and memorize it for future retrieval.")
        format_message = [{"role": "system", "content": system_message},
                          {"role": "user", "content": ask}]
    # A dropped connection (SSL EOF) raises APIConnectionError: return empty (that question is marked wrong) rather than crashing the whole run
    for attempt in range(3):
        try:
            kwargs = dict(
                model=agent.model,
                messages=format_message,
                temperature=agent.temperature,
                max_tokens=agent.max_tokens,
            )
            if "deepseek" in agent.model:
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            response = agent._create_oai_client().chat.completions.create(**kwargs)
            return (response.choices[0].message.content or "",
                    response.usage.prompt_tokens, response.usage.completion_tokens)
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                print("[whale_ying] answer retry %d/3 after %s: %s"
                      % (attempt + 1, type(e).__name__, str(e)[:120]), file=sys.stderr)
                time.sleep(10)
                continue
            print("[whale_ying] answer gave up after 3 attempts: %s"
                  % type(e).__name__, file=sys.stderr)
            return ("", 0, 0)
    return ("", 0, 0)


# ---------------------------------------------------------------- Self-Ask style iteration (multi-hop: first answer -> self-check -> sub-question -> re-retrieve -> re-answer)

SELFCHECK_PROMPT = """You are checking whether an answer to a question is fully supported by retrieved memory facts.

Question: {question}
Current answer: {answer}

Resolved intermediate steps:
{chain}

Retrieved memory facts:
{facts}

Decide:
- If the answer is definitely correct and fully supported, reply {"ok": true}.
- Otherwise (e.g. a multi-hop question whose intermediate entity is still missing), reply {"ok": false, "followup": "<ONE single-hop sub-question naming concrete entities>"}.

Return a json object and nothing else."""


ANSWER_SHORT_PROMPT = """Given the memory facts, answer the question with ONLY the answer entity (no explanation, no extra punctuation). Answer ONLY from the memory facts — do NOT use your own world knowledge (many facts here contradict the real world).

Question: {question}

Memory facts:
{facts}

Answer:"""


def _chat(agent, messages, max_tokens=300, temperature=0.0, json_mode=False):
    """Simple OpenAI-compatible call, returning (text, in_tok, out_tok); on failure ("", 0, 0)."""
    try:
        kwargs = dict(model=agent.model, messages=messages,
                      temperature=temperature, max_tokens=max_tokens)
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if "deepseek" in agent.model:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        resp = agent._create_oai_client().chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "",
                resp.usage.prompt_tokens, resp.usage.completion_tokens)
    except Exception as e:  # noqa: BLE001
        print("[whale_ying] chat failed: %s" % e, file=sys.stderr)
        return ("", 0, 0)


def _facts_text(rows, limit=20):
    """Rows -> one natural-language line per row for the LLM to read (reuses _card_summary; reads only this sentence, not the json field names)."""
    return "\n".join(_card_summary(r) for r in rows[:limit]) or "(none)"


def _chain_text(chain):
    return "\n".join("%s -> %s" % (q, a) for q, a in chain) or "(none yet)"


def _selfcheck(agent, question, answer, facts_rows, chain):
    """Self-check whether the answer is sufficient. Returns (dict, in_tok, out_tok). On parse failure treat as {ok:true} (don't fight it)."""
    raw, in_tok, out_tok = _chat(
        agent,
        [{"role": "user", "content": SELFCHECK_PROMPT.replace("{question}", question)
          .replace("{answer}", answer).replace("{chain}", _chain_text(chain))
          .replace("{facts}", _facts_text(facts_rows))}],
        max_tokens=300, json_mode=True)
    try:
        return json.loads(raw), in_tok, out_tok
    except Exception:  # noqa: BLE001
        return {"ok": True}, in_tok, out_tok


def _answer_short(agent, question, facts_rows):
    """Short answer to a sub-question (entity only). Returns (text, in_tok, out_tok)."""
    return _chat(
        agent,
        [{"role": "user", "content": ANSWER_SHORT_PROMPT.replace("{question}", question)
          .replace("{facts}", _facts_text(facts_rows))}],
        max_tokens=100)


def _iterative_answer(agent, message, retrieval_query, rows, initial_rows, max_rounds=2):
    """Self-Ask style iteration: first answer -> self-check "is it reasonable/sufficient"
    -> if a sub-question is needed -> retrieve + short answer -> re-answer with the
    evidence. Fixes multi-hop questions where a single retrieval cannot recover the
    intermediate entity. Capped at max_rounds rounds. Returns (text, in_tok, out_tok)."""
    total_in = 0
    total_out = 0
    evidence = list(initial_rows)
    seen = {id(r) for r in evidence}
    chain = []

    def add(rs):
        for r in rs:
            if id(r) not in seen:
                seen.add(id(r))
                evidence.append(r)

    answer, ain, aout = _direct_answer(agent, message, evidence, chain=chain)
    total_in += ain
    total_out += aout

    for _ in range(max_rounds):
        check, cin, cout = _selfcheck(agent, retrieval_query, answer, evidence, chain)
        total_in += cin
        total_out += cout
        if check.get("ok"):
            break
        fq = (check.get("followup") or "").strip()
        if not fq:
            break
        order = _hybrid_retrieve(agent, fq, rows)
        sub_rows = [rows[i] for i in order[:agent.retrieve_num]]
        sub_rows = _llm_rerank(agent, fq, sub_rows)
        add(sub_rows)
        sub_ans, sin, sout = _answer_short(agent, fq, sub_rows)
        total_in += sin
        total_out += sout
        if not sub_ans.strip():
            break
        chain.append((fq, sub_ans.strip()))
        print("[whale_ying] iter round: %s -> %s" % (fq, sub_ans.strip()), file=sys.stderr)
        answer, ain, aout = _direct_answer(agent, message, evidence, chain=chain)
        total_in += ain
        total_out += aout

    return answer, total_in, total_out


def handle_our_agent(agent, message, memorizing, query_id=None, context_id=None):
    if memorizing:
        # concurrent extraction: accumulate now, extract later on the first query
        agent._pending_chunks.append(
            {"context_id": context_id or 0, "seq": agent._chunk_seq, "text": message})
        agent._chunk_seq += 1
        return "Memorized"

    # query
    start = time.time()
    # Two stages run as separate processes: stage 2 sets WHALE_YING_STORE_LOAD to read the already-extracted store directly (skipping DeepSeek extraction)
    load_path = os.environ.get("WHALE_YING_STORE_LOAD", "").strip()
    if load_path:
        agent.rows = json.load(open(load_path, encoding="utf-8"))
    else:
        _extract_pending(agent)   # extract all pending chunks concurrently (no-op when empty)
    # Stage 1: if WHALE_YING_STORE_SAVE is set, save the store and exit after extracting (extract-only)
    save_path = os.environ.get("WHALE_YING_STORE_SAVE", "").strip()
    if save_path:
        json.dump(agent.rows, open(save_path, "w", encoding="utf-8"), ensure_ascii=False)
        print("[whale_ying] store saved (%d rows) -> %s, exit extract-only"
              % (len(agent.rows), save_path), file=sys.stderr)
        sys.exit(0)
    rows = _fold_latest(agent.rows)
    final_rows = []
    if rows:
        # Retrieval/rerank use the "clean question": MAB's message carries ~200 tokens
        # of task boilerplate plus one example, so retrieving with the whole thing means
        # retrieving with noise (the vector is pulled off by "president of Russia"/
        # "Donald Trump" in the example). _extract_retrieval_query extracts the real
        # question; answering still uses the full message (to keep the task instruction).
        retrieval_query = agent._extract_retrieval_query(message)
        order = _hybrid_retrieve(agent, retrieval_query, rows)
        k = min(agent.retrieve_num, len(order))
        topk = order[:k]
        # LLM rerank + dedup the top-K (sort by extraction position ascending first, then LLM ranks and keeps the latest)
        topk_rows = [rows[i] for i in topk]
        final_rows = _llm_rerank(agent, retrieval_query, topk_rows)
    retrieval_time = time.time() - start

    # Answer: WHALE_YING_ITERATIVE=1 uses Self-Ask iteration (multi-hop); otherwise one-step answering
    if os.environ.get("WHALE_YING_ITERATIVE", "").strip() in ("1", "true", "yes") and rows:
        answer, input_len, output_len = _iterative_answer(
            agent, message, retrieval_query, rows, final_rows)
    else:
        answer, input_len, output_len = _direct_answer(agent, message, final_rows)

    query_time = time.time() - start - retrieval_time
    return {
        "output": answer,
        "input_len": input_len,
        "output_len": output_len,
        # memory_construction_time is the cumulative extraction time (the real bulk of memory construction)
        "memory_construction_time": agent._extract_time,
        "query_time_len": query_time,
    }
