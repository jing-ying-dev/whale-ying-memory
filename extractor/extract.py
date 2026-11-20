# -*- coding: utf-8 -*-
"""extract.py — 抽取器（task-63）：把一个 chunk 原文抽成 4 实体 JSON。

    卡片 cards / 数值表 metrics / 标识表 identities / fact 等式句 facts
    字段一字对齐 schema v6（runs/61/schema.json == runs/60/schema.json 逐字节一致）。

task-63 三处（审查端定方案 C）：
  ① quote 从「尽量」改成硬规则：prompt 与守卫同口径（守卫是硬 ExtractError，
     prompt 说「尽量」会让失败率莫名变高）；给不出能命中的 quote 就别单独成行。
  ② 方案 C：三表**不填 event_id**，程序按偏移归属（contain / nearest），
     归属方法 fk_method 记进构建日志（out/build_log.jsonl）；nearest 占比高 = 卡片粒度太粗。
  ③ max_tokens 两道：deepseek_api 里 finish_reason=="length" 显式抛错；
     超长 chunk 按段落拆片、逐片独立抽，最后按 schema 唯一键合并去重。

task-64 四处（LLM 抽取鲁棒性三档 + 空字段 strip + 重试 + 监控；档位清单在 task-65 已移进 rules）：
  ⑤ 空字段 strip：on_missing=warn 的字段空串在必填校验之前 strip；
     必填字段是空串 = 报错（不 strip）；数值行的 值 是空串 = 整条丢。
  ⑥ 三档鲁棒性（判据：「这条数据缺了它，哪类题会答错？」）：
     A 程序能推的 -> 直接补、不记 warning（derive 指定的算法）；
     B 推不出且缺了该行没意义 -> 硬拒该片 + 重试该片（上限 2 次）：on_missing=reject 的字段
       缺失/空串、非法枚举、有偏移但无 quote（含无偏移无 quote）。
       重试 2 次仍不行 -> 原文+原因落 failed/ 并计数，不许静默丢弃。
     C 缺了不影响判分 -> 记 warning 容忍：on_missing=warn 的字段为空则 strip。
   两个监控项：warning 率 = 带 warning 的行 / 总行（<5%）；拒片率 = failed 片 / 总片（<10%）。
     超阈值不是报警了事：在 warnings 里明确写「回来查 prompt 或拆片」。
  四、quote 占比 gate 不因重试放宽：最终四表 quote 占比仍须 >= schema.gates.quote_ratio_min，
     重试只是让模型改正，不是豁免条件。

task-65 三处（P207 表驱动 + P208 R11 两降一留 + 一致性 check）：
  ① 表驱动：A/B/C 三档从「代码里三张手写清单」改成「schema 属性表驱动」。schema 旁边的
     extract_rules.json 按「表.字段」给每个属性两个属性：fill(model|derived) 谁产出、
     on_missing(fill|reject|warn) 缺了怎么办；程序遍历 schema.entities[*].properties，
     按属性决定行为，代码里不列字段名。新字段没声明 = schema 错（不给默认值，逼人表态）。
  ② R11 两降一留：metric.单位 与 card.target 去 required -> on_missing=warn（C 档）；
     identity.canonical_id 留 required -> on_missing=fill（A 档 derived：程序按「完整原文写法」
     规范化生成，模型不给）；quote.归一化 维持可选（schema 的 quote.required 只留 原文）。
     不填默认值（「不知道」不能伪装成「个」）。
  ③ 一致性 check（治根：required 与 on_missing 两份真相）：reject ⇒ 必须 required；
     warn ⇒ 必须非 required（task-64 的冲突正是这条）；fill ⇒ required 与否都行。
     三个红样例（warn 却 required / reject 却非 required / fill 正例）跑在 self_test_61.py。
  ④ task-66 内容噪声闸（P209）：
     ① 枚举值当标识名（名称 / 完整原文写法 ∈ 类型枚举值，或 名称==类型）-> 报红 + 丢行 + 计数；
     ② 同串交叉（同一 chunk 内 标识名 == 卡片 action/target）-> 报红（路由错位，不丢行）；
     ③ 密度监控只体检（每 chunk 各表行数 + 标识数/千字符，--density-scan 列前 5% chunk 给人看）；
     ④ 剩下的抽检见 spotcheck.py（每表 5 行、共 20 行，种子+判定留底，标识表错行率 <10% 才继续端到端）。

task-67 三处（P210 审查：类型枚举不全 / 纯标识符误判动作 / fk_method 丢粒度）：
  ① 类型枚举加「模型」（R12）：schema 与 extract_rules.json 同步；扩枚举 = 扩闸覆盖，
     检查逻辑不动（枚举值闸自动吃到 模型 这个值）。留痕「取代 P210」。
  ② 形态闸：卡片的 action / target 若匹配「纯标识符形态」（^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)*$ 或带 ()）
     且不含中文 -> 记 warning「疑似把标识当动作」。只报警不丢行（形态规则，不是黑名单）。
  ③ fk_method 补每行明细：每行 where + pos + method + 挂到哪张卡（card_pos/card_quote）
     + delta，全部落 build_log 的 fk_method.detail；nearest_ratio > 0.3 报警时把
     nearest 行的明细一并写进 warning。

task-68（P211 审查：显式归属为主、偏移兜底；source grounding 要求值与出处同一次产出）：
  ① prompt 给三表每行加临时字段「关联事件位置」= 该行所属事件的卡片 抽取位置（整数）。
     字段声明在 extract_rules.json 的 _temp_fields（fill=model / on_missing=warn），
     不在 schema：程序读完就 pop，不落最终产物（keep_known_fields 之前 pop，check_0 照验）。
  ② 归属顺序改成 explicit -> contain -> nearest：关联事件位置 命中某张卡片的 抽取位置
     -> fk_method="explicit"；否则按 task-63/67 的 contain -> nearest 回落。
  ③ 判据：explicit 覆盖率 >= 80%、nearest_ratio < 0.1，达不到各报一条 warning（都是 chunk
     级提示，不算行级 warning 率）；nearest 行标低置信（confidence=low），跨表 join 只允许
     explicit/contain 的行作答——answer_rows 的统计落 fk.answer 与 build_log.fk_method.answer。

task-69（P215 五条：事件序号锚点——字段名带「位置」会诱导 LLM 填值自己的偏移）：
  ① 临时字段改名「所属事件序号」（去掉「位置」这个诱导词），三表每行填整数序号；
  ② prompt 给一正一反两个例子，反例贴真数 1816（真调用里模型填的就是值自己的偏移）；
  ③ 序号是程序派生值：prompt 要求 cards 按事件在原文出现顺序输出，程序按 抽取位置 升序
     排 cards 并编号 1..n；LLM 原始顺序不是升序时记 reorder=true（不信任它的顺序）；
  ④ 序号越界（不在 1..n）/ 不是整数 -> hint_invalid 计数 + warning，不静默回落；
     「给错」与「没给」分开数（hint_ok / hint_invalid / hint_missing 三格）；
  ⑤ 判据不变：explicit 覆盖率 >= 0.8、hint_invalid 单独报数；
     explicit 上来了但 hint_invalid 也高 = LLM 数事件个数吃力（n 太大），回来查卡片切分。

task-70（P217：17 行非 explicit 里混了两种东西，必须拆开；四件）：
  ① 拆两种 fk_method，不再混成一个 nearest：
     - aggregate = metrics/facts 的派生汇总量（总数/次数/占比/派生值）：天然无单一源事件，
       指向「该 chunk 的汇总段」（anchor=chunk_summary，外键取汇总段最近的卡片）；
     - mention   = identities 的名称提及：锚点是 source_chunk_id + 抽取位置 + quote，
       **不是某个事件**（标识与事件是多对多）；
     - 跨表 join 仍只用 explicit / contain 的行作答。
  ② R12：identities.event_id 改可选（schema 去掉 required，rules 记 on_missing=fill +
     nullable=true）：缺省用 source_chunk_id + 抽取位置 + quote 定位，程序按 mention 处理、
     不硬造外键。metrics/facts 的 event_id 外键不变（一对一，explicit/contain 关联）。
  ③ 覆盖率分母分表算：metrics/facts = explicit / (rows − aggregate_rows)；
     identities 单独看 mention 覆盖率（有 quote 且能定位就算），不进 explicit 的分母。
     合并算的比例没意义，不再输出。
  ④ .bak 形态闸：完整原文写法 以 . 开头（纯扩展名）或是纯后缀词 -> warning「疑似通称不具名」
     （只报警不丢行）。

task-71（P218 两处：空分母不许报 100%；answer_rows 要与 explicit_ratio 并列当判据）：
  ① 空分母不报 100%：某表 denominator=0（全是 aggregate，或空表）时比值写 null（N/A），
     不再写 1.0。报告里单列 vacuous_tables（空分母表）计数，这些表**不进总体达标分母**
     ——达标只看 scored_tables（有分母的表）。一张全是汇总值的 chunk 不会再显示「explicit 100%」。
  ② answer_rows 与 explicit_ratio 并列：answer_rows = explicit + contain 的行数（真的能拿去
     跨表 join 的行）；报告里单列 fk.answer.answer_rows / answer_ratio，并加判据——
     answer_rows < FK_ANSWER_ROWS_MIN 或 answer_ratio < FK_ANSWER_RATIO_MIN 就 warning
     fk_answer_rows_low。覆盖率达标 ≠ 能答题（aggregate/mention 不进 answer）。

task-72（P219/P220：LLM 抽取在抖、日志两份真相、单边比例当不成对覆盖）：
  ① 稳定性：同一个 chunk 重复跑 N 次（默认 3，`--repeat`），temperature 固定 0
     （deepseek_api.call 的默认值，不传别的）；对 rows / explicit / nearest / answer_rows
     报中位数与区间 [min, max]；区间半宽 > ±10% 的指标标 unstable，**只当体检、不当判据**
     （role=health_only），稳定的才 role=judge。单次达标不算闭环。
  ② 落盘：build_log 支持 `--log-dir` 指定目录（含 --out 产物同目录即可一处可查）；
     每次运行把 out / log / repeat 一起写进日志条目，日志在哪不再靠口头约定。
  ③ 成对覆盖：跨表 join 要的是「某事件 + 它的那条数据行同时出现在 answer 里」，
     `answer_rows / rows` 只是单边行占比。`--answer-index` 落每行作答方法，
     `--pair-check questions.json --answer-index ai.json` 逐题验这对成不成对；
     答不了 = 关联问题（行挂在别的 event_id / 行本身是 aggregate/mention），不是比例问题。

task-75（P225 收尾两件：拆片参数进日志、跨配置可比指标）：
  ① 单片上限进配置 + 日志留证：MAX_CHUNK_CHARS 6000 -> 1200（环境变量 DEEPSEEK_MAX_CHUNK_CHARS
     覆盖），build_log 每条显式记 max_chunk_chars / pieces / split_on（none / line_boundary /
     line_boundary+hard_cut）。参数变是一次「可解释的 sha 变化」——日志里必须查得到，不再靠口头。
  ② 跨配置可比指标（写进 runs/61/抽取规程.md）：拆片 = 调用次数 ×2，
     所以「密度更高」必须与「成本翻倍」成对报——
       每千字产出 = 各表行数 / (chunk 字符数 / 1000)
       每千字调用 = 调用次数 / (chunk 字符数 / 1000)
     例：不拆 1.9 个/千字、1 次调用 vs 拆片 5.8 个/千字、2 次调用 = 密度 3 倍、成本 2 倍。

task-76（P226 三件：1200 档真实夹具进主回归、成本字段、warning 红信号）：
  ① fixtures/real1200（1800~2200 字、真实体量）+ meta.json 自报 max_chunk_chars，进主回归集；
  ② density 增加成本侧 `calls_per_1k_chars = pieces / chars × 1000`（与每千字产出成对）；
  ③ warning_rate=0.0909 的红信号查清是夹具构造（prompt74/missing3 故意抽掉 3 行 quote，
     3/33 行 warning）而不是 6000 档的真实问题 —— 说明写进 data。

调用面：
    默认调 deepseek_api.call(prompt)；--replay 用给定响应文本代替模型（离线自测/回归用，
    走的仍是同一条 build_prompt -> parse -> normalize 流水线，不跳过校验）。
    --replay 也接受 {"pieces": ["<第1片响应>", "<第2片响应>", ...]}，用于拆片用例；
    task-64 追加 {"attempts": {"<片号1起>": ["<第1次响应>", "<第2次响应>", ...]}}，用于重试用例
    （该片按 attempts 给出的完整序列逐次取；没给第 N 次响应就重放最后一次，模拟「模型卡住」）。

用法:
    py -3 extract.py --chunk fixtures/pass/chunk.json --out out/entities.json
    py -3 extract.py --chunk ... --replay fixtures/pass/llm_response.json --out ...
    py -3 extract.py --chunk ... --dry-run          # 只打印 prompt 摘要，不调模型
    py -3 extract.py --chunk ... --dump-prompt p.txt
    py -3 extract.py --density-scan out/build_log.jsonl   # task-66 ③ 只体检，不抽卡

退出码: 0 = 抽取并校验通过；1 = 抽取/校验失败（消息写 stderr）；2 = 用法/IO 错误。
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

import deepseek_api  # noqa: E402  (必须能在同目录找到)

DEFAULT_SCHEMA = HERE / "schema.json"
ISO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")
# task-103 一：time_norm 的两种合法写法——day 精度**只到日期**，不带时刻。
DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_NORM_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00)$")
TIME_PRECISION_VALUES = ("day", "minute", "second")


def _load_dateparser():
    """task-102 二：相对时间换 dateparser（pip 装不进系统目录，装在 工作区/vendor/）。"""
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

# task-65 ①：A/B/C 三档的行为由 schema 旁边的 extract_rules.json 决定（表驱动，P207）。
# 本文件不再列任何「哪个字段属于哪档」的清单；每个 schema 属性必须在 rules 里显式声明
# fill / on_missing（没有默认值，没声明就是 schema 错）。
RULES_NAME = "extract_rules.json"
DEFAULT_RULES = HERE / RULES_NAME
FILL_VALUES = ("model", "derived")
ON_MISSING_VALUES = ("fill", "reject", "warn", "null")

# task-64 B 档：一片最多重试 2 次（总尝试 3 次）；可用 DEEPSEEK_RETRY_MAX 覆盖。
MAX_RETRIES = 2
# task-64 三、两个监控项阈值
WARNING_RATE_MAX = 0.05
REJECT_RATE_MAX = 0.10


def gate_verdict(den, value, max_value):
    """task-104 五（P342）：闸的判定必须同时看 (分子, 分母)。

    分母 = 0 => "unverified"（未检验）——**绝不 return "pass"**：没有样本的闸是空转的闸，
    把它显示成绿就是「零样本也算通过」的盲区（rows_checked=0、null_rows=33 也显示绿就是这个坑）。
    分母 > 0 才可能 pass / fail。
    """
    if not den:
        return "unverified"
    if value is None:
        return "unverified"
    return "pass" if value < max_value else "fail"
# task-68 ③ / task-69 ③：判据（explicit 为主、序号锚点）——explicit 覆盖率下限；
# nearest 占比上限 FK_NEAREST_RATIO_MAX 定义在 FkResolver 附近（与报警文案同处，便于一处改）。
FK_EXPLICIT_RATIO_MIN = 0.8
# task-71 ②：answer_rows 与 explicit_ratio 并列当判据——覆盖率达标 ≠ 能答题。
# 跨表 join 题的前提是「相关事件与数据行都在 answer 里」；aggregate/mention 不进答案，
# 所以一条 chunk 可能覆盖率 100% 却只剩个位数的行能 join。两个阈值都是拍的（与上面同风格），
# 宁可多报一声，也不让「覆盖率好看、其实没法答题」的 chunk 静默过关。
FK_ANSWER_ROWS_MIN = 4        # 绝对下限：answer 行少于此数，跨表 join 基本没得答
FK_ANSWER_RATIO_MIN = 0.5     # 相对下限：answer_rows / 三表总行数（rows 为 0 时不判）

# task-72 ①：稳定性体检。同一 chunk 重复跑 STABILITY_REPEATS 次（temperature 固定 0），
# 报中位数 + 区间；区间半宽 (max−min)/2/median 超过 STABILITY_TOLERANCE 就标 unstable。
# task-72 ③：稳定性指标固定看这四个（单边行数 / 归属质量 / 能答的行数）。
STABILITY_REPEATS = 3
STABILITY_TOLERANCE = 0.10
STABILITY_METRICS = ("rows", "explicit", "nearest", "answer_rows")

# task-70 ①（P217）：非 explicit 的行混了两种东西，必须分开记，不再统一叫 nearest：
#   aggregate = metrics/facts 的派生汇总量（总数/次数/占比/派生值）：天然没有单一源事件，
#               指向「该 chunk 的汇总段」（记锚点 anchor=chunk_summary，外键取该段最近的卡片，
#               只为让 metrics/facts 的 event_id 外键仍非空、check_0 仍可验）；
#   mention   = identities 的名称提及：锚点是 source_chunk_id + 抽取位置 + quote，**不是某个事件**
#               （标识与事件是多对多，R12 让 identities.event_id 可选，程序不硬造外键）。
FK_METHOD_AGGREGATE = "aggregate"
FK_METHOD_MENTION = "mention"
# task-70 ②：跨表 join 只用 explicit / contain 的行作答（aggregate/mention 不进答案）。
ANSWER_METHODS = ("explicit", "contain")
# 临时字段的「角色」标记：fk_hint = 三表行用来显式指定所属事件（task-69 用「所属事件序号」，
# 序号由程序按 抽取位置 升序编号 1..n；LLM 不在卡片里写序号，少一个能写错的地方）。
TEMP_ROLE_FK_HINT = "fk_hint"
FAILED_DIR = HERE / "failed"

# task-63 ③：单次请求的输入上限（字符）。16000 只是把线往后挪，更长照样撞 max_tokens。
# task-75（P225）：默认从 6000 降到 1200——审查端定的口径是「单片上限 <=1200」，
# 这样「拆片 vs 不拆片」才可比（今晚绕圈正是因为 prompt sha 变了、日志看不出是拆片参数变了）。
# 上限是**配置**不是魔法常量：生效值一律走 max_chunk_chars()（DEEPSEEK_MAX_CHUNK_CHARS 覆盖，
# 非法/<=0 回落本默认值），并逐次写进 build_log 的 max_chunk_chars / pieces / split_on。
MAX_CHUNK_CHARS = 1200

BUILD_LOG = HERE / "out" / "build_log.jsonl"
# task-107 三：端到端断言清单（字段新增必配一条，见 QUERY_FIELD_CONTRACT）
E2E_ASSERTS_PATH = HERE / "e2e_asserts.json"


class ExtractError(RuntimeError):
    """抽取失败：模型输出不合契约 / 字段非法 / 外键无法归属。消息直接给人看。"""


class PieceRejected(ExtractError):
    """task-64 B 档：该片输出推不出、缺了该行没意义 -> 硬拒该片，触发「重试该片」。"""


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


# ---------------------------------------------------------------- 留痕：warning 采集
#
# warnings 统一存 dict：{"piece": 片号或 None, "where": 行定位或 None, "code": 分类, "msg": 人话}
#   * where 非空 = 行级 warning（进 warning 率分子；同一行多条只算一行）
#   * where 为空 = chunk 级提示（拆片合并、fk 占比、监控超阈值等，不进率）
# 对外仍输出 warnings 文本数组（老调用面兼容），另给 warnings_detail。

def warn_row(warnings, where, code, msg, piece=None):
    warnings.append({"piece": piece, "where": where, "code": code, "msg": msg})
    return warnings[-1]


def warn_note(warnings, code, msg):
    warnings.append({"piece": None, "where": None, "code": code, "msg": msg})
    return warnings[-1]


# ---------------------------------------------------------------- 基础工具

def load_json(path, what):
    p = pathlib.Path(path)
    if not p.is_file():
        raise ExtractError("[io] %s 不存在：%s" % (what, p))
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ExtractError("[io] %s 不是合法 JSON：%s：%s" % (what, p, exc)) from exc


def event_hash(chunk_id, pos):
    """schema v6 顶层 event_id_algorithm：sha256("chunk_id:偏移")[:16]。"""
    return hashlib.sha256(("%s:%d" % (chunk_id, pos)).encode("utf-8")).hexdigest()[:16]


def norm_ws(s):
    """quote 定位用：全半角归一 + 去所有空白（与 check_2 同口径）。"""
    s = unicodedata.normalize("NFKC", str(s))
    for ch in WS_CHARS:
        s = s.replace(ch, "")
    return s


def find_offset(text, needle, start=0, end=None):
    """在原文 [start,end) 窗口内定位 quote，返回**对整份 chunk**的起始字符偏移；找不到返回 None。

    拆片时每片只搜自己的窗口（片内偏移 + start = 全局偏移），保证 抽取位置 始终是
    「未改动的原始 chunk」里的偏移（R9 定义），而不是片内偏移。
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
    # 归一化后匹配，再按字符映射回原文偏移
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


