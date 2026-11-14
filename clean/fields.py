"""字段清洗和 L3 规范化（徐子雯的“洗字段”，规则照她 11-13 版）。只洗字段，不碰清洗正文。

“一份资料”（doc）是质量评分的输入：一个 dict，键和徐子雯原来的 parsed_result 一样（title、publish_date、markdown、
structured_data、extra、source_url、source_site、sniffed_meta），由 clean/quality_stage.py 的 document() 从版本拼出来。
markdown 就是雷达读的清洗正文（clean/clean_text.py 的产物），这里只读不改；她原来加在正文前的 YAML 头也不加
（那是给她直接推 WeKnora 用的，主线的元数据另外送知识库）。

- clean_fields(doc)：标题去 HTML 标签和多余空白；structured_data、extra 里的字符串（含列表和嵌套）空白折叠、全角只转
  字母和数字（全角中文标点不动）；发布日期规范成 YYYY-MM-DD / YYYY-MM / YYYY（不补零）；按站点定 doc_type 和 language。
- normalize_l3(doc)：药名同义词归一，剂量、浓度单位级联换算到 g / L；没命中的保留原值。返回 (新 doc, 改了哪些字段)。
- traced(doc)：血缘要追踪的字段和它们现在的值（通用 4 个 + 站点特有字段 SITE_FIELDS）。
- sniff_pdf_meta(text)：附件正文头部的 DOI、期刊名、标题（来源权威分要用）。
都是纯函数：返回新 dict，不改传入的，不联网、不连库。
空白、全角、去标签、日期这几个小工具用站点抽取器的那一份（fetch/extractors/text_tools.py、dates.py）：她原来也是
同一份，洗字段和抽取的口径一致。
"""

from __future__ import annotations

import copy
import re

from fetch.extractors.dates import normalize_date
from fetch.extractors.text_tools import clean_title, full_to_half, normalize_whitespace

DOC_TYPES = {
    "cde": "regulatory_notice", "cde_zdyz": "guideline", "nmpa": "regulatory_notice",
    "fda_press": "regulatory_notice", "fda_drug_safety": "regulatory_notice",
    "fda_api_enforcement": "drug_recall", "fda_api_drug_label": "drug_label",
    "chictr": "clinical_trial", "clinicaltrials": "clinical_trial", "pubmed": "literature",
    "fiercepharma": "industry_news", "phirda_rd": "industry_news", "phirda_policy": "industry_news",
    "phirda_healthcare_reform": "industry_news", "phirda_industry": "industry_news", "roche": "industry_news",
    "hkex": "company_announcement",
    # 主线比她多的来源：从她的枚举里挑最接近的类型补上（她 document_validator 给新来源预留了这几个）
    "europe_pmc": "literature", "fda_crl": "regulatory_document", "cde_xxgk": "regulatory_notice", "nccn": "guideline",
    "who_don": "regulatory_notice", "gdelt_doc": "industry_news", "sec_companyfacts": "company_announcement",
    "local_files": "",  # 上传的原件什么都有，不判类型（结构校验空值不查枚举）；别的来源的附件随所属来源
}
LANGUAGES = {
    "cde": "zh", "cde_zdyz": "zh", "nmpa": "zh", "chictr": "zh", "phirda_rd": "zh", "phirda_policy": "zh",
    "phirda_healthcare_reform": "zh", "phirda_industry": "zh", "hkex": "zh", "cde_xxgk": "zh",
    "fda_press": "en", "fda_drug_safety": "en", "fda_api_enforcement": "en", "fda_api_drug_label": "en",
    "clinicaltrials": "en", "pubmed": "en", "fiercepharma": "en", "roche": "en", "europe_pmc": "en", "fda_crl": "en",
    "who_don": "en", "sec_companyfacts": "en",  # gdelt_doc 收各种语言的新闻、local_files 是上传件：看正文判
}
_ZH = re.compile(r"[一-鿿]")

