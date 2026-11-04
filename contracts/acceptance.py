"""v3 业务验收契约：把“雷达做对了没有”写成固定、可机检的案例，而不是靠读报告的感觉。

为什么需要它：
- v3 第一部分 1.2 给了判断 A–E 各两个验收案例，第四部分给了每条纵向链路的正例/更正/异常/判断E，
  6.4 给了国家/省级/高校三种使用视角。这些只写在文档里时，最容易被“文风像内参”“又加了几个站点”替代。
- 这里每个案例都绑定 v3 原文出处（design_ref），每个出处恰好对应一个案例；
  案例必须有反例（counterexample），必须有可机检的断言（checks），禁止的捷径写进 must_not。
- 证据类型只用 v3 1.4 的 12 种（英文 slug），来源编号只用 KNOWN_SOURCES，覆盖缺口必须如实列出。
- 夹具不编造真实世界事实：没找到的真实材料标 to_be_sourced；合成资料只验证流程（6.4），
  不能据此宣布覆盖了真实场景。

v2 按审阅 8 条裁定修订：
1. 来源按覆盖的数据集分开表达（openfda 写清端点和旧库存量，CRL 单列），同上游不算两份独立证实；
2. 来源状态分三栏（旧实现 / 新框架接通 / 生产在采或已索引），不知道写 unknown；
3. 更正验收看判断维持还是修订、旧引用保留、受影响计算更新，不只看“文件变了”；
4. E 案例只在 E.json 定义，其他链路用 related_cases 引用，计分只算一次；
5. E 的触发方式覆盖新材料自动触发、用户追问、历史回放，结果维持和修订都要验；
6. 材料真实性（data_truth）与故障注入（fault_injected）分开标，合成永远不算真实覆盖；
7. 目标材料（data_basis）与执行状态（status）分开，公开回放要有内容绑定的可见性证明，比谁先判断对；
8. 内参除 1.3 五项外保留决策对象、口径、内部底册缺口、行动条件和不确定性，上报不把不确定写成确定。

本模块只定义契约和校验，不跑雷达；以后雷达的报告/运行记录按 checks 逐条判定。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "bioradar.acceptance.v2"

# ---------- 词表 ----------

# v3 1.4 的 12 种证据类型，顺序与原文一致。用英文 slug 是为了让程序和夹具不受中文写法差异影响，
# 中文名留着给报告和人读。
EVIDENCE_TYPES: dict[str, str] = {
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

# 来源状态分三栏（审阅裁定 2）：旧实现在不在、新采集框架接没接通、生产上是否真的在采/已索引。
# 以前一个 coverage 字段把三件事混在一起，“旧库存量”“旧配置”“已采通”互相冒充；分开后每栏只回答一件事，
# 不知道就写 unknown，不拿 v3 正文或旧来源目录单独当真相。
OldImplementation = Literal["yes", "no", "unknown"]
NewFramework = Literal[
    "active",  # 新框架里插件已启用
    "legacy_plugin_unverified",  # 以 legacy 插件接入，但没对原站实测
    "declared_not_connected",  # 只声明了，没接通
    "no",
    "unknown",
]
LiveStatus = Literal[
    "collecting_and_indexed",  # 生产上在采且已索引
    "old_stock_only",  # 没有新采集，只有旧库存量
    "no",
    "unknown",  # 需要证据，不能当“是”
]
Availability = Literal["has_material", "unknown", "gap"]


class SourceInfo(BaseModel):
    """来源目录里的一项。

    - datasets 写这个来源编号实际覆盖的数据集/端点/数据类型（裁定 1）：来源按覆盖的数据集分开表达，
      不能用一个笼统名字把几个端点混在一起；
    - upstream 写上游系统：同一上游的两个来源编号（如 openFDA 的说明书端点与 CRL 端点）
      永远不算两份独立证实；
    - old_stock_docs 只记录已核对过的旧库存量条数；“旧库存量”是覆盖状态，不是来源定义。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    old_implementation_exists: OldImplementation
    new_framework_connected: NewFramework
    live_collecting_or_indexed: LiveStatus
    datasets: tuple[str, ...] = ()
    upstream: str = ""
    old_stock_docs: dict[str, int] = Field(default_factory=dict)
    # True 表示编号是本模块按 v3 原文新加的（不在任务给定的编号表里），方便审阅时逐个核对
    introduced_here: bool = False
    note: str = ""

    @model_validator(mode="after")
    def _status_consistent(self) -> SourceInfo:
        # 没接通或未实测的插件，不能同时声称生产在采：这正是以前“已有”被夸大的地方
        if (self.new_framework_connected in ("legacy_plugin_unverified", "declared_not_connected", "no")
                and self.live_collecting_or_indexed == "collecting_and_indexed"):
            raise ValueError(
                f"{self.name}：new_framework_connected={self.new_framework_connected} 时不能写 "
                "collecting_and_indexed")
        # 有旧库存量就说明曾有旧实现；写 no 是自相矛盾
        if ((self.live_collecting_or_indexed == "old_stock_only" or self.old_stock_docs)
                and self.old_implementation_exists == "no"):
            raise ValueError(f"{self.name}：有旧库存量时 old_implementation_exists 不能是 no")
        return self

    @property
    def availability(self) -> Availability:
        """验收时这个来源能不能提供材料：在采/已索引或有旧库存量才算有材料；
        生产状态未知的单列为 unknown（要先拿证据），确定没有的才是缺口。"""
        if self.live_collecting_or_indexed in ("collecting_and_indexed", "old_stock_only") \
                or self.old_stock_docs:
            return "has_material"
        if self.live_collecting_or_indexed == "unknown":
            return "unknown"
        return "gap"


