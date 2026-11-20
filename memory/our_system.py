# -*- coding: utf-8 -*-
"""
our_system —— 我们的记忆系统（抽取器）接入 MemoryAgentBench 的薄适配。

1. memorize：把 benchmark 喂来的 chunk **攒起来**，第一次 query 时**并发**交给抽取器 extract()
   （chunk 独立、可并行；改前是逐段串行）。
2. query：折叠取最新（_fold_latest），BM25+向量+RRF 混合检索，再 **LLM 重排**（RankGPT 式
   listwise，照 sunnweiwei/RankGPT 的 permutation_pipeline），拼进 prompt 交给 gpt-4o-mini 作答。
3. 不夹带：不做翻译 / 兜底 / 重排之外的重排。

⑤ memory_construction_time 语义：从「检索耗时」改成「累计抽取耗时」。
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# l1test 根：our_system.py -> methods -> MemoryAgentBench -> l1test
L1TEST = Path(__file__).resolve().parents[2]
EXTRACTOR_DIR = L1TEST / "抽取器"
if str(EXTRACTOR_DIR) not in sys.path:
    sys.path.insert(0, str(EXTRACTOR_DIR))

SCHEMA_PATH = EXTRACTOR_DIR / "schema.json"
RULES_PATH = EXTRACTOR_DIR / "extract_rules.json"


_PREP = ("by", "in", "to", "at", "of", "with", "from", "on", "for", "under")
_STOP = ("the", "a", "an")


def _fact_subject(row):
    """事实身份键 = 等式主语 + 关系（copula 用主语，relation 用主语|动词短语）。
    确定性 regex 抠，不靠 LLM 填离散字段。"""
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
    """item 键 = 4 表自身主键（metrics 用指标名、facts 用等式主语）。"""
    if table == "metrics":
        return "metric:" + str(row.get("指标名", ""))
    if table == "facts":
        return "fact:" + _fact_subject(row)
    return None


def _fold_latest(rows):
    """折叠取最新（supersession current + 排除 discarded，照 our_integration.py）。"""
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
    """把一批抽取结果（四表行）按 bi-temporal/supersession 规则追加进 agent.rows。"""
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
    """抽一段 chunk，返回 (entities, dt)。异常吞掉返回空 entities，不打断整批。"""
    chunk = {"source_chunk_id": "ctx%d_c%d" % (context_id or 0, seq),
             "text": text, "time_ref": ""}
    t0 = time.time()
    try:
        result = agent.ex.extract(chunk, agent.schema, rules=agent.rules)
        entities = (result or {}).get("entities", {})
    except Exception as e:  # noqa: BLE001
        print("[our_system] extract failed: %s" % e, file=sys.stderr)
        entities = {}
    return entities, time.time() - t0


def _extract_pending(agent):
    """把攒下的 chunk 并发抽完，按**原顺序**合并进 agent.rows（保 _version 的 serial 语义）。"""
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
                print("[our_system] extract thread failed: %s" % e, file=sys.stderr)
    for i, (c, r) in enumerate(zip(chunks, results)):
        if r is None:
            continue
        entities, dt = r
        agent._extract_time += dt
        agent._n_chunks += 1
        _append_entities(agent, entities)
        print("[our_system] chunk %d extract %.1fs (累计 %.0fs)" %
              (c["seq"] + 1, dt, agent._extract_time), file=sys.stderr)


def initialize_our_agent(agent, agent_config=None):
    config = agent_config or {}
    agent.retrieve_num = config.get("retrieve_num", 10)
    agent.rows = []
    agent._chunk_seq = 0
    agent._pending_chunks = []   # 并行抽取：先攒后抽
    agent._extract_time = 0.0    # ⑤ 累计抽取耗时（memorize 阶段）
    agent._n_chunks = 0
    agent._bm25 = None
    agent._bm25_texts = None
    agent._emb_model = None
    agent._emb_failed = False    # 向量模型加载失败就永久退回 BM25-only
    agent._retr_cache = None     # 检索缓存：BM25 索引 + 整库向量只建一次（治每题全库重编码 ~60s）
    # 显式加载 l1test\.env，确保 DEEPSEEK_API_KEY 进环境（抽取器走 DeepSeek）
    try:
        from dotenv import load_dotenv
        load_dotenv(str(L1TEST / ".env"))
    except Exception:
        pass
    import extract as ex
    agent.ex = ex
    agent.schema = ex.load_json(str(SCHEMA_PATH), "schema")
    agent.rules = ex.load_rules(agent.schema, str(RULES_PATH))
    print("\n[our_system] extractor loaded\n")


# ---------------- 检索：多字段 BM25（subject 3:1 body）+ 向量 + RRF ----------------
# 依据 ADR-079（ruflo）：json 字段名污染 + bi-encoder 小语料噪声 是「噪声事实顶替」两个元凶；
# subject 短字段单独建 IDF 才救回信号。tokenizer 只留 a-z0-9_-/.、长度≥3、去停用词。
_TOKEN_RE = re.compile(r"[a-z0-9_\-/.]+")
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "to", "at", "for", "with", "on", "by", "from",
    "is", "was", "are", "were", "be", "been", "and", "or", "that", "this",
    "it", "its", "as", "has", "have", "had", "which", "who", "what", "where",
    "when", "how", "do", "does", "did", "not", "no", "he", "she", "they", "we",
    "you", "i",
})


def _tokenize(text):
    """干净分词：只留 a-z0-9_-/.、长度≥3、去停用词（治字段名污染 + 高频词稀释）。"""
    return [t for t in _TOKEN_RE.findall(str(text or "").lower())
            if len(t) >= 3 and t not in _STOPWORDS]


def _subject_body(row):
    """(subject, body)：subject=高信号短字段（3 权），body=长内容（1 权）。"""
    t = row.get("table")
    if t == "facts":
        return (str(row.get("左值指标名") or ""), str(row.get("等式") or ""))
    if t == "metrics":
        return (str(row.get("指标名") or ""), "%s = %s" % (row.get("指标名", ""), row.get("值", "")))
    if t == "cards":
        return (str(row.get("action") or ""), "%s | %s" % (row.get("action", ""), row.get("result", "")))
    return (str(row.get("名称") or row.get("完整原文写法") or ""), "")


def _build_retrieval_cache(agent, rows):
    """把检索要用的「BM25 索引 + 整库向量」一次建好缓存；之后每题只算 query 侧。
    之前每题全库重编码（16k 条 ~60s），是 262k 复测 1h 的主因（照 FAISS「索引一次、查询多次」）。"""
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
            # 向量侧用正文（body 等式为主，空则回退 subject），不再喂整条 json
            vec_texts = [b or s for s, b in zip(subj_strs, body_strs)]
            ce = agent._emb_model.encode(vec_texts, normalize_embeddings=True)
        except Exception as e:  # noqa: BLE001
            print("[our_system] vector model failed, fallback to BM25-only: %s" % e,
                  file=sys.stderr)
            agent._emb_failed = True
    agent._retr_cache = {"n": len(rows), "bm25_s": bm25_s, "bm25_b": bm25_b, "ce": ce}
    return agent._retr_cache


def _hybrid_retrieve(agent, message, rows):
    """多字段 BM25（subject 3:1 body 各自建 IDF）+ 向量 + RRF 混合检索。
    BM25 索引与整库向量只建一次（缓存复用），每题只算 query 编码 + 点积。"""
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
            print("[our_system] query encode failed, fallback to BM25-only: %s" % e,
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


# ---------------------------------------------------------------- LLM 重排 + 去重（RankGPT 式 + 同主语留最新）

def _card_pos(r):
    """抽卡片的原文位置（抽取位置），用于排序（旧→新）。"""
    if isinstance(r, dict):
        try:
            return int(r.get("抽取位置") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _card_summary(r):
    """卡片的自然语言一句话总结（给重排/去重 LLM 读，只看这一句，不看 json 字段名）。"""
    t = r.get("table")
    if t == "facts":
        return str(r.get("等式") or r.get("左值指标名") or "")
    if t == "metrics":
        return "%s = %s" % (r.get("指标名", ""), r.get("值", ""))
    if t == "cards":
        return "%s | %s" % (r.get("action", ""), r.get("result", ""))
    return str(r.get("名称") or r.get("完整原文写法") or "")


def _build_rerank_prompt(query, summaries):
    """纯排序：按相关度从高到低排。去重已由存储侧确定性主语折叠做完，这里只排序。"""
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
    """解析 LLM 输出的排序编号（去重 + 补漏，保证是全排列）。"""
    order = []
    for d in re.findall(r"\d+", text):
        i = int(d) - 1
        if 0 <= i < num and i not in order:
            order.append(i)
    order += [i for i in range(num) if i not in order]
    return order


def _llm_rerank(agent, query, rows):
    """对 top-K 行做 listwise 重排 + 去重。先按抽取位置升序（旧→新），再让 LLM 排序+留最新。
    返回重排+去重后的行列表（LLM 给序）。失败则原序。"""
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
        print("[our_system] rerank failed, keep original order: %s" % e, file=sys.stderr)
        return list(rows)


# ---------------------------------------------------------------- post-retrieval assembly（CAR 式：抽候选 → 确定性选最新）

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
    """把 top-K 行格式化成「序号. 等式」的知识池（序号 = _version 即插入序 ≈ 基准 serial）。"""
    lines = []
    for r in rows:
        serial = r.get("_version") if isinstance(r.get("_version"), int) else 0
        text = str(r.get("等式") or r.get("左值指标名") or "").strip()
        lines.append("%d. %s" % (serial, text))
    return "\n".join(lines)


def _extract_candidates(agent, query, rows):
    """LLM 抽候选：只找语义匹配项、不选赢家（照 CAR CANDIDATE_PROMPT）。返回 (candidates, in_tok, out_tok)。"""
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
    except Exception:  # noqa: BLE001 —— 解析/调用失败返空，交给兜底
        return [], 0, 0
    out = [c for c in candidates if isinstance(c, dict) and c.get("answer_entity")]
    return out, in_tok, out_tok


def _freshness_pick(candidates):
    """确定性执行「取最新」：serial 最大者胜（照 CAR max(candidates, key=serial)）。"""
    if not candidates:
        return ""
    chosen = max(candidates, key=lambda c: c.get("serial") if isinstance(c.get("serial"), int) else 0)
    return str(chosen.get("answer_entity") or "").strip()


def _direct_answer(agent, message, final_rows, chain=None):
    """一步作答。返回 (text, in_tok, out_tok)。chain 可选：[(子问题, 中间答案)] 拼在记忆前。"""
    from utils.templates import get_template
    from utils.eval_data_utils import format_chat
    mem_lines = []
    if chain:
        mem_lines.append("Resolved intermediate steps (sub-question -> answer):")
        mem_lines.extend("%s -> %s" % (q, a) for q, a in chain)
        mem_lines.append("")
    mem_lines += ["Memory %d:\n%s" % (i + 1, json.dumps(r, ensure_ascii=False, default=str))
                  for i, r in enumerate(final_rows)]
    memory_string = "\n".join(mem_lines)
    ask = (memory_string + "\n" + message) if memory_string else message
    system_message = get_template(agent.sub_dataset, "system", agent.agent_name)
    format_message = format_chat(message=ask, system_message=system_message)
    # 机场掉线（SSL EOF）会抛 APIConnectionError：答不出返空（该题计错），别让整轮崩掉
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
                print("[our_system] answer retry %d/3 after %s: %s"
                      % (attempt + 1, type(e).__name__, str(e)[:120]), file=sys.stderr)
                time.sleep(10)
                continue
            print("[our_system] answer gave up after 3 attempts: %s"
                  % type(e).__name__, file=sys.stderr)
            return ("", 0, 0)
    return ("", 0, 0)


# ---------------------------------------------------------------- Self-Ask 式迭代（多跳：首答→自评→子问题→再检索→重答）

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
    """简单 OpenAI-compatible 调用，返回 (text, in_tok, out_tok)；失败 ("", 0, 0)。"""
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
        print("[our_system] chat failed: %s" % e, file=sys.stderr)
        return ("", 0, 0)


def _facts_text(rows, limit=20):
    """行 → 「一行一条自然语言」给 LLM 读（复用 _card_summary，只看这句不看 json 字段名）。"""
    return "\n".join(_card_summary(r) for r in rows[:limit]) or "(none)"


def _chain_text(chain):
    return "\n".join("%s -> %s" % (q, a) for q, a in chain) or "(none yet)"


def _selfcheck(agent, question, answer, facts_rows, chain):
    """自评：答案是否充分。返回 (dict, in_tok, out_tok)。解析失败按 {ok:true}（不折腾）。"""
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
    """子问题短答（只回实体）。返回 (text, in_tok, out_tok)。"""
    return _chat(
        agent,
        [{"role": "user", "content": ANSWER_SHORT_PROMPT.replace("{question}", question)
          .replace("{facts}", _facts_text(facts_rows))}],
        max_tokens=100)


def _iterative_answer(agent, message, retrieval_query, rows, initial_rows, max_rounds=2):
    """Self-Ask 式迭代：首答 → 自评「是否合理/充分」→ 若需子问题 → 检索+短答 → 拼进证据重答。
    治多跳：单次检索链不起中间实体。上限 max_rounds 轮。返回 (text, in_tok, out_tok)。"""
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
        print("[our_system] iter round: %s -> %s" % (fq, sub_ans.strip()), file=sys.stderr)
        answer, ain, aout = _direct_answer(agent, message, evidence, chain=chain)
        total_in += ain
        total_out += aout

    return answer, total_in, total_out


def handle_our_agent(agent, message, memorizing, query_id=None, context_id=None):
    if memorizing:
        # 并行抽取：先攒，不立即抽；第一次 query 时并发抽完
        agent._pending_chunks.append(
            {"context_id": context_id or 0, "seq": agent._chunk_seq, "text": message})
        agent._chunk_seq += 1
        return "Memorized"

    # query
    start = time.time()
    # 两阶段分进程跑：阶段2 设 OUR_SYSTEM_STORE_LOAD 直接读已抽好的 store（跳过 DeepSeek 抽取）
    load_path = os.environ.get("OUR_SYSTEM_STORE_LOAD", "").strip()
    if load_path:
        agent.rows = json.load(open(load_path, encoding="utf-8"))
    else:
        _extract_pending(agent)   # 把攒下的 chunk 并发抽完（空则跳过，幂等）
    # 阶段1：设 OUR_SYSTEM_STORE_SAVE 则抽完即存盘退出（只抽不答）
    save_path = os.environ.get("OUR_SYSTEM_STORE_SAVE", "").strip()
    if save_path:
        json.dump(agent.rows, open(save_path, "w", encoding="utf-8"), ensure_ascii=False)
        print("[our_system] store saved (%d rows) -> %s, exit extract-only"
              % (len(agent.rows), save_path), file=sys.stderr)
        sys.exit(0)
    rows = _fold_latest(agent.rows)
    final_rows = []
    if rows:
        # 检索/重排用「干净问题」：MAB 的 message 夹着 ~200 token 的任务 boilerplate + 一个 example，
        # 直接拿整段去检索 = 拿噪声检索（向量被 example 里的 "president of Russia"/"Donald Trump" 带偏）。
        # 照 knowl：_extract_retrieval_query 抠出真问题，作答仍用完整 message（保留任务指令）。
        retrieval_query = agent._extract_retrieval_query(message)
        order = _hybrid_retrieve(agent, retrieval_query, rows)
        k = min(agent.retrieve_num, len(order))
        topk = order[:k]
        # LLM 重排 + 去重 top-K（先按抽取位置升序，LLM 排序并留最新）
        topk_rows = [rows[i] for i in topk]
        final_rows = _llm_rerank(agent, retrieval_query, topk_rows)
    retrieval_time = time.time() - start

    # 作答：OUR_SYSTEM_ITERATIVE=1 走 Self-Ask 迭代（多跳）；否则一步作答（回退大头 r1 基线）
    if os.environ.get("OUR_SYSTEM_ITERATIVE", "").strip() in ("1", "true", "yes") and rows:
        answer, input_len, output_len = _iterative_answer(
            agent, message, retrieval_query, rows, final_rows)
    else:
        answer, input_len, output_len = _direct_answer(agent, message, final_rows)

    query_time = time.time() - start - retrieval_time
    return {
        "output": answer,
        "input_len": input_len,
        "output_len": output_len,
        # ⑤ memory_construction_time 改成「累计抽取耗时」（记忆构造真正的大头）
        "memory_construction_time": agent._extract_time,
        "query_time_len": query_time,
    }
