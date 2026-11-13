"""Isolated PostgreSQL evidence and review tests. Never uses a configured live APP database.

Run explicitly with DATAFABRIC_PUBLICATION_DATABASE_URL pointing at the coordinator's
18636 / bioradar_kdd_opus_fabric instance. Source rows remain in the named test schema.
The fixed-document validator below is an explicit fixture; History's real reader is
covered separately by the object-evidence integration suite.
"""
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import legacy_bridge
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from api.review_auth import principal_from_token
from graph.db import repository as repo
from graph.db.identity import auditable_identity, get_object_evidence
from graph.db.models import (
    EntityAlias,
    EntityDict,
    EntityIdentityMergeProposal,
    EntityMergeLog,
    EntityMergeQueue,
    EntityPrimeKGMappingProposal,
    EntityRelation,
    ExtractedEntity,
)


@pytest.fixture(scope='module')
def engine():
    url = os.environ.get('DATAFABRIC_PUBLICATION_DATABASE_URL')
    if not url:
        pytest.fail('Explicit isolated source PostgreSQL is required')
    parsed = make_url(url)
    assert parsed.host in ('127.0.0.1', 'localhost') and parsed.port == 18636
    assert parsed.database == 'bioradar_kdd_opus_fabric'
    schema = 'opus_identity_20261031_' + uuid4().hex[:10]
    os.environ['DATAFABRIC_IDENTITY_TEST_SCHEMA_RESULT'] = schema
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.exec_driver_sql('CREATE SCHEMA IF NOT EXISTS ' + schema)
    scoped = create_engine(url, connect_args={'options': '-csearch_path=' + schema + ',public'})
    migration_dir = Path(__file__).resolve().parents[1] / 'store' / 'migrations' / 'versions'
    with scoped.begin() as conn:
        for migration in sorted(migration_dir.glob('V*.sql')):
            if int(migration.name[1:4]) <= 16 or migration.name.startswith('V026__'):
                conn.exec_driver_sql(migration.read_text())
        legacy_id = conn.execute(text("INSERT INTO entity_dict(entity_type,canonical_data,is_verified) VALUES ('Company','{}',true) RETURNING id")).scalar_one()
        original = conn.execute(text('SELECT is_verified FROM entity_dict WHERE id=:id'), {'id': legacy_id}).scalar_one()
        assert original
        conn.exec_driver_sql((migration_dir / 'V035__identity_evidence.sql').read_text())
        conn.exec_driver_sql((migration_dir / 'V035__identity_evidence.sql').read_text())
        legacy = conn.execute(text('SELECT is_verified,verified_by,verified_at FROM entity_dict WHERE id=:id'), {'id': legacy_id}).one()
        assert tuple(legacy) == (True, None, None)
    yield scoped
    scoped.dispose()
    admin.dispose()


@pytest.fixture
def principal(monkeypatch):
    monkeypatch.setenv('DATAFABRIC_REVIEW_TOKENS', json.dumps({'explicit-fixture-token': 'fixture-human-reviewer'}))
    return principal_from_token('explicit-fixture-token')


@pytest.fixture
def fixed_document(monkeypatch):
    content = '甲公司 sponsors Trial ABC. Exact source evidence.'
    def check(evidence, *, source_doc_uuid, source_site):
        assert source_doc_uuid and source_site
        if evidence.get('version_id') != 'a' * 64 or content[evidence['char_start']:evidence['char_end']] != evidence['quote']:
            raise ValueError('Fixed version quote does not match source')
        return {**evidence, 'fixture_source_validated': True}
    monkeypatch.setitem(sys.modules, 'graph.db.object_evidence', SimpleNamespace(validate_object_evidence=check))
    return {'version_id': 'a' * 64, 'quote': content, 'char_start': 0, 'char_end': len(content),
            'source_doc_uuid': 'fixture-doc', 'source_site': 'fixture-site'}


@pytest.fixture
def session(engine, monkeypatch):
    @contextmanager
    def sessions():
        with Session(engine) as active, active.begin():
            yield active
    monkeypatch.setattr('graph.db.session.get_session', sessions)
    monkeypatch.setattr(repo.aliases, '_write_aliases_with_embedding', lambda *a, **k: 0)
    with Session(engine) as active, active.begin():
        yield active


def dictionary(session, name=None, **extra):
    row = EntityDict(entity_type='Company', canonical_data={'name': name or uuid4().hex},
                     external_ids=extra.pop('external_ids', {}), is_verified=False, **extra)
    session.add(row)
    session.flush()
    return row


def entity(session, target, document='fixture-doc', **extra):
    row = ExtractedEntity(source_doc_uuid=document, source_site='fixture-site', entity_type='Company',
                          entity_name=(target.canonical_data or {}).get('name', 'Company'),
                          dict_id=target.id, review_status='pending', **extra)
    session.add(row)
    session.flush()
    return row


def test_repeated_hits_are_evidence_not_approval_and_replay_is_idempotent(session):
    target = dictionary(session)
    record = {'entity_type': 'Company', 'entity_name': target.canonical_data['name'], 'dict_id': target.id,
              'source_text': 'A source quote', '_disambig': {'status': 'auto_matched', 'auto_link': True,
                                                         'dict_id': target.id, 'match_step': 'step1_strong_id'}}
    first = repo.insert_extraction_result('source-one', 'site-one', [record], [], session=session)[0]
    second = repo.insert_extraction_result('source-two', 'site-two', [record], [], session=session)[0]
    replay = repo.insert_extraction_result('source-two', 'site-two', [record], [], session=session)[0]
    assert first == second == replay
    assert session.get(ExtractedEntity, first).review_status == 'pending'
    evidence = get_object_evidence('entity', first, session)
    assert len(evidence) == 2
    assert {item['source_site'] for item in evidence} == {'site-one', 'site-two'}
    assert all(item['match_step'] == 'step1_strong_id' for item in evidence)


def test_candidates_persist_every_score_and_versions_without_new_dictionary(session, principal):
    targets = [dictionary(session), dictionary(session)]
    before = session.query(EntityDict).count()
    record = {'entity_type': 'Company', 'entity_name': uuid4().hex, 'source_text': 'Candidate source',
              '_disambig': {'status': 'candidate_matched', 'dict_id': targets[0].id,
                  'match_step': 'step5_llm_rank', 'rule_version': 'rules.test.v1', 'model_version': 'model.test.v1',
                  'candidates': [{'dict_id': targets[0].id, 'score': 0.8}, {'dict_id': targets[1].id, 'score': 0.6}]}}
    source = 'candidate-' + uuid4().hex
    entity_id = repo.insert_extraction_result(source, 'fixture-site', [record], [], session=session)[0]
    assert repo.insert_extraction_result(source, 'fixture-site', [record], [], session=session)[0] == entity_id
    assert session.query(EntityDict).count() == before
    observed = session.get(ExtractedEntity, entity_id)
    assert observed.dict_id is None and observed.review_status == 'pending'
    proposals = session.query(EntityIdentityMergeProposal).filter_by(source_entity_id=entity_id).order_by(EntityIdentityMergeProposal.target_dict_id).all()
    assert len(proposals) == 2 and [p.agent_confidence for p in proposals] == [0.8, 0.6]
    assert all(p.rule_version == 'rules.test.v1' and p.model_version == 'model.test.v1' for p in proposals)
    with pytest.raises(ValueError, match='trusted'):
        repo.resolve_identity_merge_proposal(proposals[0].id, 'approved', 'claimed-human', session=session)
    repo.resolve_identity_merge_proposal(proposals[0].id, 'approved', 'forged-body-name', session=session, review_context=principal)
    assert observed.dict_id == targets[0].id and observed.review_status == 'pending'
    assert proposals[0].reviewer == principal.subject and proposals[0].reviewed_at
    assert proposals[0].merge_log_id
    repo.resolve_identity_merge_proposal(proposals[0].id, 'withdrawn', '', session=session, review_context=principal)
    assert observed.dict_id is None and observed.review_status == 'pending'
    assert proposals[0].status == 'withdrawn'
    repo.resolve_identity_merge_proposal(proposals[1].id, 'rejected', '', session=session, review_context=principal)
    assert proposals[1].status == 'rejected' and observed.dict_id is None


