# -*- coding: utf-8 -*-
"""extract.py — extractor: turn one source chunk into 4-entity JSON.

    Entities: cards / metrics / identities / fact statements (facts).
    Fields align one-to-one with schema v6.

    Core rules:
      ① quote is a hard rule: the prompt and the guard use the same definition (the
        guard is a hard ExtractError; a soft "try your best" would silently inflate the
        failure rate); a row that cannot produce a matching quote must not be emitted as
        its own line.
      ② the three value tables do not fill event_id; the program assigns it by offset
        (contain / nearest). The assignment method fk_method is recorded in the build log
        (out/build_log.jsonl); a high nearest share means card granularity is too coarse.
      ③ two max_tokens guards: deepseek_api raises explicitly when
        finish_reason=="length"; overlong chunks are split by paragraph and each piece
        extracted independently, then merged and deduplicated by the schema unique keys.

Robustness (three tiers) plus empty-field strip, retry, and monitoring:
  ⑤ empty-field strip: fields with on_missing=warn have empty strings stripped before
     required-field validation; a required field that is an empty string is an error
     (not stripped); a numeric row whose value is an empty string is dropped entirely.
  ⑥ three robustness tiers (criterion: "if this datum is missing, which questions
     would be answered wrong?"):
     A program-derivable -> fill directly, no warning (algorithm named by derive);
     B underivable and the row is meaningless without it -> hard-reject the piece and
       retry it (up to 2 times): fields with on_missing=reject that are missing/empty,
       illegal enum values, or have an offset but no quote (including no offset and no
       quote). After 2 failed retries -> write source+reason to failed/ and count, never
       drop silently.
     C missing but harmless to scoring -> warn and tolerate: fields with on_missing=warn
       that are empty are stripped.
   two monitoring items: warning rate = rows with warnings / total rows (<5%);
     reject rate = failed pieces / total pieces (<10%). Exceeding a threshold is not
     just an alarm: the warnings explicitly say "come back and check the prompt or the
     split".
   the quote-ratio gate is not relaxed by retries: the final four-table quote ratio must
     still be >= schema.gates.quote_ratio_min; retry only lets the model correct itself,
     it is not an exemption.

Table-driven tiers:
  ① table-driven: the A/B/C tiers come from schema attributes rather than three
     hand-written lists in code. extract_rules.json (next to the schema) gives each
     attribute two properties per "table.field": fill(model|derived) for who produces
     it, and on_missing(fill|reject|warn) for what to do when it is missing; the program
     iterates schema.entities[*].properties and behaves per attribute, with no field
     names hard-coded in code. A new field that is undeclared is a schema error (no
     default, forcing an explicit decision).
  ② metric.单位 and card.target drop required -> on_missing=warn (tier C);
     identity.canonical_id stays required -> on_missing=fill (tier A derived: the
     program normalizes the full original wording; the model does not supply it);
     quote.归一化 stays optional (schema's quote.required keeps only 原文). No defaults
     ("unknown" must not masquerade as a concrete value).
  ③ consistency check (single source of truth for required vs on_missing):
     reject ⇒ must be required; warn ⇒ must be non-required; fill ⇒ either way. The
     three red examples (warn-but-required / reject-but-not-required / fill positive)
     run in self_test_61.py.
  ④ content-noise gates:
     ① an enum value used as an identifier name (名称 / 完整原文写法 ∈ the type enum
       values, or 名称==类型) -> red + drop the row + count;
     ② same-string cross (within one chunk an identifier name == a card action/target)
       -> red (routing mismatch, row kept);
     ③ density monitoring is inspection only (per-chunk table row counts + identifiers
       per thousand chars; --density-scan lists the top 5% chunks for review);
     ④ the remaining spot checks live in spotcheck.py (5 rows per table, 20 total, seed
       and verdict kept; identity-table error rate <10% before end-to-end).

Further refinement:
  ① type enum gains 模型: schema and extract_rules.json stay in sync; widening the
     enum widens the gate coverage, and the check logic is unchanged (the enum-value
     gate picks up the new value automatically).
  ② shape gate: a card's action / target matching "pure identifier shape"
     (^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)*$ or with ()) and containing no CJK -> warn
     "identifier mistaken for an action". Warn only, do not drop (a shape rule, not a
     blacklist).
  ③ fk_method records per-row detail: each row's where + pos + method + which card it
     attached to (card_pos/card_quote) + delta, all in build_log fk_method.detail; when
     nearest_ratio > 0.3 the nearest-row detail is also written into the warning.

Explicit attribution with offset fallback:
  ① the prompt adds a temporary field 关联事件位置 to each row of the three tables:
     the integer extraction position of the card the row belongs to. It is declared in
     extract_rules.json _temp_fields (fill=model / on_missing=warn), not in the schema;
     the program pops it after reading (popped before keep_known_fields, check_0 still
     verifies), so it never reaches the final output.
  ② attribution order becomes explicit -> contain -> nearest: 关联事件位置 hits a
     card's extraction position -> fk_method="explicit"; otherwise fall back to
     contain -> nearest.
  ③ criterion: explicit coverage >= 80%, nearest_ratio < 0.1; each miss reports a
     warning (chunk-level hints, not row-level warning rate); nearest rows are marked
     low confidence (confidence=low), and cross-table join only answers with
     explicit/contain rows — the answer_rows statistics land in fk.answer and
     build_log.fk_method.answer.

Event-sequence anchor:
  ① the temporary field is renamed 所属事件序号 (dropping the misleading word 位置),
     and the three tables fill an integer sequence number per row;
  ② the prompt gives one positive and one negative example; the negative example uses
     the real number 1816 (what the model actually filled: the value's own offset);
  ③ the sequence number is program-derived: the prompt asks cards to be output in
     order of appearance, the program sorts cards by extraction position ascending and
     numbers them 1..n; when the LLM's original order is not ascending, reorder=true is
     recorded (its order is not trusted);
  ④ an out-of-range (not in 1..n) / non-integer number -> hint_invalid count + warning,
     not a silent fallback; "wrong" and "missing" are counted separately
     (hint_ok / hint_invalid / hint_missing);
  ⑤ criterion unchanged: explicit coverage >= 0.8, hint_invalid reported separately;
     explicit up but hint_invalid also high = the LLM struggles to count events (n too
     large); come back and check the card split.

Split two fk_method kinds (the non-explicit rows mix two things; four items):
  ① split fk_method, no longer one "nearest":
     - aggregate = derived totals in metrics/facts (total/count/share/derived values):
       naturally no single source event, points at "this chunk's summary segment"
       (anchor=chunk_summary, foreign key takes the nearest card of the summary segment);
     - mention   = name mentions in identities: anchored by source_chunk_id + extraction
       position + quote, not an event (identities and events are many-to-many);
     - cross-table join still only answers with explicit / contain rows.
  ② R12: identities.event_id becomes optional (schema drops required, rules record
     on_missing=fill + nullable=true): default locates by source_chunk_id + extraction
     position + quote, the program treats it as mention and does not fabricate a foreign
     key. metrics/facts event_id is unchanged (one-to-one, explicit/contain).
  ③ coverage denominators are per-table: metrics/facts = explicit / (rows −
     aggregate_rows); identities looks at mention coverage separately (has a quote and
     is locatable counts), and does not enter the explicit denominator. The merged ratio
     is meaningless and no longer reported.
  ④ .bak shape gate: 完整原文写法 starting with . (pure extension) or a pure suffix
     word -> warning "likely a generic term, not a name" (warn only, no drop).

Empty-denominator and answer_rows criteria:
  ① an empty denominator is not 100%: when a table's denominator=0 (all aggregate, or
     empty table) the ratio is null (N/A), not 1.0. The report lists vacuous_tables
     (empty-denominator tables) separately; these tables do not enter the overall
     passing denominator — only scored_tables (tables with a denominator) count. A
     chunk that is all summary values no longer shows "explicit 100%".
  ② answer_rows alongside explicit_ratio: answer_rows = explicit + contain rows (rows
     that can actually be used for cross-table join); the report lists
     fk.answer.answer_rows / answer_ratio separately, plus the criterion —
     answer_rows < FK_ANSWER_ROWS_MIN or answer_ratio < FK_ANSWER_RATIO_MIN warns
     fk_answer_rows_low. Coverage passing ≠ answerable (aggregate/mention do not enter
     answer).

Stability, logging, and pairwise coverage:
  ① stability: the same chunk runs N times (default 3, --repeat), temperature fixed at
     0 (deepseek_api.call's default); rows / explicit / nearest / answer_rows report
     median and range [min, max]; metrics whose half-range exceeds ±10% are marked
     unstable, as health-only (role=health_only), and only stable ones are role=judge. A
     single passing run is not a closed loop.
  ② logging: build_log supports --log-dir (pointing it at the --out directory keeps
     everything in one place); every run writes out / log / repeat into the log entry, so
     the log location is no longer an oral convention.
  ③ pairwise coverage: cross-table join needs "an event + its data row both present in
     answer"; answer_rows / rows is only a one-sided row share. --answer-index records
     each row's answer method, and --pair-check questions.json --answer-index ai.json
     verifies each question's pair; unanswerable means a linking problem (row attached
     to another event_id / row is itself aggregate/mention), not a ratio problem.

Split-parameter logging and cross-config metrics:
  ① per-piece limit into config + log evidence: MAX_CHUNK_CHARS 6000 -> 1200 (env
     DEEPSEEK_MAX_CHUNK_CHARS overrides), build_log records max_chunk_chars / pieces /
     split_on (none / line_boundary / line_boundary+hard_cut) per entry. A parameter
     change is an "explainable sha change" — it must be discoverable in the log, not by
     oral convention.
  ② cross-config comparable metrics: splitting = calls ×2, so "higher density" must be
     reported paired with "doubled cost" —
       output per thousand chars = table rows / (chunk chars / 1000)
       calls per thousand chars = calls / (chunk chars / 1000)
     e.g. no split 1.9/thousand chars, 1 call vs split 5.8/thousand chars, 2 calls =
     density 3x, cost 2x.

Cost field and fixtures:
  ① fixtures/real1200 (1800~2200 chars, real volume) + meta.json self-reports
     max_chunk_chars, in the main regression set;
  ② density gains the cost side calls_per_1k_chars = pieces / chars × 1000 (paired with
     output per thousand chars);
  ③ the warning_rate=0.0909 red signal was confirmed to be fixture construction
     (prompt74/missing3 deliberately drops 3 rows of quote, 3/33 rows warn), not a real
     problem of the 6000 tier — this explanation goes into the data.

Calling:
     default calls deepseek_api.call(prompt); --replay substitutes a given response text
     for the model (offline self-test/regression, still through the same build_prompt ->
     parse -> normalize pipeline, no validation skipped).
     --replay also accepts {"pieces": ["<piece 1 response>", "<piece 2 response>", ...]}
     for split-piece cases;
     append {"attempts": {"<piece no. from 1>": ["<1st response>", "<2nd response>", ...]}}
     for retry cases (the piece takes the full sequence in order; a missing Nth response
     replays the last one, simulating a "stuck model").

Usage:
    py -3 extract.py --chunk fixtures/pass/chunk.json --out out/entities.json
    py -3 extract.py --chunk ... --replay fixtures/pass/llm_response.json --out ...
    py -3 extract.py --chunk ... --dry-run          # print a prompt summary, don't call the model
    py -3 extract.py --chunk ... --dump-prompt p.txt
    py -3 extract.py --density-scan out/build_log.jsonl   # inspection only, no extraction

Exit code: 0 = extracted and validated; 1 = extraction/validation failed (message to
stderr); 2 = usage/IO error.
"""
import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import unicodedata

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import deepseek_api  # noqa: E402  (must be importable from this directory)

DEFAULT_SCHEMA = HERE / "schema.json"
ISO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")
# The two valid time_norm forms — day precision carries a date only, no clock time.
DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_NORM_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00)$")
TIME_PRECISION_VALUES = ("day", "minute", "second")


def _load_dateparser():
    """Relative-time parsing uses dateparser (it cannot install into the system directory,
    so it lives in the workspace vendor/ directory)."""
    try:
        import dateparser  # noqa: F401
        return dateparser
    except ModuleNotFoundError:
        pass
    vend = HERE.parent.parent / "vendor"
    if vend.is_dir():
        sys.path.insert(0, str(vend))
        try:
            import dateparser
            return dateparser
        except ModuleNotFoundError:
            return None
    return None


DATEPARSER = _load_dateparser()
WS_CHARS = " \t\r\n\u3000\u00a0\ufeff\u200b"

# The A/B/C tier behavior is driven by extract_rules.json next to the schema (table-driven).
# This file no longer lists which field belongs to which tier; every schema attribute must
# explicitly declare fill / on_missing (no defaults — undeclared is a schema error).
RULES_NAME = "extract_rules.json"
DEFAULT_RULES = HERE / RULES_NAME
FILL_VALUES = ("model", "derived")
ON_MISSING_VALUES = ("fill", "reject", "warn", "null")

# Tier B: a piece is retried at most 2 times (3 total attempts); overridable via DEEPSEEK_RETRY_MAX.
MAX_RETRIES = 2
# The two monitoring thresholds
WARNING_RATE_MAX = 0.05
REJECT_RATE_MAX = 0.10


def gate_verdict(den, value, max_value):
    """A gate verdict must consider both (numerator, denominator).

    denominator = 0 => "unverified" — never return "pass": a gate with no samples is a
    gate that is not actually tested; showing it green is the "zero samples counts as
    passing" blind spot (rows_checked=0, null_rows=33 also shows green for this reason).
    Only a denominator > 0 can be pass / fail.
    """
    if not den:
        return "unverified"
    if value is None:
        return "unverified"
    return "pass" if value < max_value else "fail"
# Criterion (explicit-first, sequence anchor) — the explicit coverage lower bound;
# the nearest upper bound FK_NEAREST_RATIO_MAX is defined near FkResolver (co-located
# with its warning text so both change in one place).
FK_EXPLICIT_RATIO_MIN = 0.8
# answer_rows is judged alongside explicit_ratio — coverage passing != answerable.
# A cross-table join question requires "the relevant event and data row are both in
# answer"; aggregate/mention rows do not enter the answer, so a chunk can be at 100%
# coverage yet have only a handful of joinable rows. Both thresholds are hand-picked
# (same style as above): prefer an extra warning over silently passing a chunk that
# "looks covered but is actually unanswerable".
FK_ANSWER_ROWS_MIN = 4        # absolute floor: fewer answer rows means the join has almost nothing to answer
FK_ANSWER_RATIO_MIN = 0.5     # relative floor: answer_rows / total rows across the three tables (not judged when rows is 0)

# Stability check: run the same chunk STABILITY_REPEATS times (temperature fixed 0),
# report median + range; a metric whose half-range (max−min)/2/median exceeds
# STABILITY_TOLERANCE is marked unstable.
# Stability metrics are fixed to these four (one-sided row count / attribution quality /
# answerable row count).
STABILITY_REPEATS = 3
STABILITY_TOLERANCE = 0.10
STABILITY_METRICS = ("rows", "explicit", "nearest", "answer_rows")

# Non-explicit rows mix two things and must be recorded separately, no longer a single "nearest":
#   aggregate = derived totals in metrics/facts (total/count/share/derived value): naturally
#               no single source event, points at "this chunk's summary segment" (anchor
#               anchor=chunk_summary, foreign key takes the nearest card of that segment,
#               only so metrics/facts still has a non-null event_id that check_0 can verify);
#   mention   = name mentions in identities: anchored by source_chunk_id + extraction
#               position + quote, not an event (identities and events are many-to-many;
#               R12 makes identities.event_id optional, the program does not fabricate a key).
FK_METHOD_AGGREGATE = "aggregate"
FK_METHOD_MENTION = "mention"
# Cross-table join only answers with explicit / contain rows (aggregate/mention do not answer).
ANSWER_METHODS = ("explicit", "contain")
# The "role" marker of a temporary field: fk_hint = the three tables use it to explicitly
# name the owning event (the sequence is program-numbered 1..n by extraction position;
# the LLM does not write the number into cards, removing one more place to get it wrong).
TEMP_ROLE_FK_HINT = "fk_hint"
FAILED_DIR = HERE / "failed"

# Input limit per request (in characters). 16000 only moves the line; longer still hits max_tokens.
# The default was lowered from 6000 to 1200 — the agreed spec is "per-piece limit <=1200",
# so that "split vs no split" is comparable (the loop earlier came from a changed prompt sha
# with no log showing the split parameter had changed).
# The limit is config, not a magic constant: the effective value always goes through
# max_chunk_chars() (DEEPSEEK_MAX_CHUNK_CHARS overrides, illegal/<=0 falls back to this
# default), and is written per run into build_log's max_chunk_chars / pieces / split_on.
MAX_CHUNK_CHARS = 1200

BUILD_LOG = HERE / "out" / "build_log.jsonl"
# End-to-end assertion list (every new field must ship with an assertion; see QUERY_FIELD_CONTRACT)
E2E_ASSERTS_PATH = HERE / "e2e_asserts.json"


class ExtractError(RuntimeError):
    """Extraction failed: model output violates the contract / illegal field / foreign key
    cannot be attributed. The message is meant to be read directly."""


class PieceRejected(ExtractError):
    """Tier B: this piece's output is underivable and the row is meaningless without it
    -> hard-reject the piece, triggering "retry this piece"."""


def max_retries():
    raw = os.environ.get("DEEPSEEK_RETRY_MAX")
    if raw:
        try:
            val = int(raw)
            if val >= 0:
                return val
        except ValueError:
            pass
    return MAX_RETRIES


# ---------------------------------------------------------------- warning collection
#
# warnings are stored as dicts: {"piece": piece number or None, "where": row location or
#   None, "code": category, "msg": human-readable text}
#   * non-empty where = row-level warning (counts toward the warning-rate numerator;
#     multiple warnings on the same row count as one row)
#   * empty where = chunk-level hint (split merge, fk ratio, monitoring thresholds, etc.,
#     not counted toward the rate)
# The output still exposes warnings as a text array (legacy calling contract), plus
# warnings_detail.

def warn_row(warnings, where, code, msg, piece=None):
    warnings.append({"piece": piece, "where": where, "code": code, "msg": msg})
    return warnings[-1]


def warn_note(warnings, code, msg):
    warnings.append({"piece": None, "where": None, "code": code, "msg": msg})
    return warnings[-1]


# ---------------------------------------------------------------- basic utilities

def load_json(path, what):
    p = pathlib.Path(path)
    if not p.is_file():
        raise ExtractError("[io] %s 不存在：%s" % (what, p))
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ExtractError("[io] %s 不是合法 JSON：%s：%s" % (what, p, exc)) from exc


def event_hash(chunk_id, pos):
    """schema v6 top-level event_id_algorithm: sha256("chunk_id:offset")[:16]."""
    return hashlib.sha256(("%s:%d" % (chunk_id, pos)).encode("utf-8")).hexdigest()[:16]


def norm_ws(s):
    """Used for quote locating: full/half-width normalization + strip all whitespace
    (same criterion as check_2)."""
    s = unicodedata.normalize("NFKC", str(s))
    for ch in WS_CHARS:
        s = s.replace(ch, "")
    return s


def find_offset(text, needle, start=0, end=None):
    """Locate a quote within the source [start,end) window, returning the starting
    character offset **against the whole chunk**; returns None if not found.

    When split, each piece only searches its own window (piece-local offset + start =
    global offset), ensuring the extraction position is always the offset in the
    "unmodified original chunk" (per R9), not a piece-local offset.
    """
    if not needle:
        return None
    needle = str(needle)
    text = str(text)
    start_ = max(0, int(start))
    end_ = len(text) if end is None else min(int(end), len(text))
    window = text[start_:end_]
    idx = window.find(needle)
    if idx >= 0:
        return start_ + idx
    # match after normalization, then map back to the original offset by character
    target = norm_ws(needle)
    if not target:
        return None
    normed = norm_ws(window)
    hit = normed.find(target)
    if hit < 0:
        return None
    count = 0
    for i, ch in enumerate(window):
        piece = norm_ws(ch)
        if not piece:
            continue
        if count == hit:
            return start_ + i
        count += len(piece)
    return None


# ---------------------------------------------------------------- time base (RELATIVE_BASE)
#
# "Relative to what" is the core of relative time: yesterday / 3 days ago / last week
# must be absolutized against **the data's own time**, never against "the moment of the
# run" (now()). Three levels, finest first: turn > session > chunk.
#   * each row records base_source (which field was used) + base_granularity (turn/session/chunk);
#   * none of the three available => time_norm=null + time_uncertain=true, never now();
#   * in-chunk time span > 24h and containing relative time => warn "chunk-level base
#     unreliable" (see span_alarm_of).
BASE_LEVELS = ("turn_time", "session_time")
BASE_GRANULARITY = {"turn_time": "turn", "session_time": "session", "time_ref": "chunk"}
BASE_FALLBACK_FIELD = "time_ref"


def _base_value(chunk, key):
    """Read the base field value; turn_time also accepts the nested chunk["turn"]={"time":...} form."""
    val = chunk.get(key)
    if isinstance(val, str) and val.strip():
        return val.strip()
    holder = chunk.get(key.split("_")[0])
    if isinstance(holder, dict):
        for sub_key in ("time", "time_ref", "at"):
            inner = holder.get(sub_key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def chunk_time_base(chunk):
    """chunk -> (base_iso, base_source, base_granularity); none available -> (None, None, None).

    Take the finest available: turn > session > chunk(time_ref). No guessing, no now().
    """
    for key in BASE_LEVELS:
        val = _base_value(chunk, key)
        if val and ISO_PATTERN.match(val):
            return val, key, BASE_GRANULARITY[key]
    val = _base_value(chunk, BASE_FALLBACK_FIELD)
    if val and ISO_PATTERN.match(val):
        return val, BASE_FALLBACK_FIELD, BASE_GRANULARITY[BASE_FALLBACK_FIELD]
    return None, None, None


# dateparser only "translates" (time_raw -> time_norm), it never "fills in".
# By default it will invent a date for any phrase (去年 -> today minus a year, 上个月 ->
# today minus a month), so we first suppress the "vague" phrases with a blacklist: vague
# year/month phrases all become None (that is the silent fabrication the verbatim check
# catches).
# Paradigm changed from "whitelist allow" to "accept by default + dangerous-pattern
# blacklist + result self-check". The old whitelist only allowed 3 English templates and
# a few Chinese forms, so phrases dateparser could actually parse ("19:41", "上周") were
# blocked into null — the general flaw of exhaustive designs: anything unlisted is
# dropped, and "unlisted" is always larger than "listed". Now only two kinds are blocked:
#   ① DP_DANGEROUS: vague phrases the parser would "fill in" (去年 -> this year minus a
#     year, 上个月 -> today minus a month);
#   ② DP_INFER: phrases that require inference (下周三, 周末 — depend on "which day the
#     week starts", different implementations give different answers).
# Everything else goes to dateparser; the parsed result then passes
# self_check_time_result (result self-check), and only a mismatch becomes null.
DP_DANGEROUS = re.compile(
    r"(去年|前年|明年|今年|本年|上年|下年|上一年|下一年|上个?月|下个?月|本月|该月|"
    r"上个?季度|下个?季度|本季度|季度末|"
    r"\b(last|next|this)\s+(year|month|quarter|decade)\b|"
    r"\b\d+\s+(year|month|decade)s?\s+ago\b|\b(years?|months?)\s+ago\b)", re.I)
DP_INFER = re.compile(
    r"(上上?个?周[一二三四五六日天]|下下?个?周[一二三四五六日天]|"
    r"(本|这|下个?|上个?)周[一二三四五六日天]|"
    r"(上上?|下下?|本|这)?个?周末|"
    r"\b(next|last|this|coming)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\b(the\s+)?(weekend|weekday)\b)", re.I)
DP_AMBIGUOUS = DP_DANGEROUS  # old name kept (self-tests / external scripts still reference it)
DP_MAX_LEN = 40
DP_ACCEPT = None  # the allow-list is deprecated (symbol kept so old scripts don't break on import)
DP_ABS_PATTERNS = (
    re.compile(r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(\s*\d{1,2}[:：]\d{2}([:：]\d{2})?)?$"),
    # an absolute time with a timezone offset is also not "relative time" (the span alarm should not count it)
    re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})$"),
)


def time_expr_rejected(raw):
    """Only phrases "known to fabricate" or "requiring inference" are rejected; the rest
    are accepted by default."""
    if not isinstance(raw, str) or not raw.strip():
        return True
    s = unicodedata.normalize("NFKC", raw).strip()
    if len(s) > DP_MAX_LEN:
        return True
    return bool(DP_DANGEROUS.search(s) or DP_INFER.search(s))


def time_expr_accepted(raw):
    """Accept by default (the exhaustive whitelist is deprecated) — anything not on the
    dangerous blacklist goes to dateparser."""
    return not time_expr_rejected(raw)


# duration conversion used by the result self-check ("3 days ago" must really equal base - 3 days)
_DURATION_TO_SECONDS = {
    "秒": 1, "second": 1, "seconds": 1,
    "分钟": 60, "分": 60, "minute": 60, "minutes": 60,
    "小时": 3600, "hour": 3600, "hours": 3600,
    "天": 86400, "day": 86400, "days": 86400,
    "周": 604800, "星期": 604800, "week": 604800, "weeks": 604800,
}
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "半": 0.5}


def cn_number(tok):
    """「3」/「三」/「两」/「半」/「十二」/「二十」-> float; returns None if unparseable (no guessing)."""
    if not isinstance(tok, str) or not tok.strip():
        return None
    t = tok.strip()
    if t.isdigit():
        return float(t)
    if t in _CN_DIGITS:
        return float(_CN_DIGITS[t])
    if t == "十":
        return 10.0
    if "十" in t:
        head, _, tail = t.partition("十")
        h = _CN_DIGITS.get(head, 1) if head else 1
        m = _CN_DIGITS.get(tail, 0) if tail else 0
        if head and head not in _CN_DIGITS:
            return None
        if tail and tail not in _CN_DIGITS:
            return None
        return float(h * 10 + m)
    return None


def _iso_to_dt(iso):
    """ISO (+08:00 / Z) -> aware datetime; returns None if unparseable."""
    from datetime import datetime
    if not isinstance(iso, str) or not iso.strip():
        return None
    s = iso.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S+08:00", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=shanghai_tz()).astimezone(shanghai_tz())
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(shanghai_tz())
    except ValueError:
        return None


SELFCHECK_MAX_YEARS = 100


def self_check_time_result(raw, dt, base_iso=None):
    """**Result self-check** — the parsed result must be explainable from the source text;
    if it cannot, the parser was "filling in".

    Three checks (all must hold to accept):
      ① source text has a clock time (19:41) => the result's hour:minute must match exactly;
      ② "N time units ago" => the result must equal base - N (difference > 90s = fail);
      ③ absolute/other => the result year must not differ from base by more than
         SELFCHECK_MAX_YEARS years (guards against 去年/上个月 being turned into a year).
    Returns (ok, reason); if base is missing and the phrase is relative => do not pass
    self-check (prefer null over guessing a base).
    """
    from datetime import timedelta
    if dt is None:
        return False, "解析器没给出结果"
    s = unicodedata.normalize("NFKC", str(raw or "")).strip()
    m = DP_TIME_CLOCK.search(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        # When the source carries its own offset (…-05:00 / Z), the clock time is that
        # timezone's wall clock: convert back to the source timezone before comparing,
        # otherwise the Shanghai-converted 08:41 would be judged "not equal to 19:41"
        # (the timezone fixture case is exactly this).
        view = dt
        off = re.search(r"([+-])(\d{2}):?(\d{2})$", s)
        if off or s.endswith("Z"):
            from datetime import timedelta as _td, timezone as _tz
            delta = _td(0) if s.endswith("Z") else _td(
                hours=int(off.group(2)), minutes=int(off.group(3)))
            if off and off.group(1) == "-":
                delta = -delta
            view = dt.astimezone(_tz(delta))
        if (view.hour, view.minute) != (hh, mm):
            return False, ("原文钟点 %02d:%02d 与解析结果 %02d:%02d 不符"
                           % (hh, mm, view.hour, view.minute))
    d = DP_DURATION.search(s)
    if d:
        num = cn_number(d.group(1))
        unit = d.group(2).lower()
        secs = _DURATION_TO_SECONDS.get(unit)
        base = _iso_to_dt(base_iso)
        if num is None or secs is None:
            return False, "相对时长 %r%r 算不出来" % (d.group(1), d.group(2))
        if base is None:
            return False, "相对时长缺基准（base_iso=%r）" % (base_iso,)
        want = base - timedelta(seconds=num * secs)
        if abs((want - dt).total_seconds()) > 90:
            return False, ("相对时长与基准算出的时刻不符（原文 %r；按基准应为 %s，解析给 %s）"
                           % (s, want.isoformat(), dt.isoformat()))
        return True, "ok"
    base = _iso_to_dt(base_iso)
    if base is not None and abs(dt.year - base.year) > SELFCHECK_MAX_YEARS:
        return False, ("结果年份 %d 离基准年份 %d 超过 %d 年（疑似被补出来的日期）"
                       % (dt.year, base.year, SELFCHECK_MAX_YEARS))
    return True, "ok"


def is_absolute_time_expr(raw):
    """Absolute date phrasing (not relative time): the span alarm only looks at chunks
    that contain relative time."""
    if not isinstance(raw, str):
        return False
    s = unicodedata.normalize("NFKC", raw).strip()
    return any(p.match(s) for p in DP_ABS_PATTERNS)


def shanghai_tz():
    """tzinfo for Asia/Shanghai (timezone-aware, no longer string-concatenating +08:00).

    Prefers zoneinfo (tzdata in vendor/); falls back to a fixed +08:00 if unavailable —
    prefer degradation over losing the timezone.
    """
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001  missing tzdata must not drop the timezone
        from datetime import timedelta, timezone
        return timezone(timedelta(hours=8))


# the "hour:minute[:second]" marker in source text (after NFKC full-width colons also become half-width)
DP_TIME_CLOCK = re.compile(r"(\d{1,2})[:\uff1a](\d{2})(?:[:\uff1a](\d{2}))?")
# duration-style relative phrases carry their own precision — "3 分钟前" is minute-precise,
# not day (otherwise 20:02 would be smeared into "some day", losing real source info).
# Unit mapping in DURATION_UNIT_PRECISION.
DP_DURATION = re.compile(
    r"(\d+|[一二三四五六七八九十两半]+)\s*(秒|分钟|分|小时|天|周|星期|second|minute|hour|day|week)s?"
    r"\s*(前|ago)", re.I)
DURATION_UNIT_PRECISION = {
    "秒": "second", "second": "second",
    "分钟": "minute", "分": "minute", "小时": "minute", "minute": "minute", "hour": "minute",
    "天": "day", "周": "day", "星期": "day", "day": "day", "week": "day",
}
# The old criterion only recognized "3 分钟前 / 3 minutes ago", so forward-looking
# phrases like "A minute later / in two hours / 5 分钟后" all fell to day, downgrading
# minute-level relative time to "date only" (the clock time in time_norm got erased).
# Here forward phrases are also recognized as durations, precision follows the unit
# (minute-level -> minute), no longer defaulting to day.
# Only "definite numbers" are recognized (digit / a / an / one..ten); vague quantifiers
# like "a few / 几分钟" do not count — those are handled by the "unparseable => unknown"
# path, never forced into day.
# Two shapes: "in two hours / 两小时后" (starts with in, no trailing time adverb needed)
# and "A minute later / 5 分钟后" (number + unit + time adverb).
DP_DURATION_FWD = re.compile(
    r"\bin\s+(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|一|二|两|三|四|五|六|七|八|九|十|几|半)\s*"
    r"(秒|分钟|分|小时|天|周|星期|second|minute|hour|day|week)s?\b"
    r"|"
    r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|一|二|两|三|四|五|六|七|八|九|十|几|半)\s*"
    r"(秒|分钟|分|小时|天|周|星期|second|minute|hour|day|week)s?\s*"
    r"(?:later|after|from now|内|后)", re.I)


