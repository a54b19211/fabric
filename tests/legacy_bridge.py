"""Legacy versions for the tests that read them. store.records.import_archive refuses legacy_documents (410) since the
bridge was retired, but the live database keeps what the bridge wrote: the legacy migration and the old index entries
read those versions. write() stores one as the bridge did."""

from fetch.projection import project_record
from store import records


def write(request, key):
    """import_archive without its refusal of legacy_documents."""
    prepared = [(item, project_record(request.source_id, item)) for item in request.records]
    with records.fabric_engine().begin() as conn:
        run, fresh = records._new_run(conn, request.source_id, request.is_demo, "archive", {}, key,
                                      request.scope_description)
        if fresh:
            records._publish(conn, run, prepared, records._now(conn), {})
        return records._view(records._run(conn, run["id"]))
