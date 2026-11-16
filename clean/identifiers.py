"""清洗层：从正文里抽取并规范化编号（设计 v3 §3.3“解析与清洗”）。

覆盖：NCT、CTR（CDE 药物临床试验登记号）、ChiCTR、EU CT 号（CTIS）、PMID、PMCID、DOI、批准文号（国药准字）、
带标签的批准文号/注册证号、受理号（CDE）、股票代码（A 股 / 北交所 / 港股）。
全仓库只有这一份编号规则：站点提取器、来源投影、图谱模型的编号格式都从这里取（NCT_ID 等）。规范化后的同一编号用来把不同来源的同一试验、论文、
药品、公司连起来——但只当同源链接和同一性候选。错连（假阳性）会把两件不相干的事并到一起，
比漏连代价大，所以每条规则都偏保守：拿不准就不出。

全角：中文资料里常见 ＮＣＴ０１２３…、“：”。这里只把全角 ASCII（U+FF01–FF5E）和全角空格（U+3000）
逐字符折成半角，结果与 NFKC 一致，而且一个字符对一个字符，所以在折叠后的文本上匹配得到的偏移就是原文
偏移，raw 直接取原文片段。不做整串 NFKC：像“①”“㎎”这类字符 NFKC 后会变成多个字符或变成数字，
既打乱偏移，又可能凭空造出编号。

raw 的取法：能让 normalize(kind, raw) 还原出 value 的原文片段。所以靠标签才成立的编号（PMID、
带“股票代码”标签的代码）raw 带上标签；DOI 带上 doi: / doi.org 前缀；受理号只取编号本身（它独立
成词时本身就成立）。

纯函数：不联网、不连库。规则改动必须升 PROCESSOR_VERSION，旧结果保留。
"""

from __future__ import annotations

import re

# v3（2026-11-16）：加 EU CT 号；同一编号在正文里出现多次只出一条。已洗过的版本用 ops/backfill_identifiers.py 补
PROCESSOR_VERSION = "datafabric.identifiers.v3"

# 编号两侧不能紧挨 ASCII 字母数字：XNCT012345678、NCT012345678（9 位）都不是 NCT
_L = r"(?<![A-Za-z0-9])"
_R = r"(?![A-Za-z0-9])"
# re.A 让 IGNORECASE 只做 ASCII 大小写折叠；否则 [a-z] 会匹配到开尔文符号 K、长 s 等，造出假编号
_FLAGS = re.A | re.I


# 原生编号的精确格式（校验结构化字段用；正文抽取用下面带边界和大小写折叠的规则）
NCT_ID = r"NCT[0-9]{8}"


def fold_fullwidth(text: str) -> str:
    """全角 ASCII 和全角空格逐字符转半角（与 NFKC 对这些字符的结果相同），长度不变。"""
    return _fold(text)


def _fold(text: str) -> str:
    """全角 ASCII 和全角空格逐字符转半角（与 NFKC 对这些字符的结果相同），长度不变。"""
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


# NCT：ClinicalTrials.gov 编号固定是 NCT + 8 位数字
_NCT = re.compile(_L + r"NCT([0-9]{8})" + _R, _FLAGS)

# CTR：CDE 药物临床试验登记号 = CTR + 4 位年份 + 4 位流水号。要求年份以 20 开头，
# 排除 CTR12345678 这类别的含义的串（比如点击率 CTR 后接数字）
_CTR = re.compile(_L + r"CTR(20[0-9]{6})" + _R, _FLAGS)

# ChiCTR：旧式 ChiCTR-类型码-8 位（类型码如 IOR、TRC、TNRC，3–4 个字母），新式 ChiCTR + 10 位
_CHICTR = re.compile(_L + r"ChiCTR(?:-([A-Z]{3,4})-([0-9]{8})|([0-9]{10}))" + _R, _FLAGS)

# EU CT 号：欧盟临床试验法规下 CTIS 给的试验编号 = 4 位年份-6 位（5 开头）-2 位-2 位，如 2024-515622-80-00。
# 两侧不能再接数字、字母或连字符。旧的 EudraCT 号（2019-001234-56）不收：三段数字的写法和别的编号、日期太像
_EUCT = re.compile(r"(?<![A-Za-z0-9-])(20[0-9]{2}-5[0-9]{5}-[0-9]{2}-[0-9]{2})(?![A-Za-z0-9-])", _FLAGS)

