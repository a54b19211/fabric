"""Reference-data steps that follow a role's migrations: rows, not structure. Each step is idempotent and returns what
it inserted (empty: nothing, nothing is logged); the runner appends the result to fabric_reference_data_log."""

from sqlalchemy import text

from biz_rules.history_models import canonical
from biz_rules.history_sources import SOURCE_CATALOG


def seed_history_sources(conn) -> dict:
    """One history_sources row per catalog source, created if missing, and nothing else: an existing row is the
    administrator's decision (read_enabled, dependencies, access) and its `catalog` is the snapshot taken when it was
    seeded. The descriptive text of a source is its declaration (biz_rules.source_licenses), which /sources reads."""
    inserted = []
    for source_id, catalog in SOURCE_CATALOG.items():
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
