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
- WHO 疫情通报（v12）：source = 原件 JSON pointer（/Title、/Overview……）+ 该节内的块序号；HTML 转换后的
  文字不是原件子串，不给字符偏移。

不编造：没有正文就返回空 text，并在 warnings 里说明（no_body / no_abstract / summary_missing ...），不补写。
"""

from __future__ import annotations

import json
import re
from json.decoder import scanstring

# Any rule change is a new version: results are stored per processor version and earlier artifacts are kept
# (what each version changed: docs/decisions/0002-processor-versions.md).
PROCESSOR_VERSION = "datafabric.clean_text.v12"

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


# 站点模板：整块删除（删整块，每段在原文里仍是连续区间，偏移照样回指）。
# lead：标题之后、第一段实质正文之前的导航块（每行都匹配其中一条，或整块不超过 20 字）；
# footer：在正文前四分之一之后，第一个以这些标记开头的块及其后全部（版权、订阅、站点导航尾巴）。
SITE_TEMPLATES = {
    "fiercepharma": {
        "lead": [re.compile(p) for p in (
            r"Fierce\s+(?:Pharma|Biotech|Healthcare|Life Sciences).*",
            r"(?:Advertise|About Us|Subscribe|Resources|Events|Podcasts|Webinars|Whitepapers|Survey)",
            r"(?:Pharma|Vaccines|Asia|Manufacturing|Facilities|Recalls|Warning Letters|Marketing)",
            r"(?:Cell & Gene Therapy|Drug Delivery|Executives|Special Reports|Fierce 50)",
            r"(?:Oncology|Supply Chain|AI and Machine Learning|Awards|Outsourcing Awards)",
            r"(?:Innovation Awards|Pharma Marketing Awards|Industry Events)",
        )],
        "footer": ("Let Google know we are your trusted source.", "©", "All rights reserved", "Terms of use",
                   "Privacy Policy"),
        "footer_lines": ("Connect", "Let's Talk"),  # 整行就是这个词（页尾导航栏）
    },
}
# 旧采集器把登记页改写成叙述段落，再把原文附在这个标题下（documents 库存里的 ChiCTR 全部如此）
_REWRITE_APPENDIX = re.compile(r"#{1,6}\s*原始注册信息\s*")


def _block_text(block):
    return "\n".join(n for n in (_norm_line(x) for x, _ in block[1]) if n)


def _skip_rewrite_appendix(blocks):
    """Rewritten registry pages keep only what follows their appendix heading → (blocks, dropped lines)."""
    for i, block in enumerate(blocks):
        if block[0] == "heading" and _REWRITE_APPENDIX.fullmatch(_block_text(block)):
            return blocks[i + 1:], sum(len(b[1]) for b in blocks[:i + 1])
    return blocks, 0


def _is_lead_block(block, template):
    text = _block_text(block)
    lines = [x for x in text.split("\n") if x]
    return len(text) <= 20 or all(any(p.fullmatch(x) for p in template["lead"]) for x in lines)


def _drop_lead(blocks, template):
    """The site navigation before the body (a first heading stays) → (kept heading, rest, dropped lines)."""
    keep, start, dropped = [], 0, 0
    if blocks[0][0] == "heading":
        keep, start = [blocks[0]], 1
    while start < len(blocks) and _is_lead_block(blocks[start], template):
        dropped += len(blocks[start][1])
        start += 1
    return keep, blocks[start:], dropped


def _drop_footer(rest, template):
    """The site footer: from the first footer block past the first quarter of the text → (rest, dropped lines)."""
    total = sum(len(_block_text(b)) for b in rest)
    seen = 0
    for j, block in enumerate(rest):
        text = _block_text(block)
        if seen > total // 4 and (text.startswith(template["footer"])
                                  or text.split("\n")[0] in template.get("footer_lines", ())):
            return rest[:j], sum(len(b[1]) for b in rest[j:])
        seen += len(text)
    return rest, 0


def _apply_templates(blocks, site):
    """返回（保留的块, 删掉的行数）。"""
    blocks, dropped = _skip_rewrite_appendix(blocks)
    template = SITE_TEMPLATES.get(site or "")
    if not template or not blocks:
        return blocks, dropped
    keep, rest, lead = _drop_lead(blocks, template)
    rest, footer = _drop_footer(rest, template)
    return keep + rest, dropped + lead + footer


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
    source_id 是站点（documents 库存按原站）：SITE_TEMPLATES 里有它的，再按站点模板整块删导航和页尾。
    documents 库存里被旧采集器“改写成叙述段落 + 原文附录”的登记页，只保留“原始注册信息”之后的原文部分。
    """
    scanner = _MarkdownBlocks()
    for line, offset in _lines_with_offsets(text or ""):
        scanner.feed(line, offset)
    scanner.flush()  # 没有闭合的 <table> 也照样整块保留到结尾，不丢内容
    blocks, dropped = _apply_templates(scanner.blocks, source_id)
    body, segments = _markdown_segments(blocks)
    warnings = [] if body else ["no_body"]
    return _result(body, segments, detect_language(body), scanner.removed + dropped, warnings)