def test_ambiguous_error_without_candidates_cannot_create_dictionary(session):
    before = session.query(EntityDict).count()
    record = {'entity_type': 'Company', 'entity_name': uuid4().hex,
              '_disambig': {'status': 'ambiguous', 'match_step': 'step_error', 'candidates': []}}
    result = repo.insert_extraction_result(uuid4().hex, 'fixture-site', [record], [], session=session)
    assert session.get(ExtractedEntity, result[0]).dict_id is None
    assert session.query(EntityDict).count() == before


def test_batch_english_name_and_namespaced_ids_but_no_conflicting_strong_id_merge(session):
    name_en = uuid4().hex
    records = [{'entity_type': 'Company', 'entity_name': '甲' + name_en, 'entity_data': {'name_en': name_en}},
               {'entity_type': 'Company', 'entity_name': '乙' + name_en, 'entity_data': {'name_en': name_en.upper()}}]
    result = repo.insert_extraction_result(uuid4().hex, 'fixture-site', records, [], session=session)
    assert len({session.get(ExtractedEntity, key).dict_id for key in result}) == 1
    shared = uuid4().hex
    conflicting = [{'entity_type': 'Company', 'entity_name': shared,
                    'entity_data': {'name_en': shared, 'external_ids': {'lei': value}}} for value in ('AAAA', 'BBBB')]
    result = repo.insert_extraction_result(uuid4().hex, 'fixture-site', conflicting, [], session=session)
    assert len({session.get(ExtractedEntity, key).dict_id for key in result}) == 2


def test_plain_approval_and_trusted_explicit_confirmation(session, principal, fixed_document):
    target = dictionary(session)
    observed = entity(session, target)
    repo.approve_entity_with_alias_writeback(observed.id, 'plain-reviewer', session=session)
    assert observed.review_status == 'approved' and not target.is_verified
    with pytest.raises(ValueError, match='trusted'):
        repo.approve_entity_with_alias_writeback(observed.id, 'human', confirm_identity=True, basis='Registry checked',
            evidence=[fixed_document], session=session)
    repo.approve_entity_with_alias_writeback(observed.id, 'forged-body-name', confirm_identity=True, basis='Registry checked',
        evidence=[fixed_document], session=session, review_context=principal)
    assert target.is_verified and auditable_identity(target)
    assert target.verified_by == principal.subject and target.verified_at and target.verification_basis == 'Registry checked'
    assert get_object_evidence('entity', observed.id, session)[0]['version_id'] == 'a' * 64


def test_relation_blocks_unknown_identity_and_requires_checked_fixed_source(session, principal, fixed_document):
    targets = [dictionary(session), dictionary(session)]
    endpoints = [entity(session, target) for target in targets]
    for endpoint in endpoints:
        endpoint.review_status = 'approved'
    targets[0].is_verified = True  # legacy boolean alone cannot authorise a relation
    relation = EntityRelation(source_doc_uuid='fixture-doc', source_site='fixture-site',
        from_entity_id=endpoints[0].id, to_entity_id=endpoints[1].id, relation_type='SPONSORS', review_status='pending')
    session.add(relation)
    session.flush()
    with pytest.raises(ValueError, match='Both relation endpoints'):
        repo.approve_relation(relation.id, '', evidence=[fixed_document], session=session, review_context=principal)
    assert relation.review_status == 'pending'
    for endpoint in endpoints:
        repo.approve_entity_with_alias_writeback(endpoint.id, '', confirm_identity=True, basis='Checked registry',
            evidence=[fixed_document], session=session, review_context=principal)
    with pytest.raises(ValueError, match='source evidence'):
        repo.approve_relation(relation.id, '', session=session, review_context=principal)
    wrong = {**fixed_document, 'quote': 'X' * len(fixed_document['quote'])}
    with pytest.raises(ValueError, match='does not match'):
        repo.approve_relation(relation.id, '', evidence=[wrong], session=session, review_context=principal)
    repo.approve_relation(relation.id, 'forged', evidence=[fixed_document], session=session, review_context=principal)
    assert relation.review_status == 'approved' and relation.reviewed_by == principal.subject


def test_dictionary_merge_restores_full_snapshots_not_just_ids(session, principal):
    keeper = dictionary(session, external_ids={'lei': 'AAA'})
    absorbed = dictionary(session, external_ids={'registry.native': 'B'})
    observed = entity(session, absorbed)
    observed.review_status = 'approved'
    observed.identity_confirmed_by = principal.subject
    observed.identity_confirmed_at = datetime.now(timezone.utc)
    observed.identity_confirmation_basis = 'Original identity checked before merge'
    alias = EntityAlias(dict_id=absorbed.id, entity_type='Company', name=uuid4().hex, is_deleted=False)
    queue = EntityMergeQueue(source_entity_id=observed.id, target_dict_id=absorbed.id,
                             field_name='name', existing_value={'v': 1}, new_value={'v': 2}, resolution='pending')
    session.add_all([alias, queue])
    session.flush()
    result = repo.merge_entities(keeper.id, [absorbed.id], {}, session=session, review_context=principal)
    assert absorbed.is_deleted and observed.dict_id == keeper.id and alias.dict_id == keeper.id
    assert observed.identity_confirmed_by is None and observed.identity_confirmed_at is None
    assert queue.resolution == 'resolved_merged' and keeper.external_ids['registry.native'] == 'B'
    rollback = repo.rollback_merge(result['merge_log_id'], 'Fixture withdrawal', session=session, review_context=principal)
    assert rollback['success'] and not absorbed.is_deleted and observed.dict_id == absorbed.id
    assert alias.dict_id == absorbed.id and not alias.is_deleted
    assert observed.identity_confirmed_by == principal.subject and observed.identity_confirmation_basis == 'Original identity checked before merge'
    assert queue.resolution == 'pending' and keeper.external_ids == {'lei': 'AAA'}
    assert session.get(EntityMergeLog, result['merge_log_id']).is_rolled_back


def test_primekg_stays_candidate_until_trusted_identity_and_checked_evidence(session, principal, fixed_document):
    target = dictionary(session)
    observed = entity(session, target)
    key = repo.create_primekg_mapping_proposal(target.id, 'primekg.drugbank', 'DBTEST', 'fixture-name', 0.75,
                                               fixed_document, session=session)
    proposal = session.get(EntityPrimeKGMappingProposal, key)
    assert proposal.status == 'pending'
    with pytest.raises(ValueError, match='auditable'):
        repo.resolve_primekg_mapping_proposal(key, 'approved', 'human', session=session, review_context=principal)
    repo.approve_entity_with_alias_writeback(observed.id, '', confirm_identity=True, basis='Registry checked',
        evidence=[fixed_document], session=session, review_context=principal)
    repo.resolve_primekg_mapping_proposal(key, 'approved', 'spoofed-name', session=session, review_context=principal)
    assert proposal.status == 'approved' and proposal.reviewer == principal.subject and proposal.reviewed_at
    repo.resolve_primekg_mapping_proposal(key, 'withdrawn', '', session=session, review_context=principal)
    assert proposal.status == 'withdrawn'


def test_migration_runner_executes_v021_transactionally_and_tracks_it_once(engine, monkeypatch):
    from store.migrations import app_ledger, runner
    migration = Path(__file__).resolve().parents[1] / 'store' / 'migrations' / 'versions' / 'V035__identity_evidence.sql'
    runner.apply('app', engine)
    runner.apply('app', engine)
    with engine.begin() as conn:
        row = conn.execute(text("SELECT count(*),min(checksum) FROM schema_migrations WHERE version='V035'")).one()
    assert row[0] == 1 and row[1] == app_ledger._checksum(migration.read_text())


@pytest.fixture(scope='module')
def real_history_engine():
    url = os.environ.get('DATAFABRIC_HISTORY_DATABASE_URL')
    assert url, 'Explicit isolated History PostgreSQL is required'
    parsed = make_url(url)
    assert parsed.host in ('127.0.0.1', 'localhost') and parsed.port == 18636
    assert parsed.database == 'bioradar_kdd_opus_history'
    schema = 'opus_identity_history_20261031'
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.exec_driver_sql('CREATE SCHEMA IF NOT EXISTS ' + schema)
    scoped = create_engine(url, connect_args={'options': '-csearch_path=' + schema})
    from store.migrations import runner
    runner.apply('history', scoped)
    yield scoped
    scoped.dispose()
    admin.dispose()


