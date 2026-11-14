"""五维质量分（徐子雯 T4，开题内容三）、结构校验（T5）和来源权威分。纯函数：不联网、不连库。

输入是一份资料 doc（键见 clean/fields.py 开头；对齐结果在 authority_resolutions，血缘在 lineage），输出 score()。
SCORER_VERSION 是这套口径的名字：改权重、阈值或任何一维的算法都要升版本，旧分数按版本留在表里，论文写明引用哪一版。

v1 = 徐子雯 11-13 版：
- 五维权重：完整性 0.30、准确性 0.20、时效性 0.15、权威性 0.25、可追溯性 0.10；总分 ≥0.80 高、≥0.60 中、≥0.40 低，其余隔离。
- 完整性：正文 ≥100 字 0.5 + 结构化字段非空比例 × 0.3 + 有标题 0.2。
- 准确性（6 个子信号，没有证据的给中性 0.7，不判劣）：字段能在正文找到 0.25、数值字段出现在正文 0.15、日期合法 0.15、
  正文里的编号格式合法 0.15、正文里的单位认得 0.15、药名对齐命中 0.15。
- 时效性：发布年离今年 ≤5 年 1.0、≤10 年 0.7、≤20 年 0.5、更早 0.3；没有日期 0.5。
- 权威性：source_confidence()——官方站点 0.90～0.98（再看顶刊、DOI、期刊名）、顶刊 0.85、有 DOI 0.70/0.75、只有期刊名 0.50、都没有 0.30。
- 可追溯性：网址是 http(s) 0.40 + 血缘覆盖“解析”“清洗”两步 0.35 + 字段有值变化的记录 0.25。
和她原版不一样的地方（公式、权重、阈值一样，不同的是下面几处口径和输入）：
1. “今天”是参数（打分那天），同一份资料同一天重算分数相同；她原版用运行时的 datetime.now()。
2. 药名对齐算命中要 found 且 std_id 非空，和她的命中率口径（ops/align_offline.py 的 summarize）一致；她原版看 resolved_value，占位名和降级也算命中。
3. 隔离只是等级，不拦截：所有版本照常建索引、给雷达（主持人：前期多出产出，未核的也带标记往下流）。
4. 正文是主线的清洗正文：前面没有她加的 YAML 头，也没有她站点清洗器的改写（临床登记改写成叙述段、Fierce 截导航），
   ClinicalTrials 也不是整条 study JSON。准确性里“字段在正文找得到”、编号、单位三个子信号因此会不同。
5. PubMed：她的正文是一行行元数据（pmid、作者、DOI……）加全文，主线清洗正文只有标题和摘要，字段在正文里大多找不到；
   她抓取当场打分时 extra 还是空的（落库时才补），不做 pmid、doi 的格式校验，主线按落库后的 extra 校验，多两条对齐血缘。
6. 结构校验不查 status：主线没有她的审核状态。
7. 编号格式族按来源选（clean/align.py）：她 approval_number 一律按 NDA/ANDA/BLA 校验，国药准字必判不合格；主线 NMPA、
   CDE 按国药准字，FDA 完整回应函的 application_number 也逐个校验。校验结果不进分数，只进对齐血缘。
"""

from __future__ import annotations

import re
from datetime import date

from clean.align import FORMATS

SCORER_VERSION = "datafabric.quality.v1"
WEIGHTS = {"completeness": 0.30, "accuracy": 0.20, "timeliness": 0.15, "authority": 0.25, "traceability": 0.10}
LEVELS = ((0.80, "高"), (0.60, "中"), (0.40, "低"))  # 都不到就是“隔离”
MIN_TEXT = 100  # 正文少于这么多字，完整性拿不到正文那 0.5
NEUTRAL = 0.7  # 准确性子信号没有证据时
ACCURACY = {"locatable": 0.25, "numeric": 0.15, "date": 0.15, "identifier": 0.15, "unit": 0.15, "aligned": 0.15}
GENERIC_FIELDS = {"title", "publish_date", "category", "summary", "doc_type", "language"}  # 不参与“正文里找得到”

