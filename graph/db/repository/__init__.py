"""Database repository: the one entry point for graph data operations, split by table family.

Callers go through this package instead of writing SQL themselves; deletes are soft (queries filter
is_deleted=False); writes are idempotent (upsert on uuid ON CONFLICT); sessions come from graph/db/session.py
get_session(). Every name is re-exported here, so `from graph.db.repository import x` and `repository.x` keep
working; the code lives in the sibling modules.
"""
# evidence and mapping APIs share the same source transaction helpers and are reached through this package too
from graph.db.identity import (  # noqa: E402,F401
    create_primekg_mapping_proposal,
    get_object_evidence,
    get_primekg_mapping_proposals,
    resolve_primekg_mapping_proposal,
)
from graph.db.repository.aliases import (  # noqa: F401
    _FORMULATION_SUFFIX,
    _write_aliases_with_embedding,
    add_alias_with_embedding,
    add_aliases,
    get_entity_with_aliases,
    lookup_alias,
    lookup_alias_candidates,
    lookup_external_id,
    lookup_external_id_candidates,
    search_similar_entities,
)
from graph.db.repository.audit import (  # noqa: F401
    _write_operation_log,
    _write_review_log,
    log_review_action,
)
from graph.db.repository.canonical import (  # noqa: F401
    _ARRAY_FIELDS,
    _SKIP_MERGE_FIELDS,
    _SNAPSHOT_FIELDS,
    _SOURCE_PRIORITY,
    _VALID_FIELDS_BY_TYPE,
    _is_empty_value,
    _sync_canonical_to_extracted,
    get_entity_dict_by_id,
    merge_canonical_data,
    process_entity_merge,
)
from graph.db.repository.documents import (  # noqa: F401
    _parse_ts,
    _upsert_doc_extra_in_session,
    get_attachments,
    get_authority_resolutions,
    get_document_by_uuid,
    get_pending_extraction,
    reset_stale_extractions,
    update_extraction_status,
    upsert_doc_extra,
    upsert_document,
)
from graph.db.repository.entity_dicts import (  # noqa: F401
    _rerank_candidates,
    find_candidate_dicts_for_review,
    soft_delete_entity_alias,
    soft_delete_entity_dict,
)
from graph.db.repository.extraction_results import (  # noqa: F401
    _cascade_recompute_stale_entities,
    _create_entity_dict_from_extraction,
    _do_insert_extraction_result,
    _extract_external_ids,
    _find_existing_entity_by_dict_id,
    _is_stale_step_none,
    _refresh_step_metadata,
    insert_extraction_result,
)
from graph.db.repository.merges import (  # noqa: F401
    _build_search_result,
    batch_resolve_merges,
    compare_entities,
    create_identity_merge_proposal,
    get_identity_merge_proposals,
    get_merge_logs,
    get_pending_merges,
    merge_entities,
    resolve_identity_merge_proposal,
    resolve_merge,
    rollback_merge,
    search_entity_dicts,
)
from graph.db.repository.review_entities import (  # noqa: F401
    _dedup_extracted_entities_by_dict_id,
    _entity_to_dict,
    approve_entity,
    approve_entity_with_alias_writeback,
    batch_approve_entities,
    batch_reject_entities,
    get_pending_review_entities,
    reject_entity,
)
from graph.db.repository.review_relations import (  # noqa: F401
    _relation_to_dict,
    approve_relation,
    batch_approve_relations,
    batch_reject_relations,
    get_pending_review_relations,
    reject_relation,
)
from graph.db.repository.weknora_sync import (  # noqa: F401
    batch_mark_entity_dict_failed,
    batch_mark_entity_dict_synced,
    batch_mark_entity_weknora_graph_failed,
    batch_mark_entity_weknora_graph_pushed,
    batch_mark_relation_weknora_graph_failed,
    batch_mark_relation_weknora_graph_pushed,
    get_entities_for_weknora_graph,
    get_entity_dict_for_weknora,
    get_relations_for_weknora_graph,
    get_weknora_graph_stats,
    mark_entity_dict_failed,
    mark_entity_dict_synced,
    mark_entity_weknora_graph_failed,
    mark_entity_weknora_graph_pushed,
    mark_relation_weknora_graph_failed,
    mark_relation_weknora_graph_pushed,
)