# 血缘追踪的字段：所有站点都有的 4 个，加各站点自己的特有字段（她 domain/schemas.py 里各站点 Extra 模型的字段）。
# 不在表里的站点追踪 extra 里实际出现的键。
STANDARD_FIELDS = ("title", "publish_date", "category", "summary")
SITE_FIELDS = {
    "nmpa": ("drug_name", "approval_number", "manufacturer"),
    "cde": ("drug_name", "approval_number", "manufacturer"),
    "chictr": ("drug_name", "regno", "reg_status", "study_type", "study_stage", "study_design", "objective",
               "intervention", "condition", "inclusion_criteria", "exclusion_criteria", "sample_size",
               "recruitment_status", "primary_sponsor", "start_date", "end_date", "funding_source", "ethics_approved"),
    "pubmed": ("drug_name", "pmid", "pmcid", "doi", "journal_name", "authors", "fulltext_status", "oa_pdf_url"),
    "hkex": ("drug_name", "stock_code", "stock_name", "stock_id"),
    "drugbank": ("drug_name", "drugbank_id", "drug_type", "state", "groups", "categories", "atc_codes", "cas_number",
                 "unii", "synonyms"),
    "fda_api_enforcement": ("drug_name", "recall_number", "recall_initiation_date", "recall_class",
                            "product_description", "reason_for_recall", "status", "recalling_firm",
                            "distribution_pattern", "approval_number"),
    "fda_api_drug_label": ("drug_name", "approval_number", "product_ndc", "spl_set_id"),
    "fda_drug_safety": ("drug_name", "safety_report_id", "report_date", "drug_names"),
    "clinicaltrials": ("drug_name", "nct_id", "study_type", "phase", "study_status", "enrollment",
                       "study_completion_date", "primary_completion_date", "sponsor", "collaborators", "conditions",
                       "interventions", "arm_groups"),
    "fda_crl": ("drug_name", "application_number", "letter_date", "letter_type", "file_name"),
    "ema": ("drug_name", "ema_product_number", "opinion_status", "decision_date"),
    "nhsa": ("drug_name", "catalog_version", "payment_scope", "effective_date", "attachment_locator"),
    "europe_pmc": ("drug_name", "pmid", "pmcid", "doi", "journal_name", "fulltext_status"),
}

# L3：同义词表（键小写），单位换算表（源单位小写 → (目标单位, 系数)，一路换到 质量 g、体积 L）
SYNONYMS = {
    "阿司匹林": "阿司匹林", "aspirin": "阿司匹林", "乙酰水杨酸": "阿司匹林", "acetylsalicylic acid": "阿司匹林",
    "扑热息痛": "对乙酰氨基酚", "paracetamol": "对乙酰氨基酚", "acetaminophen": "对乙酰氨基酚",
    "对乙酰氨基酚": "对乙酰氨基酚", "头孢曲松": "头孢曲松", "ceftriaxone": "头孢曲松",
    "阿托伐他汀": "阿托伐他汀", "atorvastatin": "阿托伐他汀", "氯吡格雷": "氯吡格雷", "clopidogrel": "氯吡格雷",
}
UNITS = {
    "ng": ("μg", 1e-3), "μg": ("mg", 1e-3), "ug": ("mg", 1e-3), "mg": ("g", 1e-3), "g": ("g", 1.0),
    "kg": ("g", 1e3), "μl": ("mL", 1e-3), "ul": ("mL", 1e-3), "ml": ("L", 1e-3), "l": ("L", 1.0),
}
# (在哪个字段组, 字段名, 按什么规范)：drug_name 走同义词，其余走单位换算
L3_FIELDS = (
    ("structured_data", "drug_name", "synonym"), ("structured_data", "dosage", "unit"),
    ("structured_data", "concentration", "unit"), ("structured_data", "amount", "unit"),
    ("structured_data", "unit", "unit"), ("extra", "drug_name", "synonym"), ("extra", "dosage", "unit"),
    ("extra", "concentration", "unit"),
)


