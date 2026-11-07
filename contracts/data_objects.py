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

现有两种派生结果（登记差分、清洗正文——含附件解析正文）用 from_registry_change / from_clean_text 映射进来，
说明它们满足同一契约；以后的清洗、抽取、测量都按这里的模型落表。

测量有两版契约：v1 的 Measurement 不改；v2（MeasurementV2，datafabric.data_objects.v2）是加工层测量对象
（db.measurements）落表和读取接口给出的形状：规范十进制数值与原文字面量分开，时期结构化，范围与口径模式、
分母状态显式，带业务维度及其核验结果、核验身份、清洗产物和对象身份。对象身份只来自已确认的图谱身份
（confirmed / rule_confirmed）；同名只是记录内隔离的临时候选（provisional），不能据此跨记录合并。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION_ID = r"^[0-9a-f]{64}$"
CONTRACT_VERSION = "datafabric.data_objects.v1"
CONTRACT_VERSION_V2 = "datafabric.data_objects.v2"
# a canonical decimal string, the same form BioCheck's contract v2+ checks (no float on the way)
DECIMAL = r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$"
PERIOD_FORMS = {"day": r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])$",
                "month": r"^[0-9]{4}-(?:0[1-9]|1[0-2])$", "quarter": r"^[0-9]{4}Q[1-4]$",
                "half": r"^[0-9]{4}H[12]$", "year": r"^[0-9]{4}$"}


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
    # json_pointer 指向哪里：raw = 版本原件（JSON 解析后）；record = 版本记录本身（如投影字段 /clean_text）。
    # json_pointer 带 start/end 时，偏移是该字段解码后字符串里的位置
    within: Literal["raw", "record"] = "raw"

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
    """关联而不是输入：派生结果没有读它的内容，所以不参与可见时间计算。
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
        （否则一个不可见或已撤权的版本也能给事件“作证”）。同一版本在支持里只能出现一次。"""
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


class PeriodV2(Strict):
    """结构化时期（与 BioCheck 契约 v2+ 的 PeriodValue 同形）：某精度的一个值，或日/月精度的起止区间。"""

    type: Literal["point", "interval"]
    precision: Literal["day", "month", "quarter", "half", "year"]
    value: str | None = None
    start: str | None = None
    end: str | None = None

    @model_validator(mode="after")
    def one_form(self):
        form = PERIOD_FORMS[self.precision]
        if self.value is not None:
            if self.start is not None or self.end is not None or not re.match(form, self.value):
                raise ValueError("时期要么是该精度的一个值，要么是起止，不能两者都有")
        elif (self.start is None or self.end is None or self.type != "interval" or self.precision not in ("day", "month")
              or not re.match(form, self.start) or not re.match(form, self.end)):
            raise ValueError("时期区间只能是日或月精度的 interval，起止都要有")
        return self


class MeasureDim(Strict):
    """测量所属的一个业务维度：原文写法、在清洗正文里的位置、核验器对它的结论（unchecked = 没有核验结论）。"""

    kind: Literal["disease", "population", "date_basis", "indication", "product_or_class", "milestone_definition",
                  "entity", "stage", "denominator_definition"]
    value: str = Field(min_length=1, max_length=300)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    check: Literal["match", "mismatch", "not_found", "unsupported", "unchecked"]


class UnlocatedDim(Strict):
    """抽取写明了、但不能定位而没有送核的维度：保留并写明原因，不当作没写过。"""

    kind: str | None = Field(default=None, max_length=60)
    text: str = Field(max_length=300)
    reason: str = Field(min_length=1, max_length=80)


class CheckRef(Strict):
    checker_version: str = Field(min_length=1, max_length=80)
    contract_version: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    status: Literal["consistent", "inconsistent", "unchecked"]


class ArtifactRef(Strict):
    """测量读自的那份清洗产物：固定版本 + 清洗器版本 + 正文哈希。"""

    text_processor_version: str = Field(min_length=3, max_length=120)
    text_sha256: str = Field(pattern=VERSION_ID)