def time_precision_of(raw):
    """The **source text's** time precision (day / minute / second), not a precision the
    parser invented.

    * has seconds -> second; only "hour:minute" -> minute;
    * only a date or relative day word (昨天 / last week / 2026-03-14) -> day;
    * no time_raw -> None ("unknown" is not "only known to the day").
    Duration-style relative phrases are recognized in both directions (3 分钟前 / A minute
    later / in two hours), precision follows the unit; the old implementation only
    recognized "前 / ago" and judged "A minute later" as day, erasing the clock time.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = unicodedata.normalize("NFKC", raw).strip()
    m = DP_TIME_CLOCK.search(s)
    if m:
        return "second" if m.group(3) is not None else "minute"
    m = DP_DURATION.search(s)
    if m:
        unit = m.group(2).lower()
        return DURATION_UNIT_PRECISION.get(unit, "day")
    m = DP_DURATION_FWD.search(s)
    if m:
        # two-branch group numbering: in-branch unit is group(2), number+adverb branch unit is group(4)
        unit = (m.group(2) or m.group(4) or "").lower()
        return DURATION_UNIT_PRECISION.get(unit, "day")
    return "day"


def format_time_norm(dt, precision):
    """datetime -> time_norm string; day precision writes **the date only**.

    Background: "昨天" is day-precise in the source, but the old implementation stored
    T10:00:00 — that is the **BASE's clock time**, not the record's time; asking "what
    time yesterday was it changed" would then yield an invented answer. So day precision
    records only the date, making "what time" unanswerable (the correct answer is "only
    known to be yesterday"); minute/second precision records the clock time.
    minute precision records HH:MM:00 (the seconds are padded, not an observed value; the
    spec is in the schema description).
    """
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=shanghai_tz())
    aware = aware.astimezone(shanghai_tz())
    if precision == "day":
        return aware.strftime("%Y-%m-%d")
    return aware.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _dp_parse(raw, base_iso):
    """dateparser parse (RELATIVE_BASE explicitly passes the data's own time); low
    confidence / parse failure -> None."""
    if DATEPARSER is None:
        raise ExtractError(
            "dateparser 不可用（task-102 二）：请确认 工作区/vendor/ 下有 dateparser，"
            "或跑 py -3 runs/102/fetch_wheels.py + py -3 runs/102/install_vendor.py")
    if time_expr_rejected(raw):
        return None
    from datetime import datetime
    base = None
    if isinstance(base_iso, str) and ISO_PATTERN.match(base_iso):
        base = datetime.strptime(base_iso, "%Y-%m-%dT%H:%M:%S+08:00")
    if base is None and not is_absolute_time_expr(raw):
        # relative time with no base => unparseable. **never fall back to dateparser's default now()**
        return None
    settings = {"TIMEZONE": "Asia/Shanghai", "TO_TIMEZONE": "Asia/Shanghai",
                "RETURN_AS_TIMEZONE_AWARE": True}
    if base is not None:
        settings["RELATIVE_BASE"] = base
    try:
        dt = DATEPARSER.parse(unicodedata.normalize("NFKC", raw).strip(),
                              languages=["zh", "en"], settings=settings)
    except Exception:  # noqa: BLE001  parser internal errors are treated as "unparseable", no guessing
        return None
    if dt is None:
        return None
    # The parsed result must carry a timezone (RETURN_AS_TIMEZONE_AWARE is on); a bare
    # time is interpreted as Asia/Shanghai then converted uniformly — time_norm's timezone
    # is no longer a dropped string suffix.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=shanghai_tz())
    dt = dt.astimezone(shanghai_tz())
    # Result self-check — the parsed result must be explainable from source+base; if not,
    # null (prefer nothing over guessing)
    ok, why = self_check_time_result(raw, dt, base_iso if isinstance(base_iso, str) else None)
    if not ok:
        return None
    return dt


def rel_time(raw, ref_iso):
    """Resolve a relative time against ref_iso into an absolute time (dateparser replaces
    the hand-written Chinese regex).

    Unparseable / low confidence returns None (no guessing).
    """
    return _dp_parse(raw, ref_iso)


def datetime_from(fmt, raw):
    from datetime import datetime
    return datetime.strptime(raw, fmt)


def normalize_time(raw, ref_iso=None, base_iso=None):
    """time_raw -> fixed-length ISO8601 (YYYY-MM-DDTHH:MM:SS+08:00); **None if underivable,
    never guess**.

    This function is the sole source of time_norm (any model-reported value is discarded).
    dateparser (translates only, never fills in); the relative-time base is the **data's
    own time** (base_iso, finest of turn>session>chunk), not now(); no base available and
    the phrase is relative => None.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = unicodedata.normalize("NFKC", raw).strip()
    precision = time_precision_of(raw)
    if ISO_PATTERN.match(raw):
        return raw
    if DAY_PATTERN.match(raw):
        # already a "date only" form: verbatim is a legal day-precision time_norm
        return raw
    base = base_iso if isinstance(base_iso, str) and base_iso.strip() else ref_iso
    dt = _dp_parse(raw, base)
    if dt is None:
        return None
    return format_time_norm(dt, precision)


# ---------------------------------------------------------------- op_type English aliases
#
# Background: English narrative actions (went to / painted / signed / scored) were all
# judged "other" — the prompt only stated Chinese criteria, and English action words were
# not in the alias table. The fix is not "make the model smarter" but to write the
# "action word -> enum" mapping as a program-recomputable alias table (both Chinese and
# English): when the model says other but the action word maps to a concrete enum in the
# table, the program re-judges and records a warning (op_type_alias). Only words absent
# from the table may stay "other".
# Order: backup/restart/read come before write (backup/restart/verify all carry a "doing"
# sense; take the specific one first).
#
# The above is the word table; the **judgment algorithm** is two further rules (see
# alias_op_type) — main-clause restriction + longest match. A misjudgment like 检查
# stealing 改写 is not a missing table entry but a whole-sentence substring match that
# returns on the first hit.

MAIN_CLAUSE_SPLIT = re.compile(r"[,，;；。]|\.(?:\s|$)|\band\b|\bthen\b|\bafter that\b", re.I)

OP_TYPE_ALIAS = (    ("backup", (
        r"\bback(ed)?\s*up\b", r"\bbackup(s|ped)?\b", r"\barchiv(e|ed|ing)\b",
        r"\bcopi(ed|es|ing)\b", r"\bcopy\b",
        "备份", "归档", "拷贝", "复制",
    )),
    ("restart", (
        r"\brestart(ed|s|ing)?\b", r"\breboot(ed|s|ing)?\b", r"\brelaunch(ed|es|ing)?\b",
        "重启", "重新启动", "重开",
    )),
    ("read", (
        r"\bread\b", r"\bre-?read\b", r"\bcheck(ed|s|ing)?\b", r"\binspect(ed|s|ing)?\b",
        r"\breview(ed|s|ing)?\b", r"\bexamin(e|ed|es|ing)\b", r"\bverif(y|ied|ies|ying)\b",
        r"\blook(ed|s|ing)?\s+(at|into|over)\b", r"\bopen(ed|s|ing)?\b",
        r"\bscan(ned|s|ning)?\b", r"\blist(ed|s|ing)?\b",
        # phrase-level aliases (longer than words; longest-match automatically beats generic words like check/read)
        r"\bwent\s+through\b", r"\bwent\s+over\b", r"\bgo(es|ing)?\s+through\b",
        r"\bread\s+through\b", r"\bcompared?\b", r"\bcompar(e|ed|es|ing)\s+with\b",
        r"\bsigned\s+off\b", r"\bcross-?check(ed|s|ing)?\b", r"\bwent\s+back\s+over\b",
        "读取", "核对", "复核", "检查", "审阅", "打开", "扫描", "列出", "过了一遍",
        "对比", "比较", "通读",
    )),
    ("write", (
        r"\bwrit(e|es|ten|ing)\b", r"\bwrote\b", r"\bcreat(e|ed|es|ing)\b",
        r"\bedit(ed|s|ing)?\b", r"\bupdat(e|ed|es|ing)\b", r"\bmodif(y|ied|ies|ying)\b",
        r"\bfix(ed|es|ing)?\b", r"\bpatch(ed|es|ing)?\b", r"\bsav(e|ed|es|ing)\b",
        r"\btyp(e|ed|es|ing)\b", r"\bdraft(ed|s|ing)?\b", r"\bcompos(e|ed|es|ing)\b",
        r"\bgenerat(e|ed|es|ing)\b", r"\bproduc(e|ed|es|ing)\b", r"\bbuild\b",
        r"\bbuilt\b", r"\bmak(e|es|ing)\b", r"\bmade\b", r"\badd(ed|s|ing)?\b",
        r"\bdelet(e|ed|es|ing)\b", r"\bremov(e|ed|es|ing)\b", r"\brenam(e|ed|es|ing)\b",
        r"\binstall(ed|s|ing)?\b", r"\bpaint(ed|s|ing)?\b", r"\bdr(ew|awn|aw|aws|awing)\b",
        r"\bsign(ed|s|ing)?\b", r"\bwrit(e|ten)\b", r"\bappend(ed|s|ing)?\b",
        r"\bwrote\s+up\b", r"\bwrote\s+down\b", r"\bfilled\s+in\b",
        # single-character aliases removed (写/读/画 hit almost everywhere in Chinese and are
        # misjudgment sources); kept entries are two-plus-character forms that clearly name an action.
        "写入", "改写", "编写", "修复", "修改", "保存", "新建", "创建", "删除", "重命名",
        "安装", "生成", "签名", "签署", "追加", "补了", "填了",
    )),
    ("other", (
        r"\bgo(es|ing)?\s+to\b", r"\bwent\b", r"\bvisit(ed|s|ing)?\b", r"\bwalk(ed|s|ing)?\b",
        r"\bsay\b", r"\bsaid\b", r"\btell\b", r"\btold\b", r"\bask(ed|s|ing)?\b",
        r"\brepl(y|ied|ies|ying)\b", r"\bthink(s|ing)?\b", r"\bthought\b",
        r"\bdecid(e|ed|es|ing)\b", r"\blaugh(ed|s|ing)?\b", r"\bsmil(e|ed|es|ing)\b",
        r"\bscor(e|ed|es|ing)\b", r"\bnotic(e|ed|es|ing)\b", r"\bsuggest(ed|s|ing)?\b",
        "指出", "提到", "说到", "建议", "判断", "决定", "认为", "笑", "得分", "前往", "去了",
    )),
)
OP_ALIAS_DEFAULT = "other"


def leading_clause(text):
    """Take only the main clause (before the first comma/semicolon/period/and/then): the
    main-clause verb is the action of this event.

    The old implementation substring-matched the **whole sentence**, so the 检查 in
    "把 red-sample 断言拆成每个检查独立断言" stole the match from 改写 (table order read
    before write), judging write as read — that is a **table flaw**, not a model flaw.
    Main-clause + longest-match fix both together.
    """
    if not isinstance(text, str):
        return ""
    parts = MAIN_CLAUSE_SPLIT.split(text, 1)
    return parts[0] if parts else text


def alias_op_type(text, quote=None):
    """Action text -> op_type enum (Chinese and English aliases; falls back to other, no
    guessing).

      * **main clause first**: only search for the action word in the main clause; look at
        the whole sentence only if the main clause gives no result;
      * **longest match first**: phrases (went through / signed off on / wrote up /
        compared) beat single words; words compare by length, ties fall back to "table
        order (specific first)";
      * **single-character Chinese aliases removed** (写/读/画): single characters hit
        almost everywhere in Chinese and are misjudgment sources;
      * when both quote (source) and action are given: the source is an English verbatim,
        prefer the source.
    """
    for src in (text, quote):
        if not isinstance(src, str) or not src.strip():
            continue
        for scope in (leading_clause(src), src):
            hit = _alias_longest(scope)
            if hit:
                return hit
    return OP_ALIAS_DEFAULT


def _alias_longest(text):
    """Find the "longest matching alias" in text; ties by position (earlier = main verb),
    then by table order."""
    low = text.lower()
    best = None      # (length, -position, -table order, op)
    for order, (op, cues) in enumerate(OP_TYPE_ALIAS):
        for cue in cues:
            if cue.startswith("\\b"):
                m = re.search(cue, low)
                if not m:
                    continue
                span_len, pos = len(m.group(0)), m.start()
            else:
                pos = low.find(cue)
                if pos < 0:
                    continue
                span_len = len(cue)
            key = (span_len, -pos, -order)
            if best is None or key > best[0]:
                best = (key, op)
    return best[1] if best else None


ALIASERS = {"op_type": alias_op_type}


def time_raw_verbatim(raw, text):
    """time_raw is pure verbatim — it must appear as-is in the chunk source (whitespace
    normalization also counts).

    Not found = the model rewrote the source (yesterday -> 昨天, relative -> absolute), a
    contract violation; returns False.
    """
    if not isinstance(raw, str) or not raw.strip():
        return True
    if not isinstance(text, str):
        return True
    return raw.strip() in text or norm_ws(raw) in norm_ws(text)


# ---------------------------------------------------------------- chunk splitting

def max_chunk_chars():
    raw = os.environ.get("DEEPSEEK_MAX_CHUNK_CHARS")
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return MAX_CHUNK_CHARS


def split_chunk(text, limit=None):
    """Split chunk source into pieces ≤limit along line boundaries, returning
    [(start, end), ...] (global offsets into the original chunk).

    A single overlong line is hard-cut (the cut lands mid-line), no characters dropped.
    """
    limit = int(limit or max_chunk_chars())
    text = str(text)
    if len(text) <= limit:
        return [(0, len(text))]
    spans = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + 1, end)
            if cut > start:
                end = cut + 1  # keep the newline at the piece tail; next piece starts at the next line
        spans.append((start, end))
        start = end
    return spans


# Split basis (build_log.split_on) — "how it was cut" must be inspectable; the pieces
# number alone cannot tell "parameter changed" from "model changed" (prompt sha changes
# must be explainable).
SPLIT_ON_NONE = "none"                 # not split (one call for the whole chunk)
SPLIT_ON_LINE = "line_boundary"        # every cut point lands on a newline
SPLIT_ON_HARD = "line_boundary+hard_cut"  # some single line overlong, hard-cut (cut mid-line)


def split_on_of(text, spans):
    """Whether these pieces were cut on line boundaries, or some single line was hard-cut —
    recorded into build_log.split_on."""
    if len(spans) <= 1:
        return SPLIT_ON_NONE
    hard = any(str(text)[end - 1:end] != "\n" for _, end in spans[:-1])
    return SPLIT_ON_HARD if hard else SPLIT_ON_LINE


# ---------------------------------------------------------------- prompt

PROMPT_TEMPLATE = """你是「聚合方案」的抽取器。把下面一个 chunk 原文抽成 4 张表的 JSON。

## 路由（先判每段/每句属于哪一类，再决定进哪张表；多表不互斥，同一片段可同时进多表；互斥只在同类内容）
- 卡片 cards：只放事件（谁 + 做了什么 + 时间 + 结果 + op_type）。**没有事件 → 本表写「无」**。
- 数值表 metrics：数值落一行（**数值的权威在数值表**；卡片 result 可以叙述数值，但数值表必须落一行）。**没有数值 → 本表写「无」**。
- 标识表 identities：文件名/路径/仓库/版本/工具/模型等标识。**没有标识别名 → 本表写「无」**。
- fact 断言句 facts：陈述性**断言**（X 是 Y / X 属于 Y / X 处于某状态），**不限于公式换算**——「the plan is field work」「本周还剩 3 件」都是 fact，「1 m = 100 cm」只是公式特例。左值指标名 = 主语（规范写法），等式 = 断言内容。**与 cards 的界线：cards 记「发生了什么」（事件+动作），facts 记「什么是什么」（断言+状态）。**没有断言 → 本表写「无」。**
- **「无」是标准答案、不是偷懒**：某张表这一片确实没有对应内容，就写「无」，不要为了填满而把不匹配的内容硬塞进去。

## 输出语言（硬规则，不是建议）
- **所有文本字段一律用原文语言**：英文 chunk 写英文、中文 chunk 写中文，**不许翻译、不许中英混写**。
- 左值指标名用原文语言的规范主语：英文事实写「Thomas Kyd birthplace」，**不要**写「Thomas Kyd 出生地」。
- 等式用原文语言重述断言：英文事实写「Thomas Kyd was born in London」，**不要**写「Thomas Kyd 出生于 London」。

## 字段（由 schema + extract_rules.json 生成；一字不许改、不许加暗字段 additionalProperties=false，空字段省略）
{fields_block}

## op_type（卡片必填，按顺序问句判定）
① 动了文件？写/改 -> write，备份 -> backup；② 读了文件并给出核对结论？-> read；
③ 重启了？-> restart；④ 都不是（说话/下判断/提建议/指出）-> other。
示例：指出=other、复核=read、建议=other、跑测试并写了文件（报告/json/日志）=write、跑测试但没落盘=other。
英文剧情同样按动作语义判（不是按语言判）：wrote / created / edited / updated / fixed / saved /
painted / signed / generated -> write；read / checked / inspected / reviewed / verified -> read；
backed up / copied / archived -> backup；restarted / rebooted -> restart；
said / asked / decided / thought / went to / scored 这类没有「动东西」的动作 -> other。

## 时间三列
- time_raw：原文写法，**逐字照抄**（英文就抄英文，yesterday 不许写成 昨天）；原文没有时间词就留空，
  留空 = null，不拒整片、也不许编一个。
- time_norm：ISO8601 定长 YYYY-MM-DDTHH:MM:SS+08:00，**程序按 time_raw 推**（可以留空）；
  推不出的（去年 / last year 这类含糊说法）就 null，**绝不许自己填一个精确日期**。
- time_ref：输入侧参数＝chunk 事件时间，数值表必填（本 chunk 的 time_ref = {time_ref}）。

## 唯一键 + event_id（R9/R10，方案 C）
- 抽取位置 = 该事件/行在**未改动的原始 chunk** 中的起始字符偏移（Python 下标从 0 起，不是序号）。
- 卡片 event_id = sha256((source_chunk_id + ':' + 抽取位置) 的 UTF-8 字节).hexdigest()[:16]；你把 抽取位置 写对，event_id 可留空由程序复算。
- **三表不填 event_id**：metrics / identities / facts 一律不要写 event_id 字段，程序按偏移自动归属到事件卡片。

## 所属事件序号（临时字段：程序读完就 pop，不落产物、不在 schema）
{temp_fields_block}

## quote 与指标名（硬规则，不是建议）
- **行照抽；quote 给得出就给、给不出就省略 quote 字段**，该行标 `quote_missing` 记 warning，不整份作废。
- 引文闸只统计「能给出 quote 的行」：quote 命中数 / (行数 − 缺引文行) ≥ 0.95（缺引文的行不进分母，单列成「引文缺口」）。
- 抽取位置 非空的行必须带 quote.原文 且能原样命中原文。
- 给不出能命中的 quote：**行照抽，只是不写 抽取位置**；quote 给不出就整条省略 quote 字段（程序记 warning）。
  **缺的是引文，不是事实——数据行不许因为缺引文就消失。**
- 指标名用规范名（不带单位），别名走标识表 canonical_id，禁止同指标多种写法混进 metrics。

## 输入 chunk
source_chunk_id: {chunk_id}
time_ref: {time_ref}
{part_note}原文（字符偏移从 0 起，即下面正文第一个字符的位置是 0）：
<<<CHUNK
{text}
CHUNK>>>

## 输出
只输出一个 JSON 对象，键固定为 cards / metrics / identities / facts，值为数组；「无」= 空数组 []：没有内容的表输出空数组 []，不要为了填满而硬塞不匹配的内容；不要解释、不要 markdown 代码围栏。
"""


# prompt self-check — the text must not contain any "delete-row instruction".
# Optional field != optional row: this is the recurring failure (optional event_id read
# as "the whole row may be omitted", missing quote read as "drop this row"). A hit is a
# hard error, so a defective prompt is never sent.
PROMPT_DELETE_ROW_PATTERNS = (
    "不要单独成行", "别单独成行", "不要成行", "别成行",
    "不要写这一行", "别写这一行", "不要这一行", "别要这一行",
    "这行别要", "整行可省",
)


def check_prompt_no_delete_rows(prompt):
    """A delete-row instruction in the prompt -> hard error (ExtractError, no request sent).

    Returns {"ok": True, "patterns": [...]}, which green cases use to prove the self-check
    is actually running.
    """
    text = prompt or ""
    hits = [p for p in PROMPT_DELETE_ROW_PATTERNS if p in text]
    if hits:
        raise ExtractError(
            "prompt 自检失败：文本里出现删行指令 %r——字段可选≠行可选，"
            "行照抽（缺 quote 只省略 quote 字段、不写 抽取位置），不许让数据行消失" % (hits,))
    return {"ok": True, "patterns": list(PROMPT_DELETE_ROW_PATTERNS)}


def render_fields_block(rules):
    """The prompt's "fields" section is generated from schema + rules: adding a field only
    changes schema/rules, no code change needed."""
    lines = []
    for fk in rules.order:
        required, optional, auto, nullable = [], [], [], []
        for name, spec in rules.tables[fk].items():
            if spec.get("enum"):
                label = "%s(%s)" % (name, "|".join(str(x) for x in spec["enum"]))
            elif spec.get("type") == "object":
                subs = list(((rules.props[fk].get(name) or {}).get("properties") or {}))
                label = "%s{%s}" % (name, ",".join(subs))
            elif spec.get("type") == "number":
                label = "%s(number)" % name
            else:
                label = name
            if spec["on_missing"] == "reject":
                required.append(label)
            elif spec["on_missing"] == "warn":
                optional.append(label)
            elif spec["on_missing"] == "null":
                # "default" != "garbage" — absent means write null, neither required nor forbidden
                # null's meaning is per-field (canonical_id is "not found", not "absent from
                # source"), so spec.null_note may override this generic explanation.
                nullable.append("%s（%s）" % (name, spec["null_note"])
                                if spec.get("null_note") else label)
            else:
                auto.append("%s（%s）" % (name, spec["note"]) if spec.get("note") else name)
        if required:
            lines.append("%s 必填: %s" % (fk, ", ".join(required)))
        if optional:
            lines.append("        选填: %s" % ", ".join(optional))
        if nullable:
            lines.append("        可空（原文没有就写 null，不许编一个）: %s" % ", ".join(nullable))
        if auto:
            lines.append("        程序自补（不用写）: %s" % ", ".join(auto))
        temps = rules.temp_fields.get(fk) or {}
        if temps:
            labels = []
            for name, spec in temps.items():
                labels.append("%s（%s）" % (name, spec.get("note")) if spec.get("note") else name)
            lines.append("        临时字段（程序读完就 pop，不落产物）: %s" % ", ".join(labels))
    return "\n".join(lines)


def render_temp_fields_block(rules):
    """The three tables reference an event via 所属事件序号 — the program numbers them by
    extraction position, the LLM does not write the number.

    Field/table names are all driven by rules (the _temp_fields role); the code hard-codes
    no field-name literal. The positive/negative examples are a hard requirement (the
    negative example uses the real number 1816 = the value's own offset the model actually
    filled in a real call).
    """
    lines = []
    for fk in rules.order:
        for name, spec in (rules.temp_fields.get(fk) or {}).items():
            if spec.get("role") != TEMP_ROLE_FK_HINT:
                continue
            lines.append("- %s 每行加一个 **%s**（整数）= 该行所属事件的序号。" % (fk, name))
    if not lines:
        return "（本 chunk 无临时字段）"
    lines.append("")
    lines.append("序号是**程序给的**，不是原文偏移：cards 请**按事件在原文里出现的顺序**输出；"
                 "程序会按 抽取位置 升序把 cards 编号 1..n，你直接引用这个号。")
    lines.append("- 正例：事件 3 是「改了阈值 720896」，那这条数值行就写 所属事件序号=3。")
    lines.append("- 反例：**不要写该数值在原文里的位置（如 1816）**——1816 是这个值自己的偏移，"
                 "不是源事件的序号；写成 1816 就是越界，程序会报红。")
    lines.append("- 说得出就写；说不准就省略（程序按偏移回落 contain/nearest；"
                 "nearest 行标低置信、不进答案）。")
    lines.append("- 这个字段**只给归属用**：写完立刻被程序 pop 掉，不进最终产物，也不算 schema 字段。")
    lines.append("- 序号越界（不在 1..n 内）会被程序报红并单独计数——给错和没给是分开算的。")
    return "\n".join(lines)


def build_prompt(chunk, part=None, rules=None, schema=None):
    part_note = ""
    if part:
        idx, total = part
        part_note = "本片：第 %d/%d 片（超长 chunk 已拆片，逐片独立抽取，程序会按唯一键合并）\n" % (idx, total)
    if rules is None:
        schema = schema if schema is not None else load_json(DEFAULT_SCHEMA, "schema")
        rules = load_rules(schema)
    prompt = PROMPT_TEMPLATE.format(
        time_ref=(chunk.get("time_ref")
                  or "（本 chunk 无时间基准：相对时间一律可空、不许编）"),
        chunk_id=chunk["source_chunk_id"],
                                    text=chunk["text"], part_note=part_note,
                                    fields_block=render_fields_block(rules),
                                    temp_fields_block=render_temp_fields_block(rules))
    # self-check "no delete-row instruction" before sending (fail fast, don't run the model on a defective prompt)
    check_prompt_no_delete_rows(prompt)
    return prompt


# ---------------------------------------------------------------- model output parsing

def strip_fences(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def parse_entities_text(text):
    raw = strip_fences(text)
    try:
        doc = json.loads(raw)
    except Exception:  # noqa: BLE001  grab the first {...} when there is chatter around it
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ExtractError("模型输出不是 JSON：%r" % raw[:200])
        try:
            doc = json.loads(raw[start:end + 1])
        except Exception as exc:  # noqa: BLE001
            raise ExtractError("模型输出 JSON 解析失败：%s（前 200 字：%r）"
                               % (exc, raw[:200])) from exc
    if not isinstance(doc, dict):
        raise ExtractError("模型输出顶层必须是对象，实际 %s" % type(doc).__name__)
    return doc


def schema_props(ent):
    return ent.get("properties") or {}


def keep_known_fields(row, props, warnings, where, piece=None):
    """R6: hidden fields must not pass. Discarded in place with a warning (check_0 still
    hard-validates the final data)."""
    unknown = [k for k in row if k not in props]
    for k in unknown:
        row.pop(k, None)
        warn_row(warnings, where, "unknown_field",
                 "%s: 丢弃 schema 外的字段 %r（R6 不许塞暗字段）" % (where, k), piece=piece)
    return row


def scope_of(chunk):
    return chunk.get("_scope") or (0, len(chunk["text"]))


# ---------------------------------------------------------------- foreign-key attribution

def card_spans(cards, text_len, pos_field, qkey=None, qtkey=None):
    """Each card's event interval [start, end] (end prefers the quote.原文 length; without
    a quote it takes the next card's start).

    Field names come from schema + rules (cards likewise hard-code no field name).
    The interval carries the card's quote head (used in fk detail for "which card it
    attached to").
    """
    ordered = sorted(cards, key=lambda r: r[pos_field])
    spans = []
    for i, card in enumerate(ordered):
        start = int(card[pos_field])
        quote = card.get(qkey) if qkey and isinstance(card.get(qkey), dict) else {}
        qtext = quote.get(qtkey) if qtkey else None
        if qtext:
            end = start + len(str(qtext))
        elif i + 1 < len(ordered):
            end = int(ordered[i + 1][pos_field])
        else:
            end = text_len
        spans.append((start, max(end, start), card["event_id"], _quote_head(qtext)))
    return spans


def _quote_head(text, limit=24):
    """Quote head (truncated to limit chars, ellipsis when overlong): for locating which
    card, not storing the whole span."""
    if not text:
        return None
    s = str(text)
    return s if len(s) <= limit else s[:limit] + "…"


# nearest-ratio alarm line (>10% means card granularity is too coarse; the nearest-row
# detail is listed alongside the alarm)
FK_NEAREST_RATIO_MAX = 0.1


def _as_event_seq(value):
    """Normalize the model's "event sequence" to an int; returns None if absent or
    ill-formed.

    "Wrong" and "missing" must stay distinguishable: this only handles the shape;
    out-of-range / pointing at a nonexistent sequence is judged by FkResolver.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        t = value.strip()
        if t.lstrip("-").isdigit():
            return int(t)
    return None


class FkResolver:
    """The three tables' event_id is assigned by the program by offset; the LLM does not
    fill it.

    Attribution order explicit -> contain -> nearest, explicit via 所属事件序号:
    ⓪ the model's 所属事件序号 = N, within 1..n and pointing at some card of this piece
        -> explicit;
        out-of-range (not in 1..n) / not an integer -> red hint_invalid (count + warning,
        not a silent fallback), still falls back to contain/nearest but must not count as
        "missing" (wrong != missing);
    ① this row's offset falls inside some card's event interval -> that card (contain);
    ② inside no interval -> the "nearest card with offset ≤ this row" (nearest, low
        confidence);
    ③ this chunk has no card at all -> error (the three-table rows must attach to some
        event).

    Per-row log adds "which card + how far" (card_pos / card_quote / delta), recorded in
    build_log's fk_method.detail; aggregate / mention rows are per-row locatable.
    Rows that do not answer get confidence=low.
    Sequences are resolved per piece (piece no. -> {event sequence: event_id}); equal
    numbers across pieces do not collide.
    The fallback is no longer uniformly "nearest" — it is split by table (table-driven:
    the table whose event_id is optional = mention):
      * mention   (identities): anchor = source_chunk_id + extraction position + quote,
        event_id not written (R12);
      * aggregate (metrics/facts): derived total, no single source event, anchor = this
        chunk's summary segment (anchor=chunk_summary, foreign key takes the nearest card
        of the summary segment, keeping metrics/facts' foreign key non-null).
      Both tables can still first hit contain (the row offset falls inside some card's
      interval = this value really happened that day).
    """

    def __init__(self, spans, text_len, pos_field, seq_maps=None, reorder=False,
                 tables=None, mention_tables=None):
        # table order (per-table stats must be able to list 0-row tables) + which tables have
        # optional event_id (= mention tables)
        self.tables = list(tables or [])
        self.mention_tables = set(mention_tables or ())
        # R14 facts independence: with no cards, mention tables (facts/identities) can still
        # be written (event_id=null); only non-mention tables (metrics) need a card to aggregate.
        if not spans:
            non_mention = [t for t in self.tables if t not in self.mention_tables]
            if non_mention:
                raise ExtractError("该 chunk 一张卡片都没有：非可选表行无法归属事件"
                                   "（%s 仍必须挂在事件下）" % "、".join(non_mention))
        self.spans = spans
        self.text_len = text_len
        self.pos_field = pos_field
        self.log = []
        self._span_of = {sp[2]: sp for sp in spans}
        # piece no. -> {event sequence: event_id} (sequences are piece-local; they repeat
        # across pieces, so one global table would collide)
        self.seq_maps = seq_maps or {}
        self.reorder = bool(reorder)   # LLM output order was not ascending, the program re-sorted
        self.hint_ok = 0            # gave a sequence and hit a card
        self.hint_invalid = 0       # gave a sequence but illegal (out of range / points at nothing)
        self.hint_missing = 0       # gave no sequence
        self.hint_invalid_rows = []  # alarm detail (where / piece / hint)

    def _is_mention_table(self, table):
        return table in self.mention_tables

    def _summary_anchor(self, probe):
        """An aggregate row's anchor = this chunk's summary segment (the last card with
        offset ≤ this row)."""
        behind = [sp for sp in self.spans if sp[0] <= probe]
        if behind:
            return max(behind, key=lambda sp: sp[0])
        return min(self.spans, key=lambda sp: abs(sp[0] - probe))

    def assign(self, row, where, hint=None, piece=None, table=None):
        pos = row.get(self.pos_field)
        if isinstance(pos, bool) or not isinstance(pos, int) or pos < 0:
            pos = None
        probe = self.text_len if pos is None else pos
        seq = _as_event_seq(hint)
        given = not (hint is None or (isinstance(hint, str) and not hint.strip()))
        eid, method, hint_status = None, None, "missing"
        if given:
            mapping = self.seq_maps.get(piece) or {}
            if seq is not None and seq in mapping:
                eid, method, hint_status = mapping[seq], "explicit", "explicit"
                self.hint_ok += 1
            else:
                hint_status = "invalid"
                self.hint_invalid += 1
                self.hint_invalid_rows.append({"where": where, "piece": piece,
                                               "hint": hint, "hint_seq": seq})
        else:
            self.hint_missing += 1
        span = None
        if eid is None:
            for (start, end, cand, _q) in self.spans:
                if start <= probe <= end:
                    eid, method = cand, "contain"
                    span = (start, end, cand, _q)
                    break
        anchor = "event"
        if eid is None:
            if self._is_mention_table(table):
                # R12: a name mention has no "single source event" — do not fabricate a key;
                # the anchor is chunk+offset+quote
                method = FK_METHOD_MENTION
                anchor = "chunk_quote"
                eid = None
            else:
                method = FK_METHOD_AGGREGATE
                anchor = "chunk_summary"
                span = self._summary_anchor(probe)
                eid = span[2]
        if eid is None:
            row.pop("event_id", None)      # mention: event_id optional, absent means not written (not empty/placeholder)
        else:
            row["event_id"] = eid
        row["__fk_method"] = method      # temporary marker (stripped uniformly after answer is computed)
        if span is None:
            span = self._span_of.get(eid) or (None, None, eid, None)
        quote = row.get("quote")
        self.log.append({"where": where, "table": table, "pos": pos, "method": method,
                         "event_id": eid, "anchor": anchor,
                         "confidence": "high" if method in ANSWER_METHODS else "low",
                         "answerable": method in ANSWER_METHODS,
                         "has_quote": isinstance(quote, dict) and _nonempty(quote.get("原文")),
                         # pairwise coverage reasons per "data row", carrying the quote source per row
                         "quote": (quote.get("原文") if isinstance(quote, dict) else None),
                         "hint": hint, "hint_seq": seq, "hint_status": hint_status,
                         "card_pos": span[0], "card_quote": span[3],
                         "delta": (pos - span[0]) if (pos is not None and span[0] is not None)
                                 else None})
        return eid, method

    def nearest_rows(self):
        return [dict(r) for r in self.log if r["method"] == "nearest"]

    def aggregate_rows(self):
        return [dict(r) for r in self.log if r["method"] == FK_METHOD_AGGREGATE]

    def mention_rows(self):
        return [dict(r) for r in self.log if r["method"] == FK_METHOD_MENTION]

    def _table_stats(self, table):
        """Coverage is computed per table, never merged.

        metrics / facts: explicit / (rows − aggregate_rows) — aggregate rows have no
        attributable event and must not enter the denominator, otherwise "derived totals"
        would proportionally drag coverage into a meaningless number.
        identities: only the mention coverage matters (has a quote and is locatable),
        and does **not** enter the explicit denominator.

        An empty denominator (all aggregate, or an empty table) writes the ratio as
        **null** (N/A), never 1.0. Writing 1.0 is "full marks" in report terms — a chunk
        that is all summary values would show "explicit 100%" while no value actually
        attached to an event. Empty-denominator tables are marked vacuous=true and do not
        enter the overall passing denominator (see summary()).
        """
        rows = [r for r in self.log if r.get("table") == table]
        n = len(rows)
        counts = {}
        for r in rows:
            counts[r["method"]] = counts.get(r["method"], 0) + 1
        explicit = counts.get("explicit", 0)
        contain = counts.get("contain", 0)
        if self._is_mention_table(table):
            locatable = sum(1 for r in rows if r.get("has_quote") and r.get("pos") is not None)
            return {"kind": "mention", "rows": n, "explicit": explicit, "contain": contain,
                    "mention": counts.get(FK_METHOD_MENTION, 0),
                    "aggregate": 0,
                    "locatable": locatable,
                    # an empty table also does not report 100%: no comparable rows, ratio is null
                    "mention_coverage": round(locatable / n, 4) if n else None,
                    "vacuous": n == 0,
                    "in_explicit_denominator": False,
                    "rule": "identities：单独看 mention 覆盖率（有 quote 且能定位就算），"
                            "不进 explicit 分母；行数 0 -> 比值 null"}
        aggregate = counts.get(FK_METHOD_AGGREGATE, 0)
        denom = n - aggregate
        return {"kind": "value", "rows": n, "explicit": explicit, "contain": contain,
                "mention": 0, "aggregate": aggregate, "denominator": denom,
                "explicit_ratio": round(explicit / denom, 4) if denom > 0 else None,
                "vacuous": denom == 0,
                "rule": "metrics/facts：explicit / (rows − aggregate_rows)；分母 0 -> 比值 null"}

    def summary(self):
        count = {}
        for r in self.log:
            count[r["method"]] = count.get(r["method"], 0) + 1
        explicit, contain = count.get("explicit", 0), count.get("contain", 0)
        aggregate = count.get(FK_METHOD_AGGREGATE, 0)
        mention = count.get(FK_METHOD_MENTION, 0)
        nearest = count.get("nearest", 0)
        rows = len(self.log)
        per_table = {t: self._table_stats(t) for t in self.tables}
        # empty-denominator tables are counted separately; "passing" only looks at tables
        # with a denominator (scored_tables); empty-denominator tables count neither as
        # full marks nor as failing — they simply do not participate in ratio terms.
        vacuous_tables = [t for t, s in per_table.items() if s.get("vacuous")]
        scored_tables = [t for t, s in per_table.items()
                         if not s.get("vacuous") and s.get("denominator")]
        return {"rows": rows, "explicit": explicit, "contain": contain,
                "aggregate": aggregate, "mention": mention, "nearest": nearest,
                # the criterion is per-table (per_table / coverage); no more "merged" explicit ratio
                "per_table": per_table,
                "coverage": per_table,
                # empty-denominator (null ratio) table list + the tables that enter the passing stats
                "vacuous_tables": vacuous_tables,
                "vacuous_table_count": len(vacuous_tables),
                "scored_tables": scored_tables,
                "no_offset": sum(1 for r in self.log if r["pos"] is None),
                "low_confidence": sum(1 for r in self.log if r.get("confidence") == "low"),
                # correct / wrong / missing counted in three separate buckets (wrong must not blend into "missing")
                "hint_given": self.hint_ok + self.hint_invalid,
                "hint_ok": self.hint_ok,
                "hint_invalid": self.hint_invalid,
                "hint_missing": self.hint_missing,
                "hint_invalid_ratio": round(self.hint_invalid / (rows or 1), 4),
                "hint_invalid_rows": list(self.hint_invalid_rows),
                "reorder": self.reorder,   # cards were re-sorted = the LLM order is not trusted
                "detail": list(self.log),
                "aggregate_rows": self.aggregate_rows(),   # per-row locatable
                "mention_rows": self.mention_rows(),
                "nearest_rows": self.nearest_rows()}


# ---------------------------------------------------------------- table-driven tiers
#
# A/B/C tiers are no longer written in code: extract_rules.json gives each schema
# attribute two properties:
#   on_missing=reject -> tier B: missing => hard-reject that piece and retry it;
#   on_missing=warn   -> tier C: missing => strip + tolerate with warning (no score impact);
#   on_missing=fill   -> tier A: the program fills it by the algorithm named in derive (silent).
# Code only iterates schema.entities[*].properties for attributes, never listing field names.
# Consistency check: reject ⇒ required; warn ⇒ non-required; fill ⇒ either.

class Rules:
    """The validated view of extract_rules.json × schema (table.field -> attribute)."""

    def __init__(self, schema, doc, path):
        self.schema = schema
        self.doc = doc
        self.path = str(path)
        self.tables = {}            # file_key -> {field: spec} (keeps schema attribute order)
        self.props = {}             # file_key -> schema attribute definition
        self.entities = {}          # file_key -> schema entity
        self.order = []             # file_key order (schema entities order)
        self.cards_key = None       # the card table's file_key
        self.quote_key = {}         # file_key -> the quote object's field name (the only object attribute in schema)
        self.quote_text_key = {}    # file_key -> the quote body sub-key (the object's first required sub-field)
        self.pos_field = {}         # file_key -> the offset field name (the field with derive=offset_from_quote)
        self.temp_fields = {}       # file_key -> {temporary field name: spec} (not in schema, popped after read)
        self.hint_field = {}        # file_key -> the temporary field name used to explicitly name the owning event
        self.mention_tables = set()  # tables whose event_id is optional (nullable) = mention tables
        self.nullable_fields = []    # nullable field list (into report, for human review)
        self.report = {}            # consistency check report

    def pop_temp_fields(self, table, row):
        """Pop temporary fields out of the row (not into the output), returning {field name:
        value}.

        Only declared fields are popped; schema attributes are never touched. The call site
        must be before keep_known_fields.
        """
        out = {}
        if not isinstance(row, dict):
            return out
        for name in (self.temp_fields.get(table) or {}):
            if name in row:
                out[name] = row.pop(name)
        return out


def load_rules(schema, path=None):
    """Read extract_rules.json, cross-check against schema both ways + run the consistency
    check; any inconsistency raises a schema error."""
    path = pathlib.Path(path) if path else DEFAULT_RULES
    doc = load_json(path, "extract_rules")
    entries = {k: v for k, v in doc.items() if not str(k).startswith("_")}
    rules = Rules(schema, doc, path)
    problems = []
    reject_ok, warn_ok, fill_ok, null_ok = [], [], [], []
    seen = set()
    # load the generic alias table (alias -> canonical_id -> canonical name, one table for
    # both languages, source traceable)
    alias_doc = doc.get("_identity_alias") or {}
    if not isinstance(alias_doc, dict):
        problems.append("_identity_alias: 必须是对象 {entries:[...]}")
    else:
        problems.extend(set_alias_table(alias_doc.get("entries")))
    for ent_name, ent in (schema.get("entities") or {}).items():
        fk = ent.get("file_key")
        if not fk:
            raise ExtractError("schema 错：entities.%s 缺 file_key" % ent_name)
        if fk in rules.tables:
            raise ExtractError("schema 错：file_key %r 重复" % fk)
        rules.order.append(fk)
        rules.entities[fk] = ent
        props = ent.get("properties") or {}
        rules.props[fk] = props
        required = set(ent.get("required") or [])
        if ent_name == "card":
            rules.cards_key = fk
        objects = [n for n, p in props.items() if (p or {}).get("type") == "object"]
        qkey = objects[0] if objects else None
        rules.quote_key[fk] = qkey
        rules.quote_text_key[fk] = None
        if qkey:
            subs = (props.get(qkey) or {}).get("required") or []
            rules.quote_text_key[fk] = subs[0] if subs else None
        table = {}
        for name, prop in props.items():
            key = "%s.%s" % (fk, name)
            seen.add(key)
            spec = entries.get(key)
            if not isinstance(spec, dict):
                problems.append("%s: 没有在 %s 里声明（不设默认值，必须显式表态）"
                                % (key, RULES_NAME))
                continue
            fill, on_missing = spec.get("fill"), spec.get("on_missing")
            if fill not in FILL_VALUES:
                problems.append("%s: fill=%r 不在 %s" % (key, fill, list(FILL_VALUES)))
            if on_missing not in ON_MISSING_VALUES:
                problems.append("%s: on_missing=%r 不在 %s"
                                % (key, on_missing, list(ON_MISSING_VALUES)))
            # consistency: required and on_missing must not contradict each other
            if on_missing == "reject" and name not in required:
                problems.append("%s: on_missing=reject 但 schema 没标 required"
                                "（一致性规则：reject ⇒ required）" % key)
            if on_missing == "warn" and name in required:
                problems.append("%s: on_missing=warn 但 schema 标了 required"
                                "（一致性规则：warn ⇒ 非 required）" % key)
            if on_missing == "fill" and fill != "derived":
                problems.append("%s: on_missing=fill 只能配 fill=derived（模型字段程序补不出）"
                                % key)
            # "default" != "garbage" — write null when there is no time info, not reject the piece.
            # consistency rule: on_missing=null ⇒ schema must mark the field nullable (one source
            # of truth; no "code says nullable, schema says required" split).
            if on_missing == "null":
                if not (prop or {}).get("nullable"):
                    problems.append("%s: on_missing=null 但 schema 没标 nullable"
                                    "（一致性规则：null ⇒ schema.nullable=true）" % key)
                if fill not in FILL_VALUES:
                    problems.append("%s: on_missing=null 的 fill=%r 不在 %s"
                                    % (key, fill, list(FILL_VALUES)))
            # nullable may only be added to a derived field that is "non-required in schema" —
            # nullability is decided by schema.required (one source); rules only declare "the
            # program may leave it unwritten"
            if spec.get("nullable"):
                # a field with on_missing=null may stay in required — the program writes null
                # when absent (the key always exists; "is there info" is expressed by the value);
                # other nullable fields must still be non-required
                if name in required and on_missing != "null":
                    problems.append("%s: nullable=True 但 schema 标了 required"
                                    "（一致性规则：nullable ⇒ 非 required，R12）" % key)
                if fill != "derived" or on_missing != "fill":
                    if on_missing != "null":
                        problems.append("%s: nullable=True 只能配 fill=derived + on_missing=fill"
                                        "（或 task-101 的 on_missing=null：缺省写 null）" % key)
            if fill == "derived" and spec.get("derive") not in DERIVERS:
                problems.append("%s: fill=derived 但 derive=%r 不是已实现的派生算法 %s"
                                % (key, spec.get("derive"), sorted(DERIVERS)))
            if on_missing == "reject":
                reject_ok.append(key)
            elif on_missing == "warn":
                warn_ok.append(key)
            elif on_missing == "fill" and fill == "derived":
                fill_ok.append(key)
            elif on_missing == "null":
                null_ok.append(key)
            # English action-word aliases — put the alias resolver in spec too, no hard-coded table name
            if spec.get("alias") and spec.get("alias") not in ALIASERS:
                problems.append("%s: alias=%r 不是已实现的别名解析器 %s"
                                % (key, spec.get("alias"), sorted(ALIASERS)))
            stored = {"fill": fill, "on_missing": on_missing,
                      "derive": spec.get("derive"), "inputs": spec.get("inputs") or {},
                      "type": (prop or {}).get("type"), "enum": (prop or {}).get("enum"),
                      "pattern": (prop or {}).get("pattern"),
                      "required": name in required, "nullable": bool(spec.get("nullable")),
                      "verbatim": bool(spec.get("verbatim")),
                      "alias": spec.get("alias"),
                      "alias_inputs": spec.get("alias_inputs") or {},
                      "null_note": spec.get("null_note"),
                      "note": spec.get("note")}
            # the enum declared in rules is a mirror of the schema enum (widening must sync both)
            if "enum" in spec and list(spec.get("enum") or []) != list((prop or {}).get("enum") or []):
                problems.append("%s: rules.enum 与 schema.enum 不一致（%r vs %r，扩枚举要两边同步）"
                                % (key, spec.get("enum"), (prop or {}).get("enum")))
            table[name] = stored
            if stored["derive"] == "offset_from_quote":
                rules.pos_field[fk] = name
        rules.tables[fk] = table
        # which table's event_id is optional (nullable) = which table goes mention (name mention)
        if (table.get("event_id") or {}).get("nullable"):
            rules.mention_tables.add(fk)
            rules.nullable_fields.append("%s.event_id" % fk)
    # temporary fields (_temp_fields) — not in schema, only used to pull 关联事件位置 out of the
    # model output. Validation: the table must exist, the field name must not collide with a
    # schema attribute, and it must be fill=model + on_missing=warn (optional).
    temp_doc = doc.get("_temp_fields") or {}
    if not isinstance(temp_doc, dict):
        problems.append("_temp_fields 必须是对象（{表.字段: spec}）")
        temp_doc = {}
    for key, spec in temp_doc.items():
        fk_name, _, fname = str(key).partition(".")
        if fk_name not in rules.tables:
            problems.append("_temp_fields.%s: 表 %r 不在 schema 里" % (key, fk_name))
            continue
        if fname in (rules.props.get(fk_name) or {}):
            problems.append("_temp_fields.%s: 临时字段不能同时是 schema 属性（临时字段必须程序 pop）"
                            % key)
            continue
        if not isinstance(spec, dict):
            problems.append("_temp_fields.%s: spec 必须是对象" % key)
            continue
        if spec.get("fill") != "model":
            problems.append("_temp_fields.%s: fill 必须是 model（临时字段只由模型产出）" % key)
        if spec.get("on_missing") != "warn":
            problems.append("_temp_fields.%s: on_missing 必须是 warn（漏写不许拒片/不许程序补）" % key)
        stored = {"fill": spec.get("fill"), "on_missing": spec.get("on_missing"),
                  "role": spec.get("role"), "note": spec.get("note")}
        rules.temp_fields.setdefault(fk_name, {})[fname] = stored
        if spec.get("role") == TEMP_ROLE_FK_HINT:
            rules.hint_field[fk_name] = fname
    # reverse direction: keys in rules with no schema attribute -> likewise a schema error
    # (adding a field must match on both sides)
    for key in entries:
        if key not in seen:
            problems.append("%s: schema 里没有这个「表.字段」（多声明同样是 schema 错）" % key)
    # quote object: only sub-fields schema marks required are mandatory; other sub-fields
    # (like quote.归一化) are stripped as optional
    for fk, qkey in rules.quote_key.items():
        if not qkey:
            continue
        qprop = rules.props[fk][qkey] or {}
        for sub, sprop in (qprop.get("properties") or {}).items():
            if not isinstance(sprop, dict):
                problems.append("%s.%s.%s: schema 子字段定义坏了" % (fk, qkey, sub))
    rules.report = {"reject_fields": reject_ok, "warn_fields": warn_ok,
                    "fill_fields": fill_ok, "null_fields": null_ok,
                    "verbatim_fields": ["%s.%s" % (fk, n)
                                        for fk, tbl in rules.tables.items()
                                        for n, s in tbl.items() if s.get("verbatim")],
                    "alias_fields": ["%s.%s" % (fk, n) for fk, tbl in rules.tables.items()
                                     for n, s in tbl.items() if s.get("alias")],
                    "nullable_fields": list(rules.nullable_fields),
                    "mention_tables": sorted(rules.mention_tables),
                    # alias-table self-description — how many aliases / canonical names / per-language counts / whether sources are present
                    "identity_alias": {
                        "aliases": len(ALIAS_TABLE),
                        "canonical_ids": len(ALIAS_CANONICAL),
                        "by_lang": {lg: sum(1 for v in ALIAS_TABLE.values() if v.get("lang") == lg)
                                    for lg in sorted({v.get("lang") for v in ALIAS_TABLE.values()} - {None})},
                        "missing_source": [k for k, v in ALIAS_TABLE.items() if not v.get("source")],
                        "table": {k: {"canonical_id": v["canonical_id"],
                                      "canonical_name": v["canonical_name"],
                                      "lang": v.get("lang"), "source": v.get("source")}
                                  for k, v in ALIAS_TABLE.items()},
                    },
                    "temp_fields": ["%s.%s" % (fk, n)
                                    for fk, names in rules.temp_fields.items() for n in names],
                    "problems": problems,
                    "schema_rules_consistent": not problems}
    if problems:
        raise ExtractError("extract_rules.json 与 schema 不一致（schema 错）：\n- "
                           + "\n- ".join(problems))
    return rules


# ---------------------------------------------------------------- derive algorithms (derive name -> implementation)
#
# Only "how to compute some field" lives here. Which field belongs to which tier, and
# whether to fill it, is decided by extract_rules.json; which other fields an algorithm
# reads is given by rules' inputs (so no field-name literal appears in this file).

def canonical_key(raw):
    """R11: canonical_id's derived value = the normalized form of 完整原文写法 (NFKC + strip
    whitespace)."""
    return norm_ws(unicodedata.normalize("NFKC", str(raw))) or None


# **Generic alias table** — alias -> canonical_id -> canonical name, both languages in one
# table. Background: throughput and 吞吐量 each become their own canonical_id and cannot
# join into one metric; the same problem exists inside Chinese (成功次数 vs 通过用例数).
# The table is extract_rules.json's identity_alias.entries, each entry carrying lang +
# source (traceable); a form absent from the table falls back to source normalization (old
# criterion unchanged).
ALIAS_TABLE = {}      # alias(NFKC+lower) -> {canonical_id, canonical_name, lang, source}
ALIAS_CANONICAL = {}  # canonical_id -> canonical name


def alias_norm(s):
    return unicodedata.normalize("NFKC", str(s)).strip().lower()


def set_alias_table(entries):
    """Load the alias table (called by load_rules); returns the list of validation problems
    (duplicate alias / missing field are table errors)."""
    global ALIAS_TABLE, ALIAS_CANONICAL
    ALIAS_TABLE, ALIAS_CANONICAL = {}, {}
    problems = []
    for i, e in enumerate(entries or [], 1):
        if not isinstance(e, dict):
            problems.append("identity_alias.entries[%d]: 不是对象" % i)
            continue
        alias, cid, name = e.get("alias"), e.get("canonical_id"), e.get("canonical_name")
        if not (_nonempty(alias) and _nonempty(cid) and _nonempty(name)):
            problems.append("identity_alias.entries[%d]: 缺 alias/canonical_id/canonical_name" % i)
            continue
        key = alias_norm(alias)
        if key in ALIAS_TABLE:
            problems.append("identity_alias: 别名 %r 重复（一个别名只能指向一个规范名）" % alias)
            continue
        ALIAS_TABLE[key] = {"canonical_id": cid, "canonical_name": name,
                            "lang": e.get("lang"), "source": e.get("source")}
        ALIAS_CANONICAL[cid] = name
    return problems


def resolve_canonical(raw):
    """Alias table -> canonical_id; **absent from table => None (say "unknown", never store
    as-is)**.

    Highest priority, pollution type: the old criterion "absent from table -> fall back to
    source normalization" turned guesswork into fact — any transient string became a
    canonical_id that looked normal, then never joined and nobody noticed. The current
    criterion: canonical_id may only come from the table; not found =>
    canonical_id=null + unresolved=true.
    """
    if not _nonempty(raw):
        return None
    hit = ALIAS_TABLE.get(alias_norm(raw))
    if hit:
        return hit["canonical_id"]
    return None


def is_canonical_registered(canonical_id):
    """Whether this canonical_id is registered in the table (null / values outside the
    table both count as unregistered)."""
    if not _nonempty(canonical_id):
        return False
    return str(canonical_id) in ALIAS_CANONICAL


def canonical_name_of(canonical_id):
    """canonical_id -> canonical name (None if absent from the table, no guessing)."""
    return ALIAS_CANONICAL.get(str(canonical_id or ""))


# **Downstream must not "use it and drop it"**.
# Old criterion: rows with canonical_id=null enter no group, so aggregation/retrieval
# silently loses N rows while looking normal = silent reduction. Now: ① count
# unresolved_rows separately; ② the answer can say "N more names were unrecognized".
# Criterion: run aggregation with 1 unregistered name => unresolved_rows=1 (not a quiet −1).
UNRESOLVED_ROWS_KEY = "unresolved_rows"


def count_unresolved_rows(entities, table=None):
    """Count rows whose "name is unregistered" (unresolved=true); table=None counts all tables."""
    tables = [table] if table else list(entities or {})
    return sum(1 for t in tables for r in ((entities or {}).get(t) or [])
               if isinstance(r, dict) and r.get("unresolved") is True)


def unresolved_note(n):
    """A human-readable gap statement that can go into the answer (0 is stated as-is, not hidden)."""
    return "另有 %d 条名称未识别（canonical_id=null，未参与归组）" % n


def aggregate_by_identity(entities, rules=None, table="identities",
                          group_field="canonical_id"):
    """Aggregate by canonical name; unresolved rows are counted separately, **never skipped
    silently**.

    Returns groups (each registered canonical_id is one group: row count + source chunks),
    unresolved_rows (unregistered rows that cannot enter a group), note (the gap statement
    carried into the answer).
    """
    groups, unresolved = {}, 0
    rows = [r for r in ((entities or {}).get(table) or []) if isinstance(r, dict)]
    for row in rows:
        cid = row.get(group_field)
        if row.get("unresolved") is True or not is_canonical_registered(cid):
            unresolved += 1
            continue
        g = groups.setdefault(cid, {"canonical_id": cid,
                                    "canonical_name": canonical_name_of(cid),
                                    "rows": 0, "sources": []})
        g["rows"] += 1
        if row.get("source_chunk_id"):
            g["sources"].append(row["source_chunk_id"])
    return {"table": table, "groups": [groups[k] for k in sorted(groups)],
            "total_rows": len(rows), "grouped_rows": len(rows) - unresolved,
            UNRESOLVED_ROWS_KEY: unresolved, "note": unresolved_note(unresolved),
            "rule": "未登记 canonical_id 的行走 unresolved_rows，不静默丢弃；"
                    "答案要带「另有 N 条名称未识别」"}


def scope_unresolved_rows(scope=None, table="identities"):
    """Total unresolved rows across the whole library (or an explicit set) — for the
    retrieval side to state the gap."""
    _, entries = lookup_scope_entries(scope)
    return sum(count_unresolved_rows(e["entities"], table=table) for e in entries)

def _locate_quote(row, chunk, ctx):
    """Take the quote field name / body sub-key name from rules, then locate in source (the
    sole source of the extraction position, R9)."""
    rules, table = ctx["rules"], ctx["table"]
    qkey, tkey = rules.quote_key.get(table), rules.quote_text_key.get(table)
    if not qkey or not tkey:
        return None
    quote = row.get(qkey)
    needle = quote.get(tkey) if isinstance(quote, dict) else None
    if not _nonempty(needle):
        return None
    start, end = ctx.get("scope") or (0, len(chunk["text"]))
    return find_offset(chunk["text"], needle, start, end)


def _derive_source_chunk_id(row, spec, chunk, ctx):
    return chunk["source_chunk_id"]


def _derive_time_ref(row, spec, chunk, ctx):
    # time_ref = the finest base available for this row (turn>session>chunk); none of the three -> None
    return chunk.get("_base_iso") or chunk.get("time_ref")


def _derive_time_norm(row, spec, chunk, ctx):
    raw = row.get(spec["inputs"].get("raw"))
    ref = chunk.get("time_ref")
    base = chunk.get("_base_iso") or ref
    return normalize_time(raw, ref, base) or None


def _derive_time_precision(row, spec, chunk, ctx):
    """This row's source time precision (day/minute/second/unknown); no time_raw -> null.

    **"Unparseable" is not "only known to the day"** — with a time_raw but empty time_norm
    (colloquial vague phrasing dateparser really cannot parse), precision records unknown,
    not day/minute (recording day lies to downstream: "I know which day this is"). Only no
    time_raw is null.
    """
    inputs = spec.get("inputs") or {}
    raw = row.get(inputs.get("raw") or TIME_RAW_FIELD)
    if not _nonempty(raw):
        return None
    norm = row.get(inputs.get("norm") or TIME_NORM_FIELD)
    if _is_blank(norm):
        return "unknown"
    return time_precision_of(raw)


def _derive_offset_from_quote(row, spec, chunk, ctx):
    return _locate_quote(row, chunk, ctx)


def _derive_card_event_id(row, spec, chunk, ctx):
    pos = row.get(spec["inputs"].get("pos"))
    if isinstance(pos, bool) or not isinstance(pos, int) or pos < 0:
        return None
    return event_hash(chunk["source_chunk_id"], pos)


def _derive_fk_event_id(row, spec, chunk, ctx):
    fk = ctx.get("fk")
    if fk is None:
        return None
    return fk.assign(row, ctx["where"], hint=ctx.get("hint"), piece=ctx.get("piece"),
                     table=ctx.get("table"))[0]


def _derive_canonical_from_raw_writing(row, spec, chunk, ctx):
    # first check the alias table (both languages); absent from table => None (no fallback to source normalization)
    return resolve_canonical(row.get(spec["inputs"].get("raw")))


def _derive_unresolved_from_canonical(row, spec, chunk, ctx):
    """canonical_id not found (null) and source writing not empty => unresolved=true.

    "Not found" must be an **explicit state**, not inferred from "canonical_id happens to
    be empty" — downstream join skips on null, but on true it knows this row is "recognized
    the name but it was unregistered".
    """
    inputs = spec.get("inputs") or {}
    raw = row.get(inputs.get("raw") or "完整原文写法")
    cid = row.get(inputs.get("canonical") or "canonical_id")
    if not _nonempty(raw):
        return False
    return not is_canonical_registered(cid)


def _derive_base_source(row, spec, chunk, ctx):
    """Which field this row uses as its relative-time base (turn_time/session_time/time_ref)."""
    return chunk.get("_base_source")


def _derive_base_granularity(row, spec, chunk, ctx):
    """Base granularity turn/session/chunk (finest of the three)."""
    return chunk.get("_base_granularity")


def _derive_time_uncertain(row, spec, chunk, ctx):
    """Has time_raw but cannot derive time_norm (including a missing base) = time uncertain
    (not fabricated)."""
    inputs = spec.get("inputs") or {}
    raw = row.get(inputs.get("raw") or TIME_RAW_FIELD)
    norm = row.get(inputs.get("norm") or TIME_NORM_FIELD)
    return bool(_nonempty(raw)) and _is_blank(norm)


DERIVERS = {
    "source_chunk_id": _derive_source_chunk_id,
    "time_ref": _derive_time_ref,
    "time_norm": _derive_time_norm,
    "time_precision": _derive_time_precision,
    "offset_from_quote": _derive_offset_from_quote,
    "card_event_id": _derive_card_event_id,
    "fk_event_id": _derive_fk_event_id,
    "canonical_from_raw_writing": _derive_canonical_from_raw_writing,
    "unresolved_from_canonical": _derive_unresolved_from_canonical,
    "base_source": _derive_base_source,
    "base_granularity": _derive_base_granularity,
    "time_uncertain": _derive_time_uncertain,
}
# derive execution order (dict-ordered): block-level/locating first, then event_id foreign
# key, then canonical name last
DERIVE_ORDER = tuple(DERIVERS)


# ---------------------------------------------------------------- empty-field strip / hard reject (tier from rules)

def _is_blank(v):
    """Empty-field check: None / empty string / all-whitespace string."""
    return v is None or (isinstance(v, str) and not v.strip())


def _nonempty(v):
    return isinstance(v, str) and bool(v.strip())


def strip_optional_empty(row, table, rules, where, warnings, piece=None):
    """Tier C: a field with on_missing=warn that is empty -> strip + warning (strip must run
    before required-field validation).

    Non-required sub-fields inside an object field (quote), like quote.归一化, are likewise
    "strip when empty" — their optionality is decided by schema's quote.required (R11 keeps
    only 原文).
    """
    if not isinstance(row, dict):
        return row
    for name, spec in rules.tables[table].items():
        if spec["on_missing"] != "warn":
            continue
        if name in row and _is_blank(row.get(name)):
            row.pop(name, None)
            warn_row(warnings, where, "empty_optional",
                     "%s: %r 为空 -> strip（on_missing=warn，C 档：缺了不影响判分）"
                     % (where, name), piece=piece)
    qkey = rules.quote_key.get(table)
    if qkey and isinstance(row.get(qkey), dict):
        qprop = rules.props[table].get(qkey) or {}
        sub_required = set(qprop.get("required") or [])
        for sub in (qprop.get("properties") or {}):
            if sub in sub_required:
                continue
            if sub in row[qkey] and _is_blank(row[qkey].get(sub)):
                row[qkey].pop(sub, None)
                warn_row(warnings, where, "empty_optional",
                         "%s: %s.%s 为空 -> strip（父对象里非 required 的选填子字段）"
                         % (where, qkey, sub), piece=piece)
    return row


# these fields are not "facts of the row" and cannot serve as text anchors (quote/offset/derived id/input-side parameters)
TEXT_ANCHOR_SKIP = ("source_chunk_id", "time_norm", "time_precision", "time_ref",
                    "event_id", "抽取位置",
                    "canonical_id", "quote", "类型")


def _row_text_anchors(row):
    """Text anchors in the row that can be checked back against the source (name/metric
    name/value/equation/action…), longer first (more specific)."""
    anchors = []
    for key, val in row.items():
        if key in TEXT_ANCHOR_SKIP or str(key).startswith("__"):
            continue
        if isinstance(val, bool) or val is None:
            continue
        if isinstance(val, (int, float)):
            s = str(val)
        elif isinstance(val, str):
            s = val.strip()
        else:
            continue
        if len(s) >= 2:
            anchors.append((len(s), s))
    anchors.sort(key=lambda x: -x[0])
    return [s for _, s in anchors]


def _derive_offset_from_row_text(row, chunk, scope):
    """Without a quote, use the row's text anchors to find a position in the source (the row
    is still extracted; the position is filled by the program).

    Not found returns (None, None) — the caller records the row in unlocated_rows, never
    silently dropped.
    """
    text = chunk["text"]
    a, b = scope
    for s in _row_text_anchors(row):
        idx = text.find(s, a, b)
        if idx >= 0:
            return idx, s
    return None, None


def _check_quote_offset(row, chunk, scope, rules, table, where, warnings=None):
    """Tier B structural rule: an offset must have a matching quote; without an offset, first
    locate by quote, then fall back to text anchors.

    "No quote available" is no longer a delete-row instruction — the row is still extracted,
    the position filled by the program (missing quote only records a warning); a row that
    also finds no text anchor is recorded in unlocated_rows + warning, never silently
    vanishes. The quote field name / body sub-key / offset field name all come from schema +
    rules; this function hard-codes no field name.
    """
    warnings = warnings if warnings is not None else []
    qkey = rules.quote_key.get(table)
    pos_field = rules.pos_field.get(table)
    if not qkey or not pos_field:
        return
    if row.get(qkey) is not None and not isinstance(row.get(qkey), dict):
        raise PieceRejected("%s: %s 必须是对象（B 档：结构坏，无法校验）" % (where, qkey))
    located = _locate_quote(row, chunk, {"rules": rules, "table": table, "scope": scope})
    tkey = rules.quote_text_key.get(table)
    label = "%s.%s" % (qkey, tkey) if tkey else qkey
    pos = row.get(pos_field)
    has_pos = not (pos is None or pos == "")
    if has_pos:
        if isinstance(pos, bool) or not isinstance(pos, int) or pos < 0:
            raise PieceRejected("%s: %s 必须是 >=0 的整数，实际 %r（B 档：行无法定位）"
                                % (where, pos_field, pos))
        if located is None:
            raise PieceRejected("%s: 有 %s=%r 但 %s 缺失/不能原样命中原文"
                                "（B 档：这个偏移无法核验，必须有能命中的 quote）"
                                % (where, pos_field, pos, label))
        return
    if located is not None:
        row[pos_field] = located  # tier A: fill the offset by locating the quote
        return
    # no quote and no offset — the row is not dropped, first try text anchors
    derived, anchor = _derive_offset_from_row_text(row, chunk, scope)
    if derived is not None:
        row[pos_field] = derived
        warn_row(warnings, where, "quote_missing",
                 "%s: 没有 %s，按文字锚点 %r 回原文补 %s=%d（行照抽；缺的是引文，不是事实）"
                 % (where, label, anchor, pos_field, derived))
        return
    row["__unlocated"] = True
    warn_row(warnings, where, "row_unlocated",
             "%s: 既无 %s、无 %s，也没有能回原文的文字锚点——行不静默丢，记进 unlocated_rows"
             % (where, pos_field, label))


def _missing(v):
    """"Missing" check (type-independent): None / empty string / all-whitespace string / empty
    list / empty object."""
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, dict)):
        return not v
    return False


def _validate_row(table, row, chunk, scope, rules, where, warnings=None):
    """Tier B hard reject: a field with on_missing=reject that is missing/empty + a broken
    quote/offset structure. Raising PieceRejected = retry this piece."""
    for name, spec in rules.tables[table].items():
        if spec["on_missing"] == "reject" and _missing(row.get(name)):
            raise PieceRejected("%s: 必填字段 %r 缺失/空串（on_missing=reject，不 strip）"
                                % (where, name))
    _check_quote_offset(row, chunk, scope, rules, table, where, warnings)


def validate_piece(doc, chunk, scope, rules, warnings, piece=None, unlocated=None):
    """Tier B: the whole piece output is validated first; failure raises PieceRejected
    (triggering a retry of that piece).

    Returns the count of rows dropped for "numeric reject field empty" (the original
    behavior: a numeric row's essential value is empty -> drop the whole row, neither a
    warning nor a reject). Tier decisions all come from rules; this function only executes
    per attribute. Validation does tier-C strip on doc in place (tier-A completion happens
    in normalize).

    Rows with neither offset nor text anchor are no longer hard-rejected (that would equal
    "missing quote => delete row"); they are recorded into the unlocated list (not into the
    output, but not silently dropped — the report has them).
    """
    if not isinstance(doc, dict):
        raise PieceRejected("模型输出顶层必须是对象，实际 %s" % type(doc).__name__)
    dropped = 0
    for table in rules.order:
        rows = doc.get(table)
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise PieceRejected("%s 必须是数组，实际 %s" % (table, type(rows).__name__))
        kept = []
        for idx, row in enumerate(rows, 1):
            where = "%s[#%d]" % (table, idx)
            if not isinstance(row, dict):
                raise PieceRejected("%s: 不是对象（B 档：整片结构不可用）" % where)
            strip_optional_empty(row, table, rules, where, warnings, piece=piece)
            drop = False
            for name, spec in rules.tables[table].items():
                if spec["on_missing"] != "reject":
                    continue
                val = row.get(name)
                if not _missing(val):
                    continue
                if spec.get("type") == "number" and isinstance(val, str):
                    dropped += 1  # original behavior: numeric row value is an empty string -> drop the whole row (no error, no warning)
                    drop = True
                    break
                raise PieceRejected("%s: 必填字段 %r 缺失/空串（on_missing=reject，不 strip）"
                                    % (where, name))
            if drop:
                continue
            _validate_row(table, row, chunk, scope, rules, where, warnings)
            if row.pop("__unlocated", False):
                if unlocated is not None:
                    unlocated.append({"table": table, "where": where, "piece": piece,
                                      "row": {k: v for k, v in row.items()
                                              if not str(k).startswith("__")}})
                continue
            kept.append(row)
        doc[table] = kept
    return dropped


def quote_ratio_of(entities, tables=None, quote_missing=None):
    """Four-table quote ratio (denominator A: 0.95 only counts "rows that can give a quote").

    quote_missing = rows registered as missing-quote (each {"table","where",...}, see
    quote_missing_of):
      ratio     = with_quote / (rows − quote_missing)   <- the gate only looks at this
      gap_ratio = quote_missing / rows                  <- the "quote gap" is a separate metric, not gated
    A row missing its quote yet unregistered stays in the denominator and drags ratio down
    — a silent gap must not be averaged away.
    """
    total = 0
    with_quote = 0
    keys = list(tables) if tables else [k for k, v in entities.items() if isinstance(v, list)]
    for table in keys:
        for row in entities.get(table) or []:
            if not isinstance(row, dict):
                continue
            total += 1
            quote = row.get("quote")
            if isinstance(quote, dict) and _nonempty(quote.get("原文")):
                with_quote += 1
    missing = len(quote_missing or [])
    denom = max(total - missing, 0)
    return {"rows": total, "with_quote": with_quote,
            "quote_missing": missing, "denominator": denom,
            "ratio": round(with_quote / denom, 4) if denom else 1.0,
            "gap_ratio": round(missing / total, 4) if total else 0.0,
            "unregistered_gap": max(total - with_quote - missing, 0)}


def quote_missing_of(warnings):
    """Take "registered missing-quote" rows from row-level warnings, listed separately as
    quote_missing_rows (not mixed into coverage)."""
    rows = []
    for w in warnings or []:
        if not isinstance(w, dict) or w.get("code") != "quote_missing":
            continue
        where = w.get("where") or ""
        table = where.split("[", 1)[0] if "[" in where else where
        rows.append({"table": table, "where": where, "piece": w.get("piece"),
                     "reason": w.get("msg") or ""})
    return rows


# ---------------------------------------------------------------- time_norm recomputation hard gate
#
# Background: event_id got recomputation, the unique key got character-offset recomputation,
# but the **time column** was left out — and the time column is the sole basis for "take the
# latest"; when it is wrong the error shows not in the value but in the key (same shape as
# event_id's earlier "fine at first, wrong later" hole). So a hard gate was added, the same
# style as event_id recomputation:
#   * the stored time_norm must be reproducible from (time_raw, time_ref) by this file's
#     normalization logic, character-for-character identical;
#   * a recomputation mismatch = red (not a warning), turned into exit code 2 by main()
#     (output still written; the trail shows which row is wrong);
#   * unreproducible (missing time_ref / time_raw unparseable) = also red: storing a time
#     value that cannot be re-verified is equivalent to having no key.

TIME_NORM_FIELD = "time_norm"
TIME_RAW_FIELD = "time_raw"
TIME_REF_FIELD = "time_ref"
TIME_PRECISION_FIELD = "time_precision"   # the query side must read it
TIME_UNCERTAIN_FIELD = "time_uncertain"

# **Query-side field contract** — every field that "should appear in the answer" registers an
# entry here. A new field (like time_precision) must ship with an end-to-end assertion (one
# assertion in e2e_asserts.json whose expectation contains the field), otherwise extract()
# raises a schema error directly (check_e2e_field_coverage). The mechanism's point: a unit
# test proving "the function returns the field" is not enough; **from question to answer,
# actually reading it** is what counts — this is exactly the second occurrence of
# "time_precision changed, query side didn't read it" (the first was filter.time).
QUERY_FIELD_CONTRACT = ("值", "单位", "精度", TIME_NORM_FIELD, TIME_UNCERTAIN_FIELD, "来源")
QUERY_PRECISION_KEY = "精度"
QUERY_SOURCE_KEY = "来源"


def time_norm_recompute(entities, rules, chunk):
    """Recompute time_norm row by row, returning a reproducibility report (rows_checked /
    reproduced / mismatch / unparseable).

    time_ref resolution order: use the row's if stored (metrics derives time_ref), otherwise
    the chunk's event time (cards don't store time_ref; relative time relies on it).
    """
    checked, reproduced = 0, 0
    null_rows, nulled_underivable = 0, []
    mismatch, unreproducible = [], []
    # the recomputation base must match the extraction base (finest of the three), otherwise
    # relative time never matches
    chunk_ref = (chunk or {}).get("_base_iso") or (chunk or {}).get(TIME_REF_FIELD)
    tables = getattr(rules, "order", None) or list(entities)
    for table in tables:
        specs = (getattr(rules, "tables", {}) or {}).get(table) or {}
        spec = specs.get(TIME_NORM_FIELD)
        if not spec:
            continue
        raw_key = ((spec.get("inputs") or {}).get("raw")) or TIME_RAW_FIELD
        for i, row in enumerate(entities.get(table) or [], 1):
            if not isinstance(row, dict):
                continue
            stored = row.get(TIME_NORM_FIELD)
            if _is_blank(stored):
                # time_norm=null is a legal result (no/underivable time info), but it must be
                # visible: count rows that nulled because underivable, don't let null be a silent exit.
                null_rows += 1
                raw = row.get(raw_key)
                if not _is_blank(raw):
                    ref = row.get(TIME_REF_FIELD) or chunk_ref
                    if isinstance(ref, str) and ref.strip() and normalize_time(raw, ref) is None:
                        nulled_underivable.append({"where": "%s[#%d]" % (table, i),
                                                   "time_raw": raw})
                continue
            checked += 1
            where = "%s[#%d]" % (table, i)
            raw = row.get(raw_key)
            ref = row.get(TIME_REF_FIELD) or chunk_ref
            if not isinstance(ref, str) or not ref.strip():
                unreproducible.append({"where": where, "why": "缺 time_ref，相对时间无法解",
                                       "time_raw": raw, "stored": stored})
                continue
            # reference-point assertion — a stored time_ref must equal the chunk's time_ref,
            # mismatch = red (if the reference point could be arbitrary, relative time would
            # resolve "correctly" no matter what).
            stored_ref = row.get(TIME_REF_FIELD)
            if (chunk_ref and isinstance(stored_ref, str) and stored_ref.strip()
                    and stored_ref != chunk_ref):
                mismatch.append({"where": where, "why": "time_ref 与 chunk 的 time_ref 不等（参照点必须=chunk 事件时间）",
                                 "stored": stored, "time_raw": raw,
                                 "time_ref": stored_ref, "chunk_time_ref": chunk_ref})
                continue
            want = normalize_time(raw, ref, chunk_ref or ref)
            if want is None:
                unreproducible.append({"where": where, "why": "time_raw 归一化解不出来",
                                       "time_raw": raw, "stored": stored})
                continue
            if want != stored:
                mismatch.append({"where": where, "stored": stored, "recomputed": want,
                                 "time_raw": raw, "time_ref": ref})
                continue
            reproduced += 1
    red = bool(mismatch) or bool(unreproducible)
    den = checked + null_rows          # denominator = rows where the field exists (including null)
    return {"rows_checked": checked, "reproduced": reproduced,
            "den": den, "num": reproduced,
            "mismatch": len(mismatch), "mismatch_rows": mismatch,
            "unreproducible": len(unreproducible), "unreproducible_rows": unreproducible,
            "null_rows": null_rows,
            "underivable_nulled": len(nulled_underivable),
            "underivable_rows": nulled_underivable[:50],
            "red": red, "ok": not red,
            # denominator=0 => unverified, not shown as passing
            "verdict": "fail" if red else ("pass" if den else "unverified"),
            "algorithm": "time_norm == normalize_time(time_raw, time_ref)（与抽取器归一化同源）；"
                         "推不出 => time_norm=null（不是 red、也不许保留模型自报值，task-101）；"
                         "行.time_ref 必须等于 chunk.time_ref（不等=红，task-80/P239）"}


def enforce_time_norm_reproducible(entities, rules, chunk, warnings=None):
    """time_norm recomputation hard gate — mismatch/unparseable both report red (not a warning)."""
    info = time_norm_recompute(entities, rules, chunk)
    if info["red"] and warnings is not None:
        head = "；".join("%s 存 %s 应为 %s" % (r["where"], r["stored"], r["recomputed"])
                        for r in info["mismatch_rows"][:5])
        bad = "；".join("%s %s" % (r["where"], r["why"]) for r in info["unreproducible_rows"][:5])
        warn_note(warnings, "time_norm_mismatch",
                  "time_norm 复算报红（task-78）：不符 %d 行、解不出 %d 行 —— 时间列是「取最新」的"
                  "唯一依据，重算对不上就是键错了（明细见 monitor.time_norm_gate）。%s%s"
                  % (info["mismatch"], info["unreproducible"],
                     ("不符：%s。" % head) if head else "",
                     ("解不出：%s。" % bad) if bad else ""))
    return info


def _iter_derived_rows(entities, rules, deriver):
    """Iterate "fields whose derive equals deriver", yielding (table, name, spec, i, row)."""
    for table in (getattr(rules, "order", None) or list(entities)):
        for name, spec in ((getattr(rules, "tables", {}) or {}).get(table) or {}).items():
            if spec.get("derive") != deriver:
                continue
            for i, row in enumerate(entities.get(table) or [], 1):
                if isinstance(row, dict):
                    yield table, name, spec, i, row


# reproducible derived-field list (the derivation self-check must be able to "recompute from
# input by the same rules")
RECOMPUTE_DERIVERS = ("time_norm", "time_precision", "canonical_from_raw_writing", "card_event_id")

# the derived-field self-check **covers the whole list, not just time**. Besides the four
# recomputed by derive above, there are also:
#   op_type     action word -> enum (look up OP_TYPE_ALIAS, the same table used on both sides);
#   值 / 单位    numeric expression (regex + reconcile against the source sentence) / unit
#               (look up UNIT_KNOWN, or the source sentence contains it verbatim).
NUM_VALUE_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
UNIT_KNOWN = {
    "次", "个", "条", "行", "张", "份", "台", "人", "天", "小时", "分钟", "秒", "毫秒",
    "元", "万元", "美元", "字节", "KB", "MB", "GB", "TB", "PB", "%", "百分比", "倍",
    "count", "times", "rows", "files", "items", "seconds", "minutes", "hours", "days",
    "bytes", "ms", "s", "min", "h", "req/s", "MB/s", "usd", "cny",
}
DERIVED_FIELD_NAMES = ("time_norm", "time_precision", "op_type", "canonical_id", "值", "单位")


def _table_of_field(rules, name):
    """Field name -> (table name, spec); the same name may appear in multiple tables (time_norm
    is in three tables)."""
    out = []
    for table, specs in (getattr(rules, "tables", {}) or {}).items():
        if name in specs:
            out.append((table, specs[name]))
    return out


def _quote_text_of(row, rules, table):
    """Take this row's quote source (quote.原文), for reconciling against the source sentence."""
    qkey = getattr(rules, "quote_key", {}).get(table)
    tkey = getattr(rules, "quote_text_key", {}).get(table)
    quote = row.get(qkey) if qkey else None
    if isinstance(quote, dict) and tkey and isinstance(quote.get(tkey), str):
        return quote.get(tkey)
    return None


