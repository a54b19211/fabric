"""只对显式配置的原采集库安装发表待办；不默认连接APP或现役。"""

import argparse
from pathlib import Path

from db.publication import publication_engine


def migrate_publication():
    directory = Path(__file__).parent / "versions"
    # V026 在 V019 之后：documents 增加 history 固定版本主体的引用列，触发器跳过这类主体的冻结
    sql = "\n".join((directory / name).read_text()
                    for name in ("V019__publication_outbox.sql", "V026__history_extraction_subjects.sql"))
    # 函数体含分号；整份SQL交给psycopg2，不用历史迁移的简单分号切分器。
    with publication_engine().begin() as conn:
        conn.exec_driver_sql("SET LOCAL lock_timeout='3s'")
        conn.exec_driver_sql("SET LOCAL statement_timeout='30s'")
        conn.exec_driver_sql(sql)


def site_index(*, drop=False):
    # 独立AUTOCOMMIT：CONCURRENTLY不在迁移事务/应用启动里运行。
    with (
        publication_engine()
        .connect()
        .execution_options(isolation_level="AUTOCOMMIT") as conn
    ):
        conn.exec_driver_sql("SET lock_timeout='3s'")
        conn.exec_driver_sql("SET statement_timeout='30min'")
        if drop:
            conn.exec_driver_sql(
                "DROP INDEX CONCURRENTLY IF EXISTS idx_documents_publication_site_id"
            )
        else:
            conn.exec_driver_sql(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_publication_site_id ON documents(source_site,id)"
            )
            valid = conn.exec_driver_sql(
                "SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('idx_documents_publication_site_id')"
            ).scalar_one()
            if valid is not True:
                raise RuntimeError(
                    "来源游标索引未完成，须先按回退命令删除无效索引后重建"
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--site-index", action="store_true")
    group.add_argument("--drop-site-index", action="store_true")
    args = parser.parse_args()
    if args.site_index or args.drop_site_index:
        site_index(drop=args.drop_site_index)
        print("独立来源游标索引操作完成")
    else:
        migrate_publication()
        print("原采集库发表待办及固定触发器已安装")
