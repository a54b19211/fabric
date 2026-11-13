"""每个版本的材料说明（payload.document_evidence，雷达读）：什么材料（material_kind）、正文到哪一级（text_level）、
和别的记录的关系（更正、撤回……）、是不是同一篇文献（同 DOI/PMID）。只照来源自己给出的写，不判断真假。"""

import json
import re
import xml.etree.ElementTree as ET

INDEPENDENCE_UNKNOWN = {"shared_identifiers": [], "status": "unknown", "origin_group_id": None,
                        "original_source": None, "basis": None}


def evidence(material_kind=None, text_level=None, relations=(), independence=None,
             statement_status="source_reported", events=()):
    return {
        "schema_version": "datafabric.document.evidence.v1",
        "material_kind": material_kind,
        "text_level": text_level,
        "document_relations": list(relations),
        "source_independence": independence or dict(INDEPENDENCE_UNKNOWN),
        "statement_status": statement_status,
        "information_events": list(events),
    }


def _locator(**fields):
    return {"json_pointer": None, "xpath": None, "start": None, "end": None, "quote": None, **fields}


def _publication_kind(values):
    if isinstance(values, dict):
        values = values.get("pubType", [])
    if not isinstance(values, list):
        return None
    values = {v.lower() for v in values if isinstance(v, str)}
    if values & {"review", "systematic review", "meta-analysis"}:
        return "review"
    if values & {"clinical trial", "randomized controlled trial", "controlled clinical trial",
                 "research article", "original research"}:
        return "primary_research"
    if values & {"conference abstract", "meeting abstract"}:
        return "conference_abstract"
    return None


def _paper_group(facts):
    """同一篇文献（共享 DOI 或 PMID）记为 same_origin，雷达不把它们当两条独立证据。"""
    identifiers = []
    doi, pmid = facts.get("doi"), facts.get("pmid")
    if isinstance(doi, str) and re.fullmatch(r"10\.\d{4,9}/\S+", doi.strip(), re.I):
        identifiers.append("doi:" + doi.strip().lower())
    if isinstance(pmid, str) and pmid.isdigit():
        identifiers.append("pmid:" + pmid)
    if not identifiers:
        return None
    return {**INDEPENDENCE_UNKNOWN, "status": "same_origin", "origin_group_id": identifiers[0],
            "shared_identifiers": identifiers, "basis": "共享任一原生DOI/PMID即构成同一文献约束；不能按单一分组键重复计数。"}


PUBMED_RELATIONS = {"ErratumFor": "corrects", "RetractionOf": "retracts", "UpdateOf": "supplements",
                    "RepublishedFrom": "reposts", "CorrectedandRepublishedFrom": "corrects"}


def _pubmed_relations(raw):
    path = "./MedlineCitation/CommentsCorrectionsList/CommentsCorrections"
    result = []
    for index, node in enumerate(ET.fromstring(raw).findall(path)):
        kind, pmid = PUBMED_RELATIONS.get(node.get("RefType")), node.findtext("PMID")
        if kind and pmid and pmid.isdigit():
            result.append({"relation_type": kind, "target_source_id": "pubmed", "target_source_record_id": pmid,
                           "target_version_id": None, "basis_locator": _locator(xpath=f"{path}[{index + 1}]")})
    return result


def _external_archive(data, projected):
    """导入的历史原件自己写了材料说明：照抄；给了 full_text 的，正文就是全文。"""
    given = {key: data[key] for key in ("material_kind", "text_level", "statement_status") if key in data}
    if data.get("full_text") is not None:
        if not isinstance(data["full_text"], str) or not data["full_text"].strip():
            raise ValueError("full_text须为非空正文")
        projected["clean_text"] = projected["title"] + "\n\n" + data["full_text"]
        projected["field_locators"]["full_text"] = {"json_pointer": "/full_text"}
        given.setdefault("text_level", "full_text")
    else:
        given.setdefault("text_level", "abstract" if projected["facts"].get("summary") else "title")
    if given.get("material_kind") in {"social_media", "repost"}:
        given["statement_status"] = "unverified_report"
    relations = data.get("document_relations") or []
    if not isinstance(relations, list):
        raise ValueError("文档关联须为列表")
    relations = [{"target_version_id": None, **r,
                  "basis_locator": r.get("basis_locator") or _locator(json_pointer=f"/document_relations/{i}")}
                 for i, r in enumerate(relations)]
    return evidence(relations=relations, independence=data.get("source_independence"),
                    events=data.get("information_events") or [], **given)


def attach_document_evidence(source_id, item, projected):
    """在投影上加 document_evidence（按来源）。"""
    data = json.loads(item.raw_content) if item.raw_media_type == "application/json" else None
    facts = projected["facts"]
    if source_id == "external_archive" and isinstance(data, dict):
        value = _external_archive(data, projected)
    elif facts.get("role") == "attachment":
        value = evidence()  # 附件（站点附件、PubMed 全文）：材料性质看它的父记录
    elif source_id in {"pubmed", "europe_pmc"}:
        relations = _pubmed_relations(item.raw_content) if item.raw_media_type == "application/xml" else ()
        value = evidence(_publication_kind(facts.get("publication_types")),
                         "abstract" if facts.get("abstract") else "title", relations, _paper_group(facts))
    elif source_id == "gdelt_doc":
        value = evidence("news_report", "title")
    elif source_id == "clinicaltrials":
        value = evidence("trial_registry", "excerpt")
    elif source_id == "fda_crl":
        value = evidence("official_notice", "full_text" if (data.get("text") or "").strip() else "metadata")
    elif source_id == "who_don":
        body = any(data.get(key) for key in ("Summary", "Overview", "Epidemiology", "Response", "Advice", "Assessment"))
        value = evidence("official_notice", "excerpt" if body else "title")
    elif source_id == "sec_companyfacts":
        value = evidence("corporate_disclosure", "metadata")
    elif item.record_kind == "graph":
        value = evidence("graph", "metadata")
    elif source_id == "legacy_documents":
        official = facts.get("source_site") in {"nmpa", "cde", "cde_xxgk", "cde_zdyz"}
        value = evidence("official_notice" if official else None, "excerpt")
    else:
        value = evidence()
    projected["document_evidence"] = value
    return projected