def unit_is_known(unit, quote=None):
    """Whether a unit is usable: in the table / every segment of a compound unit (次/秒, MB/s)
    is in the table / the source sentence contains it verbatim.

    The unit is a field derived from the "numeric expression" and must be table-recomputable
    — but a compound form like "次/秒" must not be missed by the unit table (split on `/` `·`
    and judge per segment, more reliable than exhaustively listing whole unit strings).
    """
    u = unicodedata.normalize("NFKC", str(unit or "")).strip()
    if not u:
        return True
    if u in UNIT_KNOWN:
        return True
    parts = [p.strip() for p in re.split(r"[/·]", u) if p.strip()]
    if len(parts) > 1 and all(p in UNIT_KNOWN for p in parts):
        return True
    if quote and u in unicodedata.normalize("NFKC", quote):
        return True
    return False


def _extra_derived_assertions(entities, rules, chunk):
    """op_type / 值 / 单位 derivation self-check (produces mismatch entries in the same
    format as DERIVERS)."""
    out = []
    for table, spec in _table_of_field(rules, "op_type"):
        for i, row in enumerate(entities.get(table) or [], 1):
            if not isinstance(row, dict):
                continue
            stored = row.get("op_type")
            src = row.get("action") or row.get("target")
            # **look only at action** (the action description the model wrote) — backfilling from
            # narrative connectives in quote (Then I went through ...) would judge "filled data"
            # as "read through it", building the gate on noise.
            want = alias_op_type(src)
            if want == OP_ALIAS_DEFAULT:
                continue  # this action word is absent from the table: allowed to stay other, not hard-judged
            if str(stored or "") != str(want):
                out.append({"field": "op_type", "where": "%s[#%d].op_type" % (table, i),
                            "stored": stored, "recomputed": want,
                            "why": "动作词 %r 查别名表应归 %r（task-104 一⑤：动作词->枚举）"
                                   % (src, want)})
    for table, spec in _table_of_field(rules, "值"):
        for i, row in enumerate(entities.get(table) or [], 1):
            if not isinstance(row, dict):
                continue
            where = "%s[#%d]" % (table, i)
            quote = _quote_text_of(row, rules, table)
            val = row.get("值")
            sval = "" if val is None else str(val).strip()
            # 值's self-check is a "numeric expression" structural check (regex), not a verbatim
            # reconcile against the source sentence — that belongs to verbatim fields
            # (quote/time_raw); mixing it in would flag all synthetic fixtures red.
            if not NUM_VALUE_RE.match(sval):
                out.append({"field": "值", "where": where + ".值", "stored": val,
                            "recomputed": None,
                            "why": "值不是数值表达（task-104 一⑤：数值表达要能正则复算）"})
            unit = row.get("单位")
            sunit = "" if unit is None else str(unit).strip()
            if not sunit:
                continue
            if unit_is_known(sunit, quote):
                continue
            out.append({"field": "单位", "where": where + ".单位", "stored": unit,
                        "recomputed": None,
                        "why": "单位 %r 不在单位表里、原句也没有（task-104 一⑤）" % sunit})
    return out


