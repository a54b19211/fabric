"""v3 3.3 的分类、四维质量和分流：确定性规则，不调模型，结果按（版本, 处理器版本）存。

- 分类：文档类型（来源 + 原件自身字段，如 Europe PMC 的 pubTypeList）、证据类型和服务的判断（来源目录）。
- 四个维度分开，各是“状态 + 理由”，允许未知，不合成一个分数：
  1. format：格式质量——ok / degraded（截断、缺摘要、上下标被拍平等，正文仍可用）/ broken（没有正文、解析失败）；
  2. source_credibility：来源可信度——按证据类型给出类别（官方登记、官方通报、监管文件、文献索引、未证实…）；
  3. entity_match：实体匹配——这一层不做消歧（唐的图谱负责），只记录抽到了几个编号，状态 not_assessed；
  4. fact_check：事实核验——这一层不做（潘的核验），状态 not_checked。
- 分流（Codex：缺正文、弱来源、格式错误分别记录，不能把“弱”当隔离理由）：
  - quarantine：只有 format=broken（无正文可定位、解析失败）；元数据照常保留，原因写明；
  - weak_signal：内容短（< WEAK_CHARS）或来源未证实；照常进入后续（发现、检索），带标签，不能被升级为确定事实；
  - normal：其余。
"""

from __future__ import annotations

PROCESSOR_VERSION = "datafabric.profile.v2"
WEAK_CHARS = 200

DEGRADING = ("field_truncated", "no_abstract", "no_title", "html_sup_sub_unpaired", "summary_missing")
BREAKING = ("no_body", "invalid_json", "not_a_record")

CREDIBILITY = {
    "registry_record": "official_registry",
    "official_notice": "official_notice",
    "regulatory_notice": "regulatory_document",
    "regulatory_letter": "regulatory_document",
    "regulatory_approval": "regulatory_document",
    "literature_metadata": "indexed_literature",
    "abstract": "indexed_literature",
    "preprint": "preprint_not_peer_reviewed",
    "filing_fact": "company_filing",
    "corporate_filing": "company_filing",
    "media": "media_report",
    "industry_media": "media_report",
    "social": "unverified",
    "guideline": "guideline_body",
    "policy": "official_notice",
    "regulatory_label": "regulatory_document",
    "company_disclosure": "company_filing",
    "drug_database": "curated_database",
}

# 采集目录里的来源，文档类型就是目录写的第一种证据类型；目录外的只有 documents 库存的冻结桥
NON_CATALOG_DOC_TYPES = {"legacy_documents": "stored_document"}


def _doc_type(source_id, role, pub_types):
    if role == "attachment":
        return "attachment"
    if source_id in ("europe_pmc", "pubmed"):
        types = [str(t).lower() for t in (pub_types or [])]
        if any("review" in t for t in types):
            return "review"
        if any(t in ("research-article", "journal article", "clinical trial", "randomized controlled trial")
               for t in types):
            return "primary_research"
        if any("preprint" in t for t in types):
            return "preprint"
        if types:
            return "literature_other"
        return "literature_unknown_type"
    from collection.catalog import CATALOG

    if source_id in CATALOG:
        return CATALOG[source_id].evidence_types[0]
    return NON_CATALOG_DOC_TYPES.get(source_id, "unknown")


def profile(*, source_id, role=None, chars, warnings, identifiers, pub_types=None, parse_status=None) -> dict:
    from collection.catalog import CATALOG

    spec = CATALOG.get(source_id)
    evidence = list(spec.evidence_types) if spec else []
    decisions = list(spec.decisions) if spec else []

    flags = [w for w in warnings if w.startswith(BREAKING)]
    degraded = [w for w in warnings if w.startswith(DEGRADING)]
    if parse_status in ("failed", "unsupported"):
        flags.append("attachment_" + parse_status)
    if chars == 0 and not flags:
        flags.append("no_body")
    if flags:
        fmt = {"state": "broken", "reasons": flags}
    elif degraded:
        fmt = {"state": "degraded", "reasons": degraded}
    else:
        fmt = {"state": "ok", "reasons": []}

    credibility = next((CREDIBILITY[e] for e in evidence if e in CREDIBILITY), None)
    cred = ({"state": credibility, "reason": "按来源目录的证据类型"} if credibility
            else {"state": "unknown", "reason": "来源目录没有这个来源的证据类型"})

    quality = {
        "format": fmt,
        "source_credibility": cred,
        "entity_match": {"state": "not_assessed", "identifiers": len(identifiers),
                         "reason": "实体消歧由唐的图谱负责"},
        "fact_check": {"state": "not_checked", "reason": "事实核验在输出前由潘的核验负责"},
    }
    reasons = []
    if fmt["state"] == "broken":
        route = "quarantine"
        reasons.append("没有可定位的正文：" + "、".join(fmt["reasons"]))
    else:
        if chars < WEAK_CHARS:
            reasons.append(f"正文很短（{chars} 字符）")
        if cred["state"] == "unverified":
            reasons.append("来源未证实")
        route = "weak_signal" if reasons else "normal"
    return {
        "processor_version": PROCESSOR_VERSION,
        "doc_type": _doc_type(source_id, role, pub_types),
        "evidence_types": evidence,
        "decisions": decisions,
        "quality": quality,
        "route": route,
        "route_reasons": reasons,
    }
