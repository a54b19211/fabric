"""加工层：来源版本 → 清洗后的正文（设计 v3 §3.3“解析与清洗”）。

结果按（来源版本, 处理器版本）存为派生数据，所以这里是纯函数：不联网、不碰库，同样输入永远同样输出。

清洗只做两件事：
1. 删整行的模板/导航（面包屑、打印本页、分享到、上一篇/下一篇、页码、Cookie 横幅……），
   模式见 BOILERPLATE_PATTERNS；
   一行里只要还有别的内容就保留——宁可留一行噪声，也不能丢一句“未见”“不推荐”“【更正】”。
2. 规范空白：行内连续空白（含全角空格、不换行空格、制表符）→ 一个半角空格，去行首尾空白。
   其它字符一律不改（不做全角转半角、不改大小写），这样下游引用的原话还能在原件里逐字找到。

每段都带回指原件的定位：
- Markdown / 纯文本：source = 原输入字符串里的 [start, end)；
  normalize_whitespace(原文[start:end]) == 段文本（测试逐段验证）。
- 临床登记：source = JSON pointer（来自投影时记下的 locators）。
- 文献：source = 原始 JSON 字符串里标题/摘要字符串字面量的 [start, end)（不含两侧引号），外加 JSON pointer。

不编造：没有正文就返回空 text，并在 warnings 里说明（no_body / no_abstract / summary_missing ...），不补写。
"""

from __future__ import annotations

import json
import re
from json.decoder import scanstring

PROCESSOR_VERSION = "datafabric.clean_text.v3"

# ---------------------------------------------------------------- 空白与语言

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_INLINE_SPACE = re.compile(r"\s+")  # Python 的 \s 含 　、 、\t 等 Unicode 空白


def _norm_line(line: str) -> str:
    return _INLINE_SPACE.sub(" ", line).strip()


def normalize_whitespace(text: str) -> str:
    """逐行规范空白并丢掉空行，行之间用 \\n。

    公开出来是为了让调用方/测试能验证：对原文 [source.start, source.end) 做同样处理，得到的就是段文本。
    """
    lines = (_norm_line(x) for x in _LINE_BREAK.split(text))
    return "\n".join(x for x in lines if x)


_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")
# 一个汉字大致相当于 3 个拉丁字母的信息量（英文平均词长约 5、中文词平均约 1.5 字），按此折算后再比占比。
_LATIN_PER_HAN = 3.0
_ZH_MIN_SHARE = 0.7
_EN_MAX_SHARE = 0.3


def detect_language(text: str) -> str:
    """按汉字与拉丁字母的折算占比判断语言；数字、符号不计。没有任何文字 → unknown。

    中文公告里夹几个英文缩写（ALT、ULN、mg/kg）仍算 zh；两种文字都占相当比例才算 mixed。
    """
    han = len(_HAN.findall(text or ""))
    latin = len(_LATIN.findall(text or "")) / _LATIN_PER_HAN
    if han + latin == 0:
        return "unknown"
    share = han / (han + latin)
    if share >= _ZH_MIN_SHARE:
        return "zh"
    if share <= _EN_MAX_SHARE:
        return "en"
    return "mixed"


