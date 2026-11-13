"""
tests/ 共享 fixtures —— 所有 mock，不连接数据库。
"""
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，确保测试能 import 项目模块（graph/disambiguation/、db/ 等）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_session():
    """返回一个 MagicMock 封装的 SQLAlchemy Session，用于注入 repository 函数。"""
    session = MagicMock()
    session.query.return_value = session
    session.filter.return_value = session
    session.filter_by.return_value = session
    session.join.return_value = session
    session.outerjoin.return_value = session
    session.order_by.return_value = session
    session.group_by.return_value = session
    session.having.return_value = session
    session.limit.return_value = session
    session.offset.return_value = session
    session.distinct.return_value = session
    session.all.return_value = []
    session.first.return_value = None
    session.one_or_none.return_value = None
    session.count.return_value = 0
    session.scalar.return_value = None
    return session


@pytest.fixture
def mock_engine():
    """Mock create_engine，阻止真实数据库连接。"""
    with patch("sqlalchemy.create_engine") as mock:
        yield mock


@pytest.fixture
def mock_get_session():
    """Mock graph.db.session.get_session，注入 mock_session。"""
    with patch("graph.db.session.get_session") as mock:
        yield mock


@pytest.fixture(autouse=True)
def _own_rate_dir(monkeypatch, tmp_path):
    """The rate gate and the outbound counters keep their state in DATAFABRIC_RATE_DIR. A test gets an empty one,
    so it neither waits for another test's last request nor inherits its counts."""
    monkeypatch.setenv("DATAFABRIC_RATE_DIR", str(tmp_path / "rate"))


@pytest.fixture
def mid_hour_clock(monkeypatch):
    """Pin the clock of the outbound counters to the middle of a UTC hour, so that a test cannot straddle the mark
    where an hourly or daily count starts again. Waiting and deadlines keep the real clock."""
    import time
    from types import SimpleNamespace

    from fetch import rate

    moment = 1_800_000_000 - 1_800_000_000 % 3600 + 1800
    monkeypatch.setattr(
        rate, "time", SimpleNamespace(time=lambda: float(moment), monotonic=time.monotonic, sleep=time.sleep)
    )
