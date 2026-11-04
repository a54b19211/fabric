"""清洗层编号规范化（processing.identifiers）：从正文里抽 NCT / CTR / ChiCTR / PMID / PMCID / DOI /
批准文号 / 受理号 / 股票代码，并规范成统一写法。这些编号之后只用作同源链接和同一性候选，错连比漏连代价大，
所以负例（长得像但不是）和正例一样多。纯函数，不联网、不连库。"""

import unicodedata

import pytest

from processing import identifiers as ids


def hits(text):
    return [(h["kind"], h["value"]) for h in ids.extract(text)]


def assert_offsets(text):
    result = ids.extract(text)
    for h in result:
        assert text[h["start"]:h["end"]] == h["raw"], h
        # raw 必须能被 normalize 还原成同一个 value：保证两个入口规则一致
        assert ids.normalize(h["kind"], h["raw"]) == h["value"], h
    starts = [h["start"] for h in result]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)
    return result


def test_processor_version():
    assert ids.PROCESSOR_VERSION == "datafabric.identifiers.v1"


# ---------- NCT ----------

def test_nct_chinese_and_english():
    text = "本研究已在 ClinicalTrials.gov 登记（NCT01234567），入组 300 例。"
    assert hits(text) == [("nct", "NCT01234567")]
    assert hits("The trial was registered as nct04368728.") == [("nct", "NCT04368728")]
    assert hits("登记号NCT04368728的试验") == [("nct", "NCT04368728")]


@pytest.mark.parametrize("text", [
    "XNCT01234567", "NCT012345678", "NCT0123456", "NCT01234567A", "NCT-01234567", "ANCT01234567B",
])
def test_nct_lookalikes(text):
    assert [h for h in hits(text) if h[0] == "nct"] == []


# ---------- CTR ----------

def test_ctr_chinese_and_english():
    assert hits("药物临床试验登记号：CTR20231234。") == [("ctr", "CTR20231234")]
    assert hits("Registered at chinadrugtrials.org.cn (ctr20190876).") == [("ctr", "CTR20190876")]


@pytest.mark.parametrize("text", [
    "CTR2023123", "CTR202312345", "ACTR20231234", "CTR12345678", "CTR20231234X",
])
def test_ctr_lookalikes(text):
    assert hits(text) == []


# ---------- ChiCTR ----------

def test_chictr_old_and_new_style():
    assert hits("中国临床试验注册中心注册号 ChiCTR-IOR-17012345。") == [("chictr", "ChiCTR-IOR-17012345")]
    assert hits("Registered with chictr-trc-12002345 and CHICTR2000034567.") == [
        ("chictr", "ChiCTR-TRC-12002345"), ("chictr", "ChiCTR2000034567")]
    assert hits("注册号ChiCTR2000034567，") == [("chictr", "ChiCTR2000034567")]


@pytest.mark.parametrize("text", [
    "ChiCTR200003456", "ChiCTR20000345678", "ChiCTR-IOR-1701234", "ChiCTR-IO-17012345", "XChiCTR2000034567",
])
def test_chictr_lookalikes(text):
    assert hits(text) == []


def test_chictr_new_style_is_not_ctr():
    # ChiCTR2000034567 里的 CTR 后面是 10 位，且前面紧挨字母，不能再冒出一个 ctr
    assert [k for k, _ in hits("ChiCTR2000034567")] == ["chictr"]


# ---------- PMID ----------

def test_pmid_labelled():
    assert hits("参见文献（PMID: 32678530）。") == [("pmid", "32678530")]
    assert hits("PMID 12345678 and PubMed ID: 7654321") == [("pmid", "12345678"), ("pmid", "7654321")]
    assert hits("pmid：123") == [("pmid", "123")]


@pytest.mark.parametrize("text", [
    "12345678", "共入组 32678530 人", "PMID: 123456789", "XPMID: 12345678", "PMID: 0", "PMID:abc",
])
def test_pmid_lookalikes(text):
    assert hits(text) == []


# ---------- PMCID ----------

def test_pmcid():
    assert hits("全文见 PMC7366567。") == [("pmcid", "PMC7366567")]
    assert hits("Full text: pmc1234567") == [("pmcid", "PMC1234567")]


@pytest.mark.parametrize("text", ["XPMC1234567", "PMC", "PMC0123", "PMC123456789", "PMC12345A"])
def test_pmcid_lookalikes(text):
    assert hits(text) == []


