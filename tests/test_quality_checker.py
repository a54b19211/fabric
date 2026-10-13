"""
T4 测试 —— 锁定 check_quality() 多维质量评分行为。

注意：T1 阶段本文件曾锁定"仅按正文长度判定的布尔闸门"；T4 已将其重写为
5 维度打分 → 加权总分 → 高/中/低/隔离 四级路由。下列断言已同步为修正后行为
（这是预期的"先存档、后改造"动作，并非测试写错）。

说明（对应开题报告口径）：原"质量判据仅 markdown 长度阈值"属前期探索性原型暴露的不足，
本测试现在锁定的是拟设计的多维质量评分模型。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core_engine.quality_checker import (
    check_quality,
    _DIM_WEIGHTS,
    _score_dimensions,
    _route_level,
)


def _base_parsed(markdown="这是一段足够长的正文内容，" * 20, uuid="u-qc-001", **over):
    d = {
        "uuid": uuid,
        "markdown": markdown,
        "structured_data": {},
        "source_url": "https://example.com/doc",
        "source_site": "example.com",
        "lineage": {},
    }
    d.update(over)
    return d


def test_short_markdown_fails():
    """正文过短（< 100 字符）→ 综合分低于隔离阈值 → 隔离、不通过。"""
    res = check_quality.fn(_base_parsed("短"))
    assert res["quality_passed"] is False
    assert res["quality_level"] == "隔离"
    assert any("过短" in issue for issue in res["quality_issues"])


def test_long_markdown_passes():
    """正文足够长（>= 100 字符）但无结构化/DOI → 落在"低"，仍非隔离即通过。"""
    long_md = "这是一段足够长的正文内容，" * 20  # 260 字符
    res = check_quality.fn(_base_parsed(long_md))
    assert res["quality_passed"] is True
    assert res["quality_level"] != "隔离"
    # 多维模型会列出维度缺口，issues 不再为空（与原单长度判定不同）
    assert isinstance(res["quality_issues"], list)


def test_quality_step_recorded_in_lineage():
    """quality 步骤会在字段级血缘中留下记录。"""
    long_md = "足够长的正文内容。" * 30
    res = check_quality.fn(_base_parsed(long_md))
    recorded_steps = [
        entry["step"]
        for entries in res["lineage"].values()
        for entry in entries
    ]
    assert "quality" in recorded_steps


def test_dimension_aggregation_formula():
    """加权总分 = Σ(权重 × 维度分)；quality_score 与维度拆解一致。"""
    p = _base_parsed(
        title="阿司匹林说明",
        structured_data={"drug": "阿司匹林"},
        doi="10.1234/abc",
        source_site="example.com",
        publish_date="2024-05-01",
    )
    res = check_quality.fn(p)
    dims = _score_dimensions(p)
    expected = round(sum(_DIM_WEIGHTS[d] * dims[d] for d in dims), 4)
    assert res["quality_score"] == expected
    # 维度拆解回写一致
    assert res["quality_dimensions"]["authority"] == round(dims["authority"], 4)


def test_route_level_boundaries():
    """纯路由函数：越过 0.60 / 0.40 临界点时等级切换。"""
    assert _route_level(0.601) == "中"
    assert _route_level(0.599) == "低"
    assert _route_level(0.399) == "隔离"
    assert _route_level(0.801) == "高"


def test_level_boundary_low_to_medium():
    """真实输入跨 0.60 临界：仅差一个结构化字段，从"低"翻到"中"。"""
    # 低：长正文 + DOI（权威 0.7），无结构化/标题
    low = check_quality.fn(_base_parsed(doi="10.1234/abc"))
    # 中：在"低"基础上补结构化字段
    medium = check_quality.fn(
        _base_parsed(doi="10.1234/abc", structured_data={"drug": "阿司匹林"})
    )
    assert low["quality_level"] == "低"
    assert medium["quality_level"] == "中"
    assert medium["quality_score"] > low["quality_score"] >= 0.40


def test_high_quality_doc():
    """高质输入（DOI+期刊+结构化+标题+近日期）→ 高，且通过。"""
    p = _base_parsed(
        title="临床研究报告",
        structured_data={"drug": "阿司匹林", "indication": "解热"},
        doi="10.1234/abc",
        journal_name="Nature Medicine",
        source_site="example.com",
        publish_date="2024-05-01",
    )
    res = check_quality.fn(p)
    assert res["quality_passed"] is True
    assert res["quality_level"] == "高"
    assert res["quality_score"] >= 0.80
