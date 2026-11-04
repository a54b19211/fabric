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
        min_interval_seconds=0.2,
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
        "不做规避反爬的伪装，等官方数据渠道或授权。",
    ),
    CollectionSpec(
        source_id="chictr",
        name="中国临床试验注册中心",
        status="blocked",
        plugin=None,
        evidence_types=("registry_record",),
        decisions=("A", "E"),
        incremental=_NO_INCREMENT,
        rate_group="chictr.org.cn",
        min_interval_seconds=10.0,
        blocked_reason="2026-11-04 实测：普通无头浏览器 405。旧库存 12.3 万篇按真实来源回填，新增等授权渠道。",
    ),
    CollectionSpec(
        source_id="nmpa",
        name="国家药品监督管理局",
        status="blocked",
        plugin=None,
        evidence_types=("regulatory_notice",),
        decisions=("A", "C"),
        incremental=_NO_INCREMENT,
        rate_group="nmpa.gov.cn",
        min_interval_seconds=10.0,
        blocked_reason="2026-11-04 实测：普通无头浏览器 412。旧库存按真实来源回填，新增等授权渠道。",
    ),
    CollectionSpec(
        source_id="cde",
        name="国家药品审评中心",
        status="blocked",
        plugin=None,
        evidence_types=("regulatory_notice",),
        decisions=("A",),
        incremental=_NO_INCREMENT,
        rate_group="cde.org.cn",
        min_interval_seconds=10.0,
        blocked_reason="2026-11-04 实测：普通无头浏览器 202 脚本挑战空页。旧库存按真实来源回填，新增等授权渠道。",
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
        source_id="hkex",
        name="港交所披露易",
        status="declared",
        plugin=None,
        evidence_types=("corporate_filing",),
        decisions=("A", "C"),
        incremental=_NO_INCREMENT,
        rate_group="hkexnews.hk",
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
