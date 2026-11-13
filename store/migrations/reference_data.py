"""Reference-data steps that follow a role's migrations: rows, not structure. Each step is idempotent and returns what
it inserted (empty: nothing, nothing is logged); the runner appends the result to fabric_reference_data_log."""

from sqlalchemy import text

from fetch.retired_sources import RETIRED
from fetch.sources import SOURCE_CATALOG
from store.history_models import canonical


def seed_history_sources(conn) -> dict:
    """One history_sources row per catalog source, created if missing, and nothing else: an existing row is the
    administrator's decision (read_enabled, dependencies, access) and its `catalog` is the snapshot taken when it was
    seeded. The descriptive text of a source is its declaration (fetch.source_licenses), which /sources reads."""
    inserted = []
    # a retired source keeps its row: its old identifiers still resolve through it, under the same permission rules
    rows = [*SOURCE_CATALOG.items(), *((source_id, item.catalog_entry()) for source_id, item in RETIRED.items())]
    for source_id, catalog in rows:
        created = conn.execute(
            text("""
          INSERT INTO history_sources(source_id,catalog) VALUES (:id,CAST(:catalog AS jsonb))
          ON CONFLICT (source_id) DO NOTHING
        """),
            {"id": source_id, "catalog": canonical(catalog)},
        ).rowcount
        if created:
            inserted.append(source_id)
    return {"inserted": inserted} if inserted else {}


REFERENCE_STEPS = {"history": (("seed_history_sources", seed_history_sources),)}
