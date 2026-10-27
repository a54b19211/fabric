"""content_cleaner 三处管线缺陷回归测试。

对应 2026-10-22 清洗审计的根因：
- C7 站点 cleaner 与通用清洗互斥（if/else）→ 改为串联
- C8 `extra` 容器绕过清洗 → 纳入通用清洗
- C9 frontmatter 使用未清洗 title → 标题在通用清洗中归一
"""

import json

from config.crawler_settings import SITE_PROFILES
from core_engine import content_cleaner as cc

SITE = "adhoc_test_site"


def _parsed(**overrides) -> dict:
    base = {
        "title": "阿司匹林   肠溶片\t说明书",
        "markdown": "第一段\t带Tab\n\n\n\n第二段    多空格",
        "source_site": SITE,
        "source_url": "https://example.com/a",
        "publish_date": "2026-01-01",
        "structured_data": {},
        "extra": {},
    }
    base.update(overrides)
    return base


def _clean(parsed: dict) -> dict:
    return cc.clean_content.fn(parsed)


def _body(markdown: str) -> str:
    """去掉 frontmatter，只保留正文，避免 YAML 头干扰正文断言。"""
    if markdown.startswith("---"):
        parts = markdown.split("---", 2)
        return parts[2] if len(parts) == 3 else markdown
    return markdown


# ── C7：站点 cleaner 之后仍要过通用清洗 ────────────────────────────────────


def test_site_cleaner_output_still_gets_default_clean(monkeypatch):
    """站点 cleaner 产出的正文含 Tab/多空格/多余空行，通用清洗必须兜底。"""

    def crude_cleaner(parsed):
        parsed["markdown"] = "A     B\tC\n\n\n\nD"
        return parsed

    monkeypatch.setitem(SITE_PROFILES, SITE, {"content_cleaner": crude_cleaner})
    body = _body(_clean(_parsed())["markdown"])

    assert "\t" not in body
    assert "     " not in body
    assert "\n\n\n" not in body


def test_site_cleaner_exception_still_gets_default_clean(monkeypatch):
    """站点 cleaner 抛异常时，通用清洗照常执行（原实现只在 except 里降级）。"""

    def boom(parsed):
        raise RuntimeError("cleaner 故意失败")

    monkeypatch.setitem(SITE_PROFILES, SITE, {"content_cleaner": boom})
    out = _clean(_parsed())

    assert "\t" not in out["markdown"]
    assert "     " not in out["markdown"]


def test_no_cleaner_site_still_cleaned():
    """未配置 cleaner 的站点行为不变。"""
    out = _clean(_parsed())
    assert "\t" not in out["markdown"]


# ── C8：extra 容器纳入清洗 ────────────────────────────────────────────────


def test_top_level_extra_is_normalized():
    out = _clean(_parsed(extra={
        "drug_name": "阿司匹林\u3000\u3000片\t",
        "approval_number": "Ｈ２０２６０００１",
    }))

    assert out["extra"]["drug_name"] == "阿司匹林 片"
    assert out["extra"]["approval_number"] == "H20260001"


def test_nested_structured_extra_is_normalized():
    out = _clean(_parsed(structured_data={
        "extra": {"manufacturer": "北京   制药厂\t"},
        "nct_id": "NCT00000001",
    }))

    assert out["structured_data"]["extra"]["manufacturer"] == "北京 制药厂"
    assert out["structured_data"]["nct_id"] == "NCT00000001"


def test_extra_non_string_values_untouched():
    out = _clean(_parsed(extra={"phase": 3, "flags": ["a  b"], "note": None}))

    assert out["extra"]["phase"] == 3
    assert out["extra"]["flags"] == ["a  b"]
    assert out["extra"]["note"] is None


# ── C9：标题在清洗阶段归一，frontmatter 因此干净 ──────────────────────────


def test_top_level_title_is_cleaned():
    out = _clean(_parsed())
    assert out["title"] == "阿司匹林 肠溶片 说明书"


def test_frontmatter_title_is_cleaned():
    out = _clean(_parsed())
    assert 'title: "阿司匹林 肠溶片 说明书"' in out["markdown"]


def test_frontmatter_has_no_tab_or_multi_space():
    out = _clean(_parsed())
    fm = out["markdown"].split("---", 2)[1]

    assert "\t" not in fm
    assert "  " not in fm


# ── C10：中文全角标点必须保留 ─────────────────────────────────────────────


def test_chinese_punctuation_is_preserved():
    text = "主要目的：验证有效性，并观察不良反应；纳入标准（自筹）。"
    out = _clean(_parsed(extra={"objective": text}))

    assert out["extra"]["objective"] == text


def test_fullwidth_alnum_still_converted():
    out = _clean(_parsed(extra={"approval_number": "Ｚ２０２６０００１", "code": "ＡＢＣ１２３"}))

    assert out["extra"]["approval_number"] == "Z20260001"
    assert out["extra"]["code"] == "ABC123"


def test_full_to_half_default_keeps_legacy_behavior():
    """标识符抽取等旧调用点不传参时，行为保持整段转换。"""
    from biz_rules.cleaners import full_to_half

    assert full_to_half("Ａ１（自筹）") == "A1(自筹)"
    assert full_to_half("Ａ１（自筹）", alnum_only=True) == "A1（自筹）"


# ── 幂等性：清洗结果再洗一次不变 ──────────────────────────────────────────


def test_clean_content_is_idempotent():
    once = _clean(_parsed())
    twice = _clean(json.loads(json.dumps(once, ensure_ascii=False)))

    assert twice["markdown"] == once["markdown"]
    assert twice["title"] == once["title"]
    assert twice["extra"] == once["extra"]
    assert twice["structured_data"] == once["structured_data"]


def test_clean_content_idempotent_with_site_cleaner(monkeypatch):
    """有站点 cleaner 时也要幂等：第二次清洗不应再改变正文。"""

    def crude_cleaner(parsed):
        parsed["markdown"] = "A     B\tC"
        return parsed

    monkeypatch.setitem(SITE_PROFILES, SITE, {"content_cleaner": crude_cleaner})

    once = _clean(_parsed())
    twice = _clean(json.loads(json.dumps(once, ensure_ascii=False)))

    assert twice["markdown"] == once["markdown"]