def _result(text, segments, language, removed_lines, warnings):
    return {
        "processor_version": PROCESSOR_VERSION,
        "text": text,
        "segments": segments,
        "language": language,
        "removed_lines": removed_lines,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 模板行

# 面包屑的分隔符与每一节：每节不得含句读（，。；！？等），这样“首页 > 新闻 发布了通报，未见死亡病例。”这种
# 真正的句子不会被当成面包屑。
_SEP = r"[>＞»›→]"
_CRUMB = r"[^>＞»›→，。；！？,;!?]{1,40}"
_LOCATION = r"(?:当前位置|您的位置|你的位置|您现在的位置|所在位置)\s*[：:]\s*"
_SENTENCE_END = r"[，。；！？,;!?]"

# 工具栏按钮：一行全由这些按钮（可带【】[]（）括号）组成才删。
_TOOLBAR_WORDS = (r"(?:打印本页|打印此页|打印|关闭窗口|关闭本页|关闭|收藏本页|加入收藏|收藏"
                  r"|返回顶部|返回列表|返回)")
_FONT = r"[【\[]?\s*字体\s*[：:]?\s*(?:[【\[]?\s*[大中小]\s*[】\]]?\s*){2,3}[】\]]?"
_TOOLBAR_TOKEN = r"(?:[【\[（(]\s*" + _TOOLBAR_WORDS + r"\s*[】\]）)]|" + _TOOLBAR_WORDS + r"|" + _FONT + r")"

# 每条都对“规范空白后的整行”做 fullmatch：只匹配整行就是模板的情况。
BOILERPLATE_PATTERNS = [
    # 面包屑：“首页 > 新闻中心 > 药品监管”“当前位置：首页 > 公告”，以及单独一行“首页”导航链接
    re.compile(r"(?:" + _LOCATION + r")?(?:首页|主页|网站首页)(?:\s*" + _SEP + r"\s*" + _CRUMB + r")*\s*"
               + _SEP + r"?"),
    # 不以首页开头、但带“当前位置：”前缀的面包屑：“当前位置：新闻中心 > 公告”
    re.compile(_LOCATION + _CRUMB + r"(?:\s*" + _SEP + r"\s*" + _CRUMB + r")*"),
    # 工具栏：“【字体：大 中 小】 打印本页 关闭窗口”“[关闭窗口]”“字体：[大] [中] [小]”
    re.compile(_TOOLBAR_TOKEN + r"(?:[\s|｜/·]*" + _TOOLBAR_TOKEN + r")*"),
    # 分享栏：“分享到：微信 微博”；尾部不得有句读，避免误删正文句子
    re.compile(r"分享到\s*[：:]?[^，。；！？,;!?]{0,60}"),
    # 移动端二维码提示：“扫一扫在手机打开当前页”
    re.compile(r"(?:手机|微信)?扫一扫\s*"
               r"(?:在手机(?:上)?(?:打开|查看|阅读)(?:当前页|本页)|分享到(?:微信|朋友圈))?"),
    # 上一篇/下一篇导航：必须带冒号；“上一篇研究显示……”这种句子不删
    re.compile(r"(?:上一篇|下一篇|上一条|下一条)\s*[：:][^。！？]{0,100}"),
    # 无障碍跳转链接
    re.compile(r"(?i)(?:skip to (?:main )?content|skip navigation|跳转到主要内容|跳过导航)"),
    # Cookie 横幅：以“We use cookies / This site uses cookies”开头的短句，或单独的 Cookie 设置按钮
    re.compile(r"(?i)(?:we use cookies|this (?:web)?site uses cookies|本(?:网)?站使用\s*cookies?)\b.{0,200}"),
    re.compile(r"(?i)(?:accept (?:all )?cookies|reject (?:all )?cookies|manage cookies"
               r"|cookie (?:settings|preferences|policy))"),
    # 页码行：“— 3 —”“- 12 -”“第 3 页 共 10 页”“Page 3 of 10”；单独一个数字不删（可能是正文里的编号）
    re.compile(r"[—\-–－]{1,3}\s*\d{1,4}\s*[—\-–－]{1,3}"),
    re.compile(r"第\s*\d{1,4}\s*页(?:\s*[，,/、]?\s*共\s*\d{1,4}\s*页)?"),
    re.compile(r"(?i)page\s+\d{1,4}(?:\s+of\s+\d{1,4})?"),
]


def is_boilerplate(line: str) -> bool:
    norm = _norm_line(line)
    return bool(norm) and any(p.fullmatch(norm) for p in BOILERPLATE_PATTERNS)


# ---------------------------------------------------------------- Markdown

_HEADING = re.compile(r"#{1,6}(?:\s|$)")
_LIST_ITEM = re.compile(r"(?:[-*+•·]|\d{1,3}[.)、]|[（(]\d{1,3}[)）])\s")
_HTML_TABLE_OPEN = re.compile(r"(?i)<table\b")
_HTML_TABLE_CLOSE = re.compile(r"(?i)</table\s*>")


def _lines_with_offsets(text):
    """按 \\r\\n / \\r / \\n 切行，返回 (行内容, 内容在原文中的起点)；行内容不含换行符。"""
    out, pos = [], 0
    for m in _LINE_BREAK.finditer(text):
        out.append((text[pos:m.start()], pos))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], pos))
    return out


def _content_span(line, offset):
    """一行去掉首尾空白后在原文里的 [start, end)。"""
    lead = len(line) - len(line.lstrip())
    return offset + lead, offset + len(line.rstrip())


