"""Existing six-step identity pipeline; only scoped unique rules authorize links."""

from loguru import logger

from graph.db.repository import lookup_alias_candidates, search_similar_entities
from graph.disambiguation.llm_ranker import SCORE_MID, rank_with_llm, rank_with_reranker
from graph.disambiguation.models import (
    STATUS_AMBIGUOUS,
    STATUS_AUTO_MATCHED,
    STATUS_CANDIDATE_MATCHED,
    STATUS_NEW_ENTITY,
    CandidateEntity,
    DisambiguationResult,
)
from graph.disambiguation.normalizer import generate_candidate_names, normalize_for_alignment
from graph.disambiguation.rules import match_by_entity_rules, match_strong_id
from graph.extraction.llm_client import generate_embedding


def _exact_alias_result(candidate_names, entity_type, session):
    """Step 2: exact names still need identity review. Keep matches from every generated name rather than
    accepting the first weak alias."""
    alias_candidates = {}
    for idx, name in enumerate(candidate_names):
        for match in lookup_alias_candidates(name, entity_type, session=session):
            dict_id = match["dict_id"]
            confidence, match_type = (0.95, "exact_original") if idx == 0 else (
                (0.90, "exact_alias") if idx < 5 else (0.85, "exact_normalized"))
            alias_candidates.setdefault(dict_id, CandidateEntity(
                dict_id=dict_id, name=match.get("canonical_name") or name,
                similarity_score=confidence, match_type=match_type,
                canonical_data=match.get("canonical_data") or {}, aliases=[name],
                external_ids=match.get("external_ids") or {},
            ))
    if not alias_candidates:
        return None
    candidates = list(alias_candidates.values())
    top = candidates[0]
    return DisambiguationResult(
        status=STATUS_CANDIDATE_MATCHED if len(candidates) == 1 else STATUS_AMBIGUOUS,
        dict_id=top.dict_id if len(candidates) == 1 else None,
        confidence=top.similarity_score, matched_name=top.name,
        reason=f"alias exact match requires review: '{top.name}' (type={top.match_type})",
        candidates=candidates, match_step="step2_exact_alias",
    )


class _VectorRecall:
    """Step 4: vectors recall candidates; similarity is not a link decision."""

    def __init__(self, entity_type, session):
        self.entity_type, self.session = entity_type, session
        self.candidates = []
        self.seen_ids = set()
        self.failed = False

    def add_name(self, name: str, match_type: str = "vector") -> None:
        if not name or not name.strip():
            return
        try:
            embedding = generate_embedding(name)
            if not embedding:
                self.failed = True
                return
            similar = search_similar_entities(embedding, self.entity_type, top_k=10,
                                              session=self.session, strict_errors=True)
            for candidate in similar or []:
                if candidate["dict_id"] not in self.seen_ids:
                    self.seen_ids.add(candidate["dict_id"])
                    self.candidates.append(CandidateEntity(
                        dict_id=candidate["dict_id"], name=candidate["name"],
                        similarity_score=candidate["similarity_score"], match_type=match_type,
                        canonical_data=candidate.get("canonical_data") or {},
                        aliases=candidate.get("aliases") or [],
                        external_ids=candidate.get("external_ids") or {},
                    ))
        except Exception as error:
            self.failed = True
            logger.warning(f"[Disambiguation] Candidate recall failed for '{name}': {error}")


def _recall_candidates(entity_name, entity_type, entity_data, normalized_name, session) -> _VectorRecall:
    recall = _VectorRecall(entity_type, session)
    recall.add_name(entity_name)
    name_en = str(entity_data.get("name_en") or "").strip()
    if name_en and name_en.lower() != entity_name.lower():
        recall.add_name(name_en, "vector_en")
    if normalized_name and normalized_name.lower() not in (entity_name.lower(), name_en.lower()):
        recall.add_name(normalized_name, "vector_normalized")
    return recall


def _rank_proposals(entity_name, entity_type, entity_data, source_text, all_candidates) -> DisambiguationResult:
    """Step 5: existing score bands triage proposals, never automatic links."""
    try:
        result = rank_with_reranker(entity_name, entity_type, entity_data, source_text, all_candidates)
    except Exception as error:
        result = DisambiguationResult(
            status=STATUS_AMBIGUOUS, reason=f"Reranker failed: {error}",
            match_step="step5_reranker_error",
        )
    result.auto_link = False
    result.retain_candidates(all_candidates)
    if result.status in (STATUS_AUTO_MATCHED, STATUS_NEW_ENTITY):
        result.status = STATUS_AMBIGUOUS
    return result