class GraphRef(Strict):
    source_id: str = Field(min_length=1, max_length=80)
    record_id: str = Field(min_length=1, max_length=400)
    version_id: str = Field(pattern=VERSION_ID)


class MeasureIdentity(Strict):
    """测量对象（object）是谁。confirmed / rule_confirmed：唐图里已确认的身份（namespace|id），绑定到这条测量所在
    固定版本里的那处提及；provisional：没有已确认身份，key 是 literal:<来源>|<记录>|<规范化字面>——只在同一来源记录
    内成组，同名不等于同一对象。"""

    state: Literal["confirmed", "rule_confirmed", "provisional"]
    key: str = Field(min_length=3, max_length=900)
    graph: GraphRef | None = None
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def confirmed_comes_from_the_graph(self):
        literal = self.key.startswith("literal:")
        if self.state == "provisional":
            if not literal or self.graph is not None:
                raise ValueError("临时身份只能是记录内的字面键，不能挂图谱身份")
        elif literal or self.graph is None or self.key.count("|") != 1:
            raise ValueError("已确认身份必须是 namespace|id 并写明来自哪个固定图包")
        return self


class MeasurementV2(Derived):
    """加工层的一个已核验测量对象（datafabric.data_objects.v2）。值是规范十进制字符串，原文写法另存；单位不知道写
    unknown；时期必须结构化写出；分母状态显式（比例不能是 not_applicable）；至少一个 char_span 定位落在 artifact
    指认的清洗正文上。"""

    contract_version: Literal["datafabric.data_objects.v2"] = CONTRACT_VERSION_V2
    kind: Literal["measurement"] = "measurement"
    artifact: ArtifactRef
    object: str = Field(min_length=1, max_length=300)
    metric: str = Field(min_length=1, max_length=200)
    value_text: str = Field(pattern=DECIMAL, max_length=40)
    value_literal: str = Field(min_length=1, max_length=60)
    unit: str = Field(min_length=1, max_length=40)
    period: PeriodV2
    period_literal: str | None = Field(default=None, max_length=200)
    scope: str | None = Field(default=None, max_length=200)
    scope_mode: Literal["stated", "object_metric_total", "unknown"]
    numerator_text: str | None = Field(default=None, pattern=DECIMAL, max_length=40)
    denominator_text: str | None = Field(default=None, pattern=DECIMAL, max_length=40)
    denominator_state: Literal["stated", "unknown", "not_applicable"]
    measure_kind: Literal["stock", "flow", "cumulative", "rate"]
    qualifier: Literal["exact", "approximate", "at_least", "at_most", "more_than", "less_than"]
    modality: Literal["actual", "planned", "projected"]
    family: Literal["case_count", "milestone_count", "capacity", "efficacy", ""]
    dims: list[MeasureDim] = Field(default_factory=list, max_length=9)
    dims_unlocated: list[UnlocatedDim] = Field(default_factory=list, max_length=9)
    check: CheckRef
    quote: str = Field(min_length=1, max_length=1000)
    identity: MeasureIdentity

    @model_validator(mode="after")
    def states_are_explicit(self):
        if (self.scope_mode == "stated") != bool(self.scope):
            raise ValueError("scope_mode=stated 当且仅当写出了范围")
        if (self.denominator_state == "stated") != (self.denominator_text is not None):
            raise ValueError("denominator_state=stated 当且仅当写出了分母")
        if self.measure_kind == "rate" and self.denominator_state == "not_applicable":
            raise ValueError("比例必须有分母或明确写 unknown")
        if len({d.kind for d in self.dims}) != len(self.dims):
            raise ValueError("每种维度只能一项")
        return self

    @model_validator(mode="after")
    def located_on_its_artifact(self):
        if not any(loc.kind == "char_span" and loc.derived_text_sha256 == self.artifact.text_sha256
                   for loc in self.locators):
            raise ValueError("测量必须有一个落在其清洗正文上的 char_span 定位")
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
    必须和输入一一对应，缺一个或多一个都报错，不能拿别的版本的时间顶替。
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