# PMID：裸数字到处都是（例数、金额、日期），只有明确写了 PMID / PubMed ID 标签才算；
# PMID 没有前导 0，1–8 位
_PMID = re.compile(
    _L + r"(?:PMID|PubMed[ \t]?ID)[ \t]*:?[ \t]*([1-9][0-9]{0,7})(?![0-9A-Za-z])", _FLAGS)

# PMCID：PMC + 数字，同样没有前导 0；目前最多 8 位
_PMCID = re.compile(_L + r"PMC([1-9][0-9]{0,7})" + _R, _FLAGS)

# DOI：10. + 4–9 位注册者代码 + / + 后缀。后缀只收常见 ASCII 字符：
# 不收 , ; : < > " ? # & = 和空白——这些在现代 DOI 里几乎不出现，却常是句子标点、列表分隔或 URL 查询串；
# 收括号和方括号，因为 Lancet 等的 DOI 中间带配对括号（尾部不配对的再剥掉）。
_DOI = re.compile(
    r"(?<![A-Za-z0-9.])"
    r"(?P<prefix>(?:https?://)?(?:dx\.)?doi\.org/|doi[ \t]*:[ \t]*)?"
    r"(?P<doi>10\.[0-9]{4,9}/[A-Za-z0-9\-._()/\[\]+%~'*!$@]+)",
    _FLAGS,
)
# 结尾的句子标点不属于 DOI；中文标点（。】」 等）本来就不在后缀字符集里，全角的 ）；，： 在下面按原文截断
_DOI_TRAIL = set(".,;:)]}>\"'([")
# 原文是全角字符时，只有折叠成字母数字或 . / - _ 的才允许留在 DOI 里；全角括号、分号、逗号是中文标点
_DOI_FULLWIDTH_OK = set("./-_")

# 批准文号：国药准字 + 类别字母（H 化药 Z 中药 S 生物制品 J 进口分包装 B 保健 F 辅料 T 体外诊断）+ 8 位；
# 允许中间一个空格（排版常见），多了不认
_NMPA = re.compile(r"国药准字 ?([HZSJBFT])([0-9]{8})" + _R, _FLAGS)
# 带标签的批准文号/注册证号（进口药品注册证号等不带“国药准字”的写法）：必须紧跟标签，否则 8 位数字太常见
_NMPA_LABELED = re.compile(r"(?:批准文号|注册证号)[:：]? ?([HZSJBFT]?[0-9]{8})" + _R, _FLAGS)

# 受理号：CDE 受理号前四位是 申报类别（C 国产 / J 进口）+ 新药或仿制（X / Y）+ 药品类别（H 化药 S 生物
# Z 中药）+ 申请类型（L 临床 S 上市 B 补充），后接 7 位。字母组合本身不算罕见，所以要么前面 15 个字符内
# 有“受理号”，要么它前后都不是字母数字（含汉字）、独立成词
_CDE = re.compile(_L + r"([CJ][XY][HSZ][LSB][0-9]{7})" + _R, _FLAGS)
_CDE_LABEL = "受理号"
_CDE_WINDOW = 15

# 股票：必须有交易所标记或标签，裸 6 位数字不出
_STOCK_SUFFIX = re.compile(_L + r"([0-9]{6})\.(SH|SZ|BJ)" + _R, _FLAGS)
_STOCK_PREFIX = re.compile(_L + r"(SH|SZ|BJ)([0-9]{6})" + _R, _FLAGS)
_STOCK_HK = re.compile(_L + r"([0-9]{4,5})\.HK" + _R, _FLAGS)
# 标签后面紧跟 .SH 之类时交给带后缀的规则，避免同一个代码出两条
_STOCK_LABEL = re.compile(
    r"(?:股票代码|证券代码)[ \t]*:?[ \t]*([0-9]{6})(?![0-9A-Za-z]|\.[A-Za-z])", _FLAGS)

# 各交易所股票代码的前两位。用来排除指数（000001.SH 是上证指数、399001.SZ 是深证成指）、基金
# （510300.SH、159915.SZ）和前缀与交易所对不上的写法（600519.SZ）。
# 沪：60/68 为 A 股，90 为 B 股；深：00/30 为 A 股，20 为 B 股；北交所：4x、8x、92x。
_EXCHANGE_PREFIXES = {
    "SH": ("60", "68", "90"),
    "SZ": ("00", "30", "20"),
    "BJ": ("4", "8", "92"),
}


