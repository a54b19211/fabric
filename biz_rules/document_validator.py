"""
文档级结构化校验（T5）——落库前的整体契约检查。

背景（对应开题报告 §R19「前期探索性原型暴露的不足」）：
  原系统落库前仅调用 validate_extra 校验来源特有 extra 字段，对文档整体
  （uuid/title/类型/枚举）没有任何 schema 契约检查，缺字段、类型错乱、
  doc_type 写错的文档也能照常落库，污染下游。

本模块提供 validate_document()，在 write_to_database 落库前对整篇文档做
  「必填非空 / 类型约束 / 枚举合法值」三类检查，输出可量化的违规计数与明细，
  为 T7「质量门禁有效性评估」提供结构化证据。

设计原则（与 T2/T3 一致）：
  - 非阻断式：仅报告违规，不拦截 upsert（如需阻断可另议）。
  - 绝不抛异常：校验器自身 failure 不应影响主流程落库。
"""

from dataclasses import dataclass, field
from typing import Any


# ── 文档级契约定义 ──────────────────────────────────────────────
REQUIRED_NONEMPTY = ("uuid", "title", "raw_content")  # 必填非空字段

FIELD_TYPES = {
    "uuid": str,
    "title": str,
    "raw_content": str,
    "publish_date": str,
    "category": str,
    "doc_type": str,
    "language": str,
    "status": str,
    "summary": str,
}

ENUM_LEGAL = {
    "doc_type": {
        # --- 原有 8 类（既有 15 站使用）---
        "regulatory_notice", "drug_recall", "drug_label",
        "clinical_trial", "literature", "industry_news",
        "company_announcement", "policy",
        # --- 2026-10-22 补充：任务书待接入的 4 类新来源 ---
        # 原枚举不含这些类型，新来源一落库就产生虚假 enum 违规（缺陷 D6）
        "regulatory_document",         # FDA CRL / 警告信等监管信函
        "regulatory_decision",         # EMA 决定/意见书
        "medical_insurance_catalog",   # 国家医保目录
        "guideline",                   # CDE 指导原则 / NCCN 类指南
    },
    "status": {"AUTO_PROCESSED", "PENDING_REVIEW", "REVIEWED"},
    "language": {"zh", "en"},
}


@dataclass
class ViolationReport:
    """单篇文档的结构化校验结果。"""
    valid: bool = True
    violation_count: int = 0
    violations: list = field(default_factory=list)

    def add(self, field_name: str, rule: str, message: str) -> None:
        self.violations.append(
            {"field": field_name, "rule": rule, "message": message}
        )
        self.violation_count += 1
        self.valid = False


def validate_document(doc: dict) -> ViolationReport:
    """
    对单篇文档做整体契约校验。

    参数:
        doc: 已组装好的文档字典（如 MetadataEnvelope.model_dump() 的结果）。
             只检查其中顶层标量字段，嵌套对象（provenance/confidence 等）不展开。

    返回:
        ViolationReport: 含 valid / violation_count / violations 明细。
    """
    report = ViolationReport()

    # 1) 必填非空 —— rule="required"
    for f in REQUIRED_NONEMPTY:
        val = doc.get(f)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            report.add(f, "required", f"必填字段为空: {f}")

    # 2) 存在但类型不符 —— rule="type"
    for f, expected in FIELD_TYPES.items():
        if f not in doc or doc.get(f) is None:
            continue  # 空值由 required 处理，这里只查"有值但类型错"
        if not isinstance(doc[f], expected):
            report.add(
                f, "type",
                f"字段类型错误: {f} 期望 {expected.__name__}，实际 {type(doc[f]).__name__}",
            )

    # 3) 枚举值非法（非空且不在合法集）—— rule="enum"
    for f, legal in ENUM_LEGAL.items():
        val = doc.get(f)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            continue  # 空值不判枚举
        if val not in legal:
            report.add(
                f, "enum",
                f"枚举值非法: {f}={val!r}，合法值 {sorted(legal)}",
            )

    return report
