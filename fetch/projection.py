"""原件投影（project_record）：一条原件 → 标题、正文、facts（带原件里的位置）、几个时间，外加材料性质
（fetch/document_evidence.py）。按来源分到各自的投影函数：ClinicalTrials、Europe PMC、FDA CRL、PubMed、场景回放
在这里，网站、附件、GDELT、SEC、WHO 和图包在各自的模块里。纯函数：不联网、不连库。

谁用：存版本时 store/records.py 给每条原件算投影存进版本；读取插件读到原件时也先投影一遍，格式不对当场报错。
投影只是给人和下游读的；一个原件是不是新版本，看的是它的内容指纹（store/records.py · fingerprint）。
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

from clean.identifiers import NCT_ID
from store.record_models import ArchiveRecord, FabricError, canonical, sha256, source_time


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复JSON成员")
        result[key] = value
    return result


DECODER = json.JSONDecoder(
    object_pairs_hook=_unique,
    parse_constant=lambda value: (_ for _ in ()).throw(ValueError("非有限JSON数")),
)


def parse_json(raw: str) -> dict:
    try:
        value = DECODER.decode(raw)
    except (ValueError, TypeError):
        raise FabricError(
            "invalid_source_json", "来源JSON损坏或存在重复成员"
        ) from None
    if not isinstance(value, dict):
        raise FabricError("invalid_source_json", "来源原件须为JSON对象")
    return value


def _pointer(data: dict, pointer: str) -> Any:
    node = data
    try:
        for part in pointer.strip("/").split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            node = node[int(key)] if isinstance(node, list) else node[key]
        return node
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _facts(data: dict, paths: dict[str, str]):
    # 缺字段不补空数组/零，也不捏造locator；原生null仍保留明确指针。
    facts, locators = {}, {}
    for key, path in paths.items():
        node = data
        found = True
        for part in path.strip("/").split("/"):
            if not isinstance(node, dict) or part not in node:
                found = False
                break
            node = node[part]
        if found:
            facts[key], locators[key] = node, {"json_pointer": path}
    return facts, locators


TRIAL_PATHS = {
    "trial_id": "/protocolSection/identificationModule/nctId",
    "status": "/protocolSection/statusModule/overallStatus",
    "why_stopped": "/protocolSection/statusModule/whyStopped",
    "phase": "/protocolSection/designModule/phases",
    "conditions": "/protocolSection/conditionsModule/conditions",
    "interventions": "/protocolSection/armsInterventionsModule/interventions",
    "enrollment": "/protocolSection/designModule/enrollmentInfo/count",
    "enrollment_type": "/protocolSection/designModule/enrollmentInfo/type",
    "start_date": "/protocolSection/statusModule/startDateStruct/date",
    "start_date_type": "/protocolSection/statusModule/startDateStruct/type",
    "completion_date": "/protocolSection/statusModule/completionDateStruct/date",
    "completion_date_type": "/protocolSection/statusModule/completionDateStruct/type",
    "primary_completion_date": "/protocolSection/statusModule/primaryCompletionDateStruct/date",
    "primary_completion_date_type": "/protocolSection/statusModule/primaryCompletionDateStruct/type",
    "organization_study_id": "/protocolSection/identificationModule/orgStudyIdInfo",
    "secondary_ids": "/protocolSection/identificationModule/secondaryIdInfos",
    "official_title": "/protocolSection/identificationModule/officialTitle",
    "lead_sponsor": "/protocolSection/sponsorCollaboratorsModule/leadSponsor",
    "collaborators": "/protocolSection/sponsorCollaboratorsModule/collaborators",
    "study_type": "/protocolSection/designModule/studyType",
    "design_info": "/protocolSection/designModule/designInfo",
    "arm_groups": "/protocolSection/armsInterventionsModule/armGroups",
    "primary_outcomes": "/protocolSection/outcomesModule/primaryOutcomes",
    "secondary_outcomes": "/protocolSection/outcomesModule/secondaryOutcomes",
    "eligibility": "/protocolSection/eligibilityModule",
    "locations": "/protocolSection/contactsLocationsModule/locations",
    "has_results": "/hasResults",
    "results_first_posted": "/protocolSection/statusModule/resultsFirstPostDateStruct/date",
    "results_first_posted_type": "/protocolSection/statusModule/resultsFirstPostDateStruct/type",
    "results_participant_flow": "/resultsSection/participantFlowModule",
    "results_baseline": "/resultsSection/baselineCharacteristicsModule",
    "results_outcome_measures": "/resultsSection/outcomeMeasuresModule",
    "results_adverse_events": "/resultsSection/adverseEventsModule",
}
EXTERNAL_FIELDS = {
    "trial_id",
    "program_id",
    "lineage_id",
    "indication",
    "jurisdiction",
    "mapping_available_at",
    "phase",
    "status",
    "conditions",
    "interventions",
    "event_type",
    "unit_kind",
    "unit_id",
    "event_at",
    "event_observed",
    "observation_start",
    "observation_end",
    "public_available_at",
    "sponsor",
    "source_reference",
    "summary",
    "source_published_at",
    "source_updated_at",
    # 仅交接原件声明和精确位置；不据字段名确认主体、数值口径或来源许可。
    "company_name",
    "period_start",
    "period_end",
    "unit",
    "metric_role",
    "value",
    "license",
    "license_url",
}


def crl_id(data: dict) -> str:
    name = data.get("file_name")
    if not isinstance(name, str) or not name.strip():
        raise FabricError("missing_native_id", "FDA信函缺file_name，不生成猜测ID")
    components = [
        data.get("letter_date"),
        data.get("letter_type"),
        data.get("application_number"),
    ]
    if not all(value not in (None, "", []) for value in components):
        raise FabricError(
            "ambiguous_native_id", "FDA信函复合身份字段不足，须按原件核对后采用"
        )
    return name + "#" + sha256(canonical(components))[:24]


def project_record(source_id: str, item: ArchiveRecord) -> dict:
    """原件 → 投影：标题、正文、facts（带原件里的位置）、几个时间，外加材料性质（fetch/document_evidence.py）。
    投影只是给人和下游读的；一个原件是不是新版本，看的是它的内容指纹（store.records.fingerprint）。
    legacy 迁入的原件（{"legacy_migration": {...}, 正文}）只把正文交给来源自己的投影，迁入块记进 facts。"""
    from fetch.document_evidence import attach_document_evidence

    migration, item = _migration_envelope(source_id, item)
    projected = _project(source_id, item)
    try:
        projected = attach_document_evidence(source_id, item, projected)
    except (ValueError, TypeError, AttributeError):
        raise FabricError("invalid_document_evidence", "材料类型、全文或文档关联不符合格式") from None
    if migration is None:
        return projected
    return dict(projected, facts={**projected["facts"], "legacy_migration": migration,
                                  "publication_role": "migrated_record"},
                parser_version=projected["parser_version"] + "+" + migration["converter"])


def _migration_envelope(source_id, item):
    if item.raw_media_type != "application/json" or '"legacy_migration"' not in item.raw_content[:200000]:
        return None, item
    data = parse_json(item.raw_content)
    if not isinstance(data, dict) or "legacy_migration" not in data:
        return None, item
    from fetch.legacy_migration import CONVERTER

    block = data.pop("legacy_migration")
    if (not isinstance(block, dict) or block.get("converter") != CONVERTER or source_id == "legacy_documents"
            or not re.fullmatch(r"[0-9a-f]{64}", str(block.get("legacy_version_id") or ""))):
        raise FabricError("invalid_migration_envelope", "迁入封装不符合固定格式", 422)
    return block, item.model_copy(update={"raw_content": json.dumps(data, ensure_ascii=False, sort_keys=True)})


def _project(source_id: str, item: ArchiveRecord) -> dict:
    """按来源找它的投影函数。"""
    from fetch.catalog import ATTACHMENT_SOURCES, SITE_SOURCES

    if source_id == "pubmed" and item.raw_media_type == "application/xml":
        return _project_pubmed(item)
    if item.raw_media_type != "application/json":
        raise FabricError("unsupported_media", "该来源只支持 JSON 原件")
    if source_id in SITE_SOURCES:
        from fetch.site_crawler import project_capture

        return project_capture(source_id, item)
    if source_id in ATTACHMENT_SOURCES:  # 站点以外有附件的来源（PubMed 全文、本地文件）
        from fetch.attachments import project_attachment

        return project_attachment(source_id, item)
    data = parse_json(item.raw_content)
    if source_id == "gdelt_doc":
        from fetch.gdelt_news import project_gdelt

        return project_gdelt(item, data)
    if source_id == "sec_companyfacts":
        from fetch.sec_xbrl import project_sec

        return project_sec(item, data)
    if source_id == "who_don":
        from fetch.who_don import project_who

        return project_who(item, data)
    if source_id == "legacy_documents":
        from fetch.legacy_documents import project_legacy

        try:
            return project_legacy(item, data)
        except ValueError:
            raise FabricError("invalid_graph_fields", "图包或旧文档桥字段不符合固定格式") from None
    projector = {"clinicaltrials": _project_clinicaltrials, "fda_crl": _project_fda_crl,
                 "europe_pmc": _project_europe_pmc, "external_archive": _project_external_archive}.get(source_id)
    if projector is None:
        raise FabricError("unsupported_source", "该来源没有投影函数")
    projected = projector(item, data)
    if projected["record_kind"] != item.record_kind:
        raise FabricError("record_kind_mismatch", "原件类型与导入声明不一致")
    return projected


def _record(kind, title, clean_text, facts, locators, *, url=None, published=None, updated=None, event_time=None,
            parser_version=None) -> dict:
    """一个投影（几个 JSON 来源共用的写法）。"""
    if not isinstance(title, str) or len(title) > 10000:
        raise FabricError("invalid_source_fields", "来源标题格式错误")
    projected = {
        "record_kind": kind, "title": title, "source_url": url, "clean_text": clean_text, "facts": facts,
        "field_locators": locators,
        "event_time": event_time.model_dump(mode="json") if event_time else None,
        "source_published_at": published.model_dump(mode="json") if published else None,
        "source_updated_at": updated.model_dump(mode="json") if updated else None,
    }
    if parser_version:
        projected["parser_version"] = parser_version
    return projected


def _project_clinicaltrials(item, data) -> dict:
    facts, locators = _facts(data, TRIAL_PATHS)
    for key in ("phase", "conditions"):
        if facts.get(key) is not None and (
            not isinstance(facts[key], list) or any(not isinstance(value, str) for value in facts[key])
        ):
            raise FabricError("invalid_source_fields", "临床登记数组字段类型不合法")
    if facts.get("status") is not None and not isinstance(facts["status"], str):
        raise FabricError("invalid_source_fields", "临床登记状态须为来源字符串")
    number = facts.get("enrollment")
    if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 0):
        raise FabricError("invalid_source_fields", "临床登记人数类型不合法；未知不补零")
    native = facts.get("trial_id")
    if not isinstance(native, str) or not re.fullmatch(NCT_ID, native) or native != item.source_record_id:
        raise FabricError("native_id_mismatch", "临床登记ID与原件不一致")
    title = _pointer(data, "/protocolSection/identificationModule/briefTitle") or native
    status = "/protocolSection/statusModule/"
    return _record(
        "trial", title, _pointer(data, "/protocolSection/descriptionModule/briefSummary") or str(title), facts, locators,
        url="https://clinicaltrials.gov/study/" + native,
        published=source_time(_pointer(data, status + "studyFirstPostDateStruct/date"), "registry_first_posted"),
        updated=source_time(_pointer(data, status + "lastUpdatePostDateStruct/date"), "registry_update_posted"),
        event_time=source_time(facts.get("start_date"), "registry_start_" + str(facts.get("start_date_type", "unknown"))),
        parser_version="datafabric.clinicaltrials.native.v0.2",
    )


FDA_CRL_FIELDS = {
    "application_numbers": "/application_number",
    "letter_date": "/letter_date",
    "letter_type": "/letter_type",
    "file_name": "/file_name",
    "company_name": "/company_name",
    "text": "/text",
    "approver_name": "/approver_name",
    "approver_title": "/approver_title",
    "approval_status": "/approval_status",
}


def _project_fda_crl(item, data) -> dict:
    if item.source_record_id != crl_id(data):
        raise FabricError("native_id_mismatch", "FDA复合来源ID与原件不一致")
    facts, locators = _facts(data, FDA_CRL_FIELDS)
    # 标题：谁收到的、什么信、哪个申请、信上的日期；没有公司名才退到文件名
    applications = data.get("application_number")
    applications = ", ".join(map(str, applications)) if isinstance(applications, list) else str(applications or "")
    title = (f"{data.get('company_name') or data.get('file_name')} - {data.get('letter_type')} "
             f"({applications}, {data.get('letter_date')})")
    clean_text = data.get("text") or ""
    if not isinstance(clean_text, str):
        raise FabricError("invalid_source_fields", "FDA信函text字段不是文本")
    date = data.get("letter_date")
    try:
        event_time = source_time(datetime.strptime(date, "%m/%d/%Y").date().isoformat(), "letter_signed")
    except (TypeError, ValueError):
        event_time = source_time(date, "letter_signed")
    # 信函签字日期不是网页公开日期，不填 source_published_at
    return _record("document", title, clean_text, facts, locators, event_time=event_time)


EUROPE_PMC_FIELDS = {
    "source": "/source",
    "id": "/id",
    "pmid": "/pmid",
    "pmcid": "/pmcid",
    "doi": "/doi",
    "publication_types": "/pubTypeList",
    "publication_date": "/firstPublicationDate",
    "abstract": "/abstractText",
    "correction_links": "/commentCorrectionList",
    "license": "/license",
    "is_open_access": "/isOpenAccess",
    "in_europepmc": "/inEPMC",
    "date_of_revision": "/dateOfRevision",
    "authors": "/authorList",
    "author_ids": "/authorIdList",
    "author_string": "/authorString",
    "affiliation": "/affiliation",
    "journal": "/journalInfo",
    "keywords": "/keywordList",
    "mesh_headings": "/meshHeadingList",
    "chemicals": "/chemicalList",
    "grants": "/grantsList",
    "fulltext_links": "/fullTextUrlList",
    "fulltext_ids": "/fullTextIdList",
    "data_links": "/dataLinksTagsList",
    "cross_references": "/dbCrossReferenceList",
    "publication_status": "/publicationStatus",
    "language": "/language",
    "first_index_date": "/firstIndexDate",
    "fulltext_received_date": "/fullTextReceivedDate",
}


def _project_europe_pmc(item, data) -> dict:
    native = str(data.get("source", "")) + ":" + str(data.get("id", ""))
    if not data.get("source") or not data.get("id") or native != item.source_record_id:
        raise FabricError("native_id_mismatch", "Europe PMC source:id与原件不一致")
    facts, locators = _facts(data, EUROPE_PMC_FIELDS)
    title = data.get("title") or native
    return _record(
        "document", title, str(title) + "\n\n" + str(data.get("abstractText") or ""), facts, locators,
        url="https://europepmc.org/article/" + str(data["source"]) + "/" + str(data["id"]),
        published=source_time(data.get("firstPublicationDate"), "europepmc_first_publication_may_be_imputed"),
        updated=source_time(data.get("dateOfRevision"), "europepmc_revision"),
        parser_version="datafabric.europepmc.core.v0.2",
    )


_ARCHIVE_TIMES = ("mapping_available_at", "public_available_at", "observation_start", "observation_end", "event_at")


def _project_external_archive(item, data) -> dict:
    """导入的历史原件（场景回放）：记录类型、标题、摘要和 EXTERNAL_FIELDS 里列的字段，照原件写的取。"""
    kind = data.get("record_kind")
    if kind not in ("trial", "program", "outcome", "document"):
        raise FabricError("unsupported_record_kind", "历史原件须明确trial/program/outcome/document")
    facts, locators = _facts(data, {key: "/" + key for key in sorted(EXTERNAL_FIELDS)})
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise FabricError("invalid_source_fields", "历史原件须有明确标题")
    for key in _ARCHIVE_TIMES:
        if facts.get(key) is not None and source_time(facts[key]) is None:
            raise FabricError("invalid_source_time", "历史原件时间格式/时区不合法")
    source_url = data.get("source_reference")
    if source_url is not None and (not isinstance(source_url, str) or len(source_url) > 2000):
        raise FabricError("invalid_source_fields", "来源引用格式错误")
    return _record(
        kind, title, str(title) + "\n\n" + str(facts.get("summary") or ""), facts, locators, url=source_url,
        published=source_time(data.get("source_published_at"), "archive_source_published"),
        updated=source_time(data.get("source_updated_at"), "archive_source_updated"),
        event_time=(source_time(facts.get("event_at"), "outcome_event") if kind == "outcome"
                    else source_time(data.get("event_time"), "archive_source_event")),
    )


def _project_pubmed(item):
    try:
        root = ET.fromstring(item.raw_content)
    except ET.ParseError:
        raise FabricError("invalid_source_xml", "PubMed XML原件损坏") from None
    if root.tag != "PubmedArticle":
        raise FabricError("invalid_source_xml", "PubMed原件须为单条PubmedArticle")
    pmid = root.findtext("./MedlineCitation/PMID")
    if not pmid or pmid != item.source_record_id or item.record_kind != "document":
        raise FabricError("native_id_mismatch", "PubMed PMID与原件不一致")
    article = root.find("./MedlineCitation/Article")
    if article is None:
        raise FabricError("invalid_source_xml", "PubMed缺Article")
    title = (
        "".join(article.find("ArticleTitle").itertext())
        if article.find("ArticleTitle") is not None
        else pmid
    )
    abstract = "\n".join(
        "".join(node.itertext()) for node in article.findall("./Abstract/AbstractText")
    )
    pubtypes = [
        node.text
        for node in article.findall("./PublicationTypeList/PublicationType")
        if node.text
    ]
    ids = {
        node.attrib.get("IdType"): node.text
        for node in root.findall("./PubmedData/ArticleIdList/ArticleId")
    }
    corrections = [
        {
            "ref_type": node.attrib.get("RefType"),
            "pmid": node.findtext("PMID"),
            "ref_source": node.findtext("RefSource"),
        }
        for node in root.findall(
            "./MedlineCitation/CommentsCorrectionsList/CommentsCorrections"
        )
    ]
    facts = {
        "pmid": pmid,
        "abstract": abstract,
        "publication_types": pubtypes,
        "doi": ids.get("doi"),
        "pmcid": ids.get("pmc"),
        "correction_links": corrections,
    }
    locators = {
        "pmid": {"xpath": "./MedlineCitation/PMID"},
        "abstract": {
            "xpath": "./MedlineCitation/Article/Abstract/AbstractText",
            "transform": "join_itertext_newline",
        },
        "publication_types": {
            "xpath": "./MedlineCitation/Article/PublicationTypeList/PublicationType",
            "transform": "list_text",
        },
        "doi": {"xpath": "./PubmedData/ArticleIdList/ArticleId[@IdType='doi']"},
        "pmcid": {"xpath": "./PubmedData/ArticleIdList/ArticleId[@IdType='pmc']"},
        "correction_links": {
            "xpath": "./MedlineCitation/CommentsCorrectionsList/CommentsCorrections",
            "transform": "selected_attributes_and_children",
        },
    }
    date = article.find("./Journal/JournalIssue/PubDate")
    published = None
    if date is not None:
        year, month, day = (
            date.findtext("Year"),
            date.findtext("Month"),
            date.findtext("Day"),
        )
        if year:
            value = year
            if month and month.isdigit():
                value += "-" + month.zfill(2)
                if day and day.isdigit():
                    value += "-" + day.zfill(2)
            published = source_time(value, "pubmed_publication")
    return {
        "record_kind": "document",
        "title": title,
        "source_url": "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/",
        "clean_text": title + "\n\n" + abstract,
        "facts": facts,
        "field_locators": locators,
        "event_time": None,
        "source_published_at": published.model_dump(mode="json") if published else None,
        "source_updated_at": None,
    }