def _a_share(code: str, exchange: str) -> str | None:
    exchange = exchange.upper()
    if not code.startswith(_EXCHANGE_PREFIXES[exchange]):
        return None
    return f"{code}.{exchange}"


def _infer_exchange(code: str) -> str | None:
    """只在原文有“股票代码 / 证券代码”标签时才用：按首位推断交易所。没有标签的 6 位数字太多（例数、
    金额、日期），不能推断。只推 A 股：6→沪，0/3→深，4/8/92→北交所；9 开头的 900 是沪市 B 股、
    2 开头是深市 B 股，标签不说明是哪个市场，宁可不出。"""
    if code.startswith(("60", "68")):
        return "SH"
    if code.startswith(("00", "30")):
        return "SZ"
    if code.startswith(("4", "8", "92")):
        return "BJ"
    return None


def _hk(code: str) -> str | None:
    # 港股代码 4 位为主（00700 / 0700 是同一只），去掉多余的前导 0 到 4 位；全 0 不是代码
    if not code.strip("0"):
        return None
    if len(code) == 5 and code.startswith("0"):
        code = code[1:]
    return f"{code}.HK"


def _doi_cut(text: str, folded: str, doi_start: int, end: int) -> int | None:
    """Where the matched DOI ends in the text, or None when it must not be reported."""
    # 原文是全角标点（如“）”“；”）的地方截断：它们折叠后是 ASCII，但在原文里是中文句读
    for i in range(doi_start, end):
        if ord(text[i]) > 0x7F and not (folded[i].isalnum() or folded[i] in _DOI_FULLWIDTH_OK):
            return i
    # 后缀在 ASCII 的 < : ; , 处停下、且后面紧跟字母数字：多半是 SICI 式老 DOI
    # （…30:1<1::AID-JBM1>3.0.CO;2-O）或两个编号粘在一起，截断会得到错的 DOI，不出
    if end < len(folded) and ord(text[end]) <= 0x7F:
        nxt = folded[end]
        after = folded[end + 1] if end + 1 < len(folded) else ""
        if nxt == "<" and after != "/":
            return None
        if nxt in ":;," and after.isascii() and after.isalnum():
            return None
    return end


def _strip_doi_trail(folded: str, doi_start: int, end: int) -> int:
    # 剥掉结尾标点；括号配对的保留（DOI 内部的括号）
    while end > doi_start:
        last = folded[end - 1]
        if last not in _DOI_TRAIL:
            break
        body = folded[doi_start:end]
        if last == ")" and body.count("(") >= body.count(")"):
            break
        if last == "]" and body.count("[") >= body.count("]"):
            break
        end -= 1
    return end


def _doi_hits(text: str, folded: str):
    for m in _DOI.finditer(folded):
        start = m.start()
        doi_start = m.start("doi")
        end = _doi_cut(text, folded, doi_start, m.end())
        if end is None:
            continue
        end = _strip_doi_trail(folded, doi_start, end)
        doi = folded[doi_start:end]
        registrant, _, suffix = doi.partition("/")
        if not suffix or not registrant[3:].isdigit():
            continue
        # 没有 doi: / doi.org 前缀、后缀又全是数字和点：更像“10.2345/67”这样的算式，不出
        if m.group("prefix") is None and re.fullmatch(r"[0-9./]+", suffix):
            continue
        yield "doi", doi.lower(), start, end


def _cde_standalone(folded: str, start: int, end: int) -> bool:
    # 独立成词：两侧都不是字母数字（这里含汉字，str.isalnum 对汉字为真）
    before = folded[start - 1] if start > 0 else " "
    after = folded[end] if end < len(folded) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _trial_and_literature_hits(folded: str):
    for m in _NCT.finditer(folded):
        yield "nct", "NCT" + m.group(1), m.start(), m.end()
    for m in _CTR.finditer(folded):
        yield "ctr", "CTR" + m.group(1), m.start(), m.end()
    for m in _CHICTR.finditer(folded):
        if m.group(3):
            value = "ChiCTR" + m.group(3)
        else:
            value = f"ChiCTR-{m.group(1).upper()}-{m.group(2)}"
        yield "chictr", value, m.start(), m.end()
    for m in _EUCT.finditer(folded):
        yield "euct", m.group(1), m.start(), m.end()
    for m in _PMID.finditer(folded):
        yield "pmid", m.group(1), m.start(), m.end()
    for m in _PMCID.finditer(folded):
        yield "pmcid", "PMC" + m.group(1), m.start(), m.end()