def _src(name: str, old: OldImplementation, new: NewFramework, live: LiveStatus, note: str = "",
         intro: bool = False, **kw: Any) -> SourceInfo:
    return SourceInfo(name=name, old_implementation_exists=old, new_framework_connected=new,
                      live_collecting_or_indexed=live, note=note, introduced_here=intro, **kw)


# 下面“缺口依据 v3”的条目：生产不在采取自 v3 缺口清单和任务来源表，旧实现与新框架两栏未逐个核实，写 unknown。
_V3_GAP = "缺口依据 v3；旧实现与新框架两栏未核实"

KNOWN_SOURCES: dict[str, SourceInfo] = {
    # 审阅给出的事实
    "clinicaltrials": _src("ClinicalTrials.gov", "unknown", "active", "collecting_and_indexed",
                           "新框架已接通；生产为窄 watch 在采，已索引"),
    "europe_pmc": _src("Europe PMC", "unknown", "active", "collecting_and_indexed",
                       "新框架已接通；生产为窄 watch 在采，已索引"),
    "who_don": _src("WHO Disease Outbreak News", "unknown", "active", "collecting_and_indexed",
                    "新框架已接通；生产为窄 watch 在采，已索引；v3 写只有最近 30 天"),
    "sec_companyfacts": _src("SEC companyfacts", "unknown", "active", "unknown",
                             "新框架插件已启用；生产 watch 是否配置未知（unknown，需要证据），不能写成在采"),
    "nmpa": _src("NMPA", "yes", "legacy_plugin_unverified", "old_stock_only",
                 "旧代码在；新框架以 legacy 插件接入但未对原站实测；生产无新采集，只有旧库存量"),
    "cde": _src("CDE", "yes", "legacy_plugin_unverified", "old_stock_only",
                "旧代码在；新框架以 legacy 插件接入但未对原站实测；生产无新采集，只有旧库存量"),
    "chictr": _src("ChiCTR", "yes", "legacy_plugin_unverified", "old_stock_only",
                   "旧代码在；新框架以 legacy 插件接入但未对原站实测；生产无新采集，只有旧库存量"),
    "cde_ctr": _src("CDE 药物临床试验登记平台（CTR）", "no", "no", "no", "三栏都没有；与 ChiCTR 是两个平台"),
    "hkex": _src("港交所披露", "yes", "declared_not_connected", "no",
                 "旧配置在（config/sites/hkex.py）；新框架只声明未接通；生产不在采"),
    # 任务给定、审阅未给事实：不知道的写 unknown
    "pubmed": _src("PubMed", "unknown", "unknown", "unknown", "v3 写“旧配置”，三栏均未核实，需要证据"),
    "fda_crl": _src(
        "FDA 完整回复函（CRL）", "unknown", "unknown", "unknown",
        "取自 openFDA transparency 端点（按审阅意见）；与 openfda 同上游，不算两份独立证实；"
        "采集状态未核实，需要证据",
        datasets=("CRL：api.fda.gov/transparency/crl.json",), upstream="api.fda.gov"),
    "cninfo": _src("巨潮/沪深北交易所公告与财报", "unknown", "unknown", "no", _V3_GAP),
    "biorxiv": _src("bioRxiv", "unknown", "unknown", "no", _V3_GAP),
    "drugs_fda": _src("Drugs@FDA 批准数据", "unknown", "unknown", "no", _V3_GAP),
    # 本模块按 v3 原文新加
    "openfda": _src(
        "openFDA 药品说明书与召回执行报告", "unknown", "unknown", "unknown",
        "旧库存量是覆盖状态：fda_api_drug_label 258,915 条、fda_api_enforcement 17,684 条；"
        "新框架是否接通、生产是否在采未核实；CRL 另列 fda_crl（同上游）",
        intro=True, upstream="api.fda.gov",
        datasets=("drug label：api.fda.gov/drug/label.json（药品说明书）",
                  "enforcement：召回执行报告（旧库集合 fda_api_enforcement；对应哪类产品的 "
                  "enforcement 端点待核实）"),
        old_stock_docs={"fda_api_drug_label": 258915, "fda_api_enforcement": 17684}),
    "ema": _src("EMA", "unknown", "unknown", "no", _V3_GAP + "（判断A）", intro=True),
    "company_ir": _src("企业 IR 管线页和财报演示", "unknown", "unknown", "no",
                       _V3_GAP + "（判断A）；不是独立无偏的验证来源", intro=True),
    "conference_abstracts": _src("会议摘要", "unknown", "unknown", "no", _V3_GAP + "（判断A、B）",
                                 intro=True),
    "patents": _src("专利", "unknown", "unknown", "no", _V3_GAP + "（判断A；6.4 高校视角）", intro=True),
    "medrxiv": _src("medRxiv", "unknown", "unknown", "no", _V3_GAP + "（判断B）", intro=True),
    "arxiv": _src("arXiv 相关类别", "unknown", "unknown", "no",
                  _V3_GAP + "（判断B：AI 制药等不限于 q-bio）", intro=True),
    "guidelines": _src("指南", "unknown", "unknown", "no", _V3_GAP + "（判断B）", intro=True),
    "sec_filings": _src("SEC filing 原文与交易披露", "unknown", "unknown", "no", _V3_GAP + "（判断C）",
                        intro=True),
    "nhsa": _src("国家医保局", "unknown", "unknown", "no", _V3_GAP + "（判断C）", intro=True),
    "procurement_prices": _src("采购与价格资料", "unknown", "unknown", "no", _V3_GAP + "（判断C）",
                               intro=True),
    "industry_media": _src("行业媒体", "unknown", "unknown", "no", _V3_GAP + "（判断C）", intro=True),
    "china_cdc": _src("中国疾控", "unknown", "unknown", "no", _V3_GAP + "（判断D）", intro=True),
    "health_commissions": _src("国家和地方卫健委通报", "unknown", "unknown", "no", _V3_GAP + "（判断D）",
                               intro=True),
    "ecdc": _src("ECDC", "unknown", "unknown", "no", _V3_GAP + "（判断D）", intro=True),
    "us_cdc": _src("US CDC", "unknown", "unknown", "no", _V3_GAP + "（判断D）", intro=True),
    "promed": _src("ProMED", "unknown", "unknown", "no", _V3_GAP + "（判断D）", intro=True),
    "news_media": _src("媒体线索", "unknown", "unknown", "no", _V3_GAP + "（判断D）", intro=True),
    "social_media": _src("社媒线索", "unknown", "unknown", "no", _V3_GAP + "（判断D、1.4）", intro=True),
    "national_supply_stats": _src("全国统一口径供需/产能/采购/贸易数据", "unknown", "unknown", "no",
                                  _V3_GAP + "（6.4 国家层）", intro=True),
    "policy_execution_data": _src("政策执行和结果数据", "unknown", "unknown", "no",
                                  _V3_GAP + "（6.4 国家层）", intro=True),
    "local_industry_ledger": _src("辖区企业/园区/产线底册（授权内部台账）", "unknown", "unknown", "no",
                                  _V3_GAP + "（6.4 省级）", intro=True),
    "research_institution_ledger": _src("项目/经费/设备/队伍/成果授权台账", "unknown", "unknown", "no",
                                        _V3_GAP + "（6.4 高校/科研机构）", intro=True),
    "grants": _src("公开的基金资助信息", "unknown", "unknown", "no", _V3_GAP + "（6.4 高校/科研机构）",
                   intro=True),
}


