"""v3 3.3 的分类、四维质量和分流：确定性规则，不调模型，结果按（版本, 处理器版本）存。

- 分类：文档类型（来源 + 原件自身字段，如 Europe PMC 的 pubTypeList）、证据细类和服务的判断（来源目录），
  以及细类映射到的 v3 1.4 大类（fetch.evidence_types 注册表；无合适大类写 unmapped，判不出写 unknown）。
  监管机构公告/通知的大类按内容判（注册表 document_category）：标题、摘要、来源类别字段写明监管动作
  （批准、拒绝、召回、撤市/暂停、警告信/执法等）才是监管决定，其余是官方通报。
  doc_type 只取注册表里的细类或文档角色（附件、库存冻结件），其余写 unknown。
- 四个维度分开，各是“状态 + 理由”，允许未知，不合成一个分数：
  1. format：格式质量——ok / degraded（截断、缺摘要、上下标被拍平等，正文仍可用）/ broken（没有正文、解析失败）；
  2. source_credibility：来源可信度——按证据细类在注册表里的可信度类别给出（官方登记、官方通报、监管文件、文献索引、未证实…）；
  3. entity_match：实体匹配——这一层不做消歧（人工审核图谱负责），只记录抽到了几个编号，状态 not_assessed；
  4. fact_check：事实核验——这一层不做（输出前由 BioCheck 核验），状态 not_checked。
- v3 6.2 可复用注释（v5）：
  - experiment_types：实验类型候选（in_vitro 体外 / animal 动物 / human 人体，可多值），每个带依据和原句定位
    （清洗正文里的字符范围和原文）。登记记录按来源性质是 human；监管公告、标签、召回等不是研究材料，记
    not_applicable；文献和附件按固定词规则（EXPERIMENT_RULE）找，找不到写 unknown。这是候选注释，不是判定：
    一篇动物研究里提到“患者”也会被标出 human，评价交给 MechEvidence。
  - reading_level：这个版本在 Fabric 里实际可取得的材料层级（全文附件 / 摘要 / 只有标题 / 登记记录 / 页面
    正文 / 库存冻结件 / 无正文）和依据、字符数。模型某次实际读到的窗口是另一回事，由雷达的出站绑定记录。
- 分流（缺正文、弱来源、格式错误分别记录，不能把“弱”当隔离理由）：
  - quarantine：只有 format=broken（无正文可定位、解析失败）；元数据照常保留，原因写明；
  - weak_signal：内容短（< WEAK_CHARS）或来源未证实；照常进入后续（发现、检索），带标签，不能被升级为确定事实；
  - normal：其余。
"""

from __future__ import annotations

import re

from fetch.evidence_types import FINE_TYPES, UNKNOWN, categories_of, document_category

PROCESSOR_VERSION = "datafabric.profile.v5"
WEAK_CHARS = 200

DEGRADING = ("field_truncated", "no_abstract", "no_title", "html_sup_sub_unpaired", "summary_missing",
             "attachment_missing_glyphs")
BREAKING = ("no_body", "invalid_json", "not_a_record")

# 可信度类别按证据细类取自注册表（fetch.evidence_types），这里不另写一份
CREDIBILITY = {k: t.credibility for k, t in FINE_TYPES.items() if t.credibility}


def quality_level(prof):
    """画像折成一个质量等级（/clean-text?quality=true 给 Atlas 的门槛用）：分流 quarantine → 隔离，weak_signal → 低；
    normal 里格式 ok 且来源可信度已知（不是 unknown/unverified）→ 高，其余 → 中。没有画像 → None。"""
    if not prof:
        return None
    q = prof.get("quality") or {}
    if prof.get("route") == "quarantine":
        return "隔离"
    if prof.get("route") == "weak_signal":
        return "低"
    good = (q.get("format") or {}).get("state") == "ok" and         (q.get("source_credibility") or {}).get("state") not in (None, "unknown", "unverified")
    return "高" if good else "中"

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
    from fetch.catalog import CATALOG

    if source_id in CATALOG:
        return CATALOG[source_id].evidence_types[0]
    return NON_CATALOG_DOC_TYPES.get(source_id, UNKNOWN)


def _doc_category(doc_type, evidence, lead_text):
    """文档的大类。细类按注册表映射（按内容分的细类看 lead_text）；附件随所属来源（来源只映射到
    一个大类时取它，否则 unknown）；库存冻结件和不认识的类型写 unknown，不猜。"""
    if doc_type in FINE_TYPES:
        return document_category(doc_type, lead_text)
    if doc_type == "attachment":
        cats = categories_of(evidence)
        return cats[0] if len(cats) == 1 else UNKNOWN
    return UNKNOWN


