"""资料库的数据形状：存进去的原件（ArchiveRecord）、一次采集的请求（FetchRequest）、一次导入（ArchiveImport），
读出来的版本（VersionRecord）、版本列表（RecordCollection）和运行（RunView）。只写形状，不写规则。"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PARSER_VERSION = "datafabric.history.projection.v1"
Mode = Literal["current", "operational_replay", "public_history_reconstruction"]
RecordKind = Literal["trial", "program", "outcome", "document", "graph"]
MAX_RECORD_BYTES = 1024 * 1024
MAX_IMPORT_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class FabricError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class ItemUnreadable(FabricError):
    """一次读取里有一条（例如一篇详情页）读不到。逐条读的抓取函数 yield 它而不是 raise，读取接着下一条；
    fetch/site_crawler.py 把网址和原因记进这次读取的 coverage，不为它存版本。"""

    def __init__(self, url: str, reason: str):
        super().__init__("item_unreadable", f"{reason} | {url}")
        self.url, self.reason = url, reason


class GroupStart(NamedTuple):
    """按组读的抓取函数（港交所一家公司一组，NCCN 一份指南一组）在第 index 组（从 0 数）之前 yield 它；
    fetch/site_crawler.py 把它记进续读位置，下次从这一组接着读。"""

    index: int


def make_uuid(identifier: str) -> str:
    """URL、文件路径或"来源:记录键"的确定性 UUID5：同一个标识永远得到同一个 UUID。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identifier))


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("必须提供含时区时间")
    return value.astimezone(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TimeValue(Model):
    """来源给的时间：值、精度（年/月/日/时刻）和它是什么时间（meaning）。"""

    value: str = Field(min_length=4, max_length=40)
    precision: Literal["year", "month", "day", "instant"]
    meaning: str = Field(default="source_reported", max_length=80)

    @model_validator(mode="after")
    def valid_date(self):
        self.upper_bound()
        return self

    def lower_bound(self) -> datetime:
        if self.precision == "instant":
            return utc(datetime.fromisoformat(self.value.replace("Z", "+00:00")))
        padded = {"year": "-01-01", "month": "-01", "day": ""}[self.precision]
        return datetime.fromisoformat(self.value + padded).replace(tzinfo=timezone.utc)

    def upper_bound(self) -> datetime:
        """这个时间最晚可能是哪一刻（一天的最后一微秒、一个月的最后一天……）；格式不对抛 ValueError。"""
        patterns = {"year": r"\d{4}", "month": r"\d{4}-\d{2}", "day": r"\d{4}-\d{2}-\d{2}"}
        if self.precision == "instant":
            return utc(datetime.fromisoformat(self.value.replace("Z", "+00:00")))
        if not re.fullmatch(patterns[self.precision], self.value):
            raise ValueError("时间格式与精度不符")
        parts = [int(p) for p in self.value.split("-")]
        year, month = parts[0], parts[1] if len(parts) > 1 else 12
        day = parts[2] if len(parts) > 2 else calendar.monthrange(year, month)[1]
        return datetime(year, month, day, 23, 59, 59, 999999, timezone.utc)


def source_time(value: Any, meaning: str = "source_reported") -> TimeValue | None:
    """来源给的时间字符串 → TimeValue（按写法判断精度）；空的、不是字符串的、格式不对的 → None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    precision = next((name for name, pattern in (("year", r"\d{4}"), ("month", r"\d{4}-\d{2}"),
                                                 ("day", r"\d{4}-\d{2}-\d{2}")) if re.fullmatch(pattern, value)),
                     "instant")
    try:
        return TimeValue(value=value, precision=precision, meaning=meaning)
    except ValueError:
        return None


class AvailabilityProof(BaseModel):
    """这个版本在公开历史回放（public_history_reconstruction）里从什么时候起可见：抓到的是 unverified（只按首次看到的
    时间），档案导入时声明了可见时间的是 declared_archive。旧版本可能带着当年的出处、审核字段，原样读出。"""

    model_config = ConfigDict(extra="allow")
    kind: str = "unverified"
    available_at: TimeValue | None = None


class VersionRecord(Model):
    """读出来的一个版本（雷达读的就是它）。"""

    schema_version: Literal["datafabric.history.record.v1"] = "datafabric.history.record.v1"
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
    mapping_review: dict[str, Any] | None = None  # 旧版本才有
    published_seq: int
    migration: dict[str, Any] | None = None  # 按旧编号读 legacy 版本时附上它迁成了什么（不存进版本）
    document_evidence: dict[str, Any] | None = None  # fetch/document_evidence.py：材料性质、全文、文档关联


class ArchiveRecord(Model):
    """一条原件：来源记录号、记录类型、原文（JSON 或 XML 文本）。"""

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


class ArchiveProof(BaseModel):
    """导入时声明的公开可见时间；其余字段（出处、哈希、审核说明）可以带，不再核对。"""

    model_config = ConfigDict(extra="ignore")
    available_at: TimeValue


class ArchiveImport(BaseModel):
    """POST /api/fabric/archives：一包原件（场景回放、图谱发布、本地文件）。"""

    model_config = ConfigDict(extra="ignore")
    source_id: str = Field(min_length=1, max_length=80)
    is_demo: bool
    scope_description: str = Field(min_length=1, max_length=2000)
    declared_count: int = Field(ge=0, le=5000)
    declared_complete: bool = False
    records: list[ArchiveRecord] = Field(max_length=5000)
    proof: ArchiveProof | None = None

    @model_validator(mode="after")
    def consistent(self):
        ids = [r.source_record_id for r in self.records]
        if len(set(ids)) != len(ids):
            raise ValueError("同一包里来源ID重复")
        if len(self.records) != self.declared_count:
            raise ValueError("声明条数须与记录条数一致")
        return self


class FetchContinuation(BaseModel):
    """从哪接着读：来源自己的翻页位置和已读条数（旧任务里存的多余字段忽略）。"""

    model_config = ConfigDict(extra="ignore")
    cursor: str = Field(min_length=1, max_length=4000)
    read_count: int = Field(ge=0, le=100000000)


class FetchRequest(BaseModel):
    """一次采集：读哪个来源、什么检索词、最多几条、多少秒内读完。"""

    model_config = ConfigDict(extra="ignore")  # 旧任务里存的 is_demo、demo_instance_id 等忽略
    source_id: str = Field(min_length=1, max_length=60)
    query: str = Field(min_length=1, max_length=500)
    source_record_ids: list[str] = Field(default_factory=list, max_length=100)
    max_records: int = Field(default=10, ge=1, le=100)
    max_pages: int = Field(default=3, ge=1, le=10)
    deadline_seconds: int = Field(default=60, ge=5, le=120)
    updated_since: datetime | None = None
    updated_until: datetime | None = None
    continuation: FetchContinuation | None = None
    # 增量读取（定时刷新用）：连续读到这么多条"库里已有且没变"的就停（stop_reason=reached_known），0 = 不停
    known_stop: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def fixed_scope(self):
        from fetch.catalog import FETCHABLE

        if self.source_id not in FETCHABLE:
            raise ValueError("来源未在目录中接通")
        for value in (self.updated_since, self.updated_until):
            if value is not None:
                utc(value)
        if self.updated_since and self.updated_until and self.updated_since > self.updated_until:
            raise ValueError("来源更新窗口顺序错误")
        if self.source_id not in {"who_don", "gdelt_doc"} and (self.updated_since or self.updated_until):
            raise ValueError("此来源尚不支持更新窗口参数")
        return self


class RecordCollection(Model):
    """GET /api/fabric/records 的一页。coverage：max_seq（这次读到的发布序号上界）、has_more，GDELT 线索窗口另有
    window_coverage（每个窗口这次读回了什么）和 truncated_intervals（可能没读全的区间）。"""

    schema_version: Literal["datafabric.history.collection.v1"] = "datafabric.history.collection.v1"
    items: list[VersionRecord]
    next_cursor: str | None = None
    coverage: dict[str, Any]


class RunView(BaseModel):
    """一次运行（采集或导入）：状态、存了哪些版本、读取情况（coverage）、出错原因。"""

    id: str
    source_id: str
    is_demo: bool
    kind: Literal["archive", "fetch"]
    state: Literal["queued", "running", "completed", "partial", "failed", "enqueue_failed"]
    attempt: int
    created_at: datetime
    completed_at: datetime | None = None
    scope_description: str
    version_ids: list[str]
    coverage: dict[str, Any]
    error: dict[str, Any] | None = None
