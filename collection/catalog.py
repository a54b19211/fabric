"""唯一采集框架的来源目录：每个来源一份声明。

调度（db.source_refresh）、限速（collection.rate）、增量窗口（collection.windows）、读取插件、
来源许可目录（biz_rules.history_sources 按它生成站点来源的许可项）、加工层的文档类型和业务验收
（contracts.acceptance 的来源状态）都只读这里。业务需要但还没接的来源也登记在这里（declared）。
证据细类（evidence_types）和内容覆盖层级（coverage 的键）只用 contracts.evidence_types 的注册表。学生扩源时改这里、加必要的解析插件和契约样例，不动调度、
图谱和雷达核心。

status 的三种取值如实区分：
- active：已接通，可以按声明读取；
- declared：业务需要、已登记目录，但还没有插件，不能读取，也不宣称已覆盖；
- blocked：试过接入但被来源拒绝（例如反爬拦截），原因写在 blocked_reason，等官方或授权渠道。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from contracts.evidence_types import COVERAGE, FINE_TYPES

# 来源真实支持的读取方式（FetchRequest 的哪些字段起作用）：
# query 检索词；ids 明确的原生编号清单；list_pages 站点列表入口续页；window 来源更新时间窗；upload 管理员上传/收件目录
ReadMode = Literal["query", "ids", "list_pages", "window", "upload"]
READ_MODES = ("query", "ids", "list_pages", "window", "upload")

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
class OutboundCap:
    """This instance's own ceiling on requests to one site, counted across reads and processes (collection.rate).
    It limits what this instance sends; it is not a limit shared with other instances or the live system.
    per_day: requests per UTC day, every query of the source together.
    per_hour_per_query: requests per UTC hour for each value of the request's `query` parameter."""

    per_day: int
    per_hour_per_query: int | None = None