def clean_markdown(text: str, *, source_id: str | None = None) -> dict:
    """网页/公告/附件解析出的 Markdown 或纯文本。

    按空行分块；标题行单独成段；列表、表格（| 管道表格与 <table>…</table> HTML 表格）整块保留。
    表格块内部不做模板行判断：单元格里出现“首页”“打印本页”是数据，不是导航。
    普通块中间夹了一行模板：删掉这行，并在此处断段——这样每段在原文里仍是连续区间，偏移可逐字回指。
    source_id 暂未使用，留给以后按站点追加模板模式；现在所有来源用同一套保守模式。
    """
    del source_id
    blocks = []  # 每块：[kind, [(line, offset), ...]]
    current = None
    removed = 0

    def flush():
        nonlocal current
        if current is not None and current[1]:
            blocks.append(current)
        current = None

    in_html_table = False
    for line, offset in _lines_with_offsets(text or ""):
        norm = _norm_line(line)
        if in_html_table:
            current[1].append((line, offset))
            if _HTML_TABLE_CLOSE.search(line):
                in_html_table = False
                flush()
            continue
        if not norm:
            flush()
            continue
        if _HTML_TABLE_OPEN.match(norm):
            flush()
            current = ["table", [(line, offset)]]
            if _HTML_TABLE_CLOSE.search(line):
                flush()
            else:
                in_html_table = True
            continue
        if norm.startswith("|"):
            if current is None or current[0] != "pipe_table":
                flush()
                current = ["pipe_table", []]
            current[1].append((line, offset))
            continue
        if current is not None and current[0] == "pipe_table":
            flush()
        if is_boilerplate(norm):
            removed += 1
            flush()
            continue
        if _HEADING.match(norm):
            flush()
            blocks.append(["heading", [(line, offset)]])
            continue
        is_item = bool(_LIST_ITEM.match(norm))
        if current is None:
            current = ["list" if is_item else "paragraph", []]
        elif current[0] == "paragraph" and is_item:
            # 段落后紧跟列表项（无空行）：列表另起一段；列表后的非列表行视为列表项的续行
            flush()
            current = ["list", []]
        current[1].append((line, offset))
    flush()  # 没有闭合的 <table> 也照样整块保留到结尾，不丢内容

    parts, segments, cursor = [], [], 0
    for kind, lines in blocks:
        seg_text = "\n".join(n for n in (_norm_line(x) for x, _ in lines) if n)
        if not seg_text:
            continue
        nonblank = [(x, o) for x, o in lines if _norm_line(x)]
        src_start = _content_span(*nonblank[0])[0]
        src_end = _content_span(*nonblank[-1])[1]
        if parts:
            cursor += 2
        segments.append({
            "start": cursor,
            "end": cursor + len(seg_text),
            "kind": "table" if kind == "pipe_table" else kind,
            "source": {"start": src_start, "end": src_end},
        })
        parts.append(seg_text)
        cursor += len(seg_text)
    body = "\n\n".join(parts)
    warnings = [] if body else ["no_body"]
    return _result(body, segments, detect_language(body), removed, warnings)


# ---------------------------------------------------------------- 临床登记

SUMMARY_POINTER = "/protocolSection/descriptionModule/briefSummary"
# v2：清洗正文是检索（WeKnora 分块）和抽取的输入，入组标准、终点、地点等长字段必须完整保留；
# v1 的 4000 字截断在真实 CT 数据上截了 1796/4496 个版本的关键内容，只在极端情况下才截。
MAX_FIELD_CHARS = 200000

# 固定阅读顺序：先看状态与终止原因，再看设计、时间、人群与终点，最后是申办方与结果；其余字段按键名排序附后。
REGISTRY_ORDER = [
    "status", "why_stopped", "phase", "study_type", "design_info", "enrollment", "enrollment_type",
    "start_date", "start_date_type", "primary_completion_date", "primary_completion_date_type",
    "completion_date", "completion_date_type", "conditions", "interventions", "arm_groups",
    "primary_outcomes", "secondary_outcomes", "eligibility", "lead_sponsor", "collaborators",
    "has_results", "results_first_posted",
]

# 字段的中文名只用于阅读；不得含空格（段文本按“字段名 field_key: value”解析）。
REGISTRY_LABELS = {
    "trial_id": "登记号",
    "status": "状态",
    "why_stopped": "终止原因",
    "phase": "分期",
    "study_type": "研究类型",
    "design_info": "设计",
    "enrollment": "入组人数",
    "enrollment_type": "入组人数类型",
    "start_date": "开始日期",
    "start_date_type": "开始日期类型",
    "primary_completion_date": "主要完成日期",
    "primary_completion_date_type": "主要完成日期类型",
    "completion_date": "完成日期",
    "completion_date_type": "完成日期类型",
    "conditions": "适应症",
    "interventions": "干预措施",
    "arm_groups": "研究组",
    "primary_outcomes": "主要终点",
    "secondary_outcomes": "次要终点",
    "eligibility": "入选标准",
    "lead_sponsor": "主要申办方",
    "collaborators": "合作方",
    "has_results": "有结果",
    "results_first_posted": "结果首次发布",
    "results_first_posted_type": "结果首次发布类型",
    "organization_study_id": "机构研究编号",
    "secondary_ids": "其他编号",
    "official_title": "正式标题",
    "locations": "研究地点",
    "results_participant_flow": "结果-受试者流程",
    "results_baseline": "结果-基线特征",
    "results_outcome_measures": "结果-终点测量",
    "results_adverse_events": "结果-不良事件",
}
_DEFAULT_LABEL = "其他字段"