def _clean_values(value):
    if isinstance(value, str):
        return full_to_half(normalize_whitespace(value), alnum_only=True)
    if isinstance(value, dict):
        return {key: _clean_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_values(item) for item in value]
    return value


def language(site: str, text: str) -> str:
    """站点有默认语言就用它；没有就看正文前 500 字里汉字多不多（超过 20 个算中文）。"""
    if site in LANGUAGES:
        return LANGUAGES[site]
    return "zh" if len(_ZH.findall(text[:500])) > 20 else "en"


def clean_fields(doc: dict) -> dict:
    out = copy.deepcopy(doc)
    if isinstance(out.get("title"), str) and out["title"]:
        out["title"] = clean_title(out["title"])
    out["structured_data"] = _clean_values(out.get("structured_data") or {})
    out["extra"] = _clean_values(out.get("extra") or {})
    date = normalize_date(out.get("publish_date") or "")
    if date:  # 认不出的日期保留原样，不覆盖
        out["publish_date"] = date
        out["structured_data"]["publish_date"] = date
    site = out.get("source_site") or ""
    out["doc_type"] = out.get("doc_type") or DOC_TYPES.get(site, "unknown")
    out["language"] = out.get("language") or language(site, out.get("markdown") or "")
    return out


def synonym(value: str) -> str:
    return SYNONYMS.get((value or "").strip().lower(), value)


def unit_expression(value: str) -> str:
    """'500 mg' → '0.5 g'、'100μg' → '0.0001 g'：数值加单位一路换到标准单位；不是“数值 单位”或单位不认识就原样返回。"""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-zμ°]+)\s*", value or "")
    if not m or m.group(2).lower() not in UNITS:
        return value
    number, unit = float(m.group(1)), m.group(2).lower()
    while unit in UNITS and UNITS[unit][0] != unit:
        unit, factor = UNITS[unit]
        number *= factor
    return f"{number:g} {unit}"


def normalize_l3(doc: dict) -> tuple[dict, list[tuple[str, str, str]]]:
    """(规范化后的 doc, [(字段, 原值, 新值)])，只列真的改了的字段。"""
    out = copy.deepcopy(doc)
    changed = []
    for box, field, kind in L3_FIELDS:
        container = out.setdefault(box, {})
        value = container.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        new = synonym(value) if kind == "synonym" else unit_expression(value)
        if new != value:
            changed.append((f"{box}.{field}", value, new))
            container[field] = new
    return out, changed


def traced(doc: dict) -> dict:
    """血缘要追踪的字段 → 现在的值。title 取顶层，其余先看 structured_data 再看 extra，None 记成空串。"""
    extra = doc.get("extra") or {}
    structured = doc.get("structured_data") or {}
    site_fields = SITE_FIELDS.get(doc.get("source_site") or "") or tuple(k for k in extra if k not in STANDARD_FIELDS)
    out = {}
    for field in dict.fromkeys(STANDARD_FIELDS + tuple(site_fields)):
        if field == "title":
            value = doc.get("title", "")
        else:
            value = structured[field] if field in structured else extra.get(field, "")
        out[field] = "" if value is None else value
    return out


_DOI = re.compile(r"(?:doi[:\s]*|https?://doi\.org/)?(10\.\d{4,9}/[^\s,;\"'<>]+)", re.IGNORECASE)
_JOURNAL = re.compile(r"(?:journal|期刊|刊名|published\s+in|出版)[:\s：]*([^\n,;，；。]{2,60})", re.IGNORECASE)
_HEADING = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def sniff_pdf_meta(text: str) -> dict:
    """附件正文头部 2000 字里的 DOI、期刊名、第一个 # 标题；头部没有 DOI 再找全文。找不到的是空串。"""
    head = (text or "")[:2000]
    doi = _DOI.search(head) or _DOI.search(text or "")
    journal = _JOURNAL.search(head)
    title = _HEADING.search(head)
    return {"doi": doi.group(1).rstrip(".") if doi else "", "journal_name": journal.group(1).strip() if journal else "",
            "sniffed_title": title.group(1).strip() if title else ""}