def recompute_derived(entities, rules, chunk):
    """Recompute derived fields row by row and compare character-for-character with the stored
    value; mismatch = silent fabrication / key error.

    Covers the **whole derived-field list** (time, precision, action type, canonical name,
    value, unit), no longer only time — the blacklist only blocks known dangers, the
    whole-field self-check is the real safety net.
    """
    checked, reproduced = 0, 0
    missing, mismatch = [], []
    per_field = {name: {"checked": 0, "reproduced": 0, "mismatch": 0, "unreproducible": 0}
                 for name in DERIVED_FIELD_NAMES}
    for deriver in RECOMPUTE_DERIVERS:
        for table, name, spec, i, row in _iter_derived_rows(entities, rules, deriver):
            where = "%s[#%d].%s" % (table, i, name)
            stored = row.get(name)
            try:
                want = DERIVERS[deriver](row, spec, chunk, {})
            except Exception as exc:  # noqa: BLE001  a crash in the recomputation itself must also be visible
                mismatch.append({"where": where, "why": "重算异常 %s" % type(exc).__name__,
                                 "stored": stored, "field": name})
                per_field.setdefault(name, {"checked": 0, "reproduced": 0,
                                            "mismatch": 0, "unreproducible": 0})
                per_field[name]["mismatch"] += 1
                continue
            if _is_blank(stored):
                if _is_blank(want):
                    checked += 1
                    reproduced += 1
                    per_field.setdefault(name, {"checked": 0, "reproduced": 0,
                                                "mismatch": 0, "unreproducible": 0})
                    per_field[name]["checked"] += 1
                    per_field[name]["reproduced"] += 1
                continue
            checked += 1
            per_field.setdefault(name, {"checked": 0, "reproduced": 0,
                                        "mismatch": 0, "unreproducible": 0})
            per_field[name]["checked"] += 1
            if _is_blank(want):
                missing.append({"where": where, "stored": stored, "field": name,
                                "why": "输入推不出任何值，却存了一个"})
                per_field[name]["unreproducible"] += 1
                continue
            if str(want) != str(stored):
                mismatch.append({"where": where, "stored": stored, "recomputed": want,
                                 "field": name, "why": "重算值与存的值不一致"})
                per_field[name]["mismatch"] += 1
                continue
            reproduced += 1
            per_field[name]["reproduced"] += 1
    # the other derived fields (op_type / 值 / 单位) go through table/regex self-check, same criterion
    for item in _extra_derived_assertions(entities, rules, chunk):
        name = item["field"]
        checked += 1
        per_field.setdefault(name, {"checked": 0, "reproduced": 0,
                                    "mismatch": 0, "unreproducible": 0})
        per_field[name]["checked"] += 1
        mismatch.append(item)
        per_field[name]["mismatch"] += 1
    return {"rows_checked": checked, "reproduced": reproduced,
            "mismatch": len(mismatch), "mismatch_rows": mismatch[:50],
            "unreproducible": len(missing), "unreproducible_rows": missing[:50],
            "fields": list(DERIVED_FIELD_NAMES), "per_field": per_field,
            "algorithm": "派生值(time_norm/time_precision/op_type/canonical_id/值/单位) 必须能由行内"
                         "输入按本文件同一实现重算出来且逐字符一致；不一致=红（task-104 一：全覆盖）"}


def enforce_time_precision_consistent(entities, rules, warnings=None):
    """**The precision implementation bug must be fixed** — precision and time_norm must be
    self-consistent.

    Criteria (precisely decidable => gate, not a warning):
      * has time_norm and date-only (YYYY-MM-DD)      => precision must be day;
      * has time_norm and a clock time                 => precision must be minute/second;
      * has time_raw but empty time_norm               => precision must be unknown (not day/minute);
      * no time_raw                                    => precision must be empty (None, key omittable).
    These four are exactly the shape of "precision mislabeled but we didn't notice": day with
    a clock time, minute with a date, null with day.
    """
    warnings = warnings if warnings is not None else []
    mism = []
    checked = 0
    for table, spec in _table_of_field(rules, TIME_PRECISION_FIELD):
        inputs = spec.get("inputs") or {}
        raw_key = inputs.get("raw") or TIME_RAW_FIELD
        norm_key = inputs.get("norm") or TIME_NORM_FIELD
        for i, row in enumerate(entities.get(table) or [], 1):
            if not isinstance(row, dict):
                continue
            checked += 1
            raw, norm = row.get(raw_key), row.get(norm_key)
            prec = row.get(TIME_PRECISION_FIELD)
            ok = True
            if _is_blank(raw):
                want = None
                ok = _is_blank(prec)
            elif _is_blank(norm):
                want = "unknown"
                ok = (prec == "unknown")
            elif len(str(norm).strip()) == 10:      # YYYY-MM-DD, date only
                want = "day"
                ok = (prec == "day")
            else:                                    # has a clock time: minute / second both legal
                want = "minute|second"
                ok = prec in ("minute", "second")
            if ok:
                continue
            mism.append({"where": "%s[#%d]" % (table, i), "stored": prec, "expected": want,
                         "time_raw": raw, "time_norm": norm})
    red = bool(mism)
    info = {"ok": not red, "red": red, "checked": checked, "mismatch": len(mism),
            "rows": mism[:50],
            "rule": "precision 与 time_norm 必须自洽：日期 -> day；带时刻 -> minute/second；"
                    "有写法但解不出 -> unknown；没有 time_raw -> 空",
            "algorithm": "精度不是独立字段：从 (time_raw, time_norm) 推出来的，推不出就得说 unknown"}
    if red and warnings is not None:
        warn_note(warnings, "time_precision_mismatch",
                  "时间精度不自洽 %d 行（task-107 五b：我们自己的实现错）——%s"
                  % (len(mism),
                     "；".join("%s 存 %r 应为 %r（raw=%r norm=%r）"
                              % (m["where"], m["stored"], m["expected"],
                                 m["time_raw"], m["time_norm"]) for m in mism[:5])))
    return info