@dataclass(frozen=True)
class CollectionSpec:
    source_id: str
    name: str
    status: Literal["active", "declared", "blocked"]
    plugin: str | None
    # 证据细类：只能用 contracts.evidence_types.FINE_TYPES（每个细类映射到 v3 1.4 的一个大类或 unmapped）；
    # 一个来源可以覆盖多个细类。元数据/摘要/全文是覆盖层级，写在 coverage，不写在这里
    evidence_types: tuple[str, ...]
    # 服务的业务判断（v3 第一部分矩阵）：A 管线跟进 B 突破 C 产业结构 D 公卫威胁 E 判断更新
    decisions: tuple[str, ...]
    incremental: Incremental
    rate_group: str
    min_interval_seconds: float
    outbound_cap: OutboundCap | None = None
    # 元数据、摘要、全文各自覆盖到哪一层，如实写；键只能用 contracts.evidence_types.COVERAGE
    coverage: dict = field(default_factory=dict)
    # 支持不带时间窗的全量回填（续页标记可靠、按页可续）
    backfill: bool = False
    # 只有对真实来源跑通过的才写 True；默认 False，忘了写不会被当成实测过
    verified: bool = False
    blocked_reason: str | None = None
    notes: str = ""
    # 供给系统：同一系统经不同端点/栏目供给的来源编号写同一个值（如 openFDA 的说明书、召回、CRL）。
    # 只记录供给重叠，不判断独立性：两个端点不能仅凭接口不同算两份独立证实，但同一主机上没有
    # 共同样本/试验来源的两项研究也不因此合并。独立与否由 contracts.acceptance 按原始研究/披露主体、
    # 固定材料、引用/转载关系、同源组判断。空 = 自成一个供给系统；不回退到限速组或主机名。
    supply_system: str = ""
    # 可能收录/镜像的其他来源编号（如 Europe PMC 官方收录 PubMed/MEDLINE 内容）。只说明“两边可能有同一条
    # 记录”，不说明“是同一来源”：记录是否同一份仍按 PMID/DOI 等记录级编号判断
    # （contracts.acceptance.same_record），不把整个库当成一份，也不进 supply_system 的供给重叠。
    may_include: tuple[str, ...] = ()
    # 这个来源编号实际覆盖的数据集/端点
    datasets: tuple[str, ...] = ()
    # 来源真实支持的读取方式；由插件注册的读取方式和下面的收窄表决定（见 _with_read_modes），不在各条目里手写
    read_modes: tuple[ReadMode, ...] = ()

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
        verified=True,  # 已对真实来源跑通（隔离与现役窄 watch）
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
        verified=True,  # 已对真实来源跑通（隔离与现役窄 watch）
        name="Europe PMC",
        status="active",
        plugin="public_api_reader",
        evidence_types=("primary_research", "review", "preprint"),
        decisions=("B", "E"),
        incremental=Incremental(
            "source_updated_window",
            "UPDATE_DATE",
            "Europe PMC 记录更新日期；元数据更正也会更新，不等于论文发表日期",
    ),
        rate_group="ebi.ac.uk",
        min_interval_seconds=0.4,
        coverage={"metadata": "core", "abstract": "有则取", "fulltext": "不自动抓，另核许可"},
        may_include=("pubmed",),  # Europe PMC 官方收录 PubMed/MEDLINE
    ),
    CollectionSpec(
        source_id="who_don",
        verified=True,  # 已对真实来源跑通（隔离与现役窄 watch）
        name="WHO Disease Outbreak News",
        status="active",
        plugin="who_don",
        evidence_types=("official_notice",),
        decisions=("D", "E"),
        incremental=Incremental("modified_window", "LastModified", "公告最后修改时间；不是暴发开始时间"),
        rate_group="who.int",
        min_interval_seconds=30.0,
        outbound_cap=OutboundCap(per_day=200),  # a backfill's requests count inside the 200
        coverage={"metadata": "全部", "fulltext": "公告原文"},
    ),
    CollectionSpec(
        source_id="sec_companyfacts",
        verified=True,  # 已对真实来源跑通（隔离与现役窄 watch）
        name="SEC XBRL 公司概念事实",
        status="active",
        plugin="sec_xbrl",
        evidence_types=("filing_fact",),
        decisions=("C",),
        incremental=Incremental("fixed_ids", None, "明确 CIK/概念清单；每次读完整单概念 JSON"),
        rate_group="sec.gov",
        min_interval_seconds=1.0,  # 沿用原读取循环的每秒一请求
        coverage={"metadata": "XBRL 标准事实", "fulltext": "无（filing 原文另接）"},
        supply_system="sec_edgar",
    ),
    CollectionSpec(
        source_id="pubmed",
        name="PubMed",
        status="active",
        plugin="public_api_reader",
        evidence_types=("primary_research", "review", "preprint"),
        decisions=("B",),
        incremental=_NO_INCREMENT,
        rate_group="ncbi.nlm.nih.gov",
        min_interval_seconds=0.4,
        coverage={
            "metadata": "EFetch",
            "abstract": "有则取",
            "fulltext": "PMC efetch 的 JATS 声明 CC/公共领域许可时保存；否则 Unpaywall 许可明确的 OA PDF"
            "（需配置 DATAFABRIC_UNPAYWALL_EMAIL）；许可不明或不可取得的记缺失",
            "attachments": "PMC 全文 XML / OA PDF 各成附件版本（collection.pubmed_fulltext）",
        },
        notes="只支持按需读取；增量窗口（EDAT/MHDA）未接，暂不进固定刷新。全文路径 11-04 只对 PMC 真实请求过，"
        "未跑完整发布链",
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
        supply_system="openfda",
        datasets=("api.fda.gov/transparency/crl.json",),
    ),
    CollectionSpec(
        source_id="local_files",
        name="本地文件原件导入（PDF / Office）",
        status="active",
        plugin="local_file_import",
        evidence_types=("uploaded_document",),
        decisions=("A", "B", "C", "E"),
        incremental=_NO_INCREMENT,
        rate_group="local_files",  # 不发对外请求；目录要求每个来源有限速组
        min_interval_seconds=0.1,
        coverage={"metadata": "文件名、大小、sha256", "fulltext": "加工层附件解析（MinerU / Excel 直读）",
                  "attachments": "原件本身（按 sha256 固定版本）"},
        verified=False,
        notes="入口：POST /api/history/local-files（multipart）或 /fetch query=inbox（服务器收件目录）。"
        "许可由上传者负责；真实 PG 与 MinerU 的整链集成测试已写未跑",
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
        supply_system="cde_website",
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
            supply_system={"fda_api_drug_label": "openfda", "fda_api_enforcement": "openfda",
                           "cde_xxgk": "cde_website", "cde_zdyz": "cde_website",
                           "phirda_industry": "phirda_website", "phirda_policy": "phirda_website",
                           "phirda_rd": "phirda_website", "phirda_healthcare_reform": "phirda_website",
                           "fda_press": "fda_website", "fda_drug_safety": "fda_website"}.get(source_id, ""),
            datasets={"fda_api_drug_label": ("api.fda.gov/drug/label.json",),
                      "fda_api_enforcement": ("api.fda.gov/drug/enforcement.json",)}.get(source_id, ()),
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
             "query=records；与 fda_api_enforcement、fda_crl 同属 openFDA 供给系统，不能仅凭端点不同算两份独立证实"),
            ("fda_api_enforcement", "openFDA 药品召回执行报告（drug/enforcement）", ("regulatory_notice",), ("A",),
             "api.fda.gov", 0.5, {"metadata": "召回接口记录", "fulltext": "接口字段"},
             "query=records；产品类别 drug（站点配置 config/sites/fda/api_enforcement.py 指向 "
             "api.fda.gov/drug/enforcement.json），只证明配置能读，不代表在采；与 fda_api_drug_label、fda_crl "
             "同属 openFDA 供给系统"),
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
        name="bioRxiv",
        status="declared",
        plugin=None,
        evidence_types=("preprint",),
        decisions=("B",),
        incremental=_NO_INCREMENT,
        rate_group="biorxiv.org",
        min_interval_seconds=1.0,
        supply_system="openrxiv",
    ),
    CollectionSpec(
        source_id="gdelt_doc",
        name="GDELT DOC 新闻索引",
        status="active",
        plugin="gdelt_doc",
        evidence_types=("media",),
        decisions=("D",),
        # No cursor: a full page hides the rest of its window. The watch therefore reads whole UTC hours, halves the
        # full ones (biz_rules.gdelt_news) and records what stays cut short in the run's coverage. Each hour is read
        # once (no overlap): a re-read would spend requests for the same hour.
        incremental=Incremental(
            "source_updated_window", "seendate",
            "GDELT 观察到这条新闻的时间；不是新闻发布时间，也不是本地首次见到的时间", overlap_days=0,
        ),
        rate_group="gdeltproject.org",
        min_interval_seconds=5.0,
        outbound_cap=OutboundCap(per_day=900, per_hour_per_query=7),  # 5 queries x 24 hours x 7 = 840 at most
        coverage={"metadata": "标题、原网址、域名、语言、来源国家", "fulltext": "无（不追抓第三方新闻全文）"},
        notes="新闻索引线索，不是独立证据；没有连续分页，单次最多 max_records 条",
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

# 业务验收和 v3 缺口清单点名需要、但还没有插件的来源：登记在目录里，gaps() 会把它们列成缺口
_DECLARED = (
    ("ema", "EMA", ("regulatory_approval",), ("A",), "ema.europa.eu"),
    ("company_ir", "企业 IR 管线页和财报演示（不是独立无偏来源）", ("company_disclosure",), ("A", "C"), "company_ir"),
    ("conference_abstracts", "会议摘要", ("conference_abstract",), ("A", "B"), "conference_abstracts"),
    ("patents", "专利", ("patent",), ("A", "B"), "patents"),
    ("medrxiv", "medRxiv", ("preprint",), ("B",), "medrxiv.org"),  # 与 bioRxiv 同一 openRxiv 接口，见下
    ("arxiv", "arXiv 相关类别（AI 制药等不限于 q-bio）", ("preprint",), ("B",), "arxiv.org"),
    ("guidelines", "指南（NCCN、CDE 指导原则以外）", ("guideline",), ("B",), "guidelines"),
    ("sec_filings", "SEC filing 原文与交易披露", ("corporate_filing",), ("C",), "sec.gov"),
    ("nhsa", "国家医保局", ("policy",), ("C",), "nhsa.gov.cn"),
    ("procurement_prices", "采购与价格资料", ("measurement",), ("C",), "procurement_prices"),
    ("industry_media", "行业媒体（FiercePharma、药促会以外）", ("industry_media",), ("C",), "industry_media"),
    ("china_cdc", "中国疾控", ("official_notice",), ("D",), "chinacdc.cn"),
    ("health_commissions", "国家和地方卫健委通报", ("official_notice",), ("D",), "nhc.gov.cn"),
    ("ecdc", "ECDC", ("official_notice",), ("D",), "ecdc.europa.eu"),
    ("us_cdc", "US CDC", ("official_notice",), ("D",), "cdc.gov"),
    ("promed", "ProMED", ("media",), ("D",), "promedmail.org"),
    ("news_media", "媒体线索", ("media",), ("D",), "news_media"),
    ("social_media", "社媒线索", ("social",), ("D",), "social_media"),
    ("national_supply_stats", "全国统一口径供需/产能/采购/贸易数据", ("measurement",), ("C",), "national_supply_stats"),
    ("policy_execution_data", "政策执行和结果数据", ("measurement",), ("C",), "policy_execution_data"),
    ("local_industry_ledger", "辖区企业/园区/产线底册（授权内部台账）", ("internal_ledger",), ("C",),
     "local_industry_ledger"),
    ("research_institution_ledger", "项目/经费/设备/队伍/成果授权台账", ("internal_ledger",), ("B",),
     "research_institution_ledger"),
    ("grants", "公开的基金资助信息", ("funding",), ("B",), "grants"),
)
SPECS = SPECS + tuple(
    CollectionSpec(source_id=sid, name=name, status="declared", plugin=None, evidence_types=evidence,
                   decisions=decisions, incremental=_NO_INCREMENT, rate_group=group, min_interval_seconds=1.0,
                   supply_system={"medrxiv": "openrxiv", "sec_filings": "sec_edgar"}.get(sid, ""))
    for sid, name, evidence, decisions, group in _DECLARED
)

# 读取插件注册时声明的读取方式（按代码现状：每种方式都对应插件里真正读取的 FetchRequest 字段）；
# 同一插件下某个来源不支持其中一种时，在 SOURCE_READ_MODES 里收窄。biz_rules.history_sources.PLUGINS 的插件名须与此一一对应
PLUGIN_READ_MODES: dict[str, tuple[ReadMode, ...]] = {
    "public_api_reader": ("query", "ids"),
    "who_don": ("query", "ids", "window"),
    "sec_xbrl": ("ids",),
    "gdelt_doc": ("query", "window"),
    "site_crawler": ("ids", "list_pages"),
    "local_file_import": ("upload",),
}
SOURCE_READ_MODES: dict[str, tuple[ReadMode, ...]] = {
    "fda_crl": ("query",),  # 复合键歧义：不支持精确 by-ID 重采
}


def _with_read_modes(item: CollectionSpec) -> CollectionSpec:
    if not item.fetchable:
        return item
    return replace(item, read_modes=SOURCE_READ_MODES.get(item.source_id, PLUGIN_READ_MODES.get(item.plugin, ())))


SPECS = tuple(_with_read_modes(item) for item in SPECS)

CATALOG: dict[str, CollectionSpec] = {s.source_id: s for s in SPECS}
FETCHABLE = frozenset(s.source_id for s in SPECS if s.fetchable)
WATCHABLE = frozenset(s.source_id for s in SPECS if s.watchable)
# 由站点插件采集的来源（投影和读取都按这个集合分派）
SITE_SOURCES = frozenset(s.source_id for s in SPECS if s.plugin == "site_crawler")


def _check_evidence_registry() -> None:
    """目录里的证据细类和覆盖层级只用 contracts.evidence_types 的注册表，不另立词表；
    may_include 只能指向目录里的其他来源。"""
    errors = []
    known = {s.source_id for s in SPECS}
    for s in SPECS:
        bad = [t for t in s.evidence_types if t not in FINE_TYPES]
        if bad:
            errors.append(f"{s.source_id} 的 evidence_types 有未注册的细类 {bad}")
        bad = [k for k in s.coverage if k not in COVERAGE]
        if bad:
            errors.append(f"{s.source_id} 的 coverage 有未注册的覆盖层级 {bad}")
        bad = [x for x in s.may_include if x == s.source_id or x not in known]
        if bad:
            errors.append(f"{s.source_id} 的 may_include 不是目录里的其他来源 {bad}")
    if errors:
        raise ValueError("；".join(errors))


_check_evidence_registry()

def _check_read_modes() -> None:
    """可采集的来源至少声明一种读取方式，且不超出所用插件注册的范围；未接通的来源不声明。"""
    errors = []
    for s in SPECS:
        if s.fetchable:
            if not s.read_modes:
                errors.append(f"{s.source_id} 已接通但没有读取方式")
            wide = PLUGIN_READ_MODES.get(s.plugin or "", ())
            if not set(s.read_modes) <= set(wide):
                errors.append(f"{s.source_id} 的读取方式 {list(s.read_modes)} 超出插件 {s.plugin} 注册的 {list(wide)}")
        elif s.read_modes:
            errors.append(f"{s.source_id} 未接通却声明了读取方式")
    errors += [f"SOURCE_READ_MODES 指向不在目录里的来源 {k}" for k in SOURCE_READ_MODES if k not in {s.source_id for s in SPECS}]
    if errors:
        raise ValueError("；".join(errors))


_check_read_modes()
# 有附件版本的来源（加工层 db.derived_text 只解析这些来源的 role=attachment 版本）：站点插件，加上目录里
# 声明了附件覆盖的已接通来源（PubMed 全文、本地导入）
ATTACHMENT_SOURCES = SITE_SOURCES | frozenset(
    s.source_id for s in SPECS if s.fetchable and "attachments" in s.coverage
)


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
