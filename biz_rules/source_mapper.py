"""
==========================================================
  来源推断与置信度评估
  ★ 药学研究生专属文件 ★ —— 期刊白名单和打分规则都在这里
==========================================================

【使用说明】
- JOURNAL_WHITELIST: 高质量期刊名单，匹配上就给高分。想加期刊？在列表里追加就行。
- KNOWN_SITES: 已知可信网站列表。
- calculate_confidence(): 核心打分函数，输入来源信息 → 输出置信度等级和分数。
"""


# ======================== 高质量期刊白名单（Top 期刊）========================
# 匹配时会转成小写比较，所以这里大小写不敏感
# 想添加新期刊？直接在列表末尾加一行就行
JOURNAL_WHITELIST = [
    # --- 国际顶刊 ---
    "the lancet",
    "new england journal of medicine",
    "nature medicine",
    "jama",
    "bmj",
    "cell",
    "nature",
    "science",
    "nature reviews drug discovery",
    "journal of clinical oncology",
    "annals of internal medicine",
    "circulation",
    "the lancet oncology",

    # --- 药学核心期刊 ---
    "journal of medicinal chemistry",
    "drug discovery today",
    "european journal of pharmaceutical sciences",
    "international journal of pharmaceutics",
    "pharmaceutical research",
    "journal of pharmaceutical sciences",
    "molecular pharmaceutics",
    "advanced drug delivery reviews",

    # --- 中文核心 ---
    "药学学报",
    "中国药学杂志",
    "中国新药杂志",
    "中国药理学通报",
    "中国临床药理学杂志",
    "药物分析杂志",
    "中国医药工业杂志",
    "中国抗生素杂志",
]


# ======================== 已知可信官方站点 ========================
KNOWN_SITES = [
    "nmpa.gov.cn",         # 国家药监局
    "cde.org.cn",          # 药审中心
    "nhc.gov.cn",          # 国家卫健委
    "who.int",             # 世界卫生组织
    "fda.gov",             # 美国 FDA
    "ema.europa.eu",       # 欧洲药品管理局
    "pharmnet.com.cn",     # 药智网
    "drugbank.com",        # DrugBank 药物数据库
    "phirda.com",          # 中国医药创新促进会
    "fiercepharma.com",    # Fierce Pharma 行业资讯
    "chictr.org.cn",       # 中国临床试验注册中心
]


def calculate_confidence(
    source_url: str = "",
    doi: str = "",
    journal_name: str = "",
) -> dict:
    """
    三级置信度打分函数。

    【打分规则】
    - Level A（高置信 0.8~1.0）：来自官方站点，或匹配 Top 期刊
    - Level B（中置信 0.5~0.7）：提取到了 DOI 或普通期刊名
    - Level C（低置信 0.3）：三无文件（无URL、无DOI、无期刊名）

    参数:
        source_url:   数据来源 URL（如有）
        doi:          从文献中提取的 DOI（如有）
        journal_name: 从文献中提取的期刊名（如有）

    返回:
        dict: {"level": "A/B/C", "score": 0.3~1.0, "reason": "打分依据"}
    """

    # ---------- 检查 1：是否来自已知官方站点 ----------
    if source_url:
        url_lower = source_url.lower()
        for site in KNOWN_SITES:
            if site in url_lower:
                return {
                    "level": "A",
                    "score": 0.95,
                    "reason": f"来自已知官方站点: {site}",
                }

    # ---------- 检查 2：是否匹配 Top 期刊白名单 ----------
    if journal_name:
        journal_lower = journal_name.lower().strip()
        for top_journal in JOURNAL_WHITELIST:
            if top_journal.lower() in journal_lower or journal_lower in top_journal.lower():
                return {
                    "level": "A",
                    "score": 0.85,
                    "reason": f"匹配高质量期刊: {top_journal}",
                }

    # ---------- 检查 3：有 DOI → 至少是正规发表的文献 ----------
    if doi:
        score = 0.7
        reason = f"提取到 DOI: {doi}"
        # 同时有期刊名（非白名单）：信号更完整，作为独立加分项（不降分）
        if journal_name:
            score = 0.75
            reason += f"，期刊: {journal_name}"
        return {
            "level": "B",
            "score": score,
            "reason": reason,
        }

    # ---------- 检查 4：只有期刊名，没有 DOI ----------
    if journal_name:
        return {
            "level": "B",
            "score": 0.5,
            "reason": f"提取到期刊名但无 DOI: {journal_name}",
        }

    # ---------- 兜底：三无文件 ----------
    return {
        "level": "C",
        "score": 0.3,
        "reason": "未提取到可靠来源信息（无 URL / 无 DOI / 无期刊名）",
    }
