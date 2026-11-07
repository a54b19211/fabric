"""消歧结果数据模型。"""

from dataclasses import dataclass, field

RULE_VERSION = "fabric-disambiguation-20261031-v1"


@dataclass
class CandidateEntity:
    """来自 entity_dict 的候选匹配。"""
    dict_id: int
    name: str
    similarity_score: float = 0.0
    match_type: str = ""          # external_id | alias | normalized | rule | vector
    canonical_data: dict = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    external_ids: dict = field(default_factory=dict)


@dataclass
class DisambiguationResult:
    """单个实体的消歧结果。"""
    status: str                   # auto_matched | candidate_matched | new_entity | ambiguous
    dict_id: int | None = None
    confidence: float = 0.0
    matched_name: str = ""
    reason: str = ""
    candidates: list[CandidateEntity] = field(default_factory=list)
    match_step: str = ""          # 哪个阶段产出的结果
    # Explicit rule evidence, never an identity verification or review approval.
    auto_link: bool = False
    rule_version: str = RULE_VERSION
    model_version: str | None = None
    model_trace: list[dict] = field(default_factory=list)

    def retain_candidates(self, *groups: list[CandidateEntity]) -> None:
        """Keep every recalled candidate, including a selected rule-only match."""
        by_id = {candidate.dict_id: candidate for candidate in self.candidates}
        for group in groups:
            for candidate in group:
                by_id.setdefault(candidate.dict_id, candidate)
        if self.dict_id is not None and self.dict_id not in by_id:
            by_id[self.dict_id] = CandidateEntity(
                dict_id=self.dict_id, name=self.matched_name,
                similarity_score=self.confidence, match_type=self.match_step,
            )
        self.candidates = list(by_id.values())

    def to_metadata(self) -> dict:
        from dataclasses import asdict

        self.retain_candidates()
        return {
            "status": self.status,
            "dict_id": self.dict_id,
            "candidates": [asdict(candidate) for candidate in self.candidates],
            "match_step": self.match_step,
            "rule_version": self.rule_version,
            "model_version": self.model_version,
            "confidence": self.confidence,
            "reason": self.reason,
            "auto_link": self.auto_link,
            "decision_policy": "strong_ids_only",
            "calibration_status": "uncalibrated",
            "model_trace": self.model_trace,
        }


STATUS_AUTO_MATCHED = "auto_matched"
STATUS_CANDIDATE_MATCHED = "candidate_matched"
STATUS_NEW_ENTITY = "new_entity"
STATUS_AMBIGUOUS = "ambiguous"
