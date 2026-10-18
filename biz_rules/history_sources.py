"""固定公开来源读取与确定性投影；只读小批，不推断项目身份或临床成败。"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx

from biz_rules.history_models import (
    ArchiveRecord,
    FetchRequest,
    HistoryError,
    canonical,
    sha256,
    source_time,
)

SOURCE_CATALOG = {
    "clinicaltrials": {
        "name": "ClinicalTrials.gov",
        "business_types": ["trials_pipeline"],
        "acquisition_channel": "public_api",
        "access": "public_registry",
        "license_url": "https://clinicaltrials.gov/data-api/about-api",
        "history_coverage": "本应用采用后不可变版本；当前API不是过去快照",
        "native_id": "NCT ID",
        "read_scope": "公开登记字段，含未决/终止记录",
        "limitations": ["登记状态不是药物成败；日期保留原精度和ACTUAL/ESTIMATED"],
    },
    "pubmed": {
        "name": "PubMed",
        "business_types": ["literature"],
        "acquisition_channel": "public_api",
        "access": "public_metadata",
        "license_url": "https://www.ncbi.nlm.nih.gov/home/about/policies/",
        "history_coverage": "本应用采用后版本；无匿名十年完整快照保证",
        "native_id": "PMID",
        "read_scope": "EFetch元数据、摘要和更正关系；不下载未授权全文",
        "limitations": ["同PMID/DOI跨库不构成独立研究证据"],
    },
    "fda_crl": {
        "name": "FDA Complete Response Letters",
        "business_types": ["regulatory"],
        "acquisition_channel": "public_api",
        "access": "public_metadata",
        "license_url": "https://open.fda.gov/license/",
        "history_coverage": "当前公开记录的小批采用；信函日期不证明当时可得",
        "native_id": "file_name + letter_date/type/application_number 复合键",
        "read_scope": "公开信函文本及原生字段；不把所有letter_type当否决",
        "limitations": ["同PDF可有多轮信；复合键歧义须拒绝，不猜项目谱系"],
    },
    "europe_pmc": {
        "name": "Europe PMC",
        "business_types": ["literature"],
        "acquisition_channel": "public_api",
        "access": "public_metadata",
        "license_url": "https://europepmc.org/Copyright",
        "history_coverage": "当前元数据/更正关系采用；非过去全文版本档案",
        "native_id": "source:id",
        "read_scope": "core元数据/摘要/许可和关系；不自动抓全文",
        "limitations": [
            "firstPublicationDate可能补算，保留原值，不作历史原件证明",
            "PMID/DOI跨库复用不是新独立研究；正文另核许可",
        ],
    },
    "who_don": {
        "name": "WHO Disease Outbreak News",
        "business_types": ["population_need"],
        "acquisition_channel": "public_api",
        "access": "public_metadata",
        "license_url": "https://www.who.int/about/policies/publishing/copyright",
        "history_coverage": "本地采用后版本；当前公告API不提供过去原件资格",
        "native_id": "WHO原生Id（DonId单独保留）",
        "read_scope": "明确LastModified时间窗内DON公告、原文和发布元数据",
        "limitations": [
            "DON是选择性公告，不是全球疫情全量；零公告不证明没有暴发",
            "发布/更新日期不是暴发开始；不从叙述推病例数、发病率或专业确认",
        ],
    },
    "legacy_documents": {
        "name": "已授权旧文档的精确冻结桥",
        "business_types": ["trials_pipeline", "regulatory", "literature"],
        "acquisition_channel": "derived_export",
        "access": "controlled_import",
        "license_url": None,
        "history_coverage": "本次采用后版本；旧文档日期不是历史图证明",
        "native_id": "原source_site+source_doc_uuid",
        "read_scope": "明确范围内原文；许可取全部原站依赖交集",
        "dependency_source_ids": [],
        "limitations": ["派生来源组保守授权；任一依赖撤权则整个来源组停读"],
    },
    "reviewed_graph": {
        "name": "唐的有界审核图发布",
        "business_types": ["mechanisms", "trials_pipeline"],
        "acquisition_channel": "derived_export",
        "access": "controlled_import",
        "license_url": None,
        "history_coverage": "发布后版本；不回填当前别名/合并到过去",
        "native_id": "明确选择范围及批次；包内namespace+源实体/关系ID",
        "read_scope": "所选发布子图；图审核不等于专业证据评价",
        "dependency_source_ids": [],
        "limitations": ["不是全图完整邻域；任何原来源撤权保守封整个派生来源组"],
    },
    "primekg_graph": {
        "name": "PrimeKG 当前有界背景",
        "business_types": ["mechanisms"],
        "acquisition_channel": "derived_export",
        "access": "background_context",
        "license_url": None,
        "history_coverage": "本地first_seen后可回看已收到的背景；无外部过去档案或文档证据",
        "native_id": "原生namespace+ID及有向关系",
        "read_scope": "管理员明确获准的当前有界捕获，不自动联网或扩展全图",
        "dependency_source_ids": [],
        "limitations": ["不当作新独立研究、机制核验或过去时点事实；许可另核"],
    },
    "external_archive": {
        "name": "受控历史归档",
        "business_types": [
            "trials_pipeline",
            "regulatory",
            "literature",
            "corporate_finance",
            "population_need",
        ],
        "acquisition_channel": "institutional_upload",
        "access": "controlled_import",
        "license_url": None,
        "history_coverage": "按每个明确声明/审核包记录，不承诺现有十年档案",
        "native_id": "归档原站ID；demo和真实资料隔离",
        "read_scope": "固定trial/program/outcome/document原件及同口径metric/event声明/字段绑定；不提供专业真实性判断",
        "limitations": [
            "普通导入未审核；不能倒填本地首次见到时间",
            "映射只接纳有出处审核，不做唐蔚雯负责的消歧",
        ],
    },
}


# 既有采集库存的来源许可目录；这里只声明桥接能力，不宣称新增站点抓取已接通。
for _legacy_id, _name, _types in (
    ("fda_api_drug_label", "既有FDA药品标签库存", ["regulatory"]),
    ("fda_api_enforcement", "既有FDA执法/召回库存", ["regulatory"]),
    ("chictr", "既有中国临床试验注册库存", ["trials_pipeline"]),
    ("nmpa", "既有国家药监局资料", ["regulatory"]),
    ("cde_xxgk", "既有CDE信息公开库存", ["regulatory"]),
    ("cde", "既有CDE资料", ["regulatory"]),
):
    SOURCE_CATALOG[_legacy_id] = {
        "name": _name,
        "business_types": _types,
        "acquisition_channel": "legacy_database",
        "access": "explicit_source_allowlist",
        "license_url": None,
        "history_coverage": "仅从本次自动发表后形成不可变版本；不是原站历史档案",
        "native_id": "原source_site + document UUID",
        "read_scope": "已入库清洗正文的版本化桥接；不代表该源新抓取接口或全量覆盖",
        "limitations": ["需管理员逐源许可；不保证原站覆盖、原日期语义或专业结论"],
    }


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
    if source_id == "pubmed" and item.raw_media_type == "application/xml":
        return _project_pubmed(item)
    if item.raw_media_type != "application/json":
        raise HistoryError("unsupported_media", "该来源只支持保留的JSON原件")
    data = parse_json(item.raw_content)
    if source_id == "who_don":
        from biz_rules.who_don import project_who

        return project_who(item, data)
    if (
        source_id == "external_archive"
        and data.get("schema_version") == "datafabric.observation.v1"
    ):
        from biz_rules.observations import project_observation

        try:
            return project_observation(item, data)
        except ValueError:
            raise HistoryError(
                "invalid_observation", "观测值、期间或口径不符合固定契约"
            ) from None
    if source_id in {"legacy_documents", "reviewed_graph", "primekg_graph"}:
        from biz_rules.reviewed_graph import project_graph_record

        try:
            return project_graph_record(source_id, item, data)
        except ValueError:
            raise HistoryError(
                "invalid_graph_fields", "图包或旧文档桥字段不符合固定契约"
            ) from None
    source_url, published, updated, event_time = None, None, None, None
    if source_id == "clinicaltrials":
        facts, locators = _facts(data, TRIAL_PATHS)
        for key in ("phase", "conditions"):
            if facts.get(key) is not None and (
                not isinstance(facts[key], list)
                or any(not isinstance(value, str) for value in facts[key])
            ):
                raise HistoryError(
                    "invalid_source_fields", "临床登记数组字段类型不合法"
                )
        if facts.get("status") is not None and not isinstance(facts["status"], str):
            raise HistoryError("invalid_source_fields", "临床登记状态须为来源字符串")
        number = facts.get("enrollment")
        if number is not None and (
            not isinstance(number, int) or isinstance(number, bool) or number < 0
        ):
            raise HistoryError(
                "invalid_source_fields", "临床登记人数类型不合法；未知不补零"
            )
        native = facts.get("trial_id")
        if (
            not isinstance(native, str)
            or not re.fullmatch(r"NCT\d{8}", native)
            or native != item.source_record_id
        ):
            raise HistoryError("native_id_mismatch", "临床登记ID与原件不一致")
        title = (
            _pointer(data, "/protocolSection/identificationModule/briefTitle") or native
        )
        source_url = "https://clinicaltrials.gov/study/" + native
        published = source_time(
            _pointer(
                data, "/protocolSection/statusModule/studyFirstPostDateStruct/date"
            ),
            "registry_first_posted",
        )
        updated = source_time(
            _pointer(
                data, "/protocolSection/statusModule/lastUpdatePostDateStruct/date"
            ),
            "registry_update_posted",
        )
        event_time = source_time(
            facts.get("start_date"),
            "registry_start_" + str(facts.get("start_date_type", "unknown")),
        )
        clean_text = _pointer(
            data, "/protocolSection/descriptionModule/briefSummary"
        ) or str(title)
        kind = "trial"
    elif source_id == "fda_crl":
        if item.source_record_id != crl_id(data):
            raise HistoryError("native_id_mismatch", "FDA复合来源ID与原件不一致")
        fields = {
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
        facts, locators = _facts(data, fields)
        title = " / ".join(
            str(data.get(k, "")) for k in ("file_name", "letter_type", "letter_date")
        )
        clean_text = data.get("text") or ""
        if not isinstance(clean_text, str):
            raise HistoryError("invalid_source_fields", "FDA信函text字段不是文本")
        date = data.get("letter_date")
        try:
            event_time = source_time(
                datetime.strptime(date, "%m/%d/%Y").date().isoformat(), "letter_signed"
            )
        except (TypeError, ValueError):
            event_time = source_time(date, "letter_signed")
        # 信函签字日期不是网页公开日期，不能自动填source_published_at。
        kind = "document"
    elif source_id == "europe_pmc":
        native = str(data.get("source", "")) + ":" + str(data.get("id", ""))
        if (
            not data.get("source")
            or not data.get("id")
            or native != item.source_record_id
        ):
            raise HistoryError("native_id_mismatch", "Europe PMC source:id与原件不一致")
        fields = {
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
        }
        facts, locators = _facts(data, fields)
        title = data.get("title") or native
        clean_text = str(title) + "\n\n" + str(data.get("abstractText") or "")
        published = source_time(
            data.get("firstPublicationDate"),
            "europepmc_first_publication_may_be_imputed",
        )
        updated = source_time(data.get("dateOfRevision"), "europepmc_revision")
        source_url = (
            "https://europepmc.org/article/"
            + str(data["source"])
            + "/"
            + str(data["id"])
        )
        kind = "document"
    elif source_id == "external_archive":
        kind = data.get("record_kind")
        if kind not in ("trial", "program", "outcome", "document"):
            raise HistoryError(
                "unsupported_record_kind",
                "历史原件须明确trial/program/outcome/document",
            )
        facts, locators = _facts(
            data, {key: "/" + key for key in sorted(EXTERNAL_FIELDS)}
        )
        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            raise HistoryError("invalid_source_fields", "历史原件须有明确标题")
        if kind == "trial" and not isinstance(facts.get("trial_id"), str):
            raise HistoryError("invalid_source_fields", "trial原件缺登记ID")
        if kind == "program" and not isinstance(facts.get("program_id"), str):
            raise HistoryError("invalid_source_fields", "program原件缺有出处项目ID")
        if kind == "outcome":
            _validate_outcome(facts)
        for key in (
            "mapping_available_at",
            "public_available_at",
            "observation_start",
            "observation_end",
            "event_at",
        ):
            if facts.get(key) is not None and source_time(facts[key]) is None:
                raise HistoryError("invalid_source_time", "历史原件时间格式/时区不合法")
        clean_text = str(title) + "\n\n" + str(facts.get("summary") or "")
        published = source_time(
            data.get("source_published_at"), "archive_source_published"
        )
        updated = source_time(data.get("source_updated_at"), "archive_source_updated")
        event_time = (
            source_time(facts.get("event_at"), "outcome_event")
            if kind == "outcome"
            else source_time(data.get("event_time"), "archive_source_event")
        )
        source_url = data.get("source_reference")
        if source_url is not None and (
            not isinstance(source_url, str) or len(source_url) > 2000
        ):
            raise HistoryError("invalid_source_fields", "来源引用格式错误")
    else:
        raise HistoryError("unsupported_source", "该来源没有可执行的固定投影")
    if item.record_kind != kind:
        raise HistoryError("record_kind_mismatch", "原件类型与导入声明不一致")
    if not isinstance(title, str) or len(title) > 10000:
        raise HistoryError("invalid_source_fields", "来源标题格式错误")
    return {
        "record_kind": kind,
        "title": title,
        "source_url": source_url,
        "clean_text": clean_text,
        "facts": facts,
        "field_locators": locators,
        "event_time": event_time.model_dump(mode="json") if event_time else None,
        "source_published_at": published.model_dump(mode="json") if published else None,
        "source_updated_at": updated.model_dump(mode="json") if updated else None,
    }


def _validate_outcome(facts):
    if facts.get("event_type") not in (
        "trial_stoppage",
        "regulatory_approval",
        "next_phase_disclosure",
    ):
        raise HistoryError("unsupported_outcome", "结局类型尚不支持")
    if facts.get("unit_kind") not in ("trial", "program") or not isinstance(
        facts.get("unit_id"), str
    ):
        raise HistoryError("invalid_outcome", "结局须明确观察单位")
    observed = facts.get("event_observed")
    if observed is not None and not isinstance(observed, bool):
        raise HistoryError("invalid_outcome", "event_observed须为true/false/null")
    if observed is True and not facts.get("event_at"):
        raise HistoryError("invalid_outcome", "发生事件必须有事件时间")
    if observed is False and facts.get("event_at"):
        raise HistoryError("invalid_outcome", "阴性观察不能同时声称发生时间")
    if observed is False and (
        not facts.get("observation_start") or not facts.get("observation_end")
    ):
        raise HistoryError("invalid_outcome", "阴性结局必须有明确观察窗口")
    start, end = (
        source_time(facts.get("observation_start")),
        source_time(facts.get("observation_end")),
    )
    if start and end and start.upper_bound() > end.upper_bound():
        raise HistoryError("invalid_outcome", "观察窗口起止颠倒")


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


def read_public(request: FetchRequest) -> dict:
    """返回真实完成/截断/失败及已读原件。HTTP异常绝不伪装EOF。"""
    if request.source_id == "who_don":
        from biz_rules.who_don import read_who

        return read_who(request)
    records, seen, requests_count, source_total = [], {}, 0, None
    exhausted, error, stop = False, None, "record_budget"
    began = time.monotonic()
    max_bytes = 8 * 1024 * 1024
    bytes_read = 0
    cursor = "*" if request.source_id == "europe_pmc" else None
    if request.continuation:
        cursor = request.continuation.cursor
    consumed = request.continuation.read_count if request.continuation else 0
    next_continuation = None
    previous_cursors = {cursor} if cursor else set()
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        try:
            for page in range(request.max_pages):
                if time.monotonic() - began >= request.deadline_seconds:
                    stop = "deadline"
                    break
                remaining = request.max_records - len(records)
                if remaining <= 0:
                    break
                size = min(remaining, 100)
                if request.source_id == "clinicaltrials":
                    url = "https://clinicaltrials.gov/api/v2/studies"
                    params = {
                        "format": "json",
                        "pageSize": size,
                        "query.term": request.query,
                    }
                    if request.source_record_ids:
                        if not all(
                            re.fullmatch(r"NCT\d{8}", value)
                            for value in request.source_record_ids
                        ):
                            raise ValueError("invalid_native_id")
                        params["filter.ids"] = ",".join(request.source_record_ids)
                    if cursor:
                        params["pageToken"] = cursor
                    path = ["studies"]
                elif request.source_id == "fda_crl":
                    url = "https://api.fda.gov/transparency/crl.json"
                    params = {"limit": size, "skip": len(records)}
                    if request.query.strip() != "*":
                        params["search"] = request.query
                    if request.source_record_ids:
                        raise HistoryError(
                            "by_id_unsupported", "FDA复合记录尚不支持精确by-ID重采"
                        )
                    path = ["results"]
                elif request.source_id == "europe_pmc":
                    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
                    query = request.query
                    if request.source_record_ids:
                        native = []
                        for value in request.source_record_ids:
                            if not re.fullmatch(r"[A-Z]+:[A-Za-z0-9.-]+", value):
                                raise ValueError("invalid_native_id")
                            source, ident = value.split(":", 1)
                            native.append("(SRC:" + source + " EXT_ID:" + ident + ")")
                        query = "(" + query + ") AND (" + " OR ".join(native) + ")"
                    params = {
                        "query": query,
                        "format": "json",
                        "resultType": "core",
                        "pageSize": size,
                        "cursorMark": cursor,
                        "sort": "ID asc",
                    }
                    path = ["resultList", "result"]
                else:
                    return _read_pubmed(request, client, began)
                requests_count += 1
                remaining_time = request.deadline_seconds - (time.monotonic() - began)
                with client.stream(
                    "GET", url, params=params, timeout=max(0.1, min(20, remaining_time))
                ) as response:
                    # openFDA的确定NO_MATCH只表示本查询零命中，不等于API失效。
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() - began >= request.deadline_seconds:
                            raise HistoryError(
                                "source_deadline", "公开读取超过时间预算"
                            )
                        body.extend(chunk)
                        if len(body) + bytes_read > max_bytes:
                            raise HistoryError(
                                "source_byte_budget", "公开读取超过8MiB预算"
                            )
                    text = bytes(body).decode("utf-8")
                    if response.status_code == 404 and request.source_id == "fda_crl":
                        failure = parse_json(text).get("error") or {}
                        if (
                            failure.get("code") == "NOT_FOUND"
                            and failure.get("message") == "No matches found!"
                        ):
                            exhausted, stop = True, "upstream_no_match"
                            break
                    response.raise_for_status()
                bytes_read += len(body)
                data = parse_json(text)
                if request.source_id == "fda_crl":
                    source_total = ((data.get("meta") or {}).get("results") or {}).get(
                        "total"
                    )
                elif request.source_id == "europe_pmc":
                    source_total = data.get("hitCount")
                page_items = list(_raw_array_items(text, path))
                for data_item, raw in page_items:
                    if request.source_id == "clinicaltrials":
                        native = _pointer(data_item, TRIAL_PATHS["trial_id"])
                        kind = "trial"
                    elif request.source_id == "europe_pmc":
                        native = (
                            str(data_item.get("source", ""))
                            + ":"
                            + str(data_item.get("id", ""))
                        )
                        kind = "document"
                    else:
                        native, kind = crl_id(data_item), "document"
                    item = ArchiveRecord(
                        source_record_id=native, record_kind=kind, raw_content=raw
                    )
                    project_record(request.source_id, item)
                    if native in seen:
                        if seen[native] != sha256(raw):
                            records = [
                                value
                                for value in records
                                if value.source_record_id != native
                            ]
                            raise HistoryError(
                                "ambiguous_native_id",
                                "同批同来源ID对应不同原件，拒绝猜测修订顺序",
                            )
                        continue
                    seen[native] = sha256(raw)
                    records.append(item)
                    if len(records) >= request.max_records:
                        break
                if request.source_id == "clinicaltrials":
                    next_cursor = data.get("nextPageToken")
                    exhausted = not bool(next_cursor)
                elif request.source_id == "europe_pmc":
                    next_cursor = data.get("nextCursorMark")
                    if (
                        not isinstance(source_total, int)
                        or isinstance(source_total, bool)
                        or source_total < 0
                    ):
                        raise HistoryError(
                            "invalid_source_pagination",
                            "Europe PMC缺可信命中总数，不能确认EOF",
                        )
                    consumed += len(page_items)
                    exhausted = consumed >= source_total
                    if not exhausted and (
                        not page_items or not next_cursor or next_cursor == cursor
                    ):
                        raise HistoryError(
                            "invalid_source_pagination",
                            "Europe PMC仍有未读命中但分页中断，不能视为EOF",
                        )
                else:
                    next_cursor = None
                    exhausted = (
                        isinstance(source_total, int) and len(records) >= source_total
                    )
                if not page_items and not exhausted:
                    raise HistoryError(
                        "unexpected_empty_page", "来源仍有后续标记但返回空页"
                    )
                if request.source_id == "europe_pmc":
                    next_continuation = (
                        None
                        if exhausted
                        else {
                            "scope_hash": request.scope_hash(),
                            "cursor": next_cursor,
                            "read_count": consumed,
                        }
                    )
                if exhausted:
                    stop = "upstream_exhausted"
                    break
                if len(records) >= request.max_records:
                    stop = "record_budget"
                    break
                if request.source_id != "fda_crl":
                    if next_cursor in previous_cursors:
                        raise HistoryError("repeated_page_cursor", "来源分页标记重复")
                    previous_cursors.add(next_cursor)
                    cursor = next_cursor
                time.sleep(0.4)
            else:
                stop = "page_budget"
        except HistoryError as exc:
            error = {"code": exc.code, "message": exc.message}
            stop = "source_failed"
        except (
            httpx.HTTPError,
            UnicodeError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
        ):
            error = {
                "code": "source_read_failed",
                "message": "公开来源读取或格式校验失败；已读原件单独保留",
            }
            stop = "source_failed"
    return {
        "records": records,
        "complete": False,
        "upstream_pagination_exhausted": exhausted,
        "stop_reason": stop,
        "error": error,
        "requests": requests_count,
        "bytes": bytes_read,
        "source_total": source_total,
        "next_continuation": next_continuation if not error else None,
    }


def _bounded_text(client, url, params, began, deadline, budget):
    remaining = deadline - (time.monotonic() - began)
    if remaining <= 0:
        raise ValueError("deadline")
    chunks, size = [], 0
    with client.stream(
        "GET", url, params=params, timeout=max(0.1, min(20, remaining))
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > budget or time.monotonic() - began >= deadline:
                raise ValueError("read_budget")
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


def _read_pubmed(request, client, began):
    # 新历史链用EFetch真实摘要，不复用ESummary不存在的summary字段；不抓PMC/PDF。
    records = []
    try:
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
        if request.source_record_ids:
            if not all(
                re.fullmatch(r"\d+", value) for value in request.source_record_ids
            ):
                raise ValueError("invalid_native_id")
            ids = request.source_record_ids[: request.max_records]
            count = len(request.source_record_ids)
            calls = 0
        else:
            search_text = _bounded_text(
                client,
                base + "esearch.fcgi",
                {
                    "db": "pubmed",
                    "retmode": "json",
                    "term": request.query,
                    "retmax": request.max_records,
                },
                began,
                request.deadline_seconds,
                1024 * 1024,
            )
            found = parse_json(search_text).get("esearchresult")
            if (
                not isinstance(found, dict)
                or "count" not in found
                or not isinstance(found.get("idlist"), list)
            ):
                raise ValueError("invalid_search")
            count, ids, calls = int(found["count"]), found["idlist"], 1
        if not ids:
            if count:
                raise ValueError("unexpected_empty_ids")
            return {
                "records": [],
                "complete": False,
                "upstream_pagination_exhausted": True,
                "stop_reason": "upstream_no_match",
                "error": None,
                "requests": calls,
                "bytes": 0,
                "source_total": count,
            }
        if time.monotonic() - began >= request.deadline_seconds:
            raise ValueError("deadline")
        time.sleep(0.4)
        raw_xml = _bounded_text(
            client,
            base + "efetch.fcgi",
            {"db": "pubmed", "retmode": "xml", "id": ",".join(ids)},
            began,
            request.deadline_seconds,
            7 * 1024 * 1024,
        )
        root = ET.fromstring(raw_xml)
        if root.tag != "PubmedArticleSet":
            raise ValueError("invalid_efetch")
        # 原件为实际返回的Article片段，保留XML原始字符，不用重序列化替代。
        raw_articles = re.findall(
            r"<PubmedArticle(?:\s[^>]*)?>.*?</PubmedArticle>", raw_xml, re.DOTALL
        )
        for raw in raw_articles:
            parsed = ET.fromstring(raw)
            pmid = parsed.findtext("./MedlineCitation/PMID")
            if pmid not in ids:
                raise ValueError("unrequested_id")
            item = ArchiveRecord(
                source_record_id=pmid,
                record_kind="document",
                raw_content=raw,
                raw_media_type="application/xml",
            )
            project_record("pubmed", item)
            records.append(item)
        if {item.source_record_id for item in records} != set(ids):
            raise ValueError("missing_ids")
        return {
            "records": records,
            "complete": False,
            "upstream_pagination_exhausted": count <= len(records),
            "stop_reason": "upstream_exhausted"
            if count <= len(records)
            else "record_budget",
            "error": None,
            "requests": calls + 1,
            "bytes": len(raw_xml.encode()),
            "source_total": count,
        }
    except (httpx.HTTPError, ValueError, ET.ParseError, HistoryError):
        return {
            "records": records,
            "complete": False,
            "upstream_pagination_exhausted": False,
            "stop_reason": "source_failed",
            "error": {
                "code": "source_read_failed",
                "message": "PubMed读取或EFetch原件校验失败",
            },
            "requests": 0,
            "bytes": 0,
            "source_total": None,
        }