# ---------- DOI ----------

def test_doi_plain_and_prefixed():
    assert hits("doi:10.1056/NEJMoa2034577") == [("doi", "10.1056/nejmoa2034577")]
    assert hits("DOI：10.1016/S0140-6736(20)30183-5。") == [("doi", "10.1016/s0140-6736(20)30183-5")]
    assert hits("See https://doi.org/10.1038/s41586-020-2012-7.") == [("doi", "10.1038/s41586-020-2012-7")]
    assert hits("(http://dx.doi.org/10.1001/jama.2020.1585)") == [("doi", "10.1001/jama.2020.1585")]


def test_doi_trailing_punctuation_and_chinese():
    assert hits("文章 10.1056/NEJMoa2034577。") == [("doi", "10.1056/nejmoa2034577")]
    assert hits("(doi: 10.1056/NEJMoa2034577)") == [("doi", "10.1056/nejmoa2034577")]
    assert hits("10.1056/NEJMoa2034577发表于新英格兰医学杂志") == [("doi", "10.1056/nejmoa2034577")]
    assert hits("见10.1056/NEJMoa2034577）和其他") == [("doi", "10.1056/nejmoa2034577")]
    assert hits("见【10.1056/NEJMoa2034577】") == [("doi", "10.1056/nejmoa2034577")]
    assert hits('"10.1056/NEJMoa2034577",') == [("doi", "10.1056/nejmoa2034577")]
    assert hits("<https://doi.org/10.1056/NEJMoa2034577>") == [("doi", "10.1056/nejmoa2034577")]


def test_doi_balanced_parentheses_kept():
    # Lancet 的 DOI 中间带括号，配对的括号是 DOI 本身的一部分
    assert hits("(10.1016/S0140-6736(20)30183-5)") == [("doi", "10.1016/s0140-6736(20)30183-5")]


def test_doi_fullwidth_semicolon_does_not_swallow_next_identifier():
    text = "DOI 10.1056/NEJMoa2034577；NCT04368728"
    assert hits(text) == [("doi", "10.1056/nejmoa2034577"), ("nct", "NCT04368728")]
    assert_offsets(text)


def test_doi_url_query_not_swallowed():
    assert hits("https://doi.org/10.1056/NEJMoa2034577?via=ihub") == [("doi", "10.1056/nejmoa2034577")]


@pytest.mark.parametrize("text", [
    "10.123/abc",          # 注册者代码只有 3 位
    "110.1056/abc",        # 前面还有数字
    "10.1056/",            # 没有后缀
    "比值为 10.2345/67",    # 没有 doi 前缀、后缀全是数字：更像算式
    "版本 v10.1056/abc",
])
def test_doi_lookalikes(text):
    assert hits(text) == []


def test_doi_sici_with_angle_brackets_not_emitted():
    # SICI 风格 DOI 带尖括号，截断会得到错的 DOI；拿不准就不出
    assert hits("10.1002/(SICI)1097-4636(199601)30:1<1::AID-JBM1>3.0.CO;2-O") == []


# ---------- 批准文号 ----------

def test_nmpa_approval():
    assert hits("该药批准文号为国药准字H20234567，") == [("nmpa_approval", "国药准字H20234567")]
    assert hits("Approval No. 国药准字 Z11020001 (TCM)") == [("nmpa_approval", "国药准字Z11020001")]
    assert hits("国药准字S20190012；国药准字J20180034") == [
        ("nmpa_approval", "国药准字S20190012"), ("nmpa_approval", "国药准字J20180034")]


@pytest.mark.parametrize("text", [
    "国药准字X20234567", "国药准字H2023456", "国药准字H202345678", "H20234567", "国药准字  H20234567",
])
def test_nmpa_lookalikes(text):
    assert hits(text) == []


# ---------- 受理号 ----------

def test_cde_acceptance_standalone_and_labelled():
    assert hits("CDE 承办了 CXHL2300123、JXHL2100012 两个申请") == [
        ("cde_acceptance", "CXHL2300123"), ("cde_acceptance", "JXHL2100012")]
    assert hits("Acceptance No. cyhs2101234 was approved.") == [("cde_acceptance", "CYHS2101234")]
    # 紧贴中文、不是独立词：要靠前面的“受理号”撑腰
    assert hits("受理号为CXSL2300456的上市申请") == [("cde_acceptance", "CXSL2300456")]
    assert hits("本次受理号：JYHB2200345号") == [("cde_acceptance", "JYHB2200345")]


