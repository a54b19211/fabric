"""Sources that are retired: never offered for a new selection, query, index target or default; their old identifiers
still resolve (read-only, through the alias table, with the current permission re-checked on every read).

A retirement has two dates that are kept apart. The decision is a fact about a decision (recorded as the day the
host decided, from the design record); the effective time is when the retirement took effect in this deployment
(recorded by the deployment, never back-dated, and never taken to be the decision date). Both live in the append-only
table history_source_lifecycle (migration V036), not here; this module only says what is retired and why.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetiredSource:
    source_id: str
    decision_date: str  # the day of the decision, a date (the time of day of a decision is not known)
    decision_ref: str
    resolves_via: str
    description: dict  # what the source was, kept as the explanation of old identifiers

    def catalog_entry(self) -> dict:
        return dict(self.description)


_LEGACY_DOCUMENTS = {
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
}


RETIRED = {
    "legacy_documents": RetiredSource(
        source_id="legacy_documents",
        decision_date="2026-11-06",
        decision_ref="雷达全链路设计-v3.1.md#6.3",
        resolves_via="legacy_aliases",
        description=_LEGACY_DOCUMENTS,
    ),
}
