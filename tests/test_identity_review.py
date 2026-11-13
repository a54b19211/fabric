"""Identity decisions and source evidence: no network or live database."""
from datetime import datetime, timezone

import pytest

from api.schemas.request import ReviewActionRequest
from graph.db.models import EntityDict, EntityRelation, ExtractedEntity


def test_plain_approval_does_not_implicitly_confirm_identity():
    action = ReviewActionRequest(reviewer='reviewer-human')
    assert action.confirm_identity is False
    assert action.basis == ''


def test_unknown_object_times_remain_unknown():
    from graph.db.identity import evidence_values
    values = evidence_values({'source_text': 'A sponsors B'}, 'document-1', 'trial-registry')
    assert values['version_id'] is None
    assert values['valid_from'] is None
    assert values['valid_to'] is None
    assert values['first_seen_at'] is None


def test_legacy_verified_boolean_is_not_auditable_human_identity():
    from graph.db.identity import auditable_identity
    assert not auditable_identity(EntityDict(is_verified=True))
    assert auditable_identity(EntityDict(is_verified=True, verified_by='reviewer-human',
                              verified_at=datetime.now(timezone.utc), verification_basis='Registry identifier checked'))


@pytest.mark.parametrize('reviewer', ['auto', 'system', 'AUTO', 'agent', '  system  '])
def test_machine_reviewer_cannot_supply_human_confirmation(reviewer):
    from graph.db.identity import require_human_reviewer
    with pytest.raises(ValueError, match='human'):
        require_human_reviewer(reviewer)


def test_same_english_name_is_batch_identity_but_known_ids_cannot_conflict():
    from graph.db.identity import batch_identity_keys, compatible_identity
    a = {'entity_type': 'Company', 'entity_name': '甲公司', 'entity_data': {'name_en': 'Acme Ltd'}}
    b = {'entity_type': 'Company', 'entity_name': '乙公司', 'entity_data': {'name_en': 'ACME LTD'}}
    assert batch_identity_keys(a) & batch_identity_keys(b)
    a['entity_data']['external_ids'] = {'lei': 'AAAA'}
    b['entity_data']['external_ids'] = {'lei': 'BBBB'}
    assert not compatible_identity(a['entity_data'], b['entity_data'])


def test_quote_offsets_must_identify_exact_unicode_span():
    from graph.db.identity import evidence_values, usable_evidence
    evidence = evidence_values({'version_id': 'a' * 64, 'quote': '甲乙',
                               'char_start': 3, 'char_end': 5}, 'doc', 'site')
    assert usable_evidence(evidence)
    evidence['char_end'] = 4
    assert not usable_evidence(evidence)


def test_persistence_models_include_immutable_source_reference_and_unknown_dates():
    for model in (ExtractedEntity, EntityRelation):
        for name in ('source_version_id', 'source_quote', 'source_char_start', 'source_char_end',
                     'first_seen_at', 'valid_from', 'valid_to'):
            assert name in model.__table__.c


def test_client_name_is_not_a_trusted_review_identity(monkeypatch):
    import json

    from api.review_auth import ReviewPrincipal, principal_from_token, require_trusted_review_context
    monkeypatch.setenv('DATAFABRIC_REVIEW_TOKENS', json.dumps({'fixture-token': 'server-owned-reviewer'}))
    with pytest.raises(ValueError, match='trusted'):
        require_trusted_review_context('human')
    with pytest.raises(ValueError, match='trusted'):
        require_trusted_review_context(ReviewPrincipal('human', object()))
    with pytest.raises(ValueError, match='Invalid'):
        principal_from_token('wrong-token')
    assert require_trusted_review_context(principal_from_token('fixture-token')) == 'server-owned-reviewer'


def test_unavailable_disambiguation_cannot_allocate_new_identity():
    from graph.db.identity import has_candidates
    assert has_candidates({'_disambig': {'status': 'ambiguous', 'match_step': 'step_error', 'candidates': []}})
    assert has_candidates({'_disambig': {'status': 'auto_matched', 'auto_link': False, 'dict_id': 1}})