# 正文里的编号候选（合法和不合法的写法都要抓到，才能交给格式校验判）
IDENTIFIER_PATTERNS = (
    (re.compile(r"NCT\d{1,9}", re.IGNORECASE), "nct_id"),
    (re.compile(r"\b\d{2,7}-\d{2}-\d\b"), "cas_number"),
    (re.compile(r"10\.\d{4,9}/\S+"), "doi"),
    (re.compile(r"(?:NDA|ANDA|BLA)[\s-]?\d{4,7}", re.IGNORECASE), "application_number"),
)
KNOWN_UNITS = {"mg", "g", "μg", "ug", "mcg", "ng", "kg", "ml", "l", "iu", "%", "mmol", "mol", "meq", "mg/kg", "g/kg",
               "mg/m2", "u/ml"}

# 来源权威分：顶刊白名单和已知官方站点（她 domain/source_mapper.py 的两张表，比较时都转小写）
TOP_JOURNALS = (
    "the lancet", "new england journal of medicine", "nature medicine", "jama", "bmj", "cell", "nature", "science",
    "nature reviews drug discovery", "journal of clinical oncology", "annals of internal medicine", "circulation",
    "the lancet oncology", "journal of medicinal chemistry", "drug discovery today",
    "european journal of pharmaceutical sciences", "international journal of pharmaceutics", "pharmaceutical research",
    "journal of pharmaceutical sciences", "molecular pharmaceutics", "advanced drug delivery reviews",
    "药学学报", "中国药学杂志", "中国新药杂志", "中国药理学通报", "中国临床药理学杂志", "药物分析杂志", "中国医药工业杂志",
    "中国抗生素杂志",
)
OFFICIAL_SITES = (
    "nmpa.gov.cn", "cde.org.cn", "nhc.gov.cn", "who.int", "fda.gov", "ema.europa.eu", "pharmnet.com.cn",
    "drugbank.com", "phirda.com", "fiercepharma.com", "chictr.org.cn", "clinicaltrials.gov", "pubmed.ncbi.nlm.nih.gov",
    "hkexnews.hk", "nccn.org", "roche.com", "europepmc.org", "ebi.ac.uk/europepmc",
)

# 结构校验（T5，只计数不拦截）：必填非空、类型、枚举
REQUIRED = ("uuid", "title", "raw_content")
STRING_FIELDS = ("uuid", "title", "raw_content", "publish_date", "category", "doc_type", "language", "summary")
ENUMS = {
    "doc_type": {"regulatory_notice", "drug_recall", "drug_label", "clinical_trial", "literature", "industry_news",
                 "company_announcement", "policy", "regulatory_document", "regulatory_decision",
                 "medical_insurance_catalog", "guideline"},
    "language": {"zh", "en"},
}


def level_of(total: float) -> str:
    return next((name for floor, name in LEVELS if total >= floor), "隔离")


def completeness(doc) -> float:
    score = 0.5 if len(doc.get("markdown") or "") >= MIN_TEXT else 0.0
    structured = doc.get("structured_data") or {}
    if isinstance(structured, dict) and structured:
        filled = [v for v in structured.values() if v not in (None, "", [])]
        score += 0.3 * len(filled) / len(structured)
    if doc.get("title"):
        score += 0.2
    return min(score, 1.0)


def _checkable_fields(structured):
    """站点特有字段里能拿去正文里找的值：(字段, 值)，只要字符串、至少 2 个字。"""
    if not isinstance(structured, dict):
        return []
    out = []
    for key, value in structured.items():
        if key in GENERIC_FIELDS:
            continue
        for item in value if isinstance(value, (list, tuple, set)) else [value]:
            if isinstance(item, str) and len(item.strip()) >= 2:
                out.append((key, item.strip()))
    return out


def _share_in_text(values, text):
    return sum(1 for v in values if v in text) / len(values) if values else NEUTRAL


