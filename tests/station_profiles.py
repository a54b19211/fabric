"""
按站点族的真实样本声明表（fixture 数据源，不含测试逻辑）。

数据来源：data/json_envelopes/ 下 15 个站点 × 10 条真实采集信封（2026-09-20 采集），
以及配对的 data/lineage/<site>/<uuid>.json（150/150 配对成功）。

本表的作用有三个：
  1. 把「每组站点该有什么」写成可执行声明，供 test_station_fixtures.py 参数化；
  2. 锁定当前实测基线（分数 / 权威分 / doc_type），数据漂移时测试会立刻发现；
  3. 登记已知缺陷（KNOWN_DEFECTS），修复后对应 xfail 会转为 XPASS 并让测试失败，
     从而强制更新本表——缺陷不会悄悄消失，也不会悄悄新增。

⚠️ 口径：表中所有数值都是「当前真实表现」，不是理想值。
   `expect_official` 才是应然判断；两者不一致的地方就是缺陷。

族划分依据 task_doc/T1-T7_按站点细化方案.md §1。
"""

# ── 站点族 ─────────────────────────────────────────────────────────
FAMILIES = {
    "A": "监管信函/决定与监管数据",
    "B": "文献",
    "C": "试验登记",
    "D": "中国政务公告",
    "E": "商业资讯与行业",
}

# ── 逐站声明 ───────────────────────────────────────────────────────
# 字段说明：
#   authority_now  实测权威分（calculate_confidence 的 score）
#   expect_official 按来源性质应为官方/一手来源（True 但 authority_now 低 = 白名单缺陷）
#   score_now      首条信封经 _envelope_to_parsed + check_quality 的总分（基线快照）
#   level_now      首条信封的质量等级
#   station_fields 首条信封经血缘补齐后的站点特有字段名（不含 title/publish_date/
#                  category/summary 四个通用字段）
#   title_empty    10 条中标题为空的数量（0 表示正常）
STATION_PROFILES = [
    # ── A 监管类（已有 FDA 官方 API 形态；CRL / EMA 无真实样本）──
    {
        "site": "fda_api_drug_label",
        "family": "A",
        "label": "FDA 药品标签 API",
        "doc_type_now": "drug_label",
        "language": "en",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.627,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 9,
    },
    {
        "site": "fda_api_enforcement",
        "family": "A",
        "label": "FDA 召回执法 API",
        "doc_type_now": "drug_recall",
        "language": "en",
        "authority_now": 0.95,
        "expect_official": True,
        # 2026-10-27 更正：原记 0.708 是 authority=0.30 时代的旧值（阶段0 补 fda.gov
        # 权威分后漏更新），与本行 authority_now 自相矛盾。现按实测 0.8725 更正。
        "score_now": 0.873,
        "level_now": "高",
        "station_fields": [],
        "title_empty": 0,
    },
    # ── B 文献 ──
    {
        "site": "pubmed",
        "family": "B",
        "label": "PubMed 文献",
        "doc_type_now": "literature",
        "language": "en",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.948,
        "level_now": "高",
        "station_fields": ["fulltext_status"],
        "title_empty": 0,
    },
    # ── C 试验登记 ──
    {
        "site": "clinicaltrials",
        "family": "C",
        "label": "ClinicalTrials.gov",
        "doc_type_now": "clinical_trial",
        "language": "en",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.902,
        "level_now": "高",
        "station_fields": ["enrollment"],
        "title_empty": 0,
    },
    {
        "site": "chictr",
        "family": "C",
        "label": "中国临床试验注册中心",
        "doc_type_now": "clinical_trial",
        "language": "zh",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.948,
        "level_now": "高",
        "station_fields": [
            "regno", "reg_status", "study_type", "study_design", "objective",
            "intervention", "inclusion_criteria",
        ],
        "title_empty": 0,
    },
    # ── D 中国政务 ──
    {
        "site": "nmpa",
        "family": "D",
        "label": "国家药监局",
        "doc_type_now": "regulatory_notice",
        "language": "zh",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.782,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 0,
    },
    {
        "site": "cde",
        "family": "D",
        "label": "药审中心",
        "doc_type_now": "regulatory_notice",
        "language": "zh",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.782,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 0,
    },
    # ── E 商业资讯与行业 ──
    {
        "site": "fiercepharma",
        "family": "E",
        "label": "Fierce Pharma",
        "doc_type_now": "industry_news",
        "language": "en",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.733,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 4,
    },
    {
        "site": "nccn",
        "family": "E",
        "label": "NCCN 指南",
        "doc_type_now": "unknown",
        "language": "en",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.782,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 0,
    },
    {
        "site": "roche",
        "family": "E",
        "label": "罗氏新闻稿",
        "doc_type_now": "industry_news",
        "language": "en",
        "authority_now": 0.95,
        "expect_official": True,
        # 0.733 是正确值：标题为空（D7）使 completeness/accuracy 各降一半。
        # 注意核算时必须经 _load_envelopes 挂上 _lineage_file，否则会用信封内的
        # 占位假血缘（title/drug_name 等 4 键）把分数虚高到 0.8975。
        "score_now": 0.733,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 10,
    },
    {
        "site": "hkex",
        "family": "E",
        "label": "港交所披露易",
        "doc_type_now": "company_announcement",
        "language": "zh",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.708,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 0,
    },
    {
        "site": "phirda_industry",
        "family": "E",
        "label": "药促会·行业",
        "doc_type_now": "industry_news",
        "language": "zh",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.782,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 0,
    },
    {
        "site": "phirda_policy",
        "family": "E",
        "label": "药促会·政策",
        "doc_type_now": "industry_news",
        "language": "zh",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.782,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 0,
    },
    {
        "site": "phirda_rd",
        "family": "E",
        "label": "药促会·研发",
        "doc_type_now": "industry_news",
        "language": "zh",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.782,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 0,
    },
    {
        "site": "phirda_healthcare_reform",
        "family": "E",
        "label": "药促会·医改",
        "doc_type_now": "industry_news",
        "language": "zh",
        "authority_now": 0.95,
        "expect_official": True,
        "score_now": 0.782,
        "level_now": "中",
        "station_fields": [],
        "title_empty": 0,
    },
]


