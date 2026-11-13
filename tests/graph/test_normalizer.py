"""graph.disambiguation.normalizer（Step 0 名称规范化）的单元测试。"""
from graph.disambiguation.normalizer import (
    _remove_company_suffix,
    generate_candidate_names,
    normalize_name,
)


class TestNormalizeName:
    def test_strips_and_lowercases(self):
        assert normalize_name("  AstraZeneca  ") == "astrazeneca"

    def test_fullwidth_to_halfwidth(self):
        # 全角 AＢＣ → 半角 abc
        assert normalize_name("ＡＢＣ") == "abc"

    def test_chinese_punctuation_unified(self):
        # 全角括号 → 半角括号
        assert normalize_name("（测试）") == "(测试)"

    def test_whitespace_normalized(self):
        assert normalize_name("A   B") == "a b"

    def test_trailing_punctuation_stripped(self):
        assert normalize_name("ABC.") == "abc"

    def test_fullwidth_space(self):
        # 全角空格 U+3000 → 半角空格
        assert normalize_name("A\u3000B") == "a b"


class TestRemoveCompanySuffix:
    def test_chinese_suffix(self):
        assert _remove_company_suffix("某某股份有限公司") == "某某"

    def test_chinese_limited(self):
        assert _remove_company_suffix("某某有限责任公司") == "某某"

    def test_english_inc(self):
        assert _remove_company_suffix("AstraZeneca Inc.") == "AstraZeneca"

    def test_english_ltd(self):
        assert _remove_company_suffix("GlaxoSmithKline Ltd") == "GlaxoSmithKline"

    def test_no_suffix_unchanged(self):
        assert _remove_company_suffix("AstraZeneca") == "AstraZeneca"


class TestGenerateCandidateNames:
    def test_original_name_first(self):
        names = generate_candidate_names("阿斯利康", "Company")
        assert names[0] == "阿斯利康"

    def test_with_entity_data(self):
        data = {"name": "阿斯利康", "name_en": "AstraZeneca", "aliases": ["AZ"]}
        names = generate_candidate_names("阿斯利康", "Company", data)
        assert "AstraZeneca" in names
        assert "AZ" in names

    def test_dedup(self):
        # data.name 与原始名相同，只应出现一次
        data = {"name": "阿斯利康"}
        names = generate_candidate_names("阿斯利康", "Company", data)
        assert names.count("阿斯利康") == 1

    def test_company_suffix_removed(self):
        names = generate_candidate_names("阿斯利康制药有限公司", "Company")
        assert "阿斯利康制药" in names

    def test_non_company_no_suffix_removal(self):
        names = generate_candidate_names("阿斯利康制药有限公司", "Drug")
        assert "阿斯利康制药有限公司" in names
        assert "阿斯利康制药" not in names

    def test_ignores_empty_and_non_str(self):
        data = {"name": "", "name_en": None, "aliases": ["AZ", None, ""]}
        names = generate_candidate_names("阿斯利康", "Company", data)
        assert None not in names
        assert "" not in names
        assert "None" not in names  # str(None) 不应被当成候选名
        assert "AZ" in names