@pytest.fixture
def real_history(real_history_engine, monkeypatch):
    from store import history
    monkeypatch.setattr(history, 'history_engine', lambda: real_history_engine)
    monkeypatch.setenv('DATAFABRIC_HISTORY_SOURCE_ALLOWLIST', '["legacy_documents","pubmed","clinicaltrials","primekg_graph"]')
    monkeypatch.setenv('DATAFABRIC_PUBLICATION_IS_DEMO', 'true')
    legacy_bridge.open_old_writer(monkeypatch)  # these tests freeze documents through the legacy bridge
    return real_history_engine


def freeze_document(doc_uuid, raw, source_site='pubmed'):
    from graph.publish.collected_document import collected_document
    from graph.publish.reviewed_graph import document_native_id
    from store import history
    from store.history_models import ArchiveImport, ArchiveRecord
    document = collected_document({'uuid': doc_uuid, 'source_site': source_site, 'title': 'Explicit synthetic source',
                                   'raw_content': raw}, datetime.now(timezone.utc), 'insert')
    request = ArchiveImport(source_id='legacy_documents', is_demo=True,
        scope_description='Explicit synthetic identity evidence; no production documents', declared_count=1,
        records=[ArchiveRecord(source_record_id=document_native_id(document), record_kind='trial' if document.source_projection is not None else 'document',
                               raw_content=document.model_dump_json())])
    run = history.import_archive(request, 'identity-source-' + uuid4().hex)
    return {'source_id': 'legacy_documents', 'source_record_id': document_native_id(document),
            'version_id': run.version_ids[0], 'source_doc_uuid': doc_uuid, 'source_site': source_site,
            'quote': raw, 'char_start': 0, 'char_end': len(raw), 'is_demo': True,
            'locator': {'field': 'raw_content', 'offset_unit': 'unicode_codepoint'}}


def test_real_history_validator_binds_source_sha_unicode_and_permission(real_history, monkeypatch):
    from graph.db.object_evidence import validate_object_evidence
    from store.history_models import HistoryError
    doc_uuid = 'identity-real-' + uuid4().hex
    evidence = freeze_document(doc_uuid, '甲公司🧪 has a registered sponsor relationship.')
    checked = validate_object_evidence(evidence, source_doc_uuid=doc_uuid, source_site='pubmed')
    assert checked['raw_sha256'] and checked['quote'] == evidence['quote']
    assert checked['locator']['field'] == 'raw_content'
    from store import history
    frozen = history.read_version(checked['version_id'], checked['source_id'], checked['source_record_id'],
                                  checked['is_demo'], 'current', datetime.now(timezone.utc))
    container = json.loads(frozen.raw_content)
    for token in checked['locator']['container_json_pointer'].split('/')[1:]:
        container = container[token.replace('~1', '/').replace('~0', '~')]
    assert isinstance(container, str)
    assert container[checked['char_start']:checked['char_end']] == checked['quote']

    with pytest.raises(ValueError, match='来源文档'):
        validate_object_evidence(evidence, source_doc_uuid='another-document', source_site='pubmed')
    with pytest.raises(ValueError, match='来源文档'):
        validate_object_evidence(evidence, source_doc_uuid=doc_uuid, source_site='clinicaltrials')
    with pytest.raises(ValueError, match='不一致'):
        validate_object_evidence({**evidence, 'raw_sha256': '0' * 64}, source_doc_uuid=doc_uuid, source_site='pubmed')
    wrong_quote = {**evidence, 'quote': 'X' * len(evidence['quote'])}
    with pytest.raises(ValueError, match='原文不一致'):
        validate_object_evidence(wrong_quote, source_doc_uuid=doc_uuid, source_site='pubmed')
    monkeypatch.setenv('DATAFABRIC_PUBLICATION_IS_DEMO', 'false')
    with pytest.raises(ValueError, match='真实/演示'):
        validate_object_evidence(evidence, source_doc_uuid=doc_uuid, source_site='pubmed')
    monkeypatch.setenv('DATAFABRIC_PUBLICATION_IS_DEMO', 'true')
    monkeypatch.setenv('DATAFABRIC_HISTORY_SOURCE_ALLOWLIST', '["legacy_documents"]')
    with pytest.raises(HistoryError):
        validate_object_evidence(evidence, source_doc_uuid=doc_uuid, source_site='pubmed')


def test_real_history_entity_relation_and_mapping_confirmation_with_two_sources(session, principal, real_history):
    names = ['甲' + uuid4().hex, '乙' + uuid4().hex]
    raw = names[0] + ' sponsors ' + names[1] + '. 原始规则记录。'
    first_doc, second_doc = 'first-' + uuid4().hex, 'second-' + uuid4().hex
    evidence_one, evidence_two = freeze_document(first_doc, raw), freeze_document(second_doc, raw)
    targets = [dictionary(session, name=name) for name in names]
    records = [{**evidence_one, 'entity_type': 'Company', 'entity_name': name,
                'dict_id': target.id, 'entity_data': {}, '_disambig': {'status': 'auto_matched', 'auto_link': True,
                    'dict_id': target.id, 'match_step': 'step1_strong_id'}} for target, name in zip(targets, names)]
    relation_input = {**evidence_one, 'from_entity_idx': 0, 'to_entity_idx': 1, 'relation_type': 'SPONSORS'}
    ids = repo.insert_extraction_result(first_doc, 'pubmed', records, [relation_input], session=session)
    second_records = [{**record, **evidence_two} for record in records]
    assert repo.insert_extraction_result(second_doc, 'pubmed', second_records, [], session=session) == ids
    for identity in ids:
        repo.approve_entity_with_alias_writeback(identity, 'spoofed', confirm_identity=True, basis='Both frozen sources checked',
            session=session, review_context=principal)
        assert len(get_object_evidence('entity', identity, session)) == 2
        mention = session.get(ExtractedEntity, identity)
        assert mention.identity_confirmed_by == principal.subject and mention.identity_confirmed_at
    relation = session.query(EntityRelation).filter(EntityRelation.source_doc_uuid == first_doc).one()
    repo.approve_relation(relation.id, 'spoofed', session=session, review_context=principal)
    assert relation.review_status == 'approved' and relation.reviewed_by == principal.subject
    mapping = repo.create_primekg_mapping_proposal(targets[0].id, 'primekg.company', 'SYNTHETIC-ID', 'human checked identifier',
                                                   0.9, evidence_one, session=session)
    repo.resolve_primekg_mapping_proposal(mapping, 'approved', 'spoofed', session=session, review_context=principal)
    assert session.get(EntityPrimeKGMappingProposal, mapping).status == 'approved'
    # A new mention to a previously confirmed dictionary stays unresolved after plain approve.
    extra = entity(session, targets[0], document='new-mention-' + uuid4().hex)
    repo.approve_entity_with_alias_writeback(extra.id, 'ordinary-client', session=session)
    assert extra.review_status == 'approved' and extra.identity_confirmed_by is None
    with pytest.raises(ValueError, match='not recorded'):
        repo.approve_entity_with_alias_writeback(extra.id, '', confirm_identity=True, basis='Claimed other source',
            evidence=[evidence_two], session=session, review_context=principal)


def test_confirmation_rejects_identity_changes_during_real_history_read(session, principal, real_history, monkeypatch):
    import graph.db.object_evidence as validator
    raw = 'Explicit synthetic source for identity race check.'
    doc_uuid = 'race-' + uuid4().hex
    evidence = freeze_document(doc_uuid, raw)
    target = dictionary(session)
    observed = entity(session, target, document=doc_uuid)
    observed.source_site = 'pubmed'
    original = validator.validate_object_evidence
    def changed(evidence, **kwargs):
        checked = original(evidence, **kwargs)
        observed.entity_data = {'name_en': 'Identity changed during validation'}
        return checked
    monkeypatch.setattr(validator, 'validate_object_evidence', changed)
    with pytest.raises(ValueError, match='Entity changed'):
        repo.approve_entity_with_alias_writeback(observed.id, '', confirm_identity=True, basis='Before change',
            evidence=[evidence], session=session, review_context=principal)
    assert not target.is_verified and observed.identity_confirmed_by is None