def count_independent_upstreams(source_ids: list[str]) -> int:
    """按上游系统合并后，还剩几份可能独立的来源。同一上游的多个来源编号只算一份，
    这样 openFDA 说明书和同出 api.fda.gov 的 CRL 永远不会被算成两份独立证实。"""
    groups = {KNOWN_SOURCES[s].upstream or s for s in source_ids}
    return len(groups)

# 断言种类：以后判定一份报告或一次运行是否合格时，每种都要有确定的判定程序。
# 只收 v3 点名的要求，故意不收“文风像内参”之类的主观项（6.4：评价不看文风）。
CHECK_KINDS: dict[str, str] = {
    # 1.3 与第四部分正例：每份报告都要能回指原文、如实写缺口
    "cites_fixed_version": "每条依据回指固定版本：源版本、加工版本、定位、片段哈希（6.1 复现引用）",
    "states_limitation": "写明局限，包括口径、覆盖和方法的局限",
    "states_next_evidence": "写明下一条会改变判断的证据是什么",
    "gap_stated": "没接入或没解析出来的来源写成缺口，不写成“已覆盖”或“不存在”",
    # 判断 A
    "detects_change": "由新旧版本的确定性差分产出观测（如登记状态改变、终点修改），并进入专题",
    "no_efficacy_from_status": "不用“到了几期”或 COMPLETED 等登记状态推出疗效或失败",
    "no_hype_overestimate": "宣传热度、新闻数量不抬高证据成熟度和跟进建议",
    "evidence_maturity_compared": "同口径比较证据成熟度和差异化（人群、终点、对照、样本量）",
    "uses_graph_lookup": "确实需要查图谱：身份/别名或竞品关联（同靶点、同适应症）",
    # 判断 B
    "evidence_level_stated": "区分提出主张、数据进步、证据升级、可能改变决策四个层次并给出证据等级",
    "replication_counted": "统计独立重复验证；同源材料不算独立重复",
    "retraction_tracked": "更正和撤稿关系追踪到受影响的主张",
    "uses_mech_evaluation": "确实需要机制评价（汪的 MechEvidence），按命题+适用范围+材料版本评价",
    "no_volume_proxy": "材料、公告、论文、转发的数量只当信息活动指标，不当实质指标",
    # 判断 C
    "measurement_has_caliber": "每个测量值带报告期、单位、范围、分母；分母不知道写 unknown，不补",
    "contract_not_revenue": "合同额/订单额与实际收入分开，不互相替代",
    "change_point_reported": "口径对齐后给出指标序列和变点",
    # 判断 D
    "visibility_cutoff_respected": "回放只用截止时间前可见的版本，可见性按 Fabric 的回放模式判定",
    "same_origin_not_independent": "转发、转述、同源链接不算多份独立证实",
    "competing_explanations_listed": "并列竞争性解释，不只给一个结论",
    "verification_priority_stated": "给出核实优先级",
    "claim_existence_not_truth": "区分“这条线索/主张确实出现过”和“主张内容成立”",
    "baseline_compared": "与基线比较，而不是只看绝对数",
    "timeliness_evaluated": "误报、漏报、核实延迟一起评价",
    # 判断 E 与更正
    "recompute_only_affected": "更正或撤权只重算受影响的事件、时期、指标和命题，其余不动",
    "keeps_old_judgment": "旧判断版本保留可查，不被覆盖",
    "revision_reason_stated": "修订说明写清判断为什么变（或为什么维持）以及依据的固定版本",
    "follow_up_supported": "修订后的判断可以接着追问",
    "permission_enforced": "撤权后已存的报告、摘要、缓存、快照也不再泄露受限内容（6.1）",
    # 第四部分 异常
    "no_duplicate_on_restart": "重启后不重复落盘、不重复产出观测",
    "resumes_after_partial_failure": "做到一半失败能从检查点接着做，欠账可见",
    "no_derived_feedback_loop": "图谱、报告、计算结果不再进入抽取，加工产物不形成回路",
    "identifier_not_double_counted": "同一 PMID、NCT 等编号（或同一公司+报告期+指标）不重复计数",
    # 6.4 使用视角
    "answers_pending_question": "回答了待决问题（含期限）",
    "key_evidence_not_missed": "关键证据没有漏",
    "counter_evidence_changes_judgment": "反证确实能改变判断，而不是列出后不理",
    "updatable_with_new_evidence": "后续出现新证据时能更新判断",
    "analysis_separate_from_recommendation": "分析判断和对策建议分开写，价值取舍由任务方给出",
    "no_causal_from_before_after": "只有前后差异时只报告“描述性关联”，不下因果结论",
    "no_precise_figure_without_denominator": "没有分母时不报精确的依赖度或比例",
    "comparison_controls_caliber": "区域之间、机构之间比较时控制口径、规模和时间",
    "no_real_coverage_claim": "合成资料只验证流程，不据此宣布已覆盖真实场景",
    # 审阅裁定 1、2：来源关系与状态
    "same_upstream_not_independent": "同一上游的不同来源编号（如 openFDA 与 FDA CRL）在独立证实数中合计为一",
    "source_status_verified": "状态未知的来源先拿证据（watch 配置、采集记录、索引计数），拿不到就按缺口写",
    # 审阅裁定 3：更正看判断结果，不只看文件变了
    "judgment_maintained_or_revised": "明确写出判断是维持还是修订，并给出理由；只报告“文件变了”不算",
    "old_citations_kept": "旧引用仍指向更正前的固定版本并可解析，不被改写成指向新版本",
    "affected_computations_updated": "受影响的计算（指标、序列、变点、证据等级）确实用新值重算，列出改前改后",
    # 审阅裁定 5：判断更新的触发方式
    "auto_triggered_by_new_version": "新版本、撤稿或撤权事件入库后自动触发复核，不靠人工发起",
    # 审阅裁定 6：故障注入
    "fault_not_reported_as_incident": "注入的隔离故障在运行记录和报告里标为注入，不写成生产事故或来源故障",
    # 审阅裁定 7：公开回放
    "visibility_proof_bound": "进截止前的每份材料都有与内容绑定的可见性证明（内容哈希+证明时间），没有就不进",
    "judged_correctly_first": "比“谁先判断对”而不是谁最早报警（13.5 口径）",
    # 审阅裁定 8：内参
    "uncertainty_not_upgraded": "面向更高层级时不确定性原样保留，四件事分别交代，不压成确定结论或精确百分比",
}

