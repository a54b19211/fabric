"""证据类型的唯一注册表：来源目录（collection.catalog）、加工画像（processing.profile）和
业务验收（contracts.acceptance）都只引用这里，不各自另写一套词表。

三层东西分开列，不混用：
- 大类（CATEGORIES）：v3 1.4 的十二类业务证据。业务验收的 required_evidence 只能用大类。
- 细类（FINE_TYPES）：来源目录和画像实际使用的类型，每个细类写明映射到哪个大类。
  一个来源可以覆盖多个细类，也就可以覆盖多个大类。
  没有合适大类的细类（指南、药物数据库、统计测量、内部台账、资助信息、综述等）保留原细类，
  映射标 unmapped（待扩展）：不硬塞成原始研究，也不删掉。
  类型本身判不出来的细类（出版类型未知的文献记录、未判定类别的上传原件）映射标 unknown。
  细类与大类同名，表示这个细类就是该大类本身、没有再细分，映射必须指向同名大类。
- 内容覆盖层级（COVERAGE）：元数据、摘要、全文（以及登记结果表、附件）。这是“读到了多少”，
  不是“这是什么证据”，只能写在来源目录的 coverage 里，不能当细类或大类。

另有两种文档角色（DOC_ROLES）：附件、documents 库存冻结件。它们是画像里的 doc_type，
但不是证据类型：附件的大类随所属来源，库存冻结件的大类未判定。

纯数据与纯函数，不导入采集、加工或验收模块，避免循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass

# v3 1.4 的十二类，顺序与原文一致；英文 slug 沿用业务验收原有写法，中文名给报告和人读
CATEGORIES: dict[str, str] = {
    "registry": "登记",
    "regulatory_decision": "监管决定",
    "primary_research": "原始研究",
    "preprint": "预印本",
    "conference": "会议",
    "company_disclosure": "公司披露",
    "financial": "财务",
    "policy": "政策",
    "official_notice": "官方通报",
    "media": "媒体",
    "social": "社媒",
    "patent": "专利",
}

UNMAPPED = "unmapped"  # 细类已知，但十二类里没有合适的大类（待扩展）
UNKNOWN = "unknown"  # 细类本身判不出来，或根本不是注册过的细类


@dataclass(frozen=True)
class FineType:
    name: str
    # 映射到的大类 slug；没有合适大类写 UNMAPPED，类型本身判不出来写 UNKNOWN
    category: str
    # 画像“来源可信度”维度给的类别；None 表示注册表不给类别（画像写 unknown，不猜）
    credibility: str | None
    note: str = ""


FINE_TYPES: dict[str, FineType] = {
    # 登记
    "registry_record": FineType("临床试验登记记录", "registry", "official_registry"),
    # 监管决定
    "regulatory_approval": FineType("批准资料", "regulatory_decision", "regulatory_document"),
    "regulatory_letter": FineType("监管信函（如 FDA CRL）", "regulatory_decision", "regulatory_document"),
    "regulatory_label": FineType("获批说明书", "regulatory_decision", "regulatory_document",
                                 "说明书是批准内容的一部分；说明书覆盖不能冒充批准资料覆盖"),
    "regulatory_notice": FineType("监管机构公告/通知/召回/安全通讯", "regulatory_decision",
                                  "regulatory_document",
                                  "不是每条公告都含决定本身，是否为决定按原文判断"),
    # 原始研究与其他文献记录（文献记录的细类由画像按原件自身的出版类型给出）
    "primary_research": FineType("原始研究论文", "primary_research", "indexed_literature"),
    "review": FineType("综述", UNMAPPED, "indexed_literature", "综述不是原始研究，十二类里没有对应大类"),
    "literature_other": FineType("其他文献（社论、通信等）", UNMAPPED, "indexed_literature"),
    "literature_unknown_type": FineType("出版类型未知的文献记录", UNKNOWN, "indexed_literature",
                                        "原件没给出版类型，不推断为原始研究"),
    # 预印本、会议
    "preprint": FineType("预印本", "preprint", "preprint_not_peer_reviewed"),
    "conference_abstract": FineType("会议摘要", "conference", None),
    # 公司披露、财务
    "company_disclosure": FineType("企业自披露（新闻稿、IR）", "company_disclosure", "company_filing",
                                   "企业自披露，不是独立无偏来源"),
    "corporate_filing": FineType("法定公告与定期报告", "company_disclosure", "company_filing"),
    "filing_fact": FineType("申报文件中的标准化财务事实（XBRL）", "financial", "company_filing"),
    # 政策、官方通报
    "policy": FineType("政策文件", "policy", "official_notice"),
    "official_notice": FineType("官方通报", "official_notice", "official_notice"),
    # 媒体、社媒
    "media": FineType("媒体报道", "media", "media_report"),
    "industry_media": FineType("行业媒体", "media", "media_report", "行业媒体不是独立证实来源"),
    "social": FineType("社媒", "social", "unverified"),
    # 专利
    "patent": FineType("专利", "patent", None),
    # ——没有合适大类、保留原细类的（unmapped，待扩展）——
    "guideline": FineType("临床指南/指导原则", UNMAPPED, "guideline_body"),
    "drug_database": FineType("药物数据库条目", UNMAPPED, "curated_database"),
    "measurement": FineType("统计/测量数据（价格、供需、执行结果）", UNMAPPED, None),
    "internal_ledger": FineType("授权内部台账", UNMAPPED, None),
    "funding": FineType("公开的基金资助信息", UNMAPPED, None),
    # ——类别本身未判定的（unknown）——
    "uploaded_document": FineType("本地导入的上传原件", UNKNOWN, None,
                                  "上传者提供，类别和许可都要按原件另判，不推断"),
}

# 内容覆盖层级：metadata → abstract → fulltext；results、attachments 是附属部分
COVERAGE: dict[str, str] = {
    "metadata": "元数据",
    "abstract": "摘要",
    "fulltext": "全文",
    "results": "登记结果表",
    "attachments": "附件",
}

# 画像 doc_type 里的文档角色，不是证据类型
DOC_ROLES: dict[str, str] = {
    "attachment": "附件：大类随所属来源，来源只映射到一个大类时取它，否则 unknown",
    "stored_document": "documents 库存冻结件：大类未判定（unknown）",
}


def category_of(fine_type: str) -> str:
    """细类映射到的大类；没有合适大类返回 unmapped，不是注册过的细类返回 unknown。"""
    t = FINE_TYPES.get(fine_type)
    return t.category if t else UNKNOWN


def categories_of(fine_types) -> list[str]:
    """一组细类覆盖的大类（保持顺序、去重）；unmapped / unknown 照样列出，不丢。"""
    out: list[str] = []
    for f in fine_types:
        c = category_of(f)
        if c not in out:
            out.append(c)
    return out


def unmapped_fine_types() -> list[str]:
    return [k for k, t in FINE_TYPES.items() if t.category == UNMAPPED]


def check_registry() -> None:
    """注册表自身的一致性。导入时就跑，不合格直接失败，不等到下游才发现。"""
    errors: list[str] = []
    allowed = set(CATEGORIES) | {UNMAPPED, UNKNOWN}
    for slug, t in FINE_TYPES.items():
        if t.category not in allowed:
            errors.append(f"细类 {slug} 映射到未知大类 {t.category}")
        if slug in CATEGORIES and t.category != slug:
            errors.append(f"细类 {slug} 与大类同名，必须映射到同名大类")
        if slug in COVERAGE:
            errors.append(f"{slug} 是内容覆盖层级，不能当细类")
    for name, table in (("大类", CATEGORIES), ("文档角色", DOC_ROLES)):
        clash = sorted(set(table) & set(COVERAGE))
        if clash:
            errors.append(f"{name}与覆盖层级重名：{clash}")
    clash = sorted(set(DOC_ROLES) & (set(FINE_TYPES) | set(CATEGORIES)))
    if clash:
        errors.append(f"文档角色与证据类型重名：{clash}")
    if errors:
        raise ValueError("；".join(errors))


check_registry()