def _strings_in(value):
    """收集值里的所有字符串（含 dict 的值），用于语言判断——不把字段名、中文标签算进去。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings_in(v)


def clean_registry(facts: dict, locators: dict, *, summary: str | None = None) -> dict:
    """临床登记（ClinicalTrials.gov）已投影的字段 → 逐字段的正文。

    每个存在的字段一段“字段名 field_key: 紧凑 JSON”；值为 None 视同字段缺失，直接不写（不写 0 / null）。
    值超过 MAX_FIELD_CHARS 字符就截断，并在行尾写明“…（共 N 字符，见原件）”，同时记 field_truncated:<key>。
    """
    facts = facts or {}
    locators = locators or {}
    warnings = []
    parts, segments, cursor = [], [], 0
    lang_texts = []

    def add(line, kind, pointer):
        nonlocal cursor
        if parts:
            cursor += 1
        segments.append({"start": cursor, "end": cursor + len(line), "kind": kind,
                         "source": {"json_pointer": pointer}})
        parts.append(line)
        cursor += len(line)

    summary_text = normalize_whitespace(summary) if isinstance(summary, str) else ""
    if summary_text:
        add(summary_text, "paragraph", SUMMARY_POINTER)
        lang_texts.append(summary_text)
    else:
        warnings.append("summary_missing")

    present = [k for k, v in facts.items() if v is not None]
    ordered = [k for k in REGISTRY_ORDER if k in present]
    ordered += sorted(k for k in present if k not in REGISTRY_ORDER)
    for key in ordered:
        value = facts[key]
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(rendered) > MAX_FIELD_CHARS:
            rendered = rendered[:MAX_FIELD_CHARS] + "…（共 %d 字符，见原件）" % len(rendered)
            warnings.append("field_truncated:" + key)
        locator = locators.get(key)
        pointer = locator.get("json_pointer") if isinstance(locator, dict) else None
        add(REGISTRY_LABELS.get(key, _DEFAULT_LABEL) + " " + key + ": " + rendered, "field", pointer)
        lang_texts.extend(_strings_in(value))

    body = "\n".join(parts)
    if not body:
        warnings.append("no_body")
    return _result(body, segments, detect_language("\n".join(lang_texts)), 0, warnings)


# ---------------------------------------------------------------- 文献

# Europe PMC 的标题/摘要里常见的简单 HTML。去掉标签、保留标签内的文字：
# - 行内标签（i/b/em/strong/u/sup/sub/span/sc/small）直接去掉，不加空格，避免把一个词拆开；
# - 块级标签（p/br/h1-h6/div/li）替换为一个空格，避免“BACKGROUND</h4>Patients”粘成一个词；随后空白规范化。
# 只认白名单里的标签名，“p < 0.05”“ALT>3×ULN”这类比较符号不会被当成标签。
# 上下标不能直接去标签：“10<sup>-3</sup>”变成“10-3”、“IC<sub>50</sub>”变成“IC50”会改含义（Codex r63）。
# 成对的 sup 写成 ^(…)，sub/inf 写成 _(…)；括号总是加上，“10^(-3)”不会和“10^-3 M”的读法混淆。
# 不成对的（缺闭合、嵌套被截断）记 html_sup_sub_unpaired，提醒下游回看原件。
_INLINE_TAG = re.compile(r"(?i)</?(?:i|b|em|strong|u|sup|sub|span|sc|small|inf)(?:\s[^<>]*)?/?>")
_BLOCK_TAG = re.compile(r"(?i)</?(?:p|br|h[1-6]|div|li|ul|ol)(?:\s[^<>]*)?/?>")
_SCRIPT = re.compile(r"(?is)<(sup|sub|inf)(?:\s[^<>]*)?>(.*?)</\1\s*>")
_SCRIPT_TAG = re.compile(r"(?i)</?(?:sup|sub|inf)\b")


def _mark_scripts(text: str) -> str:
    def mark(m):
        inner = normalize_whitespace(_INLINE_TAG.sub("", _BLOCK_TAG.sub(" ", m.group(2))))
        if not inner:
            return ""
        return ("^(" if m.group(1).lower() == "sup" else "_(") + inner + ")"

    return _SCRIPT.sub(mark, text)


def scripts_unpaired(text: str) -> bool:
    """成对替换之后仍留有 sup/sub/inf 标签：不成对或嵌套，替换结果不可靠。"""
    return bool(_SCRIPT_TAG.search(_mark_scripts(text)))


def strip_simple_html(text: str) -> str:
    """上下标写成 ^(…)/_(…)；再去掉白名单内的其他简单 HTML 标签、保留其中文字；不解码实体（&lt; 等原样保留）。"""
    return _INLINE_TAG.sub("", _BLOCK_TAG.sub(" ", _mark_scripts(text)))


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _string_spans(raw: str) -> dict:
    """遍历 JSON 原文，记录每个字符串值的 JSON pointer → 字面量在原文中的 [start, end)（不含两侧引号）。

    做法：自己走一遍 JSON 结构，字符串用 json.decoder.scanstring 解析（它返回闭合引号之后的位置），
    数字/true/false/null 交给 JSONDecoder.raw_decode 跳过。只在 json.loads 已成功后调用，所以不再校验语法。
    重复键与 json.loads 一样以最后一次为准。
    """
    spans = {}
    decoder = json.JSONDecoder()
    n = len(raw)

    def skip_ws(i):
        while i < n and raw[i] in " \t\n\r":
            i += 1
        return i

    def value(i, pointer):
        i = skip_ws(i)
        char = raw[i]
        if char == '"':
            _, end = scanstring(raw, i + 1, True)
            spans[pointer] = (i + 1, end - 1)
            return end
        if char == "{":
            i = skip_ws(i + 1)
            if raw[i] == "}":
                return i + 1
            while True:
                key, i = scanstring(raw, i + 1, True)
                i = skip_ws(i) + 1  # 跳过冒号
                i = skip_ws(value(i, pointer + "/" + _escape_pointer(key)))
                if raw[i] == "}":
                    return i + 1
                i = skip_ws(i + 1)  # 跳过逗号，落到下一个键的引号
        if char == "[":
            i = skip_ws(i + 1)
            if raw[i] == "]":
                return i + 1
            index = 0
            while True:
                i = skip_ws(value(i, pointer + "/" + str(index)))
                index += 1
                if raw[i] == "]":
                    return i + 1
                i += 1
        _, end = decoder.raw_decode(raw, i)
        return end

    value(0, "")
    return spans


def clean_literature(raw_json: str) -> dict:
    """Europe PMC core 记录原件（JSON 字符串）→ 标题 + 摘要。

    只取顶层 /title 与 /abstractText（作者列表等嵌套对象里的同名键不算）。标题与摘要之间空一行。
    source 的 start/end 指向原始 JSON 里该字符串字面量去掉两侧引号后的区间：raw[start-1:end+1] 是完整字面量，
    json.loads 它即得原值；再做 strip_simple_html + normalize_whitespace 即得段文本。
    没有摘要 → no_abstract，正文只有标题；没有标题 → no_title；都没有 → 空正文 + no_body；不拿 id 等字段补位。
    """
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return _result("", [], "unknown", 0, ["invalid_json", "no_body"])
    if not isinstance(data, dict):
        return _result("", [], "unknown", 0, ["not_a_record", "no_body"])
    spans = _string_spans(raw_json)

    warnings = []
    pieces = []
    for key, kind in (("title", "title"), ("abstractText", "abstract")):
        value = data.get(key)
        if not isinstance(value, str):
            continue
        cleaned = normalize_whitespace(strip_simple_html(value))
        if not cleaned:
            continue
        if scripts_unpaired(value) and "html_sup_sub_unpaired" not in warnings:
            warnings.append("html_sup_sub_unpaired")
        start, end = spans["/" + key]
        pieces.append((cleaned, kind, {"start": start, "end": end, "json_pointer": "/" + key}))

    kinds = [p[1] for p in pieces]
    if "abstract" not in kinds:
        warnings.append("no_abstract")
    if "title" not in kinds:
        warnings.append("no_title")

    parts, segments, cursor = [], [], 0
    for cleaned, kind, source in pieces:
        if parts:
            cursor += 2
        segments.append({"start": cursor, "end": cursor + len(cleaned), "kind": kind, "source": source})
        parts.append(cleaned)
        cursor += len(cleaned)
    body = "\n\n".join(parts)
    if not body:
        warnings.append("no_body")
    return _result(body, segments, detect_language(body), 0, warnings)
