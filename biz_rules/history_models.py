"""历史资料的固定输入/输出契约；不把来源声明当成审核或过去可见证明。"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "datafabric.history.v1"
PARSER_VERSION = "datafabric.history.projection.v1"
Mode = Literal["current", "operational_replay", "public_history_reconstruction"]
RecordKind = Literal["trial", "program", "outcome", "document", "graph"]
BUSINESS_TYPES = (
    "mechanisms",
    "literature",
    "trials_pipeline",
    "regulatory",
    "intellectual_property",
    "corporate_finance",
    "market_payment",
    "population_need",
    "manufacturing_supply",
    "research_capacity",
)
MAX_RECORD_BYTES = 1024 * 1024
MAX_IMPORT_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class HistoryError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class ItemUnreadable(HistoryError):
    """One item of a read (an article's detail page, read twice) could not be read. A fetcher that goes through
    items one by one yields this error instead of raising it, so its generator goes on with the next item;
    collection.site_crawler notes the item (url and reason) in the read's coverage and stores no version for it."""

    def __init__(self, url: str, reason: str):
        super().__init__("item_unreadable", f"{reason} | {url}")
        self.url, self.reason = url, reason


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("必须提供含时区时间")
    return value.astimezone(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TimeValue(Model):
    value: str = Field(min_length=4, max_length=40)
    precision: Literal["year", "month", "day", "instant"]
    meaning: str = Field(default="source_reported", max_length=80)

    @model_validator(mode="after")
    def valid_date(self):
        self.upper_bound()
        return self

    def lower_bound(self) -> datetime:
        if self.precision == "year":
            return datetime(int(self.value), 1, 1, tzinfo=timezone.utc)
        if self.precision == "month":
            return datetime.fromisoformat(self.value + "-01").replace(
                tzinfo=timezone.utc
            )
        if self.precision == "day":
            return datetime.fromisoformat(self.value).replace(tzinfo=timezone.utc)
        return utc(datetime.fromisoformat(self.value.replace("Z", "+00:00")))

    def upper_bound(self) -> datetime:
        if self.precision == "year":
            if not re.fullmatch(r"\d{4}", self.value):
                raise ValueError("年精度格式必须为 YYYY")
            return datetime(int(self.value), 12, 31, 23, 59, 59, 999999, timezone.utc)
        if self.precision == "month":
            if not re.fullmatch(r"\d{4}-\d{2}", self.value):
                raise ValueError("月精度格式必须为 YYYY-MM")
            year, month = map(int, self.value.split("-"))
            return datetime(
                year,
                month,
                calendar.monthrange(year, month)[1],
                23,
                59,
                59,
                999999,
                timezone.utc,
            )
        if self.precision == "day":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.value):
                raise ValueError("日精度格式必须为 YYYY-MM-DD")
            return datetime.fromisoformat(self.value).replace(
                hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
            )
        return utc(datetime.fromisoformat(self.value.replace("Z", "+00:00")))


def source_time(value: Any, meaning: str = "source_reported") -> TimeValue | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    precision = (
        "year"
        if re.fullmatch(r"\d{4}", value)
        else "month"
        if re.fullmatch(r"\d{4}-\d{2}", value)
        else "day"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
        else "instant"
    )
    try:
        return TimeValue(value=value, precision=precision, meaning=meaning)
    except ValueError:
        return None


class AvailabilityProof(Model):
    kind: Literal["unverified", "reviewed_archive"]
    available_at: TimeValue | None = None
    uri: str | None = None
    sha256: str | None = None
    verified_by: str | None = None
    verified_at: datetime | None = None
    review_note: str | None = None


class MappingReview(Model):
    reviewed_by: str
    reviewed_at: datetime
    basis: str
    available_at: TimeValue


class SourceRecordReference(Model):
    source_id: str = Field(min_length=1, max_length=80)
    source_record_id: str = Field(min_length=1, max_length=400)
    version_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DocumentBasisLocator(Model):
    """位置只证明原件这样写过，不证明被转述的说法属实。"""
    json_pointer: str | None = Field(default=None, pattern=r"^/")
    xpath: str | None = Field(default=None, min_length=1, max_length=2000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    quote: str | None = Field(default=None, min_length=1, max_length=10000)

    @model_validator(mode="after")
    def complete_locator(self):
        has_text = any(value is not None for value in (self.start, self.end, self.quote))
        if has_text and (self.start is None or self.end is None or self.quote is None
                         or self.end <= self.start or self.end - self.start != len(self.quote)):
            raise ValueError("正文位置必须同时提供精确quote和Unicode起止下标")
        if not self.json_pointer and not self.xpath and not has_text:
            raise ValueError("依据必须提供原件JSON路径或正文位置")
        return self


class DocumentRelation(Model):
    relation_type: Literal["corrects", "retracts", "supplements", "reposts"]
    target_source_id: str = Field(min_length=1, max_length=80)
    target_source_record_id: str = Field(min_length=1, max_length=400)
    target_version_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    basis_locator: DocumentBasisLocator | None = None


class SourceIndependence(Model):
    shared_identifiers: list[str] = Field(default_factory=list, max_length=20)
    # 同名来源或不同网站均不证明研究相互独立。
    status: Literal["unknown", "same_origin"] = "unknown"
    origin_group_id: str | None = Field(default=None, min_length=1, max_length=500)
    original_source: SourceRecordReference | None = None
    basis: str | None = Field(default=None, min_length=1, max_length=2000)


class InformationEnvironmentEvent(Model):
    event_type: Literal["reported_deletion", "official_denial", "moderation", "access_failure"]
    description: str = Field(min_length=1, max_length=2000)
    reported_at: TimeValue | None = None
    detected_at: datetime | None = None
    target: SourceRecordReference | None = None
    basis_locator: DocumentBasisLocator
    # 删除、辟谣和访问失败不是疾病真伪或传播风险的判定。

    @field_validator("detected_at")
    @classmethod
    def detected_time_has_timezone(cls, value):
        return utc(value) if value is not None else None


class DocumentEvidence(Model):
    schema_version: Literal["datafabric.document.evidence.v1"] = "datafabric.document.evidence.v1"
    material_kind: Literal[
        "primary_research", "review", "trial_registry", "press_release",
        "corporate_disclosure", "official_notice", "news_report", "social_media",
        "repost", "conference_abstract", "graph",
    ] | None = None
    text_level: Literal["full_text", "abstract", "title", "excerpt", "metadata"] | None = None
    document_relations: list[DocumentRelation] = Field(default_factory=list, max_length=100)
    source_independence: SourceIndependence = Field(default_factory=SourceIndependence)
    statement_status: Literal["source_reported", "unverified_report"] = "source_reported"
    information_events: list[InformationEnvironmentEvent] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def social_is_unverified(self):
        if self.material_kind in {"social_media", "repost"} and self.statement_status != "unverified_report":
            raise ValueError("社媒及转载说法必须保留未证实标记")
        return self


class HistoryRecord(Model):
    schema_version: Literal["datafabric.history.record.v1"] = (
        "datafabric.history.record.v1"
    )
    source_id: str
    source_record_id: str
    version_id: str
    record_kind: RecordKind
    is_demo: bool
    title: str
    source_url: str | None = None
    raw_content: str
    raw_media_type: str = "application/json"
    raw_sha256: str
    clean_text: str
    parser_version: str = PARSER_VERSION
    facts: dict[str, Any]
    field_locators: dict[str, dict[str, str]]
    event_time: TimeValue | None = None
    source_published_at: TimeValue | None = None
    source_updated_at: TimeValue | None = None
    first_seen_at: datetime
    received_at: datetime
    availability_proof: AvailabilityProof
    mapping_review: MappingReview | None = None
    published_seq: int
    # v3 6.3：按旧编号读 legacy 版本时附上它迁成了什么（db.history._with_legacy_alias），不存进版本
    migration: dict[str, Any] | None = None
    document_evidence: DocumentEvidence | None = None


class ArchiveRecord(Model):
    source_record_id: str = Field(min_length=1, max_length=400)
    record_kind: RecordKind
    raw_content: str = Field(min_length=2, max_length=MAX_RECORD_BYTES)
    raw_media_type: Literal["application/json", "application/xml"] = "application/json"

    @field_validator("source_record_id")
    @classmethod
    def clean_id(cls, value):
        if any(ord(c) < 32 for c in value) or value != value.strip():
            raise ValueError("来源ID不得含控制字符或首尾空白")
        return value

    @field_validator("raw_content")
    @classmethod
    def byte_bound(cls, value):
        if len(value.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("单条原件超过读取预算")
        return value


class ArchiveProofRequest(Model):
    available_at: TimeValue
    uri: str = Field(min_length=1, max_length=2000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed: bool = False
    review_note: str = Field(default="", max_length=2000)


class ArchiveImport(Model):
    source_id: str = Field(min_length=1, max_length=80)
    is_demo: bool
    scope_description: str = Field(min_length=1, max_length=2000)
    declared_count: int = Field(ge=0, le=5000)
    declared_complete: bool = False
    records: list[ArchiveRecord] = Field(max_length=5000)
    scope_record_ids: list[str] = Field(default_factory=list, max_length=20000)
    proof: ArchiveProofRequest | None = None
    verified_mapping_ids: list[str] = Field(default_factory=list, max_length=5000)
    mapping_review_basis: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def consistent(self):
        ids = [r.source_record_id for r in self.records]
        if len(set(ids)) != len(ids):
            raise ValueError("同一归档包来源ID重复，须分别导入明确版本")
        if self.scope_record_ids and (
            len(set(self.scope_record_ids)) != len(self.scope_record_ids)
            or not set(ids) <= set(self.scope_record_ids)
        ):
            raise ValueError("本包原件必须属于无重复的声明清单")
        if len(self.records) != self.declared_count:
            raise ValueError("声明条数须与本归档包记录条数一致")
        if len(canonical(self.model_dump(mode="json")).encode()) > MAX_IMPORT_BYTES:
            raise ValueError("归档包超过16MiB预算")
        if not set(self.verified_mapping_ids) <= set(ids):
            raise ValueError("审核映射必须来自同一包")
        if self.verified_mapping_ids and (
            not self.proof
            or not self.proof.reviewed
            or not self.mapping_review_basis.strip()
        ):
            raise ValueError("映射审核需要已审核归档及明确依据")
        return self


def archive_manifest_hash(records: list[ArchiveRecord]) -> str:
    return sha256(
        canonical(
            [
                {"source_record_id": r.source_record_id, "raw_content": r.raw_content}
                for r in sorted(records, key=lambda r: r.source_record_id)
            ]
        )
    )


class FetchContinuation(Model):
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cursor: str = Field(min_length=1, max_length=4000)
    read_count: int = Field(ge=0, le=100000000)


class FetchRequest(Model):
    # 只接受来源目录中已接通的来源（collection.catalog.FETCHABLE）；external_archive 只用于隔离模拟补采
    source_id: str = Field(min_length=1, max_length=60)
    is_demo: bool = False
    demo_instance_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    query: str = Field(min_length=1, max_length=500)
    source_record_ids: list[str] = Field(default_factory=list, max_length=100)
    max_records: int = Field(default=10, ge=1, le=100)
    max_pages: int = Field(default=3, ge=1, le=10)
    deadline_seconds: int = Field(default=60, ge=5, le=120)
    updated_since: datetime | None = None
    updated_until: datetime | None = None
    continuation: FetchContinuation | None = None

    def scope_hash(self):
        return sha256(
            canonical(
                {
                    "source_id": self.source_id,
                    "query": self.query,
                    "source_record_ids": self.source_record_ids,
                    "updated_since": self.updated_since.isoformat()
                    if self.updated_since
                    else None,
                    "updated_until": self.updated_until.isoformat()
                    if self.updated_until
                    else None,
                }
            )
        )

    @model_validator(mode="after")
    def fixed_scope(self):
        from collection.catalog import FETCHABLE

        if self.is_demo != (self.demo_instance_id is not None):
            raise ValueError("模拟补采须绑定隔离实例；真实补采不得带模拟实例")
        if self.source_id == "external_archive" and not self.is_demo:
            raise ValueError("归档来源仅允许隔离模拟补采")
        if self.source_id not in FETCHABLE and self.source_id != "external_archive":
            raise ValueError("来源未在目录中接通")
        if self.is_demo and not self.query.strip():
            raise ValueError("模拟补采查询不能为空白")
        if self.is_demo and (self.updated_since or self.updated_until or self.continuation):
            raise ValueError("私有模拟包不支持公开源时间窗或续页")
        for value in (self.updated_since, self.updated_until):
            if value is not None:
                utc(value)
        if (
            self.updated_since
            and self.updated_until
            and self.updated_since > self.updated_until
        ):
            raise ValueError("来源更新窗口顺序错误")
        if self.source_id not in {"who_don", "gdelt_doc"} and (self.updated_since or self.updated_until):
            raise ValueError("此来源尚不支持更新窗口参数")
        if self.continuation is not None:
            from collection.catalog import SITE_SOURCES

            if self.source_id not in {"europe_pmc", "who_don", "clinicaltrials", "pubmed"} | SITE_SOURCES:
                raise ValueError("此来源尚不支持连续分页")
            if self.continuation.scope_hash != self.scope_hash():
                raise ValueError("续页必须绑定原查询范围及固定时间窗")
        return self


class WindowReceipt(Model):
    """One window of one windowed read: what came back, or why nothing did. biz_rules.gdelt_news writes them into the
    run's coverage; db.history lists them with the records of a window.

    status: read (a response below the cap, or a full one that cannot be cut), split (a full response, its halves
    follow), failed (the request failed: no count, not zero), not_read (the read stopped before this window).
    full: the response reached max_records. possibly_truncated: a full response that cannot be cut any further."""

    start: str | None
    end: str | None
    status: Literal["read", "split", "failed", "not_read"]
    returned: int | None = None
    distinct_urls: int | None = None
    full: bool = False
    possibly_truncated: bool = False
    requests: int = 0
    reason: str | None = None


class Interval(Model):
    start: str | None
    end: str | None


class Coverage(Model):
    mode: Literal["declared_manifest", "bounded_public_query", "bounded_demo_query", "adopted_records"]
    scope_description: str
    complete: bool
    upstream_pagination_exhausted: bool | None = None
    as_of: datetime
    run_id: str | None = None
    max_seq: int
    declared_count: int | None = None
    declared_scope_count: int | None = None
    time_eligible_count: int = 0
    not_visible_in_scope: int = 0
    adopted_count: int
    stop_reason: str
    limitations: list[str] = Field(default_factory=list)
    # Windowed reads (GDELT): the receipts of the windows the listing speaks about, and those of them that may be cut short
    window_coverage: list[WindowReceipt] = Field(default_factory=list)
    truncated_intervals: list[Interval] = Field(default_factory=list)


class RecordCollection(Model):
    schema_version: Literal["datafabric.history.collection.v1"] = (
        "datafabric.history.collection.v1"
    )
    items: list[HistoryRecord]
    next_cursor: str | None = None
    excluded_counts: dict[str, int]
    coverage: Coverage


class RunView(Model):
    fetch_mode: Literal["public", "simulated"] | None = None
    demo_instance_id: str | None = None
    input_sha256: str | None = None
    id: str
    source_id: str
    is_demo: bool
    kind: Literal["archive", "fetch"]
    state: Literal[
        "queued", "running", "completed", "partial", "failed", "enqueue_failed"
    ]
    attempt: int
    created_at: datetime
    completed_at: datetime | None = None
    scope_description: str
    version_ids: list[str]
    manifest_sha256: str | None = None
    coverage: dict[str, Any]
    error: dict[str, str] | None = None
    attempts: list[dict[str, Any]]
