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
    # 只接受来源目录中已接通的来源（collection.catalog.FETCHABLE）
    source_id: str = Field(min_length=1, max_length=60)
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

        if self.source_id not in FETCHABLE:
            raise ValueError("来源未在目录中接通")
        for value in (self.updated_since, self.updated_until):
            if value is not None:
                utc(value)
        if (
            self.updated_since
            and self.updated_until
            and self.updated_since > self.updated_until
        ):
            raise ValueError("来源更新窗口顺序错误")
        if self.source_id != "who_don" and (self.updated_since or self.updated_until):
            raise ValueError("此来源尚不支持更新窗口参数")
        if self.continuation is not None:
            from collection.catalog import SITE_SOURCES

            if self.source_id not in {"europe_pmc", "who_don", "clinicaltrials"} | SITE_SOURCES:
                raise ValueError("此来源尚不支持连续分页")
            if self.continuation.scope_hash != self.scope_hash():
                raise ValueError("续页必须绑定原查询范围及固定时间窗")
        return self


class Coverage(Model):
    mode: Literal["declared_manifest", "bounded_public_query", "adopted_records"]
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


class RecordCollection(Model):
    schema_version: Literal["datafabric.history.collection.v1"] = (
        "datafabric.history.collection.v1"
    )
    items: list[HistoryRecord]
    next_cursor: str | None = None
    excluded_counts: dict[str, int]
    coverage: Coverage


class RunView(Model):
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
