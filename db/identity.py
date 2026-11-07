"""Small persistence helpers for evidence and reversible identity decisions.

These helpers never infer human confirmation from a legacy boolean or a machine score.
"""
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import DateTime, false, or_
from sqlalchemy.dialects.postgresql import insert

from biz_rules.timestamps import parse_timestamp
from db.models import (
    EntityAlias,
    EntityDict,
    EntityIdentityMergeProposal,
    EntityMergeLog,
    EntityMergeQueue,
    EntityPrimeKGMappingProposal,
    EntityRelation,
    EntitySourceEvidence,
    ExtractedEntity,
    ReviewLog,
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


@contextmanager
def use_session(session=None):
    if session is not None:
        yield session
    else:
        from db.session import get_session
        with get_session() as active:
            if active is None:
                raise RuntimeError('Database is not configured')
            yield active


def require_human_reviewer(reviewer):
    value = (reviewer or '').strip()
    if not value or re.match(r'^(auto|system|agent|machine|bot|rule|llm)(?:$|[\s:_./-])', value, re.I):
        raise ValueError('An explicit human reviewer is required')
    return value


def auditable_identity(entity):
    if entity is None or not entity.is_verified or entity.is_deleted:
        return False
    try:
        require_human_reviewer(entity.verified_by)
    except ValueError:
        return False
    return bool(entity.verified_at and (entity.verification_basis or '').strip())


def auditable_entity_identity(entity):
    if entity is None or entity.is_deleted or entity.review_status != 'approved' or not entity.dict_id:
        return False
    try:
        require_human_reviewer(entity.identity_confirmed_by)
    except ValueError:
        return False
    return bool(entity.identity_confirmed_at and (entity.identity_confirmation_basis or '').strip())


def clear_entity_confirmation(entity):
    entity.identity_confirmed_by = None
    entity.identity_confirmed_at = None
    entity.identity_confirmation_basis = None



_UNCHANGED = object()


def invalidate_dictionary_confirmation(session, dictionary):
    """A changed dictionary cannot retain a human signature over its previous values."""
    dictionary.is_verified = False
    dictionary.verified_by = None
    dictionary.verified_at = None
    dictionary.verification_basis = None
    for mention in session.query(ExtractedEntity).filter(ExtractedEntity.dict_id == dictionary.id).order_by(ExtractedEntity.id).populate_existing().with_for_update().all():
        clear_entity_confirmation(mention)


def set_dictionary_identity_data(session, dictionary, *, canonical_data=_UNCHANGED, external_ids=_UNCHANGED):
    # Fresh rows also prevent a cached verification signature being retained after a concurrent edit.
    current = session.query(EntityDict).filter(EntityDict.id == dictionary.id).populate_existing().with_for_update().first()
    if current is None:
        raise ValueError('Dictionary does not exist')
    changed = ((canonical_data is not _UNCHANGED and canonical_data != current.canonical_data)
               or (external_ids is not _UNCHANGED and external_ids != current.external_ids))
    if not changed:
        return False
    invalidate_dictionary_confirmation(session, current)
    if canonical_data is not _UNCHANGED:
        current.canonical_data = canonical_data
    if external_ids is not _UNCHANGED:
        current.external_ids = external_ids
    return True

def parse_time(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = parse_timestamp(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Object times must include a timezone')
    return result


# Only IDs with an explicit namespace or an unambiguous source field are comparable.
_ID_FIELDS = {'lei': 'lei', 'cas_number': 'cas', 'trial_id': 'clinicaltrials.nct',
              'nct_number': 'clinicaltrials.nct', 'approval_number': 'nmpa.approval',
              'pubchem_cid': 'pubchem', 'unii': 'unii', 'drugbank_id': 'drugbank',
              'application_number': 'fda.application_number', 'recall_number': 'fda.recall_number'}


def namespaced_ids(data):
    data = data or {}
    result = {}
    for key, value in (data.get('external_ids') or {}).items():
        if value not in (None, '', [], {}) and key.lower() not in ('id', 'stock_code', 'gene_symbol', 'event_id', 'name'):
            result[_ID_FIELDS.get(key.lower(), key.lower())] = str(value).strip().casefold()
    for key, namespace in _ID_FIELDS.items():
        value = data.get(key)
        if value not in (None, '', [], {}):
            result[namespace] = str(value).strip().casefold()
    native = data.get('_native') or {}
    if native.get('identity_namespace') and native.get('identity_value'):
        result[str(native['identity_namespace']).casefold()] = str(native['identity_value']).strip().casefold()
    stock = data.get('stock_code')
    exchange = data.get('exchange') or data.get('stock_exchange')
    if stock and exchange:
        result['stock.' + str(exchange).strip().casefold()] = str(stock).strip().casefold()
    identifier = str(data.get('id') or '').strip()
    if re.match(r'^[A-Za-z][A-Za-z0-9_.-]+:[^\s:]+$', identifier):
        namespace, native = identifier.split(':', 1)
        result[namespace.casefold()] = native.casefold()
    return result


def compatible_identity(left, right):
    a, b = namespaced_ids(left), namespaced_ids(right)
    shared = a.keys() & b.keys()
    if a and b and not shared:
        return False
    return all(a[key] == b[key] for key in shared)


def batch_identity_keys(record):
    data = record.get('entity_data') or {}
    kind = record.get('entity_type', '')
    keys = {(kind, 'id', namespace, value) for namespace, value in namespaced_ids(data).items()
            if not (kind == 'Drug' and namespace == 'cas')}
    for value in (record.get('entity_name'), data.get('name'), data.get('name_en')):
        if value and str(value).strip():
            keys.add((kind, 'name', str(value).strip().casefold()))
    return keys


def disambiguation(record):
    return record.get('_disambig') or (record.get('entity_data') or {}).get('_disambig') or {}


def has_candidates(record):
    info = disambiguation(record)
    if info.get('status') in ('candidate_matched', 'ambiguous'):
        return True
    if info.get('candidates') or info.get('dict_id'):
        return not info.get('auto_link', False)
    return bool(info and info.get('status') != 'new_entity' and not info.get('auto_link', False))


def authoritative_native_identity(record, source_site):
    data = record.get('entity_data') or {}
    native = data.get('_native') or {}
    if native.get('rule_version') != 'native-fields.20261031.v1':
        return None
    rules = {
        'ClinicalTrial': ('clinicaltrials.NCT', 'trial_id', r'NCT[0-9]{8}', {'clinicaltrials'}),
        'RegulatoryApplication': ('fda.application_number', 'application_number', r'(?:NDA|ANDA|BLA)[0-9]{6}', {'fda_crl', 'fda_api_drug_label'}),
        'RecallProduct': ('fda.recall_number', 'recall_number', r'D-[0-9]+-[0-9]{4}', {'fda_api_enforcement'}),
    }
    rule = rules.get(record.get('entity_type'))
    if not rule:
        return None
    namespace, field, pattern, sources = rule
    value = data.get(field)
    if (source_site not in sources or native.get('identity_namespace') != namespace or
            native.get('identity_value') != value or not isinstance(value, str) or not re.fullmatch(pattern, value)):
        return None
    return namespace, field, value



def native_identity_claim(record):
    native = (record.get('entity_data') or {}).get('_native') or {}
    return bool(native.get('identity_namespace') or native.get('identity_value')
                or disambiguation(record).get('match_step') == 'step1_native_id')


def verified_native_identity(record, source_site, projected):
    """Check serialized native claims against a fresh projection of the stored original."""
    identity = authoritative_native_identity(record, source_site)
    if identity is None:
        return None
    keys = ('source_quote', 'source_char_start', 'source_char_end')
    locator_keys = ('field', 'offset_unit', 'json_pointer')
    for expected in projected:
        if (expected.get('entity_type') != record.get('entity_type')
                or authoritative_native_identity(expected, source_site) != identity):
            continue
        if (all(record.get(key) == expected.get(key) for key in keys)
                and all((record.get('locator') or {}).get(key) == (expected.get('locator') or {}).get(key)
                        for key in locator_keys)
                and ((record.get('entity_data') or {}).get('_native') or {}).get('origin_raw_sha256')
                    == expected['entity_data']['_native']['origin_raw_sha256']):
            return identity
    return None

def evidence_values(record, source_doc_uuid, source_site):
    info = disambiguation(record)
    native = ((record.get('entity_data') or record.get('relation_props') or {}).get('_native') or {})
    quote = record.get('source_quote', record.get('quote', record.get('source_text', ''))) or ''
    result = {
        'source_doc_uuid': record.get('source_doc_uuid') or source_doc_uuid,
        'source_site': record.get('source_site') or source_site,
        'version_id': record.get('source_version_id') or record.get('version_id') or None,
        'quote': quote,
        'char_start': record.get('source_char_start', record.get('char_start')),
        'char_end': record.get('source_char_end', record.get('char_end')),
        'match_step': record.get('match_step') or info.get('match_step') or ('native_mapping' if native.get('rule_version') else ''),
        'first_seen_at': parse_time(record.get('first_seen_at')),
        'valid_from': parse_time(record.get('valid_from')),
        'valid_to': parse_time(record.get('valid_to')),
    }
    if result['valid_from'] and result['valid_to'] and result['valid_from'] > result['valid_to']:
        raise ValueError('valid_to precedes valid_from')
    claim = {k: record.get(k) for k in ('entity_type', 'entity_name', 'relation_type', 'from_entity_id', 'to_entity_id') if record.get(k) is not None}
    if record.get('entity_type'):
        claim['external_identity'] = namespaced_ids(record.get('entity_data') or {})
    result['evidence_key'] = digest({k: result[k] for k in ('source_doc_uuid', 'source_site', 'version_id', 'quote', 'char_start', 'char_end')} | {'claim': claim})
    result['details'] = {k: record[k] for k in ('source_id', 'source_record_id', 'raw_sha256', 'is_demo', 'locator', 'rule_version', 'model_version') if k in record}
    if info:
        result['details']['disambiguation'] = info
    if native:
        result['details']['native'] = {key: value for key, value in native.items() if key != 'field_evidence'}
        result['details'].setdefault('rule_version', native.get('rule_version'))
    return result


def usable_evidence(evidence):
    start, end, quote = evidence.get('char_start'), evidence.get('char_end'), evidence.get('quote')
    return bool(re.fullmatch(r'[0-9a-fA-F]{64}', evidence.get('version_id') or '') and
                isinstance(start, int) and not isinstance(start, bool) and start >= 0 and
                isinstance(end, int) and not isinstance(end, bool) and end > start and
                quote and end - start == len(quote))


def row_evidence_columns(record, source_doc_uuid, source_site):
    values = evidence_values(record, source_doc_uuid, source_site)
    return {'source_version_id': values['version_id'], 'source_quote': values['quote'] or None,
            'source_char_start': values['char_start'], 'source_char_end': values['char_end'],
            **{k: values[k] for k in ('first_seen_at', 'valid_from', 'valid_to')}}


def record_evidence(session, object_type, row, record, source_doc_uuid, source_site):
    if object_type == 'entity':
        expected_dict_id = row.dict_id
        if expected_dict_id:
            session.query(EntityDict).filter(EntityDict.id == expected_dict_id).with_for_update().first()
        current = session.query(ExtractedEntity).filter(ExtractedEntity.id == row.id).populate_existing().with_for_update().first()
        if current is None or current.dict_id != expected_dict_id:
            raise ValueError('Entity identity changed before source evidence persistence')
    observations = record.get('evidence') or [record]
    first_key = None
    for observation in observations:
        merged = {**record, **observation}
        values = evidence_values(merged, source_doc_uuid, source_site)
        first_key = first_key or values['evidence_key']
        session.execute(insert(EntitySourceEvidence).values(
            object_type=object_type, object_id=row.id, **values).on_conflict_do_nothing(
                constraint='uq_entity_source_evidence'))
    return first_key


def get_objects_evidence(object_type, object_ids, session=None):
    identities = set(object_ids)
    if not identities:
        return {}
    with use_session(session) as active:
        rows = active.query(EntitySourceEvidence).filter(
            EntitySourceEvidence.object_type == object_type,
            EntitySourceEvidence.object_id.in_(identities)).order_by(EntitySourceEvidence.id).all()
        result = {identity: [] for identity in identities}
        for row in rows:
            result[row.object_id].append({**(row.details or {}), **{key: getattr(row, key) for key in (
                'id', 'source_doc_uuid', 'source_site', 'version_id', 'quote', 'char_start', 'char_end',
                'match_step', 'first_seen_at', 'valid_from', 'valid_to', 'evidence_key')}})
        # Preserve immutable observations. Canonical History locators discovered
        # during explicit validation live in the append-only review audit.
        audit_rows = active.query(ReviewLog).filter(
            ReviewLog.object_type == object_type, ReviewLog.object_id.in_(identities),
            ReviewLog.action.in_(['confirm_identity'] if object_type == 'entity' else ['approve']),
            ReviewLog.is_deleted == false()).order_by(ReviewLog.id).all()
        material_fields = ('source_doc_uuid', 'source_site', 'version_id', 'quote', 'char_start', 'char_end')
        normalized = {}
        for audit in audit_rows:
            for proof in (audit.after_data or {}).get('evidence', []):
                material = tuple(proof.get(key) for key in material_fields)
                normalized[(audit.object_id, material)] = proof
        for identity, proofs in result.items():
            for proof in proofs:
                checked = normalized.get((identity, tuple(proof.get(key) for key in material_fields)))
                if checked:
                    for key in ('source_id', 'source_record_id', 'raw_sha256', 'is_demo', 'locator'):
                        if key in checked:
                            proof[key] = checked[key]
        return result


def get_object_evidence(object_type, object_id, session=None):
    return get_objects_evidence(object_type, [object_id], session=session).get(object_id, [])



def review_object_binding(row):
    fields = ('source_doc_uuid', 'source_site', 'entity_type', 'entity_name', 'entity_data', 'dict_id') if isinstance(row, ExtractedEntity) else (
        'source_doc_uuid', 'source_site', 'from_entity_id', 'to_entity_id', 'relation_type', 'relation_props')
    return digest({key: getattr(row, key) for key in fields})


def review_endpoint_binding(entity):
    return {'dict_id': entity.dict_id, 'identity_confirmed_by': entity.identity_confirmed_by,
            'identity_confirmed_at': entity.identity_confirmed_at.isoformat() if entity.identity_confirmed_at else None,
            'identity_confirmation_basis': entity.identity_confirmation_basis}


def _same_instant(left, right):
    try:
        return left is not None and right is not None and parse_time(left) == parse_time(right)
    except (ValueError, TypeError):
        return False


def get_current_review_evidence(object_type, object_ids, session=None):
    """Only the validated material of the current trusted human decision grants publication eligibility."""
    if object_type not in ('entity', 'relation'):
        raise ValueError('Unsupported review evidence object type')
    identities = set(object_ids)
    if not identities:
        return {}
    result = {identity: [] for identity in identities}
    model = ExtractedEntity if object_type == 'entity' else EntityRelation
    with use_session(session) as active:
        objects = {row.id: row for row in active.query(model).filter(model.id.in_(identities)).all()}
        endpoints = {}
        if object_type == 'relation':
            endpoint_ids = {value for row in objects.values() for value in (row.from_entity_id, row.to_entity_id)}
            endpoints = {row.id: row for row in active.query(ExtractedEntity).filter(ExtractedEntity.id.in_(endpoint_ids)).all()} if endpoint_ids else {}
        audits = active.query(ReviewLog).filter(ReviewLog.object_type == object_type,
            ReviewLog.object_id.in_(identities), ReviewLog.is_deleted == false(),
            ReviewLog.action == ('confirm_identity' if object_type == 'entity' else 'approve')).order_by(ReviewLog.id.desc()).all()
        for audit in audits:
            row = objects.get(audit.object_id)
            after = audit.after_data or {}
            if row is None or row.is_deleted or row.review_status != 'approved' or result[row.id]:
                continue
            if after.get('object_binding_hash') != review_object_binding(row):
                continue
            if object_type == 'entity':
                if (not auditable_entity_identity(row)
                        or after.get('review_audit_schema') != 'trusted_human_identity_confirmation.v1'
                        or after.get('dict_id') != row.dict_id or audit.reviewer != row.identity_confirmed_by
                        or after.get('identity_confirmed_by') != row.identity_confirmed_by
                        or not _same_instant(after.get('identity_confirmed_at'), row.identity_confirmed_at)
                        or after.get('basis') != row.identity_confirmation_basis):
                    continue
            else:
                try:
                    require_human_reviewer(row.reviewed_by)
                except ValueError:
                    continue
                if (after.get('review_audit_schema') != 'trusted_human_relation_approval.v1'
                        or audit.reviewer != row.reviewed_by or after.get('reviewed_by') != row.reviewed_by
                        or not _same_instant(after.get('reviewed_at'), row.reviewed_at)):
                    continue
                valid_endpoints = True
                for endpoint_id in (row.from_entity_id, row.to_entity_id):
                    endpoint = endpoints.get(endpoint_id)
                    saved = (after.get('endpoint_identities') or {}).get(str(endpoint_id))
                    current = review_endpoint_binding(endpoint) if endpoint is not None else None
                    if (not auditable_entity_identity(endpoint) or not saved or not current
                            or saved.get('dict_id') != current['dict_id']
                            or saved.get('identity_confirmed_by') != current['identity_confirmed_by']
                            or saved.get('identity_confirmation_basis') != current['identity_confirmation_basis']
                            or not _same_instant(saved.get('identity_confirmed_at'), current['identity_confirmed_at'])):
                        valid_endpoints = False
                        break
                if not valid_endpoints:
                    continue
            proofs = after.get('evidence')
            if not isinstance(proofs, list) or not proofs:
                continue
            if all(isinstance(proof, dict) and usable_evidence(proof)
                   and proof.get('source_id') and proof.get('source_record_id')
                   and re.fullmatch(r'[0-9a-f]{64}', proof.get('raw_sha256') or '')
                   and type(proof.get('is_demo')) is bool for proof in proofs):
                result[row.id] = proofs
    return result

def validate_evidence(items, source_doc_uuid, source_site, allowed_sources=None):
    """Resolve each fixed History version before starting a source write transaction."""
    if not items:
        raise ValueError('Fixed version source evidence is required')
    from biz_rules.object_evidence import validate_object_evidence
    validated = []
    allowed = set(allowed_sources or {(source_doc_uuid, source_site)})
    for item in items:
        origin = (item.get('source_doc_uuid') or source_doc_uuid, item.get('source_site') or source_site)
        if origin not in allowed:
            raise ValueError('Evidence belongs to a source not recorded for this object')
        normalized = {**item, **evidence_values(item, source_doc_uuid, source_site)}
        # Keep the original History locator fields rather than only the compatibility columns.
        if not usable_evidence(normalized):
            raise ValueError('Evidence requires a fixed version, exact quote and Unicode character offsets')
        checked = validate_object_evidence(normalized, source_doc_uuid=origin[0], source_site=origin[1])
        validated.append(json.loads(json.dumps({**item, **checked}, default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value))))
    return validated


def persist_candidates(session, entity, record, observation_key):
    info = disambiguation(record)
    candidates = list(info.get('candidates') or [])
    selected = info.get('dict_id')
    if selected and not any(c.get('dict_id') == selected for c in candidates):
        candidates.append({'dict_id': selected, 'score': info.get('confidence', record.get('confidence_score', 0.0))})
    for candidate in candidates:
        target = candidate.get('dict_id')
        if not target:
            continue
        add_identity_proposal(session, entity.id, target,
                              agent_reason=info.get('reason', record.get('confidence_reason', '')),
                              agent_confidence=(candidate.get('similarity_score') if candidate.get('similarity_score') is not None
                                                else candidate.get('score') if candidate.get('score') is not None
                                                else info.get('confidence', record.get('confidence_score', 0.0))),
                              match_step=info.get('match_step', ''), rule_version=info.get('rule_version'),
                              model_version=info.get('model_version'),
                              candidate_payload={'candidate': candidate, 'disambiguation': info},
                              evidence_key=digest([entity.id, target, observation_key, info.get('match_step'),
                                                   info.get('rule_version'), info.get('model_version'), candidate]))


def add_identity_proposal(session, source_entity_id, target_dict_id, agent_reason='', agent_confidence=0.0,
                          agent_run_id='', match_step='', rule_version=None, model_version=None,
                          candidate_payload=None, evidence_key=None):
    source = session.get(ExtractedEntity, source_entity_id)
    target = session.get(EntityDict, target_dict_id)
    if source is None or source.is_deleted or target is None or target.is_deleted:
        raise ValueError('Proposal source or target does not exist')
    if source.entity_type != target.entity_type:
        raise ValueError('Proposal source and target have different entity types')
    payload = candidate_payload or {}
    key = evidence_key or digest([source_entity_id, target_dict_id, agent_run_id, match_step, rule_version, model_version, payload, agent_reason, agent_confidence])
    values = dict(source_entity_id=source_entity_id, target_dict_id=target_dict_id,
                  source_entity_snapshot={k: getattr(source, k) for k in ('entity_name', 'entity_type', 'entity_data', 'source_doc_uuid', 'source_site', 'source_text', 'dict_id')},
                  target_dict_snapshot={**snapshot(target), 'canonical_name': (target.canonical_data or {}).get('name', '')},
                  agent_reason=agent_reason, agent_confidence=float(agent_confidence or 0), agent_run_id=agent_run_id,
                  match_step=match_step, rule_version=rule_version, model_version=model_version,
                  candidate_payload=payload, evidence_key=key, status='pending')
    proposal_id = session.execute(insert(EntityIdentityMergeProposal).values(**values).on_conflict_do_nothing(
        index_elements=['evidence_key']).returning(EntityIdentityMergeProposal.id)).scalar()
    if proposal_id is None:
        proposal_id = session.query(EntityIdentityMergeProposal.id).filter(EntityIdentityMergeProposal.evidence_key == key).scalar()
    return proposal_id


def snapshot(row):
    return json.loads(json.dumps({c.name: (getattr(row, c.name).isoformat() if isinstance(getattr(row, c.name), datetime) else getattr(row, c.name))
            for c in row.__table__.columns if c.name not in ('created_at', 'updated_at')}))


def restore(row, values):
    for key, value in values.items():
        column = row.__table__.c[key]
        if value is not None and isinstance(column.type, DateTime):
            value = parse_time(value)
        setattr(row, key, value)


def _identity_observation_rows(session, entity_ids, dict_ids=()):
    return {
        'evidence': session.query(EntitySourceEvidence).filter(EntitySourceEvidence.object_type == 'entity',
            EntitySourceEvidence.object_id.in_(entity_ids)).order_by(EntitySourceEvidence.id).populate_existing().all(),
        'proposals': session.query(EntityIdentityMergeProposal).filter(or_(EntityIdentityMergeProposal.source_entity_id.in_(entity_ids),
            EntityIdentityMergeProposal.target_dict_id.in_(dict_ids))).order_by(EntityIdentityMergeProposal.id).populate_existing().all(),
    }


def _merge_rows(session, dict_ids):
    rows = {
        'dictionaries': session.query(EntityDict).filter(EntityDict.id.in_(dict_ids)).order_by(EntityDict.id).populate_existing().with_for_update().all(),
        'entities': session.query(ExtractedEntity).filter(ExtractedEntity.dict_id.in_(dict_ids)).order_by(ExtractedEntity.id).populate_existing().with_for_update().all(),
        'aliases': session.query(EntityAlias).filter(EntityAlias.dict_id.in_(dict_ids)).order_by(EntityAlias.id).populate_existing().with_for_update().all(),
        'queue': session.query(EntityMergeQueue).filter(EntityMergeQueue.target_dict_id.in_(dict_ids)).order_by(EntityMergeQueue.id).populate_existing().with_for_update().all(),
        'mappings': session.query(EntityPrimeKGMappingProposal).filter(EntityPrimeKGMappingProposal.dict_id.in_(dict_ids)).order_by(EntityPrimeKGMappingProposal.id).populate_existing().with_for_update().all(),
    }
    rows.update(_identity_observation_rows(session, [row.id for row in rows['entities']], dict_ids))
    return rows


def refresh_link_dependencies(session, log):
    after = dict(log.after_snapshot)
    ids = sorted(row['id'] for row in after['dictionary_dependencies'])
    dictionaries = session.query(EntityDict).filter(EntityDict.id.in_(ids)).order_by(EntityDict.id).populate_existing().with_for_update().all()
    entity_ids = [row['id'] for row in after['entities']]
    observations = _identity_observation_rows(session, entity_ids)
    after.update(dictionary_dependencies=[snapshot(row) for row in dictionaries],
                 evidence_dependencies=[snapshot(row) for row in observations['evidence']],
                 proposal_dependencies=[snapshot(row) for row in observations['proposals']])
    log.after_snapshot = after

def merge_dictionary_identities(session, keeper_dict_id, absorbed_dict_ids, field_decisions, reviewer, note='', review_context=None):
    from api.review_auth import require_trusted_review_context
    reviewer = require_trusted_review_context(review_context)
    absorbed_dict_ids = list(dict.fromkeys(absorbed_dict_ids))
    if not absorbed_dict_ids or keeper_dict_id in absorbed_dict_ids:
        raise ValueError('Choose distinct keeper and absorbed dictionaries')
    ids = sorted([keeper_dict_id, *absorbed_dict_ids])
    dictionaries = session.query(EntityDict).filter(EntityDict.id.in_(ids)).order_by(EntityDict.id).populate_existing().with_for_update().all()
    if len(dictionaries) != len(ids) or any(row.is_deleted for row in dictionaries):
        raise ValueError('A dictionary is missing or deleted')
    if len({row.entity_type for row in dictionaries}) != 1:
        raise ValueError('Cannot merge different entity types')
    rows = _merge_rows(session, ids)
    before = {key: [snapshot(row) for row in group] for key, group in rows.items()}
    keeper = next(row for row in dictionaries if row.id == keeper_dict_id)
    canonical = dict(keeper.canonical_data or {})
    external = dict(keeper.external_ids or {})
    for absorbed in dictionaries:
        if absorbed.id == keeper_dict_id:
            continue
        for key, value in (absorbed.canonical_data or {}).items():
            if key.startswith('_'):
                continue
            if key not in canonical or (field_decisions.get(key) or {}).get('picked_dict_id') == absorbed.id:
                canonical[key] = value
        for key, value in (absorbed.external_ids or {}).items():
            external.setdefault(key, value)
        absorbed.is_deleted = True
    set_dictionary_identity_data(session, keeper, canonical_data=canonical, external_ids=external)
    keeper.canonical_updated_at = datetime.now(timezone.utc)
    migrated_entities = migrated_aliases = 0
    for entity in rows['entities']:
        if entity.dict_id in absorbed_dict_ids:
            entity.dict_id = keeper_dict_id
            clear_entity_confirmation(entity)
            migrated_entities += 1
    alias_names = {alias.name.casefold() for alias in rows['aliases'] if alias.dict_id == keeper_dict_id and not alias.is_deleted}
    for alias in rows['aliases']:
        if alias.dict_id not in absorbed_dict_ids or alias.is_deleted:
            continue
        if alias.name.casefold() in alias_names:
            alias.is_deleted = True
        else:
            alias.dict_id = keeper_dict_id
            alias_names.add(alias.name.casefold())
            migrated_aliases += 1
    for queue in rows['queue']:
        if queue.target_dict_id in absorbed_dict_ids and queue.resolution == 'pending':
            queue.resolution = 'resolved_merged'
            queue.reviewed_by = reviewer
            queue.reviewed_at = datetime.now(timezone.utc)
    for mapping in rows['mappings']:
        if mapping.dict_id in absorbed_dict_ids:
            mapping.dict_id = keeper_dict_id
    session.flush()
    after = {key: [snapshot(row) for row in group] for key, group in rows.items()}
    log = EntityMergeLog(keeper_dict_id=keeper_dict_id, absorbed_dict_ids=absorbed_dict_ids,
                         before_snapshot=before, after_snapshot=after, field_decisions=field_decisions,
                         merge_type='identity', reviewer=reviewer, note=note)
    session.add(log)
    session.flush()
    return {'success': True, 'merge_log_id': log.id, 'keeper_dict_id': keeper_dict_id,
            'merged_count': len(absorbed_dict_ids), 'migrated_extractions': migrated_entities,
            'migrated_aliases': migrated_aliases, 'migrated_relations': 0}


def link_entity(session, entity, target_dict_id, reviewer, note='', review_context=None):
    from api.review_auth import require_trusted_review_context
    reviewer = require_trusted_review_context(review_context)
    dictionary_ids = sorted({value for value in (entity.dict_id, target_dict_id) if value})
    dictionaries = session.query(EntityDict).filter(EntityDict.id.in_(dictionary_ids)).order_by(EntityDict.id).populate_existing().with_for_update().all()
    target = next((row for row in dictionaries if row.id == target_dict_id), None)
    if target is None or target.is_deleted or target.entity_type != entity.entity_type:
        raise ValueError('Invalid link target')
    before = {'entities': [snapshot(entity)]}
    entity.dict_id = target_dict_id
    clear_entity_confirmation(entity)
    session.flush()
    log = EntityMergeLog(keeper_dict_id=target_dict_id, absorbed_dict_ids=[],
                        before_snapshot=before, after_snapshot={'entities': [snapshot(entity)],
                                        'dictionary_dependencies': [snapshot(row) for row in dictionaries]},
                        field_decisions={}, merge_type='identity_link', reviewer=reviewer, note=note)
    session.add(log)
    session.flush()
    refresh_link_dependencies(session, log)
    return log.id


def rollback_identity_merge(session, merge_log_id, reason='', review_context=None):
    from api.review_auth import require_trusted_review_context
    reviewer = require_trusted_review_context(review_context)
    log = session.query(EntityMergeLog).filter(EntityMergeLog.id == merge_log_id).populate_existing().with_for_update().first()
    if log is None or log.is_rolled_back:
        raise ValueError('Merge is missing or already withdrawn')
    before, after = log.before_snapshot or {}, log.after_snapshot or {}
    if 'entities' not in before and 'dictionaries' not in before:
        raise ValueError('Legacy merge snapshot is not complete enough for safe automatic rollback')
    dependencies = after.get('dictionary_dependencies')
    if log.merge_type == 'identity_link' and not dependencies:
        raise ValueError('Legacy link log lacks dictionary identity dependencies')
    if dependencies:
        ids = sorted(value['id'] for value in dependencies)
        current = {row.id: row for row in session.query(EntityDict).filter(EntityDict.id.in_(ids)).order_by(EntityDict.id).populate_existing().with_for_update().all()}
        if any(value['id'] not in current or snapshot(current[value['id']]) != value for value in dependencies):
            raise ValueError('Link dictionary identity dependencies changed; review them before withdrawing')
    if log.merge_type == 'identity_link':
        if 'evidence_dependencies' not in after or 'proposal_dependencies' not in after:
            raise ValueError('Legacy link log lacks observation dependencies')
        entity_ids = [row['id'] for row in after['entities']]
        # The dictionaries above are locked; normal writers lock that dictionary before appending material.
        observations = _identity_observation_rows(session, entity_ids)
        for key in ('evidence', 'proposals'):
            if [snapshot(row) for row in observations[key]] != after[key[:-1] + '_dependencies' if key == 'proposals' else 'evidence_dependencies']:
                raise ValueError('Link observation dependencies changed; review them before withdrawing')
    elif 'dictionaries' in after:
        if 'evidence' not in after or 'proposals' not in after:
            raise ValueError('Legacy merge log lacks observation dependencies')
        current_rows = _merge_rows(session, [row['id'] for row in after['dictionaries']])
        if any({row.id for row in current_rows[key]} != {row['id'] for row in after.get(key, [])} for key in current_rows):
            raise ValueError('Merge has new or missing dependent rows; review them before withdrawing')
    models = {'dictionaries': EntityDict, 'entities': ExtractedEntity, 'aliases': EntityAlias,
              'queue': EntityMergeQueue, 'mappings': EntityPrimeKGMappingProposal,
              'evidence': EntitySourceEvidence, 'proposals': EntityIdentityMergeProposal}
    restored = {}
    for key, expected in after.items():
        if key not in models:
            continue
        for values in expected:
            row = session.query(models[key]).filter(models[key].id == values['id']).populate_existing().with_for_update().first()
            if row is None or snapshot(row) != values:
                raise ValueError('Merge has subsequent changes; review them before withdrawing')
            restored[(key, row.id)] = row
    for key, values_list in before.items():
        for values in values_list:
            restore(restored[(key, values['id'])], values)
    log.is_rolled_back = True
    log.rolled_back_at = datetime.now(timezone.utc)
    log.rollback_reason = reason
    log.rolled_back_by = reviewer
    for proposal in session.query(EntityIdentityMergeProposal).filter(EntityIdentityMergeProposal.merge_log_id == log.id).all():
        proposal.status = 'withdrawn'
        proposal.review_note = reason
    return {'success': True, 'merge_log_id': log.id, 'restored_dict_ids': log.absorbed_dict_ids,
            'restored_extractions': len(before.get('entities', [])),
            'restored_aliases': len(before.get('aliases', [])), 'restored_relations': 0}


def resolve_link_proposal(proposal_id, status, reviewer, note='', session=None, review_context=None):
    from api.review_auth import require_trusted_review_context
    reviewer = require_trusted_review_context(review_context)
    if status not in ('approved', 'rejected', 'withdrawn'):
        raise ValueError('Invalid proposal decision')
    with use_session(session) as active:
        proposal = active.query(EntityIdentityMergeProposal).filter(EntityIdentityMergeProposal.id == proposal_id).populate_existing().with_for_update().first()
        if proposal is None:
            raise ValueError('Proposal does not exist')
        if status == 'withdrawn':
            if proposal.status != 'approved' or not proposal.merge_log_id:
                raise ValueError('Only an accepted link can be withdrawn')
            rollback_identity_merge(active, proposal.merge_log_id, note, review_context=review_context)
        else:
            if proposal.status != 'pending':
                raise ValueError('Proposal has already been decided')
            if status == 'approved':
                entity = active.query(ExtractedEntity).filter(ExtractedEntity.id == proposal.source_entity_id).populate_existing().with_for_update().first()
                if entity is None or entity.is_deleted:
                    raise ValueError('Source entity does not exist')
                source_snapshot = proposal.source_entity_snapshot or {}
                for field in ('entity_name', 'entity_type', 'entity_data', 'source_doc_uuid', 'source_site', 'dict_id'):
                    if field in source_snapshot and getattr(entity, field) != source_snapshot[field]:
                        raise ValueError('Proposal source identity has changed; refresh the proposal')
                # Refresh while locked: a long-pending candidate cannot silently point at a changed identity.
                dictionary_ids = sorted({value for value in (entity.dict_id, proposal.target_dict_id) if value})
                dictionaries = active.query(EntityDict).filter(EntityDict.id.in_(dictionary_ids)).order_by(EntityDict.id).populate_existing().with_for_update().all()
                target = next((row for row in dictionaries if row.id == proposal.target_dict_id), None)
                target_snapshot = proposal.target_dict_snapshot or {}
                required = {'canonical_name', 'entity_type', 'canonical_data', 'external_ids', 'is_verified'}
                current_target = {**snapshot(target), 'canonical_name': (target.canonical_data or {}).get('name', '')} if target is not None else {}
                if (target is None or target.is_deleted or target.entity_type != entity.entity_type
                        or not required.issubset(target_snapshot)
                        or any(current_target.get(key) != value for key, value in target_snapshot.items())):
                    raise ValueError('Proposal target identity has changed; refresh the proposal')
                proposal.merge_log_id = link_entity(active, entity, proposal.target_dict_id, reviewer, note, review_context=review_context)
        proposal.status = status
        proposal.reviewer = reviewer
        proposal.review_note = note
        proposal.reviewed_at = datetime.now(timezone.utc)
        if status == 'approved':
            active.flush()
            refresh_link_dependencies(active, active.get(EntityMergeLog, proposal.merge_log_id))
        return True


def create_primekg_mapping_proposal(dict_id, namespace, native_id, method, score=0.0, evidence=None, session=None):
    if not all(isinstance(x, str) and x.strip() for x in (namespace, native_id, method)):
        raise ValueError('Mapping namespace, native ID and method are required')
    if not 0 <= score <= 1:
        raise ValueError('Mapping score is outside [0,1]')
    evidence = evidence or {}
    key = digest([dict_id, namespace, native_id, method, score, evidence])
    with use_session(session) as active:
        dictionary = active.get(EntityDict, dict_id)
        if dictionary is None or dictionary.is_deleted:
            raise ValueError('Dictionary does not exist')
        row_id = active.execute(insert(EntityPrimeKGMappingProposal).values(
            dict_id=dict_id, namespace=namespace, native_id=native_id, method=method, score=score,
            evidence=evidence, proposal_key=key, status='pending').on_conflict_do_nothing(
                index_elements=['proposal_key']).returning(EntityPrimeKGMappingProposal.id)).scalar()
        return row_id or active.query(EntityPrimeKGMappingProposal.id).filter(EntityPrimeKGMappingProposal.proposal_key == key).scalar()


def get_primekg_mapping_proposals(status='pending', dict_id=None, limit=100, session=None):
    with use_session(session) as active:
        query = active.query(EntityPrimeKGMappingProposal)
        if status:
            query = query.filter(EntityPrimeKGMappingProposal.status == status)
        if dict_id is not None:
            query = query.filter(EntityPrimeKGMappingProposal.dict_id == dict_id)
        return [snapshot(row) for row in query.order_by(EntityPrimeKGMappingProposal.id.desc()).limit(limit).all()]


def resolve_primekg_mapping_proposal(proposal_id, status, reviewer, note='', session=None, review_context=None):
    from api.review_auth import require_trusted_review_context
    reviewer = require_trusted_review_context(review_context)
    if status not in ('approved', 'rejected', 'withdrawn'):
        raise ValueError('Invalid mapping decision')
    # Read and validate immutable evidence before acquiring the source write lock.
    with use_session(session) as active:
        proposal = active.get(EntityPrimeKGMappingProposal, proposal_id)
        if proposal is None:
            raise ValueError('Mapping proposal does not exist')
        expected = snapshot(proposal)
        dictionary = active.get(EntityDict, proposal.dict_id)
        expected_dictionary = snapshot(dictionary) if dictionary else None
        evidence = proposal.evidence or {}
    validated = []
    if status == 'approved':
        validated = validate_evidence([evidence], evidence.get('source_doc_uuid', ''), evidence.get('source_site', ''))
    with use_session(session) as active:
        proposal = active.query(EntityPrimeKGMappingProposal).filter(EntityPrimeKGMappingProposal.id == proposal_id).populate_existing().with_for_update().first()
        if snapshot(proposal) != expected:
            raise ValueError('Mapping proposal changed during validation')
        if status == 'withdrawn':
            if proposal.status != 'approved':
                raise ValueError('Only an accepted mapping can be withdrawn')
        elif proposal.status != 'pending':
            raise ValueError('Mapping proposal has already been decided')
        if status == 'approved':
            dictionary = active.query(EntityDict).filter(EntityDict.id == proposal.dict_id).populate_existing().with_for_update().first()
            if dictionary is None or snapshot(dictionary) != expected_dictionary:
                raise ValueError('Dictionary identity changed during evidence validation')
            if not auditable_identity(dictionary):
                raise ValueError('Dictionary identity has no auditable human confirmation')
        if validated:
            proposal.evidence = validated[0]
        proposal.status = status
        proposal.reviewer = reviewer
        proposal.review_note = note
        proposal.reviewed_at = datetime.now(timezone.utc)
        return True
