"""v3 第二部分“逻辑数据对象”的程序化契约：每个派生对象都必须说清楚下面六件事，缺了就不收。

1. 来自哪些固定版本（inputs，至少一个；每个是 来源 + 记录 + 版本号）；
2. 用哪个处理器、什么配置得到（processing.processor_version / config_sha256）；
3. 在原件里的实际位置（locators；按格式给能给的定位，给不了就写 unavailable，不编页码）；
4. 受哪些来源的权限约束（permission_sources = 所有输入的来源，撤权任何一个都要遮蔽它）；
5. 什么时候可见、有没有历史证明（visibility：取所有输入里最晚的 first_seen_at；历史证明只在所有输入
   都有证明时才有，不能从有证明的输入借给没证明的输入）；
6. 未知、部分、失败、更正、撤回、被替代、未映射怎么表示（status，不靠“字段为空”暗示）。

另外各对象的业务约束（v3 第二部分、3.4、6.1）：
- 主张/观测必须有原句或字段定位，带陈述极性（肯定/否定/不确定）和限定条件；
- 测量必须有单位、时期、范围；分母不知道就写 unknown，不补；
- 领域事件的支持材料要分同源与独立，广泛转发不能算多份独立证实；未映射到 Schema 的类型保留 unmapped；
- 实体只到“候选”，消歧是唐的职责；
- 专题假设与判断版本是雷达的应用状态，这里只定义它引用证据的方式。

现有两种派生结果（登记差分、附件正文）用 from_registry_change / from_attachment_text 映射进来，
说明它们满足同一契约；以后的清洗、抽取、测量都按这里的模型落表。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION_ID = r"^[0-9a-f]{64}$"
CONTRACT_VERSION = "datafabric.data_objects.v1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionRef(Strict):
    """一个固定版本：来源 + 记录 + 版本号。派生对象的输入只能是固定版本，不能是“某记录的当前内容”。"""

    source_id: str = Field(min_length=1, max_length=80)
    record_id: str = Field(min_length=1, max_length=400)
    version_id: str = Field(pattern=VERSION_ID)
    role: Literal["primary", "previous", "parent", "context"] = "primary"


class Locator(Strict):
    """在原件（或某份派生正文）里的位置。kind 决定哪些字段必须有；unavailable 表示解析服务没给，不编。"""

    kind: Literal["json_pointer", "char_span", "pdf_page", "converted_page", "block", "sheet", "unavailable"]
    version_id: str = Field(pattern=VERSION_ID)
    json_pointer: str | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=1)
    block: int | None = Field(default=None, ge=0)
    sheet: str | None = None
    # 定位落在派生正文上时（例如附件解析正文里的偏移），写明是哪一份派生正文
    derived_text_sha256: str | None = Field(default=None, pattern=VERSION_ID)

    @model_validator(mode="after")
    def required_by_kind(self):
        need = {
            "json_pointer": ("json_pointer",),
            "char_span": ("start", "end"),
            "pdf_page": ("page",),
            "converted_page": ("page",),
            "block": ("block",),
            "sheet": ("sheet",),
            "unavailable": (),
        }[self.kind]
        missing = [k for k in need if getattr(self, k) is None]
        if missing:
            raise ValueError(f"{self.kind} 定位缺少 {missing}")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("定位的 end 不能小于 start")
        return self


class Processing(Strict):
    processor_version: str = Field(min_length=3, max_length=120)
    config_sha256: str | None = Field(default=None, pattern=VERSION_ID)
    produced_at: datetime | None = None


class Visibility(Strict):
    """可见时间：所有输入里最晚的首次所见；历史证明只有所有输入都有证明时才成立（取最晚的证明时间）。"""

    first_seen_at: datetime
    proof_available_at: datetime | None = None
    proof_refs: list[str] = Field(default_factory=list)


class Status(Strict):
    state: Literal["ok", "unknown", "partial", "failed", "corrected", "retracted", "superseded", "unmapped"]
    reason: str | None = Field(default=None, max_length=500)
    superseded_by: str | None = None

    @model_validator(mode="after")
    def reason_when_not_ok(self):
        if self.state != "ok" and not self.reason:
            raise ValueError("非 ok 状态必须写原因")
        return self


class Derived(Strict):
    """所有派生对象的公共部分。"""

    contract_version: Literal["datafabric.data_objects.v1"] = CONTRACT_VERSION
    inputs: list[VersionRef] = Field(min_length=1)
    processing: Processing
    locators: list[Locator] = Field(min_length=1)
    permission_sources: list[str] = Field(min_length=1)
    visibility: Visibility
    status: Status

    @model_validator(mode="after")
    def provenance_is_closed(self):
        sources = {ref.source_id for ref in self.inputs}
        if set(self.permission_sources) != sources:
            raise ValueError("permission_sources 必须恰好是所有输入的来源（撤权任何一个都要遮蔽）")
        versions = {ref.version_id for ref in self.inputs}
        stray = [loc.version_id for loc in self.locators if loc.version_id not in versions]
        if stray:
            raise ValueError("定位指向了不在输入里的版本")
        return self


class Relation(Strict):
    """关联而不是输入：派生结果没有读它的内容，所以不参与可见时间计算（Codex r63）。
    例如附件解析正文只读附件本身，所在公告页是关联；固定版本未知时 version_id 为空并写明依据。"""

    kind: Literal["attachment_of"]
    source_id: str = Field(min_length=1, max_length=80)
    record_id: str = Field(min_length=1, max_length=400)
    version_id: str | None = Field(default=None, pattern=VERSION_ID)
    basis: str = Field(min_length=1, max_length=80)


class CleanText(Derived):
    kind: Literal["clean_text"] = "clean_text"
    relations: list[Relation] = Field(default_factory=list)
    text_sha256: str = Field(pattern=VERSION_ID)
    chars: int = Field(ge=0)
    segments: int = Field(ge=0)
    language: Literal["zh", "en", "mixed", "unknown"] = "unknown"


class Claim(Derived):
    """原句或字段级的一条陈述或观察。"""

    kind: Literal["claim", "observation"] = "claim"
    statement: str = Field(min_length=1, max_length=4000)
    speaker: str | None = None
    polarity: Literal["affirmed", "negated", "uncertain"]
    qualifiers: list[str] = Field(default_factory=list)
    asserted_time: str | None = None

    @model_validator(mode="after")
    def has_a_real_locator(self):
        if all(loc.kind == "unavailable" for loc in self.locators):
            raise ValueError("主张/观测必须能回指原句或字段，定位不能全是 unavailable")
        return self


class EntityCandidate(Derived):
    kind: Literal["entity_candidate"] = "entity_candidate"
    name: str = Field(min_length=1, max_length=500)
    entity_type: str = Field(min_length=1, max_length=60)
    identifiers: dict[str, str] = Field(default_factory=dict)
    resolution: Literal["candidate", "resolved", "rejected"] = "candidate"
    resolved_by: str | None = None

    @model_validator(mode="after")
    def resolution_by_owner(self):
        if self.resolution != "candidate" and not self.resolved_by:
            raise ValueError("消歧结论必须写由谁（唐的消歧）给出")
        return self


class Support(Strict):
    ref: VersionRef
    independence: Literal["independent", "same_origin", "unknown"]
    origin_group: str | None = None

    @model_validator(mode="after")
    def same_origin_has_group(self):
        if self.independence == "same_origin" and not self.origin_group:
            raise ValueError("同源支持必须写同源组")
        return self


class DomainEvent(Derived):
    kind: Literal["domain_event"] = "domain_event"
    event_type: str = Field(min_length=1, max_length=80)
    schema_mapping: Literal["mapped", "unmapped"]
    subjects: list[str] = Field(min_length=1)
    event_date: str | None = None
    date_precision: Literal["day", "month", "year", "instant", "unknown"] = "unknown"
    support: list[Support] = Field(min_length=1)
    corrections: list[VersionRef] = Field(default_factory=list)

    def independent_support_count(self) -> int:
        """独立支持数：同源组只算一份，unknown 不算独立（广泛转发不能算多份独立证实）；
        同一记录的多个版本也只算一份（修订不是另一次证实）。"""
        records = {(s.ref.source_id, s.ref.record_id) for s in self.support if s.independence == "independent"}
        groups = {s.origin_group for s in self.support if s.independence == "same_origin"}
        return len(records) + len(groups)

    @model_validator(mode="after")
    def support_is_provenance(self):
        """支持和更正引用的版本必须也在输入里：这样它们才进入权限来源、撤权遮蔽和可见时间的计算
        （Codex r63：否则一个不可见或已撤权的版本也能给事件“作证”）。同一版本在支持里只能出现一次。"""
        inputs = {(r.source_id, r.record_id, r.version_id) for r in self.inputs}
        outside = [r.version_id for r in [x.ref for x in self.support] + list(self.corrections)
                   if (r.source_id, r.record_id, r.version_id) not in inputs]
        if outside:
            raise ValueError("支持或更正引用了不在输入里的版本")
        versions = [x.ref.version_id for x in self.support]
        if len(versions) != len(set(versions)):
            raise ValueError("同一版本在支持里只能出现一次")
        return self

    @model_validator(mode="after")
    def unmapped_is_flagged(self):
        if self.schema_mapping == "unmapped" and self.status.state not in ("unmapped", "partial"):
            raise ValueError("未映射到 Schema 的事件状态必须是 unmapped/partial，不能冒充已映射")
        return self


class Measurement(Derived):
    kind: Literal["measurement"] = "measurement"
    object: str = Field(min_length=1, max_length=300)
    metric: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=60)  # 原样保留数字写法，不转浮点
    unit: str = Field(min_length=1, max_length=40)  # 不知道就写 "unknown"，不能省略
    period_start: str | None = None
    period_end: str | None = None
    period: str | None = None
    scope: str = Field(min_length=1, max_length=200)
    denominator: str = Field(min_length=1, max_length=200)  # 没有就写 "unknown" / "not_applicable"
    measure_kind: Literal["stock", "flow", "cumulative", "rate"]
    quote: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def period_is_explicit(self):
        if not (self.period or (self.period_start and self.period_end)):
            raise ValueError("测量必须写时期（period 或起止）")
        if self.measure_kind == "rate" and self.denominator == "not_applicable":
            raise ValueError("比例必须有分母或明确写 unknown")
        return self


class JudgmentVersion(Strict):
    """雷达的应用状态（v3 6.1）：判断版本。这里只约束它怎样引用证据，数据本身由雷达库维护。"""

    kind: Literal["judgment_version"] = "judgment_version"
    topic_id: str
    version: int = Field(ge=1)
    previous_version: int | None = None
    proposition: str = Field(min_length=1)
    basis: list[VersionRef] = Field(min_length=1)
    derived_basis: list[str] = Field(default_factory=list)  # 引用的派生对象（如登记差分）的 id
    invalidation_conditions: list[str] = Field(min_length=1)
    change_reason: str | None = None

    @model_validator(mode="after")
    def revision_says_why(self):
        if self.version > 1 and not (self.previous_version and self.change_reason):
            raise ValueError("修订版必须写上一版和为什么变")
        return self


def merge_visibility(inputs: list[VersionRef], times: dict[str, tuple[datetime, datetime | None]]) -> Visibility:
    """多个输入的可见时间。times 按版本号给出每个输入自己的（首次所见, 历史证明时间）：
    必须和输入一一对应，缺一个或多一个都报错，不能拿别的版本的时间顶替（Codex r63）。
    首次所见取最晚；证明只有全部输入都有时才取最晚，否则没有证明。"""
    wanted = [ref.version_id for ref in inputs]
    if not wanted or set(times) != set(wanted):
        raise ValueError("可见时间必须逐个对应输入版本")
    seen = [times[v][0] for v in wanted]
    proofs = [times[v][1] for v in wanted]
    proof = max(proofs) if all(p is not None for p in proofs) else None
    return Visibility(first_seen_at=max(seen), proof_available_at=proof)


# ——现有派生结果映射进契约——


def from_registry_change(change: dict, *, source_id: str, record_id: str, from_seen, to_seen,
                         processor_version: str, description: str, from_proof=None, to_proof=None) -> Claim:
    """登记差分的一行（derived_registry_changes）→ 观测。两版都是输入，定位是两版各自的 JSON 路径。"""
    # 已投影字段用它的 JSON 路径；投影外的变化（unmapped_change）用差分记下的变化路径定位
    pointers = [change["json_pointer"]] if change.get("json_pointer") else list(
        (change.get("detail") or {}).get("changed_paths") or [])
    refs = [
        VersionRef(source_id=source_id, record_id=record_id, version_id=change["from_version_id"], role="previous"),
        VersionRef(source_id=source_id, record_id=record_id, version_id=change["to_version_id"], role="primary"),
    ]
    locators = [
        Locator(kind="json_pointer", version_id=ref.version_id, json_pointer=pointer)
        for ref in refs
        for pointer in pointers
    ] or [Locator(kind="unavailable", version_id=ref.version_id) for ref in refs]
    unmapped = change.get("change_kind") == "unmapped_change"
    return Claim(
        kind="observation",
        inputs=refs,
        processing=Processing(processor_version=processor_version),
        locators=locators,
        permission_sources=[source_id],
        visibility=merge_visibility(refs, {change["from_version_id"]: (from_seen, from_proof),
                                           change["to_version_id"]: (to_seen, to_proof)}),
        status=Status(state="unmapped", reason="原件在已投影字段之外有变化，需读两版原件")
        if unmapped
        else Status(state="ok"),
        statement=description,
        polarity="affirmed",
        qualifiers=["登记字段比较，不是成败或疗效结论"],
    )


def from_attachment_text(row: dict, *, source_id: str, record_id: str, seen, links: list[dict],
                         proof=None) -> CleanText:
    """附件派生正文（derived_clean_text 最新一次）→ 清洗正文。解析只读附件本身，输入只有附件版本；
    所在公告页是关联（Relation），固定版本未知时如实写空和依据，不参与可见时间。"""
    refs = [VersionRef(source_id=source_id, record_id=record_id, version_id=row["version_id"])]
    relations = [
        Relation(kind="attachment_of", source_id=source_id, record_id=link["parent_record_id"],
                 version_id=link.get("parent_version_id"), basis=link.get("link_basis") or "unknown")
        for link in links
    ]
    kind = row.get("locator_kind") or "unavailable"
    segments = row.get("segments") or []
    if kind in ("pdf_page", "converted_page") and segments and segments[0].get("page"):
        locators = [Locator(kind=kind, version_id=row["version_id"], page=segments[0]["page"],
                            derived_text_sha256=row["text_sha256"])]
    elif kind == "block" and segments:
        locators = [Locator(kind="block", version_id=row["version_id"], block=segments[0].get("block", 0),
                            derived_text_sha256=row["text_sha256"])]
    elif kind == "sheet" and segments:
        locators = [Locator(kind="sheet", version_id=row["version_id"], sheet=segments[0]["sheet"],
                            derived_text_sha256=row["text_sha256"])]
    else:
        locators = [Locator(kind="unavailable", version_id=row["version_id"])]
    state = {"parsed": "ok", "failed": "failed", "unsupported": "unknown"}[row["status"]]
    return CleanText(
        inputs=refs,
        relations=relations,
        processing=Processing(processor_version=row["processor_version"]),
        locators=locators,
        permission_sources=[source_id],
        visibility=merge_visibility(refs, {row["version_id"]: (seen, proof)}),
        status=Status(state=state, reason=row.get("error") or "解析未成功") if state != "ok" else Status(state="ok"),
        text_sha256=row["text_sha256"],
        chars=row.get("chars") or 0,
        segments=len(segments),
    )
