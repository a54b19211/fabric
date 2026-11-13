"""证据类型的唯一注册表：来源目录（fetch.catalog 的 evidence_types）和加工画像（clean.profile）都只引用这里，
不各自另写一套词表。

两层分开列，不混用：
- 大类（CATEGORIES）：v3 1.4 的十二类业务证据。
- 细类（FINE_TYPES）：来源目录和画像实际使用的类型，每个细类写明映射到哪个大类。
  一个来源可以覆盖多个细类，也就可以覆盖多个大类。
  没有合适大类的细类（指南、药物数据库、统计测量、内部台账、资助信息、综述等）保留原细类，
  映射标 unmapped（待扩展）：不硬塞成原始研究，也不删掉。
  类型本身判不出来的细类（出版类型未知的文献记录、未判定类别的上传原件）映射标 unknown。
  细类与大类同名，表示这个细类就是该大类本身、没有再细分，映射必须指向同名大类。
  个别细类按内容分大类（FineType.on_action）：监管机构的公告/通知只有原文写明监管动作（批准、
  拒绝、召回、撤市/暂停、警告信/执法、CRL 等）的才算监管决定，其余按它的默认大类（官方通报），
  不因出自监管机构网站就整体算“决定”。规则只在 document_category 一处。

元数据、摘要、全文是“读到了多少”，写在来源目录的 coverage 里，不是证据类型。
画像的 doc_type 另有两种文档角色：attachment（附件，大类随所属来源）、
stored_document（documents 库存冻结件，大类未判定），也不是证据类型。

纯数据与纯函数，不导入采集、加工或验收模块，避免循环依赖。
"""

from __future__ import annotations

import re
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
    # 按内容改判的大类：原文写明监管动作时取它，否则取 category；None = 不按内容改判
    on_action: str | None = None


FINE_TYPES: dict[str, FineType] = {
    # 登记
    "registry_record": FineType("临床试验登记记录", "registry", "official_registry"),
    # 监管决定（监管机构公告/通知按内容分：写明监管动作才是决定）
    "regulatory_approval": FineType("批准资料", "regulatory_decision", "regulatory_document"),
    "regulatory_letter": FineType("监管信函（如 FDA CRL）", "regulatory_decision", "regulatory_document"),
    "regulatory_label": FineType("获批说明书", "regulatory_decision", "regulatory_document",
                                 "说明书是批准内容的一部分；说明书覆盖不能冒充批准资料覆盖"),
    "regulatory_notice": FineType("监管机构公告/通知/召回/安全通讯", "official_notice", "regulatory_document",
                                  "写明监管动作的才是监管决定，一般公告/通知按官方通报",
                                  on_action="regulatory_decision"),
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

# 监管动作词（小写比对）：标题、摘要或来源给的类别字段里有这些词，按内容分的细类才改判为 on_action。
REGULATORY_ACTIONS: tuple[str, ...] = (
    # 批准 / 拒绝（含 FDA CRL）
    "批准", "核准", "准予", "默示许可", "不予注册", "不予受理", "完全回应函",
    "approv", "reject", "refus", "complete response",
    # 召回、撤市/撤销、暂停
    "召回", "撤市", "撤回", "撤销", "注销", "吊销", "暂停", "停止生产", "停止销售", "停止使用",
    "recall", "withdraw", "suspend", "revok", "revocation", "clinical hold",
    # 警告信、执法
    "警告信", "责令", "行政处罚", "warning letter", "enforcement action", "import alert",
)
# 需要两个词同时出现才是动作的写法：要求修订说明书、提前终止保护、对不合格产品的处置
ACTION_PHRASES = re.compile(r"修订.{0,40}说明书|终止.{0,20}保护|不合格.{0,30}(?:处置|处理)")
# 先去掉再比对的片段：书名号里是文件名称（如《药品召回管理办法》），批准文号/批准文件是名词不是动作
NOT_ACTION_SPANS = re.compile(r"《[^》]*》|批准文号|批准(?:证明)?文件")
# 有这些词的是在讨论规则（征求意见稿、指南草案），不是动作本身
NOT_ACTIONS: tuple[str, ...] = ("征求意见", "draft guidance", "proposed rule")


def category_of(fine_type: str) -> str:
    """细类映射到的大类；没有合适大类返回 unmapped，不是注册过的细类返回 unknown。"""
    t = FINE_TYPES.get(fine_type)
    return t.category if t else UNKNOWN


def document_category(fine_type: str, lead_text: str = "") -> str:
    """单份文档的大类。按内容分的细类（on_action）只看标题、摘要、来源类别字段这段短文本：
    写明监管动作就取 on_action，否则取默认大类。先去掉 NOT_ACTION_SPANS（文件名称、批准文号等名词）
    再比对；不看正文，正文里顺带提到的“批准”不算。其余细类同 category_of。"""
    t = FINE_TYPES.get(fine_type)
    if t is None:
        return UNKNOWN
    if t.on_action:
        text = NOT_ACTION_SPANS.sub("", lead_text or "").lower()
        acted = any(w in text for w in REGULATORY_ACTIONS) or ACTION_PHRASES.search(text)
        if acted and not any(w in text for w in NOT_ACTIONS):
            return t.on_action
    return t.category


def categories_of(fine_types) -> list[str]:
    """一组细类覆盖的大类（保持顺序、去重）；unmapped / unknown 照样列出，不丢。
    按内容分的细类两个大类都列：这类来源两种文档都可能有。"""
    out: list[str] = []
    for f in fine_types:
        t = FINE_TYPES.get(f)
        for c in (category_of(f), t.on_action if t else None):
            if c and c not in out:
                out.append(c)
    return out