@pytest.mark.parametrize("text", [
    "本品CXHL2300123获批",       # 紧贴中文且附近没有“受理号”
    "ACXHL2300123", "CXHL23001234", "CXHL230012", "CAHL2300123", "CXHA2300123",
    "受理号说明很长很长很长很长很长很长很长很长很长CXHL2300123获批",  # “受理号”离得太远
])
def test_cde_lookalikes(text):
    assert hits(text) == []


# ---------- 股票代码 ----------

def test_stock_suffix_forms():
    assert hits("贵州茅台（600519.SH）") == [("stock", "600519.SH")]
    assert hits("Ping An Bank 000001.sz, SMIC 688981.SH, 830799.BJ") == [
        ("stock", "000001.SZ"), ("stock", "688981.SH"), ("stock", "830799.BJ")]


def test_stock_hk_normalised_to_four_digits():
    assert hits("腾讯控股（00700.HK）、阿里巴巴 09988.HK、0700.HK") == [
        ("stock", "0700.HK"), ("stock", "9988.HK"), ("stock", "0700.HK")]
    assert ids.normalize("stock", "01801.HK") == "1801.HK"
    assert ids.normalize("stock", "13800.HK") == "13800.HK"


def test_stock_prefix_forms():
    assert hits("行情 SH600519 与 sz000001") == [("stock", "600519.SH"), ("stock", "000001.SZ")]


def test_stock_labelled_inference():
    assert hits("公司股票代码：600519，") == [("stock", "600519.SH")]
    assert hits("证券代码:300750") == [("stock", "300750.SZ")]
    assert hits("证券代码：000858") == [("stock", "000858.SZ")]
    assert hits("股票代码：830799") == [("stock", "830799.BJ")]
    assert hits("股票代码：920001") == [("stock", "920001.BJ")]


def test_stock_labelled_with_suffix_is_single_hit():
    assert hits("证券代码：600519.SH") == [("stock", "600519.SH")]
    assert hits("股票代码：SH600519") == [("stock", "600519.SH")]


@pytest.mark.parametrize("text", [
    "600519",                   # 没有交易所也没有标签
    "共 600519 元",
    "000001.SH",                # 上证指数，不是股票
    "399001.SZ",                # 深证成指
    "600519.SZ",                # 前缀和交易所对不上
    "510300.SH",                # ETF
    "股票代码：510300",          # 基金代码，推断不出股票
    "股票代码：6005190",
    "X600519.SH", "600519.SHX", "ASH600519", "SH6005190",
    "700.HK", "00000.HK",
])
def test_stock_lookalikes(text):
    assert hits(text) == []


# ---------- 全角、偏移、混合 ----------

def test_fullwidth_characters_map_one_to_one_like_nfkc():
    # 只按字符映射全角 ASCII 和全角空格，和 NFKC 的结果一致，且一个字符对一个字符，偏移不变
    for code in list(range(0xFF01, 0xFF5F)) + [0x3000]:
        ch = chr(code)
        assert unicodedata.normalize("NFKC", ch) == ids._fold(ch)
        assert len(ids._fold(ch)) == 1
    assert len(ids._fold("ＮＣＴ０１２３４５６７：中文")) == len("ＮＣＴ０１２３４５６７：中文")


def test_fullwidth_identifiers_with_original_offsets():
    text = ("试验编号ＮＣＴ０１２３４５６７，ＰＭＩＤ：１２３４５，"
            "股票代码：６００５１９，国药准字　Ｈ２０２３４５６７")
    result = assert_offsets(text)
    assert [(h["kind"], h["value"]) for h in result] == [
        ("nct", "NCT01234567"), ("pmid", "12345"), ("stock", "600519.SH"),
        ("nmpa_approval", "国药准字H20234567")]
    assert result[0]["raw"] == "ＮＣＴ０１２３４５６７"
    assert result[0]["start"] == 4


def test_fullwidth_doi():
    text = "ＤＯＩ：１０．１０５６／ＮＥＪＭｏａ２０３４５７７。"
    result = assert_offsets(text)
    assert [(h["kind"], h["value"]) for h in result] == [("doi", "10.1056/nejmoa2034577")]