def test_committed_concurrent_source_change_cannot_pass_confirmation(engine, principal, real_history, monkeypatch):
    import graph.db.object_evidence as validator
    doc_uuid = 'committed-race-' + uuid4().hex
    evidence = freeze_document(doc_uuid, 'Synthetic frozen identity source before concurrent edit.')
    with Session(engine) as seed, seed.begin():
        target = dictionary(seed)
        observed = entity(seed, target, document=doc_uuid)
        observed.source_site = 'pubmed'
        seed.flush()
        entity_id, dictionary_id = observed.id, target.id
    @contextmanager
    def sessions():
        with Session(engine) as active, active.begin():
            yield active
    monkeypatch.setattr('graph.db.session.get_session', sessions)
    monkeypatch.setattr(repo.aliases, '_write_aliases_with_embedding', lambda *a, **k: 0)
    original = validator.validate_object_evidence
    def concurrent_edit(evidence, **kwargs):
        checked = original(evidence, **kwargs)
        with Session(engine) as writer, writer.begin():
            row = writer.get(ExtractedEntity, entity_id)
            row.entity_data = {'name_en': 'Committed concurrent identity change'}
        return checked
    monkeypatch.setattr(validator, 'validate_object_evidence', concurrent_edit)
    with pytest.raises(ValueError, match='Entity changed'):
        repo.approve_entity_with_alias_writeback(entity_id, '', confirm_identity=True, basis='Old identity inspected',
            evidence=[evidence], review_context=principal)
    with Session(engine) as check:
        assert check.get(ExtractedEntity, entity_id).review_status == 'pending'
        assert check.get(ExtractedEntity, entity_id).identity_confirmed_by is None
        assert not check.get(EntityDict, dictionary_id).is_verified


def test_complete_migration_stream_preserves_postgresql_function_bodies(engine, monkeypatch):
    from store.migrations import runner
    schema = 'opus_identity_migrations_' + uuid4().hex[:10]
    os.environ['DATAFABRIC_IDENTITY_MIGRATION_SCHEMA_RESULT'] = schema
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE SCHEMA ' + schema)
    scoped = create_engine(engine.url, connect_args={'options': '-csearch_path=' + schema + ',public'})
    runner.apply('app', scoped)
    runner.apply('app', scoped)
    with scoped.begin() as conn:
        versions = conn.execute(text('SELECT version FROM schema_migrations ORDER BY version')).scalars().all()
        assert versions == [f'V{index:03}' for index in range(1, 36)]
        columns = conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_schema=:schema AND table_name='extracted_entities'"), {'schema': schema}).scalars().all()
        assert 'identity_confirmed_by' in columns and 'source_version_id' in columns
        assert conn.execute(text('SELECT count(*) FROM publication_outbox')).scalar_one() == 0
    scoped.dispose()


def native_trial_original(nct):
    from test_native_extraction import trial_raw
    return trial_raw(markdown=True).replace('NCT00000001', nct)


def native_trial_record(nct):
    from graph.extraction.native_extraction import extract_native_entities
    return next(item for item in extract_native_entities(native_trial_original(nct), 'clinicaltrials')['entities']
                if item['entity_type'] == 'ClinicalTrial')


