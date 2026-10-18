"""已清洗文档的自动发布原件；临床字段仍由冻结原文的确定解析取得。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from biz_rules.clinical_research import ProtocolLocator, project_clinical_trial
from biz_rules.history_models import HistoryError, Model, sha256, source_time, utc
from biz_rules.reviewed_graph import DERIVED_SOURCES


class CollectedDocument(Model):
    schema_version: Literal["datafabric.collected.document.v1"] = (
        "datafabric.collected.document.v1"
    )
    source_doc_uuid: str = Field(min_length=1, max_length=400)
    source_site: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=10000)
    source_url: str | None = Field(default=None, max_length=4000)
    original_raw_content: str
    original_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publish_date_raw: str | None = None
    original_created_at: str | None = None
    original_updated_at: str | None = None
    publication_state: Literal["active", "withdrawn"] = "active"
    state_observed_at: datetime
    capture_reason: Literal[
        "insert",
        "update",
        "backfill",
        "changed_body",
        "source_deleted",
        "graph_reference",
    ]
    publication_role: Literal["source_record", "context_reference"] = "source_record"
    previous_raw_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_projection_version: Literal["clinicaltrials.protocol.v1"] | None = None
    source_projection: dict[str, Any] | None = None
    protocol_locator: ProtocolLocator | None = None

    @model_validator(mode="after")
    def bound_source(self):
        utc(self.state_observed_at)
        if (self.capture_reason == "graph_reference") != (
            self.publication_role == "context_reference"
        ):
            raise ValueError("图旧原件引用不得冒充新的当前来源正文")
        if (
            self.source_site in DERIVED_SOURCES
            or sha256(self.original_raw_content) != self.original_raw_sha256
        ):
            raise ValueError("原站或冻结正文SHA不一致")
        if self.publication_state == "withdrawn":
            if (
                self.original_raw_content
                or self.source_projection is not None
                or self.protocol_locator is not None
            ):
                raise ValueError("撤回记录仅保存状态，不复制正文或事实")
            return self
        if not self.original_raw_content:
            raise ValueError("空正文不能冒充成功发布或删除")
        projection = (
            project_clinical_trial(self.original_raw_content)
            if self.source_site == "clinicaltrials"
            else None
        )
        if self.source_projection is not None:
            if projection is None or projection.status != "available":
                raise ValueError("临床投影没有可核对的冻结原片段")
            if (
                self.source_projection
                != {"protocolSection": json.loads(projection.text)}
                or self.protocol_locator != projection.locator
                or self.source_projection_version != projection.version
            ):
                raise ValueError("临床事实/路径与冻结原文不一致")
        elif (
            self.source_projection_version is not None
            or self.protocol_locator is not None
        ):
            raise ValueError("临床投影字段必须一起提供")
        return self


def collected_document(snapshot, observed_at, capture_reason):
    raw = snapshot.get("raw_content") or ""
    withdrawn = snapshot.get("is_deleted") is True
    previous = snapshot.get("previous_raw_sha256") or (
        sha256(raw) if withdrawn and raw else None
    )
    value = {
        "source_doc_uuid": snapshot["uuid"],
        "source_site": snapshot.get("source_site") or "unknown",
        "title": snapshot.get("title") or snapshot["uuid"],
        "source_url": snapshot.get("source_url") or None,
        "original_raw_content": "" if withdrawn else raw,
        "original_raw_sha256": sha256("" if withdrawn else raw),
        "publish_date_raw": snapshot.get("publish_date") or None,
        "original_created_at": snapshot.get("created_at"),
        "original_updated_at": snapshot.get("updated_at"),
        "publication_state": "withdrawn" if withdrawn else "active",
        "state_observed_at": observed_at,
        "capture_reason": "source_deleted" if withdrawn else capture_reason,
        "publication_role": "context_reference"
        if capture_reason == "graph_reference" and not withdrawn
        else "source_record",
        "previous_raw_sha256": previous,
    }
    if not withdrawn and value["source_site"] == "clinicaltrials":
        projection = project_clinical_trial(raw)
        if projection.status == "available":
            value.update(
                source_projection_version=projection.version,
                source_projection={"protocolSection": json.loads(projection.text)},
                protocol_locator=projection.locator,
            )
    return CollectedDocument(**value)


def project_collected(item, data):
    from biz_rules.history_sources import TRIAL_PATHS, _facts

    document = CollectedDocument.model_validate(data)
    native = "legacy:" + document.source_site + ":" + document.source_doc_uuid
    kind = "trial" if document.source_projection is not None else "document"
    if item.source_record_id != native or item.record_kind != kind:
        raise HistoryError(
            "collected_document_identity", "自动发布文档的来源ID/类型与冻结原件不一致"
        )
    fields = {
        key: "/" + key
        for key in (
            "source_site",
            "original_raw_sha256",
            "publication_state",
            "publication_role",
            "state_observed_at",
            "capture_reason",
            "previous_raw_sha256",
            "source_projection_version",
        )
    }
    fields["origin_doc_uuid"] = "/source_doc_uuid"
    facts, locators = _facts(data, fields)
    published, updated, event = None, None, None
    if kind == "trial":
        trial, bound = _facts(
            data,
            {key: "/source_projection" + value for key, value in TRIAL_PATHS.items()},
        )
        facts.update(trial)
        locators.update(bound)
        protocol = document.source_projection["protocolSection"]
        status = protocol.get("statusModule", {})
        published = source_time(
            (status.get("studyFirstPostDateStruct") or {}).get("date"),
            "registry_first_posted",
        )
        updated = source_time(
            (status.get("lastUpdatePostDateStruct") or {}).get("date"),
            "registry_update_posted",
        )
        event = source_time(
            trial.get("start_date"),
            "registry_start_" + str(trial.get("start_date_type", "unknown")),
        )
    # 只采用原样叙述前缀，不让附录JSON键进入文本挖掘；完整原件仍在raw。
    clean = re.split(
        r"(?m)^##[ \t]+原始数据[ \t]*\r?$", document.original_raw_content, maxsplit=1
    )[0].rstrip()
    if document.publication_state == "withdrawn":
        clean = "源记录已删除或撤回；这不是临床失败结论。"
    return {
        "parser_version": "datafabric.collected.document.projection.v0.1",
        "record_kind": kind,
        "title": document.title,
        "source_url": document.source_url,
        "clean_text": clean,
        "facts": facts,
        "field_locators": locators,
        "source_published_at": published.model_dump(mode="json") if published else None,
        "source_updated_at": updated.model_dump(mode="json") if updated else None,
        "event_time": event.model_dump(mode="json") if event else None,
    }