# ── 已修复缺陷（保留记录，作为论文「改造前后对照」的可复现证据）──────
# 修复后对应断言已转为常规测试（test_official_sources_reach_full_authority），
# 不再用 xfail；此处保留 before/after 权威分。
FIXED_DEFECTS = [
    {"id": "D1", "site": "clinicaltrials", "fixed_on": "2026-10-22",
     "desc": "KNOWN_SITES 缺 clinicaltrials.gov", "before": 0.30, "after": 0.95},
    {"id": "D2", "site": "pubmed", "fixed_on": "2026-10-22",
     "desc": "KNOWN_SITES 缺 pubmed.ncbi.nlm.nih.gov", "before": 0.30, "after": 0.95},
    {"id": "D3", "site": "hkex", "fixed_on": "2026-10-22",
     "desc": "KNOWN_SITES 缺 hkexnews.hk", "before": 0.30, "after": 0.95},
    {"id": "D4", "site": "nccn", "fixed_on": "2026-10-22",
     "desc": "KNOWN_SITES 缺 nccn.org", "before": 0.30, "after": 0.95},
    {"id": "D5", "site": "roche", "fixed_on": "2026-10-22",
     "desc": "KNOWN_SITES 缺 roche.com", "before": 0.30, "after": 0.95},
]


# ── 已知缺陷登记（仍未修复）─────────────────────────────────────────
# 每条对应一个 xfail(strict=True) 断言：缺陷修好后会 XPASS → 测试失败 → 强制更新本表。
# kind: scheme（代码规则缺陷，本地可修） / data（采集数据缺陷，需重采）
KNOWN_DEFECTS = [
    {
        "id": "D6",
        "site": "nccn",
        "kind": "data",
        "desc": "枚举已扩充（新增 regulatory_document / regulatory_decision / "
                "medical_insurance_catalog / guideline 4 类），但 nccn 采集端仍写 "
                "doc_type='unknown' → 该值不在合法集，T5 产生 enum 违规（非阻断）",
        "fix": "在 nccn 站点配置中把 doc_type 映射为 guideline，重采后解除",
    },
    {
        "id": "D7",
        "site": "roche",
        "kind": "data",
        "desc": "10/10 条标题为空 → T5 required:title 违规，标题字段未被抽取",
        "fix": "需重采 / 修 roche 抽取器",
    },
    {
        "id": "D8",
        "site": "fda_api_drug_label",
        "kind": "data",
        "desc": "9/10 条标题为空 → 药品标签 API 的字段映射未对齐标题字段",
        "fix": "需修 fda_api_drug_label 字段映射后重采",
    },
    {
        "id": "D9",
        "site": "fiercepharma",
        "kind": "data",
        "desc": "4/10 条标题为空 → 部分详情页标题选择器失效",
        "fix": "需重采 / 修 fiercepharma 标题选择器",
    },
    {
        "id": "D10",
        "site": "clinicaltrials",
        "kind": "data",
        "desc": "extra 仅映射 enrollment（10/10 有值），nct_id / phase / study_status / arm_groups "
                "全部未抽取 → 登记号缺失，C 族最关键的身份字段没有",
        "fix": "补 clinicaltrials 的 field_map 与 ClinicalTrialsExtra 映射后重采",
    },
    {
        "id": "D11",
        "site": "chictr",
        "kind": "data",
        "desc": "extra 抽取 8 个字段（regno/reg_status/study_type/...），但 sample_size 与 "
                "recruitment_status 为空 → 样本量、招募状态永远未知",
        "fix": "补 chictr 字段映射后重采；与任务书「未知人数不补 0」直接相关",
    },
]


# ── 待采样本（当前本地完全没有）────────────────────────────────────
# 对应任务书要求的新来源；这些站点尚无 json_envelopes，无法做真实 fixture。
MISSING_STATIONS = [
    {"site": "fda_crl", "family": "A", "why": "任务书第一项来源，配置与提取器均不存在"},
    {"site": "europe_pmc", "family": "B", "why": "任务书第二项来源，未开工"},
    {"site": "ema", "family": "A", "why": "任务书 §8 第三项来源，未开工"},
    {"site": "nhsa", "family": "D", "why": "任务书 §8 第四项来源（医保目录），未开工"},
]


def profile_of(site: str) -> dict:
    """按站点名取声明；不存在时抛 KeyError（防止拼错站点名却静默跳过）。"""
    for item in STATION_PROFILES:
        if item["site"] == site:
            return item
    raise KeyError(f"station_profiles 未登记站点: {site}")
