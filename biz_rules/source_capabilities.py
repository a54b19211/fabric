"""What each source can do, computed by Fabric from the licence state and the registered implementation.

capability = what the administrator allows now (readable, from the library) AND what the code can do (the collection
spec, the plugin's read modes, the processor registry, the configured index targets). A consumer (Radar) reads these
and keeps no list of sources of its own.

A capability snapshot is for planning and audit only. It authorises nothing later: every send, retry, cache hit and
report render re-checks the current permission, and a read by version still verifies the exact identity and its
visibility. Two digests say what a snapshot is about: `definition_sha256` covers only the static declarations (same code,
same digest) and `receipt_sha256` covers the whole receipt, including licence state and index progress (it changes
whenever any of that does). Neither may be used to cache a permission.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

CAPABILITIES_VERSION = "datafabric.source_capabilities.v1"
HISTORY_MODES = ("current", "operational_replay", "public_history_reconstruction")
# the graph packages and the retired bridge have no external proof of what was public in the past
RECONSTRUCTION_UNSUPPORTED = frozenset({"reviewed_graph", "primekg_graph", "legacy_documents"})
CAPABILITY_FLAGS = ("read", "list", "search", "select", "graph", "watch")


@dataclass(frozen=True)
class SourceFacts:
    """What the library and the configuration say about one source right now."""

    source_id: str
    readable: bool
    published_seq: int = 0
    index_targets: tuple[dict, ...] = ()
    index_error: str | None = None
    fetch_enabled: bool = True


def record_kind(source_id: str) -> str:
    from biz_rules.source_licenses import DERIVED

    for item in DERIVED:
        if item.source_id == source_id:
            return item.record_kind
    return "original"


def is_retired(source_id: str) -> bool:
    from biz_rules.retired_sources import RETIRED

    return source_id in RETIRED


def _fetch(source_id: str, facts: SourceFacts) -> dict | None:
    from collection.catalog import CATALOG

    declared = CATALOG.get(source_id)
    if declared is None or not declared.fetchable or not facts.readable:
        return None
    # the public-read switch closes every mode that reaches an outside source; an upload is not one of them
    modes = [m for m in declared.read_modes if facts.fetch_enabled or m == "upload"]
    return {"modes": modes, "admin_only": True} if modes else None


def _processors(source_id: str) -> dict:
    from processing import registry

    applied = registry.applicable(source_id)
    attachments = registry.processor("attachment_parse")
    return {
        "attachments": ({"versions": True, "text_processor": attachments.version}
                        if "attachment_parse" in applied else None),
        "clean_text": {"processor": applied["clean_text"]} if "clean_text" in applied else None,
        "registry_diff": {"processor": applied["registry_diff"]} if "registry_diff" in applied else None,
    }


def capabilities(facts: SourceFacts) -> dict:
    """The capability object of one source. Every capability but `readable` requires `readable`; nothing is inferred
    from the source's group (collected, derived, retired)."""
    source_id, readable = facts.source_id, facts.readable
    retired, kind = is_retired(source_id), record_kind(source_id)
    # only fixed originals are indexed for search; graph packages are read through the graph interface
    searchable = readable and not retired and kind == "original" and bool(facts.index_targets)
    from collection.catalog import CATALOG

    declared = CATALOG.get(source_id)
    processors = _processors(source_id) if readable else {"attachments": None, "clean_text": None,
                                                           "registry_diff": None}
    return {
        "readable": readable,
        "read": readable and facts.published_seq > 0,
        "list": readable and not retired,
        "search": searchable,
        "select": readable and not retired and kind == "original",
        "fetch": _fetch(source_id, facts),
        "graph": readable and kind == "graph",
        "watch": bool(readable and declared is not None and declared.watchable),
        **processors,
        "material_kind": "fabric_document_evidence" if readable and kind == "original" else None,
        "index": {"targets": list(facts.index_targets), "searchable": searchable,
                  **({"configuration_error": facts.index_error} if facts.index_error else {})},
        "history_modes": [m for m in HISTORY_MODES
                          if m != "public_history_reconstruction" or source_id not in RECONSTRUCTION_UNSUPPORTED],
        "status": declared.status if declared is not None else None,
        "verified": declared.verified if declared is not None else None,
        "record_kind": kind,
        "retired": retired,
    }


def unavailable(source_id: str) -> dict:
    """An entry for a source Fabric knows but that is not readable now: every capability false, so a consumer can see
    that `readable` is false for it without it ever having been offered."""
    return {"source_id": source_id, "capabilities": capabilities(SourceFacts(source_id, readable=False))}


def _spec_dict(item) -> dict:
    data = asdict(item)
    data["fetchable"], data["watchable"] = item.fetchable, item.watchable
    return data


def definition() -> dict:
    """The static declarations the capabilities are computed from."""
    from biz_rules.history_sources import SOURCE_CATALOG
    from biz_rules.retired_sources import RETIRED
    from biz_rules.source_licenses import DERIVED, collected_licenses
    from collection.catalog import SPECS
    from processing import registry

    return {
        "capabilities_version": CAPABILITIES_VERSION,
        "collection": [_spec_dict(item) for item in SPECS],
        "licenses": {source_id: {**asdict(item), "catalog": item.catalog_entry()}
                     for source_id, item in {**collected_licenses(), **{d.source_id: d for d in DERIVED}}.items()},
        "retired": {source_id: {"decision_date": item.decision_date, "decision_ref": item.decision_ref,
                                "resolves_via": item.resolves_via, "description": item.description}
                    for source_id, item in RETIRED.items()},
        "catalog_keys": list(SOURCE_CATALOG),
        "processors": registry.definition(),
    }


def digest(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def definition_sha256() -> str:
    return digest(definition())


def receipt_sha256(receipt: dict) -> str:
    """The digest of a receipt without its own digest field."""
    return digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})
