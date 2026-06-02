"""消歧引擎模块。"""

from disambiguation.engine import disambiguate, disambiguate_entities
from disambiguation.models import (
    DisambiguationResult, CandidateEntity,
    STATUS_AUTO_MATCHED, STATUS_CANDIDATE_MATCHED,
    STATUS_NEW_ENTITY, STATUS_AMBIGUOUS,
)

__all__ = [
    "disambiguate",
    "disambiguate_entities",
    "DisambiguationResult",
    "CandidateEntity",
    "STATUS_AUTO_MATCHED",
    "STATUS_CANDIDATE_MATCHED",
    "STATUS_NEW_ENTITY",
    "STATUS_AMBIGUOUS",
]