# ---------------------------------------------------------------- task-102 三：时间基准（RELATIVE_BASE）
#
# 「相对谁」是相对时间的核心：昨天 / 3 天前 / last week 必须相对**数据自身的时间**绝对化，
# 不许相对「跑的那一刻」（now()）。三级取最细：turn > session > chunk。
#   * 每行落 base_source（用了哪个字段）+ base_granularity（turn/session/chunk）；
#   * 三级都拿不到 => time_norm=null + time_uncertain=true，绝不 now()；
#   * chunk 内时间跨度 > 24h 且含相对时间 => 报警「chunk 级基准不可信」（见 span_alarm_of）。
BASE_LEVELS = ("turn_time", "session_time")
BASE_GRANULARITY = {"turn_time": "turn", "session_time": "session", "time_ref": "chunk"}
BASE_FALLBACK_FIELD = "time_ref"


def _base_value(chunk, key):
    """取基准字段值；turn_time 也接受 chunk["turn"]={"time":...} 这种嵌套写法。"""
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
    """chunk -> (base_iso, base_source, base_granularity)；三级都拿不到 -> (None, None, None)。

    取最细可用的：turn > session > chunk(time_ref)。不猜、不用 now()。
    """
    for key in BASE_LEVELS:
        val = _base_value(chunk, key)
        if val and ISO_PATTERN.match(val):
            return val, key, BASE_GRANULARITY[key]
    val = _base_value(chunk, BASE_FALLBACK_FIELD)
    if val and ISO_PATTERN.match(val):
        return val, BASE_FALLBACK_FIELD, BASE_GRANULARITY[BASE_FALLBACK_FIELD]
    return None, None, None


# task-102 二：dateparser 只做「翻译」（time_raw -> time_norm），不做「补充」。
# 它默认会给任何说法造一个日期（去年 -> 今天减一年、上个月 -> 今天减一月），所以先用
# 白名单压住「说得不确定」的写法：年/月级含糊说法一律 -> None（这就是 task-101 抓的静默造假）。
# task-104 一（P342）：范式从「白名单穷举放行」换成「默认接受 + 危险模式黑名单 + 结果自检」。
# 旧白名单只放行 3 个英文模板与几种中文写法，dateparser 明明能解的「19:41」「上周」被挡成 null——
# 穷举式设计的通病：没列到的就丢，而「没列到」永远比「列到了」多。现在只挡两类：
#   ① DP_DANGEROUS：解析器会「补」的含糊说法（去年 -> 今年减一年、上个月 -> 今天减一月）；
#   ② DP_INFER：必须推算的说法（下周三、周末——依赖「一周从哪天起」，不同实现给不同答案）。
# 其余一律交给 dateparser，解出来的结果再过 self_check_time_result（结果自检），对不上才 null。
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
DP_AMBIGUOUS = DP_DANGEROUS  # 旧名字保留（自测/外部脚本还引用）
DP_MAX_LEN = 40
DP_ACCEPT = None  # task-104 一：放行名单已废弃（保留符号防止老脚本 import 失败）
DP_ABS_PATTERNS = (
    re.compile(r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(\s*\d{1,2}[:：]\d{2}([:：]\d{2})?)?$"),
    # task-103 二：带时区偏移的绝对时间同样不是「相对时间」（跨度报警不该把它算进去）
    re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})$"),
)


def time_expr_rejected(raw):
    """task-104 一：只有「已知会造假」或「必须推算」的写法才拒绝，其余默认接受。"""
    if not isinstance(raw, str) or not raw.strip():
        return True
    s = unicodedata.normalize("NFKC", raw).strip()
    if len(s) > DP_MAX_LEN:
        return True
    return bool(DP_DANGEROUS.search(s) or DP_INFER.search(s))


def time_expr_accepted(raw):
    """task-104 一：默认接受（白名单穷举已废弃）——不在危险黑名单里就交给 dateparser 解。"""
    return not time_expr_rejected(raw)


# task-104 一：结果自检用的时长换算（「3 天前」必须真的等于 基准-3 天）
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
    """「3」/「三」/「两」/「半」/「十二」/「二十」-> float；解不出 None（不猜）。"""
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
    """ISO（+08:00 / Z）-> aware datetime；解不出 None。"""
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
    """task-104 一：**结果自检**——解析结果必须能由原文解释，解释不了就是解析器在「补」。

    三条（都对得上才认）：
      ① 原文写了钟点（19:41）=> 结果的时分必须一模一样；
      ② 「N 个时间单位前」=> 结果必须等于 基准 - N（差 > 90 秒 = 自检失败）；
      ③ 绝对/其它 => 结果年份不得离基准超过 SELFCHECK_MAX_YEARS 年（防去年/上个月被补成年份）。
    返回 (ok, reason)；base 缺失而写法是相对时间 => 不自检通过（宁可 null，不猜基准）。
    """
    from datetime import timedelta
    if dt is None:
        return False, "解析器没给出结果"
    s = unicodedata.normalize("NFKC", str(raw or "")).strip()
    m = DP_TIME_CLOCK.search(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        # 原文自带偏移（…-05:00 / Z）时，钟点是**那个时区**的墙钟：先换回原文时区再比，
        # 否则上海换算后的 08:41 会被判成「与 19:41 不符」（task-103 的时区用例就是这种）。
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
    """绝对日期写法（不是相对时间）：跨度报警只看「含相对时间」的 chunk。"""
    if not isinstance(raw, str):
        return False
    s = unicodedata.normalize("NFKC", raw).strip()
    return any(p.match(s) for p in DP_ABS_PATTERNS)


def shanghai_tz():
    """Asia/Shanghai 的 tzinfo（task-103 二：时区 aware，别再靠字符串拼 +08:00）。

    zoneinfo 优先（tzdata 在 vendor/）；拿不到退回固定 +08:00——宁可退化，不许丢时区。
    """
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001  没有 tzdata 也不能让时区消失
        from datetime import timedelta, timezone
        return timezone(timedelta(hours=8))


# 原文里的「时:分[:秒]」记号（NFKC 后全角冒号也变半角，中英同款）
DP_TIME_CLOCK = re.compile(r"(\d{1,2})[:\uff1a](\d{2})(?:[:\uff1a](\d{2}))?")
# task-103 一：时长式相对说法自带粒度——「3 分钟前」的精度是分钟，不是日（不然会把
# 20:02 抹成「某天」，反而丢掉原文真有的信息）。单位映射见 DURATION_UNIT_PRECISION。
DP_DURATION = re.compile(
    r"(\d+|[一二三四五六七八九十两半]+)\s*(秒|分钟|分|小时|天|周|星期|second|minute|hour|day|week)s?"
    r"\s*(前|ago)", re.I)
DURATION_UNIT_PRECISION = {
    "秒": "second", "second": "second",
    "分钟": "minute", "分": "minute", "小时": "minute", "minute": "minute", "hour": "minute",
    "天": "day", "周": "day", "星期": "day", "day": "day", "week": "day",
}
# task-107 五(b)：**我们自己的实现错**——旧口径只认「3 分钟前 / 3 minutes ago」，
# 「A minute later / in two hours / 5 分钟后」这类**前瞻**写法一律落到 day，把分钟级的
# 相对时间降级成「只有日期」（time_norm 里的时刻被抹掉）。这里把前向写法也认成时长，
# 精度按单位给（分钟级就 minute），不再默认 day。
# 只认「确定的数」（数字 / a / an / one..ten），「a few / 几分钟」这类模糊量词不算——
# 那种该由「解不出 => unknown」的那条路处理，不许硬猜成 day。
# 两条形状：「in two hours / 两小时后」（in 开头，后面不必再跟时间副词）与
# 「A minute later / 5 分钟后」（数量 + 单位 + 时间副词）。
DP_DURATION_FWD = re.compile(
    r"\bin\s+(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|一|二|两|三|四|五|六|七|八|九|十|几|半)\s*"
    r"(秒|分钟|分|小时|天|周|星期|second|minute|hour|day|week)s?\b"
    r"|"
    r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|一|二|两|三|四|五|六|七|八|九|十|几|半)\s*"
    r"(秒|分钟|分|小时|天|周|星期|second|minute|hour|day|week)s?\s*"
    r"(?:later|after|from now|内|后)", re.I)


def time_precision_of(raw):
    """task-103 一：**原文的**时间精度（day / minute / second），不是解析器补出来的精度。

    * 有秒 -> second；只有「时:分」-> minute；
    * 只有日期或相对日词（昨天 / last week / 2026-03-14）-> day；
    * 没有 time_raw -> None（「不知道」不等于「只知道到日」）。
    task-107 五(b)：时长式相对说法**前后向都认**（3 分钟前 / A minute later / in two hours），
    精度跟随单位；旧实现只认「前 / ago」，把「A minute later」判成 day，时刻被整条抹掉。
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
        # 两个分支的组号：in 分支单位在 group(2)，数量+副词分支单位在 group(4)
        unit = (m.group(2) or m.group(4) or "").lower()
        return DURATION_UNIT_PRECISION.get(unit, "day")
    return "day"


def format_time_norm(dt, precision):
    """datetime -> time_norm 字符串；day 精度**只写日期**（task-103 一）。

    背景（审查端 P340）：「昨天」原文只有日精度，旧实现却存了 T10:00:00——那是 **BASE 的时刻**，
    不是记录的时刻；用户问「昨天几点改的」就会拿到一个凭空来的答案。所以 day 精度只落日期，
    让「几点」答不出来（该说「只知道是昨天」），minute/second 才落时刻。
    minute 精度落 HH:MM:00（秒位是补齐，不是观测到的秒值，口径写在 schema 描述里）。
    """
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=shanghai_tz())
    aware = aware.astimezone(shanghai_tz())
    if precision == "day":
        return aware.strftime("%Y-%m-%d")
    return aware.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _dp_parse(raw, base_iso):
    """dateparser 解析（RELATIVE_BASE 显式传数据自身时间）；低置信/解析失败 -> None。"""
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
        # 相对时间却没有基准 => 解不出。**绝不退回 dateparser 默认的 now()**
        return None
    settings = {"TIMEZONE": "Asia/Shanghai", "TO_TIMEZONE": "Asia/Shanghai",
                "RETURN_AS_TIMEZONE_AWARE": True}
    if base is not None:
        settings["RELATIVE_BASE"] = base
    try:
        dt = DATEPARSER.parse(unicodedata.normalize("NFKC", raw).strip(),
                              languages=["zh", "en"], settings=settings)
    except Exception:  # noqa: BLE001  解析器内部异常一律当「解不出」，不猜
        return None
    if dt is None:
        return None
    # task-103 二：解析结果必须带时区（RETURN_AS_TIMEZONE_AWARE 已开）；裸时间按
    # Asia/Shanghai 解释，再统一换算——time_norm 的时区不再是被丢掉的字符串后缀。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=shanghai_tz())
    dt = dt.astimezone(shanghai_tz())
    # task-104 一：结果自检——解析结果必须能由原文+基准解释，解释不了就 null（宁可没有，不猜）
    ok, why = self_check_time_result(raw, dt, base_iso if isinstance(base_iso, str) else None)
    if not ok:
        return None
    return dt


def rel_time(raw, ref_iso):
    """相对时间以 ref_iso 为参照解成绝对时间（task-102：换 dateparser，删掉手写中文正则）。

    解不出/低置信返回 None（不猜）。
    """
    return _dp_parse(raw, ref_iso)


def datetime_from(fmt, raw):
    from datetime import datetime
    return datetime.strptime(raw, fmt)


def normalize_time(raw, ref_iso=None, base_iso=None):
    """time_raw -> ISO8601 定长（YYYY-MM-DDTHH:MM:SS+08:00）；**推不出就 None，绝不猜**。

    task-101：本函数是 time_norm 的唯一来源（模型自报值一律丢弃）。
    task-102：换 dateparser（只翻译、不补充）；相对时间的基准是**数据自身时间**
    （base_iso，三级取最细 turn>session>chunk），不是 now()；基准拿不到 + 是相对说法 => None。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = unicodedata.normalize("NFKC", raw).strip()
    precision = time_precision_of(raw)
    if ISO_PATTERN.match(raw):
        return raw
    if DAY_PATTERN.match(raw):
        # 已经是「只有日期」的写法：原样即 day 精度的合法 time_norm
        return raw
    base = base_iso if isinstance(base_iso, str) and base_iso.strip() else ref_iso
    dt = _dp_parse(raw, base)
    if dt is None:
        return None
    return format_time_norm(dt, precision)


