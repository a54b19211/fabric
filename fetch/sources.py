"""公开来源的读取（read_public）和原件投影（project_record）。接口类来源（ClinicalTrials、Europe PMC、
FDA CRL、PubMed）的读法在这里，网站在 fetch/site_crawler.py，其余来源各有自己的模块。"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx

from clean.identifiers import NCT_ID
from fetch import transport
from fetch.catalog import source_catalog
from store.history_models import (
    ArchiveRecord,
    FetchRequest,
    HistoryError,
    canonical,
    sha256,
    source_time,
)

# every source with a history_sources row and its name (fetch.catalog)
SOURCE_CATALOG = source_catalog()


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
        raise HistoryError(
            "invalid_source_json", "来源JSON损坏或存在重复成员"
        ) from None
    if not isinstance(value, dict):
        raise HistoryError("invalid_source_json", "来源原件须为JSON对象")
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
        raise HistoryError("missing_native_id", "FDA信函缺file_name，不生成猜测ID")
    components = [
        data.get("letter_date"),
        data.get("letter_type"),
        data.get("application_number"),
    ]
    if not all(value not in (None, "", []) for value in components):
        raise HistoryError(
            "ambiguous_native_id", "FDA信函复合身份字段不足，须按原件核对后采用"
        )
    return name + "#" + sha256(canonical(components))[:24]


def project_record(source_id: str, item: ArchiveRecord) -> dict:
    """原件 → 投影：标题、正文、facts（带原件里的位置）、几个时间，外加材料性质（fetch/document_evidence.py）。
    投影只是给人和下游读的；一个原件是不是新版本，看的是它的内容指纹（store.history.fingerprint）。
    legacy 迁入的原件（{"legacy_migration": {...}, 正文}）只把正文交给来源自己的投影，迁入块记进 facts。"""
    from fetch.document_evidence import attach_document_evidence

    migration, item = _migration_envelope(source_id, item)
    projected = _project(source_id, item)
    try:
        projected = attach_document_evidence(source_id, item, projected)
    except (ValueError, TypeError, AttributeError):
        raise HistoryError("invalid_document_evidence", "材料类型、全文或文档关联不符合格式") from None
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
        raise HistoryError("invalid_migration_envelope", "迁入封装不符合固定格式", 422)
    return block, item.model_copy(update={"raw_content": json.dumps(data, ensure_ascii=False, sort_keys=True)})


def _project(source_id: str, item: ArchiveRecord) -> dict:
    """按来源找它的投影函数。"""
    from fetch.catalog import ATTACHMENT_SOURCES, SITE_SOURCES

    if source_id == "pubmed" and item.raw_media_type == "application/xml":
        return _project_pubmed(item)
    if item.raw_media_type != "application/json":
        raise HistoryError("unsupported_media", "该来源只支持 JSON 原件")
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
    if source_id in {"legacy_documents", "reviewed_graph", "primekg_graph"}:
        from graph.publish.reviewed_graph import project_graph_record

        try:
            return project_graph_record(source_id, item, data)
        except ValueError:
            raise HistoryError("invalid_graph_fields", "图包或旧文档桥字段不符合固定格式") from None
    projector = {"clinicaltrials": _project_clinicaltrials, "fda_crl": _project_fda_crl,
                 "europe_pmc": _project_europe_pmc, "external_archive": _project_external_archive}.get(source_id)
    if projector is None:
        raise HistoryError("unsupported_source", "该来源没有投影函数")
    projected = projector(item, data)
    if projected["record_kind"] != item.record_kind:
        raise HistoryError("record_kind_mismatch", "原件类型与导入声明不一致")
    return projected


def _record(kind, title, clean_text, facts, locators, *, url=None, published=None, updated=None, event_time=None,
            parser_version=None) -> dict:
    """一个投影（几个 JSON 来源共用的写法）。"""
    if not isinstance(title, str) or len(title) > 10000:
        raise HistoryError("invalid_source_fields", "来源标题格式错误")
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
            raise HistoryError("invalid_source_fields", "临床登记数组字段类型不合法")
    if facts.get("status") is not None and not isinstance(facts["status"], str):
        raise HistoryError("invalid_source_fields", "临床登记状态须为来源字符串")
    number = facts.get("enrollment")
    if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 0):
        raise HistoryError("invalid_source_fields", "临床登记人数类型不合法；未知不补零")
    native = facts.get("trial_id")
    if not isinstance(native, str) or not re.fullmatch(NCT_ID, native) or native != item.source_record_id:
        raise HistoryError("native_id_mismatch", "临床登记ID与原件不一致")
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
        raise HistoryError("native_id_mismatch", "FDA复合来源ID与原件不一致")
    facts, locators = _facts(data, FDA_CRL_FIELDS)
    # 标题：谁收到的、什么信、哪个申请、信上的日期；没有公司名才退到文件名
    applications = data.get("application_number")
    applications = ", ".join(map(str, applications)) if isinstance(applications, list) else str(applications or "")
    title = (f"{data.get('company_name') or data.get('file_name')} - {data.get('letter_type')} "
             f"({applications}, {data.get('letter_date')})")
    clean_text = data.get("text") or ""
    if not isinstance(clean_text, str):
        raise HistoryError("invalid_source_fields", "FDA信函text字段不是文本")
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
        raise HistoryError("native_id_mismatch", "Europe PMC source:id与原件不一致")
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
        raise HistoryError("unsupported_record_kind", "历史原件须明确trial/program/outcome/document")
    facts, locators = _facts(data, {key: "/" + key for key in sorted(EXTERNAL_FIELDS)})
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HistoryError("invalid_source_fields", "历史原件须有明确标题")
    for key in _ARCHIVE_TIMES:
        if facts.get(key) is not None and source_time(facts[key]) is None:
            raise HistoryError("invalid_source_time", "历史原件时间格式/时区不合法")
    source_url = data.get("source_reference")
    if source_url is not None and (not isinstance(source_url, str) or len(source_url) > 2000):
        raise HistoryError("invalid_source_fields", "来源引用格式错误")
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
        raise HistoryError("invalid_source_xml", "PubMed XML原件损坏") from None
    if root.tag != "PubmedArticle":
        raise HistoryError("invalid_source_xml", "PubMed原件须为单条PubmedArticle")
    pmid = root.findtext("./MedlineCitation/PMID")
    if not pmid or pmid != item.source_record_id or item.record_kind != "document":
        raise HistoryError("native_id_mismatch", "PubMed PMID与原件不一致")
    article = root.find("./MedlineCitation/Article")
    if article is None:
        raise HistoryError("invalid_source_xml", "PubMed缺Article")
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


def _member_span(text: str, path: list[str], start: int = 0):
    while text[start].isspace():
        start += 1
    if not path:
        _, end = DECODER.raw_decode(text, start)
        return start, end
    if text[start] != "{":
        raise ValueError("预期对象")
    pos = start + 1
    while True:
        while text[pos].isspace() or text[pos] == ",":
            pos += 1
        if text[pos] == "}":
            raise KeyError(path[0])
        key, pos = DECODER.raw_decode(text, pos)
        while text[pos].isspace():
            pos += 1
        if text[pos] != ":":
            raise ValueError("缺冒号")
        pos += 1
        while text[pos].isspace():
            pos += 1
        if key == path[0]:
            return _member_span(text, path[1:], pos)
        _, pos = DECODER.raw_decode(text, pos)


def _raw_array_items(text: str, path: list[str]):
    start, _ = _member_span(text, path)
    if text[start] != "[":
        raise ValueError("预期数组")
    pos = start + 1
    while True:
        while text[pos].isspace() or text[pos] == ",":
            pos += 1
        if text[pos] == "]":
            return
        item, end = DECODER.raw_decode(text, pos)
        if not isinstance(item, dict):
            raise TypeError("预期记录对象")
        yield item, text[pos:end]
        pos = end


def public_fetch_enabled() -> bool:
    """Keep the legacy default; every configured value must explicitly mean true."""
    return os.environ.get("DATAFABRIC_HISTORY_FETCH_ENABLED", "true").strip().lower() == "true"


def require_public_fetch_enabled():
    if not public_fetch_enabled():
        raise HistoryError("public_fetch_disabled", "当前环境已关闭公开来源采集；归档与固定版本读取不受影响", 403)


def read_public(request: FetchRequest) -> dict:
    """读一个来源：按来源目录（fetch/catalog.py）里写的读法调对应的读取函数。返回读到的原件、为什么停、续读位置，
    以及这次经 fetch.transport 发出的请求数和字节数。DATAFABRIC_HISTORY_FETCH_ENABLED=false 时不出网。"""
    from fetch.catalog import spec

    require_public_fetch_enabled()
    plugin = spec(request.source_id).plugin
    transport.SENT.update(requests=0, bytes=0)
    if plugin == "public_api_reader":
        result = _read_public_api(request)
    elif plugin == "site_crawler":  # 浏览器等较重的库，用到时才加载
        from fetch.site_crawler import read_site

        result = read_site(request)
    elif plugin == "who_don":
        from fetch.who_don import read_who

        result = read_who(request)
    elif plugin == "sec_xbrl":
        from fetch.sec_xbrl import read_sec

        result = read_sec(request)
    elif plugin == "gdelt_doc":
        from fetch.gdelt_news import read_gdelt

        result = read_gdelt(request)
    elif plugin == "local_file_import":
        from fetch.local_files import read_local

        result = read_local(request)
    else:
        raise HistoryError("source_not_connected", "该来源在目录中尚未接通", 503)
    return {**result, "requests": transport.SENT["requests"], "bytes": transport.SENT["bytes"]}


PAGE_BYTES = 8 * 1024 * 1024  # 单个接口响应的大小上限（防内存）


def _ok(status):
    if status != 200:
        raise ValueError("HTTP " + str(status))


def _api_page(request, cursor, size, offset):
    """ClinicalTrials / FDA CRL / Europe PMC 一页的请求 → (url, 参数, 记录数组路径)。"""
    source = request.source_id
    if source == "clinicaltrials":
        params = {"format": "json", "pageSize": size, "countTotal": "true"}
        # ClinicalTrials把星号当检索表达式，会过滤掉合法登记号。
        if request.query.strip() != "*":
            params["query.term"] = request.query
        if request.source_record_ids:
            if not all(re.fullmatch(NCT_ID, value) for value in request.source_record_ids):
                raise ValueError("invalid_native_id")
            params["filter.ids"] = ",".join(request.source_record_ids)
        if cursor:
            params["pageToken"] = cursor
        return "https://clinicaltrials.gov/api/v2/studies", params, ["studies"]
    if source == "fda_crl":
        if request.source_record_ids:
            raise HistoryError("by_id_unsupported", "FDA复合记录尚不支持精确by-ID重采")
        # 指定一个顺序：不指定时 openFDA 的顺序是乱的（第一条是 2018 年的），skip 翻页要一个稳定的顺序
        params = {"limit": size, "skip": offset, "sort": "letter_date:desc"}
        if request.query.strip() != "*":
            params["search"] = request.query
        return "https://api.fda.gov/transparency/crl.json", params, ["results"]
    query = request.query
    if request.source_record_ids:
        native = []
        for value in request.source_record_ids:
            if not re.fullmatch(r"[A-Z]+:[A-Za-z0-9.-]+", value):
                raise ValueError("invalid_native_id")
            prefix, ident = value.split(":", 1)
            native.append("(SRC:" + prefix + " EXT_ID:" + ident + ")")
        query = "(" + query + ") AND (" + " OR ".join(native) + ")"
    params = {"query": query, "format": "json", "resultType": "core", "pageSize": size, "cursorMark": cursor,
              "sort": "ID asc"}
    return "https://www.ebi.ac.uk/europepmc/webservices/rest/search", params, ["resultList", "result"]


def _api_native(source, item):
    if source == "clinicaltrials":
        return _pointer(item, TRIAL_PATHS["trial_id"]), "trial"
    if source == "europe_pmc":
        return str(item.get("source", "")) + ":" + str(item.get("id", "")), "document"
    return crl_id(item), "document"


def _api_total(source, data):
    if source == "clinicaltrials":
        total = data.get("totalCount")
        if "totalCount" in data and (not isinstance(total, int) or isinstance(total, bool) or total < 0):
            raise HistoryError("invalid_source_pagination", "临床登记显式命中总数无效，不能确认EOF")
        return total
    if source == "fda_crl":
        return ((data.get("meta") or {}).get("results") or {}).get("total")
    return data.get("hitCount")


class _PublicApiRead:
    """State of one paged read of a public API: the originals read so far and where to resume."""

    def __init__(self, request: FetchRequest):
        self.request = request
        self.budget = transport.Budget(request)  # 截止时间；增量读取时“连续读到已存的就停”
        self.source = request.source_id
        self.records, self.seen, self.source_total = [], {}, None
        self.skipped: list[dict] = []  # 来源列了、但不是一条要存的原件（openFDA 的“待发布”信函占位行），不是读不到
        self.unread: list[dict] = []  # 读到了、但存不了的（CRL 有正文却认不出身份）：网址位置放标识，和站点插件的同名
        self.exhausted, self.error, self.stop = False, None, "record_budget"
        self.cursor = "*" if self.source == "europe_pmc" else None
        if request.continuation:
            self.cursor = request.continuation.cursor
        self.consumed = request.continuation.read_count if request.continuation else 0
        self.next_continuation = self.count_drift = None
        self.previous_cursors = {self.cursor} if self.cursor else set()

    def read_page(self, client) -> bool:
        """Read and absorb one page. True when reading ends here (budget, end of the source, no-match)."""
        request, source = self.request, self.source
        remaining = request.max_records - len(self.records)
        if remaining <= 0:
            return True
        url, params, path = _api_page(request, self.cursor, min(remaining, 100), self.consumed)
        response = transport.get(url, params, client=client, cap=PAGE_BYTES, deadline=self.budget.deadline)
        status, text = response.status_code, response.content.decode("utf-8")
        # openFDA的确定NO_MATCH只表示本查询零命中，不等于API失效。
        if status == 404 and source == "fda_crl":
            failure = parse_json(text).get("error") or {}
            if failure.get("code") == "NOT_FOUND" and failure.get("message") == "No matches found!":
                self.exhausted, self.stop = True, "upstream_no_match"
                return True
        _ok(status)
        data = parse_json(text)
        self.source_total = _api_total(source, data)
        page_items = list(_raw_array_items(text, path))
        self._absorb(page_items)
        next_cursor = self._next_cursor(data, page_items)
        if not page_items and not self.exhausted:
            raise HistoryError("unexpected_empty_page", "来源仍有后续标记但返回空页")
        if source in {"clinicaltrials", "europe_pmc", "fda_crl"}:
            self.next_continuation = None if self.exhausted else {
                "cursor": next_cursor, "read_count": self.consumed}
        if self.exhausted:
            self.stop = "upstream_exhausted"
            return True
        if len(self.records) >= request.max_records:
            self.stop = "record_budget"
            return True
        if source != "fda_crl":
            if next_cursor in self.previous_cursors:
                raise HistoryError("repeated_page_cursor", "来源分页标记重复")
            self.previous_cursors.add(next_cursor)
            self.cursor = next_cursor
        return False

    def _absorb(self, page_items) -> None:
        for data_item, raw in page_items:
            try:
                native, kind = _api_native(self.source, data_item)
            except HistoryError as exc:
                # openFDA CRL 里认不出身份的行，一条不卡住整次读取：没有正文的是还没发布的信函（file_name
                # "Under Review for Release"，没有类型、申请号），没有内容可存，记在 skipped_items；有正文却缺类型或申请号的
                # 是老信函，不猜编号，记为读不到（unread_items，和站点插件一样）。别的来源仍然报错
                if self.source != "fda_crl":
                    raise
                name = str(data_item.get("file_name"))
                if data_item.get("text"):
                    self.unread.append({"url": name, "reason": exc.message})
                else:
                    self.skipped.append({"id": name, "reason": "信函待发布：没有类型、申请号和正文"})
                continue
            item = ArchiveRecord(source_record_id=native, record_kind=kind, raw_content=raw)
            project_record(self.source, item)
            if native in self.seen:
                if self.seen[native] != sha256(raw):
                    self.records = [value for value in self.records if value.source_record_id != native]
                    raise HistoryError("ambiguous_native_id", "同批同来源ID对应不同原件，拒绝猜测修订顺序")
                continue
            self.seen[native] = sha256(raw)
            self.records.append(item)
            self.budget.known(item)  # 增量读取：连续读到已存没变的，抛 Stop，已读的保留
            if len(self.records) >= self.request.max_records:
                break

    def _next_cursor(self, data, page_items):
        """The source's own paging rule: the next page marker, with exhaustion decided (and checked) per source."""
        if self.source == "clinicaltrials":
            return self._clinicaltrials_cursor(data, page_items)
        if self.source == "europe_pmc":
            return self._europe_pmc_cursor(data, page_items)
        # openFDA 用 skip 翻页：位置就是已读过的条数（续读时从 continuation.read_count 接上）
        self.consumed += len(page_items)
        self.exhausted = isinstance(self.source_total, int) and self.consumed >= self.source_total
        return "skip"

    def _clinicaltrials_cursor(self, data, page_items):
        next_cursor = data.get("nextPageToken")
        self.consumed += len(page_items)
        if not next_cursor and self.source_total is not None and self.consumed < self.source_total:
            raise HistoryError("invalid_source_pagination", "临床登记仍有未读命中但缺少续页标记，不能视为EOF")
        if next_cursor and (not isinstance(next_cursor, str) or not page_items
                            or next_cursor in self.previous_cursors):
            raise HistoryError("invalid_source_pagination", "临床登记分页中断，不能视为EOF")
        self.exhausted = not bool(next_cursor)
        return next_cursor

    def _europe_pmc_cursor(self, data, page_items):
        next_cursor = data.get("nextCursorMark")
        source_total = self.source_total
        if not isinstance(source_total, int) or isinstance(source_total, bool) or source_total < 0:
            raise HistoryError("invalid_source_pagination", "Europe PMC缺可信命中总数，不能确认EOF")
        # Europe PMC recounts hitCount: a record updated again inside the fixed UPDATE_DATE window leaves it, so the
        # count can fall mid-paging. An empty page without a new marker after reads is its documented end; treat
        # it as read to the end and record the drifted count next to the read count in the coverage statement.
        drifted = not page_items and self.consumed > 0 and (not next_cursor or next_cursor == self.cursor)
        self.consumed += len(page_items)
        self.exhausted = self.consumed >= source_total or drifted
        if drifted and self.consumed < source_total:
            self.count_drift = {"source_total": source_total, "read_count": self.consumed}
        if not self.exhausted and (not page_items or not next_cursor or next_cursor == self.cursor):
            raise HistoryError("invalid_source_pagination", "Europe PMC仍有未读命中但分页中断，不能视为EOF")
        return next_cursor

    def result(self) -> dict:
        return {
            "records": self.records,
            "complete": False,
            "upstream_pagination_exhausted": self.exhausted,
            "stop_reason": self.stop,
            "error": self.error,
            "source_total": self.source_total,
            "count_drift": self.count_drift,
            "next_continuation": self.next_continuation if not self.error else None,
            "skipped_items": self.skipped,
            "unread_items": self.unread,
        }