def _regulatory_hits(folded: str):
    for m in _NMPA.finditer(folded):
        yield "nmpa_approval", "国药准字" + m.group(1).upper() + m.group(2), m.start(), m.end()
    for m in _NMPA_LABELED.finditer(folded):
        yield "nmpa_registration", m.group(1).upper(), m.start(), m.end()
    for m in _CDE.finditer(folded):
        window = folded[max(0, m.start() - _CDE_WINDOW):m.start()]
        if _CDE_LABEL in window or _cde_standalone(folded, m.start(), m.end()):
            yield "cde_acceptance", m.group(1).upper(), m.start(), m.end()


def _stock_hits(folded: str):
    for m in _STOCK_SUFFIX.finditer(folded):
        value = _a_share(m.group(1), m.group(2))
        if value:
            yield "stock", value, m.start(), m.end()
    for m in _STOCK_PREFIX.finditer(folded):
        value = _a_share(m.group(2), m.group(1))
        if value:
            yield "stock", value, m.start(), m.end()
    for m in _STOCK_HK.finditer(folded):
        value = _hk(m.group(1))
        if value:
            yield "stock", value, m.start(), m.end()
    for m in _STOCK_LABEL.finditer(folded):
        exchange = _infer_exchange(m.group(1))
        if exchange:
            yield "stock", f"{m.group(1)}.{exchange}", m.start(), m.end()


def _scan(text: str):
    folded = _fold(text)
    # the order of the kinds matters: hits with the same start and length keep it through the stable sort below
    found = list(_trial_and_literature_hits(folded))
    found.extend(_doi_hits(text, folded))
    found.extend(_regulatory_hits(folded))
    found.extend(_stock_hits(folded))

    # 同一位置只出一个：按起点、长度从长到短排，与已保留结果重叠的丢掉。
    # 典型是 DOI 后缀里嵌着 NCT 之类——那是 DOI 的一部分，不另算一个引用
    found.sort(key=lambda h: (h[2], -(h[3] - h[2])))
    kept = []
    last_end = -1
    for kind, value, start, end in found:
        if start >= last_end:
            kept.append({"kind": kind, "value": value, "raw": text[start:end], "start": start, "end": end})
            last_end = end
    return kept


def extract(text: str) -> list[dict]:
    """每个编号：{"kind", "value"（规范化后）, "raw"（原文片段）, "start", "end"}；
    start/end 是原文 text 的偏移，text[start:end] == raw。按 start 排序。同一个编号（kind + value）在正文里出现多次
    只出一条，位置是第一次出现的地方：一份登记正文里同一个 NCT 号曾记了 14 条，一份说明书里同一个号 230 条。"""
    if not isinstance(text, str):
        raise TypeError("extract() 需要 str")
    seen, first = set(), []
    for hit in _scan(text):
        if (hit["kind"], hit["value"]) not in seen:
            seen.add((hit["kind"], hit["value"]))
            first.append(hit)
    return first


def normalize(kind: str, raw: str) -> str | None:
    """单个编号的规范化；不是合法编号返回 None。

    规则与 extract 相同：raw 里必须恰好是一个该种类的编号，两侧只允许空白和标点（不允许别的字母数字）。
    唯一例外是 PMID：调用方已声明 kind="pmid"（例如结构化字段），1–8 位无前导 0 的裸数字也接受；
    股票代码不放宽——没有标签或交易所标记就不推断交易所。"""
    if not isinstance(raw, str) or not isinstance(kind, str):
        return None
    s = raw.strip()
    if kind == "pmid":
        folded = _fold(s)
        if re.fullmatch(r"[1-9][0-9]{0,7}", folded):
            return folded
    found = _scan(s)
    if len(found) != 1 or found[0]["kind"] != kind:
        return None
    hit = found[0]
    rest = _fold(s[:hit["start"]] + s[hit["end"]:])
    if any(ch.isalnum() for ch in rest):
        return None
    return hit["value"]