# ---------------------------------------------------------------- task-101 三：op_type 英文别名
#
# 背景（审查端 P331）：英文剧情动作（went to / painted / signed / scored）41/41 全判 other——
# prompt 里只写了中文判据，英文动作词没进别名表。修法不是让模型更聪明，而是把「动作词 ->
# 枚举」写成程序可复算的别名表（中英都要）：模型判 other 但动作词在表里能归到具体枚举时，
# 程序改判并留 warning（op_type_alias）。表里没有的词才允许留在 other。
# 顺序：backup/restart/read 先于 write（「备份/重启/核对」都含有「做」的意味，先拿具体的）。
#
# task-107 四：上面这些是词表，**判定算法**另外两条（见 alias_op_type）——主句限定 + 最长匹配。
# 「检查」抢「改写」这种误判不是词表少了一条，是拿整句做子串匹配、命中最先到就返回。

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
        # task-107 四：短语级别名（比单词长，最长匹配下自动压过 check/read 这类泛词）
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
        # task-107 四：去掉单字别名（「写」「读」「画」在中文里几乎处处命中，是误判源）；
        # 保留的都是一眼能定动作的双字及以上写法。
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
    """只取主句（第一个逗号/分号/句号/and/then 之前）：主句动词才是这个事件的动作。

    task-107 四：旧实现拿**整句**做子串匹配，于是「把 red-sample 断言拆成每个检查独立断言」里的
    「检查」抢在「改写」前面（表序 read 先于 write），把 write 判成 read——这是**表的错**，
    不是模型的错。主句 + 最长匹配两条一起改。
    """
    if not isinstance(text, str):
        return ""
    parts = MAIN_CLAUSE_SPLIT.split(text, 1)
    return parts[0] if parts else text


def alias_op_type(text, quote=None):
    """动作文本 -> op_type 枚举（中英别名；归不了返回 other，不猜）。

    task-107 四（谁错定论之后修表）：
      * **主句优先**：只在主句里找动作词；主句没结论才看整句；
      * **最长匹配优先**：短语（went through / signed off on / wrote up / compared）压过单词，
        单词之间比长度，长度相同再按「词表顺序（具体优先）」；
      * **去掉单字中文别名**（写/读/画）：单字在中文里几乎处处命中，是误判源；
      * quote（原文）与 action 同时给时：原文是英文剪贴，优先用原文。
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
    """在 text 里找「最长命中的别名」；同长按位置（靠前=主动词）、再按词表顺序。"""
    low = text.lower()
    best = None      # (长度, -位置, -表序, op)
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
    """task-101 二：time_raw 是纯剪贴——必须能在 chunk 原文里原样找到（空白归一后也算）。

    找不到 = 模型把原文改写了（yesterday -> 昨天、相对时间 -> 绝对时间），违约；返回 False。
    """
    if not isinstance(raw, str) or not raw.strip():
        return True
    if not isinstance(text, str):
        return True
    return raw.strip() in text or norm_ws(raw) in norm_ws(text)


# ---------------------------------------------------------------- 拆片（task-63 ③）

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
    """把 chunk 原文按行边界切成 ≤limit 的片，返回 [(start, end), ...]（对原 chunk 的全局偏移）。

    单行本身超长时硬切（切点落在这行中间），不丢字符。
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
                end = cut + 1  # 保留换行在片尾，下一片从下一行开头起
        spans.append((start, end))
        start = end
    return spans


# task-75（P225）：拆片依据（build_log.split_on）——「按什么切的」必须能查，
# 只看 pieces 数字看不出「参数变了」还是「模型变了」（prompt sha 变化要可解释）。
SPLIT_ON_NONE = "none"                 # 没拆（整份一次调用）
SPLIT_ON_LINE = "line_boundary"        # 全部切点落在换行处
SPLIT_ON_HARD = "line_boundary+hard_cut"  # 有单行超长、被硬切（切点在这行中间）


def split_on_of(text, spans):
    """task-75：这批片是按行边界切的，还是有单行超长被硬切——落到 build_log.split_on。"""
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


# task-73 ③：prompt 自检——文本里不许出现「删行指令」。
# 字段可选 ≠ 行可选：这轮反复栽的就是这条（event_id 可选被读成「整行可省」，
# quote 给不出被读成「这行别要」）。命中即硬报错，不让带病的 prompt 发出去。
PROMPT_DELETE_ROW_PATTERNS = (
    "不要单独成行", "别单独成行", "不要成行", "别成行",
    "不要写这一行", "别写这一行", "不要这一行", "别要这一行",
    "这行别要", "整行可省",
)


def check_prompt_no_delete_rows(prompt):
    """task-73 ③：prompt 文本里出现删行指令 -> 硬报错（ExtractError，不发请求）。

    返回 {"ok": True, "patterns": [...]}，green 用例用它证明「自检真的在跑」。
    """
    text = prompt or ""
    hits = [p for p in PROMPT_DELETE_ROW_PATTERNS if p in text]
    if hits:
        raise ExtractError(
            "prompt 自检失败：文本里出现删行指令 %r——字段可选≠行可选，"
            "行照抽（缺 quote 只省略 quote 字段、不写 抽取位置），不许让数据行消失" % (hits,))
    return {"ok": True, "patterns": list(PROMPT_DELETE_ROW_PATTERNS)}


def render_fields_block(rules):
    """prompt 的「字段」段由 schema + rules 生成：加字段只改 schema/rules，代码不用动。"""
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
                # task-101 二（P331）：「缺省」≠「垃圾」——没有就写 null，不是必填、也不是不许写
                # task-107 一：null 的含义按字段走（canonical_id 是「查不到」，不是「原文没有」），
                # 所以允许 spec.null_note 覆盖这句通用解释。
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
    """task-69 ②：三表用「所属事件序号」引用事件——序号由程序按 抽取位置 排序编号，LLM 不写序号。

    字段名/表全部由 rules 驱动（_temp_fields 的 role），代码不写死任何字段名字面量；
    正例/反例是 P215 ② 的硬要求（反例贴真数 1816 = 真调用里模型填的值自己的偏移）。
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
    # task-73 ③：发出去之前先自检「没有删行指令」（fail fast，不带病跑模型）
    check_prompt_no_delete_rows(prompt)
    return prompt


# ---------------------------------------------------------------- 模型输出解析

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
    except Exception:  # noqa: BLE001 允许前后有闲话时抓第一个 {...}
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
    """R6：暗字段不许过。这里就地丢弃并记 warning（check_0 仍会对最终数据做硬校验）。"""
    unknown = [k for k in row if k not in props]
    for k in unknown:
        row.pop(k, None)
        warn_row(warnings, where, "unknown_field",
                 "%s: 丢弃 schema 外的字段 %r（R6 不许塞暗字段）" % (where, k), piece=piece)
    return row


def scope_of(chunk):
    return chunk.get("_scope") or (0, len(chunk["text"]))


# ---------------------------------------------------------------- 方案 C：外键归属

def card_spans(cards, text_len, pos_field, qkey=None, qtkey=None):
    """每张卡片的事件区间 [start, end]（end 优先取 quote.原文 长度，缺 quote 就取下一卡起点）。

    字段名由 schema + rules 传入（卡片一样不写死字段名）。
    task-67 ③：区间带上卡片引文头（用于 fk 明细里「挂到哪张卡」）。
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
    """引文头（截断到 limit 字符，超长加省略号）：给人定位是哪张卡，不存整段。"""
    if not text:
        return None
    s = str(text)
    return s if len(s) <= limit else s[:limit] + "…"


# task-67 ③：nearest 占比报警线（>10% 认为卡片粒度太粗；报警时把 nearest 行明细一并列出）
FK_NEAREST_RATIO_MAX = 0.1


def _as_event_seq(value):
    """task-69 ①：把模型给的「事件序号」规整成 int；给不出/形态非法返回 None。

    「给错」和「没给」必须分得开：这里只负责形态，越界/指向不存在的序号由 FkResolver 判。
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
    """task-63 ②：三表行的 event_id 由程序按偏移归属，LLM 不填。

    task-69 ②：归属顺序 explicit -> contain -> nearest，explicit 靠「所属事件序号」：
    ⓪ 模型给的「所属事件序号」= N，在 1..n 内且指向本片某张卡 -> explicit；
       序号越界（不在 1..n）/ 形态不是整数 -> 报红 hint_invalid（计数 + warning，不是静默回落），
       仍回落 contain/nearest 但不能当「没给」算（给错 ≠ 没给，鲸鲸 P213②）；
    ① 本行偏移落在某张卡片的事件区间内 -> 归它（contain）；
    ② 不落在任何区间 -> 取「偏移 ≤ 本行的最近一张卡片」（nearest，低置信）；
    ③ 该 chunk 一张卡片都没有 -> 报错（三表行必须挂在某个事件下）。

    task-67 ③：每行日志补「挂到哪张卡 + 差多少」（card_pos / card_quote / delta），
    落 build_log 的 fk_method.detail，aggregate / mention 行能逐行定位。
    task-68 ③：不进答案的行 confidence=low。
    task-69 ②：序号按片解析（片号 -> {事件序号: event_id}），跨片同号不串。
    task-70 ①：回落不再统一叫 nearest——按「表」分工（表驱动：event_id 可选的那张表 = mention）：
      * mention   （identities）：锚点 = source_chunk_id + 抽取位置 + quote，event_id 不写（R12）；
      * aggregate （metrics/facts）：派生汇总量，无单一源事件，锚点 = 该 chunk 的汇总段
        （anchor=chunk_summary，外键取汇总段最近的卡片，保证 metrics/facts 外键仍非空）。
      两张表都仍能先命中 contain（行偏移落在某张卡的事件区间内 = 这一天确实发生了这个值）。
    """

    def __init__(self, spans, text_len, pos_field, seq_maps=None, reorder=False,
                 tables=None, mention_tables=None):
        # task-70 ①：三表顺序（分表统计要能列出 0 行的表）+ 哪些表的 event_id 是可选（= mention 表）
        self.tables = list(tables or [])
        self.mention_tables = set(mention_tables or ())
        # R14 facts 独立化：无卡片时，mention 表（facts/identities）仍可落盘（event_id=null）；
        # 只有非 mention 表（metrics）需要卡片才能 aggregate。
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
        # task-69 ②：片号 -> {事件序号: event_id}（序号是片内编号，跨片会重号，不能全局一张表）
        self.seq_maps = seq_maps or {}
        self.reorder = bool(reorder)   # task-69 ③：LLM 输出顺序不是升序、程序重排过
        self.hint_ok = 0            # 给了序号且命中卡片
        self.hint_invalid = 0       # 给了序号但非法（越界 / 指向不存在的序号）
        self.hint_missing = 0       # 没给序号
        self.hint_invalid_rows = []  # 报警明细（where / piece / hint）

    def _is_mention_table(self, table):
        return table in self.mention_tables

    def _summary_anchor(self, probe):
        """task-70 ①：aggregate 行的锚点 = 该 chunk 的汇总段（取偏移 ≤ 本行的最后一张卡）。"""
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
                # R12：名称提及没有「唯一源事件」——不硬造外键，锚点交给 chunk+偏移+quote
                method = FK_METHOD_MENTION
                anchor = "chunk_quote"
                eid = None
            else:
                method = FK_METHOD_AGGREGATE
                anchor = "chunk_summary"
                span = self._summary_anchor(probe)
                eid = span[2]
        if eid is None:
            row.pop("event_id", None)      # mention：event_id 可选，缺省不写（不是空串/占位）
        else:
            row["event_id"] = eid
        row["__fk_method"] = method      # task-68 ③：临时标记（算完 answer 统一剥掉）
        if span is None:
            span = self._span_of.get(eid) or (None, None, eid, None)
        quote = row.get("quote")
        self.log.append({"where": where, "table": table, "pos": pos, "method": method,
                         "event_id": eid, "anchor": anchor,
                         "confidence": "high" if method in ANSWER_METHODS else "low",
                         "answerable": method in ANSWER_METHODS,
                         "has_quote": isinstance(quote, dict) and _nonempty(quote.get("原文")),
                         # task-72 ③：成对覆盖要按「数据行」讲题，逐行带上 quote 原文
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
        """task-70 ③：覆盖率分表算，不合并。

        metrics / facts：explicit / (rows − aggregate_rows)——aggregate 行本来就没有可归属的事件，
        不能算进分母，否则「派生的汇总值」会把覆盖率按比例拉低成没意义的数。
        identities：只看 mention 覆盖率（有 quote 且能定位就算），**不进 explicit 的分母**。

        task-71 ①：分母为 0（全是 aggregate、或空表）时比值写 **null**（N/A），不写 1.0。
        写 1.0 在报告口径上就是「满分」——一张全是汇总值的 chunk 会显示「explicit 100%」，
        实际一个值都没挂事件。空分母的表记 vacuous=true，不进总体达标分母（见 summary()）。
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
                    # 空表也不报 100%：没有可比的行，比值就是 null
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
        # task-71 ①：空分母表单列计数；「达标」只看有分母的表（scored_tables），
        # 空分母表既不算满分、也不算不达标——它们压根不参与比例口径。
        vacuous_tables = [t for t, s in per_table.items() if s.get("vacuous")]
        scored_tables = [t for t, s in per_table.items()
                         if not s.get("vacuous") and s.get("denominator")]
        return {"rows": rows, "explicit": explicit, "contain": contain,
                "aggregate": aggregate, "mention": mention, "nearest": nearest,
                # task-70 ③：判据按表算（per_table / coverage），不再报「合并」的 explicit 比例
                "per_table": per_table,
                "coverage": per_table,
                # task-71 ①：空分母（比值 null）的表清单 + 进达标统计的表清单
                "vacuous_tables": vacuous_tables,
                "vacuous_table_count": len(vacuous_tables),
                "scored_tables": scored_tables,
                "no_offset": sum(1 for r in self.log if r["pos"] is None),
                "low_confidence": sum(1 for r in self.log if r.get("confidence") == "low"),
                # task-69 ②③：给对 / 给错 / 没给 三格分开数（给错不许混进「没给」）
                "hint_given": self.hint_ok + self.hint_invalid,
                "hint_ok": self.hint_ok,
                "hint_invalid": self.hint_invalid,
                "hint_missing": self.hint_missing,
                "hint_invalid_ratio": round(self.hint_invalid / (rows or 1), 4),
                "hint_invalid_rows": list(self.hint_invalid_rows),
                "reorder": self.reorder,   # task-69 ③：卡片重排过 = LLM 顺序不可信
                "detail": list(self.log),
                "aggregate_rows": self.aggregate_rows(),   # task-70 ①：逐行可定位
                "mention_rows": self.mention_rows(),
                "nearest_rows": self.nearest_rows()}