EXPERIMENT_RULE = "datafabric.experiment_type_rule.v1"
EXPERIMENT_TERMS = {
    "in_vitro": re.compile(r"\bin vitro\b|\bcell lines?\b|\bcell cultures?\b|\bcultured (?:cells|neurons|"
                           r"cardiomyocytes|hepatocytes)\b|\borganoids?\b|体外实验|体外研究|细胞系|细胞实验", re.I),
    "animal": re.compile(r"\bmice\b|\bmouse\b|\brats?\b|\bmurine\b|\brodents?\b|\bzebrafish\b|"
                         r"\bnon-human primates?\b|\bmonkeys?\b|\banimal models?\b|\bxenografts?\b|小鼠|大鼠|"
                         r"动物模型|动物实验", re.I),
    "human": re.compile(r"\bpatients?\b|\bparticipants?\b|\bhealthy volunteers?\b|\bhuman subjects?\b|"
                        r"\brandomi[sz]ed\b|\bclinical trials?\b|\bcohorts?\b|患者|受试者|临床试验", re.I),
}
EXPERIMENT_LOCATORS = 3  # matches kept per type
# 不是研究材料的来源角色/类型：实验类型不适用
NOT_RESEARCH_SOURCES = {"nmpa", "cde", "cde_xxgk", "fda_api_drug_label", "fda_api_enforcement", "fda_crl",
                        "who_don", "sec_companyfacts"}
REGISTRY_SOURCES = {"clinicaltrials", "chictr"}
LITERATURE_SOURCES = {"europe_pmc", "pubmed"}


def experiment_types(source_id, role, text):
    """[{type, basis, locators: [{start, end, text}]}]：按来源性质或固定词规则；可多值；找不到 unknown。"""
    if role != "attachment" and source_id in REGISTRY_SOURCES:
        return [{"type": "human", "basis": "registry_record", "locators": []}]
    if role != "attachment" and source_id in NOT_RESEARCH_SOURCES:
        return [{"type": "not_applicable", "basis": "source_is_not_research", "locators": []}]
    found = []
    for kind, pattern in EXPERIMENT_TERMS.items():
        hits = [{"start": m.start(), "end": m.end(), "text": m.group()}
                for _, m in zip(range(EXPERIMENT_LOCATORS), pattern.finditer(text or ""))]
        if hits:
            found.append({"type": kind, "basis": EXPERIMENT_RULE, "locators": hits})
    return found or [{"type": "unknown", "basis": EXPERIMENT_RULE, "locators": []}]


def reading_level(source_id, role, chars, warnings, parse_status):
    """这个版本可取得的材料层级 {level, basis, chars}（不是模型的实际窗口）。"""
    if chars == 0:
        level, basis = "none", "no_body"
    elif role == "attachment":
        level, basis = ("full_text", "attachment_parse") if parse_status == "parsed" else ("none", "attachment_" + str(
            parse_status))
    elif source_id in LITERATURE_SOURCES:
        level, basis = (("title_only", "no_abstract") if any(w.startswith("no_abstract") for w in warnings)
                        else ("abstract", "literature_record"))
    elif source_id in REGISTRY_SOURCES:
        level, basis = "registry_record", "registry_fields"
    elif source_id == "legacy_documents":
        level, basis = "stored_document", "legacy_frozen_text"
    else:
        level, basis = "page_text", "source_page_or_record"
    return {"level": level, "basis": basis, "chars": chars}


def profile(*, source_id, role=None, chars, warnings, identifiers, pub_types=None, parse_status=None,
            lead_text="", text="") -> dict:
    """lead_text：标题、摘要和来源给的类别字段拼成的短文本，只用于按内容分大类。text：清洗正文，只用于
    实验类型的词规则和定位（定位是它的字符范围）。"""
    from fetch.catalog import CATALOG

    spec = CATALOG.get(source_id)
    evidence = list(spec.evidence_types) if spec else []

    flags = [w for w in warnings if w.startswith(BREAKING)]
    degraded = [w for w in warnings if w.startswith(DEGRADING)]
    if parse_status in ("failed", "unsupported", "incomplete"):
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
                         "reason": "实体消歧由人工审核图谱负责"},
        "fact_check": {"state": "not_checked", "reason": "事实核验在输出前由 BioCheck 核验负责"},
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
        "doc_category": _doc_category(doc_type, evidence, lead_text),
        "evidence_types": evidence,
        # 来源目录细类覆盖的大类（一个来源可以覆盖多类；unmapped/unknown 照样列出）
        "evidence_categories": categories_of(evidence),
        "quality": quality,
        "route": route,
        "route_reasons": reasons,
        "experiment_types": experiment_types(source_id, role, text),
        "reading_level": reading_level(source_id, role, chars, warnings, parse_status),
    }
