"""对齐导向规范化（4.1，normalize_for_alignment）的单元测试。"""
from graph.disambiguation.normalizer import normalize_for_alignment


class TestProcessingPrefix:
    def test_chao(self):
        assert normalize_for_alignment("炒莱菔子", "Drug") == "莱菔子"

    def test_chan(self):
        assert normalize_for_alignment("燀苦杏仁", "Drug") == "苦杏仁"

    def test_mi_zhi_double_char(self):
        assert normalize_for_alignment("蜜炙甘草", "Drug") == "甘草"

    def test_jiao(self):
        assert normalize_for_alignment("焦山楂", "Drug") == "山楂"

    def test_zhi(self):
        assert normalize_for_alignment("制何首乌", "Drug") == "何首乌"

    def test_jiang_zhi_double_char(self):
        assert normalize_for_alignment("姜制半夏", "Drug") == "半夏"

    def test_tcm_formula_strips_processing(self):
        assert normalize_for_alignment("炒白芍", "TCMFormula") == "白芍"


class TestProcessingConservative:
    def test_single_char_adjuvant_not_stripped(self):
        # 单字辅料（酒/醋/盐）不入前缀表，保守不剥
        assert normalize_for_alignment("酒黄芩", "Drug") == "酒黄芩"
        assert normalize_for_alignment("醋延胡索", "Drug") == "醋延胡索"

    def test_salt_processing_double_char(self):
        # 双字辅料炮制「盐制/盐炙」正确剥离（与盐型前缀「盐酸」分层，互不误伤）
        assert normalize_for_alignment("盐制杜仲", "Drug") == "杜仲"
        assert normalize_for_alignment("盐炙黄柏", "Drug") == "黄柏"

    def test_medicinal_material_not_stripped(self):
        # 土茯苓/姜黄 是药材名，非炮制
        assert normalize_for_alignment("土茯苓", "Drug") == "土茯苓"
        assert normalize_for_alignment("姜黄", "Drug") == "姜黄"

    def test_nystatin_not_stripped(self):
        # 制霉素/制霉菌素 是 Nystatin 音译，「制」非炮制前缀
        assert normalize_for_alignment("制霉素片", "Drug") == "制霉素"
        assert normalize_for_alignment("制霉菌素", "Drug") == "制霉菌素"


class TestDosageSuffix:
    def test_long_suffix_first(self):
        assert normalize_for_alignment("阿司匹林肠溶片", "Drug") == "阿司匹林"

    def test_short_suffix(self):
        assert normalize_for_alignment("酚酞片", "Drug") == "酚酞"

    def test_capsule(self):
        assert normalize_for_alignment("阿莫西林胶囊", "Drug") == "阿莫西林"

    def test_compound_strips_dosage(self):
        assert normalize_for_alignment("对乙酰氨基酚片", "Compound") == "对乙酰氨基酚"

    def test_nasal_spray(self):
        assert normalize_for_alignment("丙酸氟替卡松鼻喷雾剂", "Drug") == "丙酸氟替卡松"

    def test_inhalation_aerosol(self):
        assert normalize_for_alignment("布地奈德福莫特罗吸入气雾剂", "Drug") == "布地奈德福莫特罗"

    def test_hollow_capsule(self):
        assert normalize_for_alignment("羟丙甲纤维素空心胶囊", "Compound") == "羟丙甲纤维素"


class TestDosageConservative:
    def test_not_end_with_dosage(self):
        # 片仔癀 以「癀」结尾，不命中「片」后缀
        assert normalize_for_alignment("片仔癀", "Drug") == "片仔癀"

    def test_min_core_length(self):
        # 核心名长度不足 MIN_CORE_LENGTH 不剥
        assert normalize_for_alignment("片", "Drug") == "片"

    def test_company_not_stripped(self):
        assert normalize_for_alignment("阿司匹林肠溶片", "Company") == "阿司匹林肠溶片"

    def test_toothpaste_not_stripped(self):
        # 「膏」单字已移除，牙膏不是药物剂型
        assert normalize_for_alignment("氟化钠牙膏", "Drug") == "氟化钠牙膏"


