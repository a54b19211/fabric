"""外部同口径观测的小契约；source_bound只表明原件字段绑定，不是专业真实性判定。"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, DecimalException
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from biz_rules.history_models import Model, TimeValue

OBSERVATION_PARSER = "datafabric.observation.projection.v0.1"


class ObservationRef(Model):
    source_id: str = Field(min_length=1, max_length=80)
    source_record_id: str = Field(min_length=1, max_length=400)
    version_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Subject(Model):
    namespace: str = Field(min_length=1, max_length=100)
    id: str = Field(min_length=1, max_length=400)


class DecimalInterval(Model):
    lower: str = Field(min_length=1, max_length=100)
    upper: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def ordered(self):
        try:
            low, high = Decimal(self.lower), Decimal(self.upper)
        except DecimalException:
            raise ValueError("区间端点须为十进制字符串") from None
        if not low.is_finite() or not high.is_finite() or low > high:
            raise ValueError("观测区间须有限且下界不超过上界")
        return self


class Basis(Model):
    kind: Literal["declared", "source_bound"]
    ref: ObservationRef | None = None
    field_pointers: dict[str, str] = Field(default_factory=dict, max_length=32)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def bound_ref(self):
        if self.kind == "source_bound" and (
            self.ref is None or not self.field_pointers
        ):
            raise ValueError("字段绑定需要完整原件引用和指针")
        if self.kind == "declared" and (self.ref is not None or self.field_pointers):
            raise ValueError("声明观测不能伪装成已绑定引用")
        if any(
            not value.startswith("/") or len(value) > 1000
            for value in self.field_pointers.values()
        ):
            raise ValueError("原件定位必须为有界JSON pointer")
        return self


class ObservationBase(Model):
    schema_version: Literal["datafabric.observation.v1"] = "datafabric.observation.v1"
    title: str = Field(min_length=1, max_length=500)
    subject: Subject
    event_time: TimeValue | None = None
    public_available_at: TimeValue | None = None
    status: Literal["active", "withdrawn"] = "active"
    supersedes: ObservationRef | None = None
    basis: Basis


class MetricObservation(ObservationBase):
    kind: Literal["metric"]
    metric_id: str = Field(min_length=1, max_length=120)
    metric_version: str = Field(min_length=1, max_length=120)
    value: DecimalInterval | None = None
    unit: str = Field(min_length=1, max_length=120)
    population: str | None = Field(default=None, max_length=500)
    region: str | None = Field(default=None, max_length=300)
    period_start: str
    period_end: str
    denominator: DecimalInterval | None = None
    denominator_unit: str | None = Field(default=None, max_length=120)
    measurement_basis: Literal["reported", "estimated", "target"]

    @field_validator("period_start", "period_end")
    @classmethod
    def iso_day(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("期间须为YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def period_and_denominator(self):
        if self.period_start >= self.period_end:
            raise ValueError("期间采用[start,end)，结束须晚于开始")
        if (self.denominator is None) != (self.denominator_unit is None):
            raise ValueError("分母及其单位须同时存在或同时未知")
        if self.denominator is not None and Decimal(self.denominator.lower) <= 0:
            raise ValueError("已知分母须大于零")
        return self


class EventObservation(ObservationBase):
    kind: Literal["event"]
    event_type: str = Field(min_length=1, max_length=120)


Observation = Annotated[
    MetricObservation | EventObservation, Field(discriminator="kind")
]
_OBSERVATION = TypeAdapter(Observation)


def observation(data):
    return _OBSERVATION.validate_python(data)


def binding_values(item):
    """固定两种观测中实际声称的字段；不把标题/人类说明当指标定义。"""
    data = item.model_dump(mode="json")
    keys = [
        "subject.namespace",
        "subject.id",
        "status",
        "event_time",
        "public_available_at",
    ]
    if item.kind == "metric":
        keys += [
            "metric_id",
            "metric_version",
            "unit",
            "population",
            "region",
            "period_start",
            "period_end",
            "measurement_basis",
            "value.lower",
            "value.upper",
            "denominator.lower",
            "denominator.upper",
            "denominator_unit",
        ]
    else:
        keys += ["event_type"]
    values = {}
    for key in keys:
        value = data
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            values[key] = value
    return values


def native_json(raw):
    # 新数值绑定单独保留JSON十进制精度，旧来源投影/哈希规则不变。
    from biz_rules.history_sources import _unique

    return json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique)


def native_pointer(data, pointer):
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("不支持的原件JSON pointer")
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(data, dict):
            if token not in data:
                raise ValueError("原件字段不存在")
            data = data[token]
        elif isinstance(data, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            index = int(token)
            if index >= len(data):
                raise ValueError("原件数组定位越界")
            data = data[index]
        else:
            raise ValueError("原件定位不属于对象或非负数组下标")
    return data


def validate_bound_values(item, raw):
    if raw.get("schema_version") == "datafabric.observation.v1":
        raise ValueError("观测不得引用另一观测循环自证")
    values = binding_values(item)
    pointers = item.basis.field_pointers
    if item.subject.namespace == item.basis.ref.source_id:
        values.pop("subject.namespace")
    if set(pointers) != set(values):
        raise ValueError("全部非空观测口径和值须绑定原件字段")
    for key, expected in values.items():
        actual = native_pointer(raw, pointers[key])
        if isinstance(actual, bool):
            raise ValueError("布尔值不能充当数值或口径")  # noqa: TRY004 -- 绑定错误统一拒绝
        if key.startswith(("value.", "denominator.")):
            if not isinstance(actual, (str, int, Decimal)):
                raise ValueError("数值指针须指向原生标量")
            try:
                number = Decimal(str(actual))
            except DecimalException:
                raise ValueError("原件数值不是十进制标量") from None
            if not number.is_finite() or number != Decimal(expected):
                raise ValueError("观测值与原件不一致")
        elif actual != expected:
            raise ValueError("观测口径或时间与原件不一致")


def same_series(left, right):
    keys = ["kind", "subject"]
    keys += (
        [
            "metric_id",
            "metric_version",
            "unit",
            "population",
            "region",
            "measurement_basis",
            "denominator_unit",
        ]
        if left.kind == "metric"
        else ["event_type"]
    )
    a, b = left.model_dump(mode="json"), right.model_dump(mode="json")
    return all(a.get(key) == b.get(key) for key in keys)


def project_observation(item, data):
    value = observation(data)
    if item.record_kind != "document":
        raise ValueError("观测只作为document采用，不混入独立outcome标签")
    facts = {key: val for key, val in data.items() if key != "title"}
    return {
        "record_kind": "document",
        "title": value.title,
        "source_url": None,
        "clean_text": value.title,
        "parser_version": OBSERVATION_PARSER,
        "facts": facts,
        "field_locators": {key: {"json_pointer": "/" + key} for key in facts},
        "event_time": value.event_time.model_dump(mode="json")
        if value.event_time
        else None,
        "source_published_at": value.public_available_at.model_dump(mode="json")
        if value.public_available_at
        else None,
        "source_updated_at": None,
    }