# 6.4 的五条评价标准（不看文风）
VIEW_EVALUATION_CHECKS: tuple[str, ...] = (
    "answers_pending_question",
    "key_evidence_not_missed",
    "counter_evidence_changes_judgment",
    "gap_stated",  # “选项的前提和数据缺口是否如实写明”
    "updatable_with_new_evidence",
)

# v3 1.3：每份报告必须说清
REPORT_MUST_SAY: tuple[str, ...] = (
    "主要判断", "业务影响", "支持和反对的证据", "局限", "下一条会改变判断的证据",
)

# v3 1.2 各判断“报告”一行点名的内容
JUDGMENT_MUST_SAY: dict[str, tuple[str, ...]] = {
    "A": ("跟进建议和理由", "证据成熟度", "差异化", "失败原因", "下一个里程碑"),
    "B": ("突破判断", "证据等级", "反证"),
    "C": ("赛道判断", "关键指标序列", "变点", "口径局限"),
    "D": ("威胁判断", "核实优先级"),
    "E": ("修订说明", "判断为什么变", "旧判断保留", "可以追问"),
    "view": (),
}

# 审阅裁定 8：内参除 1.3 五项外，还要保留 6.4 的决策对象、口径、内部底册缺口、行动条件和不确定性。
# 这些恰是“写得像内参”时最容易被省掉、却决定能不能用来做决定的内容。
VIEW_MUST_SAY: tuple[str, ...] = ("决策对象", "口径", "内部底册缺口", "行动条件", "不确定性")

# v3 6.4 内参正文结构，只要求使用视角案例
BRIEFING_STRUCTURE: tuple[str, ...] = (
    "待决问题与期限",
    "主要判断及其影响",
    "支持和反对的证据以及证据缺口",
    "情景或可选行动及其代价、前提和风险",
    "需补充的关键资料与重新复核条件",
)

# v3 点名禁止的捷径；同一判断的每个案例都必须把它们写进 must_not
REQUIRED_MUST_NOT: dict[str, tuple[str, ...]] = {
    "A": ("用“到了几期”或“COMPLETED”代替疗效",),
    "B": ("按新闻热度或论文数量认定突破",),
    "C": ("公告变多等于产业增长", "合同额当作收入"),
    "D": ("广泛转发算成多份独立证实",),
    "E": ("重写一份没有历史的报告",),
    "view": ("把合成资料当作已覆盖真实场景", "没接入的来源写成已覆盖",
             "面向更高层级时把不确定性写成确定结论"),
}

# 按案例性质追加的禁止项（审阅裁定 3、6、7）
MUST_NOT_CORRECTION = "只报告“文件变了”而不判断维持还是修订"
MUST_NOT_FAULT = "把注入的隔离故障写成生产事故"
MUST_NOT_EARLIEST_ALARM = "把最早报警当作成功指标"