# ---------------------------------------------------------------- task-65 表驱动（P207）
#
# A/B/C 三档不再写在代码里：extract_rules.json 给每个 schema 属性两个属性，
#   on_missing=reject -> B 档：缺了硬拒该片并重试该片；
#   on_missing=warn   -> C 档：缺了 strip + warning 容忍（不影响判分）;
#   on_missing=fill   -> A 档：程序按 derive 指定的算法补（静默）。
# 代码只遍历 schema.entities[*].properties 取属性，不列字段名。
# 一致性 check（P208）：reject ⇒ required；warn ⇒ 非 required；fill ⇒ 都行。

class Rules:
    """extract_rules.json × schema 的已校验视图（表.字段 -> 属性）。"""

    def __init__(self, schema, doc, path):
        self.schema = schema
        self.doc = doc
        self.path = str(path)
        self.tables = {}            # file_key -> {字段: spec}（保持 schema 属性顺序）
        self.props = {}             # file_key -> schema 属性定义
        self.entities = {}          # file_key -> schema entity
        self.order = []             # file_key 顺序（schema entities 顺序）
        self.cards_key = None       # 卡片表的 file_key
        self.quote_key = {}         # file_key -> 引文对象的字段名（schema 里唯一的 object 属性）
        self.quote_text_key = {}    # file_key -> 引文正文子键（object 的第一个 required 子字段）
        self.pos_field = {}         # file_key -> 偏移字段名（derive=offset_from_quote 的那个字段）
        self.temp_fields = {}       # task-68 ①：file_key -> {临时字段名: spec}（不在 schema，读完 pop）
        self.hint_field = {}        # task-68 ①：file_key -> 用来显式指定所属事件的临时字段名
        self.mention_tables = set()  # task-70 ①：event_id 可选（nullable）的表 = mention 表
        self.nullable_fields = []    # task-70 ②：可空字段清单（进 report，便于人核）
        self.report = {}            # 一致性 check 报告

    def pop_temp_fields(self, table, row):
        """task-68 ①：把临时字段从行里 pop 出来（不落产物），返回 {字段名: 值}。

        只 pop 声明过的字段；schema 属性一律不碰。调用点必须在 keep_known_fields 之前。
        """
        out = {}
        if not isinstance(row, dict):
            return out
        for name in (self.temp_fields.get(table) or {}):
            if name in row:
                out[name] = row.pop(name)
        return out


def load_rules(schema, path=None):
    """读 extract_rules.json，与 schema 双向核对 + 跑一致性 check；任何不一致都抛 schema 错。"""
    path = pathlib.Path(path) if path else DEFAULT_RULES
    doc = load_json(path, "extract_rules")
    entries = {k: v for k, v in doc.items() if not str(k).startswith("_")}
    rules = Rules(schema, doc, path)
    problems = []
    reject_ok, warn_ok, fill_ok, null_ok = [], [], [], []
    seen = set()
    # task-104 三：装载通用别名表（别名 -> canonical_id -> 规范名，中英同表，来源可追）
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
            # P208 一致性：required 与 on_missing 不能各说各话（task-64 的冲突就出在这）
            if on_missing == "reject" and name not in required:
                problems.append("%s: on_missing=reject 但 schema 没标 required"
                                "（一致性规则：reject ⇒ required）" % key)
            if on_missing == "warn" and name in required:
                problems.append("%s: on_missing=warn 但 schema 标了 required"
                                "（一致性规则：warn ⇒ 非 required）" % key)
            if on_missing == "fill" and fill != "derived":
                problems.append("%s: on_missing=fill 只能配 fill=derived（模型字段程序补不出）"
                                % key)
            # task-101 二（P331）：「缺省」≠「垃圾」——没时间信息时写 null，不是拒整片。
            # 一致性规则：on_missing=null ⇒ schema 该字段必须显式标 nullable（一个来源说了算，
            # 不许「代码说能空、schema 说必填」两份真相）。
            if on_missing == "null":
                if not (prop or {}).get("nullable"):
                    problems.append("%s: on_missing=null 但 schema 没标 nullable"
                                    "（一致性规则：null ⇒ schema.nullable=true）" % key)
                if fill not in FILL_VALUES:
                    problems.append("%s: on_missing=null 的 fill=%r 不在 %s"
                                    % (key, fill, list(FILL_VALUES)))
            # task-70 ②（R12）：nullable 只能加在「schema 里非 required」的派生字段上——
            # 可空与否由 schema.required 一个来源说了算，rules 只是声明「程序允许不写」
            if spec.get("nullable"):
                # task-101：on_missing=null 的字段允许留在 required——程序缺了就写 null
                # （键一定在，「有没有这个信息」靠值表达），其它 nullable 字段仍须非 required
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
            # task-101 三（P331）：英文动作词别名——把别名解析器也写进 spec，代码不写死表名
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
            # task-67 ①：rules 里声明的 enum 是 schema 枚举的镜像（扩枚举必须两边同步）
            if "enum" in spec and list(spec.get("enum") or []) != list((prop or {}).get("enum") or []):
                problems.append("%s: rules.enum 与 schema.enum 不一致（%r vs %r，扩枚举要两边同步）"
                                % (key, spec.get("enum"), (prop or {}).get("enum")))
            table[name] = stored
            if stored["derive"] == "offset_from_quote":
                rules.pos_field[fk] = name
        rules.tables[fk] = table
        # task-70 ①：哪张表的 event_id 可选（nullable）= 哪张表走 mention（名称提及）
        if (table.get("event_id") or {}).get("nullable"):
            rules.mention_tables.add(fk)
            rules.nullable_fields.append("%s.event_id" % fk)
    # task-68 ①：临时字段（_temp_fields）——不在 schema，只用来从模型输出里取「关联事件位置」。
    # 校验：表必须存在、字段名不能和 schema 属性撞、必须是 fill=model + on_missing=warn（可选）。
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
    # 反向：rules 里多出来的键 = schema 里没这个属性 -> 同样是 schema 错（加字段要两边对得上）
    for key in entries:
        if key not in seen:
            problems.append("%s: schema 里没有这个「表.字段」（多声明同样是 schema 错）" % key)
    # 引文对象：schema 说 required 的子字段才必填，其余子字段（如 quote.归一化）按选填 strip
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
                    # task-104 三：别名表自述——几个别名、几个规范名、中英各几条、来源在不在
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


# ---------------------------------------------------------------- 派生算法（derive 名 -> 实现）
#
# 这里只写「某个字段怎么算」。哪个字段属于哪档、要不要补，全由 extract_rules.json 决定；
# 算法要读哪些别的字段，也由 rules 的 inputs 给出（所以本文件不出现字段名字面量）。

def canonical_key(raw):
    """R11：canonical_id 的派生值 = 「完整原文写法」的规范化形式（NFKC + 去空白）。"""
    return norm_ws(unicodedata.normalize("NFKC", str(raw))) or None


# task-104 三（P342）：**通用别名表**——别名 -> canonical_id -> 规范名，中英进同一张表。
# 背景：throughput 与 吞吐量 各自成 canonical_id，join 时串不成同一个指标；中文内部也有
# 「成功次数」vs「通过用例数」同型问题。表在 extract_rules.json 的 identity_alias.entries，
# 每条带 lang + source（来源可追）；表里没有的写法退回原文规范化（旧口径不变）。
ALIAS_TABLE = {}      # 别名(NFKC+lower) -> {canonical_id, canonical_name, lang, source}
ALIAS_CANONICAL = {}  # canonical_id -> 规范名


def alias_norm(s):
    return unicodedata.normalize("NFKC", str(s)).strip().lower()


def set_alias_table(entries):
    """装载别名表（load_rules 调）；返回校验问题列表（重复别名/缺字段都是表错）。"""
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
    """别名表 -> canonical_id；**表里没有 => None（查不到就说不认识，不许原样存档）**。

    task-107 一（最高优先级，污染型）：旧口径「表里没有就退回原文规范化」等于把猜测当事实——
    任何临时字符串都变成看起来正常的 canonical_id，之后永远 join 不上却没人发现。
    现在的口径：canonical_id 只能来自表；查不到 => canonical_id=null + unresolved=true。
    """
    if not _nonempty(raw):
        return None
    hit = ALIAS_TABLE.get(alias_norm(raw))
    if hit:
        return hit["canonical_id"]
    return None


def is_canonical_registered(canonical_id):
    """这个 canonical_id 是不是表里登记过的（null / 表外值都算没登记）。"""
    if not _nonempty(canonical_id):
        return False
    return str(canonical_id) in ALIAS_CANONICAL


def canonical_name_of(canonical_id):
    """canonical_id -> 规范名（表里没有 -> None，不猜）。"""
    return ALIAS_CANONICAL.get(str(canonical_id or ""))


# task-107 一（P347 补）：**下游不许「用完就丢」**。
# 旧口径：canonical_id=null 的行进不了任何组，聚合/检索少 N 条却看起来正常 = 静默减少。
# 现在：① 单独计数 unresolved_rows；② 答案里能说「另有 N 条名称未识别」。判据：
# 带 1 条未登记名称跑聚合 => unresolved_rows=1（不是悄悄少 1）。
UNRESOLVED_ROWS_KEY = "unresolved_rows"


def count_unresolved_rows(entities, table=None):
    """数「名称没登记」的行（unresolved=true）；table=None 数全部表。"""
    tables = [table] if table else list(entities or {})
    return sum(1 for t in tables for r in ((entities or {}).get(t) or [])
               if isinstance(r, dict) and r.get("unresolved") is True)


def unresolved_note(n):
    """能贴进答案的人话缺口声明（0 条也照实说，不藏）。"""
    return "另有 %d 条名称未识别（canonical_id=null，未参与归组）" % n


def aggregate_by_identity(entities, rules=None, table="identities",
                          group_field="canonical_id"):
    """按规范名聚合；unresolved 行单独计数，**不静默跳过**（task-107 一 / P347 补）。

    返回 groups（每个登记过的 canonical_id 一组：行数 + 来源 chunk）、
    unresolved_rows（没登记、进不了组的行数）、note（带进答案的缺口声明）。
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
    """全库（或显式集合）里 unresolved 行总数——检索侧把缺口说出来用。"""
    _, entries = lookup_scope_entries(scope)
    return sum(count_unresolved_rows(e["entities"], table=table) for e in entries)

def _locate_quote(row, chunk, ctx):
    """按 rules 取引文字段名/正文子键名，再回原文定位（抽取位置的唯一来源，R9）。"""
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
    # task-102：time_ref = 本行可用的最细基准（turn>session>chunk）；三级都没有 -> None
    return chunk.get("_base_iso") or chunk.get("time_ref")


def _derive_time_norm(row, spec, chunk, ctx):
    raw = row.get(spec["inputs"].get("raw"))
    ref = chunk.get("time_ref")
    base = chunk.get("_base_iso") or ref
    return normalize_time(raw, ref, base) or None


def _derive_time_precision(row, spec, chunk, ctx):
    """task-103 一：这行的原文时间精度（day/minute/second/unknown）；没有 time_raw -> null。

    task-107 五(a)：**「解不出」和「只知道到日」不是一回事**——有 time_raw 但 time_norm 为空
    （口语模糊写法 dateparser 真解不了）时精度记 unknown，不记 day/minute（记 day 等于对下游
    撒谎说「我知道这是哪天」）。没有 time_raw 才是 null。
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
    # task-104 三：先查别名表（中英同表）；task-107 一：表里没有 => None（不再退回原文规范化）
    return resolve_canonical(row.get(spec["inputs"].get("raw")))


def _derive_unresolved_from_canonical(row, spec, chunk, ctx):
    """task-107 一：canonical_id 查不到（null）且原文写法不为空 => unresolved=true。

    「查不到」必须是**显式状态**，不能靠「canonical_id 恰好是空」去猜——下游 join 时
    看到 null 就跳过，看到 true 就知道这行是「认识这个名字但没登记」。
    """
    inputs = spec.get("inputs") or {}
    raw = row.get(inputs.get("raw") or "完整原文写法")
    cid = row.get(inputs.get("canonical") or "canonical_id")
    if not _nonempty(raw):
        return False
    return not is_canonical_registered(cid)


def _derive_base_source(row, spec, chunk, ctx):
    """task-102 三：这行用的是哪个字段当相对时间基准（turn_time/session_time/time_ref）。"""
    return chunk.get("_base_source")


def _derive_base_granularity(row, spec, chunk, ctx):
    """task-102 三：基准粒度 turn/session/chunk（三级取最细）。"""
    return chunk.get("_base_granularity")


def _derive_time_uncertain(row, spec, chunk, ctx):
    """task-102 三：有 time_raw 却推不出 time_norm（含基准缺失）= 时间不确定（不是造假）。"""
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
# 派生执行顺序（dict 保序）：先块级/定位，再 event_id 外键，最后规范名
DERIVE_ORDER = tuple(DERIVERS)


# ---------------------------------------------------------------- 空字段 strip / 硬拒（档位由 rules 决定）

def _is_blank(v):
    """空字段判定：None / 空串 / 全空白串。"""
    return v is None or (isinstance(v, str) and not v.strip())


def _nonempty(v):
    return isinstance(v, str) and bool(v.strip())


def strip_optional_empty(row, table, rules, where, warnings, piece=None):
    """C 档：on_missing=warn 的字段为空 -> strip + warning（strip 必须跑在必填校验之前）。

    对象字段（quote）内部的非 required 子字段（如 quote.归一化）同样「空则 strip」——
    它们可选与否由 schema 的 quote.required 决定（R11 只留 原文）。
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


# task-73 ①：这些字段不是「行里的事实」，不能拿来当文字锚点（引文/偏移/派生 id/输入侧参数）
TEXT_ANCHOR_SKIP = ("source_chunk_id", "time_norm", "time_precision", "time_ref",
                    "event_id", "抽取位置",
                    "canonical_id", "quote", "类型")


def _row_text_anchors(row):
    """行里能回原文核对的文字锚点（名称/指标名/值/等式/action…），长的优先（更具体）。"""
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
    """task-73 ①：没有 quote 时用行里的文字锚点回原文找位置（行照抽，位置由程序补）。

    找不到返回 (None, None)——调用方把该行记进 unlocated_rows，不静默丢。
    """
    text = chunk["text"]
    a, b = scope
    for s in _row_text_anchors(row):
        idx = text.find(s, a, b)
        if idx >= 0:
            return idx, s
    return None, None


def _check_quote_offset(row, chunk, scope, rules, table, where, warnings=None):
    """B 档结构规则：有偏移必须有能命中的引文；没偏移时先按引文定位，再退到文字锚点。

    task-73 ①：「给不出 quote」不再是删行指令——行照抽，位置由程序补（缺 quote 只记 warning），
    连文字锚点都找不到的行记进 unlocated_rows + warning，绝不静默消失。
    引文字段名/正文子键名/偏移字段名全部来自 schema + rules，本函数不写死任何字段名。
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
        row[pos_field] = located  # A 档：由引文定位补偏移
        return
    # task-73 ①：没有 quote 也没有偏移 —— 行不丢，先试文字锚点
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
    """「缺了」判定（与类型无关）：None / 空串 / 全空白串 / 空列表 / 空对象。"""
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, dict)):
        return not v
    return False


