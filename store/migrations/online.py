"""Online operations: statements PostgreSQL refuses inside a transaction (CREATE INDEX CONCURRENTLY). Each runs on
its own AUTOCOMMIT connection, then checks that the result is valid; the rollback is a named operation too."""

from __future__ import annotations

from dataclasses import dataclass

from store.migrations.app_ledger import MigrationStateError

SITE_INDEX = "idx_documents_publication_site_id"


@dataclass(frozen=True)
class OnlineOp:
    create: str
    drop: str
    index: str
    lock_timeout: str = "3s"
    statement_timeout: str = "30min"


ONLINE_OPS = {
    "publication": {
        "site-index": OnlineOp(
            create=f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {SITE_INDEX} ON documents(source_site,id)",
            drop=f"DROP INDEX CONCURRENTLY IF EXISTS {SITE_INDEX}",
            index=SITE_INDEX,
        ),
    },
}


def run(role: str, engine, operation: str, *, drop: bool = False) -> dict:
    op = ONLINE_OPS.get(role, {}).get(operation)
    if op is None:
        raise MigrationStateError(f"no online operation {operation!r} for the {role} role")
    from store.migrations import runner

    with runner.migration_lock(engine) as lock:
        conn = lock.execution_options(isolation_level="AUTOCOMMIT")  # the lock connection runs it, outside a transaction
        runner.assert_lock_held(conn)
        conn.exec_driver_sql(f"SET lock_timeout='{op.lock_timeout}'")
        conn.exec_driver_sql(f"SET statement_timeout='{op.statement_timeout}'")
        if drop:
            conn.exec_driver_sql(op.drop)
        else:
            conn.exec_driver_sql(op.create)
            valid = conn.exec_driver_sql(
                f"SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('{op.index}')").scalar_one()
            if valid is not True:
                raise RuntimeError("来源游标索引未完成，须先按回退命令删除无效索引后重建")
        runner.assert_lock_held(conn)  # the operation ran while this run still held the lock
    return {"db_role": role, "online": operation, "action": "drop" if drop else "create", "state": "done"}