def traceability_assert(entities, rules, chunk, trace_hits=None, warnings=None):
    """**Traceability assertion** (unified entry — every new field goes through it).

    ① verbatim self-check: verbatim fields (time_raw) must appear character-for-character in
       the source_chunk source. Rewrites (yesterday -> 昨天) were already nulled with a
       warning at normalization; here violations are aggregated row by row. Note: violations
       were already nulled, so the output holds no fake value, thus no exit-code flip (the
       rc=0 contract); but they must be visible — violations>0 is hard evidence of "the model
       rewrote".
    ② derivation self-check: derived fields are recomputed by the same rules and compared
       with the stored value; mismatch = red.
    """
    hits = list(trace_hits or [])
    verbatim_fields = ["%s.%s" % (fk, n)
                       for fk, tbl in (getattr(rules, "tables", {}) or {}).items()
                       for n, s in tbl.items() if s.get("verbatim")]
    recompute = recompute_derived(entities, rules, chunk)
    red = bool(recompute["mismatch"] or recompute["unreproducible"])
    info = {"verbatim": {"fields": verbatim_fields, "violations": len(hits),
                         "rows": hits[:50],
                         "ok": not hits,
                         "rule": "剪贴字段（quote/time_raw）必须逐字出现在 chunk 原文（连续子串）"},
            "derived": {"fields": list(DERIVED_FIELD_NAMES),
                        "checked": recompute["rows_checked"],
                        "reproduced": recompute["reproduced"],
                        "rule": "派生字段（time_norm/time_precision/op_type/canonical_id/值/单位）"
                                "必须能由（原文, 基准）按同一实现重算——provenance，不是逐字"},
            "recompute": recompute,
            "red": red, "ok": (not red) and not hits,
            "levels": "task-104 一：两级分开——剪贴字段=逐字子串（verbatim）；派生字段=可重算"
                      "（recompute）；不混成一句「逐字校验」",
            "algorithm": "① 剪贴字段必须逐字出现在 chunk 原文（子串）；② 派生字段必须能重算且逐字符"
                         "一致（全覆盖：时间/精度/动作类型/规范名/值/单位；不一致=红）"}
    if hits and warnings is not None:
        shown = "；".join("%s %s=%r" % (h["where"], h["field"], h["value"]) for h in hits[:5])
        warn_note(warnings, "time_raw_not_verbatim",
                  "可追溯性断言①：剪贴自检抓到 %d 行改写（time_raw 在 chunk 原文里找不到）："
                  "%s——已置 null，产物不留假值（task-102 一）" % (len(hits), shown))
    if red and warnings is not None:
        heads = ["%s 存 %r 应为 %r" % (r["where"], r.get("stored"), r.get("recomputed"))
                 for r in recompute["mismatch_rows"][:5]]
        miss = ["%s %s" % (r["where"], r["why"]) for r in recompute["unreproducible_rows"][:5]]
        warn_note(warnings, "traceability_recompute_red",
                  "可追溯性断言②：推导自检报红——重算不符 %d 行、解不出 %d 行（%s%s）"
                  % (recompute["mismatch"], recompute["unreproducible"],
                     "；".join(heads), ("；" + "；".join(miss)) if miss else ""))
    return info


SPAN_ALARM_HOURS = 24.0


def span_alarm_of(entities, rules, chunk, warnings=None):
    """Span alarm — in-chunk time_norm span > 24h and containing relative time => the
    chunk-level base is unreliable."""
    from datetime import datetime
    times, relative_rows = [], []
    raw_key = TIME_RAW_FIELD
    for table in (getattr(rules, "order", None) or list(entities)):
        spec = ((getattr(rules, "tables", {}) or {}).get(table) or {}).get(TIME_NORM_FIELD)
        if not spec:
            continue
        raw_key = ((spec.get("inputs") or {}).get("raw")) or TIME_RAW_FIELD
        for i, row in enumerate(entities.get(table) or [], 1):
            if not isinstance(row, dict):
                continue
            norm = row.get(TIME_NORM_FIELD)
            norm_dt = parse_time_norm(norm) if isinstance(norm, str) else None
            if norm_dt is not None:
                times.append((norm_dt, "%s[#%d]" % (table, i), norm))
            raw = row.get(raw_key)
            if _nonempty(raw) and not is_absolute_time_expr(raw):
                relative_rows.append({"where": "%s[#%d]" % (table, i), "time_raw": raw})
    span = None
    if len(times) >= 2:
        times.sort(key=lambda x: x[0])
        span = (times[-1][0] - times[0][0]).total_seconds() / 3600.0
    alarm = bool(span is not None and span > SPAN_ALARM_HOURS and relative_rows)
    info = {"chunk": (chunk or {}).get("source_chunk_id"),
            "base_source": (chunk or {}).get("_base_source"),
            "base_granularity": (chunk or {}).get("_base_granularity"),
            "times": len(times), "span_hours": round(span, 3) if span is not None else None,
            "relative_rows": len(relative_rows), "threshold_hours": SPAN_ALARM_HOURS,
            "alarm": alarm,
            "rows": relative_rows[:50],
            "hint": "跨度 > 24h 且含相对时间：chunk 级基准不可信，这个 chunk 必须升级到 turn/session 基准"}
    if alarm and warnings is not None:
        warn_note(warnings, "chunk_base_unreliable",
                  "跨度报警（task-102 三）：chunk %r 内时间跨度 %.1f 小时 > %.0f 小时、且含 %d 条相对时间"
                  "——chunk 级基准不可信（这个 chunk 需要 turn/session 级基准）"
                  % (info["chunk"], span, SPAN_ALARM_HOURS, len(relative_rows)))
    return info


QUOTE_INVALID = "quote_invalid"          # runtime substring check failed (degradation trail)
QUOTE_INVALID_RETRIED = "quote_invalid_retried"  # retried once and still failed


def quote_sub_fields(row, rules, table):
    """(sub-field name, value) list: all **string** sub-fields of this row's quote (not only 原文).

    check_2_quote gates on "every string sub-field of quote must be greppable in the source";
    the old runtime gate only checked quote.原文, so 8 cards' quote.归一化 were Chinese
    rewrites yet passed all the way through.
    """
    qkey = rules.quote_key.get(table)
    quote = row.get(qkey) if qkey else None
    if not isinstance(quote, dict):
        return []
    return [(k, v) for k, v in quote.items() if isinstance(v, str) and v.strip()]


def _substring_norm(s):
    """Substring comparison criterion: NFKC + strip all whitespace (same criterion as
    check_2_quote's norm())."""
    s = unicodedata.normalize("NFKC", str(s))
    for ch in " \t\r\n\u3000\u00a0\ufeff\u200b":
        s = s.replace(ch, "")
    return s


def quote_substring_violations(entities, rules, text):
    """Check row by row, sub-field by sub-field whether "quote is a contiguous substring of
    source_chunk", returning the violation list (no data change).

    Returns [{"table","index","where","field","value","anchor"}...]; anchor=True means the
    failing one is the quote anchor (原文), False means an auxiliary sub-field (归一化 type).
    """
    target = _substring_norm(text or "")
    out = []
    for table in (getattr(rules, "order", None) or list(entities)):
        anchor_key = getattr(rules, "quote_text_key", {}).get(table)
        for i, row in enumerate(entities.get(table) or [], 1):
            if not isinstance(row, dict):
                continue
            for sub, val in quote_sub_fields(row, rules, table):
                needle = _substring_norm(val)
                if not needle:
                    continue
                if needle in target:
                    continue
                out.append({"table": table, "index": i,
                            "where": "%s[#%d].%s.%s" % (table, i,
                                                        getattr(rules, "quote_key", {}).get(table),
                                                        sub),
                            "field": sub, "value": val[:60],
                            "anchor": (sub == anchor_key)})
    return out


def _quote_fields_checked(entities, rules):
    """How many quote string sub-fields were checked across these tables (denominator, into
    the gate output)."""
    return sum(len(quote_sub_fields(r, rules, t))
               for t in (getattr(rules, "order", None) or list(entities))
               for r in (entities.get(t) or []) if isinstance(r, dict))


def quote_substring_report(entities, rules, text, limit=50):
    """**Read-only recompute** — drops no row, changes no field, specifically for gate_final.

    Uses the same judgment function as enforce_quote_substring (quote_substring_violations);
    the only difference is it does not scrub data, so recomputing on the **final output**
    answers "did this cell actually pass or not".
    """
    viol = quote_substring_violations(entities, rules, text)
    return {"kind": "gate_final",
            "ok": not viol, "red": bool(viol),
            "fields_checked": _quote_fields_checked(entities, rules),
            "invalid": len(viol),
            "anchor_invalid": sum(1 for v in viol if v["anchor"]),
            "aux_invalid": sum(1 for v in viol if not v["anchor"]),
            # counters must not be missing keys — a missing key reads as None and is treated as
            # "no problem". This one is a **read-only recompute** that drops no row and deletes no
            # field, so the three counters are **genuinely 0** (not "uncounted"), paired with
            # unverified=False to state it clearly.
            "dropped_rows": 0, "dropped_cards": 0, "dropped_fields": 0,
            "unverified": False,
            "invalid_rows": viol[:limit],
            "rule": "quote 的每个字符串子字段必须是 source_chunk 的连续子串（NFKC+去空白）；"
                    "本份跑在**最终产物**上（清洗之后），ok=False 就是这一格没过",
            "algorithm": "只读复算：不在这一步丢行/丢字段，所以它报的数就是产物里的真数"}


def quote_substring_gates(raw, final):
    """The gate outputs two copies; no longer use "pre-scrub" as the criterion.

    * gate_raw   —— runs on the model's raw output (pre-scrub, when fields like quote.归一化
                    still exist): shows "what the model did this time", red is diagnostic only.
    * gate_final —— runs on the final output (post-scrub): **the criterion is
                    gate_final.ok == True**.

    Background: the old version had only one gate (equal to gate_raw), so "gate is red
    (invalid=8), output is green (0 violations)" appeared — the two disagreeing is deceptive.
    """
    raw = raw or {}
    final = final or {}
    return {"ok": bool(final.get("ok")), "red": bool(final.get("red")),
            "invalid": final.get("invalid"),
            "fields_checked": final.get("fields_checked"),
            "verdict_basis": "gate_final",
            "report_line": "gate_raw ok=%s red=%s invalid=%s | gate_final ok=%s red=%s invalid=%s"
                           % (raw.get("ok"), raw.get("red"), raw.get("invalid"),
                              final.get("ok"), final.get("red"), final.get("invalid")),
            "gate_raw": raw, "gate_final": final,
            "rule": "判据只看 gate_final.ok==True；gate_raw 红只当诊断（说明模型这次给了非原文引文、"
                    "已被清洗掉），不许拿 gate_raw 红去判整格不过",
            "why": "闸跑清洗前、产物看清洗后，两者不一致会骗人（审查端 P349）"}


# ---------------------------------------------------------------- counters unified to int
#
# Background: gate_final.dropped_cards read out as None (**the key does not exist**; a missing
# key reads as None), and None conflates "counted, it is 0" with "not counted" — the root of
# these silent failures (None gets treated as "no problem"). Rules (every gate in this file
# follows them):
#   ① counter field type must be int: 0 means "counted, it is 0";
#   ② genuinely not counted: write 0, **and** add `unverified: true` +
#      `unverified_fields: [path]` on that gate;
#   ③ every gate carries `report_line` (`"<gate> k=v ..."`; two-output gates list raw/final
#      with `" | "`), the report pastes this line, no oral "I fixed / retried" guarantee.
TEXT_LEAF_FIELDS = {
    "chunk", "identity_table", "table", "rule", "algorithm", "verdict", "levels",
    "hint", "base_source", "base_granularity", "note", "kind", "why", "source",
}

# gate name -> fields to paste into report_line (dotted paths; other fields stay in JSON, just not on the report line)
GATE_REPORT_KEYS = {
    "content_gates": ("enum_value_dropped", "name_equals_type_dropped",
                      "cross_same_string", "shape_suspect", "dropped_rows"),
    "identity_generic": ("suspected",),
    "fk_invariants": ("rows_checked", "nullable_tables"),
    "time_norm_gate": ("rows_checked", "reproduced", "mismatch", "unreproducible",
                       "null_rows", "underivable_nulled"),
    "precision_gate": ("checked", "mismatch"),
    "traceability_gate": ("verbatim.violations", "derived.checked", "derived.reproduced",
                          "recompute.rows_checked", "recompute.mismatch",
                          "recompute.unreproducible"),
    "span_alarm": ("times", "span_hours", "relative_rows"),
    "density": ("chars", "rows_total", "identity_rows", "pieces"),
    "e2e": ("report.red", "report.total", "report.skipped"),
    "quote_gate": ("rows", "with_quote", "denominator", "quote_missing",
                   "unregistered_gap"),
    "expected_coverage": ("missing_total", "extra_total"),
    "quote_substring_gate": ("invalid", "fields_checked", "dropped_cards"),
    "gate_raw": ("invalid", "fields_checked", "fields_remaining",
                 "dropped_cards", "dropped_fields", "retried"),
    "gate_final": ("invalid", "fields_checked", "anchor_invalid", "aux_invalid",
                   "dropped_cards", "dropped_fields"),
}


def _fmt_leaf(v):
    if isinstance(v, bool):
        return "True" if v else "False"
    if v is None:
        return "NA"
    return str(v)


def _leaf_of(info, dotted):
    cur = info
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _none_to_zero_inplace(node, path, unverified):
    """Land the gate's None leaves into definite types — counters 0, text fields "" — and
    record them in unverified.

    Modified in place (not a rebuilt dict): the gate inside monitor and the gate at result's
    top level are the **same object**; rebuilding would make the two disagree. None inside
    lists is untouched (that is row detail, not a counter).
    """
    for k, v in list(node.items()):
        sub = path + "." + k
        if v is None:
            node[k] = "" if k in TEXT_LEAF_FIELDS else 0
            unverified.append(sub)
        elif isinstance(v, dict):
            _none_to_zero_inplace(v, sub, unverified)


def gate_report_line(name, info):
    """`"<gate> ok=... red=... k=v ..."` — the report pastes this line directly."""
    parts = []
    for k in ("ok", "red", "verdict"):
        if k in info:
            parts.append("%s=%s" % (k, _fmt_leaf(info[k])))
    if info.get("unverified"):
        parts.append("unverified=True")
    for key in GATE_REPORT_KEYS.get(name, ()):
        leaf = _leaf_of(info, key)
        if isinstance(leaf, (list, dict)):
            continue
        parts.append("%s=%s" % (key.split(".")[-1], _fmt_leaf(leaf)))
    return "%s %s" % (name, " ".join(parts))


def normalize_gate(name, info):
    """Normalize one gate — fill missing counters with 0 (marking unverified), land None to 0,
    add report_line."""
    if not isinstance(info, dict):
        info = {}
    unv = []
    _none_to_zero_inplace(info, name, unv)
    for key in GATE_REPORT_KEYS.get(name, ()):
        if "." in key:
            continue                      # nested paths are handled by their own sub-gate
        if key not in info:
            info[key] = 0
            unv.append(name + "." + key)  # missing key = uncounted (exactly the gate_final.dropped_cards hole)
    if info.get("unverified") is True and not unv:
        unv.append(name + ".*")
    info["unverified"] = bool(unv) or info.get("unverified") is True
    info["unverified_fields"] = sorted(set(unv))
    if name != "quote_substring_gate":   # this one composes its own raw|final report line, see quote_substring_gates
        info["report_line"] = gate_report_line(name, info)
    return info


def finalize_gates(monitor):
    """All gates pass through normalize_gate uniformly (monitor modified in place; the gate
    at the top level is the same object and follows)."""
    done = []
    for name in GATE_REPORT_KEYS:
        if name in ("gate_raw", "gate_final"):
            continue
        if name not in monitor:
            continue
        monitor[name] = normalize_gate(name, monitor.get(name))
        done.append(name)
    qg = monitor.get("quote_substring_gate")
    if isinstance(qg, dict):
        raw = normalize_gate("gate_raw", qg.get("gate_raw") or {})
        final = normalize_gate("gate_final", qg.get("gate_final") or {})
        qg["gate_raw"], qg["gate_final"] = raw, final
        qg["ok"], qg["red"] = bool(final.get("ok")), bool(final.get("red"))
        qg["invalid"] = int(final.get("invalid") or 0)
        qg["fields_checked"] = int(final.get("fields_checked") or 0)
        qg["dropped_cards"] = int(final.get("dropped_cards") or 0)
        qg["dropped_rows"] = int(final.get("dropped_rows") or 0)
        qg["dropped_fields"] = int(final.get("dropped_fields") or 0)
        qg["unverified"] = bool(raw.get("unverified") or final.get("unverified"))
        qg["unverified_fields"] = sorted(set(qg.get("unverified_fields") or [])
                                         | set(raw.get("unverified_fields") or [])
                                         | set(final.get("unverified_fields") or []))
        qg["report_line"] = ("gate_raw ok=%s red=%s invalid=%s | gate_final ok=%s red=%s "
                             "invalid=%s dropped_cards=%s"
                             % (_fmt_leaf(raw.get("ok")), _fmt_leaf(raw.get("red")),
                                _fmt_leaf(raw.get("invalid")), _fmt_leaf(final.get("ok")),
                                _fmt_leaf(final.get("red")), _fmt_leaf(final.get("invalid")),
                                _fmt_leaf(qg["dropped_cards"])))
        monitor["quote_substring_gate"] = qg
    monitor["counter_policy"] = {
        "rule": "计数器字段一律 int（0=数了是 0）；没统计的写 0 并在该闸加 "
                "unverified=True + unverified_fields（task-109 / 审查端 P350）",
        "gates_normalized": done,
        "not_counters": "ratio / hours 这类比值仍允许 null（不是计数器）",
    }
    return monitor


def find_none_counters(doc):
    """Scan all gates for leaves still None (counters must not be None). Returns the path
    list; empty = compliant."""
    bad = []
    mon = doc.get("monitor") or {}
    for name in GATE_REPORT_KEYS:
        node = mon.get(name)
        if not isinstance(node, dict):
            continue
        stack = [(node, "monitor." + name)]
        while stack:
            cur, path = stack.pop()
            for k, v in cur.items():
                sub = path + "." + k
                if v is None:
                    bad.append(sub)
                elif isinstance(v, dict):
                    stack.append((v, sub))
    return sorted(set(bad))


def find_gates_missing_report_line(doc):
    """Every gate must have report_line (the report must not rely on oral guarantees)."""
    mon = doc.get("monitor") or {}
    return sorted(n for n in GATE_REPORT_KEYS
                  if isinstance(mon.get(n), dict) and not mon[n].get("report_line"))


def enforce_quote_substring(entities, rules, text, warnings=None, retried=False):
    """**Runtime substring check** (not a prompt change — don't expect the model to obey,
    expect the gate).

    Rule: every string sub-field of quote must be a contiguous substring of source_chunk
    (compared after NFKC + whitespace strip).
      * the anchor (quote.原文) fails => this card's provenance base is fake: **drop the row**
        + record quote_invalid;
      * an auxiliary sub-field (quote.归一化) fails => drop that sub-field (the source anchor
        remains, the row is kept) + record quote_invalid;
      * retried=True means "a piece-level retry already ran once and still failed", stated in
        the degradation trail.
    Returns gate info (into monitor.quote_substring_gate; red is visible).
    """
    warnings = warnings if warnings is not None else []
    viol = quote_substring_violations(entities, rules, text)
    # gate_raw's denominator must be taken **pre-scrub** (post-scrub quote.归一化 is gone, the
    # denominator shrinks, then raw/final denominators disagree and look miscounted)
    fields_before = _quote_fields_checked(entities, rules)
    drop_by_table = {}
    dropped_rows, dropped_fields = [], []
    if viol:
        drop_idx = {(v["table"], v["index"]) for v in viol if v["anchor"]}
        for v in viol:
            if not v["anchor"]:
                row = (entities.get(v["table"]) or [])[v["index"] - 1]
                qkey = rules.quote_key.get(v["table"])
                if isinstance(row.get(qkey), dict):
                    row[qkey].pop(v["field"], None)
                    if not row[qkey]:
                        row.pop(qkey, None)
                dropped_fields.append(v)
            else:
                dropped_rows.append(v)
            warn_row(warnings, v["where"], QUOTE_INVALID,
                     "%s: quote.%s 不是 source_chunk 的子串（改写/翻译不算引文）：%r%s"
                     % (v["where"], v["field"], v["value"],
                        "——整行丢弃（锚点是假的）" if v["anchor"] else "——只丢这个子字段"),
                     piece=None)
        for table, idx in sorted(drop_idx):
            drop_by_table.setdefault(table, set()).add(idx)
        for table, idxs in drop_by_table.items():
            rows = entities.get(table) or []
            entities[table] = [r for i, r in enumerate(rows, 1) if i not in idxs]
    if viol:
        warn_note(warnings, QUOTE_INVALID,
                  "运行时子串校验（task-107 二）：%d 处 quote 不原文（锚点 %d 处=丢 %d 行，"
                  "辅助子字段 %d 处=只丢字段；dropped_cards=%d）%s"
                  % (len(viol), sum(1 for v in viol if v["anchor"]),
                     len(dropped_rows), len(dropped_fields), len(dropped_rows),
                     "；片级已重试一轮仍不过" if retried else ""))
    # this one is **gate_raw** (computed pre-scrub) — diagnostic only, the criterion is gate_final
    return {"kind": "gate_raw",
            "ok": not viol, "red": bool(viol),
            "fields_checked": fields_before,
            "fields_remaining": _quote_fields_checked(entities, rules),
            "invalid": len(viol), "invalid_rows": viol[:50],
            "dropped_rows": len(dropped_rows),
            # **dropped cards must also be counted** — otherwise "22" becomes "20" with no explanation.
            "dropped_cards": len(dropped_rows), "dropped_fields": len(dropped_fields),
            "retried": bool(retried),
            "rule": "quote 的每个字符串子字段必须是 source_chunk 的连续子串（NFKC+去空白）；"
                    "锚点（原文）不过=丢行，辅助子字段不过=丢字段",
            "algorithm": "运行时校验，不靠 prompt；不过则片级重试一次，仍不过就降级并记 "
                         "quote_invalid；丢卡数进 dropped_cards（不静默减少）",
            # this one = gate_raw (pre-scrub); the criterion is quote_substring_gate.gate_final
            "diagnostic_only": True}


def enforce_quote_ratio(entities, schema, warnings=None):
    """quote ratio gate (denominator A): quote_present / (rows − quote_missing) >= 0.95.

    Missing-quote rows are no longer penalized as "ratio below standard" — they are removed
    from the denominator and listed separately as quote_missing_rows (quote gap); only the
    unregistered silent gap (missing quote with no warning) turns red.
    """
    raw = (schema.get("gates") or {}).get("quote_ratio_min")
    qmin = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.95
    qm_rows = quote_missing_of(warnings)
    info = quote_ratio_of(entities, quote_missing=qm_rows)
    info["min"] = qmin
    info["quote_missing_rows"] = qm_rows
    info["ok"] = info["ratio"] >= qmin
    if not info["ok"]:
        raise ExtractError(
            "quote 占比 %d/%d=%.4f < %.4f（task-74 分母 A：缺引文的 %d 行已从分母剔除；"
            "剩下的是没登记的静默缺口，schema.gates.quote_ratio_min 重试不放宽）"
            % (info["with_quote"], info["denominator"], info["ratio"], qmin, info["quote_missing"]))
    return info


# ---------------------------------------------------------------- "take the latest" lookup on the time column
#
# Background: time-column sort logic is unit-testable, but end-to-end samples must not be
# fabricated. The lookup criterion is fixed in two ways, and multi-value same-metric cases
# must give different answers:
#   * "what is X now" = take the **latest** row by time_norm (lookup, order=latest);
#   * "what is the max X" = take the **maximum** row (max) — latest and max are not the same;
#   * order=oldest is only for negative examples: taking an old value as "now" = wrong
#     (answer_ok turns red, not an oral convention).
#
# The raw chunk set currently has no multi-value metric (recomputable via scan: no chunk has
# a "same metric name >=2 places with a value" segment), so here only the lookup
# implementation and scoring live; samples are built by unit tests, end-to-end does not
# fabricate.

LOOKUP_ORDERS = ("latest", "oldest", "max")

# The "whole library" scope is fixed up front; the implementation must not interpret it on
# its own.
#   boundary = "the metrics tables of extracted chunks" = all metrics rows of every entity
#   file listed in the manifest lookup_scope.json.
#   The manifest is the sole source (no directory scan, no filename guessing); a newly
#   extracted chunk must append its entity file to the manifest to enter the library.
#   Current lookup is a full scan per query (O(library rows)), unindexed — not O(1).
LOOKUP_SCOPE_FILE = HERE / "lookup_scope.json"
LOOKUP_SCOPE_ENV = "EXTRACT_LOOKUP_SCOPE"
LOOKUP_SCOPE_RULE = ("已抽取 chunk 的数值表 = 清单/显式集合里每个实体文件 metrics 表的全部行；"
                     "全量扫描、未建索引（当前成本 O(库行数)，不宣称 O(1)）")


def lookup_scope_path():
    raw = os.environ.get(LOOKUP_SCOPE_ENV)
    return pathlib.Path(raw) if raw else LOOKUP_SCOPE_FILE


def read_lookup_manifest(path=None):
    """Read the whole-library manifest -> [(entity file path, manifest-declared
    source_chunk_id or None)].

    Missing manifest = hard error: the library boundary must be explicit, never silently
    degrade to a single chunk.
    """
    p = pathlib.Path(path or lookup_scope_path())
    if not p.is_file():
        raise ExtractError("lookup 全库清单缺失：%s（task-80：全库边界必须显式，不能退化成单 chunk）" % p)
    doc = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for it in doc.get("files") or []:
        if isinstance(it, str):
            out.append((p.parent / it, None))
        elif isinstance(it, dict) and it.get("path"):
            out.append((p.parent / it["path"], it.get("source_chunk_id")))
        else:
            raise ExtractError("lookup_scope.json 的 files 项不合法：%r" % (it,))
    return out


def is_manifest_doc(doc):
    """The manifest file's self-report marker: source == "manifest" (lookup_scope.json and its
    copies are recognized by this)."""
    return isinstance(doc, dict) and doc.get("source") == "manifest"


def reject_manifest_scope(path=None):
    """scope only accepts an entity-file list / inline dict list; passing a manifest errors
    directly.

    Before, no error: the manifest was read as an entity file (no metrics table) -> returned
    "no value rows for this metric name", a format error disguised as "not found" (silent
    empty result). Prefer exploding at the entry over returning an empty answer that looks
    normal.
    """
    hint = "manifest 请用 scope=None（或传实体文件列表）"
    if path:
        raise ExtractError("lookup scope 收到的是全库清单（manifest，不是实体文件）：%s；%s"
                           % (path, hint))
    raise ExtractError("lookup scope 收到的是全库清单（manifest，不是实体文件）；%s" % hint)


def lookup_scope_entries(scope=None):
    """Normalize scope into [{"path","source_chunk_id","entities"}], returning (scope_mode,
    entries).

    scope=None -> read the whole-library manifest (default whole library, scope_mode=manifest)
    otherwise  -> explicit set (scope_mode=explicit): elements may be an entity dict / path
                  string / {"path"} / {"entities","source_chunk_id"}.
                  manifest (dict containing files, or path JSON with source==manifest) is not
                  allowed in — raises ExtractError directly, no silent "read as entity file"
                  degradation.
    """
    if scope is None:
        entries = []
        for path, cid in read_lookup_manifest():
            if not path.is_file():
                raise ExtractError("lookup 清单里的实体文件不存在：%s" % path)
            doc = json.loads(path.read_text(encoding="utf-8"))
            entries.append({"path": str(path),
                            "source_chunk_id": cid or doc.get("source_chunk_id"),
                            "entities": doc})
        return "manifest", entries
    items = scope if isinstance(scope, list) else [scope]
    entries = []
    for it in items:
        if isinstance(it, dict) and "files" in it:
            reject_manifest_scope(it.get("path") or "(inline dict)")
        if isinstance(it, (str, pathlib.Path)):
            p = pathlib.Path(it)
            if not p.is_file():
                raise ExtractError("显式 lookup 库里的实体文件不存在：%s" % p)
            doc = json.loads(p.read_text(encoding="utf-8"))
            if is_manifest_doc(doc):
                reject_manifest_scope(p)
            entries.append({"path": str(p), "source_chunk_id": doc.get("source_chunk_id"),
                            "entities": doc})
        elif isinstance(it, dict) and "entities" in it:
            entries.append({"path": it.get("path") or "(inline)",
                            "source_chunk_id": it.get("source_chunk_id"),
                            "entities": it.get("entities") or {}})
        elif isinstance(it, dict) and it.get("path"):
            p = pathlib.Path(it["path"])
            if not p.is_file():
                raise ExtractError("显式 lookup 库里的实体文件不存在：%s" % p)
            doc = json.loads(p.read_text(encoding="utf-8"))
            if is_manifest_doc(doc):
                reject_manifest_scope(p)
            entries.append({"path": str(p),
                            "source_chunk_id": it.get("source_chunk_id") or doc.get("source_chunk_id"),
                            "entities": doc})
        elif isinstance(it, dict):
            entries.append({"path": it.get("path") or "(inline)",
                            "source_chunk_id": it.get("source_chunk_id"),
                            "entities": it})
        else:
            raise ExtractError("lookup scope 项不合法：%r" % (it,))
    return "explicit", entries


def library_metric_rows(scope, name, table="metrics", name_field="指标名", value_field="值"):
    """Candidate rows of the same metric name across the library: each row carries
    source_chunk_id + which entity file it came from (visible in the manifest)."""
    _, entries = lookup_scope_entries(scope)
    rows = []
    for e in entries:
        for it in metric_rows(e["entities"], name, table, name_field, value_field):
            row = it["row"]
            rows.append({"index": it["index"], "value": it["value"], "row": row,
                         "source_chunk_id": row.get("source_chunk_id") or e["source_chunk_id"],
                         "path": e["path"]})
    return rows


def _lookup_candidate(it):
    row = it["row"]
    return {"值": it["value"], "单位": row.get("单位"),
            TIME_NORM_FIELD: row.get(TIME_NORM_FIELD),
            "source_chunk_id": it["source_chunk_id"], "path": it["path"]}



def metric_name_key(name):
    """Metric-name comparison criterion: strip whitespace ("P95 延迟" and "P95延迟" are the
    same metric)."""
    return re.sub(r"\s+", "", str(name or ""))