def _read_public_api(request: FetchRequest) -> dict:
    """ClinicalTrials / Europe PMC / FDA CRL 的公开接口分页读取（PubMed 见 _read_pubmed）。返回真实完成/截断/失败
    及已读原件；HTTP异常绝不伪装EOF。预算用完停在最后读完的一页，续读位置指向下一页。"""
    from fetch.catalog import spec

    spec(request.source_id)  # 来源必须在采集目录里（不在则 KeyError）
    if request.source_id == "pubmed":
        return _read_pubmed(request)
    read = _PublicApiRead(request)
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        try:
            for _ in range(request.max_pages):
                if read.read_page(client):
                    break
            else:
                read.stop = "page_budget"
        except transport.Stop as stopped:
            read.stop = stopped.reason  # 停在最后读完的一页，续读位置不变
            if read.stop == transport.KNOWN:  # 读到旧的了，没有“下一页”：第二页起读到已存的，上一页留下的位置不能再给出去
                read.next_continuation = None
        except HistoryError as exc:
            read.error = {"code": exc.code, "message": exc.message}
            read.stop = "source_failed"
        except (httpx.HTTPError, UnicodeError, ValueError, TypeError, KeyError, IndexError):
            read.error = {"code": "source_read_failed", "message": "公开来源读取或格式校验失败；已读原件单独保留"}
            read.stop = "source_failed"
    if read.unread and read.stop in ("upstream_exhausted", transport.KNOWN) and read.error is None:
        # 读到底（或读到旧的）了，但有几条存不了：和站点插件一样不能说读完，运行标“部分完成”
        first = read.unread[0]
        read.error = {"code": "items_unread",
                      "message": f"有 {len(read.unread)} 条读不到，如 {first['url']}：{first['reason']}"}
        read.stop = "items_unread"
    return read.result()