def _date_valid(publish_date, today) -> float:
    if not publish_date:
        return NEUTRAL
    try:
        year = int(str(publish_date)[:4])
    except (ValueError, TypeError):
        return 0.0
    return 1.0 if 1900 <= year <= today.year else 0.0


def _identifiers_valid(text) -> float:
    found = [(kind, m.strip()) for pattern, kind in IDENTIFIER_PATTERNS for m in pattern.findall(text)]
    return sum(1 for kind, raw in found if FORMATS[kind](raw) is not None) / len(found) if found else NEUTRAL


def _units_known(text) -> float:
    # (?<!\d)：从一串数字的开头匹配（结果不变；不加的话长数字串要从每一位重试一遍，5 万位要一分钟）
    units = re.findall(r"(?<!\d)\d+(?:\.\d+)?\s*([a-zA-Zμ]+(?:/[a-zA-Zμ%]+)?%?)", text)
    return sum(1 for u in units if u.lower() in KNOWN_UNITS) / len(units) if units else NEUTRAL


def aligned(doc) -> bool:
    """药名对齐有没有命中：至少一个药名 found 且 std_id 非空（占位名、降级的不算）。"""
    rows = doc.get("authority_resolutions") or {}
    return any(isinstance(r, dict) and r.get("found") and str(r.get("std_id") or "").strip() for r in rows.values())


def accuracy(doc, today) -> float:
    text = doc.get("markdown") or ""
    fields = _checkable_fields(doc.get("structured_data"))
    numbers = [v for _, v in fields if re.fullmatch(r"\d[\d,.]*", v)]
    score = ACCURACY["locatable"] * _share_in_text([v for _, v in fields], text)
    score += ACCURACY["numeric"] * _share_in_text(numbers, text)
    score += ACCURACY["date"] * _date_valid(doc.get("publish_date"), today)
    score += ACCURACY["identifier"] * _identifiers_valid(text)
    score += ACCURACY["unit"] * _units_known(text)
    if aligned(doc):
        score += ACCURACY["aligned"]
    return min(score, 1.0)


def timeliness(doc, today) -> float:
    try:
        year = int(str(doc.get("publish_date") or "")[:4])
    except (ValueError, TypeError):
        return 0.5  # 没有日期或认不出：中性，不贸然判劣
    if year < 1900 or year > today.year:
        return 0.5
    age = today.year - year
    return 1.0 if age <= 5 else 0.7 if age <= 10 else 0.5 if age <= 20 else 0.3


def source_confidence(source_url="", doi="", journal_name="") -> dict:
    """来源权威分 {level A/B/C, score, reason}：官方站点再按顶刊、DOI、期刊名分档（不然官方源一律 0.95，排不出先后）。"""
    url = (source_url or "").lower()
    journal = (journal_name or "").lower().strip()
    top = next((t for t in TOP_JOURNALS if journal and (t in journal or journal in t)), None)
    if any(site in url for site in OFFICIAL_SITES):
        if top:
            return {"level": "A", "score": 0.98, "reason": "官方站点 + 顶刊佐证"}
        if doi:
            return {"level": "A", "score": 0.95, "reason": "官方站点 + DOI 佐证"}
        if journal_name:
            return {"level": "A", "score": 0.93, "reason": "官方站点 + 期刊名佐证"}
        return {"level": "A", "score": 0.90, "reason": "来自已知官方站点"}
    if top:
        return {"level": "A", "score": 0.85, "reason": f"匹配高质量期刊: {top}"}
    if doi:
        reason = f"提取到 DOI: {doi}" + (f"，期刊: {journal_name}" if journal_name else "")
        return {"level": "B", "score": 0.75 if journal_name else 0.7, "reason": reason}
    if journal_name:
        return {"level": "B", "score": 0.5, "reason": f"提取到期刊名但无 DOI: {journal_name}"}
    return {"level": "C", "score": 0.3, "reason": "未提取到可靠来源信息（无 URL / 无 DOI / 无期刊名）"}


