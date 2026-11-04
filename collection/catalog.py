"""唯一采集框架的来源目录：每个来源一份声明。

调度（db.source_refresh）、限速（collection.rate）、增量窗口（collection.windows）和读取插件
（collection.plugins）都只读这里。学生扩源时改这里、加必要的解析插件和契约样例，不动调度、
图谱和雷达核心。

status 的三种取值如实区分：
- active：已接通，可以按声明读取；
- declared：业务需要、已登记目录，但还没有插件，不能读取，也不宣称已覆盖；
- blocked：试过接入但被来源拒绝（例如反爬拦截），原因写在 blocked_reason，等官方或授权渠道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

IncrementalKind = Literal[
    "source_updated_window",  # 来源自己的“更新日期”字段按窗口过滤
    "modified_window",  # 来源 API 的修改时间参数（updated_since/until）
    "fixed_ids",  # 只读明确清单，没有变更流
    "none",  # 尚无可靠增量键，只能快照加对账
]


@dataclass(frozen=True)
class Incremental:
    kind: IncrementalKind
    field: str | None
    semantics: str
    # 窗口回看重叠：来源更新日期常是“天”精度且有发布延迟，重叠读到的同一原件按版本身份去重。
    overlap_days: int = 2


@dataclass(frozen=True)
class CollectionSpec:
    source_id: str
    name: str
    status: Literal["active", "declared", "blocked"]
    plugin: str | None
    evidence_types: tuple[str, ...]
    # 服务的业务判断（v3 第一部分矩阵）：A 管线跟进 B 突破 C 产业结构 D 公卫威胁 E 判断更新
    decisions: tuple[str, ...]
    incremental: Incremental
    rate_group: str
    min_interval_seconds: float
    # 元数据、摘要、全文各自覆盖到哪一层，如实写
    coverage: dict = field(default_factory=dict)
    # 支持不带时间窗的全量回填（续页标记可靠、按页可续）
    backfill: bool = False
    # False：站点插件没有对原站做过实测（主持人 11-04 接受；NMPA/CDE/ChiCTR 等）
    verified: bool = True
    blocked_reason: str | None = None
    notes: str = ""

    @property
    def fetchable(self) -> bool:
        return self.status == "active" and self.plugin is not None

    @property
    def watchable(self) -> bool:
        return self.fetchable and self.incremental.kind != "none"


_NO_INCREMENT = Incremental("none", None, "尚未接通，没有增量键")

SPECS: tuple[CollectionSpec, ...] = (
    CollectionSpec(
        source_id="clinicaltrials",
        name="ClinicalTrials.gov",
        status="active",
        plugin="public_api_reader",
        evidence_types=("registry_record",),
        decisions=("A", "B", "E"),
        incremental=Incremental(
            "source_updated_window",
            "LastUpdatePostDate",
            "登记方更新被 CT.gov 公布的日期（天精度）；不是方案生效日期，也不是状态实际改变的日期",
        ),
        rate_group="clinicaltrials.gov",
        min_interval_seconds=0.4,
        coverage={"metadata": "全部公开登记字段", "results": "登记结果表", "fulltext": "无（登记无正文）"},
        backfill=True,
    ),
    CollectionSpec(
        source_id="europe_pmc",
        name="Europe PMC",
        status="active",
        plugin="public_api_reader",
        evidence_types=("literature_metadata", "abstract"),
        decisions=("B", "E"),
        incremental=Incremental(
            "source_updated_window",
            "UPDATE_DATE",
            "Europe PMC 记录更新日期；元数据更正也会更新，不等于论文发表日期",
        ),
        rate_group="ebi.ac.uk",
        min_interval_seconds=0.4,
        coverage={"metadata": "core", "abstract": "有则取", "fulltext": "不自动抓，另核许可"},
    ),
    CollectionSpec(
        source_id="who_don",
        name="WHO Disease Outbreak News",
        status="active",
        plugin="who_don",
        evidence_types=("official_notice",),
        decisions=("D", "E"),
        incremental=Incremental("modified_window", "LastModified", "公告最后修改时间；不是暴发开始时间"),
        rate_group="who.int",
        min_interval_seconds=1.0,
        coverage={"metadata": "全部", "fulltext": "公告原文"},
    ),
    CollectionSpec(
        source_id="sec_companyfacts",
        name="SEC XBRL 公司概念事实",
        status="active",
        plugin="sec_xbrl",
        evidence_types=("filing_fact",),
        decisions=("C",),
        incremental=Incremental("fixed_ids", None, "明确 CIK/概念清单；每次读完整单概念 JSON"),
        rate_group="sec.gov",
        min_interval_seconds=1.0,  # 沿用原读取循环的每秒一请求
        coverage={"metadata": "XBRL 标准事实", "fulltext": "无（filing 原文另接）"},
    ),
    CollectionSpec(
        source_id="pubmed",
        name="PubMed",
        status="active",
        plugin="public_api_reader",
        evidence_types=("literature_metadata", "abstract"),
        decisions=("B",),
        incremental=_NO_INCREMENT,
        rate_group="ncbi.nlm.nih.gov",
        min_interval_seconds=0.4,
        coverage={"metadata": "EFetch", "abstract": "有则取", "fulltext": "不下载"},
        notes="只支持按需读取；增量窗口（EDAT/MHDA）未接，暂不进固定刷新",
    ),
    CollectionSpec(
        source_id="fda_crl",
        name="FDA Complete Response Letters",
        status="active",
        plugin="public_api_reader",
        evidence_types=("regulatory_letter",),
        decisions=("A",),
        incremental=_NO_INCREMENT,
        rate_group="api.fda.gov",
        min_interval_seconds=0.4,
        coverage={"metadata": "原生字段", "fulltext": "信函文本"},
        notes="只支持按需读取；openFDA 无可靠变更流",
    ),
    # ——以下为业务必需、尚未接通的来源。登记在目录里，是为了让缺口可见，不是宣称已覆盖。——
    CollectionSpec(
        source_id="cde_ctr",
        name="CDE 药物临床试验登记与信息公示平台",
        status="blocked",
        plugin=None,
        evidence_types=("registry_record",),
        decisions=("A", "E"),
        incremental=_NO_INCREMENT,
        rate_group="chinadrugtrials.org.cn",
        min_interval_seconds=10.0,
        blocked_reason="2026-11-04 实测：首页 200，检索列表返回 202 脚本挑战页；普通无头浏览器同样只得到 39 字节空页。"
        "旧 ETL 没有这个站的采集代码；等官方数据渠道或授权。",
    ),
    # ——站点插件 site_crawler（站点配置 config/sites/* + core_engine 抓取器）——
    # 原站列表页 11-04 对普通无头浏览器分别返回 405/412/202；插件用站点配置里的浏览器抓取器运行，
    # 本系统未对原站实测（verified=False）。没有可靠增量键，只按需读取/按列表入口续页，不进固定刷新。
    # 附件：详情页附件各成独立版本，正文由加工层 attachment_parse（自部署 MinerU）产出。
    CollectionSpec(
        source_id="chictr",
        name="中国临床试验注册中心",
        status="active",
        plugin="site_crawler",
        evidence_types=("registry_record",),
        decisions=("A", "E"),
        incremental=_NO_INCREMENT,
        rate_group="chictr.org.cn",
        min_interval_seconds=2.0,
        coverage={"metadata": "登记详情页", "fulltext": "详情页正文", "attachments": "详情页附件"},
        verified=False,
        notes="旧库存 12.3 万篇另按真实来源回填",
    ),
    CollectionSpec(
        source_id="nmpa",
        name="国家药品监督管理局",
        status="active",
        plugin="site_crawler",
        evidence_types=("regulatory_notice",),
        decisions=("A", "C"),
        incremental=_NO_INCREMENT,
        rate_group="nmpa.gov.cn",
        min_interval_seconds=1.0,
        coverage={"metadata": "公告页", "fulltext": "公告正文", "attachments": "公告附件（直链 11-04 实测 200）"},
        verified=False,
    ),
    CollectionSpec(
        source_id="cde",
        name="国家药品审评中心",
        status="active",
        plugin="site_crawler",
        evidence_types=("regulatory_notice",),
        decisions=("A",),
        incremental=_NO_INCREMENT,
        rate_group="cde.org.cn",
        min_interval_seconds=2.0,
        coverage={"metadata": "新闻/政策页", "fulltext": "正文", "attachments": "附件（需页面会话 cookies）"},
        verified=False,
    ),
    *(
        CollectionSpec(
            source_id=source_id,
            name=name,
            status="active",
            plugin="site_crawler",
            evidence_types=evidence,
            decisions=decisions,
            incremental=_NO_INCREMENT,
            rate_group=group,
            min_interval_seconds=interval,
            coverage=coverage,
            verified=False,
            notes=notes,
        )
        for source_id, name, evidence, decisions, group, interval, coverage, notes in (
            ("cde_xxgk", "国家药品审评中心·信息公开（表格页）", ("regulatory_notice",), ("A",), "cde.org.cn", 2.0,
             {"metadata": "表格页每张表", "fulltext": "表格转 Markdown"}, "query=列表入口；每张表一条 site_record"),
            ("cde_zdyz", "国家药品审评中心·指导原则", ("guideline",), ("A", "B"), "cde.org.cn", 2.0,
             {"metadata": "指导原则页", "fulltext": "正文", "attachments": "附件"}, ""),
            ("phirda_industry", "中国药促会·产业资讯", ("industry_media",), ("C",), "phirda.com", 2.0,
             {"metadata": "资讯页", "fulltext": "正文"}, ""),
            ("phirda_policy", "中国药促会·政策法规", ("policy",), ("C",), "phirda.com", 2.0,
             {"metadata": "政策页", "fulltext": "正文"}, ""),
            ("phirda_rd", "中国药促会·研发追踪", ("industry_media",), ("A",), "phirda.com", 2.0,
             {"metadata": "资讯页", "fulltext": "正文"}, ""),
            ("phirda_healthcare_reform", "中国药促会·医改专题", ("policy",), ("C",), "phirda.com", 2.0,
             {"metadata": "专题页", "fulltext": "正文"}, ""),
            ("fda_press", "FDA Press Announcements", ("regulatory_notice",), ("A",), "fda.gov", 2.0,
             {"metadata": "新闻稿页", "fulltext": "正文"}, ""),
            ("fda_drug_safety", "FDA Drug Safety Communications", ("regulatory_notice",), ("A",), "fda.gov", 2.0,
             {"metadata": "安全通讯页", "fulltext": "正文"}, ""),
            ("fda_api_drug_label", "openFDA 药品说明书（drug/label）", ("regulatory_label",), ("A",),
             "api.fda.gov", 0.5, {"metadata": "说明书接口记录", "fulltext": "接口字段"},
             "query=records；与 fda_api_enforcement 同上游 api.fda.gov，不算两份独立证实"),
            ("fda_api_enforcement", "openFDA 药品召回执行报告（drug/enforcement）", ("regulatory_notice",), ("A",),
             "api.fda.gov", 0.5, {"metadata": "召回接口记录", "fulltext": "接口字段"},
             "query=records；与 fda_api_drug_label 同上游"),
            ("hkex", "港交所披露易", ("corporate_filing",), ("A", "C"), "hkexnews.hk", 2.0,
             {"metadata": "公告列表接口", "fulltext": "公告附件（PDF）"}, "query=records；医药公司清单见站点配置"),
            ("fiercepharma", "Fierce Pharma（RSS）", ("industry_media",), ("A", "C"), "fiercepharma.com", 2.0,
             {"metadata": "RSS 条目", "fulltext": "文章页"}, "query=records；行业媒体，不是独立证实来源"),
            ("nccn", "NCCN 临床实践指南", ("guideline",), ("B",), "nccn.org", 2.0,
             {"metadata": "指南列表", "fulltext": "指南 PDF（附件）"}, "query=records；许可未核实"),
            ("roche", "Roche Media Releases", ("company_disclosure",), ("A",), "roche.com", 2.0,
             {"metadata": "新闻稿列表", "fulltext": "新闻稿正文"}, "query=records；企业披露，不是独立无偏来源"),
            ("drugbank", "DrugBank", ("drug_database",), ("A",), "drugbank.com", 1.0,
             {"metadata": "药物条目接口"}, "query=records；需要 DrugBank API 凭据与许可，未核实"),
        )
    ),
    CollectionSpec(
        source_id="cninfo",
        name="巨潮资讯（沪深北公告与财报）",
        status="declared",
        plugin=None,
        evidence_types=("corporate_filing",),
        decisions=("A", "C"),
        incremental=_NO_INCREMENT,
        rate_group="cninfo.com.cn",
        min_interval_seconds=2.0,
    ),
    CollectionSpec(
        source_id="biorxiv",
        name="bioRxiv / medRxiv",
        status="declared",
        plugin=None,
        evidence_types=("preprint",),
        decisions=("B",),
        incremental=_NO_INCREMENT,
        rate_group="biorxiv.org",
        min_interval_seconds=1.0,
    ),
    CollectionSpec(
        source_id="drugs_fda",
        name="Drugs@FDA",
        status="declared",
        plugin=None,
        evidence_types=("regulatory_approval",),
        decisions=("A",),
        incremental=_NO_INCREMENT,
        rate_group="fda.gov",
        min_interval_seconds=1.0,
        notes="批准资料看 Drugs@FDA；说明书/召回覆盖数不能冒充批准覆盖",
    ),
)

CATALOG: dict[str, CollectionSpec] = {s.source_id: s for s in SPECS}
FETCHABLE = frozenset(s.source_id for s in SPECS if s.fetchable)
WATCHABLE = frozenset(s.source_id for s in SPECS if s.watchable)
# 由站点插件采集的来源（投影和读取都按这个集合分派）
SITE_SOURCES = frozenset(s.source_id for s in SPECS if s.plugin == "site_crawler")


def spec(source_id: str) -> CollectionSpec:
    try:
        return CATALOG[source_id]
    except KeyError:
        raise KeyError(source_id) from None


def gaps() -> list[dict]:
    """业务需要但没接通的来源，带原因；报告和健康检查用它把缺口写明。"""
    return [
        {
            "source_id": s.source_id,
            "name": s.name,
            "status": s.status,
            "decisions": list(s.decisions),
            "reason": s.blocked_reason or "已登记，尚无插件",
        }
        for s in SPECS
        if not s.fetchable
    ]