class TestOcr:
    def test_known_correction(self):
        assert normalize_for_alignment("S.r.1. Pharmaceutical", "Company") == "S.r.l. Pharmaceutical"


class TestTypeScoping:
    def test_tcm_formula_no_dosage(self):
        # TCMFormula 剥炮制但不剥剂型
        assert normalize_for_alignment("六味地黄丸", "TCMFormula") == "六味地黄丸"

    def test_other_type_untouched(self):
        # Indication 不在炮制/剂型范围，仅 OCR + strip
        assert normalize_for_alignment("  Brain Hemorrhage  ", "Indication") == "Brain Hemorrhage"


class TestSaltPrefix:
    def test_hydrochloride(self):
        assert normalize_for_alignment("盐酸二甲双胍", "Drug") == "二甲双胍"

    def test_sulfate(self):
        assert normalize_for_alignment("硫酸沙丁胺醇", "Drug") == "沙丁胺醇"

    def test_citrate(self):
        assert normalize_for_alignment("枸橼酸西地那非", "Drug") == "西地那非"

    def test_tartrate(self):
        assert normalize_for_alignment("酒石酸美托洛尔", "Drug") == "美托洛尔"

    def test_malate(self):
        assert normalize_for_alignment("马来酸氯苯那敏", "Drug") == "氯苯那敏"

    def test_compound_salt(self):
        assert normalize_for_alignment("盐酸二甲双胍", "Compound") == "二甲双胍"

    def test_salt_plus_dosage(self):
        # 盐型前缀 + 剂型后缀连续剥离
        assert normalize_for_alignment("盐酸二甲双胍片", "Drug") == "二甲双胍"


class TestSaltConservative:
    def test_calcium_sulfate_not_stripped(self):
        # 硫酸钙 → 剥「硫酸」得「钙」（长度1），MIN_CORE_LENGTH 拦截
        assert normalize_for_alignment("硫酸钙", "Drug") == "硫酸钙"

    def test_sodium_acetate_not_stripped(self):
        # 醋酸钠 → 剥「醋酸」得「钠」（长度1），MIN_CORE_LENGTH 拦截
        assert normalize_for_alignment("醋酸钠", "Drug") == "醋酸钠"

    def test_ferrous_lactate_not_stripped(self):
        # 乳酸亚铁 → 剥「乳酸」得「亚铁」（命中保护表），不剥
        assert normalize_for_alignment("乳酸亚铁", "Drug") == "乳酸亚铁"

    def test_not_drug_type(self):
        # Indication 不剥盐型
        assert normalize_for_alignment("盐酸二甲双胍", "Indication") == "盐酸二甲双胍"


class TestHydrateSuffix:
    def test_monohydrate(self):
        assert normalize_for_alignment("盐酸多西环素一水合物", "Drug") == "多西环素"

    def test_hydrate_keeps_metal_cation(self):
        # 金属阳离子后缀「钠」不剥，仅剥水合物
        assert normalize_for_alignment("氨苄西林钠水合物", "Drug") == "氨苄西林钠"

    def test_sesquihydrate(self):
        assert normalize_for_alignment("阿莫西林倍半水合物", "Drug") == "阿莫西林"

    def test_hydrate_min_core(self):
        # 核心名长度不足不剥
        assert normalize_for_alignment("水合物", "Drug") == "水合物"


class TestCompanySuffixAlignment:
    def test_cn_suffix(self):
        assert normalize_for_alignment("某某制药股份有限公司", "Company") == "某某制药"

    def test_en_suffix(self):
        assert normalize_for_alignment("AstraZeneca Inc.", "Company") == "AstraZeneca"

    def test_non_company_not_stripped(self):
        assert normalize_for_alignment("某某制药股份有限公司", "Drug") == "某某制药股份有限公司"