def _validate_row(table, row, chunk, scope, rules, where, warnings=None):
    """B 档硬拒：on_missing=reject 的字段缺失/空串 + 引文/偏移结构坏。抛 PieceRejected = 重试该片。"""
    for name, spec in rules.tables[table].items():
        if spec["on_missing"] == "reject" and _missing(row.get(name)):
            raise PieceRejected("%s: 必填字段 %r 缺失/空串（on_missing=reject，不 strip）"
                                % (where, name))
    _check_quote_offset(row, chunk, scope, rules, table, where, warnings)


def validate_piece(doc, chunk, scope, rules, warnings, piece=None, unlocated=None):
    """B 档：整片输出先过校验；过不了抛 PieceRejected（触发重试该片）。

    返回被丢掉的「数值型 reject 字段为空」的行数（⑤ 原行为：数值行命根为空 -> 整条丢，
    不算 warning 也不算拒）。档位判定全部来自 rules，本函数只按属性执行。
    校验会对 doc 原地做 C 档 strip（A 档补全在 normalize 里做）。

    task-73 ①：既无偏移又无文字锚点的行不再硬拒（那等于「缺引文就删行」），
    而是记进 unlocated 列表（不落产物、但也不静默丢，报告里有它）。
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
                    dropped += 1  # ⑤ 原行为：数值行 值 是空串 -> 整条丢（不报错不记 warning）
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
    """四表 quote 占比（task-74 分母 A：0.95 只统计「能给出 quote 的行」）。

    quote_missing = 已登记缺引文的行（每项 {"table","where",...}，见 quote_missing_of）：
      ratio     = with_quote / (rows − quote_missing)   <- 闸只看这个
      gap_ratio = quote_missing / rows                  <- 「引文缺口」独立指标，不进闸
    缺 quote 又没登记的行会留在分母里、把 ratio 拖下去——静默缺口不许被平均掉。
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
    """task-74：从行级 warning 里取「已登记缺引文」的行，单列成 quote_missing_rows（不与覆盖率混）。"""
    rows = []
    for w in warnings or []:
        if not isinstance(w, dict) or w.get("code") != "quote_missing":
            continue
        where = w.get("where") or ""
        table = where.split("[", 1)[0] if "[" in where else where
        rows.append({"table": table, "where": where, "piece": w.get("piece"),
                     "reason": w.get("msg") or ""})
    return rows


# ---------------------------------------------------------------- task-78：time_norm 可复算硬闸
#
# 背景（审查端 P234/P235）：event_id 做了可复算、唯一键做了字符偏移可复算，唯独**时间列**
# 没纳入——而时间列是「取最新」的唯一依据，错了不表现在值上、表现在键上（跟 event_id
# 当年「先没错、后来错了」才加坑同型）。所以按 event_id 复算同款加硬闸：
#   * 存储的 time_norm 必须能由 (time_raw, time_ref) 按本文件归一化逻辑重算出来且逐字符一致；
#   * 重算不符 = 红（不是 warning），由 main() 落成退出码 2（产物照落盘，留痕看得见哪一行错）；
#   * 重算不出（缺 time_ref / time_raw 归一化解不出）= 也红：存了个无法复核的时间值，等于没键。

TIME_NORM_FIELD = "time_norm"
TIME_RAW_FIELD = "time_raw"
TIME_REF_FIELD = "time_ref"
TIME_PRECISION_FIELD = "time_precision"   # task-107 三：查询侧必须读它
TIME_UNCERTAIN_FIELD = "time_uncertain"

# task-107 三：**查询侧字段契约**——凡是「答案里该出现的字段」，都要在这里登记一条。
# 新增字段（比如 time_precision）= 必须同时配一条端到端断言（e2e_asserts.json 里有一条
# 预期里含这个字段的断言），否则 extract() 直接报 schema 错（check_e2e_field_coverage）。
# 机制的意义：单测证明「函数能返回字段」不算通，**从提问到答案真的读到它**才算通——
# 这正是「time_precision 说改了、查询侧没读」的第二次（上次是 filter.time）。
QUERY_FIELD_CONTRACT = ("值", "单位", "精度", TIME_NORM_FIELD, TIME_UNCERTAIN_FIELD, "来源")
QUERY_PRECISION_KEY = "精度"
QUERY_SOURCE_KEY = "来源"


def time_norm_recompute(entities, rules, chunk):
    """task-78：逐行重算 time_norm，返回可复算报告（rows_checked / reproduced / mismatch / 解不出）。

    time_ref 取值顺序：行里存了就用行里的（metrics 走 derive 落了 time_ref），
    否则用 chunk 的事件时间（cards 不存 time_ref；相对时间靠它解）。
    """
    checked, reproduced = 0, 0
    null_rows, nulled_underivable = 0, []
    mismatch, unreproducible = [], []
    # task-102 三：复算的基准必须和抽取时同一个（三级取最细），否则相对时间永远对不上
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
                # task-101 一（P331）：time_norm=null 是合法结果（没有/推不出时间信息），
                # 但要看得见：统计「因为推不出而落了 null」的行数，别让 null 变成静默出口。
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
            # task-80（P239）：参照点断言——行里存了 time_ref 就必须等于 chunk 的 time_ref，
            # 不等 = 红（参照点可随便填的话，相对时间怎么解都「对」）。
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
    den = checked + null_rows          # task-104 五：分母 = 该字段存在的行数（含 null）
    return {"rows_checked": checked, "reproduced": reproduced,
            "den": den, "num": reproduced,
            "mismatch": len(mismatch), "mismatch_rows": mismatch,
            "unreproducible": len(unreproducible), "unreproducible_rows": unreproducible,
            "null_rows": null_rows,
            "underivable_nulled": len(nulled_underivable),
            "underivable_rows": nulled_underivable[:50],
            "red": red, "ok": not red,
            # task-104 五：分母=0 => unverified（未检验），不显示成通过
            "verdict": "fail" if red else ("pass" if den else "unverified"),
            "algorithm": "time_norm == normalize_time(time_raw, time_ref)（与抽取器归一化同源）；"
                         "推不出 => time_norm=null（不是 red、也不许保留模型自报值，task-101）；"
                         "行.time_ref 必须等于 chunk.time_ref（不等=红，task-80/P239）"}


def enforce_time_norm_reproducible(entities, rules, chunk, warnings=None):
    """task-78：time_norm 复算硬闸——不符/解不出都报红（不是 warning）。"""
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
    """遍历「derive 等于 deriver 的字段」，产出 (table, name, spec, i, row)。"""
    for table in (getattr(rules, "order", None) or list(entities)):
        for name, spec in ((getattr(rules, "tables", {}) or {}).get(table) or {}).items():
            if spec.get("derive") != deriver:
                continue
            for i, row in enumerate(entities.get(table) or [], 1):
                if isinstance(row, dict):
                    yield table, name, spec, i, row


# task-102 一 / task-104 一：可复算的派生字段清单（推导自检要能「用同一套规则从输入重算一次」）
RECOMPUTE_DERIVERS = ("time_norm", "time_precision", "canonical_from_raw_writing", "card_event_id")

# task-104 一：派生字段自检**覆盖全部清单，不只时间**。除上面按 derive 重算的四个外，还有：
#   op_type     动作词 -> 枚举（查 OP_TYPE_ALIAS 表，同一张表两边用）；
#   值 / 单位    数值表达（正则 + 回原句对账）/ 单位（查 UNIT_KNOWN 表，或原句里逐字有）。
NUM_VALUE_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
UNIT_KNOWN = {
    "次", "个", "条", "行", "张", "份", "台", "人", "天", "小时", "分钟", "秒", "毫秒",
    "元", "万元", "美元", "字节", "KB", "MB", "GB", "TB", "PB", "%", "百分比", "倍",
    "count", "times", "rows", "files", "items", "seconds", "minutes", "hours", "days",
    "bytes", "ms", "s", "min", "h", "req/s", "MB/s", "usd", "cny",
}
DERIVED_FIELD_NAMES = ("time_norm", "time_precision", "op_type", "canonical_id", "值", "单位")


def _table_of_field(rules, name):
    """字段名 -> (表名, spec)；同一个名字可能出现在多张表（time_norm 就在三张表里）。"""
    out = []
    for table, specs in (getattr(rules, "tables", {}) or {}).items():
        if name in specs:
            out.append((table, specs[name]))
    return out


def _quote_text_of(row, rules, table):
    """取这一行的引文原文（quote.原文），拿来回原句对账。"""
    qkey = getattr(rules, "quote_key", {}).get(table)
    tkey = getattr(rules, "quote_text_key", {}).get(table)
    quote = row.get(qkey) if qkey else None
    if isinstance(quote, dict) and tkey and isinstance(quote.get(tkey), str):
        return quote.get(tkey)
    return None


def unit_is_known(unit, quote=None):
    """单位是否可用：表里有 / 复合单位（次/秒、MB/s）的每一段都在表里 / 原句里逐字有。

    task-104 一⑤：单位是派生自「数值表达」的字段，必须能查表复算——但「次/秒」这种复合写法
    不该被单位表漏掉（按 `/` `·` 拆开逐段判，比穷举整串单位名可靠）。
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
    """task-104 一：op_type / 值 / 单位的推导自检（产出与 DERIVERS 同格式的不符条目）。"""
    out = []
    for table, spec in _table_of_field(rules, "op_type"):
        for i, row in enumerate(entities.get(table) or [], 1):
            if not isinstance(row, dict):
                continue
            stored = row.get("op_type")
            src = row.get("action") or row.get("target")
            # task-107 四：**只看 action**（模型写的动作描述）——拿 quote 里的叙事连接词
            # （Then I went through ...）去回填会把「填了数据」判成「读了一遍」，那是把闸建在噪声上。
            want = alias_op_type(src)
            if want == OP_ALIAS_DEFAULT:
                continue  # 表里没有这个动作词：允许留在 other，不硬判
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
            # task-104 一⑤：值 的自检是「数值表达」结构检查（正则），不是回原句逐字对账——
            # 回原句属于剪贴字段（quote/time_raw）那一级，混进来会把合成夹具全判红。
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
    """task-102 一② / task-104 一：逐行重算派生字段，和存的值逐字符比；不一致 = 静默造假/键错。

    task-104 一：覆盖**全部派生字段清单**（时间、精度、动作类型、规范名、值、单位），
    不再只核时间——黑名单只挡已知危险，全字段自检才是真兜底。
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
            except Exception as exc:  # noqa: BLE001  重算本身崩了也要看得见
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
    # task-104 一：另外两个派生字段（op_type / 值 / 单位）走表/正则自检，口径一致
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
    """task-107 五(b)：**精度的实现错必须修**——精度和 time_norm 必须自洽。

    判据（可精确判定 => 闸，不是 warning）：
      * 有 time_norm 且只到日（YYYY-MM-DD）      => precision 必须 day；
      * 有 time_norm 且带时刻                    => precision 必须 minute/second；
      * 有 time_raw 但 time_norm 为空            => precision 必须 unknown（不是 day/minute）；
      * 没有 time_raw                            => precision 必须空（None，键可省）。
    这四条正是「精度标错了但我们自己没发现」的形状：day 配时刻、minute 配日期、null 配 day。
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
            elif len(str(norm).strip()) == 10:      # YYYY-MM-DD，只有日期
                want = "day"
                ok = (prec == "day")
            else:                                    # 带时刻：minute / second 都合法
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
    """task-102 一：**可追溯性断言**（统一入口，新字段进来都过一遍）。

    ① 剪贴自检：verbatim 字段（time_raw）必须逐字出现在 source_chunk 原文里。
       改写（yesterday -> 昨天）在归一化时就已经置 null 并记 warning；这里把违规逐行汇总。
       注：违规已被置 null，产物里不会留下假值，所以不翻退出码（与 task-101 的 rc=0 契约一致），
       但必须看见——violations>0 就是「模型改写过」的铁证。
    ② 推导自检：派生字段用同一套规则重算并与存值比对，不一致 = 红。
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
    """task-102 三：跨度报警——chunk 内 time_norm 跨度 > 24h 且含相对时间 => chunk 级基准不可信。"""
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


QUOTE_INVALID = "quote_invalid"          # task-107 二：运行时子串校验没过（降级留痕）
QUOTE_INVALID_RETRIED = "quote_invalid_retried"  # 重试过一次仍不过


def quote_sub_fields(row, rules, table):
    """(子字段名, 值) 列表：这一行的 quote 里所有**字符串**子字段（不止 原文）。

    task-107 二：check_2_quote 卡的是「quote 每个字符串子字段都要能在原文里 grep 到」，
    旧运行时闸只查 quote.原文 一个，于是 8 张卡的 quote.归一化 是中文改写却一路绿灯。
    """
    qkey = rules.quote_key.get(table)
    quote = row.get(qkey) if qkey else None
    if not isinstance(quote, dict):
        return []
    return [(k, v) for k, v in quote.items() if isinstance(v, str) and v.strip()]


def _substring_norm(s):
    """子串比对口径：NFKC + 去所有空白（与 check_2_quote 的 norm() 同一口径）。"""
    s = unicodedata.normalize("NFKC", str(s))
    for ch in " \t\r\n\u3000\u00a0\ufeff\u200b":
        s = s.replace(ch, "")
    return s


def quote_substring_violations(entities, rules, text):
    """逐行逐子字段查「quote 是不是 source_chunk 的连续子串」，返回违规清单（不改数据）。

    返回 [{"table","index","where","field","value","anchor"}...]；anchor=True 表示
    不过的是引文锚点（原文），False 表示辅助子字段（归一化这类）。
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
    """这四表里一共查了多少个 quote 字符串子字段（分母，写进闸输出）。"""
    return sum(len(quote_sub_fields(r, rules, t))
               for t in (getattr(rules, "order", None) or list(entities))
               for r in (entities.get(t) or []) if isinstance(r, dict))


def quote_substring_report(entities, rules, text, limit=50):
    """task-108：**只读复算**——不丢行、不改字段，专门给 gate_final 用。

    与 enforce_quote_substring 同一个判定函数（quote_substring_violations），
    区别只在：这个不洗数据，所以在**最终产物**上再算一次就是「这一格到底过没过」。
    """
    viol = quote_substring_violations(entities, rules, text)
    return {"kind": "gate_final",
            "ok": not viol, "red": bool(viol),
            "fields_checked": _quote_fields_checked(entities, rules),
            "invalid": len(viol),
            "anchor_invalid": sum(1 for v in viol if v["anchor"]),
            "aux_invalid": sum(1 for v in viol if not v["anchor"]),
            # task-109（审查端 P350）：计数器不许缺键——缺键读出来就是 None，会被当成「没问题」。
            # 这一份是**只读复算**，它自己一行不丢、一个字段不删，所以三个计数**确实**是 0
            # （不是「没数」），配 unverified=False 明确说清楚。
            "dropped_rows": 0, "dropped_cards": 0, "dropped_fields": 0,
            "unverified": False,
            "invalid_rows": viol[:limit],
            "rule": "quote 的每个字符串子字段必须是 source_chunk 的连续子串（NFKC+去空白）；"
                    "本份跑在**最终产物**上（清洗之后），ok=False 就是这一格没过",
            "algorithm": "只读复算：不在这一步丢行/丢字段，所以它报的数就是产物里的真数"}


def quote_substring_gates(raw, final):
    """task-108：闸分两份输出，别再把「清洗前」当判据。

    * gate_raw   —— 跑在模型原始输出上（清洗前，那时还有 quote.归一化 这类字段）：
                    用来看「模型这次干了什么」，红只当诊断信息。
    * gate_final —— 跑在最终产物上（清洗后）：**判据是 gate_final.ok == True**。

    背景（审查端 P349）：旧版只有一份 gate（等于 gate_raw），于是出现过
    「看 gate 是红（invalid=8），看产物是绿（0 违规）」——两边不一致会骗人。
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


