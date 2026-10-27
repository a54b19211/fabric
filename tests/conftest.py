"""
tests/ 共享 fixtures —— 所有 mock，不连接数据库。
"""
import copy
import json
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，确保测试能 import 项目模块（disambiguation/、db/ 等）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import MagicMock, patch


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
    """Mock db.session.get_session，注入 mock_session。"""
    with patch("db.session.get_session") as mock:
        yield mock


@pytest.fixture(autouse=True)
def _block_chembl_network(monkeypatch):
    """默认屏蔽 ChEMBL 在线查询，保证单测不触网、结果确定。

    为什么必须要有：`resolve_identifier` 的 drug_name 通路现在是
    PubChem → ChEMBL → 本地白名单 三级。历史上只 mock 了 PubChem 的测试，
    在「PubChem 确认未命中」时会真的去请求 EBI —— 测试变慢、依赖外网、
    还可能因限流把 degraded 翻成 True 而随机失败。

    需要验证 ChEMBL 分支的测试自己 monkeypatch 覆盖（fixture 先于测试体执行，
    测试体里的 setattr 会生效）。
    """
    from core_engine import authority_client

    monkeypatch.setattr(
        authority_client, "_fetch_chembl_molecule", lambda _name, _timeout: None
    )


@pytest.fixture(autouse=True)
def _block_authority_db_write(monkeypatch):
    """默认屏蔽对齐结果入库，保证单测不依赖数据库、也不污染真实库。

    为什么必须要有：`write_to_database` 在写库成功后会调用
    `upsert_authority_resolutions` 把对齐结果写进 `doc_authority_resolution`。
    历史测试只 mock 了 `upsert_document` 与 `save_authority_resolutions`，
    一旦本机 PG 起着，这些用例就会真的往业务库里写数据（重复跑还会改变行数）。

    需要验证双写行为的测试自己 monkeypatch 覆盖本桩（fixture 先于测试体执行，
    测试体里的 setattr 生效），或直接用模块里绑定的真实引用。
    """
    from core_engine import storage_writer

    monkeypatch.setattr(
        storage_writer, "upsert_authority_resolutions", lambda *a, **kw: 0
    )



#
# 为什么需要这一层：
#   正文在管线内叫 parsed_result["markdown"]，
#   落库时被 storage_writer.py:79 写进 raw_content 列，
#   而 check_quality() 读的又是 "markdown"。
#   也就是说二者是同一份内容、两个名字——落库信封直接喂质检会读成 0 字符。
#   生产链路上不存在这种调用（质检发生在落库前），所以这不是缺陷，
#   只是"拿落库快照做测试"时必须做的字段改名。

ENVELOPE_DIR = Path(__file__).resolve().parent.parent / "data" / "json_envelopes"
LINEAGE_DIR = Path(__file__).resolve().parent.parent / "data" / "lineage"

# 通用字段：不属于"站点特有结构化字段"，不放进 structured_data
_GENERIC_FIELDS = {"title", "publish_date", "category", "summary"}


def _lineage_final_values(lineage) -> dict:
    """从字段级血缘里取每个字段的最终值（取末步 output）。

    血缘结构：{字段名: [{"step": "parse", "op": "...", "input": "...", "output": "..."}]}
    取末步 output 即该字段经过 parse→clean 后的最终值。
    """
    values = {}
    if not isinstance(lineage, dict):
        return values
    for field, steps in lineage.items():
        if not isinstance(steps, list) or not steps:
            continue
        last = steps[-1]
        if isinstance(last, dict):
            out = last.get("output")
            if out not in (None, "", [], {}):
                values[field] = out
    return values


def _envelope_to_parsed(env: dict) -> dict:
    """把落库信封（raw_content 口径）转成质检输入（markdown 口径）。

    字段来源有二：
      1. 信封本体 —— markdown / title / publish_date / provenance / extra
      2. 字段级血缘 —— 补 structured_data 与 doi / journal_name

    ⚠️ 信封内自带的 `lineage` 字段不可信：实测 150 个信封该字段全部为
    固定的 4 个占位键（approval_number/drug_name/manufacturer/title），
    与站点无关。真实字段血缘在 data/lineage/<site>/<uuid>.json，
    由 real_envelopes fixture 读出后挂在 `_lineage_file` 上。

    信封不提供的字段（sniffed_meta / authority_resolutions）不在此虚构。
    """
    prov = env.get("provenance") or {}
    extra = env.get("extra") or {}
    lineage = env.get("_lineage_file") or env.get("lineage") or {}
    lv = _lineage_final_values(lineage)

    # 站点特有字段：先取 extra 中非空项，再用血缘补齐（血缘覆盖更全）
    structured = {k: v for k, v in extra.items() if v not in (None, "", [], {})}
    for field, value in lv.items():
        if field not in _GENERIC_FIELDS:
            structured.setdefault(field, value)

    return {
        "uuid": env.get("uuid", ""),
        "markdown": env.get("raw_content", ""),  # ← 与 raw_content 的唯一差异
        "title": env.get("title", ""),
        "publish_date": env.get("publish_date", ""),
        "structured_data": structured,
        "doi": lv.get("doi", "") or extra.get("doi", ""),
        "journal_name": lv.get("journal_name", "") or extra.get("journal_name", ""),
        "source_url": prov.get("source_url", ""),
        "source_site": prov.get("source_site", ""),
        # 深拷贝：check_quality 会往 lineage 追加 quality 步，避免污染源数据
        "lineage": copy.deepcopy(lineage) if isinstance(lineage, dict) else {},
        "doc_type": env.get("doc_type", ""),
        "language": env.get("language", ""),
    }


def _load_envelopes(site: str) -> list:
    """读取 data/json_envelopes/<site>/ 下全部真实信封，按文件名排序。

    同时配对 data/lineage/<site>/<uuid>.json，挂到 `_lineage_file` 键上
    （实测 150/150 全部配对成功）。
    """
    result = []
    for path in sorted((ENVELOPE_DIR / site).glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            env = json.load(fh)
        lineage_path = LINEAGE_DIR / site / path.name
        if lineage_path.exists():
            with lineage_path.open(encoding="utf-8") as fh:
                env["_lineage_file"] = json.load(fh)
        result.append(env)
    return result


@pytest.fixture
def envelope_to_parsed():
    """返回「信封 dict → 质检输入 dict」的转换函数。"""
    return _envelope_to_parsed


@pytest.fixture
def real_envelopes():
    """返回「站点名 → 该站真实信封列表」的加载函数。"""
    return _load_envelopes