Judgment = Literal["A", "B", "C", "D", "E", "view"]
CaseKind = Literal[
    "business_acceptance",  # v3 1.2 各判断的“验收案例”
    "chain_positive",  # 第四部分 正例
    "chain_correction",  # 第四部分 更正
    "chain_anomaly",  # 第四部分 异常
    "chain_judgment_e",  # 第四部分 判断E
    "usage_view",  # 6.4 三种使用视角
]
Chain = Literal["A", "B", "C", "D"]


class DesignRef(BaseModel):
    """一个 v3 出处应当落在哪个判断、哪类案例、哪条链路上。用表固定下来，
    夹具里的 judgment / case_kind / chain 必须与之一致，避免把案例挪到不相干的判断下面凑数。"""

    model_config = ConfigDict(frozen=True)

    judgment: Judgment
    case_kind: CaseKind
    chain: Chain | None = None


def _build_design_refs() -> dict[str, DesignRef]:
    refs: dict[str, DesignRef] = {}
    for j in "ABCDE":
        for n in (1, 2):
            refs[f"v3 1.2 判断{j} 验收案例{n}"] = DesignRef(judgment=j, case_kind="business_acceptance")
    for chain in "ABCD":
        for label, kind in (("正例", "chain_positive"), ("更正", "chain_correction"),
                            ("异常", "chain_anomaly")):
            refs[f"v3 第四部分 链路{chain} {label}"] = DesignRef(judgment=chain, case_kind=kind, chain=chain)
        # 链路上的“判断E”验的是判断更新机制，归到 E
        refs[f"v3 第四部分 链路{chain} 判断E"] = DesignRef(
            judgment="E", case_kind="chain_judgment_e", chain=chain)
    for label in ("使用视角1 国家层", "使用视角2 省级", "使用视角3 高校/科研机构"):
        refs[f"v3 6.4 {label}"] = DesignRef(judgment="view", case_kind="usage_view")
    return refs


REQUIRED_DESIGN_REFS: dict[str, DesignRef] = _build_design_refs()

# 按案例类型必须出现的断言：更正和异常是第四部分逐条点名的，缺一条就不算验过
REQUIRED_CHECKS_BY_KIND: dict[str, tuple[str, ...]] = {
    # 裁定 3：更正不只看“文件变了”，要看判断维持/修订、旧引用、受影响计算
    "chain_correction": ("recompute_only_affected", "keeps_old_judgment", "judgment_maintained_or_revised",
                         "old_citations_kept", "affected_computations_updated"),
    "chain_anomaly": ("no_duplicate_on_restart", "resumes_after_partial_failure",
                      "no_derived_feedback_loop", "identifier_not_double_counted"),
    "chain_judgment_e": ("keeps_old_judgment", "revision_reason_stated"),
    "usage_view": VIEW_EVALUATION_CHECKS + ("no_real_coverage_claim", "uncertainty_not_upgraded"),
}
REQUIRED_CHECKS_BY_JUDGMENT: dict[str, tuple[str, ...]] = {
    "E": ("keeps_old_judgment", "revision_reason_stated", "judgment_maintained_or_revised"),
}

EXPECTED_FILES: dict[str, Judgment] = {
    "A.json": "A", "B.json": "B", "C.json": "C", "D.json": "D", "E.json": "E", "views.json": "view",
}

# 目标材料类型（案例打算用什么材料验）。“待找”不是目标，是现状，放在 data_truth / status 里（裁定 6、7）。
DataBasis = Literal["real_fixed_version", "real_public_replay", "synthetic_flow_only"]
# 现在手上材料的真实性：真实固定版本 / 合成 / 待找
DataTruth = Literal["real_fixed_version", "synthetic", "to_be_sourced"]
# 执行状态：与目标分开，目标是公开回放不代表现在能跑或已通过
CaseStatus = Literal["to_be_sourced", "ready", "passed", "failed"]
WorkMode = Literal["discovery", "investigation", "judgment_update"]

# 裁定 3：每条链路的“更正”是什么。B 是勘误改数值，C 是公告/附件新版本改了测量值或口径。
CorrectionTrigger = Literal[
    "registry_field_correction",
    "erratum_number_change",
    "filing_measurement_or_caliber_change",
    "notice_count_or_definition_change",
]
CORRECTION_TRIGGER_BY_CHAIN: dict[str, str] = {
    "A": "registry_field_correction",
    "B": "erratum_number_change",
    "C": "filing_measurement_or_caliber_change",
    "D": "notice_count_or_definition_change",
}

# 裁定 5：判断 E 的三种触发方式与预期结果
Trigger = Literal["new_material_auto", "user_follow_up", "historical_replay"]
ExpectedOutcome = Literal["maintain", "revise", "either_with_reason"]
REQUIRED_CHECKS_BY_TRIGGER: dict[str, tuple[str, ...]] = {
    "new_material_auto": ("auto_triggered_by_new_version",),
    "user_follow_up": ("follow_up_supported",),
    "historical_replay": ("visibility_cutoff_respected", "visibility_proof_bound"),
}


class AcceptanceContractError(ValueError):
    """夹具集合层面的错误（重复编号、文件缺失、案例放错文件）。继承 ValueError，与单案例校验一致。"""


# ---------- 模型 ----------


class Scenario(BaseModel):
    """一个具体情景及雷达应有的可观察表现。
    expected 写“必须发现/必须说出”的东西；must_not_conclude 写“不能得出”的结论——
    反例的价值全在后者，所以反例里它不能为空。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: str = Field(min_length=1)
    expected: list[str] = Field(min_length=1)
    must_not_conclude: list[str] = Field(default_factory=list)


class Check(BaseModel):
    """对将来某份报告或某次运行的一条可机检断言。kind 只能取 CHECK_KINDS，
    这样每种断言都能写成固定的判定程序；detail 写清本案例里具体判什么。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    detail: str = Field(min_length=1)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in CHECK_KINDS:
            raise ValueError(f"未知断言种类 {v!r}")
        return v