def _pubmed_cursor(request):
    if not request.continuation:
        return 0, False
    try:
        value = json.loads(request.continuation.cursor)
        return int(value["i"]), bool(value.get("a"))
    except (ValueError, KeyError, TypeError):
        raise HistoryError("invalid_cursor", "续读位置无效") from None


def _pubmed_articles(raw_xml, ids):
    """EFetch 返回的 Article 原件片段（保留XML原始字符，不重序列化），按请求的 PMID 顺序。"""
    root = ET.fromstring(raw_xml)
    if root.tag != "PubmedArticleSet":
        raise ValueError("invalid_efetch")
    by_id = {}
    for raw in re.findall(r"<PubmedArticle(?:\s[^>]*)?>.*?</PubmedArticle>", raw_xml, re.DOTALL):
        pmid = ET.fromstring(raw).findtext("./MedlineCitation/PMID")
        if pmid not in ids:
            raise ValueError("unrequested_id")
        item = ArchiveRecord(source_record_id=pmid, record_kind="document", raw_content=raw,
                             raw_media_type="application/xml")
        project_record("pubmed", item)
        by_id[pmid] = item
    if set(by_id) != set(ids):
        raise ValueError("missing_ids")
    return [by_id[pmid] for pmid in ids]


PUBMED = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