def _llm_review(result, entity_name, entity_type, entity_data, source_text) -> DisambiguationResult:
    """Step 6 is opt-in, requires real candidates and an existing review band.
    The bands are uncalibrated and cannot authorize automatic links/new IDs."""
    try:
        llm_result = rank_with_llm(entity_name, entity_type, entity_data, source_text, result.candidates)
        llm_result.auto_link = False
        llm_result.retain_candidates(result.candidates)
        llm_result.model_trace = result.model_trace + llm_result.model_trace
        if llm_result.status in (STATUS_AUTO_MATCHED, STATUS_NEW_ENTITY):
            llm_result.status = STATUS_AMBIGUOUS
        return llm_result
    except Exception as error:
        result.reason = f"LLM review failed; candidates retained: {error}"
        result.match_step = "step6_llm_error"
    return result


def _no_candidates_result(recall_failed: bool) -> DisambiguationResult:
    return DisambiguationResult(
        status=STATUS_AMBIGUOUS if recall_failed else STATUS_NEW_ENTITY,
        reason="candidate recall unavailable; requires review" if recall_failed else "no match in any step",
        match_step="step4_recall_error" if recall_failed else "step_none",
    )


def disambiguate(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None = None,
    source_text: str = "",
    source_site: str = "",
    session=None,
    *,
    enable_llm_review: bool = False,
) -> DisambiguationResult:
    """Resolve identity evidence; model scores are uncalibrated review suggestions."""
    entity_data = entity_data or {}
    candidate_names = generate_candidate_names(entity_name, entity_type, entity_data)
    normalized_name = normalize_for_alignment(entity_name, entity_type)
    if normalized_name and normalized_name != entity_name:
        entity_data["normalized_name"] = normalized_name

    # Step 1: the rule validates namespace, granularity and uniqueness.
    result = match_strong_id(entity_type, entity_data, session=session)
    if result:
        result.retain_candidates()
        return result

    result = _exact_alias_result(candidate_names, entity_type, session)
    if result:
        return result

    # Step 3: preserve both the selected dictionary ID and every rule candidate.
    step3_result = match_by_entity_rules(entity_name, entity_type, entity_data, session=session)
    if step3_result:
        step3_result.retain_candidates()
        if step3_result.status == STATUS_AUTO_MATCHED and step3_result.auto_link:
            return step3_result

    recall = _recall_candidates(entity_name, entity_type, entity_data, normalized_name, session)

    # A rule-only candidate is already a review proposal; no extra model is
    # needed when recall is unavailable or provides nothing further.
    if not recall.candidates and step3_result:
        return step3_result

    all_candidates = recall.candidates[:]
    if step3_result:
        all_candidates.extend(candidate for candidate in step3_result.candidates
                              if candidate.dict_id not in recall.seen_ids)
    if not all_candidates:
        return _no_candidates_result(recall.failed)

    result = _rank_proposals(entity_name, entity_type, entity_data, source_text, all_candidates)
    if (enable_llm_review and result.status == STATUS_AMBIGUOUS
            and result.match_step == "step5_reranker" and result.confidence >= SCORE_MID):
        return _llm_review(result, entity_name, entity_type, entity_data, source_text)
    return result


def disambiguate_entities(entities: list[dict], session=None,
                          *, enable_llm_review: bool = False) -> list[dict]:
    """Annotate the existing extraction payload with persistable link proposals."""
    for entity in entities:
        name, entity_type = entity.get("entity_name", ""), entity.get("entity_type", "")
        entity_data = entity.get("entity_data") or {}
        entity["entity_data"] = entity_data
        if not name or not entity_type:
            continue
        try:
            result = disambiguate(
                entity_name=name, entity_type=entity_type, entity_data=entity_data,
                source_text=entity.get("source_text", ""), source_site=entity.get("source_site", ""),
                session=session, enable_llm_review=enable_llm_review,
            )
        except Exception as error:
            logger.error(f"[Disambiguation] Error for '{name}': {error}")
            result = DisambiguationResult(
                status=STATUS_AMBIGUOUS, reason=f"disambiguation error: {error}",
                match_step="step_error",
            )
        # Match-step names and high model scores do not authorize a link.
        auto_link = result.status == STATUS_AUTO_MATCHED and result.auto_link and result.dict_id is not None
        if result.status == STATUS_AUTO_MATCHED and not auto_link:
            result.status = STATUS_CANDIDATE_MATCHED if result.dict_id is not None else STATUS_AMBIGUOUS
        entity["dict_id"] = result.dict_id if auto_link else None
        entity["confidence_score"] = result.confidence
        entity["confidence_reason"] = result.reason
        metadata = result.to_metadata()
        entity["_disambig"] = metadata
        entity_data["_disambig"] = metadata
    return entities