# ---------------------------------------------------------------- task-109：计数器统一 int
#
# 背景（审查端 P350）：`gate_final.dropped_cards` 读出来是 None（**键根本不存在**，缺键读作 None），
# 而 None 把「数了、是 0」和「没数」混成一件——正是这两天「静默失败」的病根（None 会被人当成
# 「没问题」）。规则（本文件所有闸一律照办）：
#   ① 计数器字段类型只能是 int：0 就是「数了、是 0」；
#   ② 确实没统计的：写 0，**并且**在该闸上加 `unverified: true` + `unverified_fields: [路径]`；
#   ③ 每个闸都带 `report_line`（`"<闸> k=v ..."`；两份输出的闸用 `" | "` 并列 raw/final），
#      报表直接贴这一行，不靠「我修了 / 重试了」这种口头保证。
TEXT_LEAF_FIELDS = {
    "chunk", "identity_table", "table", "rule", "algorithm", "verdict", "levels",
    "hint", "base_source", "base_granularity", "note", "kind", "why", "source",
}

# 闸名 -> report_line 里要贴的字段（点分路径；别的字段还在 JSON 里，只是不上报表行）
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
    """task-109 ①：把闸里的 None 叶子落成确定类型——计数器 0、文本字段 ""，并记进 unverified。

    就地改（不重建 dict）：monitor 里的闸与 result 顶层的闸是**同一个对象**，重建会让两边不一致。
    列表里的 None 不动（那是行明细，不是计数器）。
    """
    for k, v in list(node.items()):
        sub = path + "." + k
        if v is None:
            node[k] = "" if k in TEXT_LEAF_FIELDS else 0
            unverified.append(sub)
        elif isinstance(v, dict):
            _none_to_zero_inplace(v, sub, unverified)


def gate_report_line(name, info):
    """task-109 ③：`"<闸> ok=... red=... k=v ..."`——报表直接贴这一行。"""
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
    """task-109：一个闸归一 —— 缺的计数器补 0（并标 unverified）、None 落成 0、补 report_line。"""
    if not isinstance(info, dict):
        info = {}
    unv = []
    _none_to_zero_inplace(info, name, unv)
    for key in GATE_REPORT_KEYS.get(name, ()):
        if "." in key:
            continue                      # 嵌套路径由它自己的子闸负责
        if key not in info:
            info[key] = 0
            unv.append(name + "." + key)  # 缺键 = 没数（正是 gate_final.dropped_cards 那个坑）
    if info.get("unverified") is True and not unv:
        unv.append(name + ".*")
    info["unverified"] = bool(unv) or info.get("unverified") is True
    info["unverified_fields"] = sorted(set(unv))
    if name != "quote_substring_gate":   # 这一份自己拼 raw|final 的报表行，见 quote_substring_gates
        info["report_line"] = gate_report_line(name, info)
    return info


def finalize_gates(monitor):
    """task-109：所有闸统一过一遍 normalize_gate（就地改 monitor，顶层的闸是同一对象，跟着变）。"""
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
    """task-109：扫所有闸里仍是 None 的叶子（计数器不许 None）。返回路径清单，空 = 合格。"""
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
    """task-109：每个闸都得有 report_line（报表不许靠口头保证）。"""
    mon = doc.get("monitor") or {}
    return sorted(n for n in GATE_REPORT_KEYS
                  if isinstance(mon.get(n), dict) and not mon[n].get("report_line"))


def enforce_quote_substring(entities, rules, text, warnings=None, retried=False):
    """task-107 二：**运行时子串校验**（不是改 prompt，别指望模型听话，要指望闸）。

    规则：quote 的每个字符串子字段都必须是 source_chunk 的连续子串（NFKC + 去空白后比对）。
      * 锚点（quote.原文）不过 => 这张卡溯源地基是假的：**丢这张行** + 记 quote_invalid；
      * 辅助子字段（quote.归一化）不过 => 丢掉这个子字段（原文锚点还在，行不丢）+ 记 quote_invalid；
      * retried=True 表示「片级已重试过一轮仍不过」，降级留痕里写明。
    返回 gate 信息（进了 monitor.quote_substring_gate，红了看得见）。
    """
    warnings = warnings if warnings is not None else []
    viol = quote_substring_violations(entities, rules, text)
    # task-108：gate_raw 的分母要在**清洗前**取（清洗后 quote.归一化 就没了，分母会变小，
    # 于是 raw/final 两份的分母对不上、看着像数错了）
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
    # task-108：这份是 **gate_raw**（清洗前算的）——只当诊断，判据看 gate_final
    return {"kind": "gate_raw",
            "ok": not viol, "red": bool(viol),
            "fields_checked": fields_before,
            "fields_remaining": _quote_fields_checked(entities, rules),
            "invalid": len(viol), "invalid_rows": viol[:50],
            "dropped_rows": len(dropped_rows),
            # task-107 二（P347 补）：**丢卡也要计数**——否则「22 张」变「20 张」没人知道为什么。
            "dropped_cards": len(dropped_rows), "dropped_fields": len(dropped_fields),
            "retried": bool(retried),
            "rule": "quote 的每个字符串子字段必须是 source_chunk 的连续子串（NFKC+去空白）；"
                    "锚点（原文）不过=丢行，辅助子字段不过=丢字段",
            "algorithm": "运行时校验，不靠 prompt；不过则片级重试一次，仍不过就降级并记 "
                         "quote_invalid；丢卡数进 dropped_cards（不静默减少）",
            # task-108：本份 = gate_raw（清洗前），判据要看 quote_substring_gate.gate_final
            "diagnostic_only": True}


def enforce_quote_ratio(entities, schema, warnings=None):
    """四、quote 占比 gate（task-74 分母 A）：quote_present / (rows − quote_missing) >= 0.95。

    缺引文的行不再当「比例不达标」罚——它们从分母剔除、单列成 quote_missing_rows（引文缺口）；
    要红的只有未登记的静默缺口（缺 quote 又没记 warning）。
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


# ---------------------------------------------------------------- task-79：时间列「取最新」查值
#
# 背景（审查端 P238）：时间列排序逻辑**单测可造**，端到端样本**不许造**。
# 查值口径写死两条，且对同指标多值必须给出不同答案：
#   * 「现在的 X 是多少」= 按 time_norm 取**最新**那一行（lookup, order=latest）；
#   * 「最大 X 是多少」= 取**最大值**那一行（max）——最新与最大不是一回事；
#   * order=oldest 只给反例用：拿旧值当「现在」= 答错（answer_ok 判红，不是口头约定）。
#
# 原始 chunk 集里目前没有多值指标（runs/79/scan_metrics79.json 可复算：扫到的 chunk 里
# 没有「同一指标名 >=2 处带数值」的段落），所以这里只落查值实现与判分；样本由单测构造
# （runs/79/multivalue_sample.json + runs/79/cards-79.json），端到端不造。

LOOKUP_ORDERS = ("latest", "oldest", "max")

# task-80（P240/P241）：「全库」作用域先定死，实现不许自己理解。
#   边界 = 「已抽取 chunk 的数值表」= 清单 lookup_scope.json 里列出的每个实体文件的 metrics 表全部行。
#   清单是唯一来源（不扫目录、不按文件名猜）；新抽取的 chunk 要进库，必须把它的实体文件追加进清单。
#   当前 lookup 每次查都是一遍全量扫描（O(库行数)），未建索引——不是 O(1)。
LOOKUP_SCOPE_FILE = HERE / "lookup_scope.json"
LOOKUP_SCOPE_ENV = "EXTRACT_LOOKUP_SCOPE"
LOOKUP_SCOPE_RULE = ("已抽取 chunk 的数值表 = 清单/显式集合里每个实体文件 metrics 表的全部行；"
                     "全量扫描、未建索引（当前成本 O(库行数)，不宣称 O(1)）")


def lookup_scope_path():
    raw = os.environ.get(LOOKUP_SCOPE_ENV)
    return pathlib.Path(raw) if raw else LOOKUP_SCOPE_FILE


def read_lookup_manifest(path=None):
    """读全库清单 -> [(实体文件路径, 清单声明的 source_chunk_id 或 None)]。

    清单缺失 = 硬错：全库边界必须显式，不能悄悄退化成单 chunk。
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
    """清单文件的自报标记：source == "manifest"（lookup_scope.json 及它的副本都靠这个认）。"""
    return isinstance(doc, dict) and doc.get("source") == "manifest"


def reject_manifest_scope(path=None):
    """task-81（P243）：scope 只收实体文件列表/内联 dict 列表；传 manifest 直接报错。

    以前不报错：清单被当实体文件读（里面没有 metrics 表）-> 返回「库里没有该指标名的数值行」，
    格式错误被伪装成「没找到」（静默空结果）。宁可在入口炸，也不给一个看着正常的空答案。
    """
    hint = "manifest 请用 scope=None（或传实体文件列表）"
    if path:
        raise ExtractError("lookup scope 收到的是全库清单（manifest，不是实体文件）：%s；%s"
                           % (path, hint))
    raise ExtractError("lookup scope 收到的是全库清单（manifest，不是实体文件）；%s" % hint)


def lookup_scope_entries(scope=None):
    """把 scope 归一成 [{"path","source_chunk_id","entities"}]，返回 (scope_mode, entries)。

    scope=None -> 读全库清单（默认全库，scope_mode=manifest）
    其它       -> 显式集合（scope_mode=explicit）：元素可以是
                  实体 dict / 路径字符串 / {"path"} / {"entities","source_chunk_id"}。
                  task-81：manifest（dict 含 files，或路径 JSON 的 source==manifest）不许进来 ——
                  直接抛 ExtractError，不做「当实体文件读」的静默降级。
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
    """全库同名指标候选行：每行带 source_chunk_id + 来自哪个实体文件（清单可见）。"""
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
    """指标名比对口径：去空白（「P95 延迟」与「P95延迟」算同一个指标）。"""
    return re.sub(r"\s+", "", str(name or ""))


def parse_time_norm(value):
    """time_norm -> datetime（带时区）；解不出返回 None（不猜时区）。

    task-103 一：day 精度只存 YYYY-MM-DD（原文没有时刻，不能编一个），排序时按当天
    00:00+08:00 参与——「取最新」只需要可比，不需要假造时刻。
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
    """同一指标名的候选行（值必须是数、不是 bool）；顺序保持原表顺序。"""
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
    """全库按「时间列 / 值」取一行作答：latest=最新（现在）、oldest=最旧、max=最大。

    task-80（P240/P241）三定义：
      * 作用域：scope=None 走全库清单（所有已抽取 chunk 的数值表，见 lookup_scope.json）；
        scope=... 走显式集合（单测/对照用，结果里 scope_mode=explicit 可查）。
        task-81：scope 传 manifest（dict 含 files / 路径 JSON 的 source==manifest）直接
        raise ExtractError（「manifest 请用 scope=None（或传实体文件列表）」），不静默返回空。
      * 同 time_norm 但值不同 = 冲突：不静默选一个，value=None + conflict_candidates
        （每条候选带各自 source_chunk_id），judge_card 因此判红。
      * 答案带来源 + 候选清单：source_chunk_id + compared（被比较过的所有行：
        值 / time_norm / source_chunk_id / 来自哪个实体文件）。
    """
    if order not in LOOKUP_ORDERS:
        raise ExtractError("order 只能是 %s，收到 %r" % ("/".join(LOOKUP_ORDERS), order))
    scope_mode, entries = lookup_scope_entries(scope)
    rows = library_metric_rows(scope, name, table, name_field, value_field)
    # task-107 一（P347 补）：检索也不许「用完就丢」——库里 unresolved 的行要报数。
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
                 # task-107 三：查询侧真的读 time_precision / time_uncertain——
                 # 「精度」是答案的一部分（问「几点」时 day 精度必须答「只知道到日」），
                 # 不是库里躺着的字段。缺了就是 None（不是默认 day，不猜）。
                 QUERY_PRECISION_KEY: row.get(TIME_PRECISION_FIELD),
                 "time_uncertain": row.get(TIME_UNCERTAIN_FIELD),
                 "source_chunk_id": best["source_chunk_id"]})
    return info


def answer_ok(expected, got):
    """判分：值 + 单位都要对上（值是数就按数比，容差 1e-9）。

    task-107 三：expected 里**写了**的答案侧字段（精度 / time_norm / time_uncertain）
    也必须对上——只有读了这个字段才会不同，写进预期就是在验「查询侧真的读了它」。
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
    """判一张标注卡：按卡上「查法」走全库 lookup，再与「预期答案」比对（值+单位）。

    order_override 只给反例样用（例如把「最大」那题按 latest 作答，应当判红）。
    作答里带来源（source_chunk_id）与候选清单（compared）；冲突时 ok=False、reason 写明。
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
    """task-68 ③ / task-70 ②：跨表 join 只允许 explicit/contain 的行作答。

    aggregate（派生汇总量）与 mention（名称提及）都不进答案：前者没有单一源事件，
    后者的锚点是 chunk+quote 而不是事件——拿它们去 join 会造出「看着对上、其实没对上」的答案。
    假阴 < 假阳：抓不到就不答，不污染答案。靠 normalize 时打的 __fk_method 临时标记统计；
    调用方统计完必须剥掉标记。

    task-71 ②：answer_rows 与 explicit_ratio 并列当判据——覆盖率达标 ≠ 能答题。
    这里把 answer_rows / answer_ratio / 两个阈值一起报出来，判据在 extract() 里报警
    （answer_ok：True 达标 / False 报警 / None 无行可判，三表 0 行时不算不达标）。
    """
    total, low, low_rows = 0, 0, []
    for table, rows in entities.items():
        for i, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            method = row.get("__fk_method")
            if method is None:      # 卡片表没有归属方法（它本身就是事件的根）
                continue
            total += 1
            if method not in ANSWER_METHODS:
                low += 1
                low_rows.append("%s[#%d]" % (table, i))
    answer_rows = total - low
    answer_ratio = round(answer_rows / total, 4) if total else None
    if total == 0:                       # 没有可判的行（不是不达标，是不判）
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
    """task-72 ①：一轮抽取里固定看四个数。"""
    answer = (result.get("fk") or {}).get("answer") or {}
    return {"rows": (result.get("fk") or {}).get("rows"),
            "explicit": (result.get("fk") or {}).get("explicit"),
            "nearest": (result.get("fk") or {}).get("nearest"),
            "answer_rows": answer.get("answer_rows")}


def stability_report(points):
    """task-72 ①：重复 N 次的体检报告（中位数 + 区间 + 是否抖）。

    「区间宽于 ±10%」的口径：半宽 = (max−min)/2，相对中位数的半宽 > STABILITY_TOLERANCE
    （10%）就算 unstable。中位数为 0 时：全 0 视为稳定；出现非 0 视为抖（无法算相对宽）。
    unstable 的指标 role=health_only（不当判据、只当体检），稳定的才 role=judge。
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
    """读端到端断言清单（默认 runs/61/e2e_asserts.json）。

    每条 = 一个「只有查询侧读了这个字段才会答对」的题：
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
    """task-107 三（机制）：查询侧字段契约里的每个字段，必须有一条断言把它写进「预期」。

    没覆盖 = 这个字段「说改了、其实没人在答案里读过」——直接报错，不许静默放过。
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
    """跑端到端断言：从提问到答案，证明字段真被读过（单测不算）。

    判据：lookup_metric 作答后，预期里写了的**每个答案侧字段**都要对上（值/单位/精度/…）。
    答不对 = 红，不是「没覆盖」。断言可带 "chunk"（只对某个 chunk 生效）——换 chunk 跑时
    不相关的那几条记 not_applicable（不是跳过、也不是通过）。
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
    """task-72 ③：把「每行能不能作答 / 挂在哪个事件」落成一份可复核的索引。

    成对覆盖要按数据行讲题，所以这里带 quote 原文（detail 里已加），
    不再靠 where/pos 去猜——pos 相同的行太多了（汇总段整段都挂同一张卡）。
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
    """task-72 ③：逐题验「事件 + 它的那条数据行」是否同时在 answer 里。

    每题给 event_quote（在卡片 quote / action / target 里找）和
    row={table, quote}（在数据行 quote 里找），判据：
      ① 事件抽出来了；② 数据行抽出来了；③ 数据行 method ∈ answer 集合；
      ④ 数据行的 event_id 就是这道题问的那个事件。
    ①②失败 = 抽取漏了；③失败 = 行本身不可作答（aggregate/mention）；
    ④失败 = 关联错了（行挂在别的事件上）——都不是「比例不够」，是成不成对的问题。
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
    """题单：JSON 数组，或 {"questions": [...]}。"""
    doc = load_json(path, "questions")
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict) and isinstance(doc.get("questions"), list):
        return doc["questions"]
    raise ExtractError("题单须是 JSON 数组或 {\"questions\": [...]}：%s" % path)