def _pubmed_ids(client, request, start, deadline):
    """这一次要取的 PMID：给定的清单，或 ESearch 的结果（不指定排序时新入库的在前）。返回 (命中总数, 本次的 PMID)。"""
    if request.source_record_ids:
        if not all(re.fullmatch(r"\d+", value) for value in request.source_record_ids):
            raise ValueError("invalid_native_id")
        return len(request.source_record_ids), request.source_record_ids[start: start + request.max_records]
    response = transport.get(PUBMED + "esearch.fcgi", {
        "db": "pubmed", "retmode": "json", "term": request.query, "retstart": start,
        "retmax": request.max_records}, client=client, cap=1024 * 1024, deadline=deadline)
    _ok(response.status_code)
    found = parse_json(response.content.decode("utf-8")).get("esearchresult")
    if not isinstance(found, dict) or "count" not in found or not isinstance(found.get("idlist"), list):
        raise ValueError("invalid_search")
    return int(found["count"]), found["idlist"]


def _read_pubmed(request):
    """PubMed：ESearch 找 PMID（或给定的 PMID 清单），EFetch 取摘要原件；有合法全文的另成附件版本
    （fetch.pubmed_fulltext）。文献和全文都占 max_records；额度或其他预算用完就停，给续读位置：
    cursor = {"i": 结果里第几篇, "a": 1 表示这一篇的文献版本已保存、只差全文}（与站点插件同一写法）。"""
    from fetch.pubmed_fulltext import fulltext

    budget = transport.Budget(request)
    start, fulltext_only = _pubmed_cursor(request)
    records, coverage, count = [], [], None
    stop, error, resume = "upstream_exhausted", None, (start, int(fulltext_only))
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        try:
            count, ids = _pubmed_ids(client, request, start, budget.deadline)
            if not ids:
                if start < count:
                    raise ValueError("unexpected_empty_ids")
                stop, resume = ("upstream_no_match" if count == 0 else "upstream_exhausted"), None
            else:
                response = transport.get(PUBMED + "efetch.fcgi", {"db": "pubmed", "retmode": "xml", "id": ",".join(ids)},
                                         client=client, cap=7 * 1024 * 1024, deadline=budget.deadline)
                _ok(response.status_code)
                for k, item in enumerate(_pubmed_articles(response.content.decode("utf-8"), ids)):
                    resume = (start + k, 0)
                    if not (k == 0 and fulltext_only):  # 续读时这一篇已保存，只为取全文重取
                        budget.take_record()
                        records.append(item)
                        if budget.known(item):  # 增量读取：已存且没变，全文也是上一轮存的，不再取（连续够数就抛 Stop）
                            continue
                    resume = (start + k, 1)
                    facts = _project_pubmed(item)["facts"]
                    found_records, found_coverage = fulltext(
                        client, budget, item.source_record_id, facts.get("pmcid"), facts.get("doi"))
                    records += found_records
                    coverage += found_coverage
                done = start + len(ids)
                stop, resume = ("upstream_exhausted", None) if done >= count else ("record_budget", (done, 0))
        except transport.Stop as stopped:
            stop = stopped.reason
            if stop == transport.KNOWN:  # 读到旧的了，没有“下一页”
                resume = None
        except (httpx.HTTPError, ValueError, ET.ParseError, HistoryError):
            error = {"code": "source_read_failed", "message": "PubMed读取或EFetch原件校验失败"}
            stop = "source_failed"
    return {
        "records": records,
        "complete": False,
        "upstream_pagination_exhausted": error is None and resume is None and stop != transport.KNOWN,
        "stop_reason": stop,
        "error": error,
        "source_total": count,
        "attachment_coverage": coverage,
        "next_continuation": None if error or resume is None else {
            "cursor": json.dumps({"i": resume[0], "a": resume[1]}),
            "read_count": resume[0],
        },
    }