def test_concurrent_native_first_observation_has_one_dictionary(engine, session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    nct = 'NCT' + str(int(uuid4().hex[:8], 16) % 100000000).zfill(8)
    barrier = Barrier(2)
    def write(index):
        with Session(engine) as active, active.begin():
            from graph.db.models import Document
            doc_uuid = 'native-concurrent-' + uuid4().hex
            active.add(Document(uuid=doc_uuid, source_site='clinicaltrials', raw_content=native_trial_original(nct)))
            active.flush()
            barrier.wait(timeout=10)
            return repo.insert_extraction_result(doc_uuid, 'clinicaltrials',
                                                  [native_trial_record(nct)], [], session=active)[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert results[0] == results[1]
    with Session(engine) as check:
        rows = check.query(EntityDict).filter(EntityDict.entity_type == 'ClinicalTrial',
                                               EntityDict.external_ids['trial_id'].astext == nct).all()
        assert len(rows) == 1 and not rows[0].is_verified
        observed = check.get(ExtractedEntity, results[0])
        assert observed.review_status == 'pending' and observed.identity_confirmed_by is None
        assert len({proof['source_doc_uuid'] for proof in get_object_evidence('entity', observed.id, check)}) == 2


def test_existing_native_duplicate_ids_are_candidates_without_rewriting_legacy_rows(session):
    nct = 'NCT' + str(int(uuid4().hex[:8], 16) % 100000000).zfill(8)
    old = [EntityDict(entity_type='ClinicalTrial', external_ids={'trial_id': nct},
                      canonical_data={'name': nct}, is_verified=False) for _ in range(2)]
    session.add_all(old)
    session.flush()
    count = session.query(EntityDict).count()
    from graph.db.models import Document
    doc_uuid = uuid4().hex
    session.add(Document(uuid=doc_uuid, source_site='clinicaltrials', raw_content=native_trial_original(nct)))
    session.flush()
    entity_id = repo.insert_extraction_result(doc_uuid, 'clinicaltrials', [native_trial_record(nct)], [], session=session)[0]
    assert session.query(EntityDict).count() == count
    observed = session.get(ExtractedEntity, entity_id)
    assert observed.dict_id is None and observed.review_status == 'pending'
    proposals = session.query(EntityIdentityMergeProposal).filter_by(source_entity_id=entity_id).all()
    assert {proposal.target_dict_id for proposal in proposals} == {row.id for row in old}
    assert all(proposal.agent_confidence == 1.0 for proposal in proposals)
    assert all(not row.is_deleted and not row.is_verified for row in old)


def test_review_http_serializers_keep_audits_fixed_evidence_and_dictionary_context(engine, principal, real_history, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routers import review
    doc_uuid = 'http-evidence-' + uuid4().hex
    raw = 'Synthetic Company A sponsors Synthetic Company B.'
    evidence = freeze_document(doc_uuid, raw)
    with Session(engine) as seed, seed.begin():
        targets = [dictionary(seed, name='Synthetic Company ' + value) for value in ('A', 'B')]
        records = [{**evidence, 'entity_type': 'Company', 'entity_name': target.canonical_data['name'],
                    'dict_id': target.id} for target in targets]
        ids = repo.insert_extraction_result(doc_uuid, 'pubmed', records,
            [{**evidence, 'from_entity_idx': 0, 'to_entity_idx': 1, 'relation_type': 'SPONSORS'}], session=seed)
    @contextmanager
    def sessions():
        with Session(engine) as active, active.begin():
            yield active
    monkeypatch.setattr('graph.db.session.get_session', sessions)
    monkeypatch.setattr(review, 'get_session', sessions)
    monkeypatch.setattr(repo.aliases, '_write_aliases_with_embedding', lambda *a, **k: 0)
    for entity_id in ids:
        repo.approve_entity_with_alias_writeback(entity_id, '', confirm_identity=True, basis='Source checked for HTTP export',
                                                 review_context=principal)
    with Session(engine) as check:
        relation_id = check.query(EntityRelation.id).filter_by(source_doc_uuid=doc_uuid).scalar()
    repo.approve_relation(relation_id, '', review_context=principal)
    app = FastAPI()
    app.include_router(review.router)
    with TestClient(app) as client:
        detail = client.get(f'/api/review/entities/{ids[0]}')
        assert detail.status_code == 200
        exported = detail.json()
        assert exported['identity_confirmed_by'] == principal.subject and exported['identity_confirmed_at']
        assert exported['dict_context']['verified_by'] == principal.subject
        assert exported['current_review_evidence'][0]['version_id'] == evidence['version_id']
        assert exported['evidence'][0]['version_id'] == evidence['version_id']
        assert exported['evidence'][0]['locator']['container_json_pointer'] == '/original_raw_content'
        listed = client.get('/api/review/entities', params={'source_doc_uuid': doc_uuid, 'review_status': 'approved'})
        assert listed.status_code == 200 and len(listed.json()['items']) == 2
        relation = client.get(f'/api/review/relations/{relation_id}')
        assert relation.status_code == 200 and relation.json()['evidence'][0]['version_id'] == evidence['version_id']
        graph = client.get('/api/review/graph', params={'source_doc_uuid': doc_uuid, 'review_status': 'approved'})
        assert graph.status_code == 200
        assert graph.json()['nodes'][0]['identity_confirmed_by'] == principal.subject
        assert graph.json()['edges'][0]['evidence'][0]['version_id'] == evidence['version_id']


def test_native_mapper_real_frozen_legacy_document_confirms_without_fabricated_history_refs(session, principal, real_history):
    from test_native_extraction import trial_raw

    from graph.db.object_evidence import validate_object_evidence
    from graph.extraction.native_extraction import extract_native_entities
    raw = trial_raw(markdown=True)
    doc_uuid = 'native-fixed-' + uuid4().hex
    reference = freeze_document(doc_uuid, raw, source_site='clinicaltrials')
    from graph.db.models import Document
    session.add(Document(uuid=doc_uuid, source_site='clinicaltrials', raw_content=raw))
    session.flush()
    result = extract_native_entities(raw, 'clinicaltrials', source_version_id=reference['version_id'])
    assert 'source_id' not in result['entities'][0] and 'raw_sha256' not in result['entities'][0]
    ids = repo.insert_extraction_result(doc_uuid, 'clinicaltrials', result['entities'], result['relations'], session=session)
    trial_index = next(index for index, record in enumerate(result['entities']) if record['entity_type'] == 'ClinicalTrial')
    trial_id = ids[trial_index]
    repo.approve_entity_with_alias_writeback(trial_id, '', confirm_identity=True, basis='Frozen native registration fields checked',
                                             session=session, review_context=principal)
    assert session.get(ExtractedEntity, trial_id).identity_confirmed_by == principal.subject
    evidence = get_object_evidence('entity', trial_id, session)[0]
    checked = validate_object_evidence(evidence, source_doc_uuid=doc_uuid, source_site='clinicaltrials')
    assert checked['locator']['container_json_pointer'] == '/original_raw_content'
    wrong = {**evidence, 'quote': 'X' * len(evidence['quote'])}
    with pytest.raises(ValueError, match='原文不一致'):
        validate_object_evidence(wrong, source_doc_uuid=doc_uuid, source_site='clinicaltrials')


@pytest.mark.parametrize('invalid', ['missing_document', 'source_mismatch', 'unsupported_rule',
                                     'namespace_mismatch', 'raw_value_mismatch', 'quote_mismatch'])
def test_writer_rechecks_native_claim_against_stored_source(session, invalid):
    from test_native_extraction import trial_raw

    from graph.db.models import Document
    from graph.extraction.native_extraction import extract_native_entities
    raw = trial_raw(markdown=True)
    record = next(item for item in extract_native_entities(raw, 'clinicaltrials')['entities']
                  if item['entity_type'] == 'ClinicalTrial')
    target = EntityDict(entity_type='ClinicalTrial', canonical_data={'name': 'Existing trial'},
                        external_ids={'trial_id': record['entity_data']['trial_id']}, is_verified=False)
    session.add(target)
    session.flush()
    record['dict_id'] = target.id
    record['_disambig'] = {'status': 'auto_matched', 'auto_link': True, 'dict_id': target.id,
                          'match_step': 'step1_native_id', 'candidates': [{'dict_id': target.id, 'score': 1.0}]}
    doc_uuid = 'native-untrusted-' + uuid4().hex
    if invalid != 'missing_document':
        session.add(Document(uuid=doc_uuid, source_site='news' if invalid == 'source_mismatch' else 'clinicaltrials',
                             raw_content=raw.replace('NCT00000001', 'NCT99999999') if invalid == 'raw_value_mismatch' else raw))
        session.flush()
    if invalid == 'unsupported_rule':
        record['entity_data']['_native']['rule_version'] = 'self-signed-rule'
    if invalid == 'namespace_mismatch':
        record['entity_data']['_native']['identity_namespace'] = 'untrusted.NCT'
    if invalid == 'quote_mismatch':
        record['source_quote'] = 'NCT99999999'
    before = session.query(EntityDict).count()
    entity_id = repo.insert_extraction_result(doc_uuid, 'clinicaltrials', [record], [], session=session)[0]
    observed = session.get(ExtractedEntity, entity_id)
    assert observed.dict_id is None and observed.review_status == 'pending'
    assert observed.entity_data['_disambig']['auto_link'] is False
    assert observed.entity_data['_disambig']['status'] == 'ambiguous'
    assert session.query(EntityDict).count() == before
    proposals = session.query(EntityIdentityMergeProposal).filter_by(source_entity_id=entity_id).all()
    assert {row.target_dict_id for row in proposals} == {target.id}


@pytest.mark.parametrize('merge_existing', [False, True])
def test_pending_proposal_rejects_concurrently_changed_target(engine, session, principal, merge_existing):
    with Session(engine) as setup, setup.begin():
        target = dictionary(setup, name='Proposal target ' + uuid4().hex, external_ids={'lei': 'OLD'})
        previous = dictionary(setup) if merge_existing else None
        source = entity(setup, previous or target, document='proposal-stale-' + uuid4().hex)
        source.dict_id = previous.id if previous else None
        setup.flush()
        proposal_id = repo.create_identity_merge_proposal(source.id, target.id, session=setup)
        source_id, target_id, previous_id = source.id, target.id, previous.id if previous else None
    with Session(engine) as reviewer, reviewer.begin():
        cached_target = reviewer.get(EntityDict, target_id)
        assert cached_target.external_ids == {'lei': 'OLD'}
        with Session(engine) as writer, writer.begin():
            changed = writer.get(EntityDict, target_id)
            changed.canonical_data = {'name': 'Changed after proposal'}
            changed.external_ids = {'lei': 'NEW'}
        with pytest.raises(ValueError, match='target identity has changed'):
            repo.resolve_identity_merge_proposal(proposal_id, 'approved', '', session=reviewer, review_context=principal)
        assert reviewer.get(EntityIdentityMergeProposal, proposal_id).status == 'pending'
        assert reviewer.get(ExtractedEntity, source_id).dict_id == previous_id
        assert reviewer.query(EntityMergeLog).filter(EntityMergeLog.keeper_dict_id == target_id).count() == 0


def test_current_review_evidence_cannot_borrow_appended_unreviewed_version(session, principal, real_history):
    from graph.db.identity import get_current_review_evidence, record_evidence
    raw = 'Synthetic human reviewed identity and relation.'
    doc_uuid = 'review-binding-' + uuid4().hex
    proof = freeze_document(doc_uuid, raw)
    proof = {**proof, 'source_doc_uuid': doc_uuid, 'source_site': 'pubmed', 'quote': raw,
             'char_start': 0, 'char_end': len(raw), 'locator': {'field': 'raw_content'}}
    endpoints = [entity(session, dictionary(session), document=doc_uuid) for _ in range(2)]
    for observed in endpoints:
        observed.source_site = 'pubmed'
        repo.approve_entity_with_alias_writeback(observed.id, '', confirm_identity=True, basis='Checked fixed source',
                                                evidence=[proof], session=session, review_context=principal)
    relation = EntityRelation(source_doc_uuid=doc_uuid, source_site='pubmed', from_entity_id=endpoints[0].id,
                              to_entity_id=endpoints[1].id, relation_type='SPONSORS', review_status='pending')
    session.add(relation)
    session.flush()
    repo.approve_relation(relation.id, '', evidence=[proof], session=session, review_context=principal)
    for kind, objects in [('entity', endpoints), ('relation', [relation])]:
        checked = get_current_review_evidence(kind, [row.id for row in objects], session)
        assert all([item['version_id'] for item in checked[row.id]] == [proof['version_id']] for row in objects)
        for row in objects:
            extra = {**proof, 'version_id': 'f' * 64}
            record_evidence(session, kind, row, {'entity_type': 'Company', 'evidence': [extra]}, doc_uuid, 'pubmed')
        checked = get_current_review_evidence(kind, [row.id for row in objects], session)
        assert all([item['version_id'] for item in checked[row.id]] == [proof['version_id']] for row in objects)
    endpoints[0].dict_id = dictionary(session).id
    session.flush()
    assert get_current_review_evidence('entity', [endpoints[0].id], session)[endpoints[0].id] == []
    assert get_current_review_evidence('relation', [relation.id], session)[relation.id] == []


def test_review_lists_include_recorded_second_source_without_copying_mentions(session, monkeypatch):
    from api.routers import review
    from graph.db.identity import record_evidence
    first, second = 'first-list-' + uuid4().hex, 'second-list-' + uuid4().hex
    records = [{'entity_type': 'Company', 'entity_name': target.canonical_data['name'], 'dict_id': target.id,
                'source_text': 'Second source corroboration'} for target in [dictionary(session), dictionary(session)]]
    first_ids = repo.insert_extraction_result(first, 'pubmed', records, [], session=session)
    second_ids = repo.insert_extraction_result(second, 'clinicaltrials', records, [], session=session)
    assert first_ids == second_ids
    unrelated = entity(session, dictionary(session), document='unrelated-' + uuid4().hex)
    relation = EntityRelation(source_doc_uuid=first, source_site='pubmed', from_entity_id=first_ids[0],
                              to_entity_id=first_ids[1], relation_type='SPONSORS', review_status='pending')
    session.add(relation)
    session.flush()
    record_evidence(session, 'relation', relation, {'relation_type': relation.relation_type,
        'source_text': 'Recorded second source relation'}, second, 'clinicaltrials')
    @contextmanager
    def sessions():
        yield session
    monkeypatch.setattr(review, 'get_session', sessions)
    pages = [review.list_entities(page=page, page_size=1, entity_type='', source_site='',
                                 source_doc_uuid=second, review_status='pending') for page in (1, 2)]
    assert all(page.total == 2 and len(page.items) == 1 for page in pages)
    assert {item.id for page in pages for item in page.items} == set(first_ids)
    assert all(item.source_doc_uuid == first and item.current_review_evidence == [] for page in pages for item in page.items)
    assert all(any(proof['source_doc_uuid'] == second for proof in item.evidence) for page in pages for item in page.items)
    relations = review.list_relations(page=1, page_size=1, source_site='', source_doc_uuid=second, review_status='pending')
    assert relations.total == 1 and relations.items[0].id == relation.id
    assert relations.items[0].source_doc_uuid == first and unrelated.id not in first_ids


@pytest.mark.parametrize('entrypoint', ['proposal', 'approve', 'approve_confirm'])
def test_relink_one_mention_preserves_shared_dictionary_and_can_withdraw(session, principal, fixed_document, entrypoint):
    old = dictionary(session, external_ids={'lei': 'OLD-SHARED'})
    target = dictionary(session, external_ids={'lei': 'TARGET'})
    source, sibling = [entity(session, old, document='shared-' + uuid4().hex) for _ in range(2)]
    sibling.review_status = 'approved'
    sibling.identity_confirmed_by = principal.subject
    sibling.identity_confirmed_at = datetime.now(timezone.utc)
    sibling.identity_confirmation_basis = 'Separate correct mention'
    alias = EntityAlias(dict_id=old.id, entity_type='Company', name='shared-alias-' + uuid4().hex)
    session.add(alias)
    session.flush()
    old_canonical, target_canonical = dict(old.canonical_data), dict(target.canonical_data)
    if entrypoint == 'proposal':
        proposal_id = repo.create_identity_merge_proposal(source.id, target.id, session=session)
        repo.resolve_identity_merge_proposal(proposal_id, 'approved', '', session=session, review_context=principal)
        log_id = session.get(EntityIdentityMergeProposal, proposal_id).merge_log_id
    else:
        repo.approve_entity_with_alias_writeback(source.id, 'client-claimed-human', dict_id=target.id,
            confirm_identity=entrypoint == 'approve_confirm', basis='Explicit final identity checked',
            evidence=[{**fixed_document, 'source_doc_uuid': source.source_doc_uuid, 'source_site': source.source_site}] if entrypoint == 'approve_confirm' else None, session=session, review_context=principal)
        log_id = session.query(EntityMergeLog.id).filter_by(keeper_dict_id=target.id).scalar()
    assert source.dict_id == target.id
    assert source.identity_confirmed_by == (principal.subject if entrypoint == 'approve_confirm' else None)
    assert sibling.dict_id == old.id and sibling.identity_confirmed_by == principal.subject
    assert not old.is_deleted and alias.dict_id == old.id
    assert old.canonical_data == old_canonical and target.canonical_data == target_canonical
    log = session.get(EntityMergeLog, log_id)
    assert log.merge_type == 'identity_link' and log.absorbed_dict_ids == []
    repo.rollback_merge(log_id, 'Withdraw this mention link', session=session, review_context=principal)
    assert source.dict_id == old.id and source.review_status == 'pending'
    assert sibling.dict_id == old.id and alias.dict_id == old.id and not old.is_deleted


@pytest.mark.parametrize('entrypoint', ['single', 'batch', 'process', 'legacy', 'explicit_merge', 'sync'])
def test_canonical_changes_invalidate_confirmation_and_block_relation(session, principal, fixed_document, monkeypatch, entrypoint):
    targets = [dictionary(session), dictionary(session)]
    targets[0].canonical_data = {**targets[0].canonical_data, 'country': 'OLD'}
    endpoints = [entity(session, target, entity_data={'country': 'OLD'} if index == 0 else {})
                 for index, target in enumerate(targets)]
    for endpoint in endpoints:
        repo.approve_entity_with_alias_writeback(endpoint.id, '', confirm_identity=True, basis='Checked old identity',
                                                evidence=[fixed_document], session=session, review_context=principal)
    relation = EntityRelation(source_doc_uuid='fixture-doc', source_site='fixture-site',
        from_entity_id=endpoints[0].id, to_entity_id=endpoints[1].id, relation_type='SPONSORS', review_status='pending')
    session.add(relation)
    session.flush()
    repo.approve_relation(relation.id, '', evidence=[fixed_document], session=session, review_context=principal)
    target = targets[0]
    if entrypoint in ('single', 'batch'):
        queue = EntityMergeQueue(source_entity_id=endpoints[0].id, target_dict_id=target.id, field_name='country',
                                 existing_value={'_val': 'OLD'}, new_value={'_val': 'NEW'}, resolution='pending')
        session.add(queue)
        session.flush()
        if entrypoint == 'single':
            repo.resolve_merge(queue.id, 'accept_new', 'claimed-human', session=session, review_context=principal)
        else:
            repo.batch_resolve_merges([{'merge_id': queue.id, 'resolution': 'accept_new'}], 'claimed-human',
                                      session=session, review_context=principal)
        assert queue.reviewed_by == principal.subject
    elif entrypoint == 'process':
        repo.process_entity_merge(session, target.id, endpoints[0].id, {'country': 'NEW'}, 'fda')
    elif entrypoint == 'legacy':
        @contextmanager
        def sessions():
            yield session
        monkeypatch.setattr('graph.db.session.get_session', sessions)
        repo.merge_canonical_data(target.id, {'country': 'NEW'}, 'fda')
    elif entrypoint == 'explicit_merge':
        extra = dictionary(session, external_ids={'registry': 'NEW'})
        extra.canonical_data = {'country': 'NEW'}
        repo.merge_entities(target.id, [extra.id], {'country': {'picked_dict_id': extra.id}},
                            session=session, review_context=principal)
    else:
        target.canonical_data = {**target.canonical_data, 'country': 'NEW'}
        repo._sync_canonical_to_extracted(session, target.id)
    assert not target.is_verified and target.verified_by is None and target.verified_at is None
    assert target.verification_basis is None
    assert endpoints[0].identity_confirmed_by is None and endpoints[0].identity_confirmed_at is None
    with pytest.raises(ValueError, match='Both relation endpoints'):
        repo.approve_relation(relation.id, '', evidence=[fixed_document], session=session, review_context=principal)


def test_same_canonical_value_keeps_confirmation_and_explicit_confirm_signs_final_value(session, principal, fixed_document):
    target = dictionary(session)
    target.canonical_data = {**target.canonical_data, 'country': 'OLD'}
    observed = entity(session, target, entity_data={'country': 'OLD'})
    repo.approve_entity_with_alias_writeback(observed.id, '', confirm_identity=True, basis='Checked source',
                                            evidence=[fixed_document], session=session, review_context=principal)
    old_time = observed.identity_confirmed_at
    repo.process_entity_merge(session, target.id, observed.id, {'country': 'OLD'}, 'fda')
    assert target.is_verified and observed.identity_confirmed_at == old_time
    observed.entity_data = {'country': 'NEW'}
    observed.source_site = 'fda'
    proof = {**fixed_document, 'source_site': 'fda'}
    repo.approve_entity_with_alias_writeback(observed.id, '', confirm_identity=True, basis='Checked final revised identity',
                                            evidence=[proof], session=session, review_context=principal)
    assert target.canonical_data['country'] == 'NEW'
    assert target.is_verified and target.verified_by == principal.subject
    assert observed.identity_confirmed_by == principal.subject and observed.identity_confirmed_at >= old_time


def test_link_rollback_rejects_changed_original_dictionary_identity(session, principal, fixed_document):
    from graph.db.identity import set_dictionary_identity_data
    original, target = dictionary(session), dictionary(session)
    observed = entity(session, original)
    repo.approve_entity_with_alias_writeback(observed.id, '', confirm_identity=True, basis='Original identity checked',
                                            evidence=[fixed_document], session=session, review_context=principal)
    proposal_id = repo.create_identity_merge_proposal(observed.id, target.id, session=session)
    repo.resolve_identity_merge_proposal(proposal_id, 'approved', '', session=session, review_context=principal)
    log_id = session.get(EntityIdentityMergeProposal, proposal_id).merge_log_id
    set_dictionary_identity_data(session, original, canonical_data={'name': 'Different identity after relink'})
    new_mention = entity(session, original)
    repo.approve_entity_with_alias_writeback(new_mention.id, '', confirm_identity=True, basis='Different identity newly checked',
                                            evidence=[fixed_document], session=session, review_context=principal)
    with pytest.raises(ValueError, match='dictionary identity dependencies changed'):
        repo.rollback_merge(log_id, 'Cannot restore stale original confirmation', session=session, review_context=principal)
    assert observed.dict_id == target.id and observed.identity_confirmed_by is None
    assert not session.get(EntityMergeLog, log_id).is_rolled_back


def test_link_rollback_refreshes_cached_entity_after_independent_writer_commit(engine, session, principal):
    with Session(engine) as setup, setup.begin():
        old, target = dictionary(setup), dictionary(setup)
        source = entity(setup, old)
        proposal_id = repo.create_identity_merge_proposal(source.id, target.id, session=setup)
        repo.resolve_identity_merge_proposal(proposal_id, 'approved', '', session=setup, review_context=principal)
        log_id = setup.get(EntityIdentityMergeProposal, proposal_id).merge_log_id
        source_id, target_id = source.id, target.id
    with Session(engine) as reviewer, reviewer.begin():
        cached = reviewer.get(ExtractedEntity, source_id)
        assert cached.entity_data == {}
        with Session(engine) as writer, writer.begin():
            writer.get(ExtractedEntity, source_id).entity_data = {'name_en': 'Independent committed correction'}
        with pytest.raises(ValueError, match='subsequent changes'):
            repo.rollback_merge(log_id, 'Stale reviewer must not overwrite current binding', session=reviewer, review_context=principal)
        assert cached.dict_id == target_id and cached.entity_data == {'name_en': 'Independent committed correction'}
        assert not reviewer.get(EntityMergeLog, log_id).is_rolled_back


@pytest.mark.parametrize('change', ['approved_hit', 'pending_then_approve', 'dictionary_revoke'])
def test_recorded_second_source_enqueues_existing_graph_publication(engine, change):
    from graph.db.identity import set_dictionary_identity_data
    from graph.db.models import Document
    schema = 'opus_identity_publication_' + uuid4().hex[:10]
    os.environ['DATAFABRIC_IDENTITY_PUBLICATION_SCHEMA_RESULT'] = schema
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE SCHEMA ' + schema)
    scoped = create_engine(engine.url, connect_args={'options': '-csearch_path=' + schema + ',public'})
    with scoped.begin() as conn:
        for migration in sorted((Path(__file__).resolve().parents[1] / 'store' / 'migrations' / 'versions').glob('V*.sql')):
            if int(migration.name[1:4]) <= 19 or migration.name[:5] in ('V026_', 'V033_', 'V034_', 'V035_'):
                conn.exec_driver_sql(migration.read_text())
    first, second = 'publication-first-' + uuid4().hex, 'publication-second-' + uuid4().hex
    with Session(scoped) as seed, seed.begin():
        seed.add_all([Document(uuid=doc, source_site='pubmed', raw_content='Synthetic source') for doc in (first, second)])
        target = dictionary(seed)
        target.is_verified = True
        target.verified_by = 'fixture-human'
        target.verified_at = datetime.now(timezone.utc)
        target.verification_basis = 'Explicit synthetic initial confirmation'
        record = {'entity_type': 'Company', 'entity_name': target.canonical_data['name'], 'dict_id': target.id,
                  'source_text': 'Synthetic source', 'entity_data': {}}
        source_id = repo.insert_extraction_result(first, 'pubmed', [record], [], session=seed)[0]
        target_id = target.id
        if change != 'pending_then_approve':
            seed.get(ExtractedEntity, source_id).review_status = 'approved'
        if change != 'approved_hit':
            assert repo.insert_extraction_result(second, 'pubmed', [record], [], session=seed)[0] == source_id
        seed.flush()
        seed.execute(text("UPDATE publication_outbox SET state='published',completed_at=clock_timestamp()"))
    with Session(scoped) as action, action.begin():
        if change == 'approved_hit':
            assert repo.insert_extraction_result(second, 'pubmed', [record], [], session=action)[0] == source_id
        elif change == 'pending_then_approve':
            action.get(ExtractedEntity, source_id).review_status = 'approved'
            action.flush()
        else:
            current = action.get(EntityDict, target_id)
            set_dictionary_identity_data(action, current, canonical_data={**current.canonical_data, 'country': 'NEW'})
        assert action.execute(text("SELECT count(*) FROM publication_outbox WHERE kind='graph' AND source_doc_uuid=:doc AND state='pending'"), {'doc': second}).scalar_one() == 1
    if change == 'approved_hit':
        with Session(scoped) as replay, replay.begin():
            before = replay.execute(text("SELECT count(*) FROM publication_outbox WHERE kind='graph' AND source_doc_uuid=:doc"), {'doc': second}).scalar_one()
            assert repo.insert_extraction_result(second, 'pubmed', [record], [], session=replay)[0] == source_id
            assert replay.execute(text("SELECT count(*) FROM publication_outbox WHERE kind='graph' AND source_doc_uuid=:doc"), {'doc': second}).scalar_one() == before
    scoped.dispose()


@pytest.mark.parametrize('reuse_existing', [False, True])
def test_global_merge_rollback_blocks_new_native_observation_dependencies(session, principal, reuse_existing):
    from graph.db.models import Document
    from graph.extraction.native_extraction import extract_native_entities
    nct = 'NCT' + str(int(uuid4().hex[:8], 16) % 100000000).zfill(8)
    keeper = EntityDict(entity_type='ClinicalTrial', canonical_data={'name': 'Unidentified older trial'}, external_ids={}, is_verified=False)
    absorbed = EntityDict(entity_type='ClinicalTrial', canonical_data={'name': nct, 'trial_id': nct},
                          external_ids={'trial_id': nct}, is_verified=False)
    session.add_all([keeper, absorbed])
    session.flush()
    if reuse_existing:
        session.add(ExtractedEntity(source_doc_uuid='older-' + uuid4().hex, source_site='clinicaltrials',
            entity_type='ClinicalTrial', entity_name='Unidentified older trial', dict_id=keeper.id,
            entity_data={}, review_status='pending'))
        session.flush()
    merged = repo.merge_entities(keeper.id, [absorbed.id], {}, session=session, review_context=principal)
    before = session.query(ExtractedEntity).count()
    raw = native_trial_original(nct)
    doc_uuid = 'post-merge-native-' + uuid4().hex
    session.add(Document(uuid=doc_uuid, source_site='clinicaltrials', raw_content=raw))
    session.flush()
    projected = extract_native_entities(raw, 'clinicaltrials')
    trial = next(item for item in projected['entities'] if item['entity_type'] == 'ClinicalTrial')
    observed_id = repo.insert_extraction_result(doc_uuid, 'clinicaltrials', [trial], [], session=session)[0]
    assert session.query(ExtractedEntity).count() == before + (0 if reuse_existing else 1)
    with pytest.raises(ValueError, match='dependent|subsequent changes'):
        repo.rollback_merge(merged['merge_log_id'], 'New native observations need explicit review', session=session, review_context=principal)
    assert session.get(ExtractedEntity, observed_id).dict_id == keeper.id and absorbed.is_deleted
    assert repo.lookup_external_id_candidates('ClinicalTrial', 'trial_id', nct, session=session)[0]['dict_id'] == keeper.id


def test_single_link_rollback_blocks_added_native_evidence_without_row_change(session, principal):
    from graph.db.models import Document
    from graph.extraction.native_extraction import extract_native_entities
    nct = 'NCT' + str(int(uuid4().hex[:8], 16) % 100000000).zfill(8)
    old = EntityDict(entity_type='ClinicalTrial', canonical_data={'name': 'Old unidentified identity'}, external_ids={}, is_verified=False)
    target = EntityDict(entity_type='ClinicalTrial', canonical_data={'name': nct, 'trial_id': nct}, external_ids={'trial_id': nct}, is_verified=False)
    session.add_all([old, target])
    session.flush()
    source = ExtractedEntity(source_doc_uuid='older-' + uuid4().hex, source_site='clinicaltrials', entity_type='ClinicalTrial',
                             entity_name='Old unidentified identity', dict_id=old.id, entity_data={}, review_status='pending')
    session.add(source)
    session.flush()
    proposal_id = repo.create_identity_merge_proposal(source.id, target.id, session=session)
    repo.resolve_identity_merge_proposal(proposal_id, 'approved', '', session=session, review_context=principal)
    log_id = session.get(EntityIdentityMergeProposal, proposal_id).merge_log_id
    raw = native_trial_original(nct)
    doc_uuid = 'post-link-native-' + uuid4().hex
    session.add(Document(uuid=doc_uuid, source_site='clinicaltrials', raw_content=raw))
    session.flush()
    trial = next(item for item in extract_native_entities(raw, 'clinicaltrials')['entities'] if item['entity_type'] == 'ClinicalTrial')
    assert repo.insert_extraction_result(doc_uuid, 'clinicaltrials', [trial], [], session=session)[0] == source.id
    with pytest.raises(ValueError, match='dependencies changed'):
        repo.rollback_merge(log_id, 'Cannot attach new target evidence to original identity', session=session, review_context=principal)
    assert source.dict_id == target.id and not session.get(EntityMergeLog, log_id).is_rolled_back


def test_pending_merges_auto_resolve_stale_rows_and_group_the_open_conflicts(engine, session, monkeypatch):
    @contextmanager
    def committing_session():  # like graph.db.session.get_session: the function itself commits mid-way
        with Session(engine) as active:
            yield active
            active.commit()
    monkeypatch.setattr('graph.db.session.get_session', committing_session)
    tag = uuid4().hex[:8]
    with Session(engine) as work, work.begin():
        live = EntityDict(entity_type='Company', is_verified=False, canonical_data={
            'name': 'D' + tag, 'empty_cur': '', 'same': 'v', 'keep': 'k', 'conf': 'old'})
        gone = EntityDict(entity_type='Company', is_verified=False, canonical_data={'name': 'G' + tag}, is_deleted=True)
        work.add_all([live, gone])
        work.flush()
        live_id, gone_id = live.id, gone.id

        def source(doc, data=None, **extra):
            row = ExtractedEntity(source_doc_uuid=doc, source_site='site-' + doc, entity_type='Company', entity_name='n',
                                  dict_id=live_id, review_status='pending', entity_data=data or {}, **extra)
            work.add(row)
            work.flush()
            return row

        main = source('dS' + tag, {'conf': 'new'})
        deleted_source = source('dX' + tag, is_deleted=True)
        source('d1' + tag, {'conf': 'old'})
        source('d1' + tag, {'conf': 'old'})
        source('d2' + tag, {'conf': 'new'})

        def queue(target, src, field, existing, new):
            row = EntityMergeQueue(source_entity_id=src.id, target_dict_id=target, field_name=field,
                                   existing_value=existing, new_value=new, source_site='q-site',
                                   existing_source_doc_uuid='ex-doc', resolution='pending')
            work.add(row)
            work.flush()
            return row.id

        ids = {
            'dict_deleted': queue(gone_id, main, 'f', {'_val': 'a'}, {'_val': 'b'}),
            'source_deleted': queue(live_id, deleted_source, 'conf', {'_val': 'old'}, {'_val': 'new'}),
            'both_empty': queue(live_id, main, 'nothing', {'_val': ''}, {'_val': ''}),
            'accept_new': queue(live_id, main, 'empty_cur', {'_val': ''}, {'_val': 'N'}),
            'keep_existing': queue(live_id, main, 'keep', {'_val': 'k'}, {'_val': None}),
            'already_merged': queue(live_id, main, 'same', {'_val': 'x'}, {'_val': 'v'}),
            'conflict': queue(live_id, main, 'conf', {'_val': 'stale'}, {'_val': 'new'}),
        }
        main_doc = main.source_doc_uuid
        main_id = main.id

    result = repo.get_pending_merges(limit=100000)
    mine = [group for group in result['groups'] if group['dict_id'] == live_id]
    assert len(mine) == 1 and result['total_groups'] == len(result['groups'])
    assert [group['dict_id'] for group in result['groups']] == sorted(
        (group['dict_id'] for group in result['groups']), reverse=True)
    group = mine[0]
    assert group['entity_type'] == 'Company' and group['entity_name'] == 'D' + tag and group['canonical_source'] == ''
    assert len(group['conflicts']) == 1
    conflict = group['conflicts'][0]
    sources = {key: sorted((item['source_doc_uuid'], item['source_site']) for item in conflict.pop(key))
               for key in ('existing_sources', 'new_sources')}
    assert conflict == {'merge_id': ids['conflict'], 'field_name': 'conf', 'existing_value': 'old', 'new_value': 'new',
                        'source_entity_id': main_id, 'source_site': 'q-site', 'source_doc_uuid': main_doc,
                        'existing_source_doc_uuid': 'ex-doc'}
    assert sources['existing_sources'] == [('d1' + tag, 'site-d1' + tag)]
    assert sources['new_sources'] == [('d2' + tag, 'site-d2' + tag), (main_doc, 'site-' + main_doc)]
    with Session(engine) as check:
        state = {name: check.get(EntityMergeQueue, row_id) for name, row_id in ids.items()}
        assert {name: row.resolution for name, row in state.items()} == {
            'dict_deleted': 'resolved_dict_deleted', 'source_deleted': 'resolved_source_deleted',
            'both_empty': 'resolved_both_empty', 'accept_new': 'accept_new', 'keep_existing': 'keep_existing',
            'already_merged': 'resolved_already_merged', 'conflict': 'pending'}
        assert all(state[name].reviewed_at is not None for name in state if name != 'conflict')
        assert state['conflict'].existing_value == {'_val': 'old'}
        assert check.get(EntityDict, live_id).canonical_data['empty_cur'] == {'_val': 'N'}
        logged = check.execute(text(
            "SELECT target_id, details->>'reason' FROM operation_log WHERE target_table='entity_merge_queue' "
            "AND business_action='auto_resolve' AND target_id = ANY(:ids) ORDER BY target_id"),
            {'ids': [ids['dict_deleted'], ids['source_deleted'], ids['already_merged'], ids['both_empty']]}).all()
        assert [tuple(row) for row in logged] == sorted([
            (ids['dict_deleted'], 'dict_deleted'), (ids['source_deleted'], 'source_deleted'),
            (ids['already_merged'], 'already_merged')])
