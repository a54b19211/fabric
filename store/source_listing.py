"""GET /api/history/sources: every history_sources row with its name, and the few capabilities the radar reads.

Readable = named by DATAFABRIC_HISTORY_SOURCE_ALLOWLIST, read_enabled in the library, and every source it depends on
readable too. Readable sources go to `sources`; retired sources (readable or not) go to `retired_sources`; any other
source that is not readable is left out (the radar treats a source it does not see as not readable).
"""

from __future__ import annotations

from clean import processors
from fetch.catalog import CATALOG, GRAPH_SOURCES, RETIRED

CAPABILITIES_VERSION = "datafabric.source_capabilities.v1"


def capabilities(source_id: str, readable: bool, fetch_enabled: bool) -> dict:
    """What the radar reads: readable; select (may be chosen for an analysis); fetch.modes ("query" = Fabric can
    collect fresh records for a search term); attachments.versions (attachments get text from the processing
    layer); registry_diff (versions are compared field by field). A source that is not readable has none of them."""
    spec = CATALOG.get(source_id)
    # the public-read switch closes every mode that reaches an outside source; an upload is not one of them
    modes = [m for m in spec.read_modes if fetch_enabled or m == "upload"] if readable and spec else []
    applied = processors.applicable(source_id) if readable else {}
    return {
        "readable": readable,
        "select": readable and source_id not in RETIRED and source_id not in GRAPH_SOURCES,
        "fetch": {"modes": modes} if modes else None,
        "attachments": {"versions": True} if "attachment_parse" in applied else None,
        "registry_diff": {"processor": applied["registry_diff"]} if "registry_diff" in applied else None,
    }


def listing(rows, allowed, dependencies_of) -> dict:
    """The /sources body from the library rows. `dependencies_of(row)` is store.history._catalog_dependencies."""
    from fetch.sources import SOURCE_CATALOG, public_fetch_enabled

    by_id = {row["source_id"]: row for row in rows}

    def readable(source_id, seen=()):
        row = by_id.get(source_id)
        if row is None or source_id in seen or source_id not in allowed or row["read_enabled"] is not True:
            return False
        return all(readable(value, (*seen, source_id)) for value in dependencies_of(row))

    fetch_enabled = public_fetch_enabled()
    sources, retired = [], []
    for row in rows:
        source_id, ok = row["source_id"], readable(row["source_id"])
        name = (SOURCE_CATALOG.get(source_id) or row["catalog"]).get("name")
        entry = {"source_id": source_id, "name": name, "capabilities": capabilities(source_id, ok, fetch_enabled)}
        if source_id in RETIRED:
            retired.append(entry)
        elif ok:
            sources.append(entry)
    return {"capabilities_version": CAPABILITIES_VERSION, "sources": sources, "retired_sources": retired}
