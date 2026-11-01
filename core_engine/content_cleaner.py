"""
内容清洗引擎。

在 parse → quality_check 之间执行，对 parsed_result 的 markdown 和
structured_data 做站点定制化清洗，并注入 RAG 所需的 metadata。

配置方式：在 config/sites/<site>.py 的 CONFIG 中声明：
    "content_cleaner": my_cleaner_fn   # 接受并返回 parsed_result dict

若未配置 content_cleaner，则走通用清洗（去除多余空白）。
配置了 content_cleaner 时，通用清洗仍会执行（站点特解只负责重写正文结构）。
"""

import re
from loguru import logger
from prefect import task

from config.crawler_settings import SITE_PROFILES
from biz_rules.cleaners import (
    clean_title,
    full_to_half,
    normalize_publish_date,
    normalize_whitespace,
)
from core_engine.lineage_tracer import snapshot, record_step

# ── doc_type 映射 ──────────────────────────────────────────────────────────
_DOC_TYPE_MAP = {
    "cde":                   "regulatory_notice",
    "cde_zdyz":              "guideline",
    "nmpa":                  "regulatory_notice",
    "fda_press":             "regulatory_notice",
    "fda_drug_safety":       "regulatory_notice",
    "fda_api_enforcement":   "drug_recall",
    "fda_api_drug_label":    "drug_label",
    "chictr":                "clinical_trial",
    "clinicaltrials":        "clinical_trial",
    "pubmed":                "literature",
    "fiercepharma":          "industry_news",
    "phirda_rd":             "industry_news",
    "phirda_policy":         "industry_news",
    "phirda_healthcare_reform": "industry_news",
    "phirda_industry":       "industry_news",
    "roche":                 "industry_news",
    "hkex":                  "company_announcement",
}

# ── 语言推断（按站点默认值，正文检测作为兜底）─────────────────────────────
_LANG_MAP = {
    "cde": "zh", "cde_zdyz": "zh", "nmpa": "zh", "chictr": "zh",
    "phirda_rd": "zh", "phirda_policy": "zh",
    "phirda_healthcare_reform": "zh", "phirda_industry": "zh",
    "hkex": "zh",
    "fda_press": "en", "fda_drug_safety": "en",
    "fda_api_enforcement": "en", "fda_api_drug_label": "en",
    "clinicaltrials": "en", "pubmed": "en",
    "fiercepharma": "en", "roche": "en",
}

_ZH_RE = re.compile(r'[\u4e00-\u9fff]')


def _detect_language(site_key: str, text: str) -> str:
    """先查映射表，查不到则按正文中文字符比例判断。"""
    if site_key in _LANG_MAP:
        return _LANG_MAP[site_key]
    zh_count = len(_ZH_RE.findall(text[:500]))
    return "zh" if zh_count > 20 else "en"


def _build_frontmatter(parsed_result: dict, doc_type: str, language: str) -> str:
    """生成 YAML frontmatter 块，供 WeKnora metadata 过滤使用。"""
    title = parsed_result.get("title", "").replace('"', '\\"')
    publish_date = parsed_result.get("publish_date", "")
    source_site = parsed_result.get("source_site", "")
    source_url = parsed_result.get("source_url", "")
    confidence = parsed_result.get("confidence_level", "")  # 可能在 quality_check 之前为空

    lines = ["---"]
    if title:
        lines.append(f'title: "{title}"')
    if publish_date:
        lines.append(f"publish_date: {publish_date}")
    lines.append(f"source_site: {source_site}")
    lines.append(f"doc_type: {doc_type}")
    lines.append(f"language: {language}")
    if source_url:
        lines.append(f"source_url: {source_url}")
    if confidence:
        lines.append(f"confidence_level: {confidence}")
    # drug_name：有则写入，方便 WeKnora 按药品名过滤
    # 与落库同口径：CSS 抽取器把它写在 extra，API/fetcher 路径写在 structured_data，
    # 两处都认，否则 cde / nmpa 这类站点永远写不进 frontmatter。
    structured = parsed_result.get("structured_data") or {}
    extra = parsed_result.get("extra") or {}
    drug_name = (
        str(structured.get("drug_name") or "").strip()
        or str(extra.get("drug_name") or "").strip()
        or str((structured.get("extra") or {}).get("drug_name") or "").strip()
    )
    if drug_name:
        lines.append(f'drug_name: "{drug_name}"')
    lines.append("---")
    return "\n".join(lines)


