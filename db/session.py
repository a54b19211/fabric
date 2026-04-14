"""
数据库 Session 管理 —— 引擎单例 + 上下文管理器。
DB_HOST 未配置时所有操作静默跳过，不影响本地运行。
"""
import os
os.environ["PGCLIENTENCODING"] = "UTF8"
from contextlib import contextmanager
from typing import Generator, Optional

from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from config.settings import APP
from db.models import Base

# 引擎单例，启动时按需初始化
_engine = None
_SessionLocal: Optional[sessionmaker] = None




def get_engine():
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    db_url = APP.DB_URL
    if not db_url:
        return None

    _engine = create_engine(
        db_url,
        pool_size=APP.DB_POOL_SIZE,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
        connect_args={"client_encoding": "utf8"},
    )
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    logger.info(f"[DB] 数据库引擎已初始化: {APP.DB_HOST}:{APP.DB_PORT}/{APP.DB_NAME}")
    return _engine


@contextmanager
def get_session() -> Generator[Optional[Session], None, None]:
    """
    上下文管理器，自动提交/回滚。
    DB 未配置时 yield None，调用方需判断。

    用法:
        with get_session() as session:
            if session is None:
                return   # DB 未配置，跳过
            session.add(...)
    """
    engine = get_engine()
    if engine is None:
        yield None
        return

    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"[DB] Session 异常，已回滚: {e}")
        raise
    finally:
        session.close()


def init_db():
    """
    建表（如果表不存在）。
    等价于执行 db/schema.sql，但通过 ORM 元数据驱动，幂等安全。
    """
    engine = get_engine()
    if engine is None:
        logger.warning("[DB] DB_HOST 未配置，跳过建表")
        return

    Base.metadata.create_all(engine)
    logger.info("[DB] 数据库表结构已同步")


def check_connection() -> bool:
    """测试数据库连通性，返回 True/False。"""
    engine = get_engine()
    if engine is None:
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"[DB] 连接测试失败: {e}")
        return False