class _MarkdownBlocks:
    """The blocks of a Markdown text, line by line: blocks[i] = [kind, [(line, offset), ...]]; removed counts the
    boilerplate lines left out."""

    def __init__(self):
        self.blocks = []
        self.current = None
        self.removed = 0
        self.in_html_table = False

    def flush(self):
        if self.current is not None and self.current[1]:
            self.blocks.append(self.current)
        self.current = None

    def feed(self, line, offset):
        norm = _norm_line(line)
        if self.in_html_table:
            self._html_table_line(line, offset)
        elif not norm:
            self.flush()
        elif _HTML_TABLE_OPEN.match(norm):
            self._html_table_open(line, offset)
        elif norm.startswith("|"):
            self._pipe_table_line(line, offset)
        else:
            self._text_line(line, offset, norm)

    def _html_table_line(self, line, offset):
        self.current[1].append((line, offset))
        if _HTML_TABLE_CLOSE.search(line):
            self.in_html_table = False
            self.flush()

    def _html_table_open(self, line, offset):
        self.flush()
        self.current = ["table", [(line, offset)]]
        if _HTML_TABLE_CLOSE.search(line):
            self.flush()
        else:
            self.in_html_table = True

    def _pipe_table_line(self, line, offset):
        if self.current is None or self.current[0] != "pipe_table":
            self.flush()
            self.current = ["pipe_table", []]
        self.current[1].append((line, offset))

    def _text_line(self, line, offset, norm):
        if self.current is not None and self.current[0] == "pipe_table":
            self.flush()
        if is_boilerplate(norm):
            self.removed += 1
            self.flush()
            return
        if _HEADING.match(norm):
            self.flush()
            self.blocks.append(["heading", [(line, offset)]])
            return
        is_item = bool(_LIST_ITEM.match(norm))
        if self.current is None:
            self.current = ["list" if is_item else "paragraph", []]
        elif self.current[0] == "paragraph" and is_item:
            # 段落后紧跟列表项（无空行）：列表另起一段；列表后的非列表行视为列表项的续行
            self.flush()
            self.current = ["list", []]
        self.current[1].append((line, offset))


def _markdown_segments(blocks):
    """The body (blocks joined by a blank line) and one segment per non-empty block → (body, segments)."""
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
    return "\n\n".join(parts), segments


# ---------------------------------------------------------------- 临床登记

SUMMARY_POINTER = "/protocolSection/descriptionModule/briefSummary"
# 清洗正文是语义完整的规范产物：字段一律完整保留，不按字数截断：截断会丢入组标准、终点，
# 对结果表和地点封顶会丢不良事件、分组、单位、分母、离散度。长度预算只用在索引分片、
# 展示摘要和模型阅读窗口上（store.index_publication 的分片），不用在这里。
# 结构化字段写成可读的逐行文本，不写 JSON 块。
# 唯一不进默认正文的是研究地点联系人里纯联络用途的信息：电话、分机、邮件，以及角色为 CONTACT
# （联络人）的条目（姓名与角色标签）。研究者等其他角色、角色不明的保留姓名与角色。原件保留，按 json_pointer 可读；用到排除规则的
# 产物在 warnings 里记 excluded_by_rule:<规则版本>。
CONTACT_RULE = "datafabric.site_contact_rule.v2"
CONTACT_CHANNELS = ("phone", "phoneExt", "email")