class AcceptanceCase(BaseModel):
    """一个业务验收案例。字段都是必填：缺任何一项，验收就会退化成“看报告感觉对不对”。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 稳定编号，便于在回放评测和审阅记录里引用；前缀必须与 judgment 对应（视角用 V）
    id: str = Field(pattern=r"^(A|B|C|D|E|V)-\d{2}$")
    # 服务哪个业务判断（A–E）或 6.4 使用视角
    judgment: Judgment
    # 来自 v3 哪一类验收（1.2 验收案例 / 第四部分四项 / 6.4 视角），与 design_ref 表一致
    case_kind: CaseKind
    # 第四部分的纵向链路（A 结构化登记 / B 非结构化文献 / C 历史时序 / D 弱信号）；1.2 与 6.4 案例为空
    chain: Chain | None = None
    # v3 1.1 三种工作方式：主动发现、指定调查和追问、长期判断更新
    work_mode: WorkMode
    # v3 原文出处；每个出处恰好一个案例，防止漏验或重复凑数
    design_ref: str
    # 具体的业务问题（中文）；回答不了它的报告不合格
    question: str = Field(min_length=1)
    # 需要的证据类型（v3 1.4 词表 slug）；来源目录按证据类型组织，不按单一 P 级
    required_evidence: list[str] = Field(min_length=1)
    # 需要的编号和时间字段；时间和实体对不上，同口径比较和同源判断都做不了
    required_entities_and_times: list[str] = Field(min_length=1)
    # 能提供材料的来源（生产在采/已索引，或有旧库存量）；只用来说明“能测什么”，不代表已实测
    sources_available: list[str]
    # 生产状态未知的来源（如插件已启用但 watch 是否配置不明）：不能写进 available，要先拿证据
    sources_status_unknown: list[str]
    # 需要但没接入的来源；必须如实列出，报告里也要写成缺口
    sources_gap: list[str]
    # 报告必须说清的内容：1.3 五项 + 本判断点名项（+ 视角的内参结构）
    must_say: list[str] = Field(min_length=1)
    # v3 点名禁止的捷径
    must_not: list[str] = Field(min_length=1)
    # 正例：雷达应当发现/说出的情景
    positive: Scenario
    # 反例：看起来像、但不能得出同样结论的情景；没有反例的验收只会奖励“多报”
    counterexample: Scenario
    # 可机检断言；为空就无法判定通过与否
    checks: list[Check] = Field(min_length=1)
    # 目标材料类型：real_fixed_version（真实材料的固定版本）/ real_public_replay（公开历史回放）/
    # synthetic_flow_only（只验证流程）
    data_basis: DataBasis
    # 现在手上的材料是什么（裁定 6）：真实固定版本 / 合成 / 待找；合成永远不算真实覆盖
    data_truth: DataTruth
    # 是否在材料上注入隔离故障（裁定 6）；注入的故障不能写成生产事故
    fault_injected: bool
    # 执行状态（裁定 7）：与目标分开；材料没到位就是 to_be_sourced，不能跑也不能算通过
    status: CaseStatus
    # 需要什么材料、按什么标准找；to_be_sourced 时就是找材料的说明
    material_note: str = Field(min_length=1)
    # 案例里明确标为合成的部分（如 6.4 的内部台账）；有合成部分就不能宣称覆盖真实场景
    synthetic_components: list[str] = Field(default_factory=list)
    # 相关案例编号（裁定 4）：只是引用，不是复制；同一案例只在一个文件里定义，计分只算一次
    related_cases: list[str] = Field(default_factory=list)
    # 更正类案例的更正是什么（裁定 3）；链路更正案例必填且必须与链路一致
    correction_trigger: CorrectionTrigger | None = None
    # 判断 E 的触发方式与预期结果（裁定 5）；E 案例必填
    trigger: Trigger | None = None
    expected_outcome: ExpectedOutcome | None = None

    @property
    def executable(self) -> bool:
        """材料到位、可以跑（或已跑过）。to_be_sourced 的案例既不能跑，也不能算通过。"""
        return self.status in ("ready", "passed", "failed")

    @property
    def counts_as_real_coverage(self) -> bool:
        """只有在真实固定版本上跑通、且不含合成部分的案例，才能算进真实覆盖。"""
        return (self.status == "passed" and self.data_truth == "real_fixed_version"
                and self.data_basis != "synthetic_flow_only" and not self.synthetic_components)

    @field_validator("related_cases")
    @classmethod
    def _related_ids(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if not re.fullmatch(r"(A|B|C|D|E|V)-\d{2}", x)]
        if bad:
            raise ValueError(f"related_cases 里有不合法的案例编号 {bad}")
        if len(set(v)) != len(v):
            raise ValueError(f"related_cases 重复 {v}")
        return v

    @field_validator("required_evidence")
    @classmethod
    def _known_evidence(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in EVIDENCE_TYPES]
        if bad:
            raise ValueError(f"未知证据类型 {bad}（只能用 EVIDENCE_TYPES 的英文 slug）")
        return v

    @field_validator("sources_available", "sources_status_unknown", "sources_gap")
    @classmethod
    def _known_sources(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in KNOWN_SOURCES]
        if bad:
            raise ValueError(f"未知来源编号 {bad}")
        if len(set(v)) != len(v):
            raise ValueError(f"来源编号重复 {v}")
        return v

    @model_validator(mode="after")
    def _business_rules(self) -> AcceptanceCase:
        errors: list[str] = []
        prefix = "V" if self.judgment == "view" else self.judgment
        if not self.id.startswith(prefix + "-"):
            errors.append(f"id {self.id} 与 judgment {self.judgment} 不符")

        ref = REQUIRED_DESIGN_REFS.get(self.design_ref)
        if ref is None:
            errors.append(f"design_ref {self.design_ref!r} 不是 v3 的验收出处")
        elif (ref.judgment, ref.case_kind, ref.chain) != (self.judgment, self.case_kind, self.chain):
            errors.append(
                f"design_ref {self.design_ref!r} 应为 judgment={ref.judgment} case_kind={ref.case_kind} "
                f"chain={ref.chain}")

        # 三个来源列表要与来源目录的状态一致：有材料的、状态未知的、缺口，各归各位（裁定 2）
        placement = (("sources_available", self.sources_available, "has_material"),
                     ("sources_status_unknown", self.sources_status_unknown, "unknown"),
                     ("sources_gap", self.sources_gap, "gap"))
        for field, ids, want in placement:
            for sid in ids:
                got = KNOWN_SOURCES[sid].availability
                if got != want:
                    errors.append(f"{sid} 在来源目录里是 {got}，不能列为 {field}")
        listed = self.sources_available + self.sources_status_unknown + self.sources_gap
        dup_src = sorted(s for s, n in Counter(listed).items() if n > 1)
        if dup_src:
            errors.append(f"来源同时出现在多个来源列表中：{dup_src}")

        need_say = REPORT_MUST_SAY + JUDGMENT_MUST_SAY[self.judgment]
        if self.judgment == "view":
            need_say += BRIEFING_STRUCTURE + VIEW_MUST_SAY
        missing_say = [x for x in need_say if x not in self.must_say]
        if missing_say:
            errors.append(f"must_say 缺少 {missing_say}")
        need_not = list(REQUIRED_MUST_NOT[self.judgment])
        if self.case_kind == "chain_correction":
            need_not.append(MUST_NOT_CORRECTION)
        if self.fault_injected:
            need_not.append(MUST_NOT_FAULT)
        if self.data_basis == "real_public_replay":
            need_not.append(MUST_NOT_EARLIEST_ALARM)
        missing_not = [x for x in need_not if x not in self.must_not]
        if missing_not:
            errors.append(f"must_not 缺少点名的捷径 {missing_not}")

        if not self.counterexample.must_not_conclude:
            errors.append("counterexample.must_not_conclude 不能为空：反例必须写明不能得出的结论")

        kinds = {c.kind for c in self.checks}
        need_checks = {"cites_fixed_version"}
        need_checks |= set(REQUIRED_CHECKS_BY_KIND.get(self.case_kind, ()))
        need_checks |= set(REQUIRED_CHECKS_BY_JUDGMENT.get(self.judgment, ()))
        if self.data_basis == "synthetic_flow_only" or self.synthetic_components:
            need_checks.add("no_real_coverage_claim")
        if self.data_basis == "real_public_replay":
            # 裁定 7：没有内容绑定的可见性证明就不进截止前；比谁先判断对，不比谁最早报警
            need_checks |= {"visibility_cutoff_respected", "visibility_proof_bound", "judged_correctly_first"}
        if self.sources_gap:
            need_checks.add("gap_stated")
        if self.sources_status_unknown:
            need_checks.add("source_status_verified")
        if count_independent_upstreams(listed) < len(listed):
            need_checks.add("same_upstream_not_independent")
        if self.fault_injected:
            need_checks.add("fault_not_reported_as_incident")
        if self.trigger:
            need_checks |= set(REQUIRED_CHECKS_BY_TRIGGER[self.trigger])
        missing_checks = sorted(need_checks - kinds)
        if missing_checks:
            errors.append(f"checks 缺少 {missing_checks}")

        if self.judgment == "E":
            if self.work_mode != "judgment_update":
                errors.append("判断 E 本身就是长期判断更新，work_mode 必须是 judgment_update")
            if self.trigger is None:
                errors.append("判断 E 案例必须写 trigger（新材料自动触发 / 用户追问 / 历史回放）")
            if self.expected_outcome is None:
                errors.append("判断 E 案例必须写 expected_outcome（maintain / revise / either_with_reason）")

        if self.case_kind == "chain_correction":
            want = CORRECTION_TRIGGER_BY_CHAIN[self.chain] if self.chain else None
            if self.correction_trigger != want:
                errors.append(f"链路{self.chain} 更正的 correction_trigger 必须是 {want}")
        if self.case_kind == "chain_anomaly" and not self.fault_injected:
            errors.append("异常案例就是故障案例，fault_injected 必须为 true")

        # 裁定 6、7：目标、材料真实性、执行状态三者一致
        if self.data_basis == "synthetic_flow_only" and self.data_truth != "synthetic":
            errors.append("目标是 synthetic_flow_only 时 data_truth 必须是 synthetic")
        if self.data_basis != "synthetic_flow_only" and self.data_truth == "synthetic":
            errors.append("目标是真实材料时 data_truth 不能是 synthetic：合成材料不算真实覆盖")
        if self.data_truth == "to_be_sourced" and self.status != "to_be_sourced":
            errors.append(f"材料待找（data_truth=to_be_sourced）时 status 不能是 {self.status}")

        if errors:
            raise ValueError(f"案例 {self.id}：" + "；".join(errors))
        return self


# ---------- 入口 ----------


def validate_case(case: AcceptanceCase | dict[str, Any]) -> AcceptanceCase:
    """校验单个案例并返回模型对象。不合格时抛 ValueError（pydantic.ValidationError 也是 ValueError）。"""
    if isinstance(case, AcceptanceCase):
        return case
    return AcceptanceCase.model_validate(case)


def validate_cases(cases: list[AcceptanceCase | dict[str, Any]]) -> list[AcceptanceCase]:
    """校验一组案例，并检查 id 与 design_ref 不重复——重复意味着有案例被复制凑数或被覆盖。"""
    out = [validate_case(c) for c in cases]
    for field in ("id", "design_ref"):
        counts = Counter(getattr(c, field) for c in out)
        dup = sorted(k for k, n in counts.items() if n > 1)
        if dup:
            raise AcceptanceContractError(f"{field} 重复：{dup}")
    return out


def validate_case_set(cases: list[AcceptanceCase]) -> list[AcceptanceCase]:
    """整套夹具层面的规则；只对完整案例集有意义，所以不放进 validate_cases。

    - related_cases 只能引用存在的其他案例（裁定 4）；
    - 第四部分每条链路的“判断E”案例定义在 E.json，必须被该链路文件里的某个案例引用，
      这样链路验收能找到它，又不会在链路文件里再复制一份重复计分；
    - E 案例的触发方式覆盖新材料自动触发、用户追问、历史回放三种，预期结果同时有维持和修订（裁定 5）。
    """
    validate_cases(list(cases))
    by_id = {c.id: c for c in cases}
    errors: list[str] = []
    for c in cases:
        for rid in c.related_cases:
            if rid == c.id:
                errors.append(f"{c.id} 的 related_cases 引用了自己")
            elif rid not in by_id:
                errors.append(f"{c.id} 的 related_cases 引用了不存在的案例 {rid}")
    for e in cases:
        if e.case_kind != "chain_judgment_e":
            continue
        if not any(e.id in c.related_cases and c.judgment == e.chain for c in cases):
            errors.append(
                f"链路{e.chain} 的判断E 案例 {e.id} 没有被 {e.chain}.json 里的案例用 related_cases 引用")
    e_cases = [c for c in cases if c.judgment == "E"]
    if e_cases:
        triggers = {c.trigger for c in e_cases}
        lack = [t for t in ("new_material_auto", "user_follow_up", "historical_replay") if t not in triggers]
        if lack:
            errors.append(f"判断 E 案例缺少触发方式 {lack}")
        outcomes = {c.expected_outcome for c in e_cases}
        lack = [o for o in ("maintain", "revise") if o not in outcomes]
        if lack:
            errors.append(f"判断 E 案例缺少预期结果 {lack}：维持和修订都要验")
    if errors:
        raise AcceptanceContractError("；".join(errors))
    return list(cases)


def score_units(cases: list[AcceptanceCase]) -> list[str]:
    """计分单位：每个案例编号只算一次。related_cases 只是引用，不产生新的计分单位，
    所以链路文件引用 E 案例不会让同一个 E 案例被算两遍。"""
    seen: dict[str, None] = {}
    for c in cases:
        seen.setdefault(c.id, None)
    return list(seen)


def real_coverage_cases(cases: list[AcceptanceCase]) -> list[AcceptanceCase]:
    """能算进“真实覆盖”的案例：在真实固定版本上跑通、无合成部分。合成材料只验证流程，永远不在这里。"""
    return [c for c in cases if c.counts_as_real_coverage]


def load_all(directory: str | Path) -> list[AcceptanceCase]:
    """读取目录下的 A–E 与 views 六个夹具文件，按文件顺序返回全部案例。

    要求恰好是这六个 JSON 文件：缺文件说明有判断没验，多文件说明有人绕开了约定的分组；
    同一案例编号只能在一个文件里定义，其他文件用 related_cases 引用；
    每个文件只能放对应判断的案例；跨文件的 id、design_ref 也不能重复。
    """
    d = Path(directory)
    present = {p.name for p in d.glob("*.json")}
    missing = sorted(set(EXPECTED_FILES) - present)
    if missing:
        raise AcceptanceContractError(f"缺少夹具文件：{missing}")
    extra = sorted(present - set(EXPECTED_FILES))
    if extra:
        raise AcceptanceContractError(f"未约定的夹具文件：{extra}")

    raw_by_file: dict[str, list[Any]] = {}
    files_by_id: dict[str, list[str]] = {}
    for name in EXPECTED_FILES:
        data = json.loads((d / name).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise AcceptanceContractError(f"{name} 顶层必须是案例列表")
        raw_by_file[name] = data
        for raw in data:
            if isinstance(raw, dict) and "id" in raw:
                files_by_id.setdefault(str(raw["id"]), []).append(name)
    # 先查跨文件：同一编号出现在两个文件里，就是同一案例被复制到别的链路重复计分
    multi = {k: v for k, v in files_by_id.items() if len(set(v)) > 1}
    if multi:
        detail = "；".join(f"{k} 出现在 {sorted(set(v))}" for k, v in sorted(multi.items()))
        raise AcceptanceContractError(
            f"案例编号只能在一个文件里定义，其他文件用 related_cases 引用：{detail}")

    all_cases: list[AcceptanceCase] = []
    for name, judgment in EXPECTED_FILES.items():
        for raw in raw_by_file[name]:
            case = validate_case(raw)
            if case.judgment != judgment:
                raise AcceptanceContractError(f"{name} 里出现了判断 {case.judgment} 的案例 {case.id}")
            all_cases.append(case)
    return validate_case_set(validate_cases(all_cases))