@pytest.mark.parametrize('path,payload', [
    ('/api/review/entities/1/approve', {'reviewer': 'claimed-human', 'confirm_identity': True, 'basis': 'Claimed checked'}),
    ('/api/review/relations/1/approve', {'reviewer': 'claimed-human'}),
    ('/api/review/entity-link-proposals/1/accept', {'reviewer': 'claimed-human'}),
    ('/api/review/primekg-mapping-proposals/1/accept', {'reviewer': 'claimed-human'}),
    ('/api/review/merge-entities', {'reviewer': 'claimed-human', 'keeper_dict_id': 1, 'absorbed_dict_ids': [2]}),
    ('/api/review/rollback-merge', {'merge_log_id': 1}),
    ('/api/review/merge-queue/1/resolve', {'resolution': 'accept_new', 'reviewer': 'claimed-human'}),
    ('/api/review/merge-queue/resolve-batch', {'decisions': [], 'reviewer': 'claimed-human'}),
])
def test_sensitive_api_rejects_client_claimed_human_without_review_token(path, payload, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routers.review import router
    monkeypatch.delenv('DATAFABRIC_REVIEW_TOKENS', raising=False)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(path, json=payload)
    assert response.status_code == 403


def test_native_rule_ids_and_source_locators_are_not_lost():
    from graph.db.identity import evidence_values, namespaced_ids
    from graph.db.repository import _extract_external_ids
    native = {'rule_version': 'fixture.native.v1', 'identity_namespace': 'fda.recall_number', 'identity_value': 'D-0001-2020'}
    data = {'recall_number': 'D-0001-2020', 'event_id': 'event-1', '_native': native}
    assert _extract_external_ids('RecallProduct', data)['fda.recall_number'] == 'D-0001-2020'
    assert namespaced_ids(data)['fda.recall_number'] == 'd-0001-2020'
    evidence = evidence_values({'entity_data': data, 'locator': {'field': 'raw_content', 'json_pointer': '/recall_number'},
                                'source_quote': 'D-0001-2020', 'source_char_start': 3, 'source_char_end': 14}, 'doc', 'fda')
    assert evidence['details']['locator']['field'] == 'raw_content'
    assert evidence['details']['native'] == native and evidence['match_step'] == 'native_mapping'


def test_lookup_failure_is_not_a_valid_empty_candidate_set():
    from unittest.mock import MagicMock

    from graph.db.repository import search_similar_entities
    session = MagicMock()
    session.query.side_effect = RuntimeError('fixture-private-connection-data')
    with pytest.raises(RuntimeError, match='lookup unavailable') as captured:
        search_similar_entities([0.0] * 1024, 'Company', session=session)
    assert 'fixture-private' not in str(captured.value)
    assert search_similar_entities([0.0] * 1024, 'Company', session=session, strict_errors=False) == []


def test_legacy_alias_lookup_requires_unique_identity():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from graph.db.repository import lookup_alias, lookup_alias_candidates
    session = MagicMock()
    session.query.return_value.join.return_value.filter.return_value.distinct.return_value.order_by.return_value.all.return_value = [
        SimpleNamespace(id=11, canonical_data={'name': 'First'}, external_ids={}),
        SimpleNamespace(id=12, canonical_data={'name': 'Second'}, external_ids={})]
    assert [row['dict_id'] for row in lookup_alias_candidates('Same', 'Company', session)] == [11, 12]
    assert lookup_alias('Same', 'Company', session) is None


def test_patch_changes_invalidate_existing_confirmation_and_relation_review(monkeypatch):
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    from api.routers import review
    from api.schemas.request import PatchEntityRequest, PatchRelationRequest
    row = ExtractedEntity(entity_data={'id': 'old'}, review_status='approved',
        identity_confirmed_by='old-human', identity_confirmed_at=datetime.now(timezone.utc),
        identity_confirmation_basis='Old identifier checked')
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = row
    @contextmanager
    def sessions():
        yield session
    monkeypatch.setattr(review, 'get_session', sessions)
    review.patch_entity(1, PatchEntityRequest(entity_data={'id': 'new'}))
    assert row.identity_confirmed_by is None and row.identity_confirmed_at is None
    assert row.identity_confirmation_basis is None
    relation = EntityRelation(relation_props={}, review_status='approved', reviewed_by='old-human',
                              reviewed_at=datetime.now(timezone.utc))
    session.query.return_value.filter.return_value.first.return_value = relation
    review.patch_relation(1, PatchRelationRequest(relation_props={'new_claim': 'must be reviewed'}))
    assert relation.review_status == 'pending' and relation.reviewed_at is None


def test_two_known_ids_without_a_shared_namespace_cannot_merge_by_name():
    from graph.db.identity import batch_identity_keys, compatible_identity
    assert not compatible_identity({'external_ids': {'lei': 'AAAA'}},
                                   {'external_ids': {'stock.nyse': 'BBBB'}})
    # A recall event can contain several products; event_id is not their identity key.
    a = {'entity_type': 'RecallProduct', 'entity_name': 'Product A', 'entity_data': {'external_ids': {'event_id': 'SAME'}}}
    b = {'entity_type': 'RecallProduct', 'entity_name': 'Product B', 'entity_data': {'external_ids': {'event_id': 'SAME'}}}
    assert not batch_identity_keys(a) & batch_identity_keys(b)


def test_candidate_similarity_score_is_preserved_exactly(monkeypatch):
    from types import SimpleNamespace

    import graph.db.identity as identity
    calls = []
    monkeypatch.setattr(identity, 'add_identity_proposal', lambda *args, **kwargs: calls.append(kwargs))
    identity.persist_candidates(None, SimpleNamespace(id=7), {'entity_data': {'_disambig': {
        'status': 'candidate_matched', 'match_step': 'step5_llm_rank', 'rule_version': 'fixture.rules',
        'model_version': 'fixture.model', 'confidence': 0.75,
        'candidates': [{'dict_id': 17, 'name': 'Candidate', 'similarity_score': 0.91}]}}}, 'observation')
    assert calls[0]['agent_confidence'] == 0.91
    assert calls[0]['rule_version'] == 'fixture.rules' and calls[0]['model_version'] == 'fixture.model'


@pytest.mark.parametrize('cached', [False, True])
def test_alias_writeback_can_explicitly_avoid_http_under_review_locks(monkeypatch, cached):
    from unittest.mock import MagicMock

    from graph.db.repository import _write_aliases_with_embedding
    from graph.extraction import llm_client
    generate = MagicMock(side_effect=AssertionError('Review transaction must not call the embedding service'))
    monkeypatch.setattr(llm_client, 'generate_embedding', generate)
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None
    vector = [0.0] * 1024
    data = {'_embeddings': {'Synthetic Name': vector}} if cached else {}
    assert _write_aliases_with_embedding(session, 17, 'Company', 'Synthetic Name', data,
                                         allow_embedding_generation=False) == 1
    assert session.add.call_args.args[0].name_embedding == (vector if cached else None)
    generate.assert_not_called()
