"""把来源已经给出的材料性质、全文和关联带到固定版本，不判传言真假。"""
import json
import re
import xml.etree.ElementTree as ET

from biz_rules.history_models import (
    DocumentEvidence,
    DocumentRelation,
    SourceIndependence,
)


def _pointer(data, pointer):
    node = data
    for key in pointer[1:].split("/"):
        if re.search(r"~(?![01])", key):
            raise ValueError("JSON Pointer转义无效")
        key = key.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key) or int(key) >= len(node):
                raise ValueError("原件依据路径不存在")
            node = node[int(key)]
        elif isinstance(node, dict) and key in node:
            node = node[key]
        else:
            raise ValueError("原件依据路径不存在")
    return node


def _check_basis(locator, data, clean_text):
    if locator.json_pointer is not None:
        if data is None:
            raise ValueError("JSON依据不能指向XML")
        _pointer(data, locator.json_pointer)
    if locator.xpath is not None:
        raise ValueError("导入JSON不能声明XML位置")
    if locator.quote is not None and clean_text[locator.start:locator.end] != locator.quote:
        raise ValueError("关联依据引文与本版本正文不一致")


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
    identifiers = []
    doi, pmid = facts.get("doi"), facts.get("pmid")
    if isinstance(doi, str) and re.fullmatch(r"10\.\d{4,9}/\S+", doi.strip(), re.I):
        identifiers.append("doi:" + doi.strip().lower())
    if isinstance(pmid, str) and pmid.isdigit():
        identifiers.append("pmid:" + pmid)
    if identifiers:
        return SourceIndependence(
            status="same_origin", origin_group_id=identifiers[0],
            shared_identifiers=identifiers,
            basis="共享任一原生DOI/PMID即构成同一文献约束；不能按单一分组键重复计数。",
        )
    return SourceIndependence()


def _pubmed_relations(raw):
    root = ET.fromstring(raw)
    result = []
    forward = {"ErratumFor": "corrects", "RetractionOf": "retracts",
               "UpdateOf": "supplements", "RepublishedFrom": "reposts",
               "CorrectedandRepublishedFrom": "corrects"}
    path = "./MedlineCitation/CommentsCorrectionsList/CommentsCorrections"
    for index, node in enumerate(root.findall(path)):
        kind, pmid = forward.get(node.get("RefType")), node.findtext("PMID")
        if kind and pmid and pmid.isdigit():
            result.append(DocumentRelation(
                relation_type=kind, target_source_id="pubmed", target_source_record_id=pmid,
                basis_locator={"xpath": path + f"[{index + 1}]"},
            ))
    return result


def attach_document_evidence(source_id, item, projected):
    """所有输出均属于本原件/本投影；旧版本payload保持原样。"""
    data = json.loads(item.raw_content) if item.raw_media_type == "application/json" else None
    facts = projected["facts"]
    metadata = {}
    if source_id == "external_archive" and isinstance(data, dict) and data.get("schema_version") != "datafabric.observation.v1":
        keys = {"material_kind", "text_level", "document_relations", "source_independence",
                "statement_status", "information_events"}
        metadata = {key: data[key] for key in keys if key in data}
        full_text = data.get("full_text")
        if full_text is not None:
            if not isinstance(full_text, str) or not full_text.strip():
                raise ValueError("full_text须为完整保留的非空正文")
            projected["clean_text"] = projected["title"] + "\n\n" + full_text
            projected["field_locators"]["full_text"] = {"json_pointer": "/full_text"}
            metadata.setdefault("text_level", "full_text")
        else:
            if metadata.get("text_level") == "full_text":
                raise ValueError("未提供full_text不能声明已有全文")
            metadata.setdefault("text_level", "abstract" if facts.get("summary") else "title")
        if metadata.get("material_kind") in {"social_media", "repost"}:
            metadata.setdefault("statement_status", "unverified_report")
        relations = metadata.get("document_relations", [])
        if not isinstance(relations, list):
            raise ValueError("文档关联须为列表")
        metadata["document_relations"] = [
            {**relation, "basis_locator": relation.get("basis_locator") or
             {"json_pointer": f"/document_relations/{index}"}}
            for index, relation in enumerate(relations)
        ]
    elif facts.get("role") == "attachment":
        metadata = {}  # 附件版本（站点附件、PubMed 全文）：材料性质看它的父记录
    elif source_id in {"pubmed", "europe_pmc"}:
        metadata = {
            "material_kind": _publication_kind(facts.get("publication_types")),
            "text_level": "abstract" if facts.get("abstract") else "title",
            "source_independence": _paper_group(facts),
        }
        if source_id == "pubmed" and item.raw_media_type == "application/xml":
            metadata["document_relations"] = _pubmed_relations(item.raw_content)
    elif source_id == "gdelt_doc":
        metadata = {"material_kind": "news_report", "text_level": "title"}
    elif source_id == "clinicaltrials":
        metadata = {"material_kind": "trial_registry", "text_level": "excerpt"}
    elif source_id == "fda_crl":
        metadata = {"material_kind": "official_notice",
                    "text_level": "full_text" if data.get("text", "").strip() else "metadata"}
    elif source_id == "who_don":
        body_present = any(data.get(key) for key in
                           ("Summary", "Overview", "Epidemiology", "Response", "Advice", "Assessment"))
        metadata = {"material_kind": "official_notice",
                    "text_level": "excerpt" if body_present else "title"}
    elif source_id == "sec_companyfacts":
        metadata = {"material_kind": "corporate_disclosure", "text_level": "metadata"}
    elif item.record_kind == "graph":
        metadata = {"material_kind": "graph", "text_level": "metadata"}
    elif source_id == "legacy_documents":
        metadata = {"text_level": "excerpt"}
        if facts.get("source_site") in {"nmpa", "cde", "cde_xxgk", "cde_zdyz"}:
            metadata["material_kind"] = "official_notice"

    value = DocumentEvidence.model_validate(metadata)
    if source_id == "external_archive":
        for relation in value.document_relations:
            _check_basis(relation.basis_locator, data, projected["clean_text"])
        for event in value.information_events:
            _check_basis(event.basis_locator, data, projected["clean_text"])
    projected["document_evidence"] = value.model_dump(mode="json")
    # 独立元数据有自己的schema；不改WHO等既有投影的版本语义。
    # _publish已把全部projected内容纳入version_id，新增元数据形成新版本。
    return projected
