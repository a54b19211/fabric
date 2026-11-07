"""Database repository: the one entry point for graph data operations, split by table family.

Callers go through this package instead of writing SQL themselves; deletes are soft (queries filter
is_deleted=False); writes are idempotent (upsert on uuid ON CONFLICT); sessions come from db/session.py
get_session(). Every name is re-exported here, so `from db.repository import x` and `repository.x` keep
working; the code lives in the sibling modules.
"""
from db.repository.canonical import (  # noqa: F401
    _SOURCE_PRIORITY,
    _ARRAY_FIELDS,
    _SNAPSHOT_FIELDS,
    merge_canonical_data,
    get_entity_dict_by_id,
    _is_empty_value,
    _SKIP_MERGE_FIELDS,
    _VALID_FIELDS_BY_TYPE,
    process_entity_merge,
    _sync_canonical_to_extracted,
)
from db.repository.aliases import (  # noqa: F401
    _FORMULATION_SUFFIX,
    lookup_alias_candidates,
    lookup_alias,
    search_similar_entities,
    add_aliases,
    lookup_external_id,
    get_entity_with_aliases,
    add_alias_with_embedding,
    _write_aliases_with_embedding,
    lookup_external_id_candidates,
)
from db.repository.audit import (  # noqa: F401
    _write_operation_log,
    log_review_action,
    _write_review_log,
)
from db.repository.documents import (  # noqa: F401
    upsert_document,
    upsert_doc_extra,
    _upsert_doc_extra_in_session,
    _opt_str,
    _opt_int,
    _opt_float,
    _collect_authority_rows,
    upsert_authority_resolutions,
    get_authority_resolutions,
    get_document_by_uuid,
    update_extraction_status,
    reset_stale_extractions,
    get_pending_extraction,
    get_attachments,
    _parse_ts,
)
from db.repository.merges import (  # noqa: F401
    create_identity_merge_proposal,
    get_identity_merge_proposals,
    resolve_identity_merge_proposal,
    get_pending_merges,
    resolve_merge,
    batch_resolve_merges,
    search_entity_dicts,
    _build_search_result,
    compare_entities,
    merge_entities,
    rollback_merge,
    get_merge_logs,
)
from db.repository.extraction_results import (  # noqa: F401
    _find_existing_entity_by_dict_id,
    _is_stale_step_none,
    _refresh_step_metadata,
    _cascade_recompute_stale_entities,
    insert_extraction_result,
    _do_insert_extraction_result,
    _extract_external_ids,
    _create_entity_dict_from_extraction,
)
from db.repository.review_entities import (  # noqa: F401
    get_pending_review_entities,
    approve_entity,
    _dedup_extracted_entities_by_dict_id,
    approve_entity_with_alias_writeback,
    reject_entity,
    batch_approve_entities,
    batch_reject_entities,
    _entity_to_dict,
)
from db.repository.review_relations import (  # noqa: F401
    get_pending_review_relations,
    approve_relation,
    reject_relation,
    batch_approve_relations,
    batch_reject_relations,
    _relation_to_dict,
)
from db.repository.weknora_sync import (  # noqa: F401
    get_entity_dict_for_weknora,
    mark_entity_dict_synced,
    mark_entity_dict_failed,
    batch_mark_entity_dict_synced,
    batch_mark_entity_dict_failed,
    get_entities_for_weknora_graph,
    mark_entity_weknora_graph_pushed,
    mark_entity_weknora_graph_failed,
    batch_mark_entity_weknora_graph_pushed,
    batch_mark_entity_weknora_graph_failed,
    get_relations_for_weknora_graph,
    mark_relation_weknora_graph_pushed,
    mark_relation_weknora_graph_failed,
    batch_mark_relation_weknora_graph_pushed,
    batch_mark_relation_weknora_graph_failed,
    get_weknora_graph_stats,
)
from db.repository.entity_dicts import (  # noqa: F401
    soft_delete_entity_dict,
    soft_delete_entity_alias,
    find_candidate_dicts_for_review,
    _rerank_candidates,
)

# evidence and mapping APIs share the same source transaction helpers and are reached through this package too
from db.identity import (  # noqa: E402,F401
    create_primekg_mapping_proposal,
    get_object_evidence,
    get_primekg_mapping_proposals,
    resolve_primekg_mapping_proposal,
)