def save_failed_piece(chunk, piece_idx, attempts, reason, raw):
    """⑥ B 档：重试 2 次仍拒 -> 原文+原因落 failed/，并进计数（不许静默丢弃）。"""
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


# ---------------------------------------------------------------- 归一化

def normalize_row(table, row, chunk, props, warnings, idx, ctx, piece=None):
    """A 档补全 + 形状复核（类型/枚举/pattern）。哪档怎么处理由 rules 决定，这里只按属性执行。"""
    rules = ctx["rules"]
    where = "%s[#%d]" % (table, idx)
    ctx["where"] = where
    ctx["piece"] = piece      # task-69 ②：序号按片解析，归属时要带上片号
    # task-68 ①：临时字段先 pop（不落产物），只留给 fk 归属当 explicit 线索
    hints = rules.pop_temp_fields(table, row)
    ctx["hint"] = hints.get(rules.hint_field.get(table))
    keep_known_fields(row, props, warnings, where, piece=piece)
    strip_optional_empty(row, table, rules, where, warnings, piece=piece)
    # task-101 二（P331）：「没有时间信息」=> null（不是 reject）。on_missing=null 的字段
    # 缺了/空串就显式写 null（键留着，说明「这行没有这个信息」，不是「程序漏了这个字段」）。
    for name, spec in rules.tables[table].items():
        if spec.get("on_missing") != "null":
            continue
        if _is_blank(row.get(name)):
            row[name] = None
    # task-101 二：time_raw 是纯剪贴，不许被改写。原文里找不到 => 不认这个写法：
    # 记 warning + 置 null（下游 time_norm 于是也是 null——宁可没有时间，不要假时间）。
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
    # A 档：fill=derived 的字段由程序产出（覆盖自报值，静默）；推不出且模型也没给 -> 重试该片
    for algo in DERIVE_ORDER:
        for name, spec in rules.tables[table].items():
            if spec.get("derive") != algo or spec["on_missing"] not in ("fill", "null"):
                continue
            val = DERIVERS[algo](row, spec, chunk, ctx)
            if _is_blank(val):
                # task-101 一（P331）：推不出 => null，**不许回落到模型自报值**（那是静默造假：
                # 模型给「去年」配 2022-01-01 也照收）。null 键保留，审计/check_6 只看非空值。
                if spec["on_missing"] == "null":
                    row[name] = None
                    continue
                # task-70 ②（R12）：nullable 字段推不出就不写（identities.event_id 缺省 = 按
                # mention 处理，靠 source_chunk_id + 抽取位置 + quote 定位），不硬造外键、不拒片
                if spec.get("nullable"):
                    row.pop(name, None)
                    continue
                if _is_blank(row.get(name)):
                    raise PieceRejected("%s: %r 缺失且程序推不出（derive=%s，A 档失败）"
                                        % (where, name, algo))
                continue  # 程序推不出但模型给了：保留，交给形状复核
            row[name] = val
    # 形状复核：类型 / 枚举 / pattern（schema 说什么就是什么）
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
    # task-101 三（P331）：模型判 other、但动作词在别名表里能归到具体枚举 -> 程序改判
    # （中英都走同一张表；表里没有的词才留在 other，改了记 warning 可查）
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


# ---------------------------------------------------------------- 分片合并

def unique_key_of(ent):
    """唯一键只能来自 schema（R4：唯一键属于契约，不在代码里另写一份）。"""
    uk = ent.get("unique_key") if isinstance(ent, dict) else None
    if not (isinstance(uk, list) and uk):
        raise ExtractError("schema 错：%r 没有 unique_key（R4）"
                           % (ent.get("file_key") if isinstance(ent, dict) else ent))
    return uk


def merge_rows(all_rows, unique_key, warnings, table):
    """拆片后按 schema 唯一键合并去重（同一行被两片抽到只留第一份）。"""
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


# ---------------------------------------------------------------- task-66 内容噪声闸（P209）
#
# 真调用验证发现两类内容噪声，审查端 P209 定「可精确判定的用闸，剩下的抽检」：
#   ① 类型词（仓库/版本/工具）被 LLM 当标识名 -> 能机器判定 -> 硬闸：报红 + 丢行 + 计数；
#   ② 函数名/配置项（strip_fences）同一片段既当动作又当标识 -> 硬闸：报红（路由错位要人看）；
#   ③ 密度偏高（1200 字符抽 19 个标识 = 明显过抽）-> 只体检：每 chunk 各表行数 + 标识数/千字符，
#      --density-scan 把前 5% 的 chunk 列出来给人看（阈值是拍的，不当闸）；
#   ④ 剩下的靠抽检（spotcheck.py：每表随机 5 行 + 种子留底，标识表错行率 <10% 才继续端到端）。
#
# 两道闸的「红」不是 PieceRejected（不重试、不丢整片）：它们跑在归一化之后，
# 只对已通过结构校验的行做内容判定，命中行记 warning（code 见下）+ 进 monitor.content_gates。

CONTENT_GATE_ENUM = "enum_value_as_identity"     # ① 枚举值当标识名（丢行）
CONTENT_GATE_CROSS = "identity_card_same_string"  # ② 同一串既当标识又当卡片动作（不丢行）
CONTENT_GATE_EQUALS = "name_equals_type"          # ① 的名称==类型（丢行）
CONTENT_GATE_SHAPE = "identifier_as_action"       # task-67 ② 纯标识符形态当动作（不丢行）
CONTENT_GATE_GENERIC = "identity_generic"         # task-70 ④ 通称不具名 .bak / 纯后缀词（不丢行）

# task-67 ②：纯标识符形态（下划线/点分选择器，可带空括号）——正常动作是中文动宾
# task-104 二（P343）：补路径分隔符 `/` `\`——旧正则不认 `runs/61/extract.py`，把文件路径
# 判成「非标识符」；路径本来就是标识符最常见的形态之一，漏这一类等于形态闸只挡了一半。
SHAPE_IDENT_RE = re.compile(r"^[a-z][a-z0-9_\-]*([./\\][a-z0-9_\-]+)*(\(\))?$")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

IDENTITY_ENUM_FIELD = "类型"


def identity_table_of(rules):
    """标识表 = schema 里同时有「名称」和 enum 型「类型」的实体（不写死表名）。"""
    for fk in rules.order:
        props = rules.props.get(fk) or {}
        if "名称" in props and isinstance((props.get(IDENTITY_ENUM_FIELD) or {}).get("enum"), list):
            return fk
    return None


def shape_looks_like_identifier(value):
    """task-67 ②：纯标识符形态且不含中文 -> 疑似把标识当动作（形态规则，不是黑名单）。"""
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s or CJK_RE.search(s):
        return False
    return bool(SHAPE_IDENT_RE.match(s))


def _path_like(value):
    """task-104 二：像文件路径（含路径分隔符）。target=路径是正常语义，不是「把标识当动作」。"""
    return isinstance(value, str) and ("/" in value or "\\" in value)


def apply_content_gates(entities, rules, warnings):
    """task-66 ①② 枚举值当标识名 / 同串交叉 + task-67 ② 形态闸。

    ①：名称 / 完整原文写法 ∈ 类型枚举值，或 名称==类型 -> 报红 + 丢弃该行 + 计数；
    ②：名称 / 完整原文写法 与 卡片 action/target 完全相同 -> 报红（路由错位，不丢行）；
      task-67 ②：action/target 本身是纯标识符形态且不含中文 -> 记 warning（不丢行）。
    返回报告，落 monitor.content_gates 与 build_log（丢了哪些行、命中几处都要能查）。
    """
    ident = identity_table_of(rules)
    report = {"identity_table": ident, "enum_values": [],
              "enum_value_dropped": 0, "name_equals_type_dropped": 0,
              "cross_same_string": 0, "shape_suspect": 0,
              "dropped_rows": 0, "red": False, "examples": []}
    # task-67 ② 形态闸：action/target 是纯标识符形态（strip_fences / gates.quote_ratio_min）-> warning
    # 不依赖标识表，所以放在 ident is None 早退之前（只报警、不丢行、不置 red）
    shape = 0
    for idx, row in enumerate(entities.get(rules.cards_key) or [], 1):
        if not isinstance(row, dict):
            continue
        where = "%s[#%d]" % (rules.cards_key, idx)
        for field in ("action", "target"):
            v = row.get(field)
            if not shape_looks_like_identifier(v):
                continue
            # task-104 二（P343）：形态闸补了路径分隔符后，`runs/61/extract.py` 这类**目标**会被
            # 判成标识符形态——但 target 是文件路径属于正常语义（「把标识当动作」说的是 action）。
            # 所以路径形态的 target 不告警；action 与点分标识（gates.quote_ratio_min）照旧告警。
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
    # ② 同串交叉：同一 chunk 内 标识名 == 卡片 action/target（完全相同）=> 路由错位
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


# task-70 ④（P217）：通称不具名的形态闸——标识的「完整原文写法」只写扩展名 / 纯后缀词时报警。
#   .bak / .json（以 . 开头的纯扩展名）或 bak / 备份（纯后缀词）= 没写名字，只留了通称。
#   函数名（strip_fences）、工具名（Python）该落「工具」，不在这条闸的范围里（task-67 形态闸管卡片侧）。
GENERIC_SUFFIX_WORDS = {"bak", "backup", "tmp", "temp", "log", "json", "md", "py", "txt",
                        "zip", "old", "orig", "copy", "副本", "备份", "临时", "日志", "快照"}


def identity_generic_reason(raw):
    """返回「疑似通称不具名」的原因；不像通称就返回 None。"""
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
    """task-70 ④：标识的 完整原文写法 是通称（.bak / 纯后缀词）-> warning（不丢行）。

    这类行「名字」是空的，canonical_id 会把所有 .bak 归成一个，join 时必然串味；
    但也可能真的是在说「备份」这个动作对象，所以只报警、交抽检/人看，不擅自丢。
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
    """task-70 ②（R12）：外键不变量——程序算得出就写，算不出就不写，不许「半写」。

    * event_id 非可选的表（metrics/facts）：每行必须有 event_id，且必须存在于 cards；
    * event_id 可选的表（identities）：可以没有（mention 用 chunk+偏移+quote 定位）；
      有就必须存在于 cards（不许硬造悬空外键、不许写占位串）。
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
    """③ 密度体检项：每 chunk 各表行数 + 标识数/千字符（不当闸，只给人看）。

    task-76 ②：再加成本侧 `calls_per_1k_chars = pieces / chars × 1000`，与产出侧
    （rows_per_1k_chars / identities_per_1k_chars）**成对**出现——只看密度不看调用次数，
    拆片后「密度更高」会被误当成「抽得更全」。pieces 给不出就写 null，不猜。
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
    """分布异常：按「标识数/千字符」降序取前 top_frac 的 chunk 列出来（只体检，不当闸）。

    样本 < min_samples 时仍给出排名，但 enough_samples=False（小样本的 5% 没统计意义）。
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
    """--density-scan：读 JSONL（build_log 条目 {chunk, chars, counts}），列前 5% 密度异常 chunk。"""
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
    report["note"] = "只体检：密度高不代表错，列出来给人复核过抽；阈值/前 5% 是拍的，不当闸"
    return report


# ---------------------------------------------------------------- task-77 ②：覆盖式断言
#
# P227 的核：real1200 的「期望值」是照运行结果抄的 —— 运行抽了 5 条 identities，期望就写 5 条，
# 于是漏抽的 15 条被当成了「本来就该 5 条」。判据换成覆盖式，两条各自独立：
#   * 清单内漏抽 = 红（覆盖不全就是错，不是「少抽一点也没关系」）
#   * 清单外多抽 = warning（抽多了/抽错了要可见，但不整份作废）
# 清单（expected.json）由人工读 chunk 原文数出来，跟本文件的产物无关，见 fixtures/real1200/expected.json。

EXPECTED_TABLES = ("metrics", "identities", "facts")


def metric_key(row):
    # 拼成一个字符串：报「漏了哪条」时能直接念（元组/列表在留痕里不好读）
    return "%s=%s %s" % (norm_ws(str(row.get("指标名") or "")),
                         norm_ws(str(row.get("值"))),
                         norm_ws(str(row.get("单位") or "")))


def identity_key(row):
    # 产物行用「完整原文写法」；人工清单里叫「写法」（同一个东西的两种叫法）
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
    """把人工清单归一成每表的 key 列表（保持清单顺序，报「漏了哪几条」时照原文说）。"""
    keys = {"metrics": [], "identities": [], "facts": []}
    for m in expected.get("expected_metrics") or []:
        keys["metrics"].append(metric_key(m))
    for x in expected.get("expected_identities") or []:
        keys["identities"].append(identity_key(x if isinstance(x, dict) else {"写法": x}))
    for f in expected.get("expected_facts") or []:
        keys["facts"].append(fact_key(f if isinstance(f, dict) else {"等式": f}))
    return keys


def expected_coverage(entities, expected):
    """清单内必须覆盖（漏=红）；清单外一律 warning（多抽/抽错要可见）。"""
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
        # 清单外的行（去重后报，同一写法多行只报一次）
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


# ---------------------------------------------------------------- 主流程

def load_chunk(path):
    chunk = load_json(path, "chunk")
    for key in ("source_chunk_id", "text"):
        if not chunk.get(key):
            raise ExtractError("chunk 缺字段 %r" % key)
    # task-102 三：time_ref 不再是硬要求——三级基准（turn/session/chunk）都拿不到时，
    # 相对时间一律 null + time_uncertain=true（绝不用 now()）。给了就必须是 ISO8601。
    for key in (BASE_FALLBACK_FIELD,) + BASE_LEVELS:
        val = _base_value(chunk, key)
        if val and not ISO_PATTERN.match(val):
            raise ExtractError("chunk.%s 必须是 ISO8601（YYYY-MM-DDTHH:MM:SS+08:00），实际 %r"
                               % (key, val))
    if not isinstance(chunk["text"], str):
        raise ExtractError("chunk.text 必须是字符串")
    return chunk


def load_replay(path):
    """--replay 归一成 {"default": str|None, "pieces": [str]|None, "attempts": {片号: [str]}}。"""
    return normalize_replay(load_json(path, "replay"))