def _first_locator(version_id: str, row: dict) -> Locator:
    """清洗正文第一段的定位。段落自带原件位置（JSON 路径 / 原件字符偏移）时用它；否则用解析服务给的
    页、块、表；都没有就是 unavailable，不编。页/块/表落在派生正文上，所以写明是哪一份派生正文。"""
    segments = row.get("segments") or []
    first = segments[0] if segments else {}
    src = first.get("source") or {}
    kind = row.get("locator_kind")
    on_derived = {"version_id": version_id, "derived_text_sha256": row.get("text_sha256")}
    if src.get("json_pointer"):
        return Locator(kind="json_pointer", version_id=version_id, json_pointer=src["json_pointer"],
                       start=src.get("start"), end=src.get("end"), within=src.get("within", "raw"))
    if "start" in src and "end" in src:
        return Locator(kind="char_span", version_id=version_id, start=src["start"], end=src["end"])
    if kind in ("pdf_page", "converted_page") and first.get("page"):
        return Locator(kind=kind, page=first["page"], **on_derived)
    if kind == "block" or (kind is None and "block" in first):
        return Locator(kind="block", block=first.get("block", 0), **on_derived)
    if "sheet" in first:
        return Locator(kind="sheet", sheet=first["sheet"], **on_derived)
    return Locator(kind="unavailable", version_id=version_id)


def from_clean_text(row: dict, *, source_id: str, record_id: str, seen, proof=None,
                    links: list[dict] = ()) -> CleanText:
    """derived_clean_text 的一行（清洗正文或附件解析正文）→ 清洗正文契约对象。
    输入只有这个版本自己；附件所在公告页是关联（Relation），固定版本未知时如实写空和依据，不参与可见时间。"""
    refs = [VersionRef(source_id=source_id, record_id=record_id, version_id=row["version_id"])]
    relations = [
        Relation(kind="attachment_of", source_id=source_id, record_id=link["parent_record_id"],
                 version_id=link.get("parent_version_id"), basis=link.get("link_basis") or "unknown")
        for link in links
    ]
    state = {"parsed": "ok", "failed": "failed", "unsupported": "unknown"}.get(row.get("status"), "failed")
    language = row.get("language") if row.get("language") in ("zh", "en", "mixed") else "unknown"
    return CleanText(
        inputs=refs,
        relations=relations,
        processing=Processing(processor_version=row["processor_version"]),
        locators=[_first_locator(row["version_id"], row)],
        permission_sources=[source_id],
        visibility=merge_visibility(refs, {row["version_id"]: (seen, proof)}),
        status=Status(state="ok") if state == "ok" else Status(state=state, reason=row.get("error") or "没有可用正文"),
        text_sha256=row["text_sha256"],
        chars=row.get("chars") if row.get("chars") is not None else len(row.get("text") or ""),
        segments=len(row.get("segments") or []),
        language=language,
    )


def resolve(locator: Locator, *, raw_content: str, record: dict | None = None) -> str:
    """按定位从原件（或版本记录）取回片段，验证回指用。只支持文本定位（json_pointer / char_span）。"""
    import json

    if locator.kind == "char_span":
        return raw_content[locator.start:locator.end]
    if locator.kind != "json_pointer":
        raise ValueError(f"{locator.kind} 定位要按派生正文或页面解析")
    node = json.loads(raw_content) if locator.within == "raw" else (record or {})
    for token in locator.json_pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        node = node[int(token)] if isinstance(node, list) else node[token]
    if locator.start is None:
        return node if isinstance(node, str) else json.dumps(node, ensure_ascii=False)
    return node[locator.start:locator.end]
