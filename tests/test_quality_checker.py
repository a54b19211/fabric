"""
T1 基线测试 —— 锁定 check_quality() 当前行为（仅按正文长度判定）。

这是改代码前的"快照/回归基线"：后续 T4 将重写质量评分为多维模型时，
本文件的断言需随之更新（这是预期的"先存档、后改造"动作，并非测试写错）。

说明（对应开题报告口径）：当前质量判据只有"正文长度 >= MIN_MARKDOWN_LENGTH(100)"一条，
属于前期探索性原型暴露的不足，本测试只是把该现状固化下来以便回归。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core_engine.quality_checker import check_quality


def _base_parsed(markdown, uuid="u-qc-001"):
    return {
        "uuid": uuid,
        "markdown": markdown,
        "structured_data": {},
        "source_url": "https://example.com/doc",
        "source_site": "example.com",
        "lineage": {},
    }


def test_short_markdown_fails():
    """正文过短（< 100 字符）→ 不通过，并记录"过短"问题。"""
    res = check_quality.fn(_base_parsed("短"))
    assert res["quality_passed"] is False
    assert any("过短" in issue for issue in res["quality_issues"])


def test_long_markdown_passes():
    """正文足够长（>= 100 字符）→ 通过，无问题。"""
    long_md = "这是一段足够长的正文内容，" * 20  # 13 * 20 = 260 字符
    res = check_quality.fn(_base_parsed(long_md))
    assert res["quality_passed"] is True
    assert res["quality_issues"] == []


def test_quality_step_recorded_in_lineage():
    """quality 步骤会在字段级血缘中留下记录（当前仅有 parse/clean/quality 三步埋点）。

    lineage 结构为 {字段名: [{step, op, input, output}, ...]}，
    "quality" 是各字段条目里的 step 取值，而非顶层 key。
    """
    long_md = "足够长的正文内容。" * 30
    res = check_quality.fn(_base_parsed(long_md))
    recorded_steps = [
        entry["step"]
        for entries in res["lineage"].values()
        for entry in entries
    ]
    assert "quality" in recorded_steps
