"""Reference-data steps that follow a role's migrations: rows, not structure. Each step is idempotent and returns what
it inserted (empty: nothing, nothing is logged); the runner appends the result to fabric_reference_data_log."""

from sqlalchemy import text

from biz_rules.history_models import canonical
from biz_rules.history_sources import SOURCE_CATALOG


def _merge_catalog(conn, source_id, fields):
    conn.execute(
        text("UPDATE history_sources SET catalog=catalog || CAST(:metadata AS jsonb) WHERE source_id=:id"),
        {"id": source_id, "metadata": canonical(fields)},
    )


def seed_history_sources(conn) -> dict:
    """One history_sources row per catalog source, created if missing. An existing row keeps its licence state
    (access, dependencies, read_enabled); only the descriptive fields of some sources follow the catalog."""
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
        if source_id == "external_archive":
            _merge_catalog(conn, source_id, {"business_types": catalog["business_types"],
                                             "read_scope": catalog["read_scope"]})
        if catalog.get("acquisition_channel") == "site_plugin":
            # site descriptions follow the collection catalog (an existing row is not touched by ON CONFLICT);
            # access, dependencies and licence state stay as the administrator set them
            _merge_catalog(conn, source_id, {k: catalog[k] for k in (
                "name", "business_types", "acquisition_channel", "history_coverage", "native_id",
                "read_scope", "limitations") if k in catalog})
        if source_id == "primekg_graph":
            # the current capability text only: persisted dependencies, licence and versions stay
            _merge_catalog(conn, source_id, {"history_coverage": catalog["history_coverage"]})
    return {"inserted": inserted} if inserted else {}


REFERENCE_STEPS = {"history": (("seed_history_sources", seed_history_sources),)}