def parse_time_norm(value):
    """time_norm -> datetime (timezone-aware); returns None if unparseable (no timezone
    guessing).

    day precision stores only YYYY-MM-DD (the source has no clock time, cannot invent one),
    and sorts as 00:00+08:00 of that day — "take the latest" only needs comparability, not a
    fabricated clock time.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    try:
        dt = datetime_from("%Y-%m-%dT%H:%M:%S+08:00", s)
    except ValueError:
        try:
            dt = datetime_from("%Y-%m-%d", s)
        except ValueError:
            return None
    return dt.replace(tzinfo=shanghai_tz())


def metric_rows(entities, name, table="metrics", name_field="指标名", value_field="值"):
    """Candidate rows of the same metric name (the value must be a number, not bool); order
    keeps the original table order."""
    want = metric_name_key(name)
    rows = []
    for i, row in enumerate((entities or {}).get(table) or [], 1):
        if not isinstance(row, dict) or metric_name_key(row.get(name_field)) != want:
            continue
        value = row.get(value_field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        rows.append({"index": i, "row": row, "value": value})
    return rows


def lookup_metric(name, order="latest", scope=None, table="metrics",
                  name_field="指标名", value_field="值", unit_field="单位"):
    """Take one row across the library by "time column / value" to answer: latest=most recent
    (now), oldest=oldest, max=largest.

    Three definitions:
      * scope: scope=None uses the whole-library manifest (all extracted chunks' metrics
        tables, see lookup_scope.json); scope=... uses an explicit set (for unit tests /
        comparison; scope_mode=explicit is inspectable in the result).
        scope passed a manifest (dict containing files / path JSON with source==manifest)
        raises ExtractError directly ("manifest 请用 scope=None（或传实体文件列表）"), no
        silent empty return.
      * same time_norm but different value = conflict: no silent pick, value=None +
        conflict_candidates (each candidate carrying its own source_chunk_id), judge_card
        therefore turns red.
      * the answer carries source + candidate list: source_chunk_id + compared (all compared
        rows: value / time_norm / source_chunk_id / which entity file).
    """
    if order not in LOOKUP_ORDERS:
        raise ExtractError("order 只能是 %s，收到 %r" % ("/".join(LOOKUP_ORDERS), order))
    scope_mode, entries = lookup_scope_entries(scope)
    rows = library_metric_rows(scope, name, table, name_field, value_field)
    # retrieval also must not "use it and drop it" — report unresolved rows in the library.
    unres = scope_unresolved_rows(scope)
    skipped, timed = 0, []
    for it in rows:
        if order == "max":
            timed.append(it)
            continue
        dt = parse_time_norm(it["row"].get(TIME_NORM_FIELD))
        if dt is None:
            skipped += 1
            continue
        it["dt"] = dt
        timed.append(it)
    info = {"指标名": name, "指标名口径": metric_name_key(name), "order": order,
            "table": table, "scope_mode": scope_mode,
            "scope": {"mode": scope_mode, "rule": LOOKUP_SCOPE_RULE,
                      "files": [{"path": e["path"], "source_chunk_id": e["source_chunk_id"]}
                                for e in entries]},
            "candidates": len(rows), "skipped_no_time": skipped,
            "chosen": None, "value": None, "unit": None, TIME_NORM_FIELD: None,
            "source_chunk_id": None, "conflict": False, "conflict_candidates": [],
            "compared": [_lookup_candidate(it) for it in rows],
            UNRESOLVED_ROWS_KEY: unres, "unresolved_note": unresolved_note(unres),
            "reason": None}
    if not rows:
        info["reason"] = "库里没有该指标名的数值行（未作答，不猜）"
        return info
    if not timed:
        info["reason"] = "候选行都没有可用的 time_norm（判断不了哪行是「现在」）"
        return info
    if order == "max":
        best = max(timed, key=lambda it: it["value"])
    elif order == "latest":
        best = max(timed, key=lambda it: it["dt"])
    else:
        best = min(timed, key=lambda it: it["dt"])
    if order in ("latest", "oldest"):
        tied = [it for it in timed if it["dt"] == best["dt"]]
        distinct = {it["value"] for it in tied}
        if len(distinct) > 1:
            info.update({"conflict": True,
                         "conflict_candidates": [_lookup_candidate(it) for it in tied],
                         "reason": "同 time_norm=%s 但值不同（%d 个候选）：不静默选一个；"
                                   "候选各自 source_chunk_id 见 conflict_candidates（%s）"
                                   % (best["row"].get(TIME_NORM_FIELD), len(tied),
                                      "/".join("%s@%s" % (it["value"], it["source_chunk_id"])
                                               for it in tied))})
            return info
    row = best["row"]
    info.update({"chosen": best["index"], "value": best["value"],
                 "unit": row.get(unit_field),
                 TIME_NORM_FIELD: row.get(TIME_NORM_FIELD),
                 # the query side really reads time_precision / time_uncertain — 精度 is part
                 # of the answer (asking "what time" with day precision must answer "only
                 # known to the day"), not a field lying in the library. Missing = None (not
                 # default day, no guessing).
                 QUERY_PRECISION_KEY: row.get(TIME_PRECISION_FIELD),
                 "time_uncertain": row.get(TIME_UNCERTAIN_FIELD),
                 "source_chunk_id": best["source_chunk_id"]})
    return info


def answer_ok(expected, got):
    """Scoring: both value and unit must match (values compare as numbers, tolerance 1e-9).

    The answer-side fields **written** in expected (precision / time_norm / time_uncertain)
    must also match — only reading the field makes them differ; writing them into the
    expectation is exactly verifying "the query side really read it".
    """
    if not isinstance(expected, dict) or not isinstance(got, dict):
        return False
    ev, gv = expected.get("值"), got.get("值")
    if not isinstance(ev, (int, float)) or isinstance(ev, bool):
        return False
    if not isinstance(gv, (int, float)) or isinstance(gv, bool):
        return False
    if abs(float(ev) - float(gv)) > 1e-9:
        return False
    if (expected.get("单位") or None) != (got.get("单位") or None):
        return False
    for key in QUERY_FIELD_CONTRACT:
        if key in ("值", "单位") or key not in expected:
            continue
        if (expected.get(key) if expected.get(key) is not None else None) != \
                (got.get(key) if got.get(key) is not None else None):
            return False
    return True


def judge_card(card, scope=None, order_override=None):
    """Judge one annotated card: run the whole-library lookup per the card's 查法, then compare
    against the 预期答案 (value + unit).

    order_override is only for negative examples (e.g. answering the "max" question as
    latest, which should turn red). The answer carries source (source_chunk_id) and the
    candidate list (compared); on conflict ok=False with reason stated.
    """
    order = order_override or card.get("查法") or "latest"
    got = lookup_metric(card.get("指标名"), order=order, scope=scope)
    answer = {"值": got.get("value"), "单位": got.get("unit"),
              TIME_NORM_FIELD: got.get(TIME_NORM_FIELD),
              QUERY_PRECISION_KEY: got.get(QUERY_PRECISION_KEY),
              "time_uncertain": got.get("time_uncertain"),
              "来源": got.get("source_chunk_id")}
    return {"id": card.get("id"), "题面": card.get("题面"), "查法": order,
            "作答": answer, "预期答案": card.get("预期答案"),
            "旧值": card.get("旧值"), "ok": answer_ok(card.get("预期答案"), answer),
            "reason": got.get("reason"), "冲突": got.get("conflict"),
            "候选": got.get("compared")}


def answer_stats(entities):
    """Cross-table join only allows explicit/contain rows to answer.

    aggregate (derived totals) and mention (name mentions) do not enter the answer: the
    former has no single source event, the latter's anchor is chunk+quote rather than an
    event — joining with them would produce "looks matched but is not" answers. False
    negative < false positive: when not found, don't answer, don't pollute the answer.
    Counted via the __fk_method temporary marker set at normalize; the caller must strip the
    marker after counting.

    answer_rows is judged alongside explicit_ratio — coverage passing != answerable. Here
    answer_rows / answer_ratio / both thresholds are reported together; the criterion warns
    in extract() (answer_ok: True passes / False warns / None no rows to judge, 0 rows across
    the three tables is not "below standard").
    """
    total, low, low_rows = 0, 0, []
    for table, rows in entities.items():
        for i, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            method = row.get("__fk_method")
            if method is None:      # the card table has no attribution method (it is the event root itself)
                continue
            total += 1
            if method not in ANSWER_METHODS:
                low += 1
                low_rows.append("%s[#%d]" % (table, i))
    answer_rows = total - low
    answer_ratio = round(answer_rows / total, 4) if total else None
    if total == 0:                       # no rows to judge (not "below standard", just not judged)
        answer_ok = None
    else:
        answer_ok = (answer_rows >= FK_ANSWER_ROWS_MIN
                     and answer_ratio >= FK_ANSWER_RATIO_MIN)
    return {"rows": total, "answer_rows": answer_rows,
            "answer_ratio": answer_ratio,
            "answer_rows_min": FK_ANSWER_ROWS_MIN,
            "answer_ratio_min": FK_ANSWER_RATIO_MIN,
            "answer_ok": answer_ok,
            "excluded_low_confidence": low, "low_confidence_rows": low_rows,
            "answer_methods": list(ANSWER_METHODS)}


def _median(values):
    s = sorted(values)
    n = len(s)
    if not n:
        return None
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def stability_point(result):
    """Four fixed numbers per extraction run."""
    answer = (result.get("fk") or {}).get("answer") or {}
    return {"rows": (result.get("fk") or {}).get("rows"),
            "explicit": (result.get("fk") or {}).get("explicit"),
            "nearest": (result.get("fk") or {}).get("nearest"),
            "answer_rows": answer.get("answer_rows")}


def stability_report(points):
    """Health report after N repeats (median + range + whether it jitters).

    The "range wider than ±10%" criterion: half-width = (max−min)/2, relative half-width >
    STABILITY_TOLERANCE (10%) counts as unstable. When median is 0: all-zero counts as
    stable; a nonzero appearing counts as jitter (relative width cannot be computed).
    Unstable metrics get role=health_only (not a criterion, health only), stable ones get
    role=judge.
    """
    metrics = {}
    unstable = []
    for key in STABILITY_METRICS:
        vals = [p.get(key) for p in points]
        vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not vals:
            metrics[key] = {"values": [], "median": None, "min": None, "max": None,
                            "half_width": None, "rel_half_width": None,
                            "unstable": True, "role": "health_only",
                            "why": "没有可比的数（这一轮没产出）"}
            unstable.append(key)
            continue
        med = _median(vals)
        lo, hi = min(vals), max(vals)
        half = (hi - lo) / 2.0
        if med:
            rel = round(half / med, 4)
            is_unstable = rel > STABILITY_TOLERANCE
        else:
            rel = None
            is_unstable = hi != lo
        metrics[key] = {"values": vals, "median": med, "min": lo, "max": hi,
                        "half_width": half, "rel_half_width": rel,
                        "tolerance": STABILITY_TOLERANCE,
                        "unstable": is_unstable,
                        "role": "health_only" if is_unstable else "judge"}
        if is_unstable:
            unstable.append(key)
    return {"repeats": len(points), "temperature": 0.0,
            "metrics": metrics, "unstable_metrics": unstable,
            "tolerance": STABILITY_TOLERANCE,
            "note": "同一 chunk 重复跑 %d 次、temperature=0；区间半宽 > ±%.0f%% 的指标标 unstable，"
                    "只当体检、不当判据——单次达标不算闭环。"
                    % (len(points), STABILITY_TOLERANCE * 100)}


def load_e2e_asserts(path=None):
    """Read the end-to-end assertion list (default e2e_asserts.json).

    Each entry is a question "only the query side reading this field can answer correctly":
      {"id", "题面", "指标名", "查法", "预期": {值, 单位, 精度, ...}, "证明字段"}
    """
    p = pathlib.Path(path) if path else E2E_ASSERTS_PATH
    if not p.is_file():
        raise ExtractError("端到端断言清单不存在：%s（task-107 三：字段新增必须配断言）" % p)
    doc = load_json(p, "e2e_asserts")
    items = doc.get("asserts") if isinstance(doc, dict) else doc
    if not isinstance(items, list) or not items:
        raise ExtractError("端到端断言清单为空：%s（空清单 = 没验，不算过）" % p)
    return items


def check_e2e_field_coverage(asserts):
    """Every field in the query-side field contract must have one assertion writing it into
    预期.

    No coverage = this field was "said to have changed, but nobody actually read it in an
    answer" — error directly, no silent pass.
    """
    covered = {}
    for a in asserts:
        exp = a.get("预期") or {}
        for f in QUERY_FIELD_CONTRACT:
            if f in exp:
                covered.setdefault(f, []).append(a.get("id") or a.get("题面"))
    missing = [f for f in QUERY_FIELD_CONTRACT if f not in covered]
    if missing:
        raise ExtractError(
            "端到端断言覆盖不全（task-107 三）：查询侧契约字段 %s 没有任何一条断言把答案侧字段"
            "写进预期——加字段却只写单测，就是这个坑（time_precision 的第二次）。现有覆盖：%s"
            % ("、".join(missing),
               "；".join("%s<-%s" % (k, ",".join(map(str, v))) for k, v in covered.items())))
    return {"ok": True, "covered": covered,
            "rule": "QUERY_FIELD_CONTRACT 每个字段 >=1 条端到端断言（预期里含该字段）"}


def run_e2e_asserts(asserts, scope=None, chunk=None):
    """Run end-to-end assertions: from question to answer, prove the field was really read
    (unit tests don't count).

    Criterion: after lookup_metric answers, **every answer-side field written in the
    expectation** must match (value/unit/precision/…). Wrong answer = red, not "uncovered".
    An assertion may carry "chunk" (only effective for a certain chunk) — when running on a
    different chunk, the irrelevant ones are recorded as not_applicable (not skipped, not
    passed).
    """
    out, red, skipped = [], 0, 0
    cur = (chunk or {}).get("source_chunk_id") if isinstance(chunk, dict) else None
    for a in asserts:
        want_chunk = a.get("chunk")
        if want_chunk and cur and want_chunk != cur:
            skipped += 1
            out.append({"id": a.get("id"), "题面": a.get("题面"), "ok": None,
                        "not_applicable": True,
                        "reason": "断言只针对 chunk %r（当前 chunk %r）" % (want_chunk, cur)})
            continue
        name, order = a.get("指标名"), a.get("查法") or "latest"
        got = lookup_metric(name, order=order, scope=scope or a.get("scope"))
        answer = {"值": got.get("value"), "单位": got.get("unit"),
                  TIME_NORM_FIELD: got.get(TIME_NORM_FIELD),
                  QUERY_PRECISION_KEY: got.get(QUERY_PRECISION_KEY),
                  "time_uncertain": got.get("time_uncertain"),
                  "来源": got.get("source_chunk_id")}
        exp = a.get("预期") or {}
        bad_fields = []
        for f in QUERY_FIELD_CONTRACT:
            if f not in exp:
                continue
            want = exp.get(f)
            have = answer.get(f)
            if f in ("值",) and isinstance(want, (int, float)) and isinstance(have, (int, float)):
                if abs(float(want) - float(have)) > 1e-9:
                    bad_fields.append(f)
                continue
            if (want if want is not None else None) != (have if have is not None else None):
                bad_fields.append(f)
        ok = not bad_fields
        if not ok:
            red += 1
        out.append({"id": a.get("id"), "题面": a.get("题面"), "查法": order,
                    "指标名": name, "预期": exp, "作答": answer,
                    "证明字段": a.get("证明字段"),
                    "bad_fields": bad_fields, "ok": ok,
                    "reason": got.get("reason")})
    return {"ok": red == 0, "red": red, "total": len(out), "skipped": skipped,
            "asserts": out,
            "rule": "从提问到答案：预期里写了的答案侧字段全部对上才算通；"
                    "单测证明「函数返回字段」不算端到端"}


def build_answer_index(chunk, result):
    """Record "whether each row can answer / which event it attaches to" into a reviewable
    index.

    Pairwise coverage reasons per data row, so it carries the quote source (already added in
    detail), no longer guessing by where/pos — too many rows share the same pos (a whole
    summary segment attaches to the same card).
    """
    cards = []
    for row in result["entities"].get("cards") or []:
        quote = row.get("quote")
        cards.append({"event_id": row.get("event_id"),
                      "action": row.get("action"), "target": row.get("target"),
                      "quote": (quote.get("原文") if isinstance(quote, dict) else None)})
    rows = [{"table": d.get("table"), "pos": d.get("pos"), "method": d.get("method"),
             "event_id": d.get("event_id"), "answerable": bool(d.get("answerable")),
             "quote": d.get("quote")}
            for d in (result.get("fk") or {}).get("detail") or []]
    return {"chunk": chunk.get("source_chunk_id"), "source": result.get("source"),
            "answer": (result.get("fk") or {}).get("answer"),
            "answer_methods": list(ANSWER_METHODS),
            "cards": cards, "rows": rows}


def pair_check(index, questions):
    """Verify per question whether "the event + its data row" are both in answer.

    Each question gives event_quote (searched in card quote / action / target) and
    row={table, quote} (searched in data-row quote), criteria:
      ① the event was extracted; ② the data row was extracted; ③ the data row's method ∈ the
        answer set; ④ the data row's event_id is the event this question asks about.
    ①② fail = extraction missed it; ③ fail = the row itself is unanswerable
    (aggregate/mention); ④ fail = wrong link (row attached to another event) — none of these
    is "ratio too low", it is a pairing problem.
    """
    cards = index.get("cards") or []
    rows = index.get("rows") or []
    out = []
    for q in questions:
        ev_need = q.get("event_quote") or q.get("event") or ""
        row_need = q.get("row") or {}
        table = row_need.get("table")
        row_quote = row_need.get("quote") or ""
        ev_hits = [c for c in cards if ev_need and
                   (ev_need in (c.get("quote") or "") or ev_need in (c.get("target") or "")
                    or ev_need in (c.get("action") or ""))]
        row_hits = [r for r in rows if (table is None or r.get("table") == table)
                    and row_quote and row_quote in (r.get("quote") or "")]
        ev = ev_hits[0] if ev_hits else None
        row = row_hits[0] if row_hits else None
        ok = False
        if ev is None:
            reason = "事件不在 answer 里（卡片没抽出来，或 quote 对不上）"
        elif row is None:
            reason = "数据行不在 answer 里（该表没抽到这一行）"
        elif not row.get("answerable"):
            reason = "数据行不可作答（method=%s：aggregate/mention 不进 answer）" % row.get("method")
        elif row.get("event_id") != ev.get("event_id"):
            reason = ("事件与数据行不成对：行挂在 %r，题问的是 %r（关联错，不是比例低）"
                      % (row.get("event_id"), ev.get("event_id")))
        else:
            ok = True
            reason = "事件 + 数据行都在 answer 里，且成对"
        out.append({"q": q.get("q"), "event_found": ev is not None,
                    "event_id": ev.get("event_id") if ev else None,
                    "row_found": row is not None,
                    "row_table": (row.get("table") if row else table),
                    "row_method": row.get("method") if row else None,
                    "row_event_id": row.get("event_id") if row else None,
                    "row_answerable": bool(row.get("answerable")) if row else False,
                    "pair_ok": ok, "reason": reason})
    total = len(out)
    ok_n = sum(1 for r in out if r["pair_ok"])
    return {"chunk": index.get("chunk"), "source": index.get("source"),
            "answer": index.get("answer"),
            "questions": out, "total": total, "pair_ok": ok_n,
            "pair_ratio": round(ok_n / total, 4) if total else None,
            "all_pair_ok": total > 0 and ok_n == total,
            "note": "成对覆盖 = 某事件 + 它的那条数据行同时出现在 answer 里；"
                    "answer_rows / rows 是单边行占比，代替不了这一判据。"}


def load_questions(path):
    """Question list: a JSON array, or {"questions": [...]}."""
    doc = load_json(path, "questions")
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict) and isinstance(doc.get("questions"), list):
        return doc["questions"]
    raise ExtractError("题单须是 JSON 数组或 {\"questions\": [...]}：%s" % path)


def save_failed_piece(chunk, piece_idx, attempts, reason, raw):
    """Tier B: after 2 retries still rejected -> source+reason into failed/, and counted (no
    silent drop)."""
    from datetime import datetime, timezone
    try:
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(chunk.get("source_chunk_id") or "chunk"))[:60]
        path = FAILED_DIR / ("%s__p%02d__%s.json" % (safe, piece_idx, stamp))
        payload = {"chunk": chunk.get("source_chunk_id"), "piece": piece_idx,
                   "attempts": attempts, "reason": reason, "raw": (raw or "")[-8000:]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return str(path)
    except OSError:
        return None


# ---------------------------------------------------------------- normalization

def normalize_row(table, row, chunk, props, warnings, idx, ctx, piece=None):
    """Tier A completion + shape re-check (type/enum/pattern). Which tier does what is
    decided by rules; here only executes per attribute."""
    rules = ctx["rules"]
    where = "%s[#%d]" % (table, idx)
    ctx["where"] = where
    ctx["piece"] = piece      # sequences resolve per piece; attribution must carry the piece number
    # temporary fields pop first (not into the output), only left as the explicit hint for fk attribution
    hints = rules.pop_temp_fields(table, row)
    ctx["hint"] = hints.get(rules.hint_field.get(table))
    keep_known_fields(row, props, warnings, where, piece=piece)
    strip_optional_empty(row, table, rules, where, warnings, piece=piece)
    # "no time info" => null (not reject). A field with on_missing=null that is missing/blank
    # is explicitly written null (the key stays, saying "this row has no such info", not "the
    # program forgot this field").
    for name, spec in rules.tables[table].items():
        if spec.get("on_missing") != "null":
            continue
        if _is_blank(row.get(name)):
            row[name] = None
    # time_raw is pure verbatim, must not be rewritten. Not found in source => do not accept
    # this form: record warning + null (downstream time_norm then also null — prefer no time
    # over a fake time).
    for name, spec in rules.tables[table].items():
        if not spec.get("verbatim"):
            continue
        val = row.get(name)
        if _is_blank(val):
            continue
        if not time_raw_verbatim(val, chunk.get("text")):
            ctx.setdefault("trace_hits", []).append(
                {"where": where, "field": name, "value": val,
                 "why": "值在 chunk 原文里找不到（纯剪贴字段被改写）"})
            warn_row(warnings, where, "time_raw_not_verbatim",
                     "%s: %r=%r 在 chunk 原文里找不到（time_raw 是纯剪贴，不许改写成别的写法）"
                     " -> 置 null，time_norm 跟着 null（task-101）" % (where, name, val),
                     piece=piece)
            row[name] = None
    # tier A: fill=derived fields are produced by the program (overwriting any model-reported
    # value, silently); underivable and the model also gave nothing -> retry the piece
    for algo in DERIVE_ORDER:
        for name, spec in rules.tables[table].items():
            if spec.get("derive") != algo or spec["on_missing"] not in ("fill", "null"):
                continue
            val = DERIVERS[algo](row, spec, chunk, ctx)
            if _is_blank(val):
                # underivable => null, **never fall back to the model-reported value** (that is
                # silent fabrication: the model pairing "去年" with 2022-01-01 would also be
                # accepted). The null key stays; audit/check_6 only looks at non-null values.
                if spec["on_missing"] == "null":
                    row[name] = None
                    continue
                # a nullable field underivable is left unwritten (identities.event_id default =
                # treated as mention, located by source_chunk_id + extraction position + quote),
                # no fabricated key, no piece reject
                if spec.get("nullable"):
                    row.pop(name, None)
                    continue
                if _is_blank(row.get(name)):
                    raise PieceRejected("%s: %r 缺失且程序推不出（derive=%s，A 档失败）"
                                        % (where, name, algo))
                continue  # program underivable but the model gave one: keep it, hand to shape re-check
            row[name] = val
    # shape re-check: type / enum / pattern (whatever schema says)
    for name, spec in rules.tables[table].items():
        val = row.get(name)
        if _is_blank(val):
            if spec["on_missing"] == "reject":
                raise PieceRejected("%s: 必填字段 %r 缺失/空串" % (where, name))
            continue
        t = spec.get("type")
        if t == "number" and (isinstance(val, bool) or not isinstance(val, (int, float))):
            raise PieceRejected("%s: %r 必须是 number，实际 %r（B 档：数值行命根）"
                                % (where, name, val))
        if t == "integer" and (isinstance(val, bool) or not isinstance(val, int) or val < 0):
            raise PieceRejected("%s: %r 必须是 >=0 的整数，实际 %r（B 档：无法定位）"
                                % (where, name, val))
        if t == "string" and not isinstance(val, str):
            raise PieceRejected("%s: %r 必须是字符串，实际 %r" % (where, name, val))
        if spec.get("enum") and val not in spec["enum"]:
            raise PieceRejected("%s: %r=%r 不在 %s（B 档：非法枚举）"
                                % (where, name, val, spec["enum"]))
        if spec.get("pattern") and isinstance(val, str) and re.match(spec["pattern"], val) is None:
            raise PieceRejected("%s: %r=%r 不符 %s（B 档）" % (where, name, val, spec["pattern"]))
    # the model judged other, but the action word maps to a concrete enum in the alias table ->
    # the program re-judges (both languages use the same table; only words absent from the table
    # stay other; the re-judgment records a warning, inspectable)
    for name, spec in rules.tables[table].items():
        if not spec.get("alias"):
            continue
        val = row.get(name)
        if val != OP_ALIAS_DEFAULT:
            continue
        src = None
        for key in (spec.get("alias_inputs") or {}).values():
            if isinstance(row.get(key), str) and row.get(key).strip():
                src = row.get(key)
                break
        if src is None:
            continue
        mapped = ALIASERS[spec["alias"]](src)
        if mapped != OP_ALIAS_DEFAULT:
            row[name] = mapped
            warn_row(warnings, where, "op_type_alias",
                     "%s: %r=%r 按动作词改判（动作=%r -> %s；task-101 英文别名表）"
                     % (where, name, OP_ALIAS_DEFAULT, src, mapped), piece=piece)
    return row


# ---------------------------------------------------------------- piece merge

def unique_key_of(ent):
    """The unique key may only come from schema (R4: the unique key belongs to the contract,
    no second copy written in code)."""
    uk = ent.get("unique_key") if isinstance(ent, dict) else None
    if not (isinstance(uk, list) and uk):
        raise ExtractError("schema 错：%r 没有 unique_key（R4）"
                           % (ent.get("file_key") if isinstance(ent, dict) else ent))
    return uk


def merge_rows(all_rows, unique_key, warnings, table):
    """Merge and dedupe after splitting by the schema unique key (the same row extracted by two
    pieces keeps only the first copy)."""
    seen = set()
    out = []
    for piece_idx, rows in enumerate(all_rows):
        for row in rows:
            key = tuple(norm_ws(str(row.get(k, ""))) for k in unique_key)
            if key in seen:
                warn_note(warnings, "split_dedup",
                          "%s: 拆片合并丢弃重复行（唯一键 %r 已存在，来自第 %d 片）"
                          % (table, key, piece_idx + 1))
                continue
            seen.add(key)
            out.append(row)
    return out


# ---------------------------------------------------------------- content-noise gates
#
# Real-call verification found two kinds of content noise; the decision was "precisely
# decidable => gate, the rest => spot check":
#   ① a type word (仓库/版本/工具) used by the LLM as an identifier name -> machine-decidable
#     -> hard gate: red + drop the row + count;
#   ② a function name / config item (strip_fences) used as both action and identity in the
#     same fragment -> hard gate: red (routing mismatch, for human review);
#   ③ density too high (19 identities from 1200 chars = obvious over-extraction) -> inspection
#     only: per-chunk table row counts + identities per thousand chars, --density-scan lists
#     the top 5% chunks for review (the threshold is hand-picked, not a gate);
#   ④ the rest rely on spot check (spotcheck.py: 5 random rows per table + seed kept,
#     identity-table error rate <10% before continuing end-to-end).
#
# The two gates' "red" is not PieceRejected (no retry, no whole-piece drop): they run after
# normalization, only content-judge rows that already passed structural validation; hits
# record a warning (codes below) + enter monitor.content_gates.

CONTENT_GATE_ENUM = "enum_value_as_identity"     # ① an enum value used as an identifier name (drop row)
CONTENT_GATE_CROSS = "identity_card_same_string"  # ② the same string used as both identity and card action (keep row)
CONTENT_GATE_EQUALS = "name_equals_type"          # ① name==type (drop row)
CONTENT_GATE_SHAPE = "identifier_as_action"       # pure identifier shape used as an action (keep row)
CONTENT_GATE_GENERIC = "identity_generic"         # generic term not naming anything: .bak / pure suffix word (keep row)

# pure identifier shape (underscore/dotted selector, optional empty parens) — a normal action
# is a Chinese verb-object. Path separators `/` `\` added — the old regex did not recognize
# `src/tools/extract.py` and judged the file path "not an identifier"; a path is one of the most
# common identifier shapes, missing it means the shape gate only blocks half.
SHAPE_IDENT_RE = re.compile(r"^[a-z][a-z0-9_\-]*([./\\][a-z0-9_\-]+)*(\(\))?$")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

IDENTITY_ENUM_FIELD = "类型"


def identity_table_of(rules):
    """The identity table = the entity in schema that has both 名称 and an enum-typed 类型
    (no hard-coded table name)."""
    for fk in rules.order:
        props = rules.props.get(fk) or {}
        if "名称" in props and isinstance((props.get(IDENTITY_ENUM_FIELD) or {}).get("enum"), list):
            return fk
    return None


def shape_looks_like_identifier(value):
    """Pure identifier shape and no CJK -> suspected "identifier mistaken for an action" (a
    shape rule, not a blacklist)."""
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s or CJK_RE.search(s):
        return False
    return bool(SHAPE_IDENT_RE.match(s))


def _path_like(value):
    """Looks like a file path (contains a path separator). target=path is normal semantics,
    not "identifier mistaken for an action"."""
    return isinstance(value, str) and ("/" in value or "\\" in value)


def apply_content_gates(entities, rules, warnings):
    """① ② enum value as identifier name / same-string cross + shape gate.

    ①: 名称 / 完整原文写法 ∈ the type enum values, or 名称==类型 -> red + drop the row + count;
    ②: 名称 / 完整原文写法 exactly equals a card action/target -> red (routing mismatch, keep
       row); shape gate: action/target itself is pure identifier shape without CJK -> warning
       (keep row).
    Returns the report, recorded in monitor.content_gates and build_log (which rows were
    dropped, how many hits — all inspectable).
    """
    ident = identity_table_of(rules)
    report = {"identity_table": ident, "enum_values": [],
              "enum_value_dropped": 0, "name_equals_type_dropped": 0,
              "cross_same_string": 0, "shape_suspect": 0,
              "dropped_rows": 0, "red": False, "examples": []}
    # shape gate: action/target is pure identifier shape (strip_fences / gates.quote_ratio_min)
    # -> warning. It does not depend on the identity table, so it sits before the ident-is-None
    # early return (warn only, keep row, no red)
    shape = 0
    for idx, row in enumerate(entities.get(rules.cards_key) or [], 1):
        if not isinstance(row, dict):
            continue
        where = "%s[#%d]" % (rules.cards_key, idx)
        for field in ("action", "target"):
            v = row.get(field)
            if not shape_looks_like_identifier(v):
                continue
            # with path separators added to the shape gate, a **target** like `src/tools/extract.py`
            # is judged identifier-shaped — but target as a file path is normal semantics ("identifier
            # mistaken for action" is about action). So a path-shaped target does not warn; action
            # and dotted identifiers (gates.quote_ratio_min) still warn.
            if field == "target" and _path_like(v):
                continue
            shape += 1
            if len(report["examples"]) < 10:
                report["examples"].append({"where": where, "field": field, "value": v,
                                           "code": CONTENT_GATE_SHAPE, "action": "kept"})
            warn_row(warnings, where, CONTENT_GATE_SHAPE,
                     "%s: %s=%r 是纯标识符形态且不含中文 -> 疑似把标识当动作"
                     "（task-67 ②：形态规则，只报警不丢行；正常动作应是中文动宾如「改配置」）"
                     % (where, field, v))
    report["shape_suspect"] = shape
    if ident is None:
        return report
    enum_vals = list(((rules.props.get(ident) or {}).get(IDENTITY_ENUM_FIELD) or {}).get("enum") or [])
    report["enum_values"] = enum_vals
    enum_set = set(enum_vals)
    kept = []
    for idx, row in enumerate(entities.get(ident) or [], 1):
        where = "%s[#%d]" % (ident, idx)
        name, full, typ = row.get("名称"), row.get("完整原文写法"), row.get(IDENTITY_ENUM_FIELD)
        hit = None
        if isinstance(name, str) and name in enum_set:
            hit = ("enum", "名称", name)
        elif isinstance(full, str) and full in enum_set:
            hit = ("enum", "完整原文写法", full)
        elif isinstance(name, str) and isinstance(typ, str) and norm_ws(name) == norm_ws(typ):
            hit = ("equals", "名称==类型", name)
        if hit is None:
            kept.append(row)
            continue
        kind, field, value = hit
        report["red"] = True
        report["dropped_rows"] += 1
        if kind == "enum":
            report["enum_value_dropped"] += 1
            code = CONTENT_GATE_ENUM
            why = "%r 是「%s」的枚举取值（%s），不是标识名" % (value, IDENTITY_ENUM_FIELD,
                                                              "，".join(enum_vals))
        else:
            report["name_equals_type_dropped"] += 1
            code = CONTENT_GATE_EQUALS
            why = "名称 与 %s 相同（%r），类型词不是标识名" % (IDENTITY_ENUM_FIELD, value)
        if len(report["examples"]) < 10:
            report["examples"].append({"where": where, "field": field, "value": value,
                                       "code": code, "action": "dropped"})
        warn_row(warnings, where, code,
                 "%s: %s=%s -> 报红并丢弃该行（task-66 ①：枚举值当标识名，丢弃 %d 行）"
                 % (where, field, why, report["dropped_rows"]))
    entities[ident] = kept
    # ② same-string cross: within one chunk an identifier name == card action/target (exactly equal) => routing mismatch
    card_texts = set()
    for row in entities.get(rules.cards_key) or []:
        if not isinstance(row, dict):
            continue
        for field in ("action", "target"):
            v = row.get(field)
            if isinstance(v, str) and v.strip():
                card_texts.add(v)
    cross = 0
    for idx, row in enumerate(entities.get(ident) or [], 1):
        where = "%s[#%d]" % (ident, idx)
        for field in ("名称", "完整原文写法"):
            v = row.get(field)
            if not (isinstance(v, str) and v in card_texts):
                continue
            cross += 1
            report["red"] = True
            if len(report["examples"]) < 10:
                report["examples"].append({"where": where, "field": field, "value": v,
                                           "code": CONTENT_GATE_CROSS, "action": "kept"})
            warn_row(warnings, where, CONTENT_GATE_CROSS,
                     "%s: %s=%r 与卡片 action/target 完全相同（同一片段既当动作又当标识）"
                     "-> 报红（task-66 ②：路由错位，不丢行，交抽检/人看）" % (where, field, v))
            break
    report["cross_same_string"] = cross
    return report


# generic-term-not-naming shape gate — warn when an identity's 完整原文写法 is only an
# extension / a pure suffix word. .bak / .json (pure extension starting with .) or bak / 备份
# (pure suffix word) = no name written, only a generic term left. Function names
# (strip_fences), tool names (Python) belong to 工具, outside this gate's range (the shape
# gate handles the card side).
GENERIC_SUFFIX_WORDS = {"bak", "backup", "tmp", "temp", "log", "json", "md", "py", "txt",
                        "zip", "old", "orig", "copy", "副本", "备份", "临时", "日志", "快照"}


def identity_generic_reason(raw):
    """Return the "likely generic term, not naming anything" reason; None if it does not look
    generic."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith("."):
        return "以 . 开头（纯扩展名，没有名字）"
    if s.strip(".").lower() in GENERIC_SUFFIX_WORDS:
        return "纯后缀词（只有通称，没有名字）"
    return None


def apply_identity_generic_gate(entities, rules, warnings):
    """An identity's 完整原文写法 is a generic term (.bak / pure suffix word) -> warning
    (keep row).

    Such a row's "name" is empty; canonical_id would group every .bak into one, inevitably
    cross-joining. But it might genuinely mean the action object "backup", so it only warns,
    hands off to spot check / human review, never drops on its own.
    """
    ident = identity_table_of(rules)
    report = {"identity_table": ident, "suspected": 0, "examples": [], "red": False}
    if ident is None:
        return report
    for idx, row in enumerate(entities.get(ident) or [], 1):
        if not isinstance(row, dict):
            continue
        where = "%s[#%d]" % (ident, idx)
        raw = row.get("完整原文写法")
        why = identity_generic_reason(raw)
        if not why:
            continue
        report["suspected"] += 1
        report["red"] = True
        if len(report["examples"]) < 10:
            report["examples"].append({"where": where, "value": raw, "reason": why})
        warn_row(warnings, where, CONTENT_GATE_GENERIC,
                 "%s: 完整原文写法=%r %s -> 疑似通称不具名（task-70 ④：标识要写到具体名字，"
                 "只写扩展名/后缀词的行 join 会串味；不丢行，交抽检/人看）"
                 % (where, raw, why))
    return report


def enforce_fk_invariants(entities, rules):
    """Foreign-key invariant (R12) — write when the program can compute it, not write when it
    cannot, no "half-write".

    * tables whose event_id is non-optional (metrics/facts): every row must have event_id,
      and it must exist among cards;
    * tables whose event_id is optional (identities): may omit it (mention locates by
      chunk+offset+quote); when present it must exist among cards (no fabricated dangling key,
      no placeholder string).
    """
    cards_key = rules.cards_key
    card_ids = {r.get("event_id") for r in (entities.get(cards_key) or [])
                if isinstance(r, dict)}
    problems = []
    checked = 0
    for fk in rules.order:
        if fk == cards_key:
            continue
        nullable = bool(((rules.tables.get(fk) or {}).get("event_id") or {}).get("nullable"))
        for i, row in enumerate(entities.get(fk) or [], 1):
            if not isinstance(row, dict):
                continue
            checked += 1
            eid = row.get("event_id")
            where = "%s[#%d]" % (fk, i)
            if _is_blank(eid):
                if not nullable:
                    problems.append("%s: 缺 event_id（该表 event_id 不是可选的）" % where)
                continue
            if not isinstance(eid, str) or eid not in card_ids:
                problems.append("%s: event_id=%r 不在 cards 的 event_id 集合里（不许悬空外键）"
                                % (where, eid))
    if problems:
        raise ExtractError("外键不变量破了（task-70 ② / R12）：\n- "
                           + "\n- ".join(problems[:10]))
    return {"rows_checked": checked, "nullable_tables": sorted(rules.mention_tables),
            "ok": True}


def density_of(counts, text_len, identity_table=None, chunk=None, pieces=None):
    """Density inspection item: per-chunk table row counts + identities per thousand chars
    (not a gate, for human review).

    Add the cost side `calls_per_1k_chars = pieces / chars × 1000`, paired with the output
    side (rows_per_1k_chars / identities_per_1k_chars) — looking only at density without call
    count would misread post-split "higher density" as "extracted more fully". pieces
    unavailable writes null, no guessing.
    """
    counts = {k: int(v) for k, v in (counts or {}).items() if isinstance(v, int)}
    chars = max(int(text_len or 0), 1)
    rows_total = sum(counts.values())
    ident_rows = counts.get(identity_table, 0) if identity_table else 0
    n_pieces = int(pieces) if isinstance(pieces, int) and pieces > 0 else None
    return {"chunk": chunk, "chars": int(text_len or 0), "counts": counts,
            "rows_total": rows_total, "identity_rows": ident_rows,
            "identities_per_1k_chars": round(ident_rows * 1000.0 / chars, 2),
            "rows_per_1k_chars": round(rows_total * 1000.0 / chars, 2),
            "pieces": n_pieces,
            "calls_per_1k_chars": (round(n_pieces * 1000.0 / chars, 2)
                                   if n_pieces else None)}


def density_stats(entities, text_len, identity_table=None, pieces=None):
    counts = {k: len(v) for k, v in (entities or {}).items() if isinstance(v, list)}
    return density_of(counts, text_len, identity_table, pieces=pieces)


def density_outliers(stats_list, top_frac=0.05, min_samples=20):
    """Distribution outliers: list the top top_frac chunks by descending "identities per
    thousand chars" (inspection only, not a gate).

    With samples < min_samples it still ranks, but enough_samples=False (a small sample's 5%
    has no statistical meaning).
    """
    rows = [s for s in (stats_list or [])
            if isinstance(s, dict) and isinstance(s.get("identities_per_1k_chars"), (int, float))]
    rows.sort(key=lambda s: (-s["identities_per_1k_chars"], str(s.get("chunk") or "")))
    n = len(rows)
    k = max(1, int(math.ceil(n * top_frac))) if n else 0
    top = rows[:k]
    return {"key": "identities_per_1k_chars", "top_frac": top_frac, "samples": n,
            "enough_samples": n >= min_samples, "k": k,
            "threshold": top[-1]["identities_per_1k_chars"] if top else None,
            "top": [{"chunk": s.get("chunk"), "chars": s.get("chars"),
                     "identity_rows": s.get("identity_rows"),
                     "identities_per_1k_chars": s["identities_per_1k_chars"]} for s in top]}


def density_scan(path, top_frac=0.05, rules=None):
    """--density-scan: read JSONL (build_log entries {chunk, chars, counts}), list the top 5%
    density-outlier chunks."""
    identity_table = identity_table_of(rules) if rules is not None else "identities"
    p = pathlib.Path(path)
    if not p.is_file():
        raise ExtractError("[io] density-scan 文件不存在：%s" % p)
    stats = []
    skipped = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise ExtractError("density-scan 行不是 JSON：%s" % exc) from exc
        if not isinstance(entry, dict) or not isinstance(entry.get("chars"), int):
            skipped += 1
            continue
        stats.append(density_of(entry.get("counts") or {}, entry["chars"], identity_table,
                                entry.get("chunk")))
    report = density_outliers(stats, top_frac)
    report["scanned"] = len(stats)
    report["skipped_missing_chars"] = skipped
    report["note"] = "inspection only: high density is not necessarily wrong, listed for human review; the threshold / top 5% is hand-picked, not a gate"
    return report


# ---------------------------------------------------------------- coverage-style assertion
#
# The core problem: real1200's "expected values" were copied from run results — the run
# extracted 5 identities, so the expectation wrote 5, treating the 15 missed ones as "it
# should have been 5". The criterion was changed to coverage-style, two independent items:
#   * missed within the list = red (incomplete coverage is an error, not "missing a little is fine")
#   * extra beyond the list = warning (over/mis-extraction must be visible, but not a whole-run failure)
# The list (expected.json) is counted by a human reading the chunk source, independent of this
# file's output; see fixtures/real1200/expected.json.

EXPECTED_TABLES = ("metrics", "identities", "facts")


def metric_key(row):
    # join into one string: "which one is missing" can be read out directly (tuples/lists are hard to read in the trail)
    return "%s=%s %s" % (norm_ws(str(row.get("指标名") or "")),
                         norm_ws(str(row.get("值"))),
                         norm_ws(str(row.get("单位") or "")))


def identity_key(row):
    # output rows use 完整原文写法; the human list calls it 写法 (two names for the same thing)
    return norm_ws(str(row.get("完整原文写法") or row.get("写法")
                       or row.get("名称") or ""))


def fact_key(row):
    return norm_ws(str(row.get("等式") or ""))


def load_expected(path):
    doc = load_json(path, "expected")
    if not isinstance(doc, dict):
        raise ExtractError("--expected 须是对象：{expected_metrics / expected_identities / expected_facts}")
    return doc


def expected_keys(expected):
    """Normalize the human list into per-table key lists (keep list order; "which ones are
    missing" is stated in the original terms)."""
    keys = {"metrics": [], "identities": [], "facts": []}
    for m in expected.get("expected_metrics") or []:
        keys["metrics"].append(metric_key(m))
    for x in expected.get("expected_identities") or []:
        keys["identities"].append(identity_key(x if isinstance(x, dict) else {"写法": x}))
    for f in expected.get("expected_facts") or []:
        keys["facts"].append(fact_key(f if isinstance(f, dict) else {"等式": f}))
    return keys


def expected_coverage(entities, expected):
    """Within the list must be covered (missed = red); beyond the list is always warning
    (over/mis-extraction must be visible)."""
    want = expected_keys(expected)
    keyfns = {"metrics": metric_key, "identities": identity_key, "facts": fact_key}
    tables = {}
    missing_total = 0
    extra_total = 0
    for table in EXPECTED_TABLES:
        rows = [r for r in (entities.get(table) or []) if isinstance(r, dict)]
        got = [keyfns[table](r) for r in rows]
        got_set = set(got)
        wanted = want[table]
        wanted_set = set(wanted)
        missing = [k for k in wanted if k not in got_set]
        # rows beyond the list (reported deduped; the same writing in multiple rows reports once)
        extra = []
        seen = set()
        for k in got:
            if k not in wanted_set and k not in seen:
                seen.add(k)
                extra.append(k)
        missing_total += len(missing)
        extra_total += len(extra)
        tables[table] = {"expected": len(wanted), "present": len(wanted) - len(missing),
                         "missing": missing,
                         "extra": extra,
                         "ok": not missing}
    return {"expected_path": None, "tables": tables,
            "missing_total": missing_total, "extra_total": extra_total,
            "ok": missing_total == 0,
            "note": "清单内漏抽=红；清单外=warning（可见但不作废）"}


# ---------------------------------------------------------------- main flow

def load_chunk(path):
    chunk = load_json(path, "chunk")
    for key in ("source_chunk_id", "text"):
        if not chunk.get(key):
            raise ExtractError("chunk 缺字段 %r" % key)
    # time_ref is no longer a hard requirement — when none of the three bases
    # (turn/session/chunk) is available, relative time is all null + time_uncertain=true
    # (never now()). When given, it must be ISO8601.
    for key in (BASE_FALLBACK_FIELD,) + BASE_LEVELS:
        val = _base_value(chunk, key)
        if val and not ISO_PATTERN.match(val):
            raise ExtractError("chunk.%s 必须是 ISO8601（YYYY-MM-DDTHH:MM:SS+08:00），实际 %r"
                               % (key, val))
    if not isinstance(chunk["text"], str):
        raise ExtractError("chunk.text 必须是字符串")
    return chunk


def load_replay(path):
    """--replay normalized into {"default": str|None, "pieces": [str]|None, "attempts":
    {piece no.: [str]}}."""
    return normalize_replay(load_json(path, "replay"))


def normalize_replay(doc):
    """Normalize one replay document (string / object) into the same shape (each --repeat
    round's document also goes through here)."""
    out = {"default": None, "pieces": None, "attempts": {}}
    if isinstance(doc, str):
        out["default"] = doc
        return out
    if isinstance(doc, dict):
        for key in ("response", "text", "content"):
            if isinstance(doc.get(key), str):
                out["default"] = doc[key]
                break
        for key in ("pieces", "responses"):
            if isinstance(doc.get(key), list):
                out["pieces"] = [str(x) for x in doc[key]]
                break
        attempts = doc.get("attempts")
        if isinstance(attempts, dict):
            for key, seq in attempts.items():
                try:
                    piece_no = int(key)
                except (TypeError, ValueError):
                    raise ExtractError("--replay.attempts 的键必须是片号整数，实际 %r" % (key,))
                if isinstance(seq, str):
                    seq = [seq]
                if not isinstance(seq, list) or not seq:
                    raise ExtractError("--replay.attempts[%r] 必须是非空数组" % (key,))
                out["attempts"][piece_no] = [str(x) for x in seq]
        if out["default"] is None and out["pieces"] is None and not out["attempts"]:
            raise ExtractError("--replay 文件须是字符串、含 response/text/content 的对象、"
                               "含 pieces/responses 数组的对象，或含 attempts 的对象")
        return out
    raise ExtractError("--replay 文件须是字符串或对象")


def load_replays(path, repeats):
    """--repeat uses one response per round, simulating the real jitter of "the same chunk
    run N times".

    Accepts a list (one item per round) or {"repeats": [...]}; a plain single document is
    still compatible (each round uses the same one). When rounds exceed the given count, the
    last one is reused (not fabricated content, just reused).
    """
    doc = load_json(path, "replay")
    seq = None
    if isinstance(doc, list):
        seq = doc
    elif isinstance(doc, dict) and isinstance(doc.get("repeats"), list):
        seq = doc["repeats"]
    if seq is None:
        return [normalize_replay(doc)] * repeats
    if not seq:
        raise ExtractError("--replay.repeats 不能是空数组")
    normalized = [normalize_replay(x) for x in seq]
    while len(normalized) < repeats:
        normalized.append(normalized[-1])
    return normalized[:repeats]


def pick_replay(replay, piece_no, attempt):
    """Take the response text of piece_no (from 1), attempt (from 1)."""
    seq = replay.get("attempts", {}).get(piece_no)
    if seq:
        if attempt <= len(seq):
            return seq[attempt - 1]
        return seq[-1]  # more retries than the given sequence: repeat the last (simulate a stuck model, not fake success)
    pieces = replay.get("pieces")
    if pieces is not None:
        if piece_no - 1 < len(pieces):
            return pieces[piece_no - 1]
        return "{}"  # more pieces than given responses: this piece is empty, not fabricated
    if replay.get("default") is not None:
        return replay["default"]
    return "{}"


def extract(chunk, schema, replay_text=None, prompt=None, rules=None, rules_path=None,
            expected=None):
    text = chunk["text"]
    limit = max_chunk_chars()
    pieces = split_chunk(text, limit)
    rules = rules or load_rules(schema, rules_path)
    warnings = []
    prompts = []
    metas = []
    accepted = []   # [{"piece": piece no., "scope": (a,b), "raw": parsed doc}]
    failed = []     # [{"piece","attempts","reason","path"}]
    unlocated = []  # rows with neither offset nor text anchor (not into output, but not silently dropped)
    dropped = 0
    retries = 0
    quote_retried_pieces = []   # pieces that still failed after a piece-level retry (stated in the degradation trail)
    max_try = max_retries() + 1
    source = "replay" if replay_text is not None else "api"

    # pass 0: take model output piece by piece + tier B validation; rejected => retry that piece
    # (up to 2 times), still rejected -> failed/ + count
    for i, (a, b) in enumerate(pieces, 1):
        view = {"source_chunk_id": chunk["source_chunk_id"],
                "time_ref": chunk.get("time_ref"), "text": text[a:b]}
        p = build_prompt(view, part=(i, len(pieces)) if len(pieces) > 1 else None, rules=rules)
        prompts.append(p)
        reason = ""
        last_raw = ""
        ok = False
        for attempt in range(1, max_try + 1):
            if attempt > 1:
                retries += 1
            if replay_text is None:
                repair = None if attempt == 1 else {"reason": reason, "attempt": attempt}
                resp = deepseek_api.call(p, repair=repair, disable_thinking=True)
                metas.append({k: resp.get(k) for k in
                              ("model", "endpoint", "usage", "elapsed_ms", "http_status",
                               "finish_reason")})
                piece_text = resp["text"]
            else:
                metas.append({"model": "(replay)", "elapsed_ms": 0,
                              "finish_reason": "(replay)", "attempt": attempt})
                piece_text = pick_replay(replay_text, i, attempt)
            last_raw = piece_text
            try:
                doc = parse_entities_text(piece_text)
                wtmp = []
                dropped += validate_piece(doc, chunk, (a, b), rules, wtmp, piece=i,
                                          unlocated=unlocated)
                # do one quote substring check per piece first — a fail **triggers one retry**
                # (through the same PieceRejected path below); still failing after the retry
                # degrades and drops rows (see enforce_quote_substring).
                qviol = quote_substring_violations(doc, rules, text)
                if qviol and attempt < max_try:
                    raise PieceRejected(
                        "quote 不是 source_chunk 的子串 %d 处（task-107 二：引文必须是原文剪贴，"
                        "翻译/改写不算）：%s"
                        % (len(qviol),
                           "；".join("%s.%s=%r" % (v["table"], v["field"], v["value"][:30])
                                    for v in qviol[:3])))
                if qviol:
                    quote_retried_pieces.append(i)
                warnings.extend(wtmp)
                accepted.append({"piece": i, "scope": (a, b), "raw": doc})
                ok = True
                break
            except PieceRejected as exc:
                reason = str(exc)
        if not ok:
            path = save_failed_piece(chunk, i, max_try, reason, last_raw)
            failed.append({"piece": i, "attempts": max_try, "reason": reason, "path": path})
            warn_note(warnings, "piece_rejected",
                      "第 %d/%d 片重试 %d 次仍被拒，原文已落 failed/：%s"
                      % (i, len(pieces), max_retries(), reason))

    if not accepted:
        reasons = "；".join(str(f.get("reason") or "未知") for f in failed)
        raise ExtractError("所有片都被拒（%s），无可用结果（原文已落 failed/，不是静默丢弃）"
                           % (reasons or "未知"))

    # pass 1: card table (event_id recomputed by offset, independent of the three tables)
    cards_key = rules.cards_key
    if not cards_key:
        raise ExtractError("schema 错：没有 file_key=cards 的实体（卡片表是外键归属的根）")
    rest_keys = [fk for fk in rules.order if fk != cards_key]
    base_iso, base_source, base_granularity = chunk_time_base(chunk)
    norm_chunk = {"source_chunk_id": chunk["source_chunk_id"], "time_ref": chunk.get("time_ref"),
                  "text": text, "_scope": None, "_base_iso": base_iso,
                  "_base_source": base_source, "_base_granularity": base_granularity}
    ctx = {"rules": rules, "table": cards_key, "scope": None, "fk": None, "where": None,
           "piece": None, "hint": None, "trace_hits": []}
    cards_by_piece = []
    # the sequence is a derived value — the program sorts cards by extraction position
    # ascending then numbers them 1..n (the LLM does not write the number). Sequences are
    # per-piece scoped: one batch of cards in one model output gets one numbering, each piece
    # starts from 1.
    seq_maps = {}
    reorder_pieces = []
    card_pos_field = rules.pos_field.get(cards_key)
    for item in accepted:
        norm_chunk["_scope"] = item["scope"]
        ctx["scope"] = item["scope"]
        raw = item["raw"]
        cards_raw = raw.get(cards_key) or []
        if not isinstance(cards_raw, list):
            raise ExtractError("第 %d 片：%s 必须是数组" % (item["piece"], cards_key))
        rows = []
        for j, row in enumerate(cards_raw, 1):
            if not isinstance(row, dict):
                raise ExtractError("第 %d 片 %s[#%d]: 不是对象" % (item["piece"], cards_key, j))
            rows.append(normalize_row(cards_key, dict(row), norm_chunk,
                                      rules.props[cards_key], warnings, j, ctx,
                                      piece=item["piece"]))
        # sort by extraction position ascending (no-offset rows last), and record whether the
        # LLM's original order was already ascending
        before = [r.get(card_pos_field) for r in rows]
        rows.sort(key=lambda r: (not isinstance(r.get(card_pos_field), int),
                                 r.get(card_pos_field) if isinstance(r.get(card_pos_field), int) else 0))
        after = [r.get(card_pos_field) for r in rows]
        if before != after:
            reorder_pieces.append(item["piece"])
        seq_maps[item["piece"]] = {i: r.get("event_id") for i, r in enumerate(rows, 1)}
        cards_by_piece.append(rows)
    cards = merge_rows(cards_by_piece, unique_key_of(rules.entities[cards_key]),
                       warnings, cards_key)
    if reorder_pieces:
        warn_note(warnings, "fk_card_reorder",
                  "卡片输出顺序不是按 抽取位置 升序（片 %s）：程序已重排后编号，"
                  "fk.序号 仍按重排后的顺序 1..n（reorder=true）"
                  % ",".join(str(p) for p in reorder_pieces))

    has_rest = any(bool(item["raw"].get(k)) for item in accepted for k in rest_keys)
    # R14 facts independence: with cards empty, nullable tables (facts/identities) rows can be
    # written independently (event_id=null); only non-nullable tables (metrics) must still
    # attach to an event.
    has_non_nullable_rest = any(
        bool(item["raw"].get(k)) for item in accepted
        for k in rest_keys if k not in rules.mention_tables)
    if not cards:
        failed_reasons = "；".join(str(f.get("reason") or "") for f in failed)
        if has_non_nullable_rest:
            raise ExtractError("该 chunk 一张卡片都没有：非可选表行无法归属事件"
                               "（metrics 仍必须挂在事件下）%s"
                               % (("；被拒片原因：" + failed_reasons) if failed_reasons else ""))
        if not has_rest:
            raise ExtractError("cards 为空：一个事件都没抽出来%s"
                               % (("；被拒片原因：" + failed_reasons) if failed_reasons else ""))

    card_pos = card_pos_field
    fk = FkResolver(card_spans(cards, len(text), card_pos, rules.quote_key.get(cards_key),
                               rules.quote_text_key.get(cards_key)),
                    len(text), card_pos, seq_maps=seq_maps, reorder=bool(reorder_pieces),
                    tables=rest_keys, mention_tables=rules.mention_tables)

    # pass 2: the remaining tables (foreign key attributed by the program by offset; tier
    # behavior likewise from rules)
    rows_by_piece = {fk_: [] for fk_ in rest_keys}
    for item in accepted:
        norm_chunk["_scope"] = item["scope"]
        ctx["scope"] = item["scope"]
        ctx["fk"] = fk
        raw = item["raw"]
        for fk_ in rest_keys:
            ctx["table"] = fk_
            rows_by_piece[fk_].append(
                [normalize_row(fk_, dict(r), norm_chunk, rules.props[fk_], warnings, j, ctx,
                               piece=item["piece"])
                 for j, r in enumerate(raw.get(fk_) or [], 1) if isinstance(r, dict)])

    entities = {cards_key: cards}
    for fk_ in rest_keys:
        entities[fk_] = merge_rows(rows_by_piece[fk_], unique_key_of(rules.entities[fk_]),
                                   warnings, fk_)
    metrics_key = next((fk_ for fk_ in rest_keys if fk_ == "metrics"), None)
    if metrics_key and not entities[metrics_key]:
        warn_note(warnings, "metrics_empty",
                  "metrics 为空：本 chunk 没有可落数值表的值（契约不强制非空，但请确认）")

    fk_sum = fk.summary()
    # out-of-range sequences report red separately (not a silent fallback); "wrong" and
    # "missing" must be counted separately
    if fk_sum["hint_invalid"]:
        bad = fk_sum["hint_invalid_rows"]
        shown = "；".join("%s 给的序号=%r" % (r.get("where"), r.get("hint")) for r in bad[:10])
        more = ("；…共 %d 行，明细见 fk_method.hint_invalid_rows" % len(bad)) if len(bad) > 10 else ""
        warn_note(warnings, "fk_hint_invalid",
                  "hint_invalid：%d/%d 行「所属事件序号」越界（不在 1..n 内），已回落 contain/nearest，"
                  "但**不算没给**——给对 %d 行、给错 %d 行、没给 %d 行。"
                  "若 explicit 上来了但 hint_invalid 也高，说明 LLM 数事件个数吃力（n 太大），"
                  "回来查 chunks/卡片切分，不是加 prompt。越界明细：%s%s"
                  % (fk_sum["hint_invalid"], fk_sum["rows"], fk_sum["hint_ok"],
                     fk_sum["hint_invalid"], fk_sum["hint_missing"], shown, more))
    # criterion one: coverage per table (merging gives a "looks like a number, means nothing" ratio)
    low_tables = []
    for tname, tstat in (fk_sum.get("per_table") or {}).items():
        if tstat.get("kind") != "value" or not tstat.get("denominator"):
            continue   # facts/metrics all aggregate (denominator 0) or identities (mention coverage only)
        ratio = tstat.get("explicit_ratio")
        if ratio is None:      # empty denominator -> null, neither enters the passing denominator nor reports "low"
            continue
        if ratio < FK_EXPLICIT_RATIO_MIN:
            low_tables.append(tname)
    if low_tables:
        shown = "；".join(
            "%s: explicit %d / (rows %d − aggregate %d) = %d/%d = %.1f%%"
            % (t, (fk_sum["per_table"][t])["explicit"], (fk_sum["per_table"][t])["rows"],
               (fk_sum["per_table"][t])["aggregate"], (fk_sum["per_table"][t])["explicit"],
               (fk_sum["per_table"][t])["denominator"],
               (fk_sum["per_table"][t])["explicit_ratio"] * 100)
            for t in low_tables)
        warn_note(warnings, "fk_explicit_low",
                  "fk 归属 explicit 覆盖率低于 %d%%（分表算，task-70 ③）：%s。"
                  "aggregate 行（派生汇总量）不进分母；identities（mention）不进 explicit 分母。"
                  "合并算的比例没意义，别用；达不到说明数据汇总段没和事件卡对齐，"
                  "需要单独处理（回来查 prompt 或汇总段切分）。"
                  % (FK_EXPLICIT_RATIO_MIN * 100, shown))
    # empty-denominator tables are counted separately (ratio is null, not 100%, and they do not
    # enter the passing denominator)
    if fk_sum["vacuous_tables"]:
        shown_v = "；".join(
            "%s（%s，rows %d，aggregate %d）"
            % (t, (fk_sum["per_table"][t]).get("kind"), (fk_sum["per_table"][t]).get("rows", 0),
               (fk_sum["per_table"][t]).get("aggregate", 0))
            for t in fk_sum["vacuous_tables"])
        warn_note(warnings, "fk_vacuous_tables",
                  "空分母表 %d 张（task-71 ①）：%s —— 比值记 null/N/A，不是 100%%，"
                  "也不进总体达标分母（达标只看有分母的表：%s）。"
                  "一张全是汇总值的 chunk 不会再显示「explicit 100%%」。"
                  % (fk_sum["vacuous_table_count"], shown_v,
                     ("、".join(fk_sum["scored_tables"]) if fk_sum["scored_tables"] else "无")))
    # aggregate / mention reported separately (they are not "low-confidence nearest", they are
    # two different things)
    if fk_sum["aggregate"] or fk_sum["mention"]:
        warn_note(warnings, "fk_aggregate_mention",
                  "非 explicit 行已拆两类（task-70 ①）：aggregate %d 行（metrics/facts 派生汇总量，"
                  "无单一源事件，指向该 chunk 汇总段）、mention %d 行（identities 名称提及，"
                  "锚点 source_chunk_id+抽取位置+quote，event_id 可选不硬造）。"
                  "跨表 join 只用 explicit/contain 行作答；分表覆盖率见 fk.per_table。"
                  % (fk_sum["aggregate"], fk_sum["mention"]))
    # identities' mention coverage seen separately (has a quote and is locatable counts)
    ident = (fk_sum.get("per_table") or {}).get("identities") or {}
    if (ident.get("rows") and ident.get("mention_coverage") is not None
            and ident.get("mention_coverage") < 1.0):
        warn_note(warnings, "fk_mention_coverage",
                  "identities mention 覆盖率 %d/%d=%.1f%%（有 quote 且能定位才算）："
                  "缺 quote/偏移的标识行只能靠 chunk 粗定位，回来查标识行的 quote 抽取。"
                  % (ident.get("locatable", 0), ident.get("rows", 0),
                     (ident.get("mention_coverage") or 0) * 100))
    # out-of-range sequences report red separately (not a silent fallback); "wrong" and
    # "missing" must be counted separately
    if fk_sum["hint_invalid"]:
        bad = fk_sum["hint_invalid_rows"]
        shown = "；".join("%s 给的序号=%r" % (r.get("where"), r.get("hint")) for r in bad[:10])
        more = ("；…共 %d 行，明细见 fk_method.hint_invalid_rows" % len(bad)) if len(bad) > 10 else ""
        warn_note(warnings, "fk_hint_invalid",
                  "hint_invalid：%d/%d 行「所属事件序号」越界（不在 1..n 内），已回落 contain/aggregate/mention，"
                  "但**不算没给**——给对 %d 行、给错 %d 行、没给 %d 行。"
                  "若 explicit 上来了但 hint_invalid 也高，说明 LLM 数事件个数吃力（n 太大），"
                  "回来查 chunks/卡片切分，不是加 prompt。越界明细：%s%s"
                  % (fk_sum["hint_invalid"], fk_sum["rows"], fk_sum["hint_ok"],
                     fk_sum["hint_invalid"], fk_sum["hint_missing"], shown, more))

    # runtime substring check (every string sub-field of quote must be a contiguous source
    # substring). Runs before the content/ratio gates: dropping rows changes row counts, so
    # degrade first then count, otherwise the ratio would be inflated by fake quotes.
    quote_sub = enforce_quote_substring(entities, rules, text, warnings,
                                        retried=bool(quote_retried_pieces))
    # two content hard checks (precisely decidable -> gate). Runs after normalization, before
    # counting: dropping rows affects four-table row counts / quote ratio, so must run before
    # monitor/quote gate.
    content_gates = apply_content_gates(entities, rules, warnings)
    # generic-term-not-naming shape gate (.bak / pure suffix word) — warn only, no drop, after the content gate
    generic_gate = apply_identity_generic_gate(entities, rules, warnings)
    # foreign-key invariant (R12) — write when computable, not write when not, no dangling/placeholder
    fk_invariants = enforce_fk_invariants(entities, rules)
    # time_norm recomputation hard gate (same style as event_id recomputation). Runs after
    # normalization/completion, checks "the value about to be written"; mismatch reports red —
    # a wrong time column is a wrong key, not just a warning.
    time_norm_gate = enforce_time_norm_reproducible(entities, rules, norm_chunk, warnings)
    # precision self-consistency gate (day/minute/unknown mismatching time_norm = red)
    precision_gate = enforce_time_precision_consistent(entities, rules, warnings)
    # end-to-end assertions (from question to answer) — every new field must ship with one;
    # incomplete coverage = schema error.
    e2e_asserts = load_e2e_asserts(os.environ.get("EXTRACT_E2E_ASSERTS") or None)
    e2e_coverage = check_e2e_field_coverage(e2e_asserts)
    e2e_report = run_e2e_asserts(e2e_asserts, scope=[{
        "path": "(current chunk)",
        "source_chunk_id": chunk["source_chunk_id"],
        "entities": entities}], chunk=chunk)
    # two traceability assertions (verbatim self-check + derivation self-check) — every new
    # field goes through.
    traceability = traceability_assert(entities, rules, norm_chunk,
                                       trace_hits=ctx.get("trace_hits"), warnings=warnings)
    # span alarm — in-chunk span > 24h and containing relative time => chunk-level base unreliable
    span_alarm = span_alarm_of(entities, rules, norm_chunk, warnings=warnings)

    # density inspection (not a gate; per-chunk table row counts + identities per thousand chars)
    # cost-side paired field calls_per_1k_chars = pieces / chars × 1000
    density = density_stats(entities, len(text), content_gates.get("identity_table"),
                            pieces=len(pieces))

    # coverage-style assertion (missed within list = red, beyond list = warning). Runs before
    # counting, so the warning text / monitor both show "which extras were over-extracted".
    coverage = None
    if expected is not None:
        coverage = expected_coverage(entities, expected)
        for table in EXPECTED_TABLES:
            info = coverage["tables"][table]
            if info["extra"]:
                warn_note(warnings, "expected_extra",
                          "%s 有清单外的行 %d 条（task-77 ② 只 warning 不红）：%s"
                          % (table, len(info["extra"]),
                             "；".join(info["extra"][:5])))

    # the two monitoring items: warning rate <5%, reject rate <10%; over threshold says
    # explicitly to come back and check the prompt or the split
    # each item also reports (numerator, denominator); denominator=0 => unverified, not passing
    counts = {k: len(v) for k, v in entities.items()}
    rows_total = sum(counts.values())
    warn_rows = len({(w.get("piece"), w.get("where")) for w in warnings if w.get("where")})
    warning_rate = round(warn_rows / rows_total, 4) if rows_total else None
    pieces_total = len(pieces)
    reject_rate = round(len(failed) / pieces_total, 4) if pieces_total else None
    monitor = {
        "warning_rows": warn_rows, "rows_total": rows_total,
        "warning_rate_num": warn_rows, "warning_rate_den": rows_total,
        "warning_rate": warning_rate, "warning_rate_max": WARNING_RATE_MAX,
        "warning_rate_ok": gate_verdict(rows_total, warning_rate, WARNING_RATE_MAX) == "pass",
        "warning_rate_verdict": gate_verdict(rows_total, warning_rate, WARNING_RATE_MAX),
        "pieces_failed": len(failed), "pieces_total": pieces_total,
        "reject_rate_num": len(failed), "reject_rate_den": pieces_total,
        "reject_rate": reject_rate, "reject_rate_max": REJECT_RATE_MAX,
        "reject_rate_ok": gate_verdict(pieces_total, reject_rate, REJECT_RATE_MAX) == "pass",
        "reject_rate_verdict": gate_verdict(pieces_total, reject_rate, REJECT_RATE_MAX),
        "unverified_gates": [k for k in ("warning_rate", "reject_rate")
                             if gate_verdict(rows_total if k == "warning_rate" else pieces_total,
                                             warning_rate if k == "warning_rate" else reject_rate,
                                             WARNING_RATE_MAX if k == "warning_rate" else REJECT_RATE_MAX)
                             == "unverified"],
        "retries": retries, "retry_max": max_retries(), "dropped_rows": dropped,
        "failed": failed,
        "content_gates": content_gates,   # red or not, how many rows dropped (a gate, not inspection)
        "identity_generic": generic_gate,  # generic term not naming anything (.bak / pure suffix word)
        "fk_invariants": fk_invariants,    # foreign-key invariant (R12 optional field)
        "time_norm_gate": time_norm_gate,  # time column recomputable (mismatch=red, not a warning)
        "precision_gate": precision_gate,  # precision self-consistent with time_norm (mismatch=red)
        # quote substring check (degradation=red, visible); here first put gate_raw, the function
        # end replaces it with {gate_raw, gate_final} (criterion only looks at gate_final).
        "quote_substring_gate": quote_sub,
        "e2e": {"coverage": e2e_coverage, "report": e2e_report},  # end-to-end assertions
        "traceability_gate": traceability,  # verbatim self-check + derivation self-check
        "span_alarm": span_alarm,          # chunk-level base reliability
        "time_base": {"source": base_source, "granularity": base_granularity,
                      "base": base_iso},
        "density": density,               # inspection only, not a gate
        "expected_coverage": coverage,    # coverage-style assertion (missed=red / extra=warning)
    }
    if content_gates["red"]:
        warn_note(warnings, "content_gate_red",
                  "内容闸报红（task-66 ①②）：枚举值当标识名丢 %d 行、名称==类型丢 %d 行、"
                  "同串交叉 %d 处 —— 明细见 monitor.content_gates"
                  % (content_gates["enum_value_dropped"],
                     content_gates["name_equals_type_dropped"],
                     content_gates["cross_same_string"]))
    if generic_gate["suspected"]:
        warn_note(warnings, "identity_generic_gate",
                  "标识通称不具名 %d 行（task-70 ④）：完整原文写法 是纯扩展名/纯后缀词"
                  "（.bak、备份 这类），join 时会把不同东西串成一个 —— 明细见 "
                  "monitor.identity_generic（只报警，不丢行）" % generic_gate["suspected"])
    if monitor["warning_rate_verdict"] == "fail":
        warn_note(warnings, "warning_rate_high",
                  "warning 率 %d/%d=%.2f%% 超过 %.1f%% 阈值：回来查 prompt 或拆片"
                  % (warn_rows, rows_total, (warning_rate or 0) * 100, WARNING_RATE_MAX * 100))
    elif monitor["warning_rate_verdict"] == "unverified":
        # denominator=0 is not "passing", it is "unverified" — must be said, no silent green
        warn_note(warnings, "gate_unverified",
                  "warning 率闸未检验（分母=0：没有可统计的行）——task-104 五：分母=0 一律不算通过")
    if monitor["reject_rate_verdict"] == "fail":
        warn_note(warnings, "reject_rate_high",
                  "拒片率 %d/%d=%.2f%% 超过 %.1f%% 阈值：回来查 prompt 或拆片"
                  % (len(failed), pieces_total, (reject_rate or 0) * 100, REJECT_RATE_MAX * 100))
    elif monitor["reject_rate_verdict"] == "unverified":
        warn_note(warnings, "gate_unverified",
                  "拒片率闸未检验（分母=0：没有片）——task-104 五：分母=0 一律不算通过")

    # missed within the list = red (not "missing a little") — written as a chunk-level warning;
    # main() decides the exit code (2), so the output still lands and the trail shows which ones were missed.
    if coverage is not None and not coverage["ok"]:
        miss = []
        for table in EXPECTED_TABLES:
            for k in coverage["tables"][table]["missing"]:
                miss.append("%s:%s" % (table, k))
        warn_note(warnings, "expected_missing",
                  "覆盖式断言报红（task-77 ②）：人工清单内漏抽 %d 条 —— %s"
                  % (coverage["missing_total"], "；".join(miss[:8])))

    # quote ratio gate (denominator A): missing-quote rows removed from denominator, gap listed separately, not a whole-run failure
    quote_gate = enforce_quote_ratio(entities, schema, warnings)
    if quote_gate["quote_missing"]:
        warn_note(warnings, "rows_quote_missing",
                  "缺引文行 %d/%d（task-74）：已从 quote 闸分母剔除、逐行列在 quote_missing_rows，"
                  "不整份作废——缺的是引文，不是事实（引文缺口 %.2f%%，覆盖率 %.2f%%）"
                  % (quote_gate["quote_missing"], quote_gate["rows"],
                     quote_gate["gap_ratio"] * 100, quote_gate["ratio"] * 100))

    # nearest rows marked low confidence, do not answer — strip the temporary marker after
    # counting (the final output leaves no trace)
    answer = answer_stats(entities)
    fk_sum["answer"] = answer
    # answer_rows judged alongside explicit_ratio — coverage passing != answerable. Cross-table
    # join requires "the relevant event and data row are both in answer"; aggregate/mention do
    # not enter the answer, so a 100%-coverage chunk may still have only single-digit joinable
    # rows.
    if answer["answer_ok"] is False:
        warn_note(warnings, "fk_answer_rows_low",
                  "能答题的行太少（task-71 ②，与 explicit_ratio 并列的判据）："
                  "answer_rows %d / 三表 rows %d = %s（下限：answer_rows >= %d 且比例 >= %s）。"
                  "answer 只算 explicit/contain 的行（aggregate/mention 不进答案），"
                  "所以「覆盖率达标」不代表能答题——跨表 join 题要求相关事件与数据行都在 answer 里。"
                  "占到这么少的行，先回来查汇总段/标识行是不是吃掉了大部分行。"
                  % (answer["answer_rows"], answer["rows"],
                     ("%.1f%%" % (answer["answer_ratio"] * 100))
                     if answer["answer_ratio"] is not None else "N/A",
                     FK_ANSWER_ROWS_MIN, "%.0f%%" % (FK_ANSWER_RATIO_MIN * 100)))
    for rows in entities.values():
        for row in rows:
            if isinstance(row, dict):
                row.pop("__fk_method", None)

    # rows without an anchor do not silently vanish (list into result + chunk-level hint)
    if unlocated:
        warn_note(warnings, "rows_unlocated",
                  "%d 行既无 抽取位置、无 quote，也没有能回原文的文字锚点（task-73 ①）："
                  "行没静默丢，清单见 unlocated_rows；缺的是引文，不是事实" % len(unlocated))
    monitor["unlocated_rows"] = len(unlocated)
    # the quote gap is listed separately (kept apart from coverage, not merged into one number)
    monitor["quote_missing_rows"] = len(quote_gate.get("quote_missing_rows") or [])
    monitor["quote_gap_ratio"] = quote_gate.get("gap_ratio")

    # the gate outputs two copies — the quote_sub above is gate_raw (pre-scrub, including the
    # dropped quote.归一化 type); here a recompute on the **final output** = gate_final, the
    # only criterion. The recompute must come after all row/field drops and degradations (i.e.
    # entities is already in written state), otherwise it is back to "running pre-scrub".
    quote_final = quote_substring_report(entities, rules, text)
    quote_gates = quote_substring_gates(quote_sub, quote_final)
    monitor["quote_substring_gate"] = quote_gates
    if quote_final["red"]:
        warn_note(warnings, "quote_gate_final_red",
                  "gate_final 报红（task-108）：最终产物里仍有 %d 处 quote 不是原文子串"
                  "（gate_raw invalid=%d，清洗没做干净）——明细见 "
                  "monitor.quote_substring_gate.gate_final"
                  % (quote_final["invalid"], quote_sub.get("invalid", 0)))

    # all gates close out uniformly — counters always int, None landed to 0 + unverified, every
    # gate gets report_line. Without --expected the coverage assertion is "uncounted", written
    # as unverified rather than None.
    quote_gate = normalize_gate("quote_gate", quote_gate)
    if coverage is None:
        coverage = {"unverified": True, "verdict": "unverified",
                    "tables": {}, "missing_total": 0, "extra_total": 0,
                    "note": "未给 --expected 清单：覆盖断言没数 => 计数器 0 + unverified=True"
                            "（task-109：不许用 None 冒充 0）"}
    coverage = normalize_gate("expected_coverage", coverage)
    monitor["quote_gate"] = quote_gate
    monitor["expected_coverage"] = coverage
    monitor = finalize_gates(monitor)

    prompt_sha = hashlib.sha256(prompts[0].encode("utf-8")).hexdigest()
    return {"entities": entities,
            "warnings": [w["msg"] for w in warnings],
            "warnings_detail": warnings,
            "meta": metas[0] if metas else {},
            "attempts_meta": metas,
            "prompt_sha256": prompt_sha, "prompt_len": len(prompts[0]),
            "prompt_sha256s": [hashlib.sha256(p.encode("utf-8")).hexdigest() for p in prompts],
            "source": source, "pieces": len(pieces),
            "max_chunk_chars": limit, "split_on": split_on_of(text, pieces),
            "piece_spans": [{"start": a, "end": b, "chars": b - a} for a, b in pieces],
            "fk": fk_sum, "counts": counts,
            "monitor": monitor, "quote_gate": quote_gate,
            "content_gates": content_gates, "identity_generic": generic_gate,
            "fk_invariants": fk_invariants, "density": density,
            "time_norm_gate": time_norm_gate,
            "precision_gate": precision_gate,
            "quote_substring_gate": quote_gates,
            "e2e": {"coverage": e2e_coverage, "report": e2e_report},
            "traceability_gate": traceability,
            "span_alarm": span_alarm,
            "time_base": {"source": base_source, "granularity": base_granularity,
                          "base": base_iso},
            "rules": {"path": rules.path, "tables": rules.order,
                      "constraints": rules.report},
            "dropped_rows": dropped, "retries": retries, "failed_pieces": failed,
            "unlocated_rows": unlocated,
            "expected_coverage": coverage}


def append_build_log(entry, path=None):
    """fk_method must go into the build log (a high nearest share must not happen silently).

    The log directory is set by --log-dir (default still out/build_log.jsonl); the entry
    carries out / log / repeat, so the log location is no longer an oral convention.
    """
    target = pathlib.Path(path) if path else BUILD_LOG
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # a failed log write must not swallow the extraction result


def build_log_entry(chunk, result, out_path, log_path, repeat, stability=None):
    """The log entry carries its own landing location (out / log / repeat), easy to verify
    "where the log is"."""
    entry = {"chunk": chunk["source_chunk_id"], "source": result["source"],
             "pieces": result["pieces"],
             # once a parameter changes (6000 -> 1200), the log must explain why prompt sha changed
             "max_chunk_chars": result.get("max_chunk_chars"),
             "split_on": result.get("split_on"),
             "chars": len(chunk["text"]),
             "counts": result["counts"],
             "density": result["density"],
             "content_gates": result["content_gates"],
             "identity_generic": result["identity_generic"],
             "fk_invariants": result["fk_invariants"],
             "out": str(out_path), "log": str(log_path), "repeat": repeat,
             "unlocated_rows": len(result.get("unlocated_rows") or []),
             "quote_missing_rows": len((result.get("quote_gate") or {})
                                       .get("quote_missing_rows") or []),
             "fk_method": {k: result["fk"][k] for k in
                           ("rows", "explicit", "contain", "aggregate", "mention",
                            "nearest", "per_table", "coverage",
                            "vacuous_tables", "vacuous_table_count", "scored_tables",
                            "no_offset",
                            "low_confidence", "answer",
                            "hint_given", "hint_ok", "hint_invalid", "hint_missing",
                            "hint_invalid_ratio", "hint_invalid_rows", "reorder",
                            "detail", "aggregate_rows", "mention_rows", "nearest_rows")},
             "fk_detail": result["fk"]["detail"],
             "time_norm_gate": {k: (result.get("time_norm_gate") or {}).get(k) for k in
                                ("rows_checked", "reproduced", "mismatch",
                                 "unreproducible", "null_rows", "underivable_nulled",
                                 "den", "num", "verdict", "red", "ok")},
             "traceability_gate": {
                 "verbatim": {k: (result.get("traceability_gate") or {}).get("verbatim", {}).get(k)
                              for k in ("fields", "violations", "ok")},
                 "recompute": {k: (result.get("traceability_gate") or {}).get("recompute", {}).get(k)
                               for k in ("rows_checked", "reproduced", "mismatch",
                                         "unreproducible")},
                 "red": (result.get("traceability_gate") or {}).get("red")},
             "span_alarm": {k: (result.get("span_alarm") or {}).get(k) for k in
                            ("chunk", "span_hours", "relative_rows", "alarm")},
             "time_base": result.get("time_base"),
             "monitor": {k: result["monitor"][k] for k in
                         ("warning_rows", "rows_total", "warning_rate",
                          "warning_rate_num", "warning_rate_den", "warning_rate_verdict",
                          "warning_rate_ok", "pieces_failed", "pieces_total",
                          "reject_rate", "reject_rate_num", "reject_rate_den",
                          "reject_rate_verdict", "reject_rate_ok", "unverified_gates",
                          "retries", "dropped_rows")},
             "failed_pieces": [{"piece": f["piece"], "reason": f["reason"],
                                "path": f["path"]} for f in result["failed_pieces"]],
             "quote_gate": result["quote_gate"],
             "expected_coverage": result.get("expected_coverage"),
             "prompt_sha256": result["prompt_sha256"]}
    if stability is not None:
        entry["stability"] = stability
    return entry


def main(argv=None):
    ap = argparse.ArgumentParser(description="extract: chunk 原文 -> 4 实体 JSON（schema v6）")
    ap.add_argument("--chunk", help="chunk JSON：{source_chunk_id,time_ref,text}")
    ap.add_argument("--out", default=str(HERE / "out" / "entities.json"))
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    ap.add_argument("--rules", default=None,
                    help="extract_rules.json（默认取 schema 同目录；task-65 表驱动）")
    ap.add_argument("--replay", help="用文件里的模型响应文本代替真调（离线自测）")
    ap.add_argument("--dump-prompt", help="把最终 prompt 写到该文件")
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt 摘要，不调模型")
    ap.add_argument("--density-scan", default=None,
                    help="task-66 ③：只体检，不抽卡——读 JSONL（build_log 条目 {chunk,chars,counts}），"
                         "列出标识密度前 5%% 的 chunk")
    ap.add_argument("--density-top", type=float, default=0.05,
                    help="--density-scan 的异常分位（默认前 5%%）")
    ap.add_argument("--repeat", type=int, default=1,
                    help="task-72 ①：同一 chunk 重复跑 N 次（默认 1；建议 3），报中位数+区间，"
                         "区间半宽 > ±10%% 的指标标 unstable（只当体检）")
    ap.add_argument("--log-dir", default=None,
                    help="task-72 ②：build_log.jsonl 落到该目录（默认 out/）；"
                         "要与 --out 同一处可查就指到同一目录")
    ap.add_argument("--answer-index", default=None,
                    help="task-72 ③：把每行作答方法/event_id 落到该文件（成对覆盖判据的输入）")
    ap.add_argument("--pair-check", default=None,
                    help="task-72 ③：成对覆盖题单（JSON 数组）；配合 --answer-index 逐题验"
                         "「事件 + 它的数据行」是否同时在 answer 里；全成对 exit 0，否则 1")
    ap.add_argument("--expected", default=None,
                    help="task-77 ②：人工期望清单（expected.json）。清单内漏抽 = 红（exit 2）"
                         "、清单外 = warning；清单是人读原文数的，不是抄产物")
    args = ap.parse_args(argv)

    if args.pair_check:
        if not args.answer_index:
            ap.error("--pair-check 需要 --answer-index（由抽取运行时落盘）")
        try:
            index = json.loads(pathlib.Path(args.answer_index).read_text(encoding="utf-8"))
            report = pair_check(index, load_questions(args.pair_check))
        except ExtractError as exc:
            print("PAIR CHECK FAILED: %s" % exc, file=sys.stderr)
            return 2
        except (OSError, ValueError) as exc:
            print("PAIR CHECK FAILED: %s" % exc, file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["all_pair_ok"] else 1

    try:
        schema = load_json(args.schema, "schema")
        if args.density_scan:
            report = density_scan(args.density_scan, args.density_top,
                                  load_rules(schema, args.rules))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["scanned"] else 1
        if not args.chunk:
            ap.error("--chunk 必填（或用 --density-scan 只体检）")
        chunk = load_chunk(args.chunk)
        rules = load_rules(schema, args.rules)
        expected = load_expected(args.expected) if args.expected else None
        pieces = split_chunk(chunk["text"], max_chunk_chars())
        first_view = {"source_chunk_id": chunk["source_chunk_id"],
                      "time_ref": chunk.get("time_ref"),
                      "text": chunk["text"][pieces[0][0]:pieces[0][1]]}
        prompt = build_prompt(first_view, part=(1, len(pieces)) if len(pieces) > 1 else None,
                              rules=rules)
        if args.dump_prompt:
            pathlib.Path(args.dump_prompt).write_text(prompt, encoding="utf-8")
        if args.dry_run:
            print(json.dumps({"prompt_len": len(prompt),
                              "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                              "chunk_chars": len(chunk["text"]),
                              "pieces": len(pieces),
                              "max_chunk_chars": max_chunk_chars(),
                              "split_on": split_on_of(chunk["text"], pieces),
                              "rules": {"path": rules.path, "consistent": rules.report},
                              "piece_chars": [b - a for a, b in pieces]}, ensure_ascii=False))
            return 0
        repeats = max(1, int(args.repeat or 1))
        # repeat the same chunk (temperature fixed 0, deepseek_api.call's default)
        replay_docs = load_replays(args.replay, repeats) if args.replay else None
        results = []
        for k in range(repeats):
            results.append(extract(chunk, schema,
                                   replay_text=(replay_docs[k] if replay_docs else None),
                                   prompt=prompt, rules=rules, expected=expected))
        result = results[0]
        stability = (stability_report([stability_point(r) for r in results])
                     if repeats > 1 else None)
    except ExtractError as exc:
        print("EXTRACT FAILED: %s" % exc, file=sys.stderr)
        return 1
    except deepseek_api.DeepSeekAPITruncated as exc:
        print("EXTRACT FAILED (truncated): %s" % exc, file=sys.stderr)
        return 1
    except deepseek_api.DeepSeekAPIUnavailable as exc:
        print("EXTRACT FAILED (api): %s" % exc, file=sys.stderr)
        return 1

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result["entities"], ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    # log lands in --log-dir (same directory as --out = "one place to check"); entry carries out/log/repeat
    log_path = (pathlib.Path(args.log_dir) / "build_log.jsonl") if args.log_dir else BUILD_LOG
    for k, res in enumerate(results):
        append_build_log(build_log_entry(chunk, res, out_path, log_path, k + 1,
                                         stability if k == 0 else None), log_path)
    # write the per-row answer index, for --pair-check to verify pairwise coverage per question
    ai_path = pathlib.Path(args.answer_index) if args.answer_index else None
    if ai_path is not None:
        ai_path.parent.mkdir(parents=True, exist_ok=True)
        ai_path.write_text(json.dumps(build_answer_index(chunk, result),
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out_path), "log": str(log_path),
                      "answer_index": (str(ai_path) if ai_path is not None else None),
                      "repeat": repeats, "source": result["source"],
                      "pieces": result["pieces"], "piece_spans": result["piece_spans"],
                      "max_chunk_chars": result["max_chunk_chars"],
                      "split_on": result["split_on"],
                      "counts": result["counts"], "warnings": result["warnings"],
                      "warnings_detail": result["warnings_detail"],
                      "fk": {k: result["fk"][k] for k in
                             ("rows", "explicit", "contain", "aggregate", "mention",
                              "nearest", "per_table", "coverage",
                              "vacuous_tables", "vacuous_table_count", "scored_tables",
                              "no_offset",
                              "low_confidence", "answer",
                              "hint_given", "hint_ok", "hint_invalid", "hint_missing",
                              "hint_invalid_ratio", "hint_invalid_rows", "reorder")},
                      "fk_detail": result["fk"]["detail"],
                      "time_norm_gate": result["time_norm_gate"],
                      "traceability_gate": result["traceability_gate"],
                      "span_alarm": result["span_alarm"],
                      "time_base": result["time_base"],
                      "monitor": result["monitor"],
                      "content_gates": result["content_gates"],
                      "identity_generic": result["identity_generic"],
                      "fk_invariants": result["fk_invariants"],
                      "density": result["density"],
                      "failed_pieces": result["failed_pieces"],
                      "unlocated_rows": result.get("unlocated_rows") or [],
                      "quote_gate": result["quote_gate"],
                      "quote_substring_gate": result.get("quote_substring_gate"),
                      "precision_gate": result.get("precision_gate"),
                      "e2e": result.get("e2e"),
                      "retries": result["retries"], "dropped_rows": result["dropped_rows"],
                      "prompt_sha256": result["prompt_sha256"],
                      "stability": stability,
                      "expected_coverage": result.get("expected_coverage"),
                      "meta": result["meta"]}, ensure_ascii=False, indent=2))
    # counters still None, or a gate without report_line = this cell is not finished (output
    # still written, exit code 2 — None would be treated as "no problem", so it must hard-fail).
    nones = find_none_counters(result)
    no_line = find_gates_missing_report_line(result)
    if nones or no_line:
        print("COUNTER POLICY FAILED: 计数器仍是 None %d 处%s；缺 report_line 的闸 %s —— "
              "task-109：计数器只能是 int（没数的写 0 + unverified=True，别用 None 冒充）"
              % (len(nones),
                 ("（%s）" % "；".join(nones[:6])) if nones else "",
                 "、".join(no_line) if no_line else "无"), file=sys.stderr)
        return 2
    cov = result.get("expected_coverage")
    # without --expected, cov is {"unverified": true, ...} (not None), so the criterion checks
    # ok is False, not `not cov.get("ok")` (which would treat "uncounted" as "failed").
    if cov and cov.get("ok") is False:
        miss = []
        for table in EXPECTED_TABLES:
            for k in cov["tables"][table]["missing"]:
                miss.append("%s:%s" % (table, k))
        print("EXPECTED COVERAGE FAILED: 人工清单内漏抽 %d 条：%s"
              % (cov["missing_total"], "；".join(miss[:8])), file=sys.stderr)
        return 2
    # time_norm recomputation mismatch/unparseable = red (exit code 2). Output already written;
    # the trail shows which row: the time column is the sole basis for "take the latest", a
    # recomputation mismatch equals a wrong key, not passable as a warning.
    tng = result.get("time_norm_gate") or {}
    trg = result.get("traceability_gate") or {}
    if tng.get("red") or trg.get("red"):
        details = []
        for r in (tng.get("mismatch_rows") or [])[:8]:
            details.append("%s 存 %s / 应为 %s（time_raw=%r）"
                           % (r["where"], r["stored"], r["recomputed"], r["time_raw"]))
        for r in (tng.get("unreproducible_rows") or [])[:8]:
            details.append("%s %s（time_raw=%r）" % (r["where"], r["why"], r["time_raw"]))
        print("TIME_NORM RECOMPUTE FAILED: 不符 %d 行、解不出 %d 行：%s"
              % (tng.get("mismatch", 0), tng.get("unreproducible", 0),
                 "；".join(details)), file=sys.stderr)
        return 2
    # precision self-inconsistency = red (exit code 2); end-to-end assertion answered wrong = red
    pg = result.get("precision_gate") or {}
    e2e = (result.get("e2e") or {}).get("report") or {}
    if pg.get("red") or (e2e.get("total") and not e2e.get("ok")):
        print("TASK-107 GATES FAILED: precision 不自洽 %d 行；e2e 断言 %d/%d 红：%s"
              % (pg.get("mismatch", 0), e2e.get("red", 0), e2e.get("total", 0),
                 "；".join("%s 存 %r 应为 %r" % (m["where"], m["stored"], m["expected"])
                          for m in (pg.get("rows") or [])[:5])),
              file=sys.stderr)
        return 2
    # the criterion only looks at gate_final — a non-source quote remaining in the final output
    # = red (exit code 2). gate_raw red is not a failure (it is the diagnostic "the model gave
    # a rewrite this time, and it was scrubbed").
    qg = result.get("quote_substring_gate") or {}
    gfinal = qg.get("gate_final") or {}
    graw = qg.get("gate_raw") or {}
    if gfinal.get("red"):
        print("QUOTE GATE_FINAL FAILED: gate_final ok=%s red=%s invalid=%s"
              "（gate_raw ok=%s red=%s invalid=%s）——最终产物里仍有非原文 quote，清洗没做干净"
              % (gfinal.get("ok"), gfinal.get("red"), gfinal.get("invalid"),
                 graw.get("ok"), graw.get("red"), graw.get("invalid")),
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