def excluded_contact_field(contact, key):
    """按规则不进正文的联络字段：电话、分机、邮件；角色为 CONTACT 的条目里的姓名和角色标签（整条只剩联络用途）。
    v2：把 CONTACT 条目的角色标签也明确列入排除（v1 实际也不写，但规则里没写明）。"""
    return key in CONTACT_CHANNELS or (key in ("name", "role") and contact.get("role") == "CONTACT")

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


def _leaf(value):
    return normalize_whitespace(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _empty(value):
    return value is None or value == "" or value == [] or value == {}


def _readable(value, path=""):
    """结构化值 → 可读的逐行文本（确定的顺序）：标量直接写；只含标量的字典写成一行“键: 值; 键: 值”；
    只含标量的列表写成一行，用“; ”分隔；含结构的列表逐项展开，项前标 [序号]；更深的嵌套用“上级.键”写路径。
    空值不写。"""
    if isinstance(value, dict):
        yield from _readable_dict(value, path)
    elif isinstance(value, list):
        yield from _readable_list(value, path)
    elif not _empty(value):
        yield (path + ": " if path else "") + _leaf(value)


def _readable_dict(value, path):
    items = sorted((k, v) for k, v in value.items() if not _empty(v))
    flat = [(k, v) for k, v in items if not isinstance(v, (dict, list))]
    if flat:  # 标量字段同一行，嵌套的字段随后逐个展开
        yield (path + " " if path else "") + "; ".join(k + ": " + _leaf(v) for k, v in flat)
    for k, v in items:
        if isinstance(v, (dict, list)):
            yield from _readable(v, path + "." + k if path else k)


def _readable_list(value, path):
    values = [v for v in value if not _empty(v)]
    if all(not isinstance(v, (dict, list)) for v in values):
        if values:
            yield (path + ": " if path else "") + "; ".join(_leaf(v) for v in values)
        return
    for i, v in enumerate(values, 1):
        yield from _readable(v, path + "[" + str(i) + "]")


def _site(site):
    """一个研究地点（全部字段），联系人按 CONTACT_RULE 去掉纯联络信息 → (地点, 去掉的字段数)。"""
    if not isinstance(site.get("contacts"), list):
        return site, 0
    contacts, removed = [], 0
    for contact in site["contacts"]:
        if not isinstance(contact, dict):
            contacts.append(contact)
            continue
        kept = {k: v for k, v in contact.items() if not excluded_contact_field(contact, k)}
        removed += len(contact) - len(kept)
        if kept:
            contacts.append(kept)
    return {**site, "contacts": contacts}, removed


def _locations(value):
    """研究地点：先写一行全部国家及各自地点数（由地点名单算出的汇总，便于按国家检索），再逐个地点写全部字段
    （联系人按 CONTACT_RULE）→ (渲染用的值, 按规则去掉的字段数)。"""
    if not isinstance(value, list):
        return value, 0
    sites, removed = [], 0
    for site in value:
        if isinstance(site, dict):
            site, n = _site(site)
            sites.append(site)
            removed += n
    countries = {}
    for site in sites:
        if site.get("country"):
            countries[site["country"]] = countries.get(site["country"], 0) + 1
    ranked = sorted(countries.items(), key=lambda item: (-item[1], item[0]))
    return {"countries": ["%s (%d)" % item for item in ranked], "sites": sites}, removed


def _render_field(key, value):
    """一个字段的正文（完整，不截断）：标量或只有一行就写在“字段名 field_key: ”后面；多行另起，逐行写。
    → (正文, 按规则去掉的字段数)。"""
    value, removed = _locations(value) if key == "locations" else (value, 0)
    lines = list(_readable(value))
    if not lines:
        return "", removed
    return (lines[0] if len(lines) == 1 else "\n" + "\n".join(lines)), removed


def clean_registry(facts: dict, locators: dict, *, summary: str | None = None) -> dict:
    """临床登记（ClinicalTrials.gov）已投影的字段 → 逐字段的正文。

    每个存在的字段一段，以“字段名 field_key:”开头：标量写在同一行；结构化的值写成可读的逐行文本
    （见 _readable，v7 起不再写 JSON）。值为 None 视同字段缺失，直接不写（不写 0 / null）。
    字段一律完整保留，不截断；只有研究地点联系人里纯联络用途的信息不进正文（CONTACT_RULE），
    用到时 warnings 记 excluded_by_rule:<规则版本>。
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
        rendered, removed = _render_field(key, value)
        if removed and "excluded_by_rule:" + CONTACT_RULE not in warnings:
            warnings.append("excluded_by_rule:" + CONTACT_RULE)
        if not rendered.strip():
            continue
        locator = locators.get(key)
        pointer = locator.get("json_pointer") if isinstance(locator, dict) else None
        add(REGISTRY_LABELS.get(key, _DEFAULT_LABEL) + " " + key + ":" + ("" if rendered.startswith("\n") else " ")
            + rendered, "field", pointer)
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
# 上下标不能直接去标签：“10<sup>-3</sup>”变成“10-3”、“IC<sub>50</sub>”变成“IC50”会改含义。
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


# ---------------------------------------------------------------- WHO 疫情通报（v12）

# v11 读的是采集投影的 clean_text：标题是普通首行，各节 HTML 由 get_text 压平，<table> 的单元格被拼成一串，
# 行列关系丢失（S18 mpox 的 /Overview）。v12 直接从原件 JSON 清洗：标题写成一级 markdown 标题；各节按 HTML
# 的块写出（段落、列表项、标题），<table> 写成 markdown 管道表格（表头行 + 分隔行 + 数据行），表题在表前单独
# 一段。节的取舍与投影相同（WHO_SECTIONS），不新增也不删内容；不在任何块里的文字照样成段，不丢。
# 转换后的文字不是原件字符串的子串，所以每段的 source 是原件 JSON pointer 加该节内的块序号，不是字符偏移。
WHO_SECTIONS = ("Summary", "Overview", "Epidemiology", "Response", "Advice", "Assessment")
_INLINE_TAGS = {"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "font", "i", "kbd", "mark",
                "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var", "wbr", "br", "img"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _who_text(node) -> str:
    return normalize_whitespace(node.get_text(" ", strip=True)).replace("\n", " ")


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _span(cell, name):
    try:
        return max(1, min(int(cell.get(name) or 1), 50))
    except (TypeError, ValueError):
        return 1


def _who_table(table) -> tuple[str | None, str, int]:
    """(表题或 None, markdown 管道表格, 占位格数)。单元格按真实网格排：rowspan 从上一行延续下来的格子、
    colspan 向右展开的格子都写成空占位格，所以每个值留在它自己的列（只保住词序不够，
    数字不能错到别的指标列）。占位格不重复文字，也不编；数量随段一起记下，下游知道表里有合并格。"""
    caption = table.find("caption")
    caption_text = _who_text(caption) if caption else None
    rows, carry, placeholders = [], {}, 0  # carry: column -> rows still spanned from above
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue  # 嵌套表格的行属于里面那张表（它在单元格文字里，不丢）
        row, col = [], 0
        cells = iter(tr.find_all(["th", "td"], recursive=False))

        def held(c):
            nonlocal placeholders
            if c in carry:
                carry[c] -= 1
                if not carry[c]:
                    del carry[c]
                placeholders += 1
                return True
            return False

        for cell in cells:
            while held(col):
                row.append("")
                col += 1
            colspan, rowspan = _span(cell, "colspan"), _span(cell, "rowspan")
            for k in range(colspan):
                row.append(_cell(_who_text(cell)) if k == 0 else "")
                placeholders += k > 0
                if rowspan > 1:
                    carry[col] = rowspan - 1
                col += 1
        while any(c >= col for c in carry):  # spans from above to the right of this row's own cells
            held(col)  # a spanned column counts its placeholder; any other column here is padding
            row.append("")
            col += 1
        if row:
            rows.append(row)
    if not rows:
        return caption_text, "", 0
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return caption_text, "\n".join(lines), placeholders


def _who_blocks(html: str):
    """一节 HTML 的块，按文档顺序：[(kind, text)]，kind 为 heading / paragraph / list / table。"""
    from bs4 import BeautifulSoup, Comment, NavigableString

    reader = _WhoBlockReader(Comment, NavigableString)
    reader.walk(BeautifulSoup(html, "html.parser"))
    reader.flush()
    return reader.out


def _who_text_block(kind, text, prefix=""):
    return [(kind, prefix + text)] if text else []


def _who_list_blocks(child):
    out = []
    for li in child.find_all("li", recursive=False):
        out += _who_text_block("list", _who_text(li), "- ")
    rest = [c for c in child.children if getattr(c, "name", None) != "li"]
    for c in rest:  # 列表里不在 <li> 中的文字也不丢
        text = c.get_text(" ", strip=True) if hasattr(c, "get_text") else str(c).strip()
        if text:
            out.append(("paragraph", normalize_whitespace(text).replace("\n", " ")))
    return out


def _who_table_blocks(child):
    if child.find("table") is not None:
        # 嵌套表格：行列关系没有可靠的展开方式，不冒充结构化表格；可读文字整块保留（含表题）
        return _who_text_block("table_degraded", _who_text(child))
    caption, table, placeholders = _who_table(child)
    out = []
    if caption:
        out.append(("paragraph", caption))
    if table:
        out.append(("table", table, placeholders))
    return out


def _who_element_blocks(child):
    """The blocks of one block-level element; None for a container, which is read block by block
    (div / section / blockquote / figure / unknown: a table inside a figure is still a table)."""
    name = child.name
    if name in _HEADING_TAGS:
        return _who_text_block("heading", _who_text(child), "#" * max(2, int(name[1])) + " ")
    if name in ("p", "figcaption"):
        return _who_text_block("paragraph", _who_text(child))
    if name in ("ul", "ol"):
        return _who_list_blocks(child)
    if name == "li":
        return _who_text_block("list", _who_text(child), "- ")
    if name == "table":
        return _who_table_blocks(child)
    return None


class _WhoBlockReader:
    """Walks one section's HTML in document order; inline text between blocks becomes a paragraph."""

    def __init__(self, comment_type, string_type):
        self.comment_type, self.string_type = comment_type, string_type
        self.out, self.inline = [], []

    def flush(self):
        text = normalize_whitespace(" ".join(self.inline)).replace("\n", " ")
        self.inline.clear()
        if text:
            self.out.append(("paragraph", text))

    def walk(self, node):
        for child in node.children:
            if isinstance(child, self.comment_type):
                continue
            if isinstance(child, self.string_type):
                if child.strip():
                    self.inline.append(str(child))
                continue
            if child.name in _INLINE_TAGS:
                text = child.get_text(" ", strip=True)
                if text:
                    self.inline.append(text)
                continue
            self.flush()
            blocks = _who_element_blocks(child)
            if blocks is None:
                self.walk(child)
            else:
                self.out.extend(blocks)


def clean_who(raw_json: str) -> dict:
    """WHO DON 原件（JSON 字符串）→ 正文：一级标题 + 各节的块。每段 source = {json_pointer, block}。

    不编造：缺标题记 no_title，没有任何正文记 no_body；节不是字符串的记 section_not_text 并跳过该节。
    """
    data = json.loads(raw_json)
    parts, segments, cursor, warnings = [], [], 0, []

    def add(text, kind, pointer, block, placeholders=0):
        nonlocal cursor
        if parts:
            cursor += 2
        segments.append({"start": cursor, "end": cursor + len(text), "kind": kind,
                         "source": {"json_pointer": pointer, "within": "raw", "block": block}})
        if placeholders:
            segments[-1]["placeholder_cells"] = placeholders  # empty cells standing for merged (spanned) ones
        parts.append(text)
        cursor += len(text)

    title = normalize_whitespace(data.get("Title") or "").replace("\n", " ") if isinstance(data.get("Title"), str) \
        else ""
    if title:
        add("# " + title, "heading", "/Title", 0)
    else:
        warnings.append("no_title")
    for key in WHO_SECTIONS:
        value = data.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            warnings.append("section_not_text:" + key)
            continue
        for i, (kind, text, *extra) in enumerate(_who_blocks(value)):
            add(text, kind, "/" + key, i, *extra)
    body = "\n\n".join(parts)
    if len(parts) <= (1 if title else 0):
        warnings.append("no_body")
    if any(s["kind"] == "table_degraded" for s in segments):
        warnings.append("nested_table_degraded")
    return _result(body, segments, detect_language(body), 0, warnings)
