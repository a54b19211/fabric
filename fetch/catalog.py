"""来源目录：Fabric 知道的每个来源都登记在这里，一处。

- SPECS：从外面采集的来源，一个来源一份 CollectionSpec（名称、证据类型、许可、限速、增量读法、读取插件、覆盖说明）。
  调度（fetch.source_refresh）、限速（fetch.rate）、增量窗口（fetch.windows）、读取（fetch.sources.read_public）、
  加工画像（clean.profile）和 /api/fabric/collection/catalog 都只读这里。业务需要但还没接的来源也登记（declared）。
- DERIVED：不从外面采集、在 Fabric 里派生或受控导入的来源；RETIRED：已退役的来源。它们只有名称。
- source_catalog()：fabric_sources 每行的种子（迁移时没有的行才插入），也给 /api/fabric/sources 名称。

status 的三种取值：active 已接通，可以按声明读取；declared 已登记、还没有插件；
blocked 不抓：试过接入但被来源拒绝（例如反爬拦截），或者决定了不在线抓（DrugBank），原因写在 blocked_reason。
许可：主持人定的口径是学术、非商用、不再分发。fabric_sources 里每个来源有可读开关 read_enabled 和定时抓取开关
fetch_enabled，管理员经 /api/fabric/admin/sources 改。迁移补行时默认都打开，DEFAULT_CLOSED 里的来源可读开关默认关。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# 来源真实支持的读取方式（FetchRequest 的哪些字段起作用）：
# query 检索词；ids 明确的原生编号清单；list_pages 站点列表入口续页；window 来源更新时间窗；upload 管理员上传/收件目录
ReadMode = Literal["query", "ids", "list_pages", "window", "upload"]

IncrementalKind = Literal[
    "source_updated_window",  # 来源自己的“更新日期”字段按窗口过滤
    "modified_window",  # 来源 API 的修改时间参数（updated_since/until）
    "fixed_ids",  # 只读明确清单，没有变更流
    "stop_at_known",  # 没有可过滤的更新时间，但列表从新到旧：每轮从头读，连续读到已存且没变的就停（fetch.windows）；分组的按组停
    "full_reread",  # 没有可过滤的更新时间，列表顺序也靠不住（旧条目会后补进来）：每轮整份重读，没变的版本不重复存
    "none",  # 尚无可靠增量键，只能快照加对账
]


@dataclass(frozen=True)
class Incremental:
    kind: IncrementalKind
    # 窗口回看重叠：来源更新日期常是“天”精度且有发布延迟，重叠读到的同一原件按版本身份去重。
    overlap_days: int = 2
    # 固定刷新（fetch.source_refresh）的最短间隔。一轮至少一次请求。
    min_cadence_seconds: int = 3600
    # stop_at_known 按组算（站点配置里也写了 "by_group": True）：每一组各自从新到旧，“连续读到已存的就停”只停这一组，
    # 一个周期读完全部的组，读不到的组下个周期自然再读
    by_group: bool = False


@dataclass(frozen=True)
class CollectionSpec:
    source_id: str
    name: str
    status: Literal["active", "declared", "blocked"]
    plugin: str | None  # 读取插件（fetch.sources.PLUGINS 的键）；没接通的是 None
    evidence_types: tuple[str, ...]  # 证据细类，fetch.evidence_types.FINE_TYPES 的键（元数据/全文是覆盖层级，不写这里）
    incremental: Incremental
    rate_group: str  # 限速组：主机名后缀（fetch.rate 按最长后缀找）
    min_interval_seconds: float  # 同一限速组里两次请求的最短间隔（秒）
    read_modes: tuple[ReadMode, ...] = ()  # 来源真实支持的读法；/api/fabric/sources 的 fetch.modes 就是它
    # 覆盖说明：metadata 元数据、abstract 摘要、fulltext 全文、results 登记结果表、attachments 附件各读到哪一层；
    # 写了 attachments 的已接通来源才产出附件版本（ATTACHMENT_SOURCES）
    coverage: dict = field(default_factory=dict)
    license_url: str | None = None  # 来源的使用条款；None = 没有可引用的条款（站点、上传原件），靠管理员逐源许可
    backfill: bool = False  # 支持不带时间窗的全量回填（续页标记可靠、按页可续）
    verified: bool = False  # 只有对真实来源跑通过的才写 True；默认 False，忘了写不会被当成实测过
    blocked_reason: str | None = None

    @property
    def fetchable(self) -> bool:
        return self.status == "active" and self.plugin is not None

    @property
    def watchable(self) -> bool:
        return self.fetchable and self.incremental.kind != "none"


_NO_INCREMENT = Incremental("none")
# 列表从新到旧、没有可过滤的更新时间：每轮从头读，连续读到 3 条（fetch.windows.KNOWN_STOP）已存且没变的就停；
# 旧条目被改动读不到。列表顺序是站点自己的默认顺序，不是来源的承诺。
_NEWEST_FIRST = Incremental("stop_at_known")
# 列表不大、但新条目不一定排在前面（成批公布的旧信函排在读过的新信函后面，“遇到已存的就停”会漏）：每轮整份重读
_FULL_REREAD = Incremental("full_reread")
# 分组读：一组一组地读，续读位置记第几组。港交所按公司（每家各自从新到旧）；NCCN 按指南（清单没有时间顺序，
# 每个周期把全部指南的详情页读一遍，已存且没变的不再下载附件）
_BY_GROUP = Incremental("stop_at_known", by_group=True)
_SITE_MODES: tuple[ReadMode, ...] = ("ids", "list_pages")


def _site(source_id, name, *, evidence, group, coverage, interval=2.0, incremental=_NEWEST_FIRST,
          backfill=False):
    """站点插件（site_crawler，站点配置在 fetch/sites/*）的来源：已接通、未经本系统对原站实测（verified=False）。"""
    return CollectionSpec(source_id, name, "active", "site_crawler", evidence, incremental, group, interval,
                          _SITE_MODES, coverage, backfill=backfill)


def _declared(source_id, name, evidence, group, interval=1.0, blocked_reason=None):
    """业务需要、还没有插件的来源：登记在目录里，gaps() 把它们列成缺口，不宣称已覆盖。"""
    status = "blocked" if blocked_reason else "declared"
    return CollectionSpec(source_id, name, status, None, evidence, _NO_INCREMENT, group, interval,
                          blocked_reason=blocked_reason)


SPECS: tuple[CollectionSpec, ...] = (
    # 增量键 LastUpdatePostDate：登记方更新被 CT.gov 公布的日期（天精度），不是方案生效或状态实际改变的日期
    CollectionSpec(
        "clinicaltrials", "ClinicalTrials.gov", "active", "public_api_reader", ("registry_record",),
        Incremental("source_updated_window"), "clinicaltrials.gov", 0.4, ("query", "ids"),
        {"metadata": "全部公开登记字段", "results": "登记结果表", "fulltext": "无（登记无正文）"},
        license_url="https://clinicaltrials.gov/data-api/about-api", backfill=True, verified=True,
    ),
    # 增量键 UPDATE_DATE：元数据更正也会更新，不等于发表日期。官方收录 PubMed/MEDLINE，同一 PMID 不算独立证据
    CollectionSpec(
        "europe_pmc", "Europe PMC", "active", "public_api_reader", ("primary_research", "review", "preprint"),
        Incremental("source_updated_window"), "ebi.ac.uk", 0.4, ("query", "ids"),
        {"metadata": "core", "abstract": "有则取", "fulltext": "不自动抓，另核许可"},
        license_url="https://europepmc.org/Copyright", verified=True,
    ),
    # 增量键 LastModified（公告最后修改时间，不是暴发开始时间）；每 15 分钟一轮，一天 96 次请求。
    # DON 是选择性公告，零公告不证明没有暴发
    CollectionSpec(
        "who_don", "WHO Disease Outbreak News", "active", "who_don", ("official_notice",),
        Incremental("modified_window", min_cadence_seconds=900), "who.int", 30.0, ("query", "ids", "window"),
        {"metadata": "全部", "fulltext": "公告原文"},
        license_url="https://www.who.int/about/policies/publishing/copyright", verified=True,
    ),
    # 明确的 CIK/概念清单，每次读完整的单概念 JSON；每秒一请求沿用原读取循环
    CollectionSpec(
        "sec_companyfacts", "SEC XBRL 公司概念事实", "active", "sec_xbrl", ("filing_fact",),
        Incremental("fixed_ids"), "sec.gov", 1.0, ("ids",),
        {"metadata": "XBRL 标准事实", "fulltext": "无（filing 原文另接）"},
        license_url="https://www.sec.gov/search-filings/edgar-application-programming-interfaces", verified=True,
    ),
    # ESearch 不指定排序时新入库的 PMID 在前（实测），所以按“遇到已存的就停”增量读；全文只存许可明确的
    CollectionSpec(
        "pubmed", "PubMed", "active", "public_api_reader", ("primary_research", "review", "preprint"),
        _NEWEST_FIRST, "ncbi.nlm.nih.gov", 0.4, ("query", "ids"),
        {"metadata": "EFetch", "abstract": "有则取",
         "fulltext": "PMC efetch 的 JATS 声明 CC/公共领域许可时保存；否则 Unpaywall 许可明确的 OA PDF"
         "（需配置 DATAFABRIC_UNPAYWALL_EMAIL）；许可不明或不可取得的记缺失",
         "attachments": "PMC 全文 XML / OA PDF 各成附件版本（fetch.pubmed_fulltext）"},
        license_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
    ),
    # openFDA 无变更流，FDA 成批公布信函：每轮整份重读（约 460 封，5 次请求）。复合键有歧义，不支持按编号重采（只有 query）
    CollectionSpec(
        "fda_crl", "FDA Complete Response Letters", "active", "public_api_reader", ("regulatory_letter",),
        _FULL_REREAD, "api.fda.gov", 0.4, ("query",), {"metadata": "原生字段", "fulltext": "信函文本"},
        license_url="https://open.fda.gov/license/",
    ),
    # 入口：POST /api/fabric/local-files（multipart）或 /fetch query=inbox（服务器收件目录）；许可由上传者负责。
    # 不发对外请求，限速组只是因为每个来源都要有一个
    CollectionSpec(
        "local_files", "本地文件原件导入（PDF / Office）", "active", "local_file_import", ("uploaded_document",),
        _NO_INCREMENT, "local_files", 0.1, ("upload",),
        {"metadata": "文件名、大小、sha256", "fulltext": "加工层附件解析（MinerU / Excel 直读）",
         "attachments": "原件本身（按 sha256 固定版本）"},
    ),
    _declared("cde_ctr", "CDE 药物临床试验登记与信息公示平台", ("registry_record",),
              "chinadrugtrials.org.cn", 10.0,
              blocked_reason="2026-11-04 实测：首页 200，检索列表返回 202 脚本挑战页；普通无头浏览器同样只得到 39 字节"
              "空页。旧 ETL 没有这个站的采集代码；等官方数据渠道或授权。"),
    # ——站点插件 site_crawler：用 ops/check_sources.py 对原站真抓过多轮，verified 要不要改 True 由主持人定。
    # 详情页附件各成独立版本，正文由加工层 attachment_parse（自部署 MinerU）产出。——
    _site("chictr", "中国临床试验注册中心", evidence=("registry_record",), group="chictr.org.cn",
          coverage={"metadata": "登记详情页", "fulltext": "详情页正文", "attachments": "详情页附件"}),
    _site("nmpa", "国家药品监督管理局", evidence=("regulatory_notice",), group="nmpa.gov.cn",
          interval=1.0, coverage={"metadata": "公告页", "fulltext": "公告正文", "attachments": "公告附件（直链 11-04 实测 200）"}),
    _site("cde", "国家药品审评中心", evidence=("regulatory_notice",), group="cde.org.cn",
          coverage={"metadata": "新闻/政策页", "fulltext": "正文", "attachments": "附件（需页面会话 cookies）"}),
    # query=列表入口；每张表一条 site_record
    _site("cde_xxgk", "国家药品审评中心·信息公开（表格页）", evidence=("regulatory_notice",), group="cde.org.cn", coverage={"metadata": "表格页每张表", "fulltext": "表格转 Markdown"}),
    _site("cde_zdyz", "国家药品审评中心·指导原则", evidence=("guideline",), group="cde.org.cn",
          coverage={"metadata": "指导原则页", "fulltext": "正文", "attachments": "附件"}),
    _site("phirda_industry", "中国药促会·产业资讯", evidence=("industry_media",), group="phirda.com",
          coverage={"metadata": "资讯页", "fulltext": "正文"}),
    _site("phirda_policy", "中国药促会·政策法规", evidence=("policy",), group="phirda.com",
          coverage={"metadata": "政策页", "fulltext": "正文"}),
    _site("phirda_rd", "中国药促会·研发追踪", evidence=("industry_media",), group="phirda.com",
          coverage={"metadata": "资讯页", "fulltext": "正文"}),
    _site("phirda_healthcare_reform", "中国药促会·医改专题", evidence=("policy",), group="phirda.com",
          coverage={"metadata": "专题页", "fulltext": "正文"}),
    _site("fda_press", "FDA Press Announcements", evidence=("regulatory_notice",), group="fda.gov",
          coverage={"metadata": "新闻稿页", "fulltext": "正文"}),
    _site("fda_drug_safety", "FDA Drug Safety Communications", evidence=("regulatory_notice",), group="fda.gov", coverage={"metadata": "安全通讯页", "fulltext": "正文"}),
    # openFDA 的两个接口（query=records）：与 fda_crl 同属 openFDA，不能仅凭端点不同算两份独立证实
    _site("fda_api_drug_label", "openFDA 药品说明书（drug/label）", evidence=("regulatory_label",), group="api.fda.gov", interval=0.5, coverage={"metadata": "说明书接口记录", "fulltext": "接口字段"}),
    _site("fda_api_enforcement", "openFDA 药品召回执行报告（drug/enforcement）", evidence=("regulatory_notice",),
          group="api.fda.gov", interval=0.5, coverage={"metadata": "召回接口记录", "fulltext": "接口字段"}),
    # query=records；医药公司清单见站点配置。每个周期读完全部公司，每家最多最新 GROUP_LIMIT 条（fetch.windows），
    # 更老的用 ops/backfill_source.py hkex 补
    _site("hkex", "港交所披露易", evidence=("corporate_filing",), group="hkexnews.hk",
          coverage={"metadata": "公告列表接口", "fulltext": "公告附件（PDF）"}, incremental=_BY_GROUP, backfill=True),
    # query=records；行业媒体，不是独立证实来源
    _site("fiercepharma", "Fierce Pharma（RSS）", evidence=("industry_media",), group="fiercepharma.com", coverage={"metadata": "RSS 条目", "fulltext": "文章页"}),
    # query=records；许可未核实
    _site("nccn", "NCCN 临床实践指南", evidence=("guideline",), group="nccn.org",
          coverage={"metadata": "指南列表", "fulltext": "指南 PDF（附件）"}, incremental=_BY_GROUP, backfill=True),
    # query=records；企业披露，不是独立无偏来源
    _site("roche", "Roche Media Releases", evidence=("company_disclosure",), group="roche.com",
          coverage={"metadata": "新闻稿列表", "fulltext": "新闻稿正文"}),
    _site("hkex_reports", "港交所披露易·定期报告", evidence=("corporate_filing",), group="hkexnews.hk",
          coverage={"metadata": "公告列表接口", "fulltext": "报告 PDF（附件）"}, incremental=_BY_GROUP, backfill=True),
    _declared("cninfo", "巨潮资讯（沪深北公告与财报）", ("corporate_filing",), "cninfo.com.cn", 2.0),
    # openRxiv 官方接口（api.biorxiv.org），两个站一个读法；只存标题和摘要
    _site("biorxiv", "bioRxiv", evidence=("preprint",), group="biorxiv.org", interval=1.0,
          coverage={"metadata": "接口记录", "abstract": "摘要", "fulltext": "不抓"}),
    _site("medrxiv", "medRxiv", evidence=("preprint",), group="biorxiv.org", interval=1.0,
          coverage={"metadata": "接口记录", "abstract": "摘要", "fulltext": "不抓"}),
    # 官方接口 export.arxiv.org；使用条款：两次请求至少隔 3 秒
    _site("arxiv", "arXiv q-bio", evidence=("preprint",), group="arxiv.org", interval=3.0,
          coverage={"metadata": "接口记录", "abstract": "摘要", "fulltext": "不抓"}),
    # SEC EDGAR filing 原文（官方 Archives；User-Agent 写明身份）。一家公司一组
    _site("sec_filings", "SEC EDGAR 定期报告（10-K / 10-Q / 20-F）", evidence=("corporate_filing",), group="sec.gov", interval=1.0, coverage={"metadata": "filing 清单", "fulltext": "主文件转文字"},
          incremental=_BY_GROUP),
    _site("sec_8k", "SEC EDGAR 临时公告（8-K）", evidence=("corporate_filing",), group="sec.gov",
          interval=1.0, coverage={"metadata": "filing 清单", "fulltext": "主文件和 EX-99 转文字"}, incremental=_BY_GROUP),
    # 通用 RSS：企业新闻稿（企业披露，不是独立无偏来源）；只用订阅源里的内容
    _site("rss_press", "企业新闻稿（RSS）", evidence=("company_disclosure",), group="rss_press",
          interval=1.0, coverage={"metadata": "订阅源条目", "fulltext": "订阅源给的正文或摘要"}),
    _site("ema", "EMA News", evidence=("regulatory_notice",), group="ema.europa.eu", interval=5.0,
          coverage={"metadata": "订阅源条目", "fulltext": "新闻正文；审评文件（EPAR）没接"}),
    # 新闻索引线索，不是独立证据。增量键 seendate（GDELT 观察到的时间，不是发布时间）；没有续页，固定刷新按 UTC 6 小时一段读，
    # 满额的对半拆开再读（fetch.gdelt_news），3 小时一轮（overlap 0）。对方要求每 5 秒最多一个请求，间隔留 6 秒。
    # 2026-11-13 实测限流是对方的：直连和代理、各种间隔和查询词成功率都约 17%，429 没有 Retry-After；所以不走代理、
    # 间隔不再调，每个请求最多发 6 次（fetch.gdelt_news.TRIES），还是 429 就记为对方限流，下个周期接着读
    CollectionSpec(
        "gdelt_doc", "GDELT DOC 新闻索引", "active", "gdelt_doc", ("media",),
        Incremental("source_updated_window", overlap_days=0), "gdeltproject.org", 6.0, ("query", "window"),
        {"metadata": "标题、原网址、域名、语言、来源国家", "fulltext": "无（不追抓第三方新闻全文）"},
        license_url="https://www.gdeltproject.org/about.html#termsofuse",
    ),
    # 批准资料看 Drugs@FDA；说明书/召回覆盖数不能冒充批准覆盖
    _declared("drugs_fda", "Drugs@FDA", ("regulatory_approval",), "fda.gov"),
    # 原采集层有 DrugBank 的站点配置和抓取器（config/sites/drugbank.py、drugbank_fetcher.py），主线不搬
    _declared("drugbank", "DrugBank", ("drug_database",), "drugbank.com",
              blocked_reason="不在线抓：主持人 2026-11-14 决定退出采集，以后下载 DrugBank 数据库文件（学术许可）离线导入"),
    # ——业务验收和 v3 缺口清单点名需要、还没有插件的——
    _declared("company_ir", "企业 IR 管线页和财报演示（不是独立无偏来源）", ("company_disclosure",), "company_ir"),
    _declared("conference_abstracts", "会议摘要", ("conference_abstract",), "conference_abstracts"),
    _declared("patents", "专利", ("patent",), "patents"),
    _declared("guidelines", "指南（NCCN、CDE 指导原则以外）", ("guideline",), "guidelines"),
    _declared("nhsa", "国家医保局", ("policy",), "nhsa.gov.cn"),
    _declared("procurement_prices", "采购与价格资料", ("measurement",), "procurement_prices"),
    _declared("industry_media", "行业媒体（FiercePharma、药促会以外）", ("industry_media",), "industry_media"),
    _declared("china_cdc", "中国疾控", ("official_notice",), "chinacdc.cn"),
    _declared("health_commissions", "国家和地方卫健委通报", ("official_notice",), "nhc.gov.cn"),
    _declared("ecdc", "ECDC", ("official_notice",), "ecdc.europa.eu"),
    _declared("us_cdc", "US CDC", ("official_notice",), "cdc.gov"),
    _declared("promed", "ProMED", ("media",), "promedmail.org"),
    _declared("news_media", "媒体线索", ("media",), "news_media"),
    _declared("social_media", "社媒线索", ("social",), "social_media",
              blocked_reason="X/Twitter 不抓（没有付费接口，使用条款不许爬）；微信公众号等主持人定读取账号"),
    _declared("national_supply_stats", "全国统一口径供需/产能/采购/贸易数据", ("measurement",),
              "national_supply_stats"),
    _declared("policy_execution_data", "政策执行和结果数据", ("measurement",), "policy_execution_data"),
    _declared("local_industry_ledger", "辖区企业/园区/产线底册（授权内部台账）", ("internal_ledger",),
              "local_industry_ledger"),
    _declared("research_institution_ledger", "项目/经费/设备/队伍/成果授权台账", ("internal_ledger",),
              "research_institution_ledger"),
    _declared("grants", "公开的基金资助信息", ("funding",), "grants"),
)

# 不从外面采集、在 Fabric 里派生或受控导入的来源：在 fabric_sources 里有一行（管理员的可读开关），不进采集目录
DERIVED = {
    "external_archive": "受控历史归档",  # 固定原件的受控导入（POST /api/fabric/archives）
}
# 已退役：不再写入、不能被选、不作索引目标，已存的旧版本照读。
# legacy_documents：v3 6.3（2026-11-06）决定，旧编号经 legacy_aliases 照读。
# reviewed_graph、primekg_graph：图谱 2026-11-16 拆到 Atlas，图包由 Atlas 自己导入，Fabric 不再收
RETIRED = {
    "legacy_documents": "已授权旧文档的精确冻结桥",
    "reviewed_graph": "人工审核图谱发布（已移交 Atlas）",
    "primekg_graph": "PrimeKG 背景图（已移交 Atlas）",
}
# 可读开关默认关的来源：NCCN（许可未核实）一定关；本地导入和 SEC 公司概念事实由管理员按需打开。V047 关上的其余来源
# 2026-11-17 随十个知识库打开（V052）
DEFAULT_CLOSED = frozenset({"local_files", "nccn", "sec_companyfacts"})


def _watch(watch_id, source_id, query="records", *, records=100, pages=1, seconds=120, cadence=86400, lookback=7):
    return {"id": watch_id, "request": {"source_id": source_id, "query": query, "max_records": records,
                                        "max_pages": pages, "deadline_seconds": seconds},
            "cadence_seconds": cadence, "lookback_days": lookback}


_H = 3600
# Europe PMC 的 UPDATE_DATE 是天精度、窗口回看 2 天：每小时读一次只是把同一窗口重读 6 遍（2026-11-19 六个计划 339 次）。
# 一页 core 结果要 17-32 秒，30 秒期限常常一页都读不完；改成 6 小时一轮、期限 120 秒
_EPMC = {"records": 100, "seconds": 120, "cadence": 6 * _H}
_CT_DRUGS = "AREA[StudyType]INTERVENTIONAL AND AREA[InterventionType](DRUG OR BIOLOGICAL OR GENETIC OR COMBINATION_PRODUCT)"
_PUBMED = '("clinical trial"[Publication Type] OR "drug approval"[MeSH Terms] OR "drug therapy"[MeSH Subheading])'
# 定时刷新计划：哪个来源、读什么、多久一轮（fetch/source_refresh.py 读这里；环境变量 DATAFABRIC_WATCHES 给了就用它的）。
# 一个来源抓不抓 = 这里有它的计划 + 库里两个开关（fabric_sources.read_enabled / fetch_enabled）都开着。
# 接口站点 query 写 records，网页站点写列表入口（站点配置的 start_urls 之一，一个入口一条计划）。
WATCHES = (
    _watch("who-recent", "who_don", "*", records=5, seconds=30, cadence=_H, lookback=30),
    _watch("epmc-bcma-updates", "europe_pmc", "BCMA AND myeloma", records=50, seconds=120, cadence=6 * _H,
           lookback=30),
    _watch("epmc-phase3", "europe_pmc", '("phase 3" OR "phase III") AND (randomized OR randomised)', **_EPMC),
    _watch("epmc-outbreak", "europe_pmc",
           'TITLE:(outbreak OR epidemic OR "case cluster" OR "novel virus" OR "emerging infection")', **_EPMC),
    _watch("epmc-metabolic", "europe_pmc", "GLP-1 OR semaglutide OR tirzepatide OR incretin", **_EPMC),
    _watch("epmc-modalities", "europe_pmc", '"antibody-drug conjugate" OR "CAR-T" OR "bispecific antibody"', **_EPMC),
    _watch("epmc-regulatory", "europe_pmc",
           '"FDA approval" OR "accelerated approval" OR "breakthrough therapy" OR NMPA', **_EPMC),
    # 临床登记：全部分期的药物 / 生物制品 / 基因治疗的干预性试验（原来只有三期）
    _watch("ct-drug-trials", "clinicaltrials", _CT_DRUGS, seconds=60, cadence=6 * _H, lookback=3),
    # 整份重读约 460 封；FDA 成批公布，一天读一次就够（原来每小时一次，2026-11-19 读了 102 次、0 个新版本）
    _watch("fda-crl", "fda_crl", "*", seconds=60),
    _watch("pubmed-drug-trials", "pubmed", _PUBMED, records=50, cadence=6 * _H),
    _watch("gdelt-fda-approval", "gdelt_doc", "FDA approval", seconds=120, cadence=3 * _H, lookback=1),
    _watch("fda-drug-label", "fda_api_drug_label"),
    _watch("fda-enforcement", "fda_api_enforcement"),
    _watch("fda-press", "fda_press", "https://www.fda.gov/news-events/fda-newsroom/press-announcements", records=30,
           cadence=6 * _H),
    _watch("fda-drug-safety", "fda_drug_safety",
           "https://www.fda.gov/drugs/drug-safety-and-availability/drug-safety-communications", records=30),
    _watch("ema-news", "ema", records=20, cadence=6 * _H),
    _watch("nmpa-ggtg", "nmpa", "https://www.nmpa.gov.cn/xxgk/ggtg/index.html", records=30, cadence=6 * _H),
    _watch("nmpa-fgwj", "nmpa", "https://www.nmpa.gov.cn/xxgk/fgwj/index.html", records=30),
    _watch("nmpa-zhcjd", "nmpa", "https://www.nmpa.gov.cn/xxgk/zhcjd/index.html", records=30),
    _watch("nmpa-zhqyi", "nmpa", "https://www.nmpa.gov.cn/xxgk/zhqyj/index.html", records=30),
    _watch("nmpa-fxjzh", "nmpa", "https://www.nmpa.gov.cn/xxgk/fxjzh/index.html", records=30),
    _watch("nmpa-ypggtg", "nmpa", "https://www.nmpa.gov.cn/yaopin/ypggtg/index.html", records=30, cadence=6 * _H),
    _watch("nmpa-ypzhcjd", "nmpa", "https://www.nmpa.gov.cn/yaopin/ypzhcjd/index.html", records=30),
    _watch("cde-news", "cde", "https://www.cde.org.cn/main/news/listpage/545cf855a50574699b46b26bcb165f32", records=30,
           cadence=6 * _H),
    _watch("cde-policy", "cde", "https://www.cde.org.cn/main/policy/listpage/9f9c74c73e0f8f56a8bfbc646055026d",
           records=30),
    _watch("cde-xxgk-1", "cde_xxgk", "https://www.cde.org.cn/main/xxgk/listpage/9f9c74c73e0f8f56a8bfbc646055026d",
           records=30),
    _watch("cde-xxgk-2", "cde_xxgk", "https://www.cde.org.cn/main/xxgk/listpage/4b5255eb0a84820cef4ca3e8b6bbe20c",
           records=30),
    _watch("cde-zdyz", "cde_zdyz", "https://www.cde.org.cn/zdyz/listpage/9cd8db3b7530c6fa0c86485e563f93c7", records=30),
    _watch("chictr-list", "chictr", "https://www.chictr.org.cn/searchproj.html", records=30),
    _watch("phirda-industry", "phirda_industry", "https://www.phirda.com/news_9.html", records=30, cadence=12 * _H),
    _watch("phirda-policy", "phirda_policy", "https://www.phirda.com/news_10.html", records=30),
    _watch("phirda-rd", "phirda_rd", "https://www.phirda.com/news_11.html", records=30, cadence=12 * _H),
    _watch("phirda-reform", "phirda_healthcare_reform", "https://www.phirda.com/news_12.html", records=30),
    _watch("fiercepharma", "fiercepharma", records=30, cadence=6 * _H),
    _watch("roche-releases", "roche", records=30),
    _watch("rss-press", "rss_press", records=50, cadence=6 * _H),
    _watch("hkex", "hkex"),
    _watch("hkex-reports", "hkex_reports", records=50),
    _watch("sec-filings", "sec_filings", records=30),
    _watch("sec-8k", "sec_8k", records=30, cadence=12 * _H),
    _watch("biorxiv", "biorxiv", cadence=12 * _H),
    _watch("medrxiv", "medrxiv", cadence=12 * _H),
    _watch("arxiv-qbio", "arxiv", cadence=12 * _H),
)

CATALOG: dict[str, CollectionSpec] = {s.source_id: s for s in SPECS}
FETCHABLE = frozenset(s.source_id for s in SPECS if s.fetchable)
WATCHABLE = frozenset(s.source_id for s in SPECS if s.watchable)
# 由站点插件采集的来源（投影和读取都按这个集合分派）
SITE_SOURCES = frozenset(s.source_id for s in SPECS if s.plugin == "site_crawler")
# 有附件版本的来源（加工层 clean.derived_text 只解析这些来源的 role=attachment 版本）：站点插件，加上
# 覆盖说明里写了附件的已接通来源（PubMed 全文、本地导入）
ATTACHMENT_SOURCES = SITE_SOURCES | frozenset(s.source_id for s in SPECS if s.fetchable and "attachments" in s.coverage)


def spec(source_id: str) -> CollectionSpec:
    return CATALOG[source_id]


def source_catalog() -> dict[str, dict]:
    """fabric_sources 每行的 catalog 种子（只有名称）：能采集的来源、派生来源、退役来源。"""
    names = {s.source_id: s.name for s in SPECS if s.fetchable} | DERIVED | RETIRED
    return {source_id: {"name": name} for source_id, name in names.items()}


def gaps() -> list[dict]:
    """业务需要但没接通的来源，带原因；报告和健康检查用它把缺口写明。"""
    return [
        {
            "source_id": s.source_id,
            "name": s.name,
            "status": s.status,
            "reason": s.blocked_reason or "已登记，尚无插件",
        }
        for s in SPECS
        if not s.fetchable
    ]


# 试抓一个来源（main_etl.py、ops/check_sources.py、ops/backfill_source.py）时用的读法，站点配置以外的来源各写一个：
# (查询词, 指定的来源记录号)
SAMPLE_QUERIES = {
    "clinicaltrials": ("*", []),
    "europe_pmc": ("BRCA1", []),
    "pubmed": ("BRCA1", []),
    "fda_crl": ("*", []),
    "who_don": ("*", []),
    "gdelt_doc": ("FDA approval", []),
    "sec_companyfacts": ("*", ["CIK0000320193:us-gaap:Assets", "CIK0000078003:us-gaap:Assets",
                               "CIK0001682852:us-gaap:Assets"]),
}


def sample_query(source_id):
    """试抓一个来源用的 (查询词, 记录号)：接口站点填 records，网页站点填第一个列表页网址，其余见 SAMPLE_QUERIES。"""
    if source_id in SAMPLE_QUERIES:
        return SAMPLE_QUERIES[source_id]
    from fetch.site_profiles import SITE_PROFILES  # 在这里才导入：站点配置会导入站点插件，插件又读这个目录

    profile = SITE_PROFILES[source_id]
    return ("records" if profile.get("source_type") == "api" else profile["start_urls"][0]), []
