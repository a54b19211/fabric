"""The History library's migrations (the common runner's `history` role) and its reference data. Must be given an
independent URL (DATAFABRIC_HISTORY_DATABASE_URL); the object library's configuration is never loaded.

    python -m migration.history          # = python -m migration.runner --db history --apply
"""

from db.history import history_engine
from migration import runner


def migrate_history():
    return runner.apply("history", history_engine())


if __name__ == "__main__":
    migrate_history()
    print("历史库迁移完成（仅独立历史数据库）")