def _confidence_of(doc) -> dict:
    meta = doc.get("sniffed_meta") or {}
    return source_confidence(doc.get("source_url") or "", doc.get("doi") or meta.get("doi"),
                             doc.get("journal_name") or meta.get("journal_name"))


CORE_STEPS = ("parse", "clean", "normalize", "align")  # 打分自己这一步不算


def _core_entries(lineage):
    """至少有一条核心加工步骤记录的字段（每个字段一个记录列表）。"""
    return [entries for entries in (lineage or {}).values() if isinstance(entries, list)
            and any(isinstance(e, dict) and e.get("step") in CORE_STEPS for e in entries)]


def traceability(doc) -> float:
    """有出处网址 0.40 + “解析”“清洗”两步的覆盖 0.35 + 有值变化记录的字段占比 0.25。"""
    source = 1.0 if (doc.get("source_url") or "").startswith(("http://", "https://")) else 0.0
    fields = _core_entries(doc.get("lineage"))
    if not fields:
        return 0.40 * source
    steps = [{e.get("step") for e in entries} for entries in fields]
    covered = sum(("parse" in s) + ("clean" in s) for s in steps) / (2 * len(fields))
    changed = sum(1 for entries in fields if any(e.get("input") != e.get("output") for e in entries)) / len(fields)
    return 0.40 * source + 0.35 * covered + 0.25 * changed


def dimensions(doc, today: date) -> dict:
    return {"completeness": completeness(doc), "accuracy": accuracy(doc, today), "timeliness": timeliness(doc, today),
            "authority": _confidence_of(doc)["score"], "traceability": traceability(doc)}


def issues_of(doc, dims, level) -> list[str]:
    issues = []
    text = doc.get("markdown") or ""
    if len(text) < MIN_TEXT:
        issues.append(f"正文过短({len(text)}字符 < {MIN_TEXT})")
    if not doc.get("structured_data"):
        issues.append("缺少结构化字段(structured_data 为空)")
    if not doc.get("title"):
        issues.append("缺少标题(title)")
    if dims["authority"] <= 0.3:
        issues.append("来源权威性低(无 DOI/期刊/已知官方站点)")
    if level == "隔离":
        issues.append("综合质量分低于隔离阈值（只记等级，不拦截）")
    return issues


def validate(doc) -> list[dict]:
    """结构校验（T5）：对整份资料查必填非空、类型、枚举，返回违规清单 [{field, rule, message}]，不拦截。"""
    structured = doc.get("structured_data") or {}
    record = {"uuid": doc.get("uuid"), "title": doc.get("title") or structured.get("title", ""),
              "raw_content": doc.get("markdown"), "summary": structured.get("summary", ""),
              "publish_date": doc.get("publish_date") or structured.get("publish_date", ""),
              "category": structured.get("category", ""), "doc_type": doc.get("doc_type"),
              "language": doc.get("language")}
    out = []
    for field in REQUIRED:
        value = record.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            out.append({"field": field, "rule": "required", "message": f"必填字段为空: {field}"})
    for field in STRING_FIELDS:
        if record.get(field) is not None and not isinstance(record[field], str):
            out.append({"field": field, "rule": "type",
                        "message": f"字段类型错误: {field} 期望 str，实际 {type(record[field]).__name__}"})
    for field, legal in ENUMS.items():
        value = record.get(field)
        if value not in (None, "") and value not in legal:
            out.append({"field": field, "rule": "enum", "message": f"枚举值非法: {field}={value!r}"})
    return out


def score(doc, today: date) -> dict:
    """{score 总分, level 高/中/低/隔离, dimensions 五维, issues 问题, violations 结构校验, confidence 来源权威分}。"""
    dims = dimensions(doc, today)
    total = max(0.0, min(1.0, sum(WEIGHTS[d] * dims[d] for d in dims)))
    level = level_of(total)
    return {"score": round(total, 4), "level": level, "dimensions": {k: round(v, 4) for k, v in dims.items()},
            "issues": issues_of(doc, dims, level), "violations": validate(doc), "confidence": _confidence_of(doc)}