def _clean_str_fields(mapping) -> None:
    """就地归一字典中的字符串值（全角转半角 + 空白折叠）。非 dict 直接跳过。"""
    if not isinstance(mapping, dict):
        return
    for key in list(mapping.keys()):
        val = mapping[key]
        if isinstance(val, str):
            mapping[key] = full_to_half(normalize_whitespace(val), alnum_only=True)


def _default_clean(parsed_result: dict) -> dict:
    """通用清洗：markdown 空白规范化 + 标题清洗 + 字段值归一。

    对所有站点无条件执行（包括配置了 content_cleaner 的站点），且自身幂等。
    """
    md = parsed_result.get("markdown", "")
    if md:
        parsed_result["markdown"] = normalize_whitespace(md, preserve_newlines=True)

    # 标题同样要洗：frontmatter 与信封顶层 title 直接取这个值
    title = parsed_result.get("title")
    if isinstance(title, str) and title:
        parsed_result["title"] = clean_title(title)

    # structured_data 是历史容器；extra 是 router/api_router 产出、落库实际取用的容器
    structured = parsed_result.get("structured_data")
    _clean_str_fields(structured)
    if isinstance(structured, dict):
        _clean_str_fields(structured.get("extra"))
    _clean_str_fields(parsed_result.get("extra"))

    return parsed_result


def _inject_metadata(parsed_result: dict) -> dict:
    """
    推断并注入 doc_type、language、rag_metadata，
    并在 markdown 头部插入 YAML frontmatter。
    """
    site_key = parsed_result.get("source_site", "")
    md = parsed_result.get("markdown", "")

    doc_type = parsed_result.get("doc_type") or _DOC_TYPE_MAP.get(site_key, "unknown")
    language = parsed_result.get("language") or _detect_language(site_key, md)

    # 注入到 parsed_result 顶层，供 storage_writer 写库
    parsed_result["doc_type"] = doc_type
    parsed_result["language"] = language
    parsed_result["rag_metadata"] = {
        "source_site": site_key,
        "doc_type": doc_type,
        "language": language,
        "publish_date": parsed_result.get("publish_date", ""),
        "source_url": parsed_result.get("source_url", ""),
        "confidence_level": parsed_result.get("confidence_level", ""),
    }

    # 在 markdown 头部插入 frontmatter（跳过已有 frontmatter 的情况）
    if md and not md.startswith("---"):
        frontmatter = _build_frontmatter(parsed_result, doc_type, language)
        parsed_result["markdown"] = frontmatter + "\n\n" + md

    return parsed_result


@task(name="clean_content")
def clean_content(parsed_result: dict) -> dict:
    """
    内容清洗任务：站点特解清洗 → 通用清洗 → metadata 注入。

    参数:
        parsed_result: route_webpage / route_pdf / iter_api_results 的输出

    返回:
        dict: 清洗后并注入 metadata 的 parsed_result
    """
    site_key = parsed_result.get("source_site", "")
    cleaner = SITE_PROFILES.get(site_key, {}).get("content_cleaner")

    # 快照关键字段（清洗前），用于记录 clean 步骤血缘
    before = snapshot(parsed_result)

    if cleaner:
        logger.debug(f"[Cleaner] 站点特解清洗: {site_key}")
        try:
            parsed_result = cleaner(parsed_result)
        except Exception as e:
            logger.warning(f"[Cleaner] 站点清洗失败: {site_key} | {e}")

    # 通用清洗对所有站点无条件执行：站点特解只负责重写正文结构，
    # 空白折叠 / 全角归一 / 标题清洗必须兜底，否则该站点的正文与字段永远是脏的。
    parsed_result = _default_clean(parsed_result)

    # 日期标准化（所有站点统一执行，覆盖自定义 cleaner 的站点）
    parsed_result = normalize_publish_date(parsed_result)

    # metadata 注入（所有站点统一执行）
    parsed_result = _inject_metadata(parsed_result)

    # 记录 clean 步骤血缘（对比 before/after）
    record_step(parsed_result, step="clean", before=before, op="clean")

    return parsed_result
