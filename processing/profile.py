"""v3 3.3 的分类、四维质量和分流：确定性规则，不调模型，结果按（版本, 处理器版本）存。

- 分类：文档类型（来源 + 原件自身字段，如 Europe PMC 的 pubTypeList）、证据细类和服务的判断（来源目录），
  以及细类映射到的 v3 1.4 大类（contracts.evidence_types 注册表；无合适大类写 unmapped，判不出写 unknown）。
  doc_type 只取注册表里的细类或文档角色（附件、库存冻结件），其余写 unknown。
- 四个维度分开，各是“状态 + 理由”，允许未知，不合成一个分数：
  1. format：格式质量——ok / degraded（截断、缺摘要、上下标被拍平等，正文仍可用）/ broken（没有正文、解析失败）；
  2. source_credibility：来源可信度——按证据细类在注册表里的可信度类别给出（官方登记、官方通报、监管文件、文献索引、未证实…）；
  3. entity_match：实体匹配——这一层不做消歧（唐的图谱负责），只记录抽到了几个编号，状态 not_assessed；
  4. fact_check：事实核验——这一层不做（潘的核验），状态 not_checked。
- 分流（Codex：缺正文、弱来源、格式错误分别记录，不能把“弱”当隔离理由）：
  - quarantine：只有 format=broken（无正文可定位、解析失败）；元数据照常保留，原因写明；
  - weak_signal：内容短（< WEAK_CHARS）或来源未证实；照常进入后续（发现、检索），带标签，不能被升级为确定事实；
  - normal：其余。
"""

from __future__ import annotations

from contracts.evidence_types import FINE_TYPES, UNKNOWN, categories_of, category_of

PROCESSOR_VERSION = "datafabric.profile.v3"
WEAK_CHARS = 200

DEGRADING = ("field_truncated", "no_abstract", "no_title", "html_sup_sub_unpaired", "summary_missing")
BREAKING = ("no_body", "invalid_json", "not_a_record")

# 可信度类别按证据细类取自注册表（contracts.evidence_types），这里不另写一份
CREDIBILITY = {k: t.credibility for k, t in FINE_TYPES.items() if t.credibility}

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
    return NON_CATALOG_DOC_TYPES.get(source_id, UNKNOWN)


def _doc_category(doc_type, evidence):
    """文档的大类。细类按注册表映射；附件随所属来源（来源只映射到一个大类时取它，否则 unknown）；
    库存冻结件和不认识的类型写 unknown，不猜。"""
    if doc_type in FINE_TYPES:
        return category_of(doc_type)
    if doc_type == "attachment":
        cats = categories_of(evidence)
        return cats[0] if len(cats) == 1 else UNKNOWN
    return UNKNOWN


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
    cred = ({"state": credibility, "reason": "按来源目录的证据细类（注册表的可信度类别）"} if credibility
            else {"state": "unknown", "reason": "来源不在目录，或注册表没给它的证据细类定可信度类别"})
    doc_type = _doc_type(source_id, role, pub_types)

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
        "doc_type": doc_type,
        # 文档本身的大类：细类映射到的 v3 1.4 大类；无合适大类 unmapped，判不出 unknown
        "doc_category": _doc_category(doc_type, evidence),
        "evidence_types": evidence,
        # 来源目录细类覆盖的大类（一个来源可以覆盖多类；unmapped/unknown 照样列出）
        "evidence_categories": categories_of(evidence),
        "decisions": decisions,
        "quality": quality,
        "route": route,
        "route_reasons": reasons,
    }