def test_mixed_paragraph_in_order_with_offsets():
    text = (
        "某公司（股票代码：688981；港股 00981.HK）宣布，其创新药（受理号：CXHL2300123）已获批开展临床，"
        "登记号 CTR20231234，同时在 ClinicalTrials.gov 登记 NCT05012345 和 ChiCTR2100045678。"
        "前期结果发表于 https://doi.org/10.1016/S1470-2045(21)00123-4（PMID: 33861234，PMC8123456）。"
        "该公司另一产品批准文号国药准字H20210001。"
    )
    result = assert_offsets(text)
    assert [(h["kind"], h["value"]) for h in result] == [
        ("stock", "688981.SH"), ("stock", "0981.HK"), ("cde_acceptance", "CXHL2300123"),
        ("ctr", "CTR20231234"), ("nct", "NCT05012345"), ("chictr", "ChiCTR2100045678"),
        ("doi", "10.1016/s1470-2045(21)00123-4"), ("pmid", "33861234"), ("pmcid", "PMC8123456"),
        ("nmpa_approval", "国药准字H20210001"),
    ]


def test_english_paragraph():
    text = ("Results (NCT04368728; doi:10.1056/NEJMoa2034577) were consistent with PMID 33301246 "
            "and BioNTech partner Fosun Pharma (600196.SH, 02196.HK).")
    result = assert_offsets(text)
    assert [(h["kind"], h["value"]) for h in result] == [
        ("nct", "NCT04368728"), ("doi", "10.1056/nejmoa2034577"), ("pmid", "33301246"),
        ("stock", "600196.SH"), ("stock", "2196.HK")]


@pytest.mark.parametrize("text", [
    "12345678", "20231234", "日期 2023-12-34，编号 20231234。", "电话 010-12345678", "", "   ",
    "10.5%", "IC50 = 10.1234 nM",
])
def test_plain_numbers_produce_nothing(text):
    assert ids.extract(text) == []


def test_identifier_inside_doi_is_not_duplicated():
    # 嵌在 DOI 后缀里的 NCT 是 DOI 的一部分，同一位置只出一个结果
    assert hits("https://doi.org/10.1186/ISRCTN-NCT01234567") == [("doi", "10.1186/isrctn-nct01234567")]


# ---------- normalize ----------

@pytest.mark.parametrize("kind,raw,value", [
    ("nct", "nct01234567", "NCT01234567"),
    ("nct", " NCT01234567 ", "NCT01234567"),
    ("ctr", "ctr20231234", "CTR20231234"),
    ("chictr", "chictr-ior-17012345", "ChiCTR-IOR-17012345"),
    ("chictr", "ChiCTR2000034567", "ChiCTR2000034567"),
    ("pmid", "PMID: 32678530", "32678530"),
    ("pmid", "32678530", "32678530"),  # 调用方已声明是 PMID（结构化字段），裸数字可以
    ("pmcid", "pmc7366567", "PMC7366567"),
    ("doi", "https://doi.org/10.1056/NEJMoa2034577", "10.1056/nejmoa2034577"),
    ("doi", "doi:10.1056/NEJMoa2034577.", "10.1056/nejmoa2034577"),
    ("nmpa_approval", "国药准字 H20234567", "国药准字H20234567"),
    ("cde_acceptance", "cxhl2300123", "CXHL2300123"),
    ("stock", "00700.HK", "0700.HK"),
    ("stock", "sz000001", "000001.SZ"),
    ("stock", "股票代码：600519", "600519.SH"),
    ("nct", "ＮＣＴ０１２３４５６７", "NCT01234567"),
])
def test_normalize_valid(kind, raw, value):
    assert ids.normalize(kind, raw) == value


@pytest.mark.parametrize("kind,raw", [
    ("nct", "NCT012345678"),
    ("nct", "NCT01234567 NCT01234568"),   # 两个编号不是一个编号
    ("nct", "see NCT01234567"),
    ("ctr", "NCT01234567"),               # 种类不符
    ("pmid", "0123"),
    ("pmid", "123456789"),
    ("doi", "10.12/abc"),
    ("stock", "600519"),                  # 没有标签不推断交易所
    ("stock", "600519.SZ"),
    ("unknown_kind", "NCT01234567"),
    ("nct", ""),
])
def test_normalize_invalid(kind, raw):
    assert ids.normalize(kind, raw) is None


def test_extract_rejects_non_string():
    with pytest.raises(TypeError):
        ids.extract(None)
