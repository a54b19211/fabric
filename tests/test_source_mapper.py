"""
T1 基线测试 —— 锁定 calculate_confidence() 当前行为（含"反降分"bug）。

快照目的：后续 T3（标准标识符对齐 / 来源置信度重构）修复"有期刊名反而降分"时，
test_doi_with_journal_drops_score 这条 0.65 断言需更新为更高分值。

说明（对应开题报告口径）：当前置信度是来源硬编码查表，且存在反直觉逻辑
（有 DOI 时记 0.7，若同时有期刊名反而降为 0.65），属前期探索性原型暴露的不足。
"""

from biz_rules.source_mapper import calculate_confidence


def test_known_official_site():
    """已知官方站点（fda.gov）→ Level A, 0.95。"""
    r = calculate_confidence(source_url="https://www.fda.gov/drug/xxx")
    assert r["level"] == "A"
    assert r["score"] == 0.95


def test_whitelist_journal():
    """白名单顶刊（Nature Medicine）→ Level A, 0.85。"""
    r = calculate_confidence(journal_name="Nature Medicine")
    assert r["level"] == "A"
    assert r["score"] == 0.85


def test_doi_only():
    """仅 DOI（无期刊名）→ Level B, 0.7。"""
    r = calculate_confidence(doi="10.1234/abc")
    assert r["level"] == "B"
    assert r["score"] == 0.7


def test_doi_with_journal_drops_score():
    """当前 bug：有 DOI 且有期刊名（非白名单）→ 0.65（比仅 DOI 的 0.7 还低）。"""
    r = calculate_confidence(doi="10.1234/abc", journal_name="Journal of Unrelated Studies")
    assert r["level"] == "B"
    assert r["score"] == 0.65


def test_journal_only():
    """仅期刊名（非白名单，无 DOI）→ Level B, 0.5。"""
    r = calculate_confidence(journal_name="Journal of Unrelated Studies")
    assert r["level"] == "B"
    assert r["score"] == 0.5


def test_no_signal():
    """三无文件（无 URL / 无 DOI / 无期刊名）→ Level C, 0.3。"""
    r = calculate_confidence()
    assert r["level"] == "C"
    assert r["score"] == 0.3
