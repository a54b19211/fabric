"""只对显式配置的原采集库安装发表待办；不默认连接APP或现役。"""

from pathlib import Path

from db.publication import publication_engine


def migrate_publication():
    sql = (
        Path(__file__).parent / "versions" / "V019__publication_outbox.sql"
    ).read_text()
    # 函数体含分号；整份SQL交给psycopg2，不用历史迁移的简单分号切分器。
    with publication_engine().begin() as conn:
        conn.exec_driver_sql("SET LOCAL lock_timeout='3s'")
        conn.exec_driver_sql("SET LOCAL statement_timeout='30s'")
        conn.exec_driver_sql(sql)


if __name__ == "__main__":
    migrate_publication()
    print("原采集库发表待办及固定触发器已安装")