def normalize_replay(doc):
    """把一份 replay 文档（字符串 / 对象）归一成同一形状（--repeat 的每轮文档也走这里）。"""
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
    """task-72 ①：--repeat 每轮用一份响应，模拟「同一 chunk 跑 N 次」的真实抖动。

    接受 list（每轮一项）或 {"repeats": [...]}；普通单文档照样兼容（每轮用同一份）。
    轮数多于给定份数时沿用最后一份（不伪造内容，只是复用）。
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
    """取第 piece_no 片（1 起）第 attempt 次（1 起）的响应文本。"""
    seq = replay.get("attempts", {}).get(piece_no)
    if seq:
        if attempt <= len(seq):
            return seq[attempt - 1]
        return seq[-1]  # 重试次数多于给定序列：重复最后一次（模拟模型卡住，不伪造成功）
    pieces = replay.get("pieces")
    if pieces is not None:
        if piece_no - 1 < len(pieces):
            return pieces[piece_no - 1]
        return "{}"  # 片数多于给定响应：该片按空结果处理，不伪造内容
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
    accepted = []   # [{"piece": 片号, "scope": (a,b), "raw": 解析后的 doc}]
    failed = []     # [{"piece","attempts","reason","path"}]
    unlocated = []  # task-73 ①：既无偏移又无文字锚点的行（不落产物，但不静默丢）
    dropped = 0
    retries = 0
    quote_retried_pieces = []   # task-107 二：片级重试过仍不过的片（降级留痕要写明）
    max_try = max_retries() + 1
    source = "replay" if replay_text is not None else "api"

    # 第 0 遍：逐片取模型输出 + B 档校验；被拒就重试该片（上限 2 次），仍拒 -> failed/ + 计数
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
                # task-107 二：片级先做一次 quote 子串校验——不过就**触发重试一次**
                # （走下面同一个 PieceRejected 通道），重试仍不过才降级丢行（见 enforce_quote_substring）。
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

    # 第一遍：卡片表（event_id 由偏移复算，不依赖三表）
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
    # task-69 ③：序号是派生值——程序按 抽取位置 升序排 cards 后编号 1..n（LLM 不写序号）。
    # 序号按「片」作用域：同一次模型输出里的一批卡编一套号，跨片各自从 1 开始。
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
        # task-69 ③：按 抽取位置 升序排（无偏移的排最后），并记 LLM 原始顺序是否已是升序
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
    # R14 facts 独立化：cards 空时，nullable 表（facts/identities）行可独立落盘（event_id=null），
    # 只有非 nullable 表（metrics）的行仍必须挂在事件下。
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

    # 第二遍：其余表（外键由程序按偏移归属；档位行为同样来自 rules）
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
    # task-69 ④：序号越界单独报红（不是静默回落）；「给错」和「没给」必须分开数
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
    # task-70 ③ 判据一：覆盖率分表算（合并算会得到「看着是数字、实际没意义」的比例）
    low_tables = []
    for tname, tstat in (fk_sum.get("per_table") or {}).items():
        if tstat.get("kind") != "value" or not tstat.get("denominator"):
            continue   # facts/metrics 全 aggregate（分母 0）或 identities（只看 mention 覆盖率）
        ratio = tstat.get("explicit_ratio")
        if ratio is None:      # task-71 ①：空分母 -> null，不进达标分母，也不报「低」
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
    # task-71 ①：空分母表单列计数（比值是 null，不是 100%，也不进达标分母）
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
    # task-70 ①：aggregate / mention 单独报数（它们不是「低置信 nearest」，是两类不同的东西）
    if fk_sum["aggregate"] or fk_sum["mention"]:
        warn_note(warnings, "fk_aggregate_mention",
                  "非 explicit 行已拆两类（task-70 ①）：aggregate %d 行（metrics/facts 派生汇总量，"
                  "无单一源事件，指向该 chunk 汇总段）、mention %d 行（identities 名称提及，"
                  "锚点 source_chunk_id+抽取位置+quote，event_id 可选不硬造）。"
                  "跨表 join 只用 explicit/contain 行作答；分表覆盖率见 fk.per_table。"
                  % (fk_sum["aggregate"], fk_sum["mention"]))
    # task-70 ③：identities 的 mention 覆盖率单独看（有 quote 且能定位就算）
    ident = (fk_sum.get("per_table") or {}).get("identities") or {}
    if (ident.get("rows") and ident.get("mention_coverage") is not None
            and ident.get("mention_coverage") < 1.0):
        warn_note(warnings, "fk_mention_coverage",
                  "identities mention 覆盖率 %d/%d=%.1f%%（有 quote 且能定位才算）："
                  "缺 quote/偏移的标识行只能靠 chunk 粗定位，回来查标识行的 quote 抽取。"
                  % (ident.get("locatable", 0), ident.get("rows", 0),
                     (ident.get("mention_coverage") or 0) * 100))
    # task-69 ④：序号越界单独报红（不是静默回落）；「给错」和「没给」必须分开数
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

    # task-107 二：运行时子串校验（quote 每个字符串子字段都必须是原文连续子串）。
    # 跑在内容闸/占比闸之前：丢行会改行数，先降级再统计，占比才不会被假引文撑高。
    quote_sub = enforce_quote_substring(entities, rules, text, warnings,
                                        retried=bool(quote_retried_pieces))
    # task-66 ① ②：两道内容硬校验（可精确判定 -> 闸）。跑在归一化之后、统计口径之前：
    # 丢行会影响四表行数/quote 占比，所以必须先于 monitor/quote gate 执行。
    content_gates = apply_content_gates(entities, rules, warnings)
    # task-70 ④：通称不具名的形态闸（.bak / 纯后缀词）——只报警不丢行，跑在内容闸之后
    generic_gate = apply_identity_generic_gate(entities, rules, warnings)
    # task-70 ②（R12）：外键不变量——算得出就写、算不出就不写，不许悬空/占位
    fk_invariants = enforce_fk_invariants(entities, rules)
    # task-78：time_norm 可复算硬闸（跟 event_id 复算同款）。跑在归一化/补全之后，
    # 核的是「将要落盘的值」，不符就报红——时间列错了是键错，不能只记 warning。
    time_norm_gate = enforce_time_norm_reproducible(entities, rules, norm_chunk, warnings)
    # task-107 五(b)：精度自洽闸（day/minute/unknown 与 time_norm 对不上 = 红）
    precision_gate = enforce_time_precision_consistent(entities, rules, warnings)
    # task-107 三：端到端断言（从提问到答案）——字段新增必配一条，覆盖不全 = schema 错。
    e2e_asserts = load_e2e_asserts(os.environ.get("EXTRACT_E2E_ASSERTS") or None)
    e2e_coverage = check_e2e_field_coverage(e2e_asserts)
    e2e_report = run_e2e_asserts(e2e_asserts, scope=[{
        "path": "(current chunk)",
        "source_chunk_id": chunk["source_chunk_id"],
        "entities": entities}], chunk=chunk)
    # task-102 一：两条可追溯性断言（剪贴自检 + 推导自检）——新字段进来都过一遍。
    traceability = traceability_assert(entities, rules, norm_chunk,
                                       trace_hits=ctx.get("trace_hits"), warnings=warnings)
    # task-102 三：跨度报警——chunk 内跨度 > 24h 且含相对时间 => chunk 级基准不可信
    span_alarm = span_alarm_of(entities, rules, norm_chunk, warnings=warnings)

    # task-66 ③：密度体检（不当闸；每 chunk 各表行数 + 标识数/千字符）
    # task-76 ②：成本侧成对字段 calls_per_1k_chars = pieces / chars × 1000
    density = density_stats(entities, len(text), content_gates.get("identity_table"),
                            pieces=len(pieces))

    # task-77 ②：覆盖式断言（清单内漏抽=红，清单外=warning）。跑在计数之前，
    # 这样 warning 文本/monitor 里都看得到「多抽了哪几条」。
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

    # 三、两个监控项：warning 率 <5%、拒片率 <10%；超了明确说回来查 prompt 或拆片
    # task-104 五：每项同时给 (分子, 分母)，分母=0 => unverified（未检验），不算通过
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
        "content_gates": content_gates,   # task-66 ①②：红没红、丢了几行（闸，不是体检）
        "identity_generic": generic_gate,  # task-70 ④：通称不具名（.bak / 纯后缀词）
        "fk_invariants": fk_invariants,    # task-70 ②：外键不变量（R12 可选字段）
        "time_norm_gate": time_norm_gate,  # task-78：时间列可复算（不符=红，不是 warning）
        "precision_gate": precision_gate,  # task-107 五b：精度与 time_norm 自洽（不符=红）
        # task-107 二：quote 子串校验（降级=红，看得见）；task-108：这里先放 gate_raw，
        # 函数结尾会换成 {gate_raw, gate_final} 两份（判据只看 gate_final）。
        "quote_substring_gate": quote_sub,
        "e2e": {"coverage": e2e_coverage, "report": e2e_report},  # task-107 三：端到端断言
        "traceability_gate": traceability,  # task-102 一：剪贴自检 + 推导自检
        "span_alarm": span_alarm,          # task-102 三：chunk 级基准可信度
        "time_base": {"source": base_source, "granularity": base_granularity,
                      "base": base_iso},
        "density": density,               # task-66 ③：只体检，不当闸
        "expected_coverage": coverage,    # task-77 ②：覆盖式断言（漏=红 / 多=warning）
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
        # task-104 五：分母=0 不是「通过」，是「没检验」——必须说出来，不能静默显示绿
        warn_note(warnings, "gate_unverified",
                  "warning 率闸未检验（分母=0：没有可统计的行）——task-104 五：分母=0 一律不算通过")
    if monitor["reject_rate_verdict"] == "fail":
        warn_note(warnings, "reject_rate_high",
                  "拒片率 %d/%d=%.2f%% 超过 %.1f%% 阈值：回来查 prompt 或拆片"
                  % (len(failed), pieces_total, (reject_rate or 0) * 100, REJECT_RATE_MAX * 100))
    elif monitor["reject_rate_verdict"] == "unverified":
        warn_note(warnings, "gate_unverified",
                  "拒片率闸未检验（分母=0：没有片）——task-104 五：分母=0 一律不算通过")

    # task-77 ②：清单内漏抽 = 红（不是「少抽一点」）——写成 chunk 级 warning，
    # 由 main() 决定退出码（2），这样产物仍然落盘、留痕里看得到漏了哪几条。
    if coverage is not None and not coverage["ok"]:
        miss = []
        for table in EXPECTED_TABLES:
            for k in coverage["tables"][table]["missing"]:
                miss.append("%s:%s" % (table, k))
        warn_note(warnings, "expected_missing",
                  "覆盖式断言报红（task-77 ②）：人工清单内漏抽 %d 条 —— %s"
                  % (coverage["missing_total"], "；".join(miss[:8])))

    # 四、quote 占比 gate（task-74 分母 A）：缺引文的行剔除分母、单列缺口，不整份作废
    quote_gate = enforce_quote_ratio(entities, schema, warnings)
    if quote_gate["quote_missing"]:
        warn_note(warnings, "rows_quote_missing",
                  "缺引文行 %d/%d（task-74）：已从 quote 闸分母剔除、逐行列在 quote_missing_rows，"
                  "不整份作废——缺的是引文，不是事实（引文缺口 %.2f%%，覆盖率 %.2f%%）"
                  % (quote_gate["quote_missing"], quote_gate["rows"],
                     quote_gate["gap_ratio"] * 100, quote_gate["ratio"] * 100))

    # task-68 ③：nearest 行标低置信、不进答案——统计完把临时标记剥掉（最终产物不留痕）
    answer = answer_stats(entities)
    fk_sum["answer"] = answer
    # task-71 ②：answer_rows 与 explicit_ratio 并列当判据——覆盖率达标 ≠ 能答题。
    # 跨表 join 的前提是「相关事件与数据行都在 answer 里」；aggregate/mention 不进答案，
    # 所以覆盖率 100% 的 chunk 也可能只剩个位数行能 join。
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

    # task-73 ①：没有锚点的行不静默消失（清单进结果 + chunk 级提示）
    if unlocated:
        warn_note(warnings, "rows_unlocated",
                  "%d 行既无 抽取位置、无 quote，也没有能回原文的文字锚点（task-73 ①）："
                  "行没静默丢，清单见 unlocated_rows；缺的是引文，不是事实" % len(unlocated))
    monitor["unlocated_rows"] = len(unlocated)
    # task-74：引文缺口单列（与覆盖率分开记，不混成一个数）
    monitor["quote_missing_rows"] = len(quote_gate.get("quote_missing_rows") or [])
    monitor["quote_gap_ratio"] = quote_gate.get("gap_ratio")

    # task-108（审查端 P349）：闸分两份输出——上面 quote_sub 是 gate_raw（清洗前，含被丢掉的
    # quote.归一化 这类），这里在**最终产物**上再复算一次 = gate_final，判据只看它。
    # 复算必须放在所有丢行/丢字段/降级之后（即 entities 已经是落盘状态），否则又回到「跑清洗前」。
    quote_final = quote_substring_report(entities, rules, text)
    quote_gates = quote_substring_gates(quote_sub, quote_final)
    monitor["quote_substring_gate"] = quote_gates
    if quote_final["red"]:
        warn_note(warnings, "quote_gate_final_red",
                  "gate_final 报红（task-108）：最终产物里仍有 %d 处 quote 不是原文子串"
                  "（gate_raw invalid=%d，清洗没做干净）——明细见 "
                  "monitor.quote_substring_gate.gate_final"
                  % (quote_final["invalid"], quote_sub.get("invalid", 0)))

    # task-109（审查端 P350）：所有闸统一收口——计数器一律 int、None 落成 0 + unverified、
    # 每个闸补 report_line。没给 --expected 清单时覆盖断言是「没数」，明写 unverified 而不是 None。
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
    """task-63 ②：fk_method 必须进构建日志（nearest 占比高不能默默发生）。

    task-72 ②：日志落哪个目录由 --log-dir 定（默认仍是 out/build_log.jsonl）；
    条目里带 out / log / repeat，日志在哪不再靠口头约定。
    """
    target = pathlib.Path(path) if path else BUILD_LOG
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 日志写不进去不该吞掉抽取结果


def build_log_entry(chunk, result, out_path, log_path, repeat, stability=None):
    """task-72 ②：日志条目自带落盘位置（out / log / repeat），方便核「日志在哪」。"""
    entry = {"chunk": chunk["source_chunk_id"], "source": result["source"],
             "pieces": result["pieces"],
             # task-75：参数一旦变（6000 -> 1200），日志要能解释 prompt sha 为什么变
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
        # task-72 ①：重复跑同一 chunk（temperature 固定 0，deepseek_api.call 的默认值）
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
    # task-72 ②：日志落到 --log-dir（与 --out 同目录即「一处可查」）；条目自带 out/log/repeat
    log_path = (pathlib.Path(args.log_dir) / "build_log.jsonl") if args.log_dir else BUILD_LOG
    for k, res in enumerate(results):
        append_build_log(build_log_entry(chunk, res, out_path, log_path, k + 1,
                                         stability if k == 0 else None), log_path)
    # task-72 ③：落每行作答索引，供 --pair-check 逐题验成对覆盖
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
    # task-109（审查端 P350）：计数器还有 None、或有闸没有 report_line = 这一格没做完
    # （产物照落盘，退出码 2——None 会被人当成「没问题」，所以必须硬失败）。
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
    # task-109：没给 --expected 时 cov 是 {"unverified": true, ...}（不是 None），所以判据认 ok is False，
    # 不能写 `not cov.get("ok")`（那会把「没数」当「没过」）。
    if cov and cov.get("ok") is False:
        miss = []
        for table in EXPECTED_TABLES:
            for k in cov["tables"][table]["missing"]:
                miss.append("%s:%s" % (table, k))
        print("EXPECTED COVERAGE FAILED: 人工清单内漏抽 %d 条：%s"
              % (cov["missing_total"], "；".join(miss[:8])), file=sys.stderr)
        return 2
    # task-78：time_norm 复算不符/解不出 = 红（退出码 2）。产物已落盘，留痕里能查到哪一行：
    # 时间列是「取最新」的唯一依据，重算对不上等于键错了，不能当 warning 混过去。
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
    # task-107 五(b)：精度不自洽 = 红（退出码 2）；task-107 三：端到端断言答不对 = 红
    pg = result.get("precision_gate") or {}
    e2e = (result.get("e2e") or {}).get("report") or {}
    if pg.get("red") or (e2e.get("total") and not e2e.get("ok")):
        print("TASK-107 GATES FAILED: precision 不自洽 %d 行；e2e 断言 %d/%d 红：%s"
              % (pg.get("mismatch", 0), e2e.get("red", 0), e2e.get("total", 0),
                 "；".join("%s 存 %r 应为 %r" % (m["where"], m["stored"], m["expected"])
                          for m in (pg.get("rows") or [])[:5])),
              file=sys.stderr)
        return 2
    # task-108（审查端 P349）：判据只看 gate_final —— 最终产物里还剩非原文 quote = 红（退出码 2）。
    # gate_raw 红不算不过（那是「模型这次给了改写、被清洗掉了」的诊断信息）。
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
